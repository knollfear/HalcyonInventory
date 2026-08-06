import base64
import hashlib
import hmac
import json
import random
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.forms import formset_factory
from django import forms
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.db import transaction
from django.db.models import F, Count, Sum, Max, Case, When, IntegerField, ExpressionWrapper, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
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

from . import colorbands
from .colorutils import nearest_by_color, pick_color_cluster
from .forms import QuickRecipeRowForm, RecipeDyesForm
from .models import Dye, Recipe
from .s3utils import download_object, presigned_post, upload_object
from django.db.models import Prefetch


def page_meta(title, description, category="General", note="", show_in_index=True):
    """
    Attach human-readable metadata to a view so the site map (index) can
    describe it automatically. Add this decorator to any new view and it will
    show up on the site map at /scarves/private/ with no extra wiring.

    note: optional caveat shown under the description (e.g. "POST only",
          "requires ?raw_ids=1,2,3").
    show_in_index: set False to hide a view from the site map. That's the answer
          for a route taking URL params: give it a picker page, list the picker,
          and hide the parameterised view rather than showing a card nobody can
          click.
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


def _site_map(bucket=None):
    """Build the site-map cards by introspecting the URLconf.

    Nothing here is hardcoded: decorate a view with @page_meta and it appears.
    Pass `bucket` ("public") to list only that half — which is what keeps the
    public map from naming staff pages it would only be teasing visitors with.
    """
    from scarves import urls as scarves_urls

    prefix = "/scarves/"
    categories = {}
    seen = set()
    counts = {"public": 0, "private": 0}

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

        # The URL's own first segment is the source of truth for exposure —
        # the same string URLBucketTests checks the view's behaviour against,
        # so a card can't claim to be public while the view demands a login.
        route = str(entry.pattern)
        entry_bucket = route.split("/")[0] or "private"
        if bucket is not None and entry_bucket != bucket:
            continue

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
            "route": prefix + route,
            "url": url,
            "needs_params": needs_params,
            "params": params,
            "bucket": entry_bucket,
        }
        if entry_bucket in counts:
            counts[entry_bucket] += 1
        categories.setdefault(meta["category"], []).append(item)

    # Stable, sorted output: categories alphabetical, items by title.
    grouped = [
        {"name": name, "items": sorted(items, key=lambda i: i["title"])}
        for name, items in sorted(categories.items())
    ]
    total = sum(len(g["items"]) for g in grouped)

    return {
        "grouped": grouped,
        "total": total,
        "public_count": counts["public"],
        "private_count": counts["private"],
    }


@page_meta(
    title="Site Map",
    description="This page — an auto-generated index of every scarves view.",
    category="Overview",
    show_in_index=False,
)
@login_required
def index(request):
    """The staff directory: every page, each badged with its exposure."""
    context = _site_map()
    context["public_map_url"] = reverse("public_index")
    return render(request, "scarves/index.html", context)


@page_meta(
    title="Public Site Map",
    description="This page — the directory of everything reachable without "
                "logging in.",
    category="Public",
    show_in_index=False,
)
def public_index(request):
    """The same directory, filtered to `public/`, and public itself.

    Deliberately not just `index` with a filter argument: this one has to be
    safe to hand to a stranger, so the filtering happens before anything
    reaches the template rather than inside it.
    """
    return render(request, "scarves/public_index.html", _site_map(bucket="public"))

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
    title="Raw Inventory",
    description="Pick a category to see its raw products, with items below par "
                "highlighted so you know what to order. Adjust stock inline.",
    category="Inventory",
)
@login_required
def raw_inventory_index(request):
    """Category picker for the raw inventory pages.

    Exists so `raw_inventory_view` is reachable by clicking rather than by
    knowing a category id — which also keeps the param route off the site map,
    where it could only ever have been a dead entry.

    Carries the shortage counts rather than just naming the categories, so the
    page answers "where do I need to look" without a click.
    """
    categories = (
        RawProductCategory.objects.annotate(
            product_count=Count(
                "raw_products",
                filter=Q(raw_products__is_active=True),
                distinct=True,
            ),
            on_hand=Sum(
                "raw_products__number_on_hand",
                filter=Q(raw_products__is_active=True),
            ),
            below_par=Count(
                "raw_products",
                filter=Q(
                    raw_products__is_active=True,
                    raw_products__par_level__gt=0,
                    raw_products__number_on_hand__lt=F("raw_products__par_level"),
                ),
                distinct=True,
            ),
        )
        .filter(product_count__gt=0)
        .order_by("name")
    )
    return render(request, "scarves/raw_inventory_index.html", {"categories": categories})


@page_meta(
    title="Raw Inventory (by category)",
    description="Raw products for a single category, highlighting items below "
                "par so you know what to order. Adjust stock inline.",
    category="Inventory",
    # Reached from the picker above, which is what the site map lists. A route
    # needing a category id can only ever be a dead card there.
    show_in_index=False,
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
        # The stock buttons on each row. Here rather than spelled out three
        # times in the template, so changing the steps is a one-line edit.
        "adjustments": [(-1, "-1"), (1, "+1"), (10, "+10")],
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

# ---------------------------------------------------------------------------
# Rainbow bands: which sections of the reference sheet a colorway claims.
#
# The whole point of this page is that the claim is *visible and editable*. The
# alphabetical sheet fails silently — you look under orange, the scarf isn't
# there, and nothing tells you it was filed under red. So the bands are stored,
# shown as chips you can toggle, and never written by the classifier: it only
# fills the form in. See `colorbands` for why its guesses can't be trusted
# unreviewed.
# ---------------------------------------------------------------------------


def _band_chips(claimed, suggested=()):
    """The nine toggles for one row, in rainbow order.

    `claimed` is what's stored (or what a pending suggestion has proposed);
    `suggested` marks which of them the classifier put there, so a row can show
    the difference between "you decided this" and "a guess is waiting for you".
    """
    claimed = set(claimed)
    suggested = set(suggested)
    return [
        {
            "slug": slug,
            "label": label,
            "color": color,
            "on": slug in claimed,
            "guessed": slug in suggested,
        }
        for slug, label, color in colorbands.BANDS
    ]


def _first_photo(recipe):
    """The first uploaded photo across a recipe's products, or None.

    Only an uploaded file will do — an external `image_url` can't be sampled
    without fetching someone else's server, same restriction the PDF has.
    """
    for fp in recipe.finished_products.all():
        for img in fp.images.all():
            if img.image:
                return img
    return None


def _classify_row(recipe, suggested=None, saved=False):
    """Context for one row of the classification page."""
    from_dyes = colorbands.bands_from_dyes(recipe)
    if suggested is None:
        # Nothing pending: show what's stored, and offer the dye reading as a
        # suggestion only while the recipe is still unconfirmed.
        pending = None if recipe.bands_confirmed else from_dyes
        chips = _band_chips(recipe.color_bands or (pending or []), pending or [])
    else:
        chips = _band_chips(suggested, suggested)

    return {
        "recipe": recipe,
        "chips": chips,
        "from_dyes": from_dyes,
        "photo": _first_photo(recipe),
        "saved": saved,
        "pending": suggested is not None,
    }


@page_meta(
    title="Colour Classification",
    description="Say which sections of the rainbow each colorway belongs in, so "
                "a scarf can be looked up by the obvious thing about it — that "
                "it's red — instead of by name. Dyes and photos are read to "
                "suggest bands; you confirm or correct them.",
    category="Recipes",
    note="Add ?todo=true for just the unconfirmed ones.",
)
@login_required
def color_classify(request):
    todo_only = request.GET.get("todo") == "true"

    recipes = (
        Recipe.objects.filter(is_active=True)
        .prefetch_related("recipe_dyes__dye", "finished_products__images")
        .order_by("name")
    )
    if todo_only:
        recipes = recipes.filter(bands_confirmed_at__isnull=True)

    rows = [_classify_row(recipe) for recipe in recipes]

    total = Recipe.objects.filter(is_active=True).count()
    confirmed = Recipe.objects.filter(
        is_active=True, bands_confirmed_at__isnull=False
    ).count()

    return render(
        request,
        "scarves/color_classify.html",
        {
            "rows": rows,
            "todo_only": todo_only,
            "total_count": total,
            "confirmed_count": confirmed,
            "todo_count": total - confirmed,
            "bands": colorbands.BANDS,
        },
    )


@require_POST
@login_required
def color_bands_save(request, pk):
    """Store one recipe's bands and stamp them as confirmed by a person.

    Saving nothing is a legitimate answer — it's how you say "this colorway
    doesn't belong in any section" — so an empty list still counts as
    confirmed. What it must not do is leave the row looking unreviewed forever.
    """
    recipe = get_object_or_404(Recipe, pk=pk)

    picked = colorbands.sort_bands(
        b for b in request.POST.getlist("bands") if b in colorbands.BAND_SLUGS
    )
    recipe.color_bands = picked
    recipe.bands_confirmed_at = timezone.now()
    recipe.save(update_fields=["color_bands", "bands_confirmed_at"])

    recipe = (
        Recipe.objects.prefetch_related("recipe_dyes__dye", "finished_products__images")
        .get(pk=pk)
    )
    return render(
        request,
        "scarves/partials/color_row.html",
        _classify_row(recipe, saved=True),
    )


@require_POST
@login_required
def color_suggest_from_photo(request, pk):
    """Read the product photo and tick the bands it seems to show.

    Deliberately a per-row action rather than something the page does on load:
    in production the photos live in the bucket, so sampling all of them would
    mean dozens of downloads every time the page opened. Here it's one image,
    when you ask for it.

    Nothing is saved — the ticks land in the form for you to correct, exactly
    like copying dyes from another recipe on the showcase.
    """
    recipe = get_object_or_404(
        Recipe.objects.prefetch_related("recipe_dyes__dye", "finished_products__images"),
        pk=pk,
    )

    photo = _first_photo(recipe)
    suggested = []
    if photo:
        try:
            with photo.image.open("rb") as f:
                suggested = colorbands.bands_from_image(BytesIO(f.read()))
        except Exception:
            suggested = []

    # Union with what's already ticked: the photo is evidence to add, not a
    # verdict that overrides a band you'd already decided on.
    merged = colorbands.sort_bands(list(recipe.color_bands or []) + suggested)
    return render(
        request,
        "scarves/partials/color_row.html",
        _classify_row(recipe, suggested=merged),
    )


def _showcase_recipes(missing_only=False):
    """Active recipes with their dyes and their finished products.

    Finished products (and a photo) come along because the recipe name alone is
    often not enough to know what a colorway actually was — looking at the
    scarf is how you identify the dyes.
    """
    recipes = (
        Recipe.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch(
                "recipe_dyes",  # ← matches your related_name
                queryset=RecipeDye.objects.select_related("dye").order_by("order", "id"),
            ),
            Prefetch(
                "finished_products",
                queryset=FinishedProduct.objects.filter(is_active=True)
                .prefetch_related("images")
                .order_by("name"),
            ),
        )
        .order_by("name")
    )
    if missing_only:
        recipes = recipes.filter(recipe_dyes__isnull=True)
    return recipes


def _recipe_row_context(request, recipe, form=None, source=None, saved=False):
    """Shared context for one showcase row, however it is rendered."""
    if form is None:
        # Prefill from the source recipe when copying, else from the recipe's
        # own dyes. Either way nothing is written until Save.
        prefill = source if source is not None else recipe
        initial = {
            f"dye{i}": rd.dye_id
            for i, rd in enumerate(prefill.recipe_dyes.all()[: RecipeDyesForm.SLOTS], start=1)
        }
        form = RecipeDyesForm(initial=initial)
    return {
        "recipe": recipe,
        "form": form,
        "edit_mode": True,
        "copied_from": source,
        "saved": saved,
        "dye_sources": _dye_source_recipes(),
    }


def _dye_source_recipes():
    """Recipes that have dyes recorded — the library you can copy from."""
    return (
        Recipe.objects.filter(recipe_dyes__isnull=False)
        .distinct()
        .order_by("name")
        .values("pk", "name")
    )


@page_meta(
    title="Recipe Showcase",
    description="Gallery of active recipes with their dyes, colour swatches and "
                "the finished products made from them. Edit mode fills in "
                "missing dyes, including copying them from a recipe that "
                "already has them.",
    category="Recipes",
    note="Add ?edit=true to edit dyes, &missing=true for just the backlog.",
)
@login_required
def recipe_showcase(request):
    edit_mode = request.GET.get("edit") == "true"
    missing_only = request.GET.get("missing") == "true"
    recipes = list(_showcase_recipes(missing_only=missing_only))

    rows = []
    if edit_mode:
        sources = _dye_source_recipes()
        for recipe in recipes:
            initial = {
                f"dye{i}": rd.dye_id
                for i, rd in enumerate(recipe.recipe_dyes.all()[: RecipeDyesForm.SLOTS], start=1)
            }
            rows.append({
                "recipe": recipe,
                "form": RecipeDyesForm(initial=initial),
                "edit_mode": True,
                "dye_sources": sources,
            })
    else:
        rows = [{"recipe": r, "edit_mode": False} for r in recipes]

    total = Recipe.objects.filter(is_active=True).count()
    without = Recipe.objects.filter(is_active=True, recipe_dyes__isnull=True).count()

    return render(
        request,
        "scarves/recipe_showcase.html",
        {
            "rows": rows,
            "edit_mode": edit_mode,
            "missing_only": missing_only,
            "total_count": total,
            "missing_count": without,
            # Swatch colours for the pickers, same idea as quick recipe entry.
            "dye_hex": json.dumps(
                {str(d.pk): str(d.hex_color) for d in Dye.objects.all()}
            ),
        },
    )


@login_required
def recipe_row(request, pk):
    """One showcase row, re-rendered. `?source=<pk>` prefills the pickers from
    another recipe's dyes without saving; no source re-renders the row as it
    stands (used by Cancel)."""
    recipe = get_object_or_404(
        Recipe.objects.prefetch_related("recipe_dyes__dye", "finished_products__images"),
        pk=pk,
    )
    source = None
    source_pk = request.GET.get("source")
    if source_pk:
        source = (
            Recipe.objects.filter(pk=source_pk)
            .prefetch_related("recipe_dyes__dye")
            .first()
        )

    return render(
        request,
        "scarves/partials/recipe_row.html",
        _recipe_row_context(request, recipe, source=source),
    )


@require_POST
@login_required
def recipe_dyes_save(request, pk):
    """Save one recipe's dyes and hand back the re-rendered row."""
    recipe = get_object_or_404(Recipe, pk=pk)
    form = RecipeDyesForm(request.POST)

    if not form.is_valid():
        recipe = Recipe.objects.prefetch_related(
            "recipe_dyes__dye", "finished_products__images"
        ).get(pk=pk)
        return render(
            request,
            "scarves/partials/recipe_row.html",
            _recipe_row_context(request, recipe, form=form),
        )

    form.save(recipe)
    recipe = Recipe.objects.prefetch_related(
        "recipe_dyes__dye", "finished_products__images"
    ).get(pk=pk)
    return render(
        request,
        "scarves/partials/recipe_row.html",
        _recipe_row_context(request, recipe, saved=True),
    )


