"""Tiers: the page where a person says how well each colorway sells.

The rule itself is in `scarves/tiers.py`. This is only the door, and it
exists because a ladder built on tiers is a person's rule only if a person
set the tiers.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from .. import production, tiers
from ..models import FinishedProduct, RawProduct
from ..sitemap import page_meta

#: Yarn only — see `tiers.TIERED_CATEGORY` for why.
TIERED_CATEGORY = tiers.TIERED_CATEGORY


def _blanks():
    """Yarn blanks with something dyed on them, with how far tiering has got."""
    live = Q(finished_products__is_active=True,
             finished_products__recipe__is_active=True)
    return (
        RawProduct.objects.filter(
            is_active=True,
            made_in_a_dye_bath=True,
            category__name=TIERED_CATEGORY,
        )
        .annotate(
            colorways=Count("finished_products", filter=live, distinct=True),
            tiered=Count(
                "finished_products",
                filter=live & ~Q(finished_products__tier=""),
                distinct=True,
            ),
        )
        .filter(colorways__gt=0)
        .order_by("name")
    )


@page_meta(
    title="Tiers",
    description=(
        "Say how well each yarn colorway sells: High, Medium, Low or "
        "Discontinued. Pick a base; the planner's By tier order dyes from it."
    ),
    category="Production",
)
@login_required
def tiers_index(request):
    """Pick a base to tier. Exists so the per-blank page is reachable by clicking."""
    return render(request, "scarves/tiers_index.html", {"blanks": _blanks()})


@page_meta(
    title="Tiers (one base)",
    description="Each colorway on one yarn base, with its tier, what it sold and what it holds.",
    category="Production",
    show_in_index=False,
)
@login_required
def tiers_blank(request, raw_id):
    """Every colorway on one base, a tier each, saved together.

    **Ordered by what sold, most first**, so the page reads top to bottom as
    the decision it asks for: the top of the list is where High lives. The
    season's sales and a suggested tier print beside each choice and nothing
    is applied for her — a suggestion that filled itself in would be the app
    setting the rule it then asks her to trust.
    """
    blank = get_object_or_404(
        RawProduct, pk=raw_id, category__name=TIERED_CATEGORY
    )
    products = list(
        FinishedProduct.objects.dyed()
        .filter(raw_product=blank)
        .select_related("raw_product", "recipe")
    )

    if request.method == "POST":
        changed = []
        with transaction.atomic():
            for product in products:
                choice = request.POST.get(f"tier-{product.pk}")
                if choice and tiers.set_tier(product, choice):
                    changed.append(product)
        if changed:
            messages.success(
                request,
                f"Saved {len(changed)} change{'' if len(changed) == 1 else 's'} "
                f"on {blank.name}.",
            )
        return redirect("tiers_blank", raw_id=blank.pk)

    # The same days and the same sold figure *par from sales* divides, so a
    # rate printed here is the rate printed there.
    demand = production.demand_par()
    rows = []
    for product in products:
        sold = demand.sold.get(product.pk, 0)
        per_day = sold / demand.days if demand.available else None
        rows.append({
            "product": product,
            "sold": sold,
            "per_day": per_day,
            "suggested": tiers.suggested(per_day),
            "current": tiers.current(product),
        })
    rows.sort(key=lambda r: (-r["sold"], r["product"].name))

    return render(request, "scarves/tiers_blank.html", {
        "blank": blank,
        "rows": rows,
        "demand": demand,
        "choices": FinishedProduct.TIER_CHOICES + [
            (tiers.DISCONTINUED, "Discontinued"),
        ],
        "pars": [
            (label, tiers.TIER_RUNGS[tier],
             tiers.tier_par(tier, blank.bath_size))
            for tier, label in FinishedProduct.TIER_CHOICES
        ],
        "display": tiers.TIER_DISPLAY,
        "high_at": tiers.HIGH_AT,
        "medium_at": tiers.MEDIUM_AT,
    })
