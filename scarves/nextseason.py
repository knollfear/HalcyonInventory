"""Weekends of cover: what each colour's stock buys, in time, and what it costs to buy more.

**The question this answers is "when does it run out", per colour.** A base
total cannot say it: 25 skeins of a colour that sells one a weekend is the
whole season, and 26 of Homespun Cabernet is about two weekends. So every
live colour gets its own expected sales, weekend by weekend, and its stock
is walked forward against them until it no longer covers the next one.

`scarves/rawdemand.py` answers the rest of the *running* season for a blank
and says outright that next season's order is a different calculation. This
is that calculation, and it reads both ways: while the running season still
has weekends to come it looks at those (which is how it gets checked — it
says what weekend 9 will run out of, and weekend 9 then happens), and once
the run is over it looks at all nine weekends of the next one.

Everything here reads. Nothing writes a row.

**How a colour's expected sales are built, and why that way.**

- **The base's weekend shape, from the season on file.** Each weekend's sales
  per faire day for the blank, times the days that weekend trades (three for
  weekend 2). A rained-out day drops out of the denominator rather than
  reading as a day nothing sold. A weekend the running season has not
  reached is projected at the blank's per-day rate times how much stronger
  that weekend ran in the previous season — pooled across the blanks asked
  about, because one blank's one weekend is too thin to scale by.
- **Times the colour's share of its base.** Its own share of the season's
  colour-attributed sales when it sold at least `MIN_HISTORY` — and its
  tier's average share when it did not, which is what makes a new colour
  plannable on day one: tier it Medium and it is planned like a Medium.
  Shares are then rescaled to sum to one, so the base total holds whatever
  the colours are guessed at — the base total is the number the data
  supports, and the colours divide it.
- **Times growth**, next season only. Growth over the running season would be
  a forecast of a weekend that is already on the calendar.

**"Covered through weekend N" is at a confidence, not on average.** Stocking
for the average weekend runs out on about half of them; the backtest in
`docs/claude/production.md` (*"By tier"*) found that planning for most
weekends is the part that matters, and that how you compute it matters far
less. A colour is covered through N when, with Poisson-shaped weekends, it
still has its display left after N at least `confidence` of the time.

**Two on display is an absolute floor** (`tiers.TIER_DISPLAY`). A bare peg is
a lost sale, so stock does not count as cover until the display is held
back.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from django.utils import timezone

from . import production, sales, seasonreport, seasons, slowsellers, tiers
from .models import Faire, FaireDay, FinishedProduct, RawProduct

#: Below this many units in the season on file, a colour's own share is noise
#: and its tier's average is used instead.
MIN_HISTORY = 5

#: Yarn arrives in packs of ten. Nowhere stores a pack size per blank yet, and
#: this page is yarn only (`tiers.TIERED_CATEGORY`), so it is a constant here.
YARN_PACK = 10

CONFIDENCES = (90, 95)
DEFAULT_CONFIDENCE = 90
DEFAULT_GROWTH = 15
DEFAULT_DYE_WEEKS = 40


def poisson_cdf(k, mean):
    """P(X <= k) for X ~ Poisson(mean). Exact by summation; k is small here."""
    if k < 0:
        return 0.0
    if mean <= 0:
        return 1.0
    term = math.exp(-mean)
    total = term
    for i in range(1, int(k) + 1):
        term *= mean / i
        total += term
    return min(total, 1.0)


@dataclass
class Horizon:
    """The weekends being looked ahead over, and where their shape came from."""

    year: int
    weekends: list            # [(number, trading days)]
    current: bool             # the running season's remainder, or next season
    source_year: int          # the season the rates are read from
    days_counted: int = 0


@dataclass
class Colour:
    product: FinishedProduct
    sold: int
    share: float
    from_tier: bool
    expected: list            # expected units per horizon weekend
    available: int            # on hand + on a sheet - display floor
    covered_through: int      # last horizon weekend covered at the confidence; 0 = none
    covers_all: bool
    baths: int = 0            # baths to cover through the target weekend


@dataclass
class Blank:
    raw: RawProduct
    colours: list = field(default_factory=list)
    expected_total: float = 0.0
    baths: int = 0
    skeins: int = 0
    to_buy: int = 0
    packs: int = 0
    cost: float = 0.0

    @property
    def not_covered_first(self):
        return sum(1 for c in self.colours if c.covered_through == 0)


def _faire(today):
    day = FaireDay.objects.filter(date__lte=today).order_by("-date").first()
    return day.faire if day else Faire.objects.order_by("-year").first()


def horizon(today=None):
    """The running season's weekends still to come, or else all of next season's."""
    today = today or timezone.localdate()
    faire = _faire(today)
    if faire is None:
        return None
    days = list(FaireDay.objects.filter(faire=faire, traded=True).values_list("date", "weekend"))
    to_come = {}
    for when, number in days:
        if when > today:
            to_come[number] = to_come.get(number, 0) + 1
    if to_come:
        return Horizon(faire.year, sorted(to_come.items()), True, faire.year)
    try:
        nxt = seasons.days_for(faire.rule, faire.year + 1)
    except (ValueError, AttributeError):
        nxt = seasons.labor_day_run(faire.year + 1)
    counts = {}
    for _when, number, _labor in nxt:
        counts[number] = counts.get(number, 0) + 1
    return Horizon(faire.year + 1, sorted(counts.items()), False, faire.year)


def weekend_rates(blanks, hz, today=None):
    """`{blank_pk: {weekend: units per faire day}}` for every weekend in the horizon."""
    today = today or timezone.localdate()
    faire = _faire(today)
    focus, prior = {}, {}
    for blank in blanks:
        built = seasonreport.build(faire.slug, blank=blank.pk, today=today)
        by_year = {s.year: s for s in built}
        focus[blank.pk] = by_year.get(hz.source_year)
        prior[blank.pk] = by_year.get(hz.source_year - 1)

    def per_day(weekend):
        return float(weekend.units) / weekend.traded_days if weekend.traded_days else 0.0

    # How much stronger each weekend ran than an average day, the season
    # before — pooled across the blanks, because one blank's weekend is thin.
    lift = {}
    units, days = {}, {}
    total_u = total_d = 0.0
    for season in prior.values():
        if not season:
            continue
        for w in season.weekends:
            if w.has_data and w.traded_days:
                units[w.number] = units.get(w.number, 0.0) + float(w.units)
                days[w.number] = days.get(w.number, 0) + w.traded_days
                total_u += float(w.units)
                total_d += w.traded_days
    if total_d:
        avg = total_u / total_d
        for number in units:
            lift[number] = (units[number] / days[number]) / avg if avg else 1.0

    rates = {}
    for blank in blanks:
        season = focus.get(blank.pk)
        known = {}
        if season:
            for w in season.weekends:
                if w.has_data and w.traded_days and not w.to_come:
                    known[w.number] = per_day(w)
        counted_days = sum(
            w.traded_days for w in (season.weekends if season else [])
            if w.has_data and not w.to_come
        )
        counted_units = sum(
            float(w.units) for w in (season.weekends if season else [])
            if w.has_data and not w.to_come
        )
        base = counted_units / counted_days if counted_days else 0.0
        rates[blank.pk] = {
            number: known.get(number, base * lift.get(number, 1.0))
            for number, _days in hz.weekends
        }
        hz.days_counted = max(hz.days_counted, counted_days)
    return rates


def _source_range(hz, today):
    """The season the shares are read from, up to today — the same lines
    `slowsellers.sold_units` counts everywhere else, over that season's days."""
    days = list(
        FaireDay.objects.filter(faire__year=hz.source_year, faire=_faire(today))
        .order_by("date").values_list("date", flat=True)
    )
    if not days:
        return slowsellers.season_range({})
    return sales.DateRange("season", days[0], min(days[-1], today),
                           f"{hz.source_year} season")


