"""Fill in the weather for a faire's trading days, once, from the archive.

See `scarves/weather.py` for the source and the two things worth knowing
about it — the five-day lag, and that cloud and humidity are averaged over
opening hours rather than the whole day.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as djtz

from scarves import weather
from scarves.models import DayWeather, Faire


class Command(BaseCommand):
    help = "Fetch and store daily weather for a faire's trading days."

    def add_arguments(self, parser):
        parser.add_argument("--faire", default="labor-day-run", help="Event slug.")
        parser.add_argument("--year", type=int, action="append", dest="years", help="Repeatable.")
        parser.add_argument("--range", help="Inclusive span, e.g. 2021-2026.")
        parser.add_argument(
            "--lat", type=float,
            help="Latitude, if the faire has none stored. Saved onto the faire when given.",
        )
        parser.add_argument("--lon", type=float, help="Longitude, likewise.")
        parser.add_argument(
            "--force", action="store_true",
            help="Re-fetch days that already have a reading. Off by default: the "
                 "weather on a past weekend does not change, so a re-run should "
                 "cost nothing and only fill what is missing.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Print what would be stored.")

    def handle(self, *args, **options):
        faires = self._faires(options)
        stored = pending = skipped = future = 0

        for faire in faires:
            latitude, longitude = self._where(faire, options)
            days = list(faire.days.order_by("date"))
            if not days:
                self.stdout.write(self.style.WARNING(
                    f"{faire}: no days generated — run generate_faire first."
                ))
                continue

            wanted = days if options["force"] else [
                day for day in days if not hasattr(day, "weather")
            ]
            already = len(days) - len(wanted)
            skipped += already

            # Three states, not two. A day still ahead has no weather because
            # it has not happened; a day just gone has none because the
            # archive lags about five days. Asking for either is what earns a
            # 400 from the API, and lumping them together would tell somebody
            # to "come back later" for a weekend in 2027.
            today = djtz.localdate()
            ahead = [day for day in wanted if day.date > today]
            wanted = [day for day in wanted if day.date <= today]
            if ahead:
                future += len(ahead)
                self.stdout.write(
                    f"{faire}: {len(ahead)} day(s) still ahead — nothing to fetch yet."
                )
            if not wanted:
                if not ahead:
                    self.stdout.write(f"{faire}: all {len(days)} days already on file.")
                continue

            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{faire} — {len(wanted)} day(s) to fetch"
                + (f", {already} already on file" if already else "")
            ))
            try:
                readings = weather.fetch(
                    latitude, longitude, wanted[0].date, wanted[-1].date,
                    djtz.get_current_timezone_name(),
                )
            except weather.WeatherUnavailable as exc:
                raise CommandError(str(exc))

            missing = []
            with transaction.atomic():
                for day in wanted:
                    reading = readings.get(day.date)
                    if reading is None:
                        missing.append(day)
                        continue
                    line = (
                        f"  {day.date:%a %d %b}  "
                        f"{reading['high_f']}/{reading['low_f']}°F  "
                        f"rain {reading['precipitation_in']}in  "
                        f"cloud {reading['cloud_pct']}%  hum {reading['humidity_pct']}%"
                    )
                    self.stdout.write(line)
                    if not options["dry_run"]:
                        DayWeather.objects.update_or_create(
                            day=day, defaults={**reading, "source": weather.SOURCE},
                        )
                    stored += 1

            if missing:
                pending += len(missing)
                self.stdout.write(self.style.WARNING(
                    f"  {len(missing)} day(s) the archive has no reading for yet: "
                    + ", ".join(f"{d.date:%d %b}" for d in missing)
                ))
                self.stdout.write(
                    "  It lags real time by about five days, so this is almost "
                    "always 'come back later' rather than a day it will never "
                    "have. Nothing was written for them."
                )

        self.stdout.write("")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing written."))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Done: {stored} day(s) stored, {skipped} already on file, "
            f"{pending} awaiting the archive, {future} still ahead."
        ))

    def _faires(self, options):
        years = list(options.get("years") or [])
        if options.get("range"):
            try:
                first, last = (int(p) for p in options["range"].split("-", 1))
            except ValueError:
                raise CommandError(f"Could not read --range {options['range']!r}.")
            years.extend(range(first, last + 1))
        found = Faire.objects.filter(slug=options["faire"])
        if years:
            found = found.filter(year__in=years)
        found = list(found.order_by("year"))
        if not found:
            raise CommandError(
                f"No faire matched slug {options['faire']!r}"
                + (f" and years {sorted(set(years))}" if years else "")
                + ". Run generate_faire first."
            )
        return found

    def _where(self, faire, options):
        """Coordinates for the fetch, stored on the faire once given.

        Deliberately not a zip code: turning one into a latitude and longitude
        means another service and another failure mode, for a value that is
        typed once per faire and never again. The archive grid is about nine
        kilometres across, so anywhere in the right town is the right answer.
        """
        latitude = options.get("lat") if options.get("lat") is not None else faire.latitude
        longitude = options.get("lon") if options.get("lon") is not None else faire.longitude
        if latitude is None or longitude is None:
            raise CommandError(
                f"{faire} has no coordinates. Pass --lat and --lon once and they "
                "are saved onto the faire (the Maryland Renaissance Festival "
                "sits at about --lat 39.0068 --lon -76.6000; confirm before "
                "trusting it)."
            )
        if (faire.latitude, faire.longitude) != (latitude, longitude):
            faire.latitude, faire.longitude = latitude, longitude
            faire.save(update_fields=["latitude", "longitude"])
        return latitude, longitude
