"""Season against season, indexed on the weekend of the run.

The page this feeds (`private/seasons/`) answers the question the old React
site was built for and could not keep answering: is this season ahead of the
last one, and by how much. Nothing here writes.

Three things make it different from `sales.py`, which reports over calendar
dates:

- **The axis is the weekend of the run, never the date.** Labor Day moves, so
  week 1 drifts across six calendar days between seasons and a date-to-date
  comparison lines up the wrong days. `scarves/seasons.py` holds the rule.
- **It reads `SaleLine`, not `InventoryLog`.** The stock log has no money on
  it and stamps rows when they were written; see the `Sale` docstring for the
  four reasons it cannot answer this.
- **Per trading day is a first-class measure, not a derived nicety.** Weekend
  2 carries Labor Day Monday, so its weekly total is about a third higher than
  its neighbours for a reason that has nothing to do with trade. Across
  2021-2024 it ranks first or second by weekly total and sixth or eighth per
  day. A page offering only the weekly figure would be read, and acted on.

**A weekend that has not happened is not a weekend with no sales**, and the
difference decides whether a number may be projected. A weekend whose trading
days are all still in the future is *to come* and can be projected from the
shape of the seasons behind it. A weekend in the past with no lines against it
is a **gap** — most likely nobody imported it — and is reported as one rather
than quietly filled in, because a projection sitting where a missing import
should be is a number that looks like evidence.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Faire, FaireDay, SaleLine

#: How the y axis is measured.
METRIC_NET = "net"
METRIC_UNITS = "units"
METRICS = [(METRIC_NET, "Dollars"), (METRIC_UNITS, "Units")]
METRIC_KEYS = {key for key, _ in METRICS}

#: How a weekend's figure is expressed.
MODE_CUMULATIVE = "cum"
MODE_WEEKEND = "weekend"
MODE_DAY = "day"
MODES = [
    (MODE_CUMULATIVE, "Cumulative"),
    (MODE_WEEKEND, "Per weekend"),
    (MODE_DAY, "Per trading day"),
]
MODE_KEYS = {key for key, _ in MODES}

DEFAULT_METRIC = METRIC_NET
DEFAULT_MODE = MODE_CUMULATIVE


class Weekend:
    """One weekend of one season, and what is known about it."""

    def __init__(self, number, days, traded_days, value, units, lines, to_come,
                 mean_f=None, wet_days=0, weather_days=0):
        self.number = number
        self.days = days
        self.traded_days = traded_days
        self.value = value
        self.units = units
        self.lines = lines
        #: Every traded day of it is still in the future.
        self.to_come = to_come
        #: Projected rather than counted. Set by `project()`.
        self.projected = False
        #: Mean temperature over the days of it that have a reading, and how
        #: many of those were wet. Absent rather than zero when nothing has
        #: been fetched — a weekend with no reading is not a cold dry one.
        self.mean_f = mean_f
        self.wet_days = wet_days
        self.weather_days = weather_days
        #: Whether the season this belongs to has any sales at all. A weekend
        #: is only a *gap* when the rest of its season arrived without it —
        #: for a season nobody has imported, every weekend being flagged says
        #: nine times over what one sentence says once, and buries the real
        #: gaps in the seasons that are half loaded.
        self.season_empty = False

    @property
    def has_data(self):
        return self.lines > 0

    @property
    def is_gap(self):
        """In the past, traded, part of a season that otherwise arrived, and
        nothing recorded against it."""
        return (
            not self.season_empty
            and not self.to_come
            and self.traded_days > 0
            and not self.has_data
        )

    @property
    def has_weather(self):
        return self.weather_days > 0

    @property
    def was_wet(self):
        return self.wet_days > 0

    @property
    def per_day(self):
        if not self.traded_days:
            return Decimal(0)
        return Decimal(self.value) / self.traded_days


class Season:
    """One `Faire` with its weekends filled in."""

    def __init__(self, faire, weekends):
        self.faire = faire
        self.year = faire.year
        self.weekends = weekends
        #: What a projection over this season was built from. A season
        #: extrapolated from its opening weekend and one extrapolated from
        #: eight are both "a projection", and only one of them is worth
        #: acting on — so the basis is carried rather than left for the
        #: reader to work out from the dashes on the chart.
        self.projection_weekends = 0
        self.projection_share = None

    @property
    def total(self):
        """What was actually taken. **Projected weekends are not in it.**

        They are in `w.value` — the chart needs them to draw the dashed tail —
        so a total that summed every weekend would silently become the
        projection, and the figure captioned "so far" would equal the one
        captioned "on course for". That is the page agreeing with itself about
        a number nobody measured.
        """
        return sum(w.value for w in self.weekends if w.has_data and not w.projected)

    @property
    def units(self):
        return sum(w.units for w in self.weekends if w.has_data and not w.projected)

    @property
    def traded_days(self):
        """Only the days a *counted* weekend covers — the honest denominator.

        A season half imported would otherwise divide its takings by the whole
        run and read as a catastrophe; a season half projected would divide a
        projection by days nobody has worked yet.
        """
        return sum(
            w.traded_days for w in self.weekends
            if w.has_data and not w.projected
        )

    @property
    def per_day(self):
        days = self.traded_days
        return Decimal(self.total) / days if days else Decimal(0)

    @property
    def calendar_days(self):
        """Every day of the run, traded or not — the shape of the season."""
        return sum(w.days for w in self.weekends)

    @property
    def gaps(self):
        return [w.number for w in self.weekends if w.is_gap]

    @property
    def to_come(self):
        return [w.number for w in self.weekends if w.to_come]

    @property
    def is_complete(self):
        return not self.gaps and not self.to_come and any(w.has_data for w in self.weekends)

    @property
    def has_any_data(self):
        return any(w.has_data for w in self.weekends)

    def cumulative(self):
        running = Decimal(0)
        out = []
        for weekend in self.weekends:
            running += Decimal(weekend.value)
            out.append(running)
        return out

    def series(self, mode):
        if mode == MODE_CUMULATIVE:
            return self.cumulative()
        if mode == MODE_DAY:
            return [w.per_day for w in self.weekends]
        return [Decimal(w.value) for w in self.weekends]


def faire_slugs():
    """Every event on file, with how many seasons each has."""
    counts = defaultdict(int)
    for slug in Faire.objects.values_list("slug", flat=True):
        counts[slug] += 1
    return sorted(counts.items())


def categories_on_file():
    """Categories that appear in the ledger, for the filter pills.

    **`.order_by()` is load-bearing, not tidying.** `SaleLine` has a default
    `Meta.ordering`, and Django puts ordering columns into the SELECT — so a
    `values_list(...).distinct()` without it de-duplicates on
    `(category, sold_at, item_name)` and hands back one row per line. On a
    twelve-thousand-line ledger that rendered the filter as eleven thousand
    pills.

    Worth knowing how it hid: `.count()` wraps the query in a subquery and
    reports the right number, so checking the count says five and iterating
    says twelve thousand. Verify a `distinct()` by iterating it.

    Aggregates are not affected — `values().annotate()` drops the default
    ordering, which is why `_totals_by_date` and `source_breakdown` were
    always right.
    """
    return sorted(
        name for name in
        SaleLine.objects.order_by().values_list("category", flat=True).distinct()
        if name
    )


def build(slug, categories=None, today=None):
    """Every season of `slug`, weekend by weekend.

    One query for the calendar and one for the money, whatever the number of
    seasons — the folding is done in Python over at most a couple of hundred
    day rows.
    """
    today = today or timezone.localdate()
    faires = list(Faire.objects.filter(slug=slug).order_by("year"))
    if not faires:
        return []

    days = FaireDay.objects.filter(faire__in=faires).select_related("weather").order_by("date")
    by_faire = defaultdict(lambda: defaultdict(list))
    for day in days:
        by_faire[day.faire_id][day.weekend].append(day)

    every_date = [day.date for day in days]
    totals = _totals_by_date(every_date, categories)

    seasons = []
    for faire in faires:
        weekends = []
        for number in sorted(by_faire[faire.id]):
            group = by_faire[faire.id][number]
            traded = [day for day in group if day.traded]
            value = sum(totals[day.date]["value"] for day in group)
            units = sum(totals[day.date]["units"] for day in group)
            lines = sum(totals[day.date]["lines"] for day in group)
            readings = [
                day.weather for day in group
                if getattr(day, "weather", None) and day.weather.mean_f is not None
            ]
            weekends.append(Weekend(
                number=number,
                days=len(group),
                traded_days=len(traded),
                value=value,
                units=units,
                lines=lines,
                mean_f=(sum(r.mean_f for r in readings) / len(readings)) if readings else None,
                wet_days=sum(1 for r in readings if r.was_wet),
                weather_days=len(readings),
                # "To come" means every day that would trade is still ahead.
                # A weekend part-way through is not to come: it has happened
                # enough to be counted, and counting it is what makes a
                # part-season readable at all.
                to_come=bool(traded) and all(day.date > today for day in traded),
            ))
        season = Season(faire, weekends)
        if not season.has_any_data:
            for weekend in weekends:
                weekend.season_empty = True
        seasons.append(season)
    return seasons


def _totals_by_date(dates, categories):
    """Money and units per calendar day, zero-filled."""
    blank = {"value": 0, "units": Decimal(0), "lines": 0}
    totals = defaultdict(lambda: dict(blank))
    if not dates:
        return totals

    lines = SaleLine.objects.filter(sold_at__date__in=dates)
    if categories:
        lines = lines.filter(category__in=categories)
    rows = (
        lines.annotate(day=TruncDate("sold_at"))
        .values("day")
        .annotate(value=Sum("net_cents"), units=Sum("quantity"), lines=Count("id"))
    )
    for row in rows:
        totals[row["day"]] = {
            "value": row["value"] or 0,
            "units": row["units"] or Decimal(0),
            "lines": row["lines"] or 0,
        }
    return totals


def metric_of(season, metric):
    """Swap a season's weekend values over to units, in place-ish.

    Money is the default because it is what a season is judged on, but units
    answer "which styles are moving" without a price change muddying it.
    """
    if metric != METRIC_UNITS:
        return season
    for weekend in season.weekends:
        weekend.value = weekend.units
    return season


def project(focus, priors):
    """Fill the focus season's *to come* weekends from the shape behind it.

    The arithmetic is the one the old React site used and it is the right one:
    take what this season has banked, divide by the share of their final total
    the earlier seasons had reached by the same weekend, and spread the
    remainder over the weekends still to come in the proportions they ran at.

    **Only complete prior seasons count**, and **only weekends genuinely still
    ahead are filled**. A weekend in the past with no lines is a gap and is
    left empty on purpose — see the module docstring.

    Returns the projected season total, or None when there is nothing to
    project or nothing to project from.
    """
    usable = [s for s in priors if s.is_complete and s.total]
    to_come = [w for w in focus.weekends if w.to_come]
    if not usable or not to_come or not focus.has_any_data:
        return None

    banked_numbers = {w.number for w in focus.weekends if w.has_data}
    if not banked_numbers:
        return None

    shares = []
    for season in usable:
        banked = sum(w.value for w in season.weekends if w.number in banked_numbers)
        shares.append(Decimal(banked) / Decimal(season.total))
    share = sum(shares) / len(shares)
    if not share:
        return None

    banked = Decimal(sum(w.value for w in focus.weekends if w.has_data))
    projected_total = banked / share
    focus.projection_weekends = len(banked_numbers)
    focus.projection_share = share * 100
    remainder = projected_total - banked

    # Spread the remainder the way the prior seasons spent those weekends,
    # rather than evenly — the closing weekend is reliably the biggest of the
    # run and an even split would understate it.
    weights = []
    for weekend in to_come:
        weight = sum(
            Decimal(w.value) / Decimal(season.total)
            for season in usable
            for w in season.weekends
            if w.number == weekend.number
        ) / len(usable)
        weights.append(weight)
    weight_total = sum(weights) or Decimal(1)

    for weekend, weight in zip(to_come, weights):
        weekend.value = remainder * weight / weight_total
        weekend.projected = True

    return projected_total


def share_of_season(seasons):
    """Mean share of the season each weekend took, over complete seasons only."""
    usable = [s for s in seasons if s.is_complete and s.total]
    if not usable:
        return {}
    numbers = sorted({w.number for s in usable for w in s.weekends})
    shares = {}
    for number in numbers:
        values = [
            Decimal(w.value) / Decimal(season.total)
            for season in usable
            for w in season.weekends
            if w.number == number
        ]
        shares[number] = sum(values) / len(values) * 100 if values else Decimal(0)
    return shares


def source_breakdown(slug, categories=None):
    """Which pipeline supplied this faire's rows.

    Printed rather than filtered on, the same bargain `private/sales/` makes
    with `InventoryLog.source`: a season carrying `square_csv` rows where
    `app` ones sit everywhere else is the changeover happening, and it should
    be visible without changing what any total means.
    """
    dates = list(FaireDay.objects.filter(faire__slug=slug).values_list("date", flat=True))
    lines = SaleLine.objects.filter(sold_at__date__in=dates)
    if categories:
        lines = lines.filter(category__in=categories)
    return sorted(
        lines.values("source").annotate(lines=Count("id"), value=Sum("net_cents")),
        key=lambda row: -(row["value"] or 0),
    )


# --------------------------------------------------------------------- chart

#: Chart geometry, in user units. The SVG scales to its container, so these
#: are proportions rather than pixels.
CHART_W, CHART_H = 900, 340
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 70, 62, 12, 34


def chart(seasons, focus_year, mode, metric):
    """The pace chart, as an SVG string.

    Drawn on the server rather than by a script, for the same reason the rest
    of this app avoids JavaScript where it can: the page then prints, works
    with scripts blocked, and has nothing to fail silently. Colours come from
    CSS classes rather than attributes so the stylesheet keeps one copy of
    them.

    **Seasons are ordered, so they are drawn on one hue light-to-dark**, not
    as a rainbow of categorical colours — recency then reads without a legend
    lookup. The focused season is the only line carrying a second hue, and
    every line is labelled at its own end, so colour never carries identity
    alone.
    """
    drawable = [s for s in seasons if s.has_any_data]
    if not drawable:
        return ""

    plotted = {}
    for season in drawable:
        values = season.series(mode)
        # A weekend with nothing known is a break in the line, not a zero.
        # Joining across it would draw a season that traded nothing where in
        # fact nobody has imported it.
        plotted[season.year] = [
            (float(value) if (w.has_data or w.projected) else None)
            for w, value in zip(season.weekends, values)
        ]

    weekend_count = max(len(v) for v in plotted.values())
    top = max(
        (v for values in plotted.values() for v in values if v is not None),
        default=0,
    )
    if top <= 0:
        return ""
    step = _nice_step(top)
    top = step * (int(top / step) + 1)

    inner_w = CHART_W - PAD_LEFT - PAD_RIGHT
    inner_h = CHART_H - PAD_TOP - PAD_BOTTOM
    divisor = max(weekend_count - 1, 1)

    def x(index):
        return PAD_LEFT + inner_w * index / divisor

    def y(value):
        return PAD_TOP + inner_h * (1 - value / top)

    parts = []
    ticks = []
    tick = 0
    while tick <= top:
        ticks.append(tick)
        tick += step
    for value in ticks:
        parts.append(
            f'<line class="grid" x1="{PAD_LEFT}" y1="{y(value):.1f}" '
            f'x2="{PAD_LEFT + inner_w}" y2="{y(value):.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{PAD_LEFT - 10}" y="{y(value) + 4:.1f}" '
            f'text-anchor="end">{_tick_label(value, metric)}</text>'
        )
    for index in range(weekend_count):
        parts.append(
            f'<text class="tick" x="{x(index):.1f}" y="{CHART_H - 12}" '
            f'text-anchor="middle">{index + 1}</text>'
        )
    parts.append(
        f'<text class="axis-label" x="{PAD_LEFT + inner_w / 2:.1f}" y="{CHART_H + 2}" '
        f'text-anchor="middle">WEEKEND OF THE RUN</text>'
    )

    ordered = sorted(plotted)
    ranks = {year: rank for rank, year in enumerate(y for y in ordered if y != focus_year)}
    depth = max(len(ranks), 1)

    def draw(year, values, klass, extra=""):
        segments = _segments(values)
        for segment in segments:
            path = " ".join(
                f"{'M' if i == 0 else 'L'}{x(idx):.1f} {y(val):.1f}"
                for i, (idx, val) in enumerate(segment)
            )
            parts.append(f'<path class="{klass}" d="{path}"{extra}/>')

    for year in ordered:
        if year == focus_year:
            continue
        # Older seasons sit closer to the surface; the most recent prior
        # season is the darkest step, which is what makes recency legible.
        shade = 1 + int(ranks[year] * 3 / depth)
        draw(year, plotted[year], f"line prior shade-{min(shade, 3)}")

    if focus_year in plotted:
        values = plotted[focus_year]
        season = next(s for s in drawable if s.year == focus_year)
        real = [
            v if (w.has_data and not w.projected) else None
            for w, v in zip(season.weekends, values)
        ]
        draw(focus_year, real, "line focus")
        forecast = [
            v if (w.projected or w.has_data) else None
            for w, v in zip(season.weekends, values)
        ]
        if any(w.projected for w in season.weekends):
            draw(focus_year, forecast, "line focus forecast")

    # Direct labels at each line's own end, so identity never rides on colour.
    for year in ordered:
        values = plotted[year]
        last = max((i for i, v in enumerate(values) if v is not None), default=None)
        if last is None:
            continue
        klass = "end-label focus" if year == focus_year else "end-label"
        parts.append(
            f'<text class="{klass}" x="{x(last) + 8:.1f}" y="{y(values[last]) + 4:.1f}">{year}</text>'
        )

    body = "".join(parts)
    return (
        f'<svg viewBox="0 -4 {CHART_W} {CHART_H + 18}" role="img" '
        f'aria-label="Season pace by weekend of the run">{body}</svg>'
    )


def _segments(values):
    """Runs of consecutive known points, so a gap breaks the line.

    A run of one is kept and doubled rather than dropped: with a round line
    cap it draws as a dot, which is what a season with a single weekend
    imported should look like. Dropping it instead — which an earlier version
    did for any lone point that was not the last — meant two non-adjacent
    weekends drew nothing at all, and a chart with no line on it reads as a
    season that took nothing.
    """
    out, current = [], []

    def flush():
        if len(current) > 1:
            out.append(list(current))
        elif current:
            out.append(current * 2)

    for index, value in enumerate(values):
        if value is None:
            flush()
            current = []
        else:
            current.append((index, value))
    flush()
    return out


def _nice_step(top):
    """A round gridline interval for a value of this size."""
    for candidate in (10, 25, 50, 100, 250, 500, 1000, 2500, 5000,
                      10000, 25000, 50000, 100000, 250000, 500000, 1000000,
                      2500000, 5000000):
        if top / candidate <= 5:
            return candidate
    return 10000000


def _tick_label(value, metric):
    if metric == METRIC_UNITS:
        return f"{value:,.0f}"
    dollars = value / 100
    if dollars >= 1000:
        return f"${dollars / 1000:,.0f}k"
    return f"${dollars:,.0f}"