def _shares(products, sold):
    """`{product_pk: (share, from_tier)}` across one blank's live colours."""
    own = {p.pk: sold.get(p.pk, 0) for p in products}
    total = sum(v for k, v in own.items() if v >= MIN_HISTORY) or 0
    raw = {}
    for p in products:
        if own[p.pk] >= MIN_HISTORY and total:
            raw[p.pk] = (own[p.pk] / total, False)
    by_tier = {}
    for p in products:
        if p.pk in raw:
            by_tier.setdefault(p.tier, []).append(raw[p.pk][0])
    everyone = [s for s, _ in raw.values()]
    fallback = (sum(everyone) / len(everyone)) if everyone else (1.0 / max(len(products), 1))
    for p in products:
        if p.pk not in raw:
            peers = by_tier.get(p.tier) if p.tier else None
            raw[p.pk] = ((sum(peers) / len(peers)) if peers else fallback, True)
    scale = sum(s for s, _ in raw.values()) or 1.0
    return {pk: (s / scale, t) for pk, (s, t) in raw.items()}


def _covered(available, expected, confidence):
    """Last horizon index (1-based) still covered at the confidence; 0 if none."""
    cum, last = 0.0, 0
    for i, units in enumerate(expected, start=1):
        cum += units
        if poisson_cdf(available, cum) >= confidence:
            last = i
        else:
            break
    return last