#: How much of a recipe's inventory history to show at once. Long enough to
#: cover a season of a busy recipe, short enough that the page stays a page.
RECIPE_LOG_LIMIT = 200


@page_meta(
    title="Recipe",
    description="Everything about one recipe: its dyes, every finished product "
                "made from it, and the full inventory history behind those "
                "products — production runs, sales and manual adjustments.",
    category="Recipes",
    # Reached from the showcase at private/recipes/, which is the picker.
    show_in_index=False,
)
@login_required
def recipe_detail(request, pk):
    """One recipe, end to end.

    The inventory history is the reason this page exists: on/hand counts say
    where a recipe is now, and only the log says how it got there — whether a
    low count means it sold or was never produced.
    """
    recipe = get_object_or_404(
        Recipe.objects.prefetch_related("recipe_dyes__dye"), pk=pk
    )

    products = list(
        recipe.finished_products
        .select_related("raw_product", "raw_product__category")
        .prefetch_related("images")
        .order_by("-is_active", "name")
    )

    # One query for the whole history rather than one per product.
    logs = (
        InventoryLog.objects
        .filter(finished_product__in=products)
        .select_related("finished_product")
        .order_by("-created_at")[: RECIPE_LOG_LIMIT + 1]
    )
    logs = list(logs)
    truncated = len(logs) > RECIPE_LOG_LIMIT
    logs = logs[:RECIPE_LOG_LIMIT]

    # Lifetime movement, computed over every log rather than the page's slice —
    # a truncated history would otherwise quietly understate the totals.
    totals = (
        InventoryLog.objects
        .filter(finished_product__in=products)
        .values("log_type")
        .annotate(qty=Sum("quantity"), entries=Count("id"))
    )
    by_type = {row["log_type"]: row for row in totals}

    def _qty(log_type):
        return (by_type.get(log_type) or {}).get("qty") or 0

    return render(request, "scarves/recipe_detail.html", {
        "recipe": recipe,
        "products": products,
        "logs": logs,
        "truncated": truncated,
        "log_limit": RECIPE_LOG_LIMIT,
        "on_hand": sum(p.number_on_hand for p in products),
        "par_total": sum(p.par or 0 for p in products),
        "produced": _qty(InventoryLog.PRODUCTION),
        # Sales are recorded negative; show the count as a positive number.
        "sold": -_qty(InventoryLog.SALE),
        "adjusted": _qty(InventoryLog.ADJUSTMENT),
        "log_count": sum(row["entries"] for row in totals),
        # Caps the back-date picker; a dye session can't be in the future.
        "today": timezone.localdate(),
    })


