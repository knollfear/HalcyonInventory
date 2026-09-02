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
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scarves import salesimport, squareorders
from scarves.models import Faire, Sale


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
                "The season running now, from its first day to today. This is "
                "also what no arguments at all means, so the bare command is "
                "the one to run after a weekend and the one to schedule."
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
        # The module raises its own exception because a command is only one of
        # the things that may call it; translating here is the command's job.
        try:
            categories, category_names = squareorders.category_index(client)
        except squareorders.SquareUnavailable as exc:
            raise CommandError(str(exc))

        lines, skipped = [], Counter()
        for label, start, end in windows:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{label}: {start:%d %b %Y} to {end:%d %b %Y}"
            ))
            try:
                orders = squareorders.search(client, start, end)
            except squareorders.SquareUnavailable as exc:
                raise CommandError(str(exc))
            self.stdout.write(f"  {len(orders)} completed order(s)")
            for order in orders:
                lines.extend(squareorders.lines_from_order(order, categories, skipped))

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
            try:
                recovered = squareorders.categories_for_deleted(
                    client, missing, category_names,
                )
            except squareorders.SquareUnavailable as exc:
                raise CommandError(str(exc))
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
        """Kept as a seam the tests patch; the work itself lives in the module."""
        return squareorders.client()


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
        # No arguments means the season running now. Naming a year is still
        # the simple way to say it and always works — but the bare command
        # has to mean something useful, because the alternative is a
        # scheduled `--year 2026` that keeps exiting 0 in 2027 while quietly
        # covering nothing.
        if options.get("current") or not years:
            years.append(self._current_year(options["faire"]))

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


