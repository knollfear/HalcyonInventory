"""Tiers: how well a colorway sells, said by a person, and the order baths come in.

**This is the rule she already runs in her head, written down.** Every
colorway on a base is High, Medium or Low. A session dyes one bath of every
High that needs one, then every Medium, then every Low; then a second bath of
every High and every Medium; then a third of every High. When that ladder is
climbed, the yarn is well stocked and nothing more is suggested. Discontinued
never suggests anything, and it is not a tier — it is par 0, the switch every
page in the app already honours (see `set_tier`).

**Why tiers rather than a number per colorway.** The season was backtested
before this was built (2026-10-18, the four main yarns, 160 colorways, 15
faire days): at the same total stock, every per-colorway formula tried — a
straight rate, a Poisson percentile, the shuffled-weekends simulation, a
square-root safety stock — came out within noise of the others. The "90% of
weekends" figure for a single colorway, recomputed on reshuffles of the same
season, swung across a whole bath. What the data *does* support is coarse: a
base's total is steady, and Cabernet really does outsell Midnight. And a yarn
bath is five skeins, so any precision finer than a bath is rounded away by the
microwave anyway. Three tiers and a bath count is the honest resolution.

**The tier is the person's, never the app's.** `suggested` proposes one from
the season's sales and the tier page prints it beside the choice, but nothing
here writes a tier on its own. The rule only gets trusted if it is visibly the
rule she set: each row on the planner says which rung it is on, in her terms
("High · has 1 of 3 baths"), so a suggestion can be checked against the
ladder by looking rather than taken on faith.
"""
from dataclasses import dataclass
from math import inf

from .models import FinishedProduct

#: What the display holds, and it is the same for every yarn colorway: four
#: pegs, and down to two is still a stocked display. **A constant, not
#: `display_slots`**, which is the point — a new rack must never raise a par
#: (`CLAUDE.md`, *Display capacity is not demand*), and a fixed two cannot.
TIER_DISPLAY = 2

#: Baths above the display each tier is filled to. High three, Medium two,
#: Low one. On the season's numbers a Low (under half a skein a faire day)
#: sold 0–7 skeins all season, so one bath plus the display covers it; a
#: second would be next year's stock. Two Medium baths cover 1.5 a day for
#: over a week. High is where a third pays, and where it stops is what lets
#: the planner ever say "well stocked".
TIER_RUNGS = {
    FinishedProduct.TIER_HIGH: 3,
    FinishedProduct.TIER_MEDIUM: 2,
    FinishedProduct.TIER_LOW: 1,
}

#: Within a rung, Highs first. A product nobody has tiered yet comes after
#: the Lows rather than off the list — an untiered shortage is still a
#: shortage, and a list that dropped it would be making the decision itself.
PRIORITY = {
    FinishedProduct.TIER_HIGH: 0,
    FinishedProduct.TIER_MEDIUM: 1,
    FinishedProduct.TIER_LOW: 2,
    "": 3,
}

#: The tier page's fourth answer. Stored as par 0, not as a tier value.
DISCONTINUED = "discontinued"

#: Where the tier page's *suggestion* draws its lines, in skeins per faire
#: day. On the four yarns in 2026 these split 160 colorways roughly 18 / 69 /
#: 41. They are a starting point printed beside the choice, nothing more.
HIGH_AT = 1.5
MEDIUM_AT = 0.5


def tier_par(tier, bath_size):
    """The par a tier means: the display, plus that many whole baths."""
    return TIER_DISPLAY + TIER_RUNGS[tier] * bath_size


def suggested(per_day):
    """A first guess from what sold, for the page to print beside the choice."""
    if per_day is None:
        return ""
    if per_day >= HIGH_AT:
        return FinishedProduct.TIER_HIGH
    if per_day >= MEDIUM_AT:
        return FinishedProduct.TIER_MEDIUM
    return FinishedProduct.TIER_LOW


def current(product):
    """What the tier page shows as chosen: discontinued if par is 0."""
    if not product.par:
        return DISCONTINUED
    return product.tier


def set_tier(product, choice):
    """Apply a choice from the tier page. True when anything changed.

    **A tier writes par**, as the display plus that tier's baths. That keeps
    one number on every page — the close, *Use par*, the recipe page's par
    box and this ladder all read the same par — and it is a person's
    deliberate choice made on a page built for it, which is what changing
    par is meant to be. The recipe page's par box still overrides it; the
    ladder reads its depth from par, so a High hand-raised to 22 for the
    pre-season simply gets a fourth rung.

    **Discontinued writes par 0 and leaves the tier alone**, because par 0 is
    already "not making this to order now" on every page, and two switches
    for one meaning eventually disagree. Keeping the tier means bringing a
    colour back is one click that remembers what it was.
    """
    if choice == DISCONTINUED:
        if product.par == 0:
            return False
        product.par = 0
        product.save(update_fields=["par"])
        return True
    if choice not in TIER_RUNGS:
        return False
    par = tier_par(choice, product.bath_size)
    if product.tier == choice and product.par == par:
        return False
    product.tier, product.par = choice, par
    product.save(update_fields=["tier", "par"])
    return True


@dataclass(frozen=True)
class Step:
    """One bath on the ladder: which rung it fills, and whether it overshoots."""

    rung: int
    overshoot: bool = False


def annotate(product, include_overshoot=False):
    """Hang the ladder on a product the planner has already annotated.

    Reads `target_par`, `number_on_hand` and `in_flight` from
    `production.annotate_flight`, so what is already out on a sheet counts
    as on the way. Sets:

    - `ladder` — a `Step` per bath still wanted, rung by rung;
    - `rungs_total` — how many baths above the display par asks for;
    - `rungs_had` — how many of those are already there or on a sheet;
    - `ladder_label` — the row's sentence, in her terms.

    **Whole baths only, unless asked.** A Low at nothing on hand gets one
    bath, not two: par 7 less a bath of 5 still leaves it short by two, and
    rounding that up would dye a second Low bath — exactly the bath the
    ladder exists not to suggest. A bath that would only top up the
    remainder is the overshoot case, and `include_overshoot` adds it after
    every whole rung on the list, so it never jumps ahead of a real one.
    """
    bath = product.bath_size
    target = product.target_par
    expected = product.number_on_hand + product.in_flight
    total = max(target - TIER_DISPLAY, 0) // bath
    short = max(target - expected, 0)
    whole = short // bath
    had = max(total - whole, 0)
    steps = [Step(rung=had + k) for k in range(1, whole + 1)]
    if include_overshoot and short % bath:
        steps.append(Step(rung=had + whole + 1, overshoot=True))
    product.ladder = steps
    product.rungs_total = total
    product.rungs_had = had
    name = dict(FinishedProduct.TIER_CHOICES).get(product.tier, "No tier")
    product.ladder_label = (
        f"{name} · has {had} of {total} bath{'' if total == 1 else 's'}"
    )
    return product


def key(product, step):
    """Sort key for one bath: rung first, then tier, then what sold.

    `sold_colorway` and `sold_here` are set by `production.candidates`. Ties
    inside a rung and tier go to what the colorway sold, then the blank, then
    the name — never the alphabet first.
    """
    return (
        step.overshoot,
        step.rung,
        PRIORITY.get(product.tier, PRIORITY[""]),
        -getattr(product, "sold_colorway", 0),
        -getattr(product, "sold_here", 0),
        product.name,
    )


def first_key(product):
    """Where a product sits on the ladder: the key of its first bath."""
    if not product.ladder:
        return (True, inf, PRIORITY[""], 0, 0, product.name)
    return key(product, product.ladder[0])