@require_POST
@login_required
def record_recipe_production(request, pk):
    """Record a dye session for one recipe: N baths per finished product.

    This is the batch form of `record_dye_bath`, and it exists because a
    colorway — not a product — is the unit of work. A session is 2–3 bases of
    one colour, entered afterwards from notes, so one submit per colour beats
    one click per product.

    Quantities are counted in baths rather than items: a bath is indivisible,
    which is exactly why finishing slightly over par is normal.
    """
    recipe = get_object_or_404(Recipe, pk=pk)

    # Optional back-date, for digitising sessions off paper. Blank means now,
    # which is the everyday case — day-level precision doesn't matter here,
    # the question is always which season something was dyed in.
    dyed_on = None
    dyed_on_raw = (request.POST.get("dyed_on") or "").strip()
    if dyed_on_raw:
        try:
            dyed_on = datetime.strptime(dyed_on_raw, "%Y-%m-%d").date()
        except ValueError:
            messages.error(
                request,
                f"'{dyed_on_raw}' isn't a date I understand (YYYY-MM-DD) — "
                "nothing was recorded.",
            )
            return redirect("recipe_detail", pk=pk)
        if dyed_on > timezone.localdate():
            messages.error(
                request, "That date is in the future — nothing was recorded."
            )
            return redirect("recipe_detail", pk=pk)

    # A back-dated session is history being typed up, not stock arriving: the
    # yarn was sold or counted long ago. Adding it to number_on_hand would
    # inflate current inventory by however many years get digitised, so a past
    # date writes log rows only.
    historical = dyed_on is not None and dyed_on < timezone.localdate()

    # Read the form before touching anything, so a bad field can't leave a
    # session half-recorded.
    entries = []
    for product in recipe.finished_products.filter(is_active=True):
        raw_value = (request.POST.get(f"baths_{product.pk}") or "").strip()
        if not raw_value:
            continue
        if not raw_value.isdigit():
            messages.error(
                request,
                f"'{raw_value}' isn't a number of baths for {product.name} — "
                "nothing was recorded.",
            )
            return redirect("recipe_detail", pk=pk)
        baths = int(raw_value)
        if baths:
            entries.append((product, baths))

    if not entries:
        messages.info(request, "No baths entered, so nothing was recorded.")
        return redirect("recipe_detail", pk=pk)

    made = 0
    with transaction.atomic():
        for product, baths in entries:
            # Locked and re-read per entry: two finished products of one
            # recipe often share a raw base, and read-modify-write on stale
            # copies would silently lose one of the two deductions.
            raw_product = (
                RawProduct.objects.select_for_update().get(pk=product.raw_product_id)
            )
            per_bath = raw_product.number_per_dye_bath or 1
            quantity = baths * per_bath

            if not historical:
                raw_product.number_on_hand = max(
                    raw_product.number_on_hand - quantity, 0
                )
                raw_product.save(update_fields=["number_on_hand"])

                product.number_on_hand += quantity
                product.save(update_fields=["number_on_hand"])

            # One row per product per session, not one per bath: the bath
            # count is recoverable from quantity, and a single deliberate
            # entry reads better in the history than N identical rows.
            log = InventoryLog.objects.create(
                finished_product=product,
                raw_product=raw_product,
                log_type=InventoryLog.PRODUCTION,
                quantity=quantity,
                notes=(
                    f"{baths} dye bath{'' if baths == 1 else 's'} × {per_bath}, "
                    + (
                        f"back-dated entry for {dyed_on:%d %b %Y}; "
                        "stock left unchanged."
                        if historical
                        else f"recorded from the {recipe.name} recipe page."
                    )
                ),
            )
            if dyed_on is not None:
                # created_at is auto_now_add, so it can only be set after the
                # fact. Noon local, because a date carries no time and midnight
                # is the value most likely to slide into the adjacent day.
                InventoryLog.objects.filter(pk=log.pk).update(
                    created_at=timezone.make_aware(
                        datetime.combine(dyed_on, time(12, 0))
                    )
                )
            made += quantity

    if historical:
        messages.success(
            request,
            f"Logged {made} item{'' if made == 1 else 's'} for {recipe.name} "
            f"dyed on {dyed_on:%d %b %Y}. Current stock was left alone — "
            "back-dated entries record history only.",
        )
    else:
        messages.success(
            request,
            f"Recorded {made} item{'' if made == 1 else 's'} across "
            f"{len(entries)} product{'' if len(entries) == 1 else 's'} "
            f"for {recipe.name}.",
        )
    return redirect("recipe_detail", pk=pk)


