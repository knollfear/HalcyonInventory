"""Pull the sales ledger straight from Square, no export step.

This is the door `import_sales_history` should have been from the start. The
CSV path stays — it is how a file somebody already downloaded gets loaded, and
it needs no credentials — but the API is strictly better where it can be used:

- **Line items carry `catalog_object_id`**, so a line matches a local product
  by Square's own variation id rather than by guessing from a SKU that most
  historical lines do not have, or from an item name that only gets as far as
  the blank.
- **Nothing has to be remembered.** No dashboard, no date pickers, no
  download, no file. `--year 2021` reads the faire calendar for its dates.

Two things it cannot do that the export can, and both are handled rather than
ignored:

- **Order line items have no category on them.** Category decides whether the
  wax hands are in a season total, so it is resolved from Square's own
  catalogue in one pass at the start, and lines whose category could not be
  resolved are counted and named.
- **The Orders API only knows orders.** Very old transactions that predate it
  are not there at all. That is why the range is asked for explicitly and the
  line count is printed against the dashboard's own figure.

Read-only against Square: it calls `SearchOrders` and `ListCatalog`, and
writes nothing there.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scarves import salesimport
from scarves.models import Faire, Sale, SaleLine

#: Square's page size for SearchOrders. The API caps it; asking for more just
#: gets fewer back with a cursor, so this only decides how many round trips.
PAGE = 500

#: Guard against a paging bug turning into an unbounded loop against a live
#: API. A faire weekend is a few hundred orders; a whole season is thousands.
MAX_PAGES = 200

#: Object ids per BatchRetrieveCatalogObjects call. Square caps it at 1000.
BATCH = 500


class Command(BaseCommand):
    help = "Import sales from the Square Orders API into the reporting ledger. Moves no stock."

    def add_arguments(self, parser):
        parser.add_argument("--faire", default="labor-day-run", help="Event slug, for --year.")
        parser.add_argument(
            "--year", type=int, action="append", dest="years",
            help="Season to pull, using the faire calendar for its dates. Repeatable.",
        )
        parser.add_argument("--range", help="Inclusive span of seasons, e.g. 2021-2026.")
        parser.add_argument(
            "--current", action="store_true",
            help=(
                "The season that is running now, from its first day to today. "
                "Nothing to look up and nothing to type — this is the one to "
                "put on a schedule or run after a weekend."
            ),
        )
        parser.add_argument(
            "--since", type=int, metavar="DAYS",
            help=(
                "Only ask Square for the last DAYS days of the window. A "
                "weekly refresh does not need to re-read August every time; "
                "re-reading is harmless but it is thousands of orders for the "
                "handful that are new."
            ),
        )
        parser.add_argument("--from", dest="start", help="Explicit start date, YYYY-MM-DD.")
        parser.add_argument("--to", dest="end", help="Explicit end date, YYYY-MM-DD, inclusive.")
        parser.add_argument("--dry-run", action="store_true", help="Fetch and reconcile, write nothing.")
        parser.add_argument(
            "--force", action="store_true",
            help="Load orders even where another pipeline already supplied them. "
                 "Off by default — see salesimport.write for why the order is the unit.",
        )

    def handle(self, *args, **options):
        if not settings.SQUARE_ACCESS_TOKEN:
            raise CommandError("SQUARE_ACCESS_TOKEN is not set.")
        if not settings.SQUARE_LOCATION_ID:
            raise CommandError("SQUARE_LOCATION_ID is not set.")

        windows = self._windows(options)
        client = self._client()
        categories, category_names = self._categories(client)

        lines, skipped = [], Counter()
        for label, start, end in windows:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{label}: {start:%d %b %Y} to {end:%d %b %Y}"
            ))
            orders = self._orders(client, start, end)
            self.stdout.write(f"  {len(orders)} completed order(s)")
            for order in orders:
                lines.extend(self._lines(order, categories, skipped))

        if not lines:
            self.stdout.write(self.style.WARNING(
                "\nNo line items came back for that range. Square has no orders "
                "there, or they predate the Orders API."
            ))
            return

        matcher = salesimport.Matcher()
        for line in lines:
            matcher.attach(line)

        # Square's live catalogue only lists what is still in it, and a
        # season five years old is mostly things that are not. Asking about
        # those objects by id — with the deleted ones included — is a
        # different question from listing the catalogue, and it is the one
        # that gets a real answer. Without this the four base yarns came back
        # uncategorised, which put roughly $40k of a $48k yarn year into a
        # bucket labelled "(uncategorised)".
        missing = {
            line["square_variation_id"] for line in lines
            if line["square_variation_id"] and not line["category"]
        }
        if missing:
            recovered = self._deleted_categories(client, missing, category_names)
            found = 0
            for line in lines:
                if line["category"]:
                    continue
                label = recovered.get(line["square_variation_id"])
                if label:
                    line["category"] = label
                    found += 1
            if found:
                skipped[f"{found} categor(ies) recovered from catalogue objects "
                        "Square has since deleted"] = 0

        # Square's catalogue only describes what is *currently* in it, so a
        # line for something retired years ago comes back with no category.
        # Where this app knows the blank, its own category is the answer —
        # that is a classification somebody made here, not an inference from
        # the item name, so it is filled in rather than guessed at.
        filled = 0
        for line in lines:
            if line["category"] or not line.get("raw_product"):
                continue
            # Use Square's own name for the category where this app records
            # which one it is. Otherwise a season filter grows two pills for
            # one thing — "Silk Scarves" from Square and "Silk" from here.
            local = line["raw_product"].category
            line["category"] = (
                category_names.get(local.square_category_id or "") or local.name
            )
            filled += 1
        if filled:
            skipped[f"{filled} categor(ies) filled from this app's own catalogue, "
                    "for items Square no longer lists"] = 0

        uncategorised = sum(1 for line in lines if not line["category"])
        if uncategorised:
            skipped[f"{uncategorised} line(s) still with no category — neither "
                    "Square nor this app knows what they were"] = 0

        dry_run = options["dry_run"]
        result = salesimport.Result() if dry_run else salesimport.write(
            lines, Sale.SOURCE_SQUARE_API, force=options["force"],
        )
        salesimport.report(self, lines, skipped, matcher, result, dry_run)

    # ---------------------------------------------------------------- Square

    def _client(self):
        from square.client import Client
        return Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )

    def _orders(self, client, start, end):
        """Every COMPLETED order closed inside the window, paged through.

        Filtered and sorted on `closed_at` — Square requires the sort field to
        match the filter, and closed_at is when the money was taken rather
        than when the cart was opened.
        """
        zone = timezone.get_current_timezone()
        start_at = timezone.make_aware(datetime.combine(start, time.min), zone)
        end_at = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), zone)

        orders, cursor, pages = [], None, 0
        while pages < MAX_PAGES:
            body = {
                "location_ids": [settings.SQUARE_LOCATION_ID],
                "query": {
                    "filter": {
                        "date_time_filter": {
                            "closed_at": {
                                "start_at": start_at.isoformat(),
                                "end_at": end_at.isoformat(),
                            }
                        },
                        "state_filter": {"states": ["COMPLETED"]},
                    },
                    "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"},
                },
                "limit": PAGE,
            }
            if cursor:
                body["cursor"] = cursor
            result = client.orders.search_orders(body=body)
            if result.is_error():
                raise CommandError(f"Square refused the order search: {result.errors}")
            payload = result.body or {}
            orders.extend(payload.get("orders") or [])
            cursor = payload.get("cursor")
            pages += 1
            if not cursor:
                break
        else:
            raise CommandError(
                f"Stopped after {MAX_PAGES} pages — the cursor never ran out, "
                "which means the paging is wrong rather than the season being big."
            )
        return orders

    def _categories(self, client):
        """`(variation id → category name, category id → name)`, built once.

        Order lines carry no category and season totals need one, because the
        wax hands were on this till through 2024 and are gone. Resolved from
        Square's catalogue rather than from the local one: a line for
        something this app never knew about still needs a category, and that
        is exactly the group being excluded.
        """
        items, names, cursor = [], {}, None
        while True:
            result = client.catalog.list_catalog(cursor=cursor, types="ITEM,CATEGORY")
            if result.is_error():
                raise CommandError(f"Square refused the catalogue list: {result.errors}")
            payload = result.body or {}
            for obj in payload.get("objects") or []:
                if obj.get("type") == "CATEGORY":
                    names[obj["id"]] = (obj.get("category_data") or {}).get("name", "")
                elif obj.get("type") == "ITEM":
                    items.append(obj)
            cursor = payload.get("cursor")
            if not cursor:
                break

        by_variation = {}
        for item in items:
            data = item.get("item_data") or {}
            # `category_id` is the older shape and `categories` the newer one;
            # the API version in use still answers with either depending on how
            # the item was written, so both are read.
            label = _category_name(data, names)
            for variation in data.get("variations") or []:
                by_variation[variation.get("id")] = label
        return by_variation, names

    def _deleted_categories(self, client, variation_ids, names):
        """Category per variation id, for objects no longer in the catalogue.

        `ListCatalog` returns the living catalogue; this asks about specific
        objects and says `include_deleted_objects`, which is the only way to
        learn what a line sold in 2021 actually was. The parent item comes
        back as a related object, and the category hangs off that.
        """
        found = {}
        ids = sorted(variation_ids)
        for start in range(0, len(ids), BATCH):
            chunk = ids[start:start + BATCH]
            result = client.catalog.batch_retrieve_catalog_objects(body={
                "object_ids": chunk,
                "include_deleted_objects": True,
                "include_related_objects": True,
            })
            if result.is_error():
                raise CommandError(f"Square refused the catalogue lookup: {result.errors}")
            payload = result.body or {}
            items = {
                obj["id"]: obj for obj in payload.get("related_objects") or []
                if obj.get("type") == "ITEM"
            }
            # A category can be deleted too, in which case its name is not in
            # the list we already have; fall back to the id rather than to
            # blank, so the pill at least groups the lines together.
            for obj in payload.get("objects") or []:
                if obj.get("type") != "ITEM_VARIATION":
                    continue
                item = items.get((obj.get("item_variation_data") or {}).get("item_id"))
                if item is None:
                    continue
                found[obj["id"]] = _category_name(item.get("item_data") or {}, names)
        return {key: value for key, value in found.items() if value}

    # ----------------------------------------------------------------- lines

    def _lines(self, order, categories, skipped):
        order_id = order.get("id") or ""
        sold_at = self._when(order)
        if not order_id or sold_at is None:
            skipped["order with no id or no timestamp"] += 1
            return []

        tender = (order.get("tenders") or [{}])[0]
        card = ((tender.get("card_details") or {}).get("card") or {})
        seen = Counter()
        out = []

        for item in order.get("line_items") or []:
            name = item.get("name") or ""
            variation_name = item.get("variation_name") or ""
            variation_id = item.get("catalog_object_id") or ""

            # The same key shape the CSV path builds, so the two doors can be
            # compared line for line when checking one against the other.
            base = f"{name}|{variation_name}"
            seen[base] += 1
            gross = _cents(item.get("gross_sales_money"))
            discount = -_cents(item.get("total_discount_money"))

            out.append({
                "order_id": order_id,
                "sold_at": sold_at,
                "line_key": f"{base}|{seen[base]}",
                "event_type": SaleLine.PAYMENT,
                "category": categories.get(variation_id, ""),
                "item_name": name,
                "price_point": variation_name,
                "sku": "",
                "square_variation_id": variation_id,
                "quantity": Decimal(item.get("quantity") or "1"),
                "gross_cents": gross,
                "discount_cents": discount,
                "net_cents": gross + discount,
                "tax_cents": _cents(item.get("total_tax_money")),
                "location": order.get("location_id") or "",
                "device": "",
                "customer_name": "",
                "card_brand": card.get("card_brand") or "",
            })

        for refund in order.get("refunds") or []:
            # A refund is a line of its own rather than a negative adjustment
            # to the sale, so a season total can be read gross or net and the
            # page can say which it is showing.
            amount = -_cents(refund.get("amount_money"))
            out.append({
                "order_id": order_id,
                "sold_at": sold_at,
                "line_key": f"refund|{refund.get('id', '')}|1",
                "event_type": SaleLine.REFUND,
                "category": "",
                "item_name": refund.get("reason") or "Refund",
                "price_point": "",
                "sku": "",
                "square_variation_id": "",
                "quantity": Decimal(0),
                "gross_cents": amount,
                "discount_cents": 0,
                "net_cents": amount,
                "tax_cents": 0,
                "location": order.get("location_id") or "",
                "device": "",
                "customer_name": "",
                "card_brand": card.get("card_brand") or "",
            })
        return out

    def _when(self, order):
        for field in ("closed_at", "created_at"):
            raw = order.get(field)
            if not raw:
                continue
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
        return None

    # --------------------------------------------------------------- windows

    def _windows(self, options):
        if options.get("start") or options.get("end"):
            if not (options.get("start") and options.get("end")):
                raise CommandError("--from and --to go together.")
            try:
                start = datetime.strptime(options["start"], "%Y-%m-%d").date()
                end = datetime.strptime(options["end"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError(f"Could not read the dates: {exc}")
            if end < start:
                raise CommandError("--to is before --from.")
            return [("explicit range", start, end)]

        years = list(options.get("years") or [])
        if options.get("range"):
            try:
                first, last = (int(p) for p in options["range"].split("-", 1))
            except ValueError:
                raise CommandError(f"Could not read --range {options['range']!r}.")
            years.extend(range(first, last + 1))
        if options.get("current"):
            years.append(self._current_year(options["faire"]))
        if not years:
            raise CommandError(
                "Give --current, --year, --range, or --from and --to."
            )

        today = timezone.localdate()
        windows = []
        for year in sorted(set(years)):
            faire = Faire.objects.filter(slug=options["faire"], year=year).first()
            if faire is None:
                raise CommandError(
                    f"No {options['faire']} faire for {year}. Run "
                    f"generate_faire --year {year} first — the calendar is what "
                    "says which dates to ask Square for."
                )
            days = list(faire.days.order_by("date"))
            if not days:
                raise CommandError(f"{faire} has no days generated.")
            start, end = days[0].date, days[-1].date
            # Never ask for days that have not happened. It earns a 400 from
            # the archive-shaped APIs and buys nothing from this one.
            end = min(end, today)
            if options.get("since"):
                start = max(start, today - timedelta(days=options["since"]))
            if end < start:
                self.stdout.write(self.style.WARNING(
                    f"{faire}: nothing in range — it has not started yet."
                ))
                continue
            windows.append((str(faire), start, end))
        if not windows:
            raise CommandError("Every window resolved to nothing to fetch.")
        return windows

    def _current_year(self, slug):
        """The season running now, or the most recent one that has begun.

        Between seasons this deliberately answers with the last one rather
        than refusing: the reason to run this in October is a weekend just
        gone, and the reason to run it in February is to top up what that
        season finished with.
        """
        today = timezone.localdate()
        live = Faire.objects.filter(
            slug=slug, days__date__lte=today,
        ).order_by("-year").first()
        if live is None:
            raise CommandError(
                f"No {slug} faire has started yet. Run generate_faire first."
            )
        return live.year


def _cents(money):
    return int((money or {}).get("amount") or 0)


def _category_name(item_data, names):
    """The category label for an item, across the shapes Square answers in.

    `category_id` is the old field and is `None` on everything written
    recently; `reporting_category` is the one the dashboard's own reports use;
    `categories` is the list. All three are read because the catalogue has
    objects of every vintage in it.
    """
    category_id = item_data.get("category_id") or ""
    if not category_id:
        reporting = item_data.get("reporting_category") or {}
        category_id = reporting.get("id") or ""
    if not category_id:
        listed = item_data.get("categories") or []
        category_id = listed[0].get("id", "") if listed else ""
    if not category_id:
        return ""
    return names.get(category_id) or category_id
