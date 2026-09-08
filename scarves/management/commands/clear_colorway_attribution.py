"""Take the colorway off sale lines that were rung up under the wrong one.

**The case this exists for**: Square's Sash Belt variations were misconfigured
for a weekend, so 55 of 59 attributed units came back as `Amethyst` — one
colorway credited with sales belonging to forty-odd others. Nothing in the app
can detect which sale was really which, because the information was never
captured.

**Deleting the lines would be worse than keeping them wrong.** They carry four
things that are true — the units, the money, the date and the blank — and one
that is false. Deleting throws away all five to be rid of one, and it cannot
fix the part that actually costs anything: `number_on_hand` is stored rather
than derived, so removing rows puts nothing back on a shelf.

So this removes only the false claim. The line keeps its quantity, its money,
its timestamp and its blank; it loses its link to a finished product and its
price point, and **what Square originally said is written into `notes`** so
the claim is recorded rather than erased. The result is the honest statement:
this many of that blank sold, and nobody knows in which colour.

Every report already handles that shape. `slowsellers.sold_units` counts only
linked lines, so the wrongly-credited colorway stops leading the production
queue; `unattributed()` names the blank under the table; and the season and
category totals are untouched, because they never read the colorway.

**It does not touch `InventoryLog`, and must not.** Those decrements really
happened — a colorway's count really was reduced — and nothing in this app
deletes a stock movement. The count is wrong in a way only a physical count
can settle, which is what `private/bulk-inventory/` is for: pick the blank,
type what is on the shelf. An absolute count heals whatever went unrecorded,
which is the whole reason corrections here are absolutes rather than deltas.

    python manage.py clear_colorway_attribution --blank "Sash Belt" --year 2026
    python manage.py clear_colorway_attribution --blank "Sash Belt" \\
        --colorway Amethyst --year 2026 --apply

**Prefer `--colorway` when one colour swallowed the rest.** A partial
misconfiguration still lets some lines through correctly, and those few are
the only good colorway data of that weekend — clearing the blank wholesale
takes them with it. Run the dry run first and read the breakdown: one colour
holding nearly all the units with a handful of plausible singles beside it is
that shape.

Dry run unless `--apply`, because it is not reversible from here: the price
point moves into free text, and a re-import will not restore it — the lines
already exist and match on `line_key`, so they are skipped rather than
rewritten.

**There is a floor** (`--floor`, default 10). Below it the command refuses:
neutralizing a handful of units destroys real colorway data to remove a few
doubtful rows, which is the wrong side of the trade. The dry run also names
the weekends in scope, because running `--year` after the catalogue is fixed
would sweep up the weeks that came back correctly.
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves.models import FaireDay, SaleLine


class Command(BaseCommand):
    help = "Remove the colorway from sale lines a Square misconfiguration mislabelled."

    def add_arguments(self, parser):
        parser.add_argument(
            "--blank", required=True,
            help="Item name to scope to, e.g. 'Sash Belt'. Matched case-insensitively.",
        )
        parser.add_argument("--year", type=int, help="Scope to this faire season.")
        parser.add_argument("--from", dest="start", help="YYYY-MM-DD.")
        parser.add_argument("--to", dest="end", help="YYYY-MM-DD.")
        parser.add_argument(
            "--colorway",
            help=(
                "Only clear lines claiming this colorway. Use it when the "
                "misconfiguration named one colour and the rest came through "
                "correctly — those are the lines that worked, and clearing "
                "them destroys the only good data of the weekend."
            ),
        )
        parser.add_argument(
            "--floor", type=int, default=10,
            help=(
                "Refuse to run under this many units (default 10). A small "
                "neutralize is not worth doing: it destroys real colorway "
                "data to remove a handful of doubtful rows, which is the "
                "wrong side of the trade. Under the floor, leave it alone."
            ),
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it this only reports.",
        )

    def handle(self, *args, **options):
        start, end = self._window(options)
        lines = SaleLine.objects.filter(
            item_name__icontains=options["blank"],
            sold_at__date__gte=start, sold_at__date__lte=end,
        )
        # Only lines that make a colorway claim. One already carrying no price
        # point is already saying the honest thing.
        claimed = lines.exclude(price_point="").exclude(
            price_point__iexact="Regular").exclude(
            price_point__iexact="Regular Price")
        if options.get("colorway"):
            claimed = claimed.filter(price_point__iexact=options["colorway"])

        if not claimed.exists():
            which = (f" claiming {options['colorway']!r}"
                     if options.get("colorway") else "")
            self.stdout.write(self.style.WARNING(
                f"No colorway-carrying lines for {options['blank']!r}{which} "
                f"between {start} and {end}. Nothing to clear."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{options['blank']} — {start} to {end}"
        ))
        by_colorway = {}
        for line in claimed:
            entry = by_colorway.setdefault(line.price_point, [0, 0])
            entry[0] += 1
            entry[1] += float(line.quantity)
        for colorway, (rows, units) in sorted(
                by_colorway.items(), key=lambda kv: -kv[1][1]):
            self.stdout.write(f"  {units:>7.0f} units over {rows:>3} lines  {colorway}")

        total = sum(units for _rows, units in by_colorway.values())
        linked = claimed.filter(finished_product__isnull=False).count()

        # Which weekends this reaches, so over-reach is visible before it
        # happens. Running `--year` after the catalogue is fixed would sweep
        # up the weeks that came back correctly, and the breakdown is where
        # that shows.
        weekends = sorted({
            d.weekend for d in FaireDay.objects.filter(
                date__in=[line.sold_at.date() for line in claimed])
        })
        if weekends:
            self.stdout.write(
                f"\n  weekend{'' if len(weekends) == 1 else 's'} affected: "
                + ", ".join(str(w) for w in weekends)
            )
        self.stdout.write(
            f"\n  {claimed.count()} line(s), {total:.0f} units, "
            f"{linked} linked to a product."
        )
        self.stdout.write(
            "  Money, dates, quantities and the blank are untouched. "
            "InventoryLog is untouched — fix the counts with a stock take at "
            "private/bulk-inventory/."
        )

        # The floor is checked before the dry run reports success, so a run
        # that would be refused says so while it is still a question.
        if total < options["floor"]:
            raise CommandError(
                f"Only {total:.0f} unit(s) in scope, under the floor of "
                f"{options['floor']}. A neutralize this small destroys more "
                f"real colorway data than it removes doubtful rows — leave it. "
                f"Pass --floor to override deliberately."
            )

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run. Re-run with --apply to write."
            ))
            return

        with transaction.atomic():
            changed = 0
            for line in claimed:
                # The original claim is recorded rather than erased: it is what
                # Square said, and a repair that leaves no trace of what it
                # repaired is the silent kind.
                note = (f"Colorway attribution cleared: Square reported "
                        f"{line.price_point!r}, which was miscoded.")
                line.notes = note[:300]
                line.price_point = ""
                line.finished_product = None
                line.save(update_fields=["notes", "price_point", "finished_product"])
                changed += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nCleared the colorway on {changed} line(s). They now read as "
            f"{options['blank']} sold with no colour recorded."
        ))

    def _window(self, options):
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
            return start, end

        if not options.get("year"):
            raise CommandError("Give --year, or --from and --to.")
        days = list(
            FaireDay.objects.filter(faire__year=options["year"]).order_by("date")
        )
        if not days:
            raise CommandError(
                f"No faire days for {options['year']} — run generate_faire first."
            )
        return days[0].date, days[-1].date
