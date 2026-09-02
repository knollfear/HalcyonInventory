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

from scarves import salesimport
from scarves.models import Sale, SaleLine

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
        matcher = salesimport.Matcher()
        for line in lines:
            matcher.attach(line)

        dry_run = options["dry_run"]
        result = salesimport.Result() if dry_run else salesimport.write(
            lines, options["source"],
        )
        salesimport.report(self, lines, skipped, matcher, result, dry_run)

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
                # The itemised export carries no catalog object id — which is
                # the main reason the Orders API path exists. Square's `Token`
                # column looks like one and is not: it names the product, so
                # three separate sales of a triangle fringe share it.
                "square_variation_id": "",
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
        return salesimport.write(lines, source)
