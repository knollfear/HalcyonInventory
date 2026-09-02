"""Load Square's itemised CSV export into the sales ledger.

**This is not `import_square_sales`, and running the wrong one is the mistake
this docstring exists to prevent.** That command moves stock: it decrements
`number_on_hand` and writes an `InventoryLog` row, and it is part of the
inventory pipeline. This one writes `Sale` and `SaleLine` and **touches no
stock at all**. It is a reporting ledger — the record of what the till took,
loaded for seasons that in most cases ended years before this app existed.
Pointing it at a fresh weekend does not book those sales; pointing the other
one at 2021 would wreck the counts.

Three decisions in here are worth knowing before changing anything:

- **A line is identified by item and price point, never by SKU.** Twenty of
  the thirty-six lines in a 2026 export carry no SKU, and older seasons carry
  fewer still. Keying on it would drop most of the history silently.
- **Square's `Token` column is not a line id.** It repeats across orders for
  the same product — three separate triangle-fringe sales share one token — so
  keying on it collapses them into one and quietly loses revenue.
- **An unknown time zone stops the run.** Hour-of-day is one of the questions
  this ledger exists to answer, and guessing the zone shifts every answer by
  hours without anything looking wrong.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves import seasons, skus
from scarves.models import FaireDay, FinishedProduct, RawProduct, Sale, SaleLine

#: Square writes zone names in its own dialect. Mapped explicitly rather than
#: parsed, because the failure of a near-miss is a whole catalogue of sales
#: landing an hour out with nothing to show for it.
TIME_ZONES = {
    "Eastern Time (US & Canada)": "America/New_York",
    "Central Time (US & Canada)": "America/Chicago",
    "Mountain Time (US & Canada)": "America/Denver",
    "Pacific Time (US & Canada)": "America/Los_Angeles",
    "Arizona": "America/Phoenix",
    "Alaska": "America/Anchorage",
    "Hawaii": "Pacific/Honolulu",
    "UTC": "UTC",
}

MONEY = re.compile(r"[^0-9.\-]")


def money_cents(raw: str) -> int:
    """`'-$12.50'` → `-1250`. Blank is zero; anything unreadable raises."""
    text = (raw or "").strip()
    if not text:
        return 0
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    cleaned = MONEY.sub("", text).lstrip("-")
    if not cleaned:
        return 0
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"could not read {raw!r} as money")
    cents = int((value * 100).quantize(Decimal("1")))
    return -cents if negative else cents


class Command(BaseCommand):
    help = "Import a Square itemised CSV export into the Sale/SaleLine reporting ledger. Moves no stock."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to Square's itemised transactions export.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read, match and reconcile, then write nothing.",
        )
        parser.add_argument(
            "--source",
            default=Sale.SOURCE_SQUARE_CSV,
            choices=[key for key, _label in Sale.SOURCE_CHOICES],
            help="Which pipeline this file came from. Recorded, never branched on.",
        )

    def handle(self, *args, **options):
        rows = self._read(options["csv_file"])
        if not rows:
            raise CommandError("The file has no rows.")

        lines, skipped = self._parse(rows)
        matcher = _Matcher()
        for line in lines:
            matcher.attach(line)

        if options["dry_run"]:
            self._report(lines, skipped, matcher, written=None)
            self.stdout.write(self.style.WARNING("\nDRY RUN — nothing written."))
            return

        written, already = self._write(lines, options["source"])
        self._report(lines, skipped, matcher, written=(written, already))

    # ---------------------------------------------------------------- reading

    def _read(self, path):
        try:
            with open(path, newline="", encoding="utf-8-sig") as handle:
                return list(csv.DictReader(handle))
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")

    def _parse(self, rows):
        """Turn CSV rows into line dicts, counting what could not be read.

        A row missing an order id or a date is skipped and counted rather than
        guessed at — but the count is printed, because a silent skip is how a
        short total gets mistaken for a quiet season.
        """
        lines = []
        skipped = Counter()
        # Square aggregates identical items within an order onto one line, but
        # nothing promises it always will. The occurrence counter keeps a
        # second identical line addressable instead of overwriting the first.
        seen = defaultdict(int)

        for number, row in enumerate(rows, start=2):
            order_id = (row.get("Transaction ID") or "").strip()
            if not order_id:
                skipped["no transaction id"] += 1
                continue

            try:
                sold_at = self._when(row)
            except CommandError:
                raise
            except ValueError as exc:
                skipped[f"unreadable date ({exc})"] += 1
                continue

            item = (row.get("Item") or "").strip()
            price_point = (row.get("Price Point Name") or "").strip()
            key_base = f"{item}|{price_point}"
            seen[(order_id, key_base)] += 1
            line_key = f"{key_base}|{seen[(order_id, key_base)]}"

            try:
                gross = money_cents(row.get("Gross Sales"))
                discount = money_cents(row.get("Discounts"))
                net = money_cents(row.get("Net Sales"))
                tax = money_cents(row.get("Tax"))
            except ValueError as exc:
                skipped[f"row {number}: {exc}"] += 1
                continue

            try:
                quantity = Decimal((row.get("Qty") or "1").strip() or "1")
            except InvalidOperation:
                skipped["unreadable quantity"] += 1
                continue

            raw_event = (row.get("Event Type") or "").strip()
            event = SaleLine.REFUND if "refund" in raw_event.lower() else SaleLine.PAYMENT

            lines.append({
                "order_id": order_id,
                "sold_at": sold_at,
                "line_key": line_key,
                "event_type": event,
                "raw_event": raw_event,
                "category": (row.get("Category") or "").strip(),
                "item_name": item,
                "price_point": price_point,
                "sku": (row.get("SKU") or "").strip(),
                "quantity": quantity,
                "gross_cents": gross,
                "discount_cents": discount,
                "net_cents": net,
                "tax_cents": tax,
                "location": (row.get("Location") or "").strip(),
                "device": (row.get("Device Name") or "").strip(),
                "customer_name": (row.get("Customer Name") or "").strip(),
                "card_brand": (row.get("Card Brand") or "").strip(),
            })

        return lines, skipped

    def _when(self, row):
        zone_name = (row.get("Time Zone") or "").strip()
        if zone_name not in TIME_ZONES:
            raise CommandError(
                f"Unknown time zone {zone_name!r}. Add it to TIME_ZONES in this "
                "command rather than letting it fall back — every hour-of-day "
                "figure in the reporting depends on this being right, and a "
                "wrong zone looks exactly like a correct one."
            )
        stamp = f"{(row.get('Date') or '').strip()} {(row.get('Time') or '').strip()}"
        naive = datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M:%S")
        return naive.replace(tzinfo=ZoneInfo(TIME_ZONES[zone_name]))

    # ---------------------------------------------------------------- writing

    def _write(self, lines, source):
        written = already = 0
        by_order = defaultdict(list)
        for line in lines:
            by_order[line["order_id"]].append(line)

        with transaction.atomic():
            for order_id, order_lines in by_order.items():
                first = order_lines[0]
                sale, _made = Sale.objects.get_or_create(
                    order_id=order_id,
                    defaults={
                        "sold_at": first["sold_at"],
                        "location": first["location"],
                        "device": first["device"],
                        "customer_name": first["customer_name"],
                        "card_brand": first["card_brand"],
                        "source": source,
                    },
                )
                for line in order_lines:
                    _row, made = SaleLine.objects.get_or_create(
                        sale=sale,
                        line_key=line["line_key"],
                        defaults={
                            "sold_at": line["sold_at"],
                            "event_type": line["event_type"],
                            "category": line["category"],
                            "item_name": line["item_name"],
                            "price_point": line["price_point"],
                            "sku": line["sku"],
                            "quantity": line["quantity"],
                            "gross_cents": line["gross_cents"],
                            "discount_cents": line["discount_cents"],
                            "net_cents": line["net_cents"],
                            "tax_cents": line["tax_cents"],
                            "finished_product": line.get("finished_product"),
                            "raw_product": line.get("raw_product"),
                            "source": source,
                        },
                    )
                    written += made
                    already += not made
        return written, already

    # -------------------------------------------------------------- reporting

    def _report(self, lines, skipped, matcher, written):
        out = self.stdout
        out.write(self.style.MIGRATE_HEADING("\nRead"))
        out.write(f"  {len(lines)} lines across {len({l['order_id'] for l in lines})} orders")
        if lines:
            first = min(l["sold_at"] for l in lines)
            last = max(l["sold_at"] for l in lines)
            out.write(f"  {first:%d %b %Y} to {last:%d %b %Y}")
        for reason, count in skipped.items():
            out.write(self.style.WARNING(f"  skipped {count}: {reason}"))

        events = Counter(l["raw_event"] or "(blank)" for l in lines)
        if len(events) > 1 or SaleLine.REFUND in {l["event_type"] for l in lines}:
            out.write("  event types: " + ", ".join(f"{k} ×{v}" for k, v in events.items()))

        out.write(self.style.MIGRATE_HEADING("\nMoney, as the file states it"))
        by_category = defaultdict(int)
        for line in lines:
            by_category[line["category"] or "(uncategorised)"] += line["net_cents"]
        for name, cents in sorted(by_category.items(), key=lambda kv: -kv[1]):
            out.write(f"  {name:<28} {self._usd(cents):>12}")
        total = sum(l["net_cents"] for l in lines)
        out.write(f"  {'NET TOTAL':<28} {self._usd(total):>12}")
        out.write(self.style.HTTP_INFO(
            "  ↑ reconcile this against Square's own dashboard for the same range "
            "before trusting anything built on it."
        ))

        out.write(self.style.MIGRATE_HEADING("\nMatched to the catalogue"))
        out.write(f"  {matcher.by_sku} lines matched a product by SKU")
        out.write(f"  {matcher.by_item} more matched a blank by item name")
        if matcher.unmatched:
            out.write(self.style.WARNING(
                f"  {sum(matcher.unmatched.values())} lines matched nothing:"
            ))
            for name, count in matcher.unmatched.most_common():
                out.write(self.style.WARNING(f"      {name or '(no item name)'} ×{count}"))
            out.write(
                "    Unmatched lines are kept in full — item and price point are "
                "text on the row, so they still count toward every total."
            )

        out.write(self.style.MIGRATE_HEADING("\nPlaced in a season"))
        placed = Counter()
        known = {day.date: day for day in FaireDay.objects.all()}
        for line in lines:
            day = known.get(line["sold_at"].date())
            if day is None:
                placed[seasons.labor_day_season_for(line["sold_at"].date())] += 1
            else:
                placed[day.faire.year] += 1
        for year, count in sorted(placed.items(), key=lambda kv: (kv[0] is None, kv[0])):
            if year is None:
                out.write(self.style.WARNING(
                    f"  {count} lines fall outside any faire — kept, and excluded "
                    "from season reporting by construction."
                ))
            else:
                generated = year in {day.faire.year for day in known.values()}
                note = "" if generated else "  (run generate_faire --year %d)" % year
                out.write(f"  {year}: {count} lines{note}")

        if written is not None:
            added, existing = written
            out.write("")
            out.write(self.style.SUCCESS(
                f"Written: {added} new lines, {existing} already on file and left alone."
            ))
            out.write("No stock was moved and no InventoryLog row was written.")

    @staticmethod
    def _usd(cents):
        return f"${cents / 100:,.2f}"


class _Matcher:
    """Links a line to the catalogue, by SKU first and item name second.

    Both lookups are built once. The queue this replaces asked a
    catalogue-sized question per row; a hundred-thousand-line backfill would
    make that a hundred thousand queries.
    """

    def __init__(self):
        self.products = {
            product.sku: product
            for product in FinishedProduct.objects.select_related("raw_product")
            if product.sku
        }
        self.blanks = {}
        for blank in RawProduct.objects.all():
            self.blanks.setdefault(skus.slug(blank.name), blank)
        self.by_sku = 0
        self.by_item = 0
        self.unmatched = Counter()

    def attach(self, line):
        product = self.products.get(line["sku"]) if line["sku"] else None
        if product is not None:
            line["finished_product"] = product
            line["raw_product"] = product.raw_product
            self.by_sku += 1
            return
        blank = self.blanks.get(skus.slug(line["item_name"]))
        if blank is not None:
            line["finished_product"] = None
            line["raw_product"] = blank
            self.by_item += 1
            return
        line["finished_product"] = None
        line["raw_product"] = None
        self.unmatched[line["item_name"]] += 1