#: Blank rows offered per card. A card holds a handful of entries; you can
#: always submit and come back for a long one.
CARD_ROWS = 12


def parse_card_date(text):
    """Read a date off a kanban card, keeping track of how much is known.

    Returns `(date, precision)`. A card saying "9/2024" gives back the 1st of
    September with MONTH precision — the day is storage padding, and the
    precision flag is what stops it ever being shown as though it were real.

    Accepts what people actually write: `9/15/2024`, `9-15-24`, `2024-09-15`
    for a day; `9/2024`, `2024-09` for a month. Four digits anywhere means a
    year, so ISO and US order are told apart rather than guessed at.
    """
    parts = [p for p in text.strip().replace("/", "-").replace(".", "-").split("-") if p]
    if not all(p.isdigit() for p in parts):
        raise ValueError("not a date")

    def _year(value):
        # Cards are recent, so a two-digit year is 20xx.
        return int(value) + 2000 if len(value) <= 2 else int(value)

    if len(parts) == 3:
        if len(parts[0]) == 4:
            year, month, day = _year(parts[0]), int(parts[1]), int(parts[2])
        else:
            month, day, year = int(parts[0]), int(parts[1]), _year(parts[2])
        return date(year, month, day), InventoryLog.DAY

    if len(parts) == 2:
        if len(parts[0]) == 4:
            year, month = _year(parts[0]), int(parts[1])
        else:
            month, year = int(parts[0]), _year(parts[1])
        return date(year, month, 1), InventoryLog.MONTH

    raise ValueError("not a date")


@page_meta(
    title="Kanban Card Backfill",
    description="Type up the handwritten production history from the old "
                "kanban cards, one card per finished product. Records history "
                "only — current stock is never touched.",
    category="Production",
)
@login_required
def card_backfill_index(request):
    """Pick a card to type up, and see how far through the stack you are."""
    products = (
        FinishedProduct.objects.filter(is_active=True)
        .select_related("recipe", "raw_product")
        .annotate(
            backfilled=Count(
                "inventory_logs",
                filter=Q(
                    inventory_logs__date_precision__in=[
                        InventoryLog.DAY, InventoryLog.MONTH
                    ]
                ),
            )
        )
        .order_by("name")
    )
    products = list(products)
    return render(request, "scarves/card_backfill_index.html", {
        "products": products,
        "done": sum(1 for p in products if p.backfilled),
    })


@page_meta(
    title="Kanban Card",
    description="Type up one card's handwritten production entries.",
    category="Production",
    show_in_index=False,
)
@require_http_methods(["GET", "POST"])
@login_required
def card_backfill(request, pk):
    """One card: a column of dates and bath counts, submitted together.

    Everything here is history — the yarn was counted or sold long ago — so
    no entry on this page moves current stock. That's the difference between
    this and the recipe page's production form.
    """
    product = get_object_or_404(
        FinishedProduct.objects.select_related("recipe", "raw_product"), pk=pk
    )
    per_bath = product.raw_product.number_per_dye_bath or 1

    if request.method == "POST":
        # Parse every row before writing any of it: half a transcribed card
        # is worse than none, because you can't tell which half.
        entries, errors = [], []
        for index in range(CARD_ROWS):
            when = (request.POST.get(f"date_{index}") or "").strip()
            baths = (request.POST.get(f"baths_{index}") or "").strip()
            if not when and not baths:
                continue
            if not when:
                errors.append(f"Row {index + 1}: baths but no date.")
                continue
            if not baths.isdigit() or int(baths) < 1:
                errors.append(f"Row {index + 1}: '{baths}' isn't a bath count.")
                continue
            try:
                when_date, precision = parse_card_date(when)
            except ValueError:
                errors.append(f"Row {index + 1}: can't read the date '{when}'.")
                continue
            if when_date > timezone.localdate():
                errors.append(f"Row {index + 1}: {when} is in the future.")
                continue
            entries.append((when_date, precision, int(baths)))

        if errors:
            for error in errors:
                messages.error(request, error)
            messages.info(request, "Nothing was recorded — fix those and resubmit.")
            return redirect("card_backfill", pk=pk)

        if not entries:
            messages.info(request, "Nothing entered, so nothing was recorded.")
            return redirect("card_backfill", pk=pk)

        with transaction.atomic():
            for when_date, precision, baths in entries:
                log = InventoryLog.objects.create(
                    finished_product=product,
                    raw_product=product.raw_product,
                    log_type=InventoryLog.PRODUCTION,
                    quantity=baths * per_bath,
                    date_precision=precision,
                    notes=(
                        f"{baths} dye bath{'' if baths == 1 else 's'} × {per_bath}, "
                        "from the kanban card. History only; stock unchanged."
                    ),
                )
                # created_at is auto_now_add, so it can only be set afterwards.
                # Noon local: a date carries no time, and midnight is the value
                # most likely to slide into the neighbouring day — or month.
                InventoryLog.objects.filter(pk=log.pk).update(
                    created_at=timezone.make_aware(
                        datetime.combine(when_date, time(12, 0))
                    )
                )

        messages.success(
            request,
            f"Added {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} "
            f"from {product.name}'s card. Current stock is unchanged.",
        )
        return redirect("card_backfill", pk=pk)

    return render(request, "scarves/card_backfill.html", {
        "product": product,
        "per_bath": per_bath,
        "rows": range(CARD_ROWS),
        "existing": (
            product.inventory_logs
            .filter(date_precision__in=[InventoryLog.DAY, InventoryLog.MONTH])
            .order_by("-created_at")
        ),
        "today": timezone.localdate(),
    })


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


