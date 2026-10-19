"""Weekends of cover: each yarn colour's stock as time, and what more costs.

The arithmetic is in `scarves/nextseason.py`. Every setting rides in the query
string, so a plan is a link somebody can send and nothing about it is stored.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .. import nextseason
from ..sitemap import page_meta


def _settings(request, blanks):
    """Read the plan's settings off the URL, falling back to the defaults."""
    def number(key, default, low, high):
        try:
            value = int(request.GET.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, low), high)

    confidence = number("confidence", nextseason.DEFAULT_CONFIDENCE, 50, 99)
    if confidence not in nextseason.CONFIDENCES:
        confidence = nextseason.DEFAULT_CONFIDENCE
    every = number("growth", nextseason.DEFAULT_GROWTH, -90, 300)
    growth = {b.pk: number(f"g{b.pk}", every, -90, 300) for b in blanks}
    try:
        through = int(request.GET["through"])
    except (KeyError, ValueError):
        through = None
    weeks = number("weeks", nextseason.DEFAULT_DYE_WEEKS, 1, 40)
    return confidence, every, growth, through, weeks


def _context(request, blanks):
    confidence, every, growth, through, weeks = _settings(request, blanks)
    hz, rows = nextseason.plan(growth=growth, confidence=confidence,
                               through=through, blanks=blanks)
    numbers = [n for n, _ in hz.weekends] if hz else []
    target = through if through in numbers else (numbers[-1] if numbers else None)
    for row in rows:
        row.growth = growth[row.raw.pk]
    totals = {
        "baths": sum(r.baths for r in rows),
        "skeins": sum(r.skeins for r in rows),
        "to_buy": sum(r.to_buy for r in rows),
        "packs": sum(r.packs for r in rows),
        "cost": sum(r.cost for r in rows),
        "colours": sum(len(r.colours) for r in rows),
        "uncovered": sum(r.not_covered_first for r in rows),
        "short": sum(1 for r in rows for c in r.colours if c.baths),
    }
    return {
        "hz": hz, "rows": rows, "totals": totals, "numbers": numbers,
        "target": target, "confidence": confidence, "confidences": nextseason.CONFIDENCES,
        "growth_all": every, "weeks": weeks,
        "per_week": -(-totals["baths"] // weeks) if weeks else 0,
        "display": nextseason.tiers.TIER_DISPLAY, "pack": nextseason.YARN_PACK,
        "query": request.GET.urlencode(),
        "min_history": nextseason.MIN_HISTORY,
    }


@page_meta(
    title="Weekends of Cover",
    description=(
        "Each yarn colour's stock turned into weekends of sales, with the two "
        "on display held back, and what it costs to dye and buy enough to "
        "reach a chosen weekend. Looks at the weekends left this season, or "
        "next season once this one is over."
    ),
    category="Reports",
)
@login_required
def next_season_index(request):
    blanks = list(nextseason.tiered_blanks())
    return render(request, "scarves/next_season_index.html", _context(request, blanks))


@page_meta(
    title="Weekends of Cover (one base)",
    description="Every live colour on one yarn base, least covered first.",
    category="Reports",
    show_in_index=False,
)
@login_required
def next_season_blank(request, raw_id):
    blank = get_object_or_404(nextseason.tiered_blanks(), pk=raw_id)
    context = _context(request, [blank])
    context["row"] = context["rows"][0] if context["rows"] else None
    return render(request, "scarves/next_season_blank.html", context)
