"""What sold, per finished product, over a date range.

The first report in the app that reads the `InventoryLog` as a *dataset*
rather than as the audit trail of one product. Everything here is a read —
nothing in this module writes a row.

**One row is one finished product**, which is blank × colorway — the axis the
whole catalogue is organised on, so "what sold" and "what to dye next" line up
without anybody translating between them.

Scope is `log_type=SALE` and nothing else. That is narrower than "stock that
left the tent" and deliberately so: a Sunday close writes an *adjustment* when
the count disagrees, and some of those really are sales nobody registered, but
the app does not know which — folding them in would put a guess in the same
column as a till receipt with nothing on the page to tell them apart. The
close's own numbers already have a page (`close_history`); this one answers
"what did we ring up".

Two properties of the date column are worth knowing before reading a total,
and both are stated on the page rather than hidden here:

- **The date is when the row was written, not always when the sale happened.**
  A webhook row lands within seconds, so those agree. `import_square_sales`
  stamps the row at import time, so a CSV loaded on Monday piles Saturday's
  sales onto Monday. A resolved unidentified sale is the exception that goes
  the other way — it is back-dated to Square's own sale time.
- **A sale that never reached the app is not here.** That is the same silence
  the close exists to catch, and the reason the source breakdown is printed:
  a day of `square_import` rows where `square_webhook` ones normally sit is a
  dropped integration, visible without going near Square.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import InventoryLog


#: The presets, in the order they appear. `today` and `yesterday` are resolved
#: at request time rather than being links to fixed dates: somebody who taps
#: "today" and sends the link means today, and a frozen `?from=2026-08-29`
#: would quietly mean something else by tomorrow.
RANGES = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("all", "All time"),
    ("custom", "Choose dates"),
]
RANGE_KEYS = {key for key, _ in RANGES}
DEFAULT_RANGE = "today"


class DateRange:
    """A resolved range: two inclusive local dates, or None for all time."""

    def __init__(self, key, start, end, label):
        self.key = key
        self.start = start
        self.end = end
        self.label = label

    @property
    def is_all_time(self):
        return self.start is None and self.end is None

    def querystring(self):
        """Just this range's parameters, for building a link that keeps it."""
        params = [("range", self.key)]
        if self.key == "custom":
            if self.start:
                params.append(("from", self.start.isoformat()))
            if self.end:
                params.append(("to", self.end.isoformat()))
        return params


def _parse_date(text):
    """A `YYYY-MM-DD` off the query string, or None.

    Refuses rather than guesses, the same way `parse_card_date` does — a
    range built from a half-read date is a total for days nobody asked about,
    and it looks exactly like a real one.
    """
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def resolve_range(params):
    """Turn the query string into a `DateRange`.

    `from`/`to` on their own mean a custom range — the dates are the form
    fields the page posts, so a hand-built link with only those in it works
    without knowing about `range` at all.
    """
    today = timezone.localdate()
    start = _parse_date(params.get("from"))
    end = _parse_date(params.get("to"))

    key = params.get("range", "")
    if key not in RANGE_KEYS:
        key = "custom" if (start or end) else DEFAULT_RANGE

    if key == "today":
        return DateRange(key, today, today, "Today")
    if key == "yesterday":
        day = today - timedelta(days=1)
        return DateRange(key, day, day, "Yesterday")
    if key in ("7", "30"):
        days = int(key)
        # Inclusive of today, so "last 7 days" is a week ending now rather
        # than a week ending last night.
        return DateRange(key, today - timedelta(days=days - 1), today,
                         f"Last {days} days")
    if key == "all":
        return DateRange(key, None, None, "All time")

    # Custom. A missing half is open-ended rather than an error: "everything
    # since the faire opened" is a real question and needs no end date.
    if start and end and end < start:
        start, end = end, start
    if start and end:
        label = f"{start:%d %b %Y} – {end:%d %b %Y}"
    elif start:
        label = f"Since {start:%d %b %Y}"
    elif end:
        label = f"Up to {end:%d %b %Y}"
    else:
        label = "All time"
    return DateRange("custom", start, end, label)


def _window(rng):
    """The half-open datetime window a `DateRange` selects.

    Half-open on purpose: `created_at < midnight tomorrow` takes the whole of
    the last day whatever the clock says, where `<= end` on a date would drop
    everything sold after midnight-minus-one-microsecond, i.e. all of it.
    """
    tz = timezone.get_current_timezone()
    lower = upper = None
    if rng.start:
        lower = timezone.make_aware(datetime.combine(rng.start, time.min), tz)
    if rng.end:
        upper = timezone.make_aware(
            datetime.combine(rng.end + timedelta(days=1), time.min), tz
        )
    return lower, upper


def sale_logs(rng):
    """Every sale row in the range, before any grouping."""
    qs = InventoryLog.objects.filter(log_type=InventoryLog.SALE)
    lower, upper = _window(rng)
    if lower:
        qs = qs.filter(created_at__gte=lower)
    if upper:
        qs = qs.filter(created_at__lt=upper)
    return qs


