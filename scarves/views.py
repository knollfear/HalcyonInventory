import base64
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.forms import formset_factory
from django import forms
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.db import transaction
from django.db.models import F, Sum, Max, Case, When, IntegerField, ExpressionWrapper, Q
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from .models import (
    FinishedProduct,
    FinishedProductImage,
    InventoryLog,
    ProductImageUpload,
    RawProduct,
    RawProductCategory,
    RecipeDye,
)

from django.contrib import messages
from django.shortcuts import render, redirect
from django.template.response import TemplateResponse

from .forms import QuickRecipeRowForm
from .models import Dye, Recipe
from .s3utils import download_object, presigned_post
from django.db.models import Prefetch


def page_meta(title, description, category="General", note="", show_in_index=True):
    """
    Attach human-readable metadata to a view so the site map (index) can
    describe it automatically. Add this decorator to any new view and it will
    show up on the /scarves/ landing page with no extra wiring.

    note: optional caveat shown under the description (e.g. "POST only",
          "requires ?raw_ids=1,2,3").
    show_in_index: set False to hide a view from the site map.
    """
    def decorator(view_func):
        view_func.page_meta = {
            "title": title,
            "description": description,
            "category": category,
            "note": note,
            "show_in_index": show_in_index,
        }
        return view_func
    return decorator


@page_meta(
    title="Site Map",
    description="This page — an auto-generated index of every scarves view.",
    category="Overview",
    show_in_index=False,
)
@login_required
def index(request):
    """
    Dynamically builds a directory of every URL in this app by introspecting
    the URLconf and reading each view's @page_meta. Nothing here is hardcoded:
    decorate a new view and it appears automatically.
    """
    from scarves import urls as scarves_urls

    prefix = "/scarves/"
    categories = {}
    seen = set()

    for entry in scarves_urls.urlpatterns:
        callback = getattr(entry, "callback", None)
        if callback is None:
            continue

        meta = getattr(callback, "page_meta", None)
        if not meta or not meta.get("show_in_index", True):
            continue

        # Same view can be registered under several routes (e.g. the webhook
        # with/without a trailing slash) — only list it once.
        if callback in seen:
            continue
        seen.add(callback)

        converters = getattr(entry.pattern, "converters", {}) or {}
        params = list(converters.keys())
        needs_params = bool(params)

        url = None
        if entry.name and not needs_params:
            try:
                url = reverse(entry.name)
            except Exception:
                url = None

        item = {
            "title": meta["title"],
            "description": meta["description"],
            "note": meta["note"],
            "name": entry.name or "—",
            "route": prefix + str(entry.pattern),
            "url": url,
            "needs_params": needs_params,
            "params": params,
        }
        categories.setdefault(meta["category"], []).append(item)

    # Stable, sorted output: categories alphabetical, items by title.
    grouped = [
        {"name": name, "items": sorted(items, key=lambda i: i["title"])}
        for name, items in sorted(categories.items())
    ]
    total = sum(len(g["items"]) for g in grouped)

    return render(request, "scarves/index.html", {"grouped": grouped, "total": total})

