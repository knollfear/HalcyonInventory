from django.contrib.auth.decorators import login_required
from django.shortcuts import render

# Create your views here.
from django.http import HttpResponse
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.contrib import messages

from django.db.models import F
from django.shortcuts import render

from .models import FinishedProduct, ProductionLog, RawProduct, RawProductCategory


def index(request):
    return HttpResponse("Hello, world. You're at the polls index.")

@login_required
def production_needed_view(request):
    category_id = request.GET.get("category")

    qs = (
        FinishedProduct.objects.filter(
            is_active=True,
            par__gt=0,
            number_on_hand__lt=F("par"),
        )
        .select_related(
            "raw_product",
            "raw_product__category",
            "recipe",
        )
        .prefetch_related(
            "recipe__recipe_dyes__dye",  # 👈 prefetch dyes for each recipe
        )
    )

    if category_id:
        qs = qs.filter(raw_product__category_id=category_id)

    # Optional: annotation; or just rely on fp.shortage property
    qs = qs.annotate(shortage_value=F("par") - F("number_on_hand"))

    products = qs.order_by("-shortage_value")
    categories = RawProductCategory.objects.all().order_by("name")

    context = {
        "products": products,
        "categories": categories,
        "selected_category_id": int(category_id) if category_id else None,
    }
    return render(request, "scarves/production_needed.html", context)


@require_POST
@login_required
def record_dye_bath(request, pk):
    """
    When called, this:
    - Adds raw_product.number_per_dye_bath to finished_product.number_on_hand
    - Subtracts the same amount from raw_product.number_on_hand
    - Creates a ProductionLog record
    """
    finished_product = get_object_or_404(
        FinishedProduct,
        pk=pk,
        is_active=True,
    )

    raw_product = finished_product.raw_product
    qty = raw_product.number_per_dye_bath or 1  # default safety fallback

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"

    with transaction.atomic():
        # Update raw product stock
        if raw_product.number_on_hand is not None:
            new_raw_on_hand = max(raw_product.number_on_hand - qty, 0)
            raw_product.number_on_hand = new_raw_on_hand
            raw_product.save()

        # Update finished product stock
        finished_product.number_on_hand = finished_product.number_on_hand + qty
        finished_product.save()

        # Log it
        ProductionLog.objects.create(
            finished_product=finished_product,
            raw_product=raw_product,
            quantity=qty,
            notes=f"Dye bath recorded from production-needed page.",
        )

    # ✅ Add a success message
    messages.success(
        request,
        (
            f"Recorded dye bath for '{finished_product.name}': "
            f"+{qty} finished (now {finished_product.number_on_hand} on hand), "
            f"-{qty} raw '{raw_product.name}' (now {raw_product.number_on_hand} left)."
        ),
    )

    return redirect(next_url)

@login_required
def raw_inventory_view(request, category_id):
    """
    Shows raw products for a single category and highlights which ones are below par.
    Lets you see what needs to be ordered and adjust stock.
    """
    category = get_object_or_404(RawProductCategory, pk=category_id)

    # Only this category, active products
    products = (
        RawProduct.objects.filter(
            category=category,
            is_active=True,
        )
        .order_by("name")
    )

    # You might want all categories for navigation
    all_categories = RawProductCategory.objects.all().order_by("name")

    context = {
        "category": category,
        "products": products,
        "all_categories": all_categories,
    }
    return render(request, "scarves/raw_inventory.html", context)

@require_POST
@login_required
def adjust_raw_stock(request, pk):
    """
    Adjust number_on_hand for a raw product.
    `delta` is posted as an integer (e.g. +1, -1, +10).
    """
    raw_product = get_object_or_404(RawProduct, pk=pk, is_active=True)

    try:
        delta = int(request.POST.get("delta", "0"))
    except ValueError:
        delta = 0

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"

    if delta == 0:
        messages.info(request, f"No change applied to '{raw_product.name}'.")
        return redirect(next_url)

    old_on_hand = raw_product.number_on_hand
    new_on_hand = max(old_on_hand + delta, 0)
    raw_product.number_on_hand = new_on_hand
    raw_product.save()

    if delta > 0:
        action = f"Received {delta} units"
    else:
        action = f"Removed {abs(delta)} units"

    messages.success(
        request,
        (
            f"{action} of '{raw_product.name}' in category '{raw_product.category.name}'. "
            f"Now {new_on_hand} on hand (par {raw_product.par_level})."
        ),
    )

    return redirect(next_url)