def _matrix_picker_products():
    """
    Every active raw product, for the bulk-matrix picker.

    Deliberately unfiltered, unlike `_bulk_inventory_picker_products`: this is
    the page where a raw product's finished products get *created*, so the ones
    with none yet are the point rather than a dead end.
    """
    return (
        RawProduct.objects.filter(is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )


def _matrix_picker_response(request):
    return render(
        request,
        "scarves/bulk_recipe_matrix_entry.html",
        {"show_picker": True, "picker_products": _matrix_picker_products()},
    )


@page_meta(
    title="Bulk Recipe Matrix",
    description="Spreadsheet-style grid: rows are recipes, columns are raw "
                "products. Bulk-creates/updates FinishedProducts with auto-named "
                '"<Raw> - <Recipe>" entries and default pricing.',
    category="Recipes",
    note="Add ?raw_ids=1,2,3 — or open with none to pick from a list.",
)
@require_http_methods(["GET", "POST"])
@login_required
def bulk_recipe_matrix_entry(request):
    """
    Usage:
      /scarves/private/bulk-matrix/?raw_ids=1,2,3   (or open bare and pick from a list)

    Each row = one recipe name and counts for each raw product.
    Creates/updates FinishedProduct for every (recipe, raw_product) cell provided.
    Finished product names are auto-generated as "<Raw> - <Recipe>".
    """
    # Accept either ?raw_ids=1,2,3 or repeated ?raw_ids=1&raw_ids=2 (picker form).
    raw_ids_param = ",".join(v.strip() for v in request.GET.getlist("raw_ids") if v.strip())
    raw_ids = _parse_raw_ids(raw_ids_param)

    # No selection yet: show the picker. The hidden `picked` marker is what
    # tells a submitted-but-empty picker apart from a bare first visit — an
    # unticked checkbox form submits no parameters at all.
    if not raw_ids:
        if request.GET.get("picked"):
            messages.error(request, "Pick at least one raw product to build the grid.")
        return _matrix_picker_response(request)

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
        return _matrix_picker_response(request)

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
                    # Without the columns the grid re-renders with no cells at
                    # all, hiding the very errors this branch exists to show.
                    "columns": columns,
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


def _bulk_inventory_picker_products():
    """
    Raw products worth picking on the bulk-inventory page: active, and with at
    least one active finished product. Anything else has no rows to edit.
    """
    return (
        RawProduct.objects.filter(is_active=True, finished_products__is_active=True)
        .select_related("category")
        .distinct()
        .order_by("category__name", "name")
    )


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
        return render(
            request,
            "scarves/bulk_inventory_update.html",
            {"show_picker": True, "picker_products": _bulk_inventory_picker_products()},
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
        return render(
            request,
            "scarves/bulk_inventory_update.html",
            {"show_picker": True, "picker_products": _bulk_inventory_picker_products()},
        )

    finished_products = list(
        FinishedProduct.objects.filter(raw_product__in=raw_products, is_active=True)
        .select_related("raw_product", "recipe")
        .order_by("raw_product__name", "recipe__name", "name")
    )

    # Raw products with nothing to edit only add empty tables — drop them.
    with_rows = {fp.raw_product_id for fp in finished_products}
    empty = [rp for rp in raw_products if rp.id not in with_rows]
    raw_products = [rp for rp in raw_products if rp.id in with_rows]
    if empty:
        messages.info(
            request,
            "Skipped (no active finished products yet): "
            + ", ".join(rp.name for rp in empty)
            + ".",
        )
        # Keep the skipped ids out of the save-redirect so the notice isn't sticky.
        raw_ids_param = ",".join(str(rp.id) for rp in raw_products)

    if not raw_products:
        return render(
            request,
            "scarves/bulk_inventory_update.html",
            {"show_picker": True, "picker_products": _bulk_inventory_picker_products()},
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
    """Category picker for the reference-sheet PDFs.

    Public, like the sheets themselves: the contents are product photos,
    names and barcodes — the same things printed on the stall table.

    Carries the same kind of at-a-glance counts as `raw_inventory_index`, so
    the page says what you'd get before you wait on a PDF build: one page per
    recipe, one barcode card per item, and how many items would print without a
    photo — the sheet's whole point is matching a photo to a barcode.
    """
    printable = Q(
        raw_products__is_active=True,
        raw_products__finished_products__is_active=True,
        raw_products__finished_products__recipe__is_active=True,
    )
    categories = list(
        RawProductCategory.objects.annotate(
            recipe_count=Count(
                "raw_products__finished_products__recipe",
                filter=printable,
                distinct=True,
            ),
            item_count=Count(
                "raw_products__finished_products",
                filter=printable,
                distinct=True,
            ),
        )
        .filter(recipe_count__gt=0)
        .order_by("name")
    )

    # Only an uploaded file can be embedded in the PDF, so an external
    # image_url doesn't count as having a photo (see _select_recipe_photos).
    photoless = dict(
        FinishedProduct.objects.filter(
            is_active=True,
            raw_product__is_active=True,
            recipe__is_active=True,
        )
        .exclude(images__image__gt="")
        .values("raw_product__category")
        .annotate(n=Count("pk", distinct=True))
        .values_list("raw_product__category", "n")
    )
    for category in categories:
        category.photoless = photoless.get(category.pk, 0)

    return render(request, "scarves/reference_sheet_index.html", {"categories": categories})


def _image_flowable(fpi, max_w, max_h):
    """A ReportLab Image of a FinishedProductImage's uploaded file, scaled to
    fit max_w x max_h (preserving aspect), or None if there's no usable file.

    The source is downscaled with PIL first. Uploads are already capped at
    IMAGE_MAX_EDGE, so for anything shot since that change this is a no-op —
    but it stays as the backstop that made the PDF viable in the first place:
    at full phone resolution reportlab's base85 encode was slow enough to
    threaten the gunicorn timeout, and it bloated the file. Externally-sourced
    images still arrive at whatever size they like."""
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
        im.thumbnail((1400, 1400), PILImage.LANCZOS)
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


def _photo_gallery(photos, usable_width, area_h):
    """Stack photos vertically, each spanning the full page width, sharing the
    vertical space `area_h`. Full-width best fits landscape photos on a portrait
    page (least whitespace). Returns a flowable (Table) or None."""
    from reportlab.platypus import Table, TableStyle, Spacer

    n = len(photos)
    if n == 0 or area_h <= 0:
        return None

    per_h = area_h / n
    rows = [[_image_flowable(p, usable_width, per_h) or Spacer(1, 1)] for p in photos]
    t = Table(rows, colWidths=[usable_width], rowHeights=[per_h] * n)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
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
    note="Returns a PDF.",
    # Reached from the "Reference Sheets" picker, same reasoning as
    # raw_inventory_view: a route needing a category id is a dead card here.
    show_in_index=False,
)
def reference_sheet_pdf(request, category_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, portrait
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepInFrame

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
    title_style = ParagraphStyle("title", parent=styles["h1"], fontSize=18, leading=22, spaceBefore=0, spaceAfter=0)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#555555"), spaceBefore=0, spaceAfter=0)
    name_style = ParagraphStyle("cardname", parent=styles["Normal"], fontSize=10, leading=12, fontName="Helvetica-Bold", alignment=1)
    sku_style = ParagraphStyle("cardsku", parent=styles["Normal"], fontSize=8, leading=10, alignment=1)

    page_w, page_h = portrait(letter)
    margin = 0.5 * inch
    usable_width = page_w - 2 * margin
    usable_height = page_h - 2 * margin
    top_gap = 0.15 * inch
    mid_gap = 0.2 * inch
    safety = 0.1 * inch  # keeps the flowed block just under the frame height

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

        # Measure the fixed parts (title + barcodes) so the photos can take all
        # the remaining height on THIS page — everything stays on one page so a
        # recipe's barcodes are always printed with its photos.
        title_p = Paragraph(recipe.name, title_style)
        sub_p = Paragraph(f"{category.name} · {len(items)} item(s)", sub_style)
        _, th = title_p.wrap(usable_width, usable_height)
        _, sh = sub_p.wrap(usable_width, usable_height)
        bc_grid = _barcode_grid(items, usable_width, name_style, sku_style)
        _, bc_h = bc_grid.wrap(usable_width, usable_height)

        photo_area = (
            usable_height - th - sh - top_gap - mid_gap - bc_h - safety
        )
        gallery = None
        if photo_area > 1.2 * inch:
            gallery = _photo_gallery(
                _select_recipe_photos(items, cap=4), usable_width, photo_area
            )

        block = [title_p, sub_p, Spacer(1, top_gap)]
        if gallery is not None:
            block += [gallery, Spacer(1, mid_gap)]
        else:
            # No photos to show: push the barcodes toward the bottom anyway.
            block.append(Spacer(1, max(photo_area + mid_gap, 0)))
        block.append(bc_grid)

        # Force the whole recipe (photos + every barcode row) onto one page;
        # shrink slightly rather than split if it's ever a hair too tall.
        story.append(KeepInFrame(usable_width, usable_height, block, mode="shrink"))

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
    # iPhones shoot HEIC and Safari sometimes hands it over as-is. It lands as
    # .jpg on purpose, not by accident: _shrink_image transcodes it to JPEG
    # during processing, so the extension describes what ends up in the bucket.
    "image/heic": ".jpg",
    "image/heif": ".jpg",
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
    note="Uploads to the bucket when configured, otherwise to local storage.",
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
def local_upload(request):
    """Dev-only transport: take the file straight into default storage.

    The bucket path (presign -> browser POSTs to the bucket) needs S3, so
    without it the upload page was unusable locally. Everything downstream —
    barcode decode, SKU match, manual assign — is storage-agnostic and shared,
    so this only replaces the two transport steps.
    """
    if settings.USE_S3:
        return JsonResponse(
            {"error": "Bucket storage is configured; use the presigned upload."},
            status=400,
        )

    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "No file supplied."}, status=400)

    content_type = (f.content_type or "image/jpeg").lower()
    ext = _CONTENT_TYPE_EXT.get(content_type, ".jpg")
    # Storage may rename on collision, so trust the key it hands back —
    # _attach_image points FinishedProductImage.image at exactly this key.
    key = default_storage.save(f"finished_products/{uuid.uuid4().hex}{ext}", f)

    upload = ProductImageUpload.objects.create(key=key)
    return JsonResponse({"upload_id": upload.id})


def _upload_bytes(key):
    """Read an uploaded object back, from the bucket or from local storage."""
    if settings.USE_S3:
        return download_object(key)
    with default_storage.open(key, "rb") as fh:
        return fh.read()


def _replace_upload_bytes(key, data, content_type):
    """Overwrite an uploaded object in place. Returns the key actually written.

    Local storage is configured not to overwrite either, so the old file is
    removed first; the returned key is what the caller must trust, since a
    FinishedProductImage points straight at it.
    """
    if settings.USE_S3:
        upload_object(key, data, content_type=content_type)
        return key
    default_storage.delete(key)
    return default_storage.save(key, ContentFile(data))


# Long edge of a stored product photo. Phone cameras hand us ~4000px / 5MB
# JPEGs, which is 40MB+ of downloads for one round of the matching game and
# slow enough in the reference-sheet PDF to threaten the gunicorn timeout.
# 1200 is comfortably past what either use needs.
IMAGE_MAX_EDGE = 1200
IMAGE_JPEG_QUALITY = 85

# Formats a browser will actually render. Anything else has to be transcoded no
# matter its size — an iPhone HEIC is the case that matters, and Chrome and
# Firefox both refuse to display it.
WEB_SAFE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "GIF"}