def _extra_needed(available, mean, confidence):
    """Fewest extra units so the stock covers `mean` at the confidence."""
    extra = 0
    while poisson_cdf(available + extra, mean) < confidence and extra < 10000:
        extra += 1
    return extra


def plan(growth=None, confidence=DEFAULT_CONFIDENCE, through=None, today=None,
         blanks=None):
    """`(horizon, [Blank])` — every live colour's cover, and the baths to reach `through`.

    `growth` is `{blank_pk: percent}`; a blank missing from it gets
    `DEFAULT_GROWTH`, and none of it applies to the running season.
    `through` is a weekend number in the horizon; default is the last one.
    """
    today = today or timezone.localdate()
    hz = horizon(today)
    if hz is None:
        return None, []
    growth = growth or {}
    q = confidence / 100.0
    if blanks is None:
        blanks = tiered_blanks()
    blanks = list(blanks)
    rates = weekend_rates(blanks, hz, today)
    sold = slowsellers.sold_units(_source_range(hz, today))
    flight = production.in_flight()
    numbers = [n for n, _ in hz.weekends]
    target = through if through in numbers else numbers[-1]
    upto = numbers.index(target) + 1

    out = []
    for raw in blanks:
        g = 1.0 if hz.current else 1 + growth.get(raw.pk, DEFAULT_GROWTH) / 100.0
        base = [rates[raw.pk][n] * days * g for n, days in hz.weekends]
        products = list(
            FinishedProduct.objects.dyed().filter(raw_product=raw, par__gt=0)
            .select_related("recipe", "raw_product")
        )
        shares = _shares(products, sold)
        row = Blank(raw=raw, expected_total=sum(base))
        for p in products:
            share, from_tier = shares[p.pk]
            expected = [b * share for b in base]
            available = p.number_on_hand + flight.get(p.pk, 0) - tiers.TIER_DISPLAY
            through_i = _covered(available, expected, q)
            colour = Colour(
                product=p, sold=sold.get(p.pk, 0), share=share, from_tier=from_tier,
                expected=expected, available=available,
                covered_through=numbers[through_i - 1] if through_i else 0,
                covers_all=through_i == len(numbers),
            )
            colour.to_target = sum(expected[:upto])
            extra = _extra_needed(max(available, 0), colour.to_target, q)
            # Below the floor counts too: the display has to be rebuilt first.
            extra += max(-available, 0)
            colour.baths = math.ceil(extra / p.bath_size) if extra else 0
            row.colours.append(colour)
        row.colours.sort(key=lambda c: (c.covered_through, -sum(c.expected), c.product.name))
        row.baths = sum(c.baths for c in row.colours)
        row.skeins = sum(c.baths * c.product.bath_size for c in row.colours)
        row.to_buy = max(row.skeins - raw.number_on_hand, 0)
        row.packs = math.ceil(row.to_buy / YARN_PACK)
        row.cost = row.packs * YARN_PACK * float(raw.price)
        out.append(row)
    return hz, out


def tiered_blanks():
    """The blanks the tier page offers: yarn, dyed, with a live colour."""
    return (
        RawProduct.objects.filter(
            is_active=True, made_in_a_dye_bath=True,
            category__name=tiers.TIERED_CATEGORY,
            finished_products__is_active=True,
            finished_products__recipe__is_active=True,
            finished_products__par__gt=0,
        ).distinct().order_by("name")
    )