@page_meta(
    title="Production Needed",
    description="Finished products that are below par, grouped by recipe and "
                "sorted so out-of-stock items surface first. Record dye baths "
                "inline. Optional ?category=<id> filter.",
    category="Production",
)
@login_required
def production_needed_view(request):
    category_id = request.GET.get("category")

    shortage_expr = ExpressionWrapper(
        F("par") - F("number_on_hand"),
        output_field=IntegerField(),
    )

    base_qs = (
        FinishedProduct.objects.filter(
            is_active=True,
            par__gt=0,
            number_on_hand__lt=F("par"),
        )
        .select_related("raw_product", "raw_product__category", "recipe")
        .prefetch_related("recipe__recipe_dyes__dye")
        .annotate(shortage_value=shortage_expr)
    )

    if category_id:
        base_qs = base_qs.filter(raw_product__category_id=category_id)

    # Aggregate per recipe to get sort keys
    recipe_stats = (
        base_qs.values("recipe_id", "recipe__name")
        .annotate(
            total_shortage=Sum("shortage_value"),
            has_zero=Max(
                Case(
                    When(number_on_hand=0, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
        )
        .order_by("-has_zero", "-total_shortage", "recipe__name")
    )

    # Group finished products by recipe id
    by_recipe = {}
    for fp in base_qs.order_by("recipe__name", "number_on_hand", "-shortage_value", "name"):
        by_recipe.setdefault(fp.recipe_id, []).append(fp)

    # Build a list of recipe groups in the sorted order from recipe_stats
    groups = []
    for row in recipe_stats:
        rid = row["recipe_id"]
        fps = by_recipe.get(rid, [])
        if not fps:
            continue
        groups.append(
            {
                "recipe_id": rid,
                "recipe_name": row["recipe__name"],
                "has_zero": bool(row["has_zero"]),
                "total_shortage": row["total_shortage"] or 0,
                "items": fps,
                "recipe_obj": fps[0].recipe,  # already select_related
            }
        )

    categories = RawProductCategory.objects.all().order_by("name")
    context = {
        "groups": groups,
        "categories": categories,
        "selected_category_id": int(category_id) if category_id else None,
    }
    return render(request, "scarves/production_needed.html", context)

@require_POST
@login_required
def record_dye_bath(request, pk):
    finished_product = get_object_or_404(FinishedProduct, pk=pk, is_active=True)
    raw_product = finished_product.raw_product

    qty_str = (request.POST.get("qty") or "").strip()
    qty = int(qty_str) if qty_str.isdigit() else (raw_product.number_per_dye_bath or 1)

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"

    with transaction.atomic():
        raw_product.number_on_hand = max(raw_product.number_on_hand - qty, 0)
        raw_product.save()

        finished_product.number_on_hand += qty
        finished_product.save()

        InventoryLog.objects.create(
            finished_product=finished_product,
            raw_product=raw_product,
            log_type=InventoryLog.PRODUCTION,
            quantity=qty,
            notes="Dye bath recorded from production-needed page.",
        )

    # If HTMX request, return the updated row HTML (no redirect)
    if request.headers.get("HX-Request") == "true":
        finished_product.refresh_from_db()
        finished_product = (
            FinishedProduct.objects
            .select_related("raw_product", "raw_product__category", "recipe")
            .get(pk=finished_product.pk)
        )
        return TemplateResponse(
            request,
            "scarves/partials/production_needed_row.html",
            {"fp": finished_product},
        )

    # Normal browser POST: message + redirect
    messages.success(
        request,
        (
            f"Recorded dye bath for '{finished_product.name}': "
            f"+{qty} finished (now {finished_product.number_on_hand}), "
            f"-{qty} raw '{raw_product.name}' (now {raw_product.number_on_hand})."
        ),
    )
    return redirect(next_url)



@page_meta(
    title="Raw Inventory (by category)",
    description="Raw products for a single category, highlighting items below "
                "par so you know what to order. Adjust stock inline.",
    category="Inventory",
    note="Requires a category id in the URL.",
)
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



@page_meta(
    title="Quick Recipe Entry",
    description="Internal form for adding up to 5 recipes at once, with a live "
                "dye color picker (in-stock dyes only).",
    category="Recipes",
)
@login_required
def quick_recipe_entry(request):
    dyes = Dye.objects.filter(in_stock=True).select_related("brand").order_by("brand__name", "name")
    dye_hex = {str(d.pk): str(d.hex_color) for d in dyes}

    forms = [QuickRecipeRowForm(prefix=f"r{i}") for i in range(1, 6)]

    if request.method == "POST":
        bound_forms = []
        saved_count = 0

        for i in range(1, 6):
            f = QuickRecipeRowForm(request.POST, prefix=f"r{i}")
            bound_forms.append(f)

            if f.is_valid():
                recipe = f.save()
                if recipe:
                    saved_count += 1
                    messages.success(request, f"Saved: {recipe.name}")

        # If any errors, re-render with bound forms so you see them inline
        if any(not f.is_valid() for f in bound_forms):
            return render(
                request,
                "scarves/quick_recipe_entry.html",
                {"forms": bound_forms, "dye_hex": dye_hex},
            )

        if saved_count:
            return redirect("quick_recipe_entry")

        # nothing saved, but no errors → just reload
        return redirect("quick_recipe_entry")

    return render(request, "scarves/quick_recipe_entry.html", {"forms": forms, "dye_hex": dye_hex})

@page_meta(
    title="Recipe Showcase",
    description="Read-only gallery of active recipes and their dyes, with color "
                "swatches.",
    category="Recipes",
)
@login_required
def recipe_showcase(request):
    recipes = (
        Recipe.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch(
                "recipe_dyes",  # ← matches your related_name
                queryset=RecipeDye.objects.select_related("dye").order_by("order", "id"),
            )
        )
        .order_by("name")
    )

    return render(
        request,
        "scarves/recipe_showcase.html",
        {"recipes": recipes},
    )


def _parse_raw_ids(raw_ids_param: str) -> list[int]:
    raw_ids = []
    for part in (raw_ids_param or "").split(","):
        part = part.strip()
        if part.isdigit():
            raw_ids.append(int(part))
    # de-dupe preserving order
    seen = set()
    out = []
    for i in raw_ids:
        if i not in seen:
            out.append(i)
            seen.add(i)
    return out


def _default_finished_name(raw_product_name: str, recipe_name: str) -> str:
    return f"{raw_product_name} - {recipe_name}".strip()


def _default_price_for_raw(raw_product: RawProduct) -> Decimal:
    if raw_product.suggested_price is not None:
        return raw_product.suggested_price
    return (raw_product.price or Decimal("0")) * Decimal("3.0")


def build_recipe_matrix_form_class(raw_products: list[RawProduct]):
    """
    Dynamically builds a Form class with:
      - recipe_name
      - on_hand_<raw_id> for each raw product
    """
    fields = {
        "recipe_name": forms.CharField(max_length=150, required=True),
    }

    for rp in raw_products:
        fields[f"on_hand_{rp.id}"] = forms.IntegerField(
            required=False,
            min_value=0,
            label=f"{rp.name} (id={rp.id})",
            help_text=f"How many finished items on hand for this raw product.",
        )

    return type("RecipeMatrixRowForm", (forms.Form,), fields)


@page_meta(
    title="Bulk Recipe Matrix",
    description="Spreadsheet-style grid: rows are recipes, columns are raw "
                "products. Bulk-creates/updates FinishedProducts with auto-named "
                '"<Raw> - <Recipe>" entries and default pricing.',
    category="Recipes",
    note="Requires ?raw_ids=1,2,3 in the query string.",
)
@require_http_methods(["GET", "POST"])
def bulk_recipe_matrix_entry(request):
    """
    Usage:
      /scarves/bulk-matrix/?raw_ids=1,2,3

    Each row = one recipe name and counts for each raw product.
    Creates/updates FinishedProduct for every (recipe, raw_product) cell provided.
    Finished product names are auto-generated as "<Raw> - <Recipe>".
    """
    raw_ids_param = request.GET.get("raw_ids", "").strip()
    raw_ids = _parse_raw_ids(raw_ids_param)

    if not raw_ids:
        messages.error(request, "Provide raw_ids in the query string, e.g. ?raw_ids=1,2,3")
        return render(request, "scarves/bulk_recipe_matrix_entry.html", {"raw_ids_param": raw_ids_param})

    raw_products_qs = (
        RawProduct.objects.filter(id__in=raw_ids, is_active=True)
        .select_related("category")
    )
    raw_products_by_id = {rp.id: rp for rp in raw_products_qs}

    # Preserve the user-specified order
    raw_products = [raw_products_by_id[i] for i in raw_ids if i in raw_products_by_id]

    columns = [(rp, f"on_hand_{rp.id}") for rp in raw_products]

    missing = [i for i in raw_ids if i not in raw_products_by_id]
    if missing:
        messages.error(request, f"Some raw_ids were not found/active: {missing}")

    if not raw_products:
        messages.error(request, "No valid raw products found for the provided raw_ids.")
        return render(request, "scarves/bulk_recipe_matrix_entry.html", {"raw_ids_param": raw_ids_param})

    RowForm = build_recipe_matrix_form_class(raw_products)
    RowFormSet = formset_factory(RowForm, extra=10)

    if request.method == "POST":
        formset = RowFormSet(request.POST)

        if not formset.is_valid():
            return render(
                request,
                "scarves/bulk_recipe_matrix_entry.html",
                {
                    "formset": formset,
                    "raw_ids_param": raw_ids_param,
                    "raw_products": raw_products,
                },
            )

        created_recipes = 0
        created_fp = 0
        updated_fp = 0
        touched_cells = 0

        with transaction.atomic():
            for form in formset:
                cd = form.cleaned_data
                if not cd:
                    continue

                recipe_name = (cd.get("recipe_name") or "").strip()
                if not recipe_name:
                    continue

                recipe, recipe_created = Recipe.objects.get_or_create(
                    name=recipe_name,
                    defaults={"description": "", "is_active": True},
                )
                if recipe_created:
                    created_recipes += 1

                # For each raw product column, if user provided a number, set that inventory
                for rp in raw_products:
                    field_name = f"on_hand_{rp.id}"
                    value = cd.get(field_name, None)

                    # Blank cell => skip (don’t change existing)
                    if value is None:
                        continue

                    finished_name = _default_finished_name(rp.name, recipe.name)
                    price = _default_price_for_raw(rp)

                    fp, created = FinishedProduct.objects.update_or_create(
                        raw_product=rp,
                        recipe=recipe,
                        name=finished_name,
                        defaults={
                            "price": price,
                            "number_on_hand": int(value),
                            "is_active": True,
                        },
                    )

                    touched_cells += 1
                    if created:
                        created_fp += 1
                    else:
                        updated_fp += 1

        messages.success(
            request,
            (
                f"Saved {touched_cells} inventory cells. "
                f"Recipes: {created_recipes} created. "
                f"Finished products: {created_fp} created, {updated_fp} updated."
            ),
        )
        return redirect(f"{request.path}?raw_ids={raw_ids_param}")

    # GET
    formset = RowFormSet()
    return render(
        request,
        "scarves/bulk_recipe_matrix_entry.html",
        {
            "formset": formset,
            "raw_ids_param": raw_ids_param,
            "raw_products": raw_products,
            "columns": columns,
        },
    )


def _verify_square_signature(request):
    signature = request.headers.get("x-square-hmacsha256-signature", "")
    key = getattr(settings, "SQUARE_WEBHOOK_SIGNATURE_KEY", "")
    if not signature or not key:
        return False
    url = settings.SQUARE_WEBHOOK_URL
    payload = url + request.body.decode("utf-8")
    expected = base64.b64encode(
        hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(signature, expected)


def build_bulk_inventory_form_class(finished_products):
    """
    Dynamically builds a Form with one optional count field per finished
    product: count_<fp_id>, pre-filled with its current on-hand value.
    """
    fields = {}
    for fp in finished_products:
        fields[f"count_{fp.id}"] = forms.IntegerField(
            required=False,
            min_value=0,
            initial=fp.number_on_hand,
            label=fp.recipe.name,
        )
    return type("BulkInventoryForm", (forms.Form,), fields)


@page_meta(
    title="Bulk Inventory Update",
    description="Pick raw products and update the on-hand count of every "
                "finished product (by recipe) in one form. Each change is "
                "written to the inventory log as an adjustment.",
    category="Inventory",
    note="Add ?raw_ids=1,2,3 — or open with none to pick from a list.",
)
@login_required
@require_http_methods(["GET", "POST"])
def bulk_inventory_update(request):
    # Accept either ?raw_ids=1,2,3 or repeated ?raw_ids=1&raw_ids=2 (picker form).
    raw_ids_param = ",".join(v.strip() for v in request.GET.getlist("raw_ids") if v.strip())
    raw_ids = _parse_raw_ids(raw_ids_param)

    # No selection yet: show a picker grouped by category.
    if not raw_ids:
        picker_products = (
            RawProduct.objects.filter(is_active=True)
            .select_related("category")
            .order_by("category__name", "name")
        )
        return render(
            request,
            "scarves/bulk_inventory_update.html",
            {"picker_products": picker_products},
        )

    raw_products_by_id = {
        rp.id: rp
        for rp in RawProduct.objects.filter(id__in=raw_ids, is_active=True).select_related("category")
    }
    # Preserve the user-specified order.
    raw_products = [raw_products_by_id[i] for i in raw_ids if i in raw_products_by_id]

    missing = [i for i in raw_ids if i not in raw_products_by_id]
    if missing:
        messages.error(request, f"Some raw_ids were not found/active: {missing}")

    if not raw_products:
        messages.error(request, "No valid raw products found for the provided raw_ids.")
        picker_products = (
            RawProduct.objects.filter(is_active=True)
            .select_related("category")
            .order_by("category__name", "name")
        )
        return render(
            request,
            "scarves/bulk_inventory_update.html",
            {"picker_products": picker_products},
        )

    finished_products = list(
        FinishedProduct.objects.filter(raw_product__in=raw_products, is_active=True)
        .select_related("raw_product", "recipe")
        .order_by("raw_product__name", "recipe__name", "name")
    )

    FormClass = build_bulk_inventory_form_class(finished_products)

    if request.method == "POST":
        form = FormClass(request.POST)
        if form.is_valid():
            changed = 0
            with transaction.atomic():
                for fp in finished_products:
                    new_val = form.cleaned_data.get(f"count_{fp.id}")
                    if new_val is None or new_val == fp.number_on_hand:
                        continue

                    delta = new_val - fp.number_on_hand
                    fp.number_on_hand = new_val
                    fp.save(update_fields=["number_on_hand"])

                    InventoryLog.objects.create(
                        finished_product=fp,
                        raw_product=fp.raw_product,
                        log_type=InventoryLog.ADJUSTMENT,
                        quantity=delta,
                        notes="Bulk inventory update.",
                    )
                    changed += 1

            messages.success(
                request,
                f"Updated {changed} finished product(s)." if changed
                else "No changes to save.",
            )
            return redirect(f"{request.path}?raw_ids={raw_ids_param}")
    else:
        form = FormClass()

    # Group rows by raw product for display, pairing each fp with its bound field.
    groups = []
    for rp in raw_products:
        rows = [
            {"fp": fp, "field": form[f"count_{fp.id}"]}
            for fp in finished_products
            if fp.raw_product_id == rp.id
        ]
        groups.append({"raw_product": rp, "rows": rows})

    return render(
        request,
        "scarves/bulk_inventory_update.html",
        {"form": form, "groups": groups, "raw_ids_param": raw_ids_param},
    )


@csrf_exempt
def square_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    if not _verify_square_signature(request):
        return HttpResponse(status=403)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if payload.get("type") != "order.updated":
        return HttpResponse(status=200)

    order_data = payload.get("data", {}).get("object", {}).get("order_updated", {})
    if order_data.get("state") != "COMPLETED":
        return HttpResponse(status=200)

    order_id = order_data.get("order_id")

    from square.client import Client
    client = Client(
        access_token=settings.SQUARE_ACCESS_TOKEN,
        environment=settings.SQUARE_ENVIRONMENT,
    )

    result = client.orders.retrieve_order(order_id)
    if result.is_error():
        return HttpResponse(status=500)

    line_items = result.body.get("order", {}).get("line_items", [])

    with transaction.atomic():
        for item in line_items:
            variation_id = item.get("catalog_object_id")
            qty = int(item.get("quantity", "0"))
            if not variation_id or qty == 0:
                continue
            try:
                fp = FinishedProduct.objects.get(square_variation_id=variation_id, is_active=True)
            except FinishedProduct.DoesNotExist:
                continue

            fp.number_on_hand = max(fp.number_on_hand - qty, 0)
            fp.save(update_fields=["number_on_hand"])

            InventoryLog.objects.create(
                finished_product=fp,
                log_type=InventoryLog.SALE,
                quantity=-qty,
                sale_reference=order_id,
                notes=f"Square sale via webhook.",
            )

    return HttpResponse(status=200)


@page_meta(
    title="Reference Sheets",
    description="Pick a category to generate a printable barcode/SKU reference "
                "sheet (PDF) — one portrait page per recipe with photos and "
                "per-item barcode cards.",
    category="Reference Sheets",
)
def reference_sheet_index(request):
    categories = RawProductCategory.objects.filter(
        raw_products__is_active=True,
        raw_products__finished_products__is_active=True,
    ).distinct().order_by("name")
    return render(request, "scarves/reference_sheet_index.html", {"categories": categories})


def _image_flowable(fpi, max_w, max_h):
    """A ReportLab Image of a FinishedProductImage's uploaded file, scaled to
    fit max_w x max_h (preserving aspect), or None if there's no usable file.

    The source is downscaled with PIL first: phone photos are ~4000px/several
    MB, and embedding them at full resolution makes reportlab's base85 encode
    slow enough to hit the gunicorn timeout (and bloats the PDF). ~1000px is
    plenty for print at these display sizes."""
    from reportlab.platypus import Image as RLImage
    from PIL import Image as PILImage

    if not fpi or not fpi.image:
        return None
    try:
        with fpi.image.open("rb") as f:
            raw = f.read()
        im = PILImage.open(BytesIO(raw))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((1000, 1000), PILImage.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=80)
        out.seek(0)
        iw, ih = im.size
    except Exception:
        return None
    if not iw or not ih:
        return None
    ratio = min(max_w / iw, max_h / ih)
    return RLImage(out, width=iw * ratio, height=ih * ratio)


def _select_recipe_photos(items, cap=4):
    """One uploaded photo per item first (item order), then fill remaining
    slots with each item's additional photos, up to `cap`. Only counts images
    that have an uploaded file (external image_url can't be embedded)."""
    per_item = [[img for img in fp.images.all() if img.image] for fp in items]
    selected = []
    for imgs in per_item:            # first photo of each item
        if len(selected) >= cap:
            break
        if imgs:
            selected.append(imgs[0])
    idx = 1                          # then fill from additional photos
    while len(selected) < cap:
        added = False
        for imgs in per_item:
            if len(selected) >= cap:
                break
            if len(imgs) > idx:
                selected.append(imgs[idx])
                added = True
        if not added:
            break
        idx += 1
    return selected[:cap]


def _photo_gallery(photos, usable_width):
    """Arrange 1-4 photos adaptively: 1 large, 2 side-by-side, 3-4 in a 2x2
    grid. Returns a flowable (Table) or None."""
    from reportlab.lib.units import inch
    from reportlab.platypus import Table, TableStyle, Spacer

    n = len(photos)
    if n == 0:
        return None

    gap = 0.2 * inch
    if n == 1:
        flow = _image_flowable(photos[0], usable_width, 4.6 * inch)
        if flow is None:
            return None
        grid, col_widths = [[flow]], [usable_width]
    else:
        col_w = (usable_width - gap) / 2
        cell_h = 3.6 * inch if n == 2 else 2.9 * inch
        cells = [_image_flowable(p, col_w, cell_h) or Spacer(1, 1) for p in photos]
        if n == 2:
            grid = [[cells[0], cells[1]]]
        elif n == 3:
            grid = [[cells[0], cells[1]], [cells[2], ""]]
        else:
            grid = [[cells[0], cells[1]], [cells[2], cells[3]]]
        col_widths = [col_w, col_w]

    t = Table(grid, colWidths=col_widths, hAlign="CENTER")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _barcode_card(fp, card_w, name_style, sku_style):
    """A bordered card: item (raw-product) name, Code128 barcode, and SKU."""
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Table, TableStyle, Paragraph, Spacer
    from reportlab.graphics.barcode import code128

    inner = [Paragraph(fp.raw_product.name, name_style), Spacer(1, 0.06 * inch)]
    if fp.sku:
        inner.append(code128.Code128(fp.sku, barHeight=0.45 * inch, barWidth=0.62))
        inner.append(Spacer(1, 0.03 * inch))
        inner.append(Paragraph(fp.sku, sku_style))
    else:
        inner.append(Paragraph(f"${fp.price} (no SKU)", sku_style))

    card = Table([[inner]], colWidths=[card_w])
    card.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#9cbce0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return card


def _barcode_grid(items, usable_width, name_style, sku_style):
    """2-across grid of barcode cards for all items sharing the recipe."""
    from reportlab.lib.units import inch
    from reportlab.platypus import Table, TableStyle

    gap = 0.25 * inch
    card_w = (usable_width - gap) / 2
    cards = [_barcode_card(fp, card_w, name_style, sku_style) for fp in items]
    rows = []
    for i in range(0, len(cards), 2):
        pair = cards[i:i + 2]
        if len(pair) == 1:
            pair.append("")
        rows.append(pair)
    grid = Table(rows, colWidths=[card_w, card_w], hAlign="CENTER")
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return grid


@page_meta(
    title="Reference Sheet PDF",
    description="Generates a printable PDF reference sheet for one category: "
                "one portrait page per recipe, with product photos and a "
                "Code128 barcode card for every item sharing that recipe.",
    category="Reference Sheets",
    note="Requires a category id in the URL · returns a PDF.",
)
def reference_sheet_pdf(request, category_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, portrait
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak

    category = get_object_or_404(RawProductCategory, pk=category_id)

    from .models import Recipe
    recipes = (
        Recipe.objects.filter(
            finished_products__raw_product__category=category,
            finished_products__is_active=True,
            is_active=True,
        )
        .distinct()
        .order_by("name")
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["h1"], fontSize=18, leading=22)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#555555"))
    name_style = ParagraphStyle("cardname", parent=styles["Normal"], fontSize=10, leading=12, fontName="Helvetica-Bold", alignment=1)
    sku_style = ParagraphStyle("cardsku", parent=styles["Normal"], fontSize=8, leading=10, alignment=1)

    page_w, page_h = portrait(letter)
    margin = 0.5 * inch
    usable_width = page_w - 2 * margin

    story = []
    first = True
    for recipe in recipes:
        items = list(
            FinishedProduct.objects.filter(
                recipe=recipe,
                raw_product__category=category,
                is_active=True,
            )
            .select_related("raw_product")
            .prefetch_related("images")
            .order_by("raw_product__name")
        )
        if not items:
            continue
        if not first:
            story.append(PageBreak())
        first = False

        story.append(Paragraph(recipe.name, title_style))
        story.append(Paragraph(f"{category.name} · {len(items)} item(s)", sub_style))
        story.append(Spacer(1, 0.2 * inch))

        gallery = _photo_gallery(_select_recipe_photos(items, cap=4), usable_width)
        if gallery is not None:
            story.append(gallery)
            story.append(Spacer(1, 0.25 * inch))

        story.append(_barcode_grid(items, usable_width, name_style, sku_style))

    if not story:
        story = [Paragraph(f"{category.name} — no active items with recipes.", styles["h1"])]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=portrait(letter),
        topMargin=margin,
        bottomMargin=margin,
        leftMargin=margin,
        rightMargin=margin,
    )
    doc.build(story)
    buf.seek(0)
    return HttpResponse(buf, content_type="application/pdf")


# ---------------------------------------------------------------------------
# Product image upload: phone -> presigned POST straight to bucket -> server
# decodes the barcode and files the photo against a FinishedProduct. If the
# barcode can't be read, the uploader picks the product inline (HTMX type-ahead).
# ---------------------------------------------------------------------------

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _attach_image(upload, product):
    """Create a FinishedProductImage pointing at the already-uploaded object."""
    next_order = (
        product.images.aggregate(Max("order"))["order__max"] or 0
    ) + 1
    fpi = FinishedProductImage(finished_product=product, order=next_order)
    # The file already lives in the bucket at upload.key; reference it in place
    # rather than re-uploading (no move, no egress).
    fpi.image.name = upload.key
    fpi.save()
    return fpi


@page_meta(
    title="Upload Product Photos",
    description="Snap or pick product photos from your phone; each is uploaded "
                "straight to the bucket and auto-filed to the matching product "
                "by reading its barcode. Unreadable ones are assigned inline.",
    category="Products",
    note="Requires bucket storage configured (USE_S3).",
)
@login_required
def image_upload(request):
    return render(
        request,
        "scarves/image_upload.html",
        {"use_s3": settings.USE_S3},
    )


@require_POST
@login_required
def presign_upload(request):
    """Create a tracking row and return presigned POST fields for direct upload."""
    if not settings.USE_S3:
        return JsonResponse(
            {"error": "Bucket storage is not configured on this environment."},
            status=400,
        )
    content_type = (request.POST.get("content_type") or "image/jpeg").lower()
    ext = _CONTENT_TYPE_EXT.get(content_type, ".jpg")
    key = f"finished_products/{uuid.uuid4().hex}{ext}"

    upload = ProductImageUpload.objects.create(key=key)
    post = presigned_post(key, content_type=content_type)
    return JsonResponse(
        {
            "upload_id": upload.id,
            "url": post["url"],
            "fields": post["fields"],
        }
    )


@require_POST
@login_required
def process_upload(request, upload_id):
    """Download the object, decode its barcode, and file it if a SKU matches."""
    upload = get_object_or_404(ProductImageUpload, id=upload_id)

    # Idempotency: if already filed, just re-render the filed card.
    if upload.status in (ProductImageUpload.STATUS_MATCHED, ProductImageUpload.STATUS_ASSIGNED):
        return render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True})

    codes = []
    try:
        # Imported lazily so the app still runs where libzbar0 isn't installed.
        from pyzbar.pyzbar import decode as zbar_decode
        from PIL import Image

        data = download_object(upload.key)
        img = Image.open(BytesIO(data))
        codes = [r.data.decode("utf-8", "ignore").strip() for r in zbar_decode(img)]
    except Exception as exc:  # decode/download failure -> fall back to manual
        upload.error = str(exc)

    product = None
    for code in codes:
        if not code:
            continue
        product = FinishedProduct.objects.filter(sku=code).first()
        if product:
            upload.detected_sku = code
            break

    if product:
        fpi = _attach_image(upload, product)
        upload.finished_product = product
        upload.product_image = fpi
        upload.status = ProductImageUpload.STATUS_MATCHED
        upload.save()
        return render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True})

    # No barcode / no match -> uploader assigns it inline.
    upload.save()
    return render(request, "scarves/partials/upload_card.html",
                  {"upload": upload, "needs_assign": True})


@login_required
def product_search(request):
    """HTMX type-ahead: products matching the typed name or SKU."""
    q = (request.GET.get("q") or "").strip()
    upload_id = request.GET.get("upload_id")
    products = FinishedProduct.objects.none()
    if q:
        products = FinishedProduct.objects.filter(
            Q(name__icontains=q) | Q(sku__icontains=q),
            is_active=True,
        ).order_by("name")[:10]
    return render(
        request,
        "scarves/partials/product_search_results.html",
        {"products": products, "upload_id": upload_id},
    )


@require_POST
@login_required
def assign_upload(request, upload_id):
    """File a manually-picked product for an upload the barcode couldn't match."""
    upload = get_object_or_404(ProductImageUpload, id=upload_id)
    product = get_object_or_404(FinishedProduct, id=request.POST.get("product_id"))

    if upload.status not in (ProductImageUpload.STATUS_MATCHED, ProductImageUpload.STATUS_ASSIGNED):
        fpi = _attach_image(upload, product)
        upload.finished_product = product
        upload.product_image = fpi
        upload.status = ProductImageUpload.STATUS_ASSIGNED
        upload.save()

    return render(request, "scarves/partials/upload_card.html",
                  {"upload": upload, "matched": True})