def _shrink_image(data, max_edge=IMAGE_MAX_EDGE):
    """Downscale an uploaded photo so its long edge is at most `max_edge`.

    Returns `(bytes, content_type)`, or None when the image is already small
    enough, correctly oriented, and in a format browsers can render — an
    in-bounds upload is never re-encoded, so it can't lose quality just by
    passing through here.

    Aspect ratio is preserved: a 4032x3024 phone photo becomes 1200x900, and a
    portrait one 900x1200. Nothing is cropped or squared off.

    A web-safe format is kept as-is so the object still matches the extension in
    its key and the Content-Type it was uploaded under. HEIC becomes JPEG, which
    is what `_CONTENT_TYPE_EXT` already assumes when it names the key.
    """
    from PIL import Image as PILImage, ImageOps

    im = PILImage.open(BytesIO(data))
    im.load()
    fmt = (im.format or "JPEG").upper()

    # 0x0112 is the EXIF Orientation tag. Phones record rotation there rather
    # than rotating the pixels, and re-encoding drops the tag — so a portrait
    # photo that looked upright would come out sideways in the games and the
    # PDF. Baking the rotation in is what makes the resize safe.
    needs_rotation = im.getexif().get(0x0112, 1) != 1
    # Size is not the only reason to rewrite: a small HEIC left alone would sit
    # in the bucket under a .jpg key that no browser can open.
    needs_transcode = fmt not in WEB_SAFE_FORMATS

    if max(im.size) <= max_edge and not needs_rotation and not needs_transcode:
        return None

    im = ImageOps.exif_transpose(im)
    im.thumbnail((max_edge, max_edge), PILImage.LANCZOS)

    out = BytesIO()
    if fmt == "PNG":
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    if fmt == "WEBP":
        im.save(out, "WEBP", quality=IMAGE_JPEG_QUALITY)
        return out.getvalue(), "image/webp"

    # Everything else lands as JPEG, which is what phone cameras send anyway.
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(out, "JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), "image/jpeg"


@require_POST
@login_required
def process_upload(request, upload_id):
    """Download the object, decode its barcode, and file it if a SKU matches."""
    upload = get_object_or_404(ProductImageUpload, id=upload_id)

    # Idempotency: if already filed, just re-render the filed card.
    if upload.status in (ProductImageUpload.STATUS_MATCHED, ProductImageUpload.STATUS_ASSIGNED):
        return render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True})

    data = None
    codes = []
    try:
        data = _upload_bytes(upload.key)
    except Exception as exc:
        upload.error = str(exc)

    if data is not None:
        try:
            # Imported lazily so the app still runs where libzbar0 isn't installed.
            from pyzbar.pyzbar import decode as zbar_decode
            from PIL import Image

            # Decoded at full resolution, before the downscale below: a Code128
            # label is a small part of the frame, and shrinking first is exactly
            # what would stop it resolving.
            img = Image.open(BytesIO(data))
            codes = [r.data.decode("utf-8", "ignore").strip() for r in zbar_decode(img)]
        except Exception as exc:  # decode failure -> fall back to manual assign
            upload.error = str(exc)

        # Swap the phone-sized original for a display-sized copy. Done after the
        # decode and before the photo is ever served, so nothing downstream —
        # the games, the PDF, the upload card — deals with a 5MB file again.
        try:
            shrunk = _shrink_image(data)
            if shrunk:
                body, content_type = shrunk
                upload.key = _replace_upload_bytes(upload.key, body, content_type)
        except Exception as exc:
            # A photo that won't resize is still a usable photo; keep the
            # original rather than losing the upload over it.
            upload.error = (upload.error + " | " if upload.error else "") + f"resize: {exc}"

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


