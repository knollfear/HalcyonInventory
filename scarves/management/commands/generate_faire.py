"""Create a faire's trading days.

For a faire whose dates follow a rule, they are generated — see
`scarves/seasons.py` for the Labor Day rule and why it is a function rather
than a table somebody types every year.

For a faire whose dates are announced, pass them with `--dates`. They are
still not typed twice: the weekend numbering is derived from the gaps between
them, so Saturday–Sunday is one weekend and Saturday–Sunday–Monday is too.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves import seasons
from scarves.models import Faire, FaireDay


class Command(BaseCommand):
    help = "Create (or top up) a faire's trading days, from its rule or from a list of dates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--faire",
            default=seasons.DEFAULT_FAIRE_SLUG,
            help=f"Event slug. Defaults to {seasons.DEFAULT_FAIRE_SLUG!r}.",
        )
        parser.add_argument(
            "--year", type=int, action="append", dest="years",
            help="Season to generate. Repeatable.",
        )
        parser.add_argument(
            "--range",
            help=(
                "Inclusive span, e.g. 2021-2026. 2020 is skipped: there was no "
                "faire, and an absent season records that better than a run of zeroes."
            ),
        )
        parser.add_argument(
            "--dates",
            help=(
                "Comma-separated YYYY-MM-DD list, for a faire whose dates are "
                "announced rather than derived. Implies one year and a manual rule."
            ),
        )
        parser.add_argument("--name", default="", help="Label, applied only when creating the faire.")
        parser.add_argument("--dry-run", action="store_true", help="Print the calendar without writing it.")

    #: No faire happened. Generating one would put nineteen days of guaranteed
    #: zero into every average that walks the seasons.
    SKIP_YEARS = {2020}

    def handle(self, *args, **options):
        if options["dates"]:
            return self._manual(options)
        return self._generated(options)

    # ------------------------------------------------------------- generated

    def _generated(self, options):
        years = self._years(options)
        slug = options["faire"]
        created_faires = created_days = existing_days = 0

        for year in years:
            if year in self.SKIP_YEARS:
                self.stdout.write(self.style.WARNING(f"{year}: skipped — no faire was held."))
                continue

            faire = Faire.objects.filter(slug=slug, year=year).first()
            rule = faire.rule if faire else seasons.LABOR_DAY_RULE
            if rule == seasons.MANUAL:
                raise CommandError(
                    f"{slug} {year} has its dates entered rather than derived. "
                    "Pass them with --dates, or add them in the admin."
                )
            try:
                days = seasons.days_for(rule, year)
            except ValueError as exc:
                raise CommandError(str(exc))

            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{slug} {year} — Labor Day {seasons.labor_day(year):%a %d %b}, "
                f"week 1 opens {seasons.week_one_saturday(year):%a %d %b}, "
                f"{len(days)} trading days"
            ))
            if options["dry_run"]:
                for when, weekend, monday in days:
                    self.stdout.write(
                        f"  wk{weekend}  {when:%a %d %b %Y}" + ("   ← Labor Day" if monday else "")
                    )
                continue

            made, added, existing = self._write(slug, year, options["name"], rule, days)
            created_faires += made
            created_days += added
            existing_days += existing

        return self._finish(options, created_faires, created_days, existing_days)

    # ---------------------------------------------------------------- manual

    def _manual(self, options):
        years = options.get("years") or []
        if options.get("range"):
            raise CommandError("--dates covers one faire; --range makes no sense with it.")
        try:
            entered = [date.fromisoformat(part.strip()) for part in options["dates"].split(",") if part.strip()]
        except ValueError as exc:
            raise CommandError(f"Could not read --dates: {exc}")
        if not entered:
            raise CommandError("--dates was empty.")

        implied = {when.year for when in entered}
        if len(implied) > 1 and not years:
            raise CommandError(
                f"Those dates span {sorted(implied)}. Give --year to say which season they belong to."
            )
        year = years[0] if years else implied.pop()

        days = [(when, weekend, False) for when, weekend in seasons.group_into_weekends(entered)]
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{options['faire']} {year} — {len(days)} trading days entered, "
            f"{days[-1][1]} weekend(s)"
        ))
        if options["dry_run"]:
            for when, weekend, _monday in days:
                self.stdout.write(f"  wk{weekend}  {when:%a %d %b %Y}")
            return self._finish(options, 0, 0, 0)

        made, added, existing = self._write(
            options["faire"], year, options["name"], seasons.MANUAL, days
        )
        return self._finish(options, made, added, existing)

    # ----------------------------------------------------------------- shared

    def _write(self, slug, year, name, rule, days):
        with transaction.atomic():
            faire, made = Faire.objects.get_or_create(
                slug=slug, year=year, defaults={"name": name, "rule": rule},
            )
            added = existing = 0
            for when, weekend, monday in days:
                # A day already on file is left exactly as it is, including a
                # `traded` flag somebody unticked. Regenerating a season must
                # never quietly re-open a day that was washed out.
                _day, day_made = FaireDay.objects.get_or_create(
                    date=when,
                    defaults={"faire": faire, "weekend": weekend, "is_labor_day": monday},
                )
                added += day_made
                existing += not day_made
            self.stdout.write(
                f"  {'created' if made else 'already on file'}; "
                f"{faire.days.count()} days, {faire.trading_days} traded"
            )
        return made, added, existing

    def _finish(self, options, faires, days, existing):
        self.stdout.write("")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing written."))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Done: {faires} faires created, {days} days added, "
            f"{existing} already on file and left alone."
        ))

    def _years(self, options):
        years = list(options.get("years") or [])
        span = options.get("range")
        if span:
            try:
                first, last = (int(part) for part in span.split("-", 1))
            except ValueError:
                raise CommandError(f"Could not read --range {span!r}; expected e.g. 2021-2026.")
            if last < first:
                raise CommandError(f"--range {span!r} ends before it starts.")
            years.extend(range(first, last + 1))
        if not years:
            raise CommandError("Give --year or --range; there is no sensible default.")
        return sorted(set(years))