def narrow(logs, q="", category_id=None, raw_product_id=None):
    """Apply the column filters.

    Blank and category are the two that pay: a blank is the style half of
    `BLANK-DYEBATH` and a category is which table at the stall, so "what sold
    on the yarn table this weekend" is one select each and no typing.
    """
    if category_id:
        logs = logs.filter(
            finished_product__raw_product__category_id=category_id
        )
    if raw_product_id:
        logs = logs.filter(finished_product__raw_product_id=raw_product_id)
    if q:
        logs = logs.filter(
            Q(finished_product__name__icontains=q)
            | Q(finished_product__sku__icontains=q)
            | Q(finished_product__recipe__name__icontains=q)
        )
    return logs


def product_rows(logs):
    """One row per finished product: what sold, and what is left.

    Sales are stored negative, so `units` comes back negated — a refund
    booked as a positive sale row correctly *reduces* the total rather than
    adding to it.

    **Transactions is not a row count.** A dye bath yields several units of
    one SKU and they leave in ones and twos, so "12 sold across 11 sales" and
    "12 sold in one" are different facts about the same product and only one
    of them is a colorway with a following. Rows carrying no order reference
    count one each: nothing says two of them were the same sale, and assuming
    so would quietly deflate the number.
    """
    from .models import FinishedProduct

    agg = list(
        logs.values("finished_product")
        .annotate(
            signed=Sum("quantity"),
            orders=Count(
                "sale_reference", distinct=True, filter=~Q(sale_reference="")
            ),
            loose=Count("id", filter=Q(sale_reference="")),
            days=Count(TruncDate("created_at"), distinct=True),
            last_sold=Max("created_at"),
        )
    )
    products = {
        p.pk: p
        for p in FinishedProduct.objects.filter(
            pk__in=[a["finished_product"] for a in agg]
        ).select_related("raw_product", "recipe")
    }

    rows = []
    for a in agg:
        product = products.get(a["finished_product"])
        if product is None:          # PROTECTed, so this cannot happen
            continue
        units = -(a["signed"] or 0)
        par = product.par or 0
        rows.append({
            "product": product,
            "units": units,
            "transactions": a["orders"] + a["loose"],
            "days": a["days"],
            "last_sold": a["last_sold"],
            "on_hand": product.number_on_hand,
            "par": par,
            # Clamped, because "how far below par" is the question production
            # asks and a product sitting above par is not minus-three short.
            # Overshoot is bath-size rounding and means nothing here.
            "short": max(par - product.number_on_hand, 0),
            "value": units * (product.price or Decimal("0")),
        })
    return rows


#: Sortable columns. The value is what to sort on; every one of them is a
#: number or a string already on the row, so the sort happens in Python — a
#: few hundred products, and it keeps the derived columns (shortfall, value)
#: sortable on the same terms as the queried ones instead of being the two
#: headings that mysteriously aren't links.
SORTS = {
    "units": lambda r: r["units"],
    "transactions": lambda r: r["transactions"],
    "days": lambda r: r["days"],
    "value": lambda r: r["value"],
    "on_hand": lambda r: r["on_hand"],
    "short": lambda r: r["short"],
    "last": lambda r: r["last_sold"],
    "name": lambda r: r["product"].name.casefold(),
    "blank": lambda r: r["product"].raw_product.name.casefold(),
    "colorway": lambda r: (
        r["product"].recipe.name.casefold() if r["product"].recipe else ""
    ),
}
DEFAULT_SORT = "units"
#: Which way round a column reads first. A ranking wants its biggest number at
#: the top; a name wants A first. Getting this backwards means every column is
#: one wasted click away from what was wanted.
DESCENDING_FIRST = {"units", "transactions", "days", "value", "on_hand",
                    "short", "last"}


def sort_rows(rows, sort, descending):
    """Sort in place-ish, always breaking ties by name.

    The tie-break is what stops two equal rows swapping places between one
    page load and the next — a table that shuffles under a filter change
    reads as though the numbers moved.
    """
    key = SORTS.get(sort) or SORTS[DEFAULT_SORT]
    rows = sorted(rows, key=lambda r: r["product"].name.casefold())
    return sorted(rows, key=key, reverse=descending)


def totals(rows):
    """The strip above the table.

    Absolute counts. **Units and transactions are not divided into each
    other**: an average basket over a stall's worth of colorways is a number
    that moves for reasons nobody can act on, and printing it beside the two
    that can be acted on is how those stop being read.
    """
    return {
        "products": len(rows),
        "units": sum(r["units"] for r in rows),
        "transactions": sum(r["transactions"] for r in rows),
        "value": sum((r["value"] for r in rows), Decimal("0")),
    }


def by_source(logs):
    """How the range's sales came to be recorded, biggest first.

    Printed because the totals above it cannot say this and it changes what
    they mean. A day carrying `square_import` rows where `square_webhook`
    ones normally sit is a webhook that stopped delivering and a CSV loaded
    afterwards; `test` rows are a simulated sale somebody left behind. Both
    read as ordinary sales in every other column.
    """
    labels = dict(InventoryLog.SOURCE_CHOICES)
    counts = (
        logs.values("source")
        .annotate(units=Sum("quantity"), entries=Count("id"))
        .order_by()
    )
    out = [
        {
            "source": c["source"],
            "label": labels.get(c["source"], "") or "Recorded before this was tracked",
            "units": -(c["units"] or 0),
            "entries": c["entries"],
        }
        for c in counts
    ]
    return sorted(out, key=lambda c: -c["units"])