# --------------------------------------------------------------------------
# Matching game
#
# Public, and designed to be embedded on other origins (the Shopify store), so
# the whole game ships as one self-contained htmx fragment. The server deals a
# board; the browser plays it. Doing the flips server-side would be 50-100
# requests per game and would need a session cookie, which is blocked as a
# third-party cookie inside an embed.
# --------------------------------------------------------------------------

GAME_PAIR_SIZES = (4, 6, 8)
GAME_DEFAULT_PAIRS = 6


def _recipe_game_pool():
    """Active recipes that have at least one photographed active product.

    Unlike the PDF reference sheet (`_select_recipe_photos`), externally-hosted
    images are fine here — the browser fetches them itself — so we don't filter
    down to images with an uploaded file.
    """
    return list(
        Recipe.objects.filter(
            is_active=True,
            finished_products__is_active=True,
        )
        .filter(
            Q(finished_products__images__image__gt="")
            | Q(finished_products__images__image_url__gt=""),
        )
        .distinct()
        .prefetch_related(
            Prefetch(
                "recipe_dyes",
                queryset=RecipeDye.objects.select_related("dye").order_by("order", "id"),
            ),
            Prefetch(
                "finished_products",
                queryset=FinishedProduct.objects.filter(is_active=True).prefetch_related("images"),
            ),
        )
    )


def _recipe_images(recipe):
    """Every usable image across a recipe's active products."""
    return [
        img
        for fp in recipe.finished_products.all()
        for img in fp.images.all()
        if img.image or img.image_url
    ]


def _deal_board(pairs, pool=None, rng=None, family=False):
    """Deal `pairs` photo/name card pairs, shuffled.

    One pair per *recipe*, never per product: an infinity and a rectangle from
    the same dye bath photograph almost identically, so dealing both would make
    the board unwinnable by sight.

    `family=True` draws the board from a single color family instead of at
    random, which is a much harder and more useful drill (Blueberry vs Midnight
    Sky vs Aegean Sea). It's opt-in because it leans on dye-swatch hexes as a
    proxy for the photographed color — see `colorutils` — and is only as good as
    the recipe/dye color data behind it.
    """
    rng = rng or random
    pool = _recipe_game_pool() if pool is None else pool

    if family:
        chosen = pick_color_cluster(pool, pairs, rng=rng)
    else:
        chosen = rng.sample(pool, min(pairs, len(pool)))

    cards = []
    for pair_id, recipe in enumerate(chosen, start=1):
        images = _recipe_images(recipe)
        if not images:
            continue
        image = rng.choice(images)
        dyes = [
            {"name": rd.dye.name, "hex_color": rd.dye.hex_color}
            for rd in recipe.recipe_dyes.all()
        ]
        cards.append({
            "pair_id": pair_id,
            "kind": "photo",
            "image_url": image.url,
            "alt_text": image.alt_text or recipe.name,
            "name": recipe.name,
            "dyes": dyes,
        })
        cards.append({
            "pair_id": pair_id,
            "kind": "name",
            "name": recipe.name,
            "dyes": dyes,
        })

    rng.shuffle(cards)
    return cards, len(cards) // 2


def _cors_headers(response):
    """Open up the game endpoints so any page can embed them.

    htmx sends the custom `HX-Request` header, which makes the browser fire a
    preflight — so `Allow-Origin` on its own is not enough, and getting this
    half-right fails silently in the console. The board is anonymous public
    read-only data with no cookies and no writes, hence the wildcard.
    """
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response["Access-Control-Allow-Headers"] = (
        "HX-Request, HX-Current-URL, HX-Target, HX-Trigger, HX-Trigger-Name, HX-Boosted"
    )
    response["Access-Control-Max-Age"] = "86400"
    return response


@require_http_methods(["GET", "OPTIONS"])
def game_board(request):
    """The game itself, as an embeddable fragment. Anonymous; no CSRF (GET only)."""
    if request.method == "OPTIONS":
        return _cors_headers(HttpResponse(status=204))

    try:
        requested = int(request.GET.get("pairs", GAME_DEFAULT_PAIRS))
    except (TypeError, ValueError):
        requested = GAME_DEFAULT_PAIRS
    if requested not in GAME_PAIR_SIZES:
        requested = GAME_DEFAULT_PAIRS

    # Opt-in: deal from one color family instead of at random. Off by default
    # until the recipe/dye color data is known to be trustworthy.
    family = request.GET.get("family") in ("1", "true", "yes")

    pool = _recipe_game_pool()
    cards, dealt = _deal_board(min(requested, len(pool)), pool=pool, family=family)

    # Absolute URLs throughout: this fragment is rendered into pages on *other*
    # origins, where a relative path would resolve against the host site and
    # 404. Testing only on the Django page would never catch it.
    for card in cards:
        url = card.get("image_url")
        if url and not url.startswith(("http://", "https://", "//")):
            card["image_url"] = request.build_absolute_uri(url)

    response = render(request, "scarves/partials/game_board.html", {
        "cards": cards,
        "pairs": dealt,
        "requested_pairs": requested,
        # Only offer sizes the catalog can actually fill.
        "sizes": [n for n in GAME_PAIR_SIZES if n <= len(pool)],
        "board_url": request.build_absolute_uri(reverse("game_board")),
        # Carried through so "play again" and the size toggle keep the mode.
        "family_qs": "&family=1" if family else "",
        "too_few": dealt < 2,
        # Scopes this instance's CSS and JS, so two embeds on one page don't
        # collide and a re-swap can't leave a stale listener behind.
        "instance_id": uuid.uuid4().hex[:8],
    })
    return _cors_headers(response)


@page_meta(
    title="Scarf Matching Game",
    description="Public memory game: match each scarf photo to its dye recipe "
                "name. Boards are dealt from a single color family, so it drills "
                "the distinctions that actually matter.",
    category="Public",
    note="No login required; embeddable on other sites.",
)
def game_page(request):
    """Thin public shell. The game arrives via htmx so it can also be dropped
    into the Shopify storefront with the same endpoint."""
    return render(request, "scarves/game.html", {
        "board_url": reverse("game_board"),
        "default_pairs": GAME_DEFAULT_PAIRS,
        "quiz_url": reverse("quiz_page"),
        "embed_origin": request.build_absolute_uri("/").rstrip("/"),
    })


# --------------------------------------------------------------------------
# Name quiz
#
# Multiple-choice sibling of the matching game, and deliberately the same shape:
# one self-contained htmx fragment, dealt whole by the server and played in the
# browser, so it drops into the Shopify storefront through the same endpoint
# with no session cookie (blocked third-party inside an embed) and no per-answer
# round trip.
#
# The consequence of dealing whole is that the answers sit in the DOM, so the
# score is not tamper-proof. That's the accepted trade for embeddability — it's
# a shop-window game, not a leaderboard.
# --------------------------------------------------------------------------

QUIZ_LENGTHS = (5, 10, 15)
QUIZ_DEFAULT_QUESTIONS = 10
QUIZ_CHOICES = 4
# Below this there aren't enough names to build a question that isn't a giveaway.
QUIZ_MIN_POOL = QUIZ_CHOICES

# Scoring lives here rather than in the template's JS so it's tunable in one
# place and assertable in a test: a right answer is worth QUIZ_POINTS_CORRECT,
# plus a bonus that starts at QUIZ_SPEED_BONUS and decays to nothing over
# QUIZ_SPEED_WINDOW seconds of thinking.
QUIZ_POINTS_CORRECT = 100
QUIZ_SPEED_BONUS = 50
QUIZ_SPEED_WINDOW = 10


def _quiz_product_pool():
    """Active, photographed finished products — at most one per recipe.

    The one-per-recipe rule is the same guarantee the matching game makes, for
    the same reason: an infinity and a rectangle from one dye bath photograph
    near-identically, so putting both names under one photo makes the question
    unanswerable rather than hard. Because every option in the quiz — the answer
    and all its distractors — is drawn from this one list, dedupe here fixes it
    everywhere.
    """
    products = (
        FinishedProduct.objects.filter(is_active=True, recipe__is_active=True)
        .filter(Q(images__image__gt="") | Q(images__image_url__gt=""))
        .distinct()
        .select_related("recipe")
        .prefetch_related(
            "images",
            Prefetch(
                "recipe__recipe_dyes",
                queryset=RecipeDye.objects.select_related("dye").order_by("order", "id"),
            ),
        )
    )

    seen = set()
    pool = []
    for product in products:
        if product.recipe_id in seen:
            continue
        seen.add(product.recipe_id)
        pool.append(product)
    return pool


def _product_images(product):
    """Every usable image on a product."""
    return [img for img in product.images.all() if img.image or img.image_url]


def _deal_quiz(questions, pool=None, rng=None, family=False, choices=QUIZ_CHOICES):
    """Deal `questions` multiple-choice questions: a photo and `choices` names.

    `family=True` draws the distractors from the answer's nearest color
    neighbours instead of at random — a far harder drill, and the whole point of
    the exercise. It's opt-in for the same reason it is on the matching game:
    it leans on dye-swatch hexes as a proxy for the photographed color, so it's
    only as good as the recipe/dye data behind it.
    """
    rng = rng or random
    pool = _quiz_product_pool() if pool is None else pool

    asked = rng.sample(pool, min(questions, len(pool)))

    out = []
    for product in asked:
        images = _product_images(product)
        if not images:
            continue

        others = [p for p in pool if p.pk != product.pk]
        wanted = min(choices - 1, len(others))
        if family:
            distractors = nearest_by_color(
                others, product.recipe, wanted,
                recipe_of=lambda p: p.recipe, rng=rng,
            )
        else:
            distractors = rng.sample(others, wanted)

        options = [{"name": p.name, "correct": False} for p in distractors]
        options.append({"name": product.name, "correct": True})
        rng.shuffle(options)

        out.append({
            "image_url": rng.choice(images).url,
            "answer": product.name,
            "recipe_name": product.recipe.name,
            "options": options,
            "dyes": [
                {"name": rd.dye.name, "hex_color": rd.dye.hex_color}
                for rd in product.recipe.recipe_dyes.all()
            ],
        })

    return out


@require_http_methods(["GET", "OPTIONS"])
def quiz_board(request):
    """The quiz itself, as an embeddable fragment. Anonymous; no CSRF (GET only)."""
    if request.method == "OPTIONS":
        return _cors_headers(HttpResponse(status=204))

    try:
        requested = int(request.GET.get("questions", QUIZ_DEFAULT_QUESTIONS))
    except (TypeError, ValueError):
        requested = QUIZ_DEFAULT_QUESTIONS
    if requested not in QUIZ_LENGTHS:
        requested = QUIZ_DEFAULT_QUESTIONS

    family = request.GET.get("family") in ("1", "true", "yes")

    pool = _quiz_product_pool()
    questions = _deal_quiz(requested, pool=pool, family=family)

    # Absolute, for the same reason as the matching board: this fragment renders
    # into pages on other origins, where a relative path resolves against the
    # host site and 404s. Testing only on the Django page would never catch it.
    for question in questions:
        url = question["image_url"]
        if url and not url.startswith(("http://", "https://", "//")):
            question["image_url"] = request.build_absolute_uri(url)

    response = render(request, "scarves/partials/quiz_board.html", {
        "questions": questions,
        "asked": len(questions),
        "requested_questions": requested,
        # Only offer lengths the catalog can actually fill.
        "lengths": [n for n in QUIZ_LENGTHS if n <= len(pool)],
        "board_url": request.build_absolute_uri(reverse("quiz_board")),
        "family_qs": "&family=1" if family else "",
        "too_few": len(pool) < QUIZ_MIN_POOL,
        "points_correct": QUIZ_POINTS_CORRECT,
        "speed_bonus": QUIZ_SPEED_BONUS,
        "speed_window": QUIZ_SPEED_WINDOW,
        # Scopes this instance's CSS and JS, so two embeds on one page don't
        # collide and a re-swap can't leave a stale listener behind.
        "instance_id": uuid.uuid4().hex[:8],
    })
    return _cors_headers(response)


@page_meta(
    title="Name That Scarf",
    description="Public multiple-choice quiz: a scarf photo and four names, "
                "ten times over. Scored on how many you get right and how fast "
                "you answer.",
    category="Public",
    note="No login required; embeddable on other sites.",
)
def quiz_page(request):
    """Thin public shell, same as the matching game's."""
    return render(request, "scarves/quiz.html", {
        "board_url": reverse("quiz_board"),
        "default_questions": QUIZ_DEFAULT_QUESTIONS,
        "game_url": reverse("game_page"),
        "embed_origin": request.build_absolute_uri("/").rstrip("/"),
    })
