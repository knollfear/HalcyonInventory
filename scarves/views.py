import base64
import colorsys
from collections import Counter
from urllib.parse import urlencode
import hashlib
import hmac
import json
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.forms import formset_factory
from django import forms
from django.http import FileResponse, HttpResponse, JsonResponse
from django.urls import reverse
from django.db import transaction
from django.db.models import F, Count, Sum, Max, Case, When, IntegerField, ExpressionWrapper, Q, Value
from django.db.models.functions import Greatest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from .models import (
    BoothPhoto,
    CloseRun,
    DisplayFixture,
    CloseRunRow,
    Dye,
    FinishedProduct,
    FinishedProductImage,
    InventoryLog,
    ProductImageUpload,
    ProductionRun,
    ProductionRunRow,
    RawProduct,
    RawProductCategory,
    RecipeDye,
    TimeEntry,
    UnmatchedSale,
)

from django.contrib import messages
from django.shortcuts import render, redirect
from django.template.response import TemplateResponse

from . import (
    closing, colorbands, crew, fancy, labels, photowalk, production, restock,
    sales, sheetscan, skus, timesheets,
)
from .colorutils import hex_to_rgb, nearest_by_color, pick_color_cluster
from .forms import (
    BoothPhotoForm,
    CloseStartForm,
    DisplayFixtureForm,
    RestockPassForm,
    CrewHandbookForm,
    HoursForm,
    LabelRunForm,
    NewDyeForm,
    ProductionSheetForm,
    QuickRecipeRowForm,
    RecipeDyesForm,
    build_close_count_form_class,
    dye_option_attrs,
)
from .models import Recipe, normalize_token
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
    counts = {"public": 0, "private": 0, "secret": 0}

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
        "secret_count": counts["secret"],
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

    # The SQL half of `FinishedProduct.behind_a_bath` — shortage >= bath size,
    # rearranged so it doesn't have to reference the annotation above. Greatest
    # keeps a bath size of 0 from making the test vacuously true, matching the
    # `or 1` the property and `record_dye_bath` both use.
    behind_a_bath_expr = Case(
        When(
            par__gte=F("number_on_hand") + Greatest(
                F("raw_product__number_per_dye_bath"), Value(1)
            ),
            then=1,
        ),
        default=0,
        output_field=IntegerField(),
    )

    base_qs = (
        FinishedProduct.objects.filter(
            is_active=True,
            par__gt=0,
            number_on_hand__lt=F("par"),
            # A passthrough has no recipe and cannot be produced — you order
            # more, you don't dye more. `private/raw-inventory/` is where its
            # shortfall belongs.
            recipe__isnull=False,
            # And a fancy veil *has* a colorway but still isn't dyed into
            # existence — the work added to it is line work on a scarf that
            # already exists.
            raw_product__made_in_a_dye_bath=True,
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
            has_behind=Max(behind_a_bath_expr),
        )
        .order_by("-has_behind", "-total_shortage", "recipe__name")
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
                "has_behind": bool(row["has_behind"]),
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
            source=InventoryLog.SOURCE_PRODUCTION_NEEDED,
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
                "par so you know what to order. Book a delivery or a shelf "
                "count for the whole category in one save.",
    category="Inventory",
    # Reached from the picker above, which is what the site map lists. A route
    # needing a category id can only ever be a dead card there.
    show_in_index=False,
)
@login_required
@require_http_methods(["GET", "POST"])
def raw_inventory_view(request, category_id):
    """One category's blanks: what is on the shelf, and what a delivery adds.

    **This is where a bill gets entered**, and a bill is one document. The
    page used to post a row at a time — three nudge buttons and a "set" box
    per product, each its own form — so booking a delivery of nine lines was
    nine round trips, nine page rebuilds and nine flash messages, with the
    invoice in somebody's other hand the whole time. It is one form and one
    Save now.

    **Two columns, because there are two questions and they are not the same
    one.** *Received* is a delta: the invoice says twelve arrived, and nobody
    wants to add twelve to the current figure in their head first. *Counted*
    is an absolute: the shelf was counted and holds nine, whatever the app
    believed — the same shape every correction in this app takes, and the
    reason it is here rather than being expressible as a delta is that an
    absolute heals whatever went unrecorded before it.

    Blank means untouched, which is what makes a bill of nine lines cheap on
    a page of forty products. Counted wins if somebody fills in both, which
    is the rule the single-row endpoint used and the right one: a count is a
    measurement and a delivery note is a claim about a change.

    **Nothing is applied unless every line reads.** A bill is one document,
    and half of one booked in is worse than none — the missing half is
    invisible afterwards. So an unreadable figure re-renders the page with
    everything still typed and the bad line named, rather than applying what
    parsed and reporting the rest.

    No `InventoryLog` here, which is the same as before this change rather
    than an omission introduced by it. Raw stock is an opening balance that
    gets counted and topped up, not a ledger of movements; the finished side
    is where provenance is tracked.
    """
    category = get_object_or_404(RawProductCategory, pk=category_id)
    products = list(
        RawProduct.objects.filter(category=category, is_active=True).order_by("name")
    )

    typed, errors = {}, {}
    if request.method == "POST":
        typed, errors = _read_raw_lines(request, products)
        if not errors:
            applied = _apply_raw_lines(typed)
            if applied:
                messages.success(
                    request,
                    f"Booked {len(applied)} line{'' if len(applied) == 1 else 's'}: "
                    + ", ".join(applied)
                    + ".",
                )
            else:
                messages.info(request, "Nothing filled in, so nothing changed.")
            return redirect("raw_inventory", category_id=category.pk)
        messages.error(
            request,
            "Nothing was booked — a bill goes in whole or not at all. Fix the "
            "line below and save again.",
        )

    return render(request, "scarves/raw_inventory.html", {
        "category": category,
        "products": products,
        "all_categories": RawProductCategory.objects.all().order_by("name"),
        "typed": typed,
        "errors": errors,
    })


def _read_raw_lines(request, products):
    """What was typed, per product, and what didn't read.

    Returns `(typed, errors)` both keyed by pk. `typed` comes back whether or
    not it parsed, because it is what the page re-renders with — losing a
    nine-line bill to one fat-fingered digit is the expensive failure here.
    """
    typed, errors = {}, {}
    for product in products:
        received = (request.POST.get(f"received_{product.pk}") or "").strip()
        counted = (request.POST.get(f"counted_{product.pk}") or "").strip()
        if not received and not counted:
            continue

        entry = {"product": product, "received": received, "counted": counted}
        typed[product.pk] = entry

        # Counted wins, so it is the one checked first — a bad delta beside a
        # good count is not a reason to refuse the count.
        if counted:
            try:
                value = int(counted)
            except ValueError:
                errors[product.pk] = f"“{counted}” isn't a count."
                continue
            if value < 0:
                errors[product.pk] = "A count can't be negative."
                continue
            entry["set_to"] = value
        else:
            try:
                # Signed on purpose: a return to the supplier is a delivery
                # note with a minus in front of it, and it was reachable
                # before through the row's -1 button.
                entry["delta"] = int(received)
            except ValueError:
                errors[product.pk] = f"“{received}” isn't a number received."
    return typed, errors


def _apply_raw_lines(typed):
    """Write the lines that changed something. Returns a sentence each.

    `save()` rather than a queryset `update()`, because a `post_save` signal
    on `RawProduct` is what mirrors the count onto a passthrough's finished
    row — and a passthrough is one pile with one row allowed to count it.
    An `update()` here would leave the two disagreeing silently, in the
    direction that decides when to reorder.
    """
    applied = []
    for entry in typed.values():
        product = entry["product"]
        before = product.number_on_hand
        if "set_to" in entry:
            after = entry["set_to"]
        else:
            after = max(before + entry["delta"], 0)
        if after == before:
            continue
        product.number_on_hand = after
        product.save()
        applied.append(f"{product.name} {before}→{after}")
    return applied


@require_POST
@login_required
def dye_create(request):
    """Add a dye from a recipe picker, and hand back the option for it.

    Answers with the same data attributes `DyeSelect` renders, so the script
    can put the new dye into every picker on the page without a reload —
    which is the point. Reloading to pick up a dye you just named would throw
    away the four rows typed either side of it.

    A name that already belongs to a dye returns *that* dye with
    `created: false` rather than an error. The script's job at that moment is
    to select something, and the honest answer to "add Peacock Blue" when
    peacock blue exists is to hand back the one that exists — an error would
    leave the row empty and the person retyping a name that was right.
    """
    form = NewDyeForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"error": " ".join(form.errors.get("name", ["That name won't work."]))},
            status=400,
        )

    dye, created = form.save()
    return JsonResponse({
        "created": created,
        "id": dye.pk,
        "label": str(dye),
        "attrs": dye_option_attrs(dye),
    })


@page_meta(
    title="Quick Recipe Entry",
    description="Internal form for adding up to 5 recipes at once. The dye "
                "boxes filter as you type, and a dye that isn't on the list "
                "can be added without leaving the page.",
    category="Recipes",
)
@login_required
def quick_recipe_entry(request):
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
                {"forms": bound_forms},
            )

        if saved_count:
            return redirect("quick_recipe_entry")

        # nothing saved, but no errors → just reload
        return redirect("quick_recipe_entry")

    return render(request, "scarves/quick_recipe_entry.html", {"forms": forms})

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
    """Confirm each colorway's rainbow sections, on two independent axes.

    **Confirmed-or-not is one question; sellable-or-not is a different one**,
    and collapsing them into one row of pills is what made the page noisy. A
    recipe with no active product under it prints on no sheet and hangs on no
    peg, so confirming its bands changes nothing anybody can see today — but
    it is still worth doing eventually, which is why it is filtered rather
    than dropped. The pair that matters is "has a product **and** isn't
    confirmed": those are the colorways a customer can ask for and the sheet
    is currently leaving out.

    Both filters ride in the query string, so a particular view of the work
    is a link somebody can send, and every pill carries the other axis with
    it rather than resetting it.

    Counts are scoped to whatever is on screen. A pill reading "Unconfirmed
    57" over a list of nine is the page contradicting itself, and the number
    people act on is the one beside the list they are looking at.
    """
    todo_only = request.GET.get("todo") == "true"
    products_only = request.GET.get("with_products") == "true"

    scope = Recipe.objects.filter(is_active=True)
    if products_only:
        # `distinct` because a colorway is normally dyed onto several blanks,
        # and the join would otherwise list it once per product.
        scope = scope.filter(finished_products__is_active=True).distinct()

    recipes = scope.prefetch_related(
        "recipe_dyes__dye", "finished_products__images"
    ).order_by("name")
    if todo_only:
        recipes = recipes.filter(bands_confirmed_at__isnull=True)

    rows = [_classify_row(recipe) for recipe in recipes]

    total = scope.count()
    confirmed = scope.filter(bands_confirmed_at__isnull=False).count()
    # Stated whichever way the filter is set, because it is the whole reason
    # the filter exists: what is left to do on the colorways that are
    # actually for sale.
    with_products = (
        Recipe.objects.filter(is_active=True, finished_products__is_active=True)
        .distinct()
        .count()
    )

    return render(
        request,
        "scarves/color_classify.html",
        {
            "rows": rows,
            "todo_only": todo_only,
            "products_only": products_only,
            "total_count": total,
            "confirmed_count": confirmed,
            "todo_count": total - confirmed,
            "with_products_count": with_products,
            # Each pill's destination, built here so the template isn't doing
            # querystring arithmetic — one axis toggles, the other is carried.
            "all_href": _classify_href(False, products_only),
            "todo_href": _classify_href(True, products_only),
            "products_href": _classify_href(todo_only, not products_only),
            "bands": colorbands.BANDS,
        },
    )


def _classify_href(todo, with_products):
    """`private/colors/` with those two filters set."""
    params = []
    if todo:
        params.append("todo=true")
    if with_products:
        params.append("with_products=true")
    return reverse("color_classify") + ("?" + "&".join(params) if params else "")


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
    products = _recipe_products(recipe)
    context = {"recipe": recipe, "products": products}
    context.update(_recipe_history(request, recipe, products))
    return render(request, "scarves/recipe_detail.html", context)


@login_required
def recipe_history(request, pk):
    """The history half of the recipe page, for a chip click.

    The chips sit under a long page, so following a link meant landing back at
    the top and scrolling down again for every filter — paid on every click.
    Swapping instead leaves the reader where they are.

    Returns the figures and the focus note out-of-band with it. Those are
    above the fold and follow the same filter, so leaving them behind would
    put a colorway-wide total over a one-product list — the contradiction the
    scoping rule exists to prevent, made invisible until somebody scrolls up.
    """
    recipe = get_object_or_404(Recipe, pk=pk)
    products = _recipe_products(recipe)
    context = {"recipe": recipe, "products": products}
    context.update(_recipe_history(request, recipe, products))
    return render(request, "scarves/partials/recipe_history_swap.html", context)


def _recipe_products(recipe):
    """This colorway on every blank it is dyed on, retired ones last."""
    return list(
        recipe.finished_products
        .select_related("raw_product", "raw_product__category")
        .prefetch_related("images")
        .order_by("-is_active", "name")
    )


def _recipe_history(request, recipe, products):
    """Everything the `?product=` chip filter decides, for either renderer.

    One function because the page and the fragment must agree about what a
    filter means; two would drift, and the drift shows as a swapped-in view
    disagreeing with the one a refresh produces.
    """
    # Which product's history is on screen. There is no per-finished-product
    # page anywhere in this app, so without this a colorway on four blanks
    # gives one interleaved column and "what has this one actually done" is
    # unanswerable. A chip rather than a separate table each, because the
    # combined view is the one that shows a session across bases as a session.
    #
    # In the query string, like every other filter here, so a reading is a
    # link somebody can send. An unreadable or unknown id falls back to the
    # whole recipe rather than erroring — a stale link is navigation, and the
    # worst it should do is show more than was asked for.
    by_pk = {p.pk: p for p in products}
    try:
        focus = by_pk.get(int(request.GET.get("product") or 0))
    except ValueError:
        focus = None
    scope = [focus] if focus else products

    # One query for the whole history rather than one per product.
    logs = (
        InventoryLog.objects
        .filter(finished_product__in=scope)
        .select_related("finished_product")
        .order_by("-created_at")[: RECIPE_LOG_LIMIT + 1]
    )
    logs = list(logs)
    truncated = len(logs) > RECIPE_LOG_LIMIT
    logs = logs[:RECIPE_LOG_LIMIT]

    # Lifetime movement, computed over every log rather than the page's slice —
    # a truncated history would otherwise quietly understate the totals — and
    # over the *scope*, not the recipe, because a total that disagrees with
    # the list under it is the page contradicting itself. Same rule the colour
    # page's pills follow.
    totals = (
        InventoryLog.objects
        .filter(finished_product__in=scope)
        .values("log_type")
        .annotate(qty=Sum("quantity"), entries=Count("id"))
    )
    by_type = {row["log_type"]: row for row in totals}

    def _qty(log_type):
        return (by_type.get(log_type) or {}).get("qty") or 0

    # How many rows each chip stands for, in one grouped query rather than
    # one per product.
    chip_counts = {
        row["finished_product"]: row["n"]
        for row in (
            InventoryLog.objects
            .filter(finished_product__in=products)
            .values("finished_product")
            .annotate(n=Count("id"))
        )
    }
    page_url = reverse("recipe_detail", args=[recipe.pk])
    swap_url = reverse("recipe_history", args=[recipe.pk])
    chips = [
        {
            "product": product,
            "count": chip_counts.get(product.pk, 0),
            # Two URLs per chip on purpose: the fragment is what htmx fetches,
            # the page URL is what it pushes. Pushing the fragment's would put
            # an address in the bar that renders a bare table on reload.
            "url": f"{page_url}?product={product.pk}",
            "history_url": f"{swap_url}?product={product.pk}",
            "current": focus is not None and product.pk == focus.pk,
        }
        for product in products
    ]

    return {
        "logs": logs,
        "truncated": truncated,
        "log_limit": RECIPE_LOG_LIMIT,
        "focus": focus,
        "chips": chips,
        "all_url": page_url,
        "all_history_url": swap_url,
        "all_count": sum(chip_counts.values()),
        "on_hand": sum(p.number_on_hand for p in scope),
        "par_total": sum(p.par or 0 for p in scope),
        "produced": _qty(InventoryLog.PRODUCTION),
        # Sales are recorded negative; show the count as a positive number.
        "sold": -_qty(InventoryLog.SALE),
        "adjusted": _qty(InventoryLog.ADJUSTMENT),
        "log_count": sum(row["entries"] for row in totals),
        "scope_count": len(scope),
    }


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

    **This form means exactly one thing: baths dyed now, and stock moves.**
    It used to carry an optional back-date, folded into a disclosure *below*
    the submit button — so one button meant two materially different things
    (move stock, or write history and don't), and the switch deciding which
    was under it and closed by default. Somebody reading top to bottom
    reached the button before learning the option existed.

    Typing up old sessions has its own door at `private/cards/`, which is
    better at it: a kanban card is a column of dates and bath counts, it
    parses a month-only date honestly, and nothing on it can move current
    stock at all. Two doors to one job is how the ambiguous one survives, and
    this was the ambiguous one.
    """
    recipe = get_object_or_404(Recipe, pk=pk)

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
                source=InventoryLog.SOURCE_RECIPE_PAGE,
                quantity=quantity,
                notes=(
                    f"{baths} dye bath{'' if baths == 1 else 's'} × {per_bath}, "
                    f"recorded from the {recipe.name} recipe page."
                ),
            )
            made += quantity

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
        # A kanban card records a dye bath, so anything not made in one has
        # nothing to backfill and shouldn't be offered: an undyed passthrough
        # (no recipe) or a fancy veil (a colorway, but line work rather than
        # a bath).
        FinishedProduct.objects.filter(
            is_active=True,
            recipe__isnull=False,
            raw_product__made_in_a_dye_bath=True,
        )
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
                    source=InventoryLog.SOURCE_CARD_BACKFILL,
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
                        # Par comes from the blank, not the field default, and
                        # only on creation — an existing product's par is
                        # someone's decision and this form never asked about it.
                        fp.par = rp.finished_par_default
                        fp.save(update_fields=["par"])
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


#: The handful of reasons a counted number actually differs from a recorded
#: one. Offered as a list because a free box asks someone to compose a
#: sentence at the exact moment they want to be finished, and what you get
#: back is blank — which is the state this whole field exists to end.
#:
#: They are stored as their own text, not as codes. The value of a reason is
#: that it reads back plainly in `InventoryLog.notes` two seasons later, and a
#: code would need this list to still exist and still mean the same thing.
#:
#: Both directions are here because a bulk count moves either way, and the
#: pair that gets confused is "we have more than I thought" versus "these came
#: back" — same arithmetic, different stories.
BULK_REASON_PRESETS = [
    "Found items",
    "Recount",
    "Returned from display",
    "Damaged or unsellable",
    "Lost or missing",
]

#: Blank is a real answer and stays first — a count with no reason is still
#: worth having, and demanding one would cost the correction to punish the
#: omission.
BULK_REASON_CHOICES = [("", "—")] + [(r, r) for r in BULK_REASON_PRESETS]


def bulk_reason(preset, free_text):
    """One reason line from a picked preset and anything typed beside it.

    Both, when there are both: "Found items" says which kind of thing
    happened and "under the cutting table" says which one. Neither is a
    substitute for the other, so joining them keeps the categorisation a list
    gives you without throwing away the detail only a person has.
    """
    return " — ".join(p for p in (preset.strip(), free_text.strip()) if p)


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
        # Per row, because one save can hold two different stories — most of
        # the rack recounted, and one row that moved for its own reason. A
        # single form-level box would make the rarer one either unsayable or
        # a lie about every other row it lands on.
        fields[f"reason_preset_{fp.id}"] = forms.ChoiceField(
            required=False,
            choices=BULK_REASON_CHOICES,
            label=f"Why {fp.name} changed",
            widget=forms.Select(attrs={"class": "row-reason-preset"}),
        )
        fields[f"reason_{fp.id}"] = forms.CharField(
            required=False,
            max_length=200,
            label=f"More about {fp.name}",
            widget=forms.TextInput(attrs={
                "placeholder": "or say it yourself",
                "class": "row-reason",
            }),
        )
    # And once for the whole save, since a bulk count is usually a single act
    # — you walked the rack once. Rows fall back to this, so the common case
    # is one choice rather than forty.
    fields["reason_preset"] = forms.ChoiceField(
        required=False,
        choices=BULK_REASON_CHOICES,
        label="Why (applies to every row you change)",
        widget=forms.Select(attrs={"class": "form-reason-preset"}),
    )
    fields["reason"] = forms.CharField(
        required=False,
        max_length=200,
        label="Anything to add",
        widget=forms.TextInput(attrs={
            "placeholder": "e.g. counted the display rack in with the back stock",
            "class": "form-reason",
        }),
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
            form_reason = bulk_reason(
                form.cleaned_data.get("reason_preset") or "",
                form.cleaned_data.get("reason") or "",
            )
            with transaction.atomic():
                for fp in finished_products:
                    new_val = form.cleaned_data.get(f"count_{fp.id}")
                    if new_val is None or new_val == fp.number_on_hand:
                        continue

                    delta = new_val - fp.number_on_hand
                    # Through the model, because an undyed passthrough's count
                    # lives on its raw product — see set_on_hand.
                    fp.set_on_hand(new_val)

                    # The row wins over the form, whole: it is the more
                    # specific answer, and someone who filled one in on this
                    # row meant it to describe this row. Falling back
                    # field-by-field would blend a row's preset with the
                    # form's free text and produce a sentence nobody wrote.
                    reason = bulk_reason(
                        form.cleaned_data.get(f"reason_preset_{fp.id}") or "",
                        form.cleaned_data.get(f"reason_{fp.id}") or "",
                    ) or form_reason
                    InventoryLog.objects.create(
                        finished_product=fp,
                        raw_product=fp.raw_product,
                        log_type=InventoryLog.ADJUSTMENT,
                        source=InventoryLog.SOURCE_BULK_UPDATE,
                        quantity=delta,
                        notes=(
                            f"Bulk inventory update — {reason}" if reason
                            else "Bulk inventory update."
                        ),
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
            {
                "fp": fp,
                "field": form[f"count_{fp.id}"],
                "reason_preset_field": form[f"reason_preset_{fp.id}"],
                "reason_field": form[f"reason_{fp.id}"],
            }
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

    order = result.body.get("order", {})
    line_items = order.get("line_items", [])
    sold_at = _order_sold_at(order)

    with transaction.atomic():
        for item in line_items:
            variation_id = item.get("catalog_object_id")
            qty = int(item.get("quantity", "0"))
            if qty == 0:
                continue

            fp = None
            if variation_id:
                fp = FinishedProduct.objects.filter(
                    square_variation_id=variation_id, is_active=True
                ).first()

            if fp is None:
                # Not a product this app knows: rung up as a generic item, sold
                # as a custom amount, or a variation that was never synced.
                # This used to `continue`, which meant a scarf nobody could
                # name left the tent and nothing anywhere recorded it — Square
                # had the money, this app still had the stock, and neither said
                # they disagreed. Now it goes in a queue a person empties.
                UnmatchedSale.objects.get_or_create(
                    order_id=order_id,
                    line_uid=item.get("uid") or "",
                    defaults={
                        "name": item.get("name") or "",
                        "variation_name": item.get("variation_name") or "",
                        "square_variation_id": variation_id or "",
                        "quantity": qty,
                        "amount_cents": (
                            item.get("total_money", {}).get("amount") or 0
                        ),
                        "sold_at": sold_at,
                    },
                )
                continue

            # Square sends order.updated more than once for an order, and
            # COMPLETED is not a one-shot state — so without this a re-delivery
            # decrements the same sale again. One line item is one row: a
            # genuine second sale of the same product arrives on its own order.
            already = InventoryLog.objects.filter(
                finished_product=fp,
                sale_reference=order_id,
                log_type=InventoryLog.SALE,
            ).exists()
            if already:
                continue

            if fp.is_passthrough:
                # One pile, and the raw row is the one that holds it. Writing
                # the finished count here instead would be writing to a mirror
                # — the next raw save would overwrite it, and the reorder
                # signal this stock exists for would never move.
                raw = fp.raw_product
                raw.number_on_hand = max(raw.number_on_hand - qty, 0)
                raw.save(update_fields=["number_on_hand"])   # signal mirrors down
            else:
                fp.number_on_hand = max(fp.number_on_hand - qty, 0)
                fp.save(update_fields=["number_on_hand"])

            InventoryLog.objects.create(
                finished_product=fp,
                raw_product=fp.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
                quantity=-qty,
                sale_reference=order_id,
                notes=f"Square sale via webhook.",
            )

    return HttpResponse(status=200)


def _order_sold_at(order):
    """When Square says the order happened, not when we heard about it.

    The reconciliation screen pairs a sale with a photo taken within fifteen
    minutes of it, so this timestamp is the join key — using receipt time
    instead would drift by however long the webhook took to arrive, or by a
    whole redelivery.
    """
    for field in ("closed_at", "created_at"):
        raw = order.get(field)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return timezone.now()


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
        # The same category, two orderings. Counted here rather than left to
        # the click because the by-colour sheet is longer than the by-name one
        # (a colorway prints once per section it claims) and skips whatever
        # nobody has classified — both are things to know before printing.
        category.band_pages, category.unclassified = _by_color_counts(category)

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
# The same category, ordered by the rainbow instead of by colorway.
#
# The sheet above answers "what does this colorway look like?". This one
# answers the question a customer actually asks — "what have you got in red?"
# — off the same category and the same picker, because Yarn and Silk is how
# the stall is laid out and a sheet is printed per table, not per style.
# ---------------------------------------------------------------------------


def _by_color_pages(category):
    """`(slug, label, color, recipe, items)` for one category, rainbow order.

    One entry is one page, and it holds the same thing a page of the by-name
    sheet holds: a colorway, its photos, and a barcode for every style dyed in
    it. What changes is the order and the repetition.

    A colorway claiming red and blue yields two entries, which is the entire
    point: the dyes aren't blended, so a red-and-blue scarf is genuinely in
    both sections, and printing it once leaves it missing from one of them
    (same reasoning as `colorbands`).

    Unconfirmed recipes are left out rather than guessed at. `colorbands` gets
    roughly 85% of these right, and the 15% is silent — you look under orange,
    the scarf isn't there, and nothing tells you it was filed under red.
    """
    items_by_recipe = {}
    for fp in (
        FinishedProduct.objects.filter(
            raw_product__category=category,
            is_active=True,
            raw_product__is_active=True,
            recipe__is_active=True,
            recipe__bands_confirmed_at__isnull=False,
        )
        .select_related("recipe", "raw_product")
        .prefetch_related("images")
        .order_by("raw_product__name")
    ):
        items_by_recipe.setdefault(fp.recipe, []).append(fp)

    recipes = sorted(items_by_recipe, key=lambda r: r.name)
    return [
        (slug, label, color, recipe, items_by_recipe[recipe])
        for slug, label, color in colorbands.BANDS
        for recipe in recipes
        if slug in (recipe.color_bands or [])
    ]


def _by_color_counts(category):
    """What the by-colour link on the picker promises: pages, and what's
    missing. Two different silences, kept apart — an unconfirmed colorway is
    work someone still has to do, while a confirmed one claiming no band is a
    decision already taken, and merging them would keep re-raising the settled
    one."""
    recipes = (
        Recipe.objects.filter(
            finished_products__raw_product__category=category,
            finished_products__is_active=True,
            finished_products__raw_product__is_active=True,
            is_active=True,
        )
        .distinct()
        .values_list("color_bands", "bands_confirmed_at")
    )
    pages = sum(len(bands or []) for bands, confirmed in recipes if confirmed)
    unclassified = sum(1 for _, confirmed in recipes if not confirmed)
    return pages, unclassified


def _band_tab_painter(page_bands):
    """Paint each page's colour as a tab on the right edge, thumb-index style.

    The band is named on the page too, but the tab is what makes a printed
    stack usable: fan it and the sections are visible from the edge, which is
    how someone standing at the stall finds red without reading anything. Its
    slot is fixed per band, so the gaps in a stack tell you which sections that
    category doesn't have.

    Keyed on page number, which holds because the story puts exactly one entry
    on each page (`KeepInFrame` shrinks rather than splitting).
    """
    from reportlab.lib import colors
    from reportlab.lib.units import inch

    slots = len(colorbands.BANDS)

    def paint(canvas, doc):
        index = canvas.getPageNumber() - 1
        if index >= len(page_bands):
            return
        slug, label, color = page_bands[index]
        slot = colorbands.BAND_SLUGS.index(slug)

        page_w, page_h = doc.pagesize
        top = page_h - doc.topMargin
        height = (page_h - doc.topMargin - doc.bottomMargin) / slots
        y = top - (slot + 1) * height
        width = 0.28 * inch
        x = page_w - doc.rightMargin + 0.10 * inch

        canvas.saveState()
        if slug == colorbands.RAINBOW:
            # **No single colour can say rainbow**, and picking one would make
            # the tab a lie in the one place a tab is read without reading:
            # fanned, from the edge. So the section draws the spectrum in
            # stripes, which is recognisable at a glance and survives a
            # photocopy as a banded block rather than as a flat grey the same
            # as its neighbours.
            stripes = colorbands.CHROMATIC
            band_h = height / len(stripes)
            for i, stripe in enumerate(stripes):
                canvas.setFillColor(colors.HexColor(colorbands.BAND_COLORS[stripe]))
                canvas.rect(x, y + i * band_h, width, band_h + 0.5, stroke=0, fill=1)
            # The label needs a plate to sit on: rotated text over eight
            # colours is unreadable in either ink, and this is the tab whose
            # name matters most because it is the one nobody expects.
            plate_h = min(height * 0.5, 0.9 * inch)
            canvas.setFillColor(colors.white)
            canvas.rect(x, y + (height - plate_h) / 2, width, plate_h, stroke=0, fill=1)
            canvas.setFillColor(colors.black)
        else:
            canvas.setFillColor(colors.HexColor(color))
            canvas.rect(x, y, width, height, stroke=0, fill=1)
            # Yellow, orange, pink and grey are too light to carry white text;
            # the rest are too dark to carry black. Cheap luminance rather
            # than a lookup nobody would remember to update when a band colour
            # changes — the label has to survive a black-and-white photocopy.
            r, g, b = colors.HexColor(color).rgb()
            canvas.setFillColor(
                colors.black if (0.299 * r + 0.587 * g + 0.114 * b) > 0.5 else colors.white
            )
        canvas.setFont("Helvetica-Bold", 8)
        canvas.translate(x + width / 2, y + height / 2)
        canvas.rotate(90)
        canvas.drawCentredString(0, -3, label.upper())
        canvas.restoreState()

    return paint


@page_meta(
    title="Reference Sheet by Colour (PDF)",
    description="The same category sheet ordered by the rainbow: a page per "
                "colorway per section it claims, with a colour tab on the "
                "edge, so a red scarf can be found by being red.",
    category="Reference Sheets",
    note="Returns a PDF. Only colorways whose sections have been confirmed are printed.",
    # Same picker as the by-name sheet — this is the other button on the card,
    # not a second directory. The URL keeps the category ahead of the ordering
    # for that reason, which also puts it under the existing picker for
    # PickerPageConventionTests.
    show_in_index=False,
)
def reference_sheet_by_color_pdf(request, category_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, portrait
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepInFrame

    category = get_object_or_404(RawProductCategory, pk=category_id)
    pages = _by_color_pages(category)

    styles = getSampleStyleSheet()
    kicker_style = ParagraphStyle("kicker", parent=styles["Normal"], fontSize=11, leading=13, fontName="Helvetica-Bold", spaceBefore=0, spaceAfter=0)
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
    safety = 0.1 * inch

    story = []
    for position, (slug, label, color, recipe, items) in enumerate(pages):
        if position:
            story.append(PageBreak())

        kicker_p = Paragraph(
            f'<font color="{color}">{label.upper()}</font>', kicker_style
        )
        title_p = Paragraph(recipe.name, title_style)
        sub_p = Paragraph(
            f"{category.name} · {len(items)} item(s)", sub_style
        )
        _, kh = kicker_p.wrap(usable_width, usable_height)
        _, th = title_p.wrap(usable_width, usable_height)
        _, sh = sub_p.wrap(usable_width, usable_height)
        bc_grid = _barcode_grid(items, usable_width, name_style, sku_style)
        _, bc_h = bc_grid.wrap(usable_width, usable_height)

        photo_area = (
            usable_height - kh - th - sh - top_gap - mid_gap - bc_h - safety
        )
        gallery = None
        if photo_area > 1.2 * inch:
            gallery = _photo_gallery(
                _select_recipe_photos(items, cap=4), usable_width, photo_area
            )

        block = [kicker_p, title_p, sub_p, Spacer(1, top_gap)]
        if gallery is not None:
            block += [gallery, Spacer(1, mid_gap)]
        else:
            block.append(Spacer(1, max(photo_area + mid_gap, 0)))
        block.append(bc_grid)

        story.append(KeepInFrame(usable_width, usable_height, block, mode="shrink"))

    if not story:
        story = [Paragraph(
            f"{category.name} — no colorways with confirmed colour sections yet.",
            styles["h1"],
        )]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=portrait(letter),
        topMargin=margin,
        bottomMargin=margin,
        leftMargin=margin,
        rightMargin=margin,
    )
    painter = _band_tab_painter([p[:3] for p in pages])
    doc.build(story, onFirstPage=painter, onLaterPages=painter)
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
    """The upload page, plus the blanks you can say you are shooting.

    **Saying what is in front of the camera is what makes a failed decode
    cheap.** Roughly half the barcodes in a session of forty photos don't
    read — a phone, a small Code128 on a hang tag, whatever the light is
    doing — and each miss then costs a product name typed out in full on a
    phone next to a pile of scarves. But a photo session is a *pile of one
    blank*: forty half-circle veils, then forty sash belts. So the blank is
    known before the first shot and stays true for the whole pile, which is
    exactly the half of `BLANK-DYEBATH` the picker can fill in for you.

    It fills the box in; it does not decide anything. The barcode still wins
    whenever it reads, and the prefill is ordinary editable text — same
    bargain `colorbands` and the crew cookie make.
    """
    # Only blanks that have something photographable under them. A blank
    # whose colorways are all retired would be a line in the menu that
    # narrows to nothing.
    blanks = [
        {"name": raw.name, "prefix": skus.slug(raw.name)}
        for raw in RawProduct.objects.filter(
            is_active=True, finished_products__is_active=True
        )
        .distinct()
        .order_by("name")
    ]
    return render(
        request,
        "scarves/image_upload.html",
        {"use_s3": settings.USE_S3, "blanks": blanks},
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

    scanned = None
    for code in codes:
        if not code:
            continue
        scanned = FinishedProduct.objects.filter(sku=code).first()
        if scanned:
            upload.detected_sku = code
            break

    # **On a walk, the peg is the claim and it beats a barcode that
    # disagrees.** The blank picker on the batch page is a coarse statement
    # covering forty photos, so there a decoded symbol is the better evidence.
    # A stop is the opposite: made per photo, at the peg, by somebody looking
    # at the scarf — while a symbol that happens to resolve in shot may belong
    # to the colorway hanging two inches to the left. Reported either way,
    # because a silent resolution in either direction is how a photo ends up
    # on the wrong colorway with nothing to say so.
    stop = _walk_stop(request.POST)
    expected = stop["product"] if stop else None
    product = expected or scanned
    mismatch = scanned if expected and scanned and scanned != expected else None

    if product:
        fpi = _attach_image(upload, product)
        upload.finished_product = product
        upload.product_image = fpi
        upload.status = ProductImageUpload.STATUS_MATCHED
        upload.save()
        return render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True, "mismatch": mismatch,
                       "expected": expected})

    # No barcode / no match -> uploader assigns it inline, with the blank
    # they said they were shooting already typed into the box. Run through
    # `slug` rather than trusted as sent: it is a value from the page, it
    # goes straight into a search box, and `slug` is the same function that
    # built the SKU half it is meant to match.
    upload.save()

    # **An empty peg is the fresh-board case, and it is the main one.** Set
    # the display up, walk it once, and come away with the photos *and* the
    # map — which only works if naming the colorway is quick. So the photo
    # that was just taken orders the list: the bands it shows against the
    # bands each colorway claims, exact first, then supersets, then any
    # overlap, then the rest alphabetically.
    #
    # Ordering only. A band set is not an identity — dozens of colorways are
    # blue-and-green — so this moves the answer near the top and the person
    # holding the scarf does the rest. Same rule `colorbands` follows
    # everywhere else: fill the form in, never decide.
    candidates = []
    confirmed = total = 0
    photo_bands = []
    if stop and expected is None:
        if data is not None:
            try:
                photo_bands = colorbands.bands_from_image(BytesIO(data))
            except Exception:
                photo_bands = []
        candidates, _ = photowalk.candidates(stop["fixture"], photo_bands)
        confirmed, total = photowalk.rankable(stop["fixture"])

    return render(request, "scarves/partials/upload_card.html",
                  {"upload": upload, "needs_assign": True,
                   "prefix": skus.slug(request.POST.get("prefix")),
                   # An empty peg: pick the colorway and it lands on the peg
                   # as well as on the photo. Carried through the search so
                   # the buttons it returns can do both — see `assign_upload`.
                   "stop": stop,
                   "candidates": candidates,
                   "photo_bands": photo_bands,
                   "band_names": [
                       colorbands.BAND_LABELS.get(b, b) for b in photo_bands
                   ],
                   # Stated, because a list that fell back to alphabetical for
                   # want of confirmed bands looks exactly like one where the
                   # photo matched nothing — and only one of those has a fix.
                   "confirmed_count": confirmed,
                   "candidate_total": total})


@page_meta(
    title="Photograph a Display",
    description="Go round a board peg by peg and photograph what hangs there. The "
                "peg says what the picture is of, so nothing has to be typed "
                "or scanned.",
    category="Products",
)
@login_required
def photo_walk_index(request):
    """The boards, as places to photograph rather than places to restock."""
    fixtures = []
    for fixture in DisplayFixture.objects.filter(is_active=True).select_related(
        "raw_product"
    ):
        walk = photowalk.stops(fixture)
        products = [stop["product"] for stop in walk if stop["product"]]
        # Counted here because it is the only number that decides which board
        # to walk: how much of it the catalogue still has no picture of.
        missing = [p for p in products if not p.images.exists()]
        fixtures.append({
            "fixture": fixture,
            "stops": len(walk),
            "assigned": len(products),
            "missing": len(missing),
        })
    # Most to photograph first, which is the only ordering worth having here.
    fixtures.sort(key=lambda entry: (-entry["missing"], entry["fixture"].name))
    return render(request, "scarves/photo_walk_index.html", {"fixtures": fixtures})


@page_meta(
    title="Photograph a Display (one board)",
    description="One peg at a time: what to shoot, whether it already has a "
                "photo, and where you were when you stopped.",
    category="Products",
    show_in_index=False,
)
@login_required
def photo_walk(request, fixture_id):
    """One stop on the walk. **Where you are is the URL and nothing else.**

    Fifteen photos in, get distracted, come back to `?row=3&column=5` — a
    stored cursor would be a second place the answer lived, and the one it
    disagreed with would be the one somebody was looking at. It also means a
    peg can be handed to somebody else as a link.

    An address that isn't a stop advances to the next one that is, so a
    bookmark taken before the board was rearranged resumes rather than
    failing — see `photowalk.stop_at`.
    """
    fixture = get_object_or_404(DisplayFixture, pk=fixture_id, is_active=True)

    def _int(name):
        try:
            return int(request.GET[name])
        except (KeyError, TypeError, ValueError):
            return None

    stop, walk = photowalk.stop_at(fixture, _int("row"), _int("column"))
    product = stop["product"] if stop else None

    return render(request, "scarves/photo_walk.html", {
        "fixture": fixture,
        "stop": stop,
        "product": product,
        "label": photowalk.label_for(product),
        # Stated so a photo that already exists is a choice rather than a
        # surprise: retake it, or move on. Nothing here decides which.
        "existing": list(product.images.all()[:3]) if product else [],
        # Plain navigation. "Peg 17 of 42" is how somebody knows roughly how
        # much board is left, not a score — nothing counts walks, and there is
        # no completeness anywhere.
        "index": (
            next(
                (
                    i + 1
                    for i, candidate in enumerate(walk)
                    if (candidate["row"], candidate["column"])
                    == (stop["row"], stop["column"])
                ),
                None,
            )
            if stop
            else None
        ),
        "total": len(walk),
        "next_url": _walk_url(fixture, photowalk.next_after(walk, stop)) if stop else "",
        "done": stop is None,
        "use_s3": settings.USE_S3,
    })


def search_products(q, limit=10):
    """Active products matching a typed name or SKU.

    One definition, three callers — the upload page's picker, the label
    page's hand-picked list, and the close page's "I'm holding a tag for
    this". They differ in what a result *does*, never in what counts as a
    match, and a second copy of the query is how one of them quietly starts
    finding a different set of products.
    """
    q = (q or "").strip()
    if not q:
        return FinishedProduct.objects.none()
    return FinishedProduct.objects.filter(
        Q(name__icontains=q) | Q(sku__icontains=q),
        is_active=True,
    ).order_by("name")[:limit]


@login_required
def product_search(request):
    """HTMX type-ahead: products matching the typed name or SKU."""
    q = (request.GET.get("q") or "").strip()
    upload_id = request.GET.get("upload_id")
    for_labels = request.GET.get("mode") == "labels"
    # Passed straight through to the assign call's URL. The search itself is
    # unchanged by it — a walk narrows nothing, because the whole reason this
    # peg is being typed into is that the map doesn't know what hangs there.
    stop_query = _stop_query(_walk_stop(request.GET))

    products = search_products(q)

    # Same search, two click behaviours: the upload page assigns the product
    # to an upload, the label page adds it to a list. Only the template
    # differs, so it's picked here rather than duplicating the query.
    template = (
        "scarves/partials/label_item_results.html" if for_labels
        else "scarves/partials/product_search_results.html"
    )
    return render(request, template, {
        "products": products, "upload_id": upload_id, "stop_query": stop_query,
    })


def _walk_stop(data):
    """The peg a photo was taken at, from whatever the page sent.

    `None` on the batch page, which is not standing anywhere. Anything
    unparseable is also `None` rather than an error: the walk is navigation,
    and the worst a bad address can do is fall back to filing the photo the
    way the batch page would.
    """
    try:
        fixture_id = int(data.get("fixture"))
        row = int(data.get("row"))
        column = int(data.get("column"))
    except (TypeError, ValueError):
        return None

    fixture = DisplayFixture.objects.filter(pk=fixture_id, is_active=True).first()
    if fixture is None:
        return None
    position = fixture.positions.filter(row=row, column=column).first()
    if position is not None and not position.is_home:
        return None
    return {
        "fixture": fixture,
        "row": row,
        "column": column,
        "position": position,
        "product": position.finished_product if position else None,
    }


def _stop_query(stop):
    """`?fixture=…&row=…&column=…`, or empty off a walk."""
    if not stop:
        return ""
    return (
        f"?fixture={stop['fixture'].pk}&row={stop['row']}&column={stop['column']}"
    )


@require_POST
@login_required
def assign_upload(request, upload_id):
    """File a manually-picked product for an upload the barcode couldn't match.

    On a walk it does a second thing: **an empty peg gets the colorway you
    just picked.** You are standing in front of the hook, you have just said
    what is hanging on it, and the map not knowing is the reason the walk had
    nothing to tell you here. Making that a separate trip to the map editor
    would mean the fact is known at the wall and recorded nowhere.

    **An occupied peg is never overwritten**, the same refusal
    `copy_board_layout` makes: a peg that already names a colorway is
    somebody's decision, and disagreeing with it is a map question rather than
    a photo one.
    """
    upload = get_object_or_404(ProductImageUpload, id=upload_id)
    product = get_object_or_404(FinishedProduct, id=request.POST.get("product_id"))

    if upload.status not in (ProductImageUpload.STATUS_MATCHED, ProductImageUpload.STATUS_ASSIGNED):
        fpi = _attach_image(upload, product)
        upload.finished_product = product
        upload.product_image = fpi
        upload.status = ProductImageUpload.STATUS_ASSIGNED
        upload.save()

    stop = _walk_stop(request.GET)
    response = render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True})
    if stop is None:
        return response

    position = stop["position"]
    if position is None:
        position = photowalk.position_for(stop["fixture"], stop["row"], stop["column"])
    if position.finished_product_id is None:
        position.finished_product = product
        # The signal on DisplayPosition writes `display_slots` from here, so
        # this peg starts counting towards the close and the restock walk
        # immediately — which is what putting a colorway on the map means.
        position.save(update_fields=["finished_product"])
        messages.success(
            request,
            f"Filed the photo and put {product.name} on "
            f"{stop['fixture'].name} r{stop['row']}c{stop['column']}.",
        )
    else:
        messages.info(
            request,
            f"Filed the photo. Left the peg as it was — it already says "
            f"{position.finished_product.name}.",
        )

    # The walk moves on by navigating, so the redirect rides on the response
    # to the click rather than being a second request the page has to make.
    walk = photowalk.stops(stop["fixture"])
    nxt = photowalk.next_after(walk, stop)
    response["HX-Redirect"] = _walk_url(stop["fixture"], nxt)
    return response


def _walk_url(fixture, stop):
    """Where the walk goes next, or back to the board when it is done."""
    url = reverse("photo_walk", args=[fixture.pk])
    if stop is None:
        return url + "?done=1"
    return f"{url}?row={stop['row']}&column={stop['column']}"


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
    title="Colour Bands",
    description="How a dye's hex becomes a section of the rainbow reference "
                "sheet. Every dye on file as a swatch, plotted by hue, with any "
                "of the boundaries draggable so the judgement calls are visible "
                "rather than asserted.",
    category="Public",
    note="No login required. ?edge=green-blue picks the boundary. Changes nothing.",
)
def color_bands_page(request):
    """A piece about the colour classifier, readable by anyone.

    Almost all template — but not a static file, for two reasons. The dyes are
    read live, so the page picks up whatever gets bought next week rather than
    freezing at the catalogue as it stood the day it was written. And the
    boundaries come from `colorbands.HUE_EDGES`, so a page quoting 70 after
    somebody moved it to 61 can't happen.

    Which boundary you're looking at rides in the query string rather than a
    session, so a particular argument is a link somebody can send.

    Each dye carries the band *Python* gave it. The slider re-classifies in the
    browser for the sake of exploring, and the page says so when the two
    disagree — the same rule the rest of the colour code follows: show the
    guess, never let it pass as the answer.
    """
    edges = []
    for slug, degrees in colorbands.HUE_EDGES:
        below, above = colorbands.edge_bands(degrees)
        if below == above:
            # A line the classifier draws that no mid-tone can see across:
            # 345 separates two zones that differ only in how light a colour
            # has to be to read pink. Offering it would be a slider that
            # appears to do nothing.
            continue
        edges.append({
            "slug": slug,
            "degrees": degrees,
            "below": below,
            "above": above,
            "label": f"{colorbands.BAND_LABELS[below]} / {colorbands.BAND_LABELS[above]}",
        })

    # Yellow/green is the default because it is the argument people actually
    # have — chartreuse and avocado are the jars two reasonable people fall out
    # over. Opening on red/orange, first only because it is first round the
    # wheel, buries that behind a boundary nobody disputes.
    wanted = request.GET.get("edge")
    fallback = next(
        (e for e in edges if e["slug"] == "yellow-green"), edges[0]
    )
    edge = next((e for e in edges if e["slug"] == wanted), None) or fallback

    # Where the slider is sitting rides in the URL beside which line it is,
    # because the page's job is to start an argument and an argument you can't
    # send is one you have to win in person. Parsed defensively for the same
    # reason `done=` is: a hand-edited or stale link should land somewhere
    # readable rather than erroring. The name is the slider's own, so the URL
    # is what the form would have serialised.
    at = edge["degrees"]
    try:
        typed = float(request.GET["cut"])
    except (KeyError, TypeError, ValueError):
        pass
    else:
        if 0 <= typed <= 360:
            at = round(typed * 2) / 2       # the slider's half-degree step

    dyes = []
    for dye in Dye.objects.select_related("brand"):
        rgb = hex_to_rgb(dye.hex_color)
        if not rgb:
            # A dye with no colour contributes nothing here, exactly as it
            # contributes nothing to a band, a palette or a rainbow sheet.
            continue
        r, g, b = (c / 255.0 for c in rgb)
        h, ll, sat = colorsys.rgb_to_hls(r, g, b)
        dyes.append({
            "name": dye.name,
            "hex": dye.hex_color,
            "hue": round(h * 360, 1),
            "sat": round(sat, 3),
            "light": round(ll, 3),
            "band": colorbands.band_for_hex(dye.hex_color),
            "brand": dye.brand.name if dye.brand_id else "",
        })
    dyes.sort(key=lambda d: d["hue"])

    return render(request, "scarves/color_bands.html", {
        "dyes_json": dyes,
        "bands": colorbands.BANDS,
        "edges": edges,
        "edges_json": {e["slug"]: e["degrees"] for e in edges},
        "edge": edge,
        "at": at,
        "dye_count": len(dyes),
    })


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


# --- Timekeeping -----------------------------------------------------------
#
# Two pages that between them replace a paper bag and a lot of mental
# arithmetic: a public form where somebody reports a day's hours, and a staff
# page that adds a Saturday–Friday week up.
#
# The form is under public/ because the whole point is that nobody needs an
# account. What guards it is a four-digit PIN, which is enough to stop the
# wrong name being tapped and not much more — see Employee's docstring. The
# weekly sheet, which shows everyone's hours at once, is staff-only.

#: Wrong PINs tolerated per browser session before the form stops answering.
#: A speed bump, not a lock: sessions are cheap to discard. It costs a casual
#: guesser their patience, and the honest case never sees it.
HOURS_PIN_ATTEMPT_LIMIT = 8


@page_meta(
    title="Crew Handbook",
    description=(
        "What the crew needs to know to work the booth — the till, the "
        "look-up books, sending photos in, and reporting hours. Ends by "
        "handing that person their faire pass. No login: name and PIN, the "
        "same as the other crew pages."
    ),
    category="Booth",
    note="Passes are uploaded per employee in the admin.",
)
@require_http_methods(["GET", "POST"])
def crew_handbook(request):
    """The handbook, and the pass at the bottom of it.

    Two submissions to the same view. The first is the name and PIN, which
    unlocks the text; the second is the "Give me my pass" button at the
    bottom, which additionally wants the box ticked. Putting the button at
    the end of the page is the whole of the scroll enforcement, deliberately
    — anything cleverer means JavaScript, and JavaScript failing here means
    somebody standing at the gate without a pass because their phone had one
    bar. A checkbox costs a tap and can't fail closed.

    Nothing is recorded. There is no read-receipt, no timestamp, no per-season
    version: the tick is a speed bump asking somebody to look at the page, not
    evidence to be produced later. Storing it would invite exactly that use.

    The pass is *downloaded and kept*. Coming back for a lost one means coming
    back through this page, which is cheap — `crew.initial` has already filled
    the name and PIN in on the phone that fetched it the first time.
    """
    if crew.asked_to_forget(request):
        return crew.forget(redirect("crew_handbook"))

    def page(form, *, unlocked, no_pass=False):
        return render(request, "scarves/crew_handbook.html", {
            "form": form,
            "unlocked": unlocked,
            "no_pass": no_pass,
            "remembered": crew.remembered(request)[0],
            "forget_param": crew.FORGET,
        })

    if request.method != "POST":
        form = CrewHandbookForm(user=request.user, initial=crew.initial(request))
        # A signed-in person has already answered the only question the gate
        # asks, so it doesn't get asked. Opening straight onto the text is the
        # point of the login — a screen whose single control is "yes, it's
        # me" is the paperwork this was supposed to remove.
        return page(form, unlocked=form.signed_in_as is not None)

    wants_pass = "want_pass" in request.POST
    form = CrewHandbookForm(request.POST, wants_pass=wants_pass, user=request.user)

    if not form.is_valid():
        # A missing tick is not a reason to throw somebody back to the top of
        # a page they just read. Only a name or PIN we can't place re-locks
        # it; everything else re-renders in place with the error on the box.
        identified = not (form.has_error("employee") or form.has_error("pin"))
        return page(form, unlocked=wants_pass and identified)

    employee = form.cleaned_data["employee"]
    # Absent for a signed-in person, whose PIN was never asked for. There is
    # nothing to remember in that case and nothing to remember it for — the
    # session already does this job, and better.
    pin = form.cleaned_data.get("pin")

    def keep(response):
        return crew.remember(request, response, employee, pin) if pin else response

    if not wants_pass:
        # After the PIN has been checked, never before — see crew.remember.
        return keep(page(form, unlocked=True))

    if not employee.pass_pdf:
        # Named, not a dead button. Whoever is looking at this is legitimate
        # and already knows how to reach Michael; what they need is to be
        # told that waiting for the page to work is not the answer.
        return keep(page(form, unlocked=True, no_pass=True))

    # Streamed rather than handed over as a bucket URL. The bucket is private,
    # so an unsigned `.url` would simply 403 — but even where it wouldn't, a
    # link to somebody's pass outlives the page that produced it, and this one
    # dies with the response.
    response = FileResponse(
        employee.pass_pdf.open("rb"),
        as_attachment=True,
        filename=f"{employee.name} - faire pass.pdf",
    )
    return keep(response)


@page_meta(
    title="Report Hours",
    description=(
        "Where staff report the hours they worked: name, PIN, how long, which "
        "day. No login — the URL is the whole way in, so it's meant to be "
        "bookmarked or put on a card at the stall, not linked publicly."
    ),
    category="Payroll",
)
@require_http_methods(["GET", "POST"])
def hours_entry(request):
    """Report one day's hours.

    Reporting the same day twice is a correction, not a second shift — the
    database won't take two rows for a person and a date, so the second
    submission asks before it overwrites the first. Getting that wrong in the
    other direction is the expensive mistake: a double-tapped Submit that
    quietly books sixteen hours is exactly the kind of thing that survives
    all the way to payroll.
    """
    today = timezone.localdate()

    # "Not you?" — drop the remembered name and PIN, come back to an empty
    # form. A GET with a side effect, but the side effect is this browser's
    # own cookie: idempotent, nothing written, nothing to re-submit.
    if crew.asked_to_forget(request):
        return crew.forget(redirect("hours_entry"))

    # Post/redirect/get. The success state is carried in the session rather
    # than the URL so a refresh can't re-submit and a shared screen doesn't
    # leave somebody's name in the address bar.
    saved_pk = request.session.pop("hours_entry_saved", None)
    saved = None
    if saved_pk:
        saved = TimeEntry.objects.filter(pk=saved_pk).select_related("employee").first()

    if request.method == "POST":
        attempts = request.session.get("hours_pin_attempts", 0)
        if attempts >= HOURS_PIN_ATTEMPT_LIMIT:
            return render(request, "scarves/hours_entry.html", {
                "form": HoursForm(today=today),
                "locked": True,
                "today": today,
            })

        form = HoursForm(request.POST, today=today)
        if form.is_valid():
            request.session["hours_pin_attempts"] = 0
            employee = form.cleaned_data["employee"]
            work_date = form.cleaned_data["work_date"]
            hours = form.cleaned_data["hours"]

            existing = TimeEntry.objects.filter(
                employee=employee, work_date=work_date
            ).first()

            # An unconfirmed overwrite bounces back with the old figure shown.
            # The confirm token is the previous hours value, so a stale form
            # left open in another tab can't confirm away a number it never
            # displayed.
            if existing and request.POST.get("confirm_replace") != str(existing.hours):
                return render(request, "scarves/hours_entry.html", {
                    "form": form,
                    "existing": existing,
                    "today": today,
                })

            entry, _created = TimeEntry.objects.update_or_create(
                employee=employee,
                work_date=work_date,
                defaults={"hours": hours},
            )
            request.session["hours_entry_saved"] = entry.pk
            # After the PIN has been checked, never before — see crew.remember.
            return crew.remember(
                request, redirect("hours_entry"), employee, form.cleaned_data["pin"]
            )

        if form.has_error("pin"):
            request.session["hours_pin_attempts"] = attempts + 1
    else:
        form = HoursForm(today=today, initial=crew.initial(request, work_date=today))

    return render(request, "scarves/hours_entry.html", {
        "form": form,
        "saved": saved,
        "saved_week": _employee_week(saved) if saved else None,
        "today": today,
        "remembered": crew.remembered(request)[0],
        "forget_param": crew.FORGET,
    })


def _employee_week(entry):
    """One employee's pay week around a just-saved entry, for the receipt.

    Showing the week back is the cheapest error check there is: the person
    who worked the days is the only one who can look at Saturday through
    Friday and say "that's not right" while it's still easy to fix.
    """
    start = timesheets.week_start(entry.work_date)
    entries = list(
        TimeEntry.objects
        .filter(
            employee=entry.employee,
            work_date__gte=start,
            work_date__lte=timesheets.week_end(start),
        )
        .order_by("work_date")
    )
    return {
        "start": start,
        "end": timesheets.week_end(start),
        "entries": entries,
        "total": sum((e.hours for e in entries), Decimal("0")),
    }


@page_meta(
    title="Timesheet",
    description=(
        "Everyone's booth hours for one Saturday–Friday week, totalled per "
        "person and per day, with the rows worth a second look flagged."
    ),
    category="Payroll",
    note="Defaults to this week; ?week=YYYY-MM-DD picks another.",
)
@login_required
def timesheet(request):
    """The week, added up.

    Takes its week from the query string rather than a URL parameter, which
    is what keeps it a single page with no picker to maintain — every week
    that has ever existed is one link away from this one.
    """
    today = timezone.localdate()
    start = timesheets.parse_week(request.GET.get("week"), today)
    summary = timesheets.week_summary(start)

    return render(request, "scarves/timesheet.html", {
        "summary": summary,
        "previous_week": start - timedelta(days=7),
        "next_week": start + timedelta(days=7),
        "is_current_week": start == timesheets.week_start(today),
        "today": today,
        "hours_entry_url": request.build_absolute_uri(reverse("hours_entry")),
    })


# ---------------------------------------------------------------------------
# Production sheets: paper to the dye room, one scan back.
#
# Three views for staff (plan it, look at it, print it) and two for the crew
# (find your sheet, report it done). The split is the usual one — planning is
# a staff job at a desk, reporting is done by whoever was at the sink, and
# they have no accounts.
# ---------------------------------------------------------------------------


def _crew_run_url(request, run):
    """The absolute URL that goes in the QR code."""
    return request.build_absolute_uri(
        reverse("production_run", args=[run.token])
    )


@page_meta(
    title="Production Sheet",
    description="Print a dye-room worksheet: the next N baths to run, most "
                "urgent first, with a QR code the crew scan afterwards to "
                "say which ones they got through.",
    category="Production",
)
@login_required
@require_http_methods(["GET", "POST"])
def production_sheet_index(request):
    """Plan the sheet, see exactly what it asks for, then print it.

    Preview by GET, create by POST. A run only exists once somebody has
    decided to print one, so browsing the options leaves nothing behind —
    but the moment paper exists, so does the row that the crew's return URL
    points at.
    """
    if request.method == "POST":
        form = ProductionSheetForm(request.POST)
        if form.is_valid():
            baths = production.plan_baths(
                form.cleaned_data["baths"],
                category=form.cleaned_data.get("category"),
                include_overshoot=form.cleaned_data["include_overshoot"],
            )
            if not baths:
                messages.warning(request, "Nothing needs dyeing for those settings.")
                return redirect(f"{reverse('production_sheet_index')}?{request.POST.urlencode()}")

            with transaction.atomic():
                run = ProductionRun.objects.create(
                    category=form.cleaned_data.get("category"),
                    included_overshoot=form.cleaned_data["include_overshoot"],
                )
                ProductionRunRow.objects.bulk_create([
                    ProductionRunRow(
                        run=run,
                        finished_product=bath.product,
                        order=index,
                        quantity=bath.quantity,
                    )
                    for index, bath in enumerate(baths, start=1)
                ])
                # Printing a sixth retires the oldest rather than being
                # refused. Nothing is lost: the run is a work aid, and the
                # record of what actually happened is the inventory log.
                retired = production.retire_superseded_runs()

            if retired:
                messages.info(
                    request,
                    f"Closed {len(retired)} older sheet(s) that were never "
                    f"reported: {', '.join(str(r.pk) for r in retired)}.",
                )
            return redirect("production_run_detail", pk=run.pk)
    else:
        form = ProductionSheetForm(request.GET or None)

    baths = []
    if form.is_bound and form.is_valid():
        baths = production.plan_baths(
            form.cleaned_data["baths"],
            category=form.cleaned_data.get("category"),
            include_overshoot=form.cleaned_data["include_overshoot"],
        )

    return render(request, "scarves/production_sheet_index.html", {
        "form": form,
        "baths": baths,
        "plan": production.dye_plan_for_baths(baths),
        "bath_count": len(baths),
        # Only a form that actually asked a question gets an answer below.
        # Keyed on validity rather than "was anything submitted", or a typo in
        # the bath count reads back as "nothing needs dyeing" — which is a
        # different and much more alarming statement.
        "submitted": form.is_bound and form.is_valid(),
        "short_blanks": production.short_blanks(baths),
        # Sheets printed and not reported. The whole design leans on paper
        # coming back, so a sheet that never does has to be visible here
        # rather than being remembered by whoever printed it.
        "open_runs": (
            ProductionRun.objects.filter(submitted_at__isnull=True)
            .prefetch_related("rows")
        ),
    })


@page_meta(
    title="Production Sheet (one run)",
    description="One printed sheet: what it asked for, what came back, and "
                "the link the crew use to report it.",
    category="Production",
    show_in_index=False,
)
@login_required
def production_run_detail(request, pk):
    """One sheet from the office side.

    A sheet leaves the outstanding list the moment anything on it is reported
    — one tick is enough, because at that point somebody is working from it
    and the loop is closing. After that the QR code is how you get back to
    it, which is all the way back it needs to be found.
    """
    run = get_object_or_404(
        ProductionRun.objects.prefetch_related(
            "rows__finished_product__recipe",
            "rows__finished_product__raw_product",
            "rows__finished_product__recipe__recipe_dyes__dye__brand",
        ),
        pk=pk,
    )

    return render(request, "scarves/production_run_detail.html", {
        "run": run,
        "crew_url": _crew_run_url(request, run),
        "plan": production.dye_plan_for_run(run),
        "bath_count": run.rows.count(),
    })


@page_meta(
    title="Production Sheet PDF",
    description="Renders one run's worksheet for printing.",
    category="Production",
    note="Returns a PDF. Reached from the run's page.",
    show_in_index=False,
)
@login_required
def production_sheet_pdf(request, pk):
    run = get_object_or_404(ProductionRun, pk=pk)
    pdf = production.render_sheet(run, _crew_run_url(request, run))
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="production-run-{run.pk}.pdf"'
    )
    return response


@page_meta(
    title="Report a Dyeing Session",
    description="Pick the sheet you were working from and tick off the baths "
                "you finished. No login — the sheet's own code is the way in.",
    category="Production",
)
def production_run_index(request):
    """The fallback for a sheet whose QR won't scan.

    The QR is the fast path and this is the one that still works with a
    cracked camera, a flat battery or a photocopied sheet. Open sheets only,
    newest first, which is almost always the one in your hand.
    """
    return render(request, "scarves/production_run_index.html", {
        "runs": (
            ProductionRun.objects.filter(submitted_at__isnull=True)
            .prefetch_related("rows")[:20]
        ),
    })


def _photo_reading(request, run):
    """`(summary, prefilled)` from a `?done=` handed over by the upload page.

    Parsed defensively rather than trusted: ids that aren't this run's, or
    are already recorded, are dropped. Not for safety — a person can tick any
    box on this page by hand — but because a stale link should degrade to an
    ordinary empty form instead of a page half-ticked from some other sheet.
    """
    if "done" not in request.GET:
        return None, set()

    wanted = {
        int(value) for value in request.GET.getlist("done") if value.isdigit()
    }
    prefilled = {
        row.pk for row in run.rows.all()
        if row.pk in wanted and not row.is_applied
    }

    def number(name):
        value = request.GET.get(name, "")
        return int(value) if value.isdigit() else 0

    return {
        "read": number("read"),
        "filled": number("filled"),
        "unsure": number("unsure"),
        "strays": number("strays"),
        "total": run.rows.count(),
    }, prefilled


@page_meta(
    title="Photograph a Sheet",
    description="Send in a photo of a marked production sheet and it works "
                "out which run it is and which baths were filled in.",
    category="Production",
)
@require_http_methods(["GET", "POST"])
def production_upload(request):
    """One page for photographing any sheet, rather than one per run.

    Camera first: the photo is what says which run this is, so there is no
    navigating to a page before taking it. That is what makes the QR do real
    work — it isn't a second presentation of something the address bar
    already proved, it is the only thing that names the sheet.

    Nothing is applied here. The reading is handed to that run's own page,
    already ticked, and a person submits it.
    """
    held = request.session.get("production_photo") or {}

    if request.method == "POST" and "sheet" in request.FILES:
        scan = sheetscan.read_sheet(request.FILES["sheet"].read())
        held = {
            "error": scan.error,
            "read": len(scan.marks),
            "filled": len(scan.filled),
            "unsure": len(scan.unsure),
            # The marks travel, not the photo: the photo is an input to a
            # form, and it has done its job by here.
            "codes": sorted(scan.filled_codes),
            "token": scan.qr_token,
        }
        request.session["production_photo"] = held
        return _hand_off_photo(request, held) or redirect("production_upload")

    if request.method == "POST" and "sheet_code" in request.POST:
        # The QR wouldn't read — nearly always a soft photo rather than the
        # wrong sheet, since the code is on every page. Typing it off the
        # sheet is the same claim the QR makes.
        held["token"] = (request.POST.get("sheet_code") or "").strip()
        held["typed"] = True
        request.session["production_photo"] = held
        return _hand_off_photo(request, held) or redirect("production_upload")

    return render(request, "scarves/production_upload.html", {"photo": held})


def _hand_off_photo(request, held):
    """Send a read photo to its run's page, pre-ticked. None if it can't be."""
    token = (held.get("token") or "").strip()
    if not token or not held.get("read"):
        return None

    run = next(
        (
            candidate
            for candidate in ProductionRun.objects.all()
            if normalize_token(candidate.token) == normalize_token(token)
        ),
        None,
    )
    if run is None:
        held["unknown_run"] = token[:40]
        request.session["production_photo"] = held
        return None

    filled = set(held.get("codes") or [])
    codes = {production.row_code(row): row for row in run.rows.all()}
    ticked = [
        row.pk for code, row in codes.items()
        if code in filled and not row.is_applied
    ]

    # The reading rides in the query string rather than the session. It
    # belongs to *this run's* URL, which is what stops one sheet's photo
    # pre-ticking another sheet's page, and it costs nothing in safety: a
    # hand-edited `checked` can only tick boxes a person could tick anyway,
    # and the submit below is still the only thing that records.
    query = urlencode({
        # Named for the checkbox it fills, so the URL is exactly what the form
        # would have serialised: `?done=12&done=15` prefills the boxes called
        # `done`. Nothing in HTML does that by itself, but a page can, and it
        # leaves the link self-describing rather than carrying a private
        # parameter that only this view understands.
        "done": [str(pk) for pk in ticked],
        "read": held.get("read", 0),
        "filled": held.get("filled", 0),
        "unsure": held.get("unsure", 0),
        # Rows in the photo that aren't on this sheet. Expected to be empty
        # forever; if it isn't, the photo is of another run and the matched
        # marks would otherwise land here unremarked.
        "strays": len(filled - set(codes)),
    }, doseq=True)
    request.session.pop("production_photo", None)
    return redirect(f"{reverse('production_run', args=[run.token])}?{query}")


@page_meta(
    title="Report a Dyeing Session (one sheet)",
    description="The rows from one printed sheet, to tick off.",
    category="Production",
    show_in_index=False,
)
@require_http_methods(["GET", "POST"])
def production_run(request, token):
    """The crew's page: the same rows as the paper, in the same order.

    No login and no PIN. The token is on a sheet of paper that was in the dye
    room, which is the same bargain the other `secret/` pages make, and
    asking for a PIN on a page you reached by scanning something you are
    holding would be friction with nothing on the other side of it. The name
    is filled in from the phone if it knows one, purely as a record of who
    reported.
    """
    run = get_object_or_404(
        ProductionRun.objects.prefetch_related(
            "rows__finished_product__recipe",
            "rows__finished_product__raw_product",
        ),
        token=token,
    )

    if request.method == "POST":
        ticked = set(request.POST.getlist("done"))
        applied = 0
        with transaction.atomic():
            for row in run.rows.all():
                if str(row.pk) not in ticked or row.is_applied:
                    continue
                row.done_at = timezone.now()
                row.save(update_fields=["done_at"])
                production.apply_row(row)
                applied += 1

            if run.submitted_at is None:
                run.submitted_at = timezone.now()
            employee, _pin = crew.remembered(request)
            if employee is not None and run.submitted_by_id is None:
                run.submitted_by = employee
            run.save(update_fields=["submitted_at", "submitted_by"])

        request.session["production_run_applied"] = applied
        request.session.pop("production_photo", None)
        return redirect("production_run", token=run.token)

    applied = request.session.pop("production_run_applied", None)
    scan, prefilled = _photo_reading(request, run)
    employee, _pin = crew.remembered(request)
    return render(request, "scarves/production_run.html", {
        "run": run,
        "just_applied": applied,
        "remembered": employee,
        "scan": scan,
        # Pre-ticked from the photo, if there was one and the sheet has
        # identified itself. Kept in the session so the upload can
        # post/redirect/get like everything else here — a refresh must not
        # re-send a phone photo over a stall's signal.
        "prefilled": prefilled,
    })


# ---------------------------------------------------------------------------
# Barcode labels: pick a dataset, see exactly what it will use up, print.
#
# The picker and the preview are one page on purpose. Labels cost a sheet at a
# time and a run can't be un-printed, so every number that matters — how many
# stickers, how many sheets, which rows, what to write on the sheet afterwards
# — is on screen before the PDF is ever built.
# ---------------------------------------------------------------------------


def _label_stock_from(form):
    """The chosen stock, with any per-print offset override applied in memory.

    The offsets live on the model as a printer's saved registration, which
    assumes you print on a printer you own. Printing at a shop inverts that: a
    different machine every time, no chance to calibrate first, and no
    computer to hand when the first sheet comes out 2mm high. So the override
    rides in the query string — adjust it on a phone, re-download, print
    again, without going near the admin. Never saved: a correction for one
    store's machine on one day is not a property of the paper.
    """
    stock = form.cleaned_data["stock"]
    for field in ("x_offset_mm", "y_offset_mm"):
        override = form.cleaned_data.get(field)
        if override is not None:
            setattr(stock, field, override)
    return stock


def _label_run_from(form):
    """Build the run described by a valid LabelRunForm."""
    data = form.cleaned_data
    style = data.get("style") or labels.BARCODE
    if data["dataset"] == LabelRunForm.ITEMS:
        return labels.specific_items(data["items"], style=style)
    if data["dataset"] == LabelRunForm.SINCE:
        return labels.produced_since(data["since"], extra=data["extra"], style=style)
    return labels.inventory_run(
        extra=data["extra"],
        category=data.get("category"),
        raw_products=data.get("raw_products"),
        include_zero=data.get("include_zero", False),
        style=style,
    )


@page_meta(
    title="Barcode Labels",
    description="Print Code128 stickers for stock you're adding to inventory "
                "— everything produced since a date, or everything on hand. "
                "Shows the sheet layout and which rows it uses before you "
                "print.",
    category="Reference Sheets",
)
@login_required
def label_index(request):
    """Picker and preview in one. Submits to itself by GET, so a run is a URL."""
    submitted = bool(request.GET)
    form = LabelRunForm(request.GET or None)

    context = {"form": form, "submitted": submitted}

    if submitted and form.is_valid():
        stock = _label_stock_from(form)
        start_at = form.cleaned_data["start_at"] - 1  # UI is 1-indexed
        run = _label_run_from(form)
        sequence = run.sequence(stock.columns)
        blanks = {i for i, p in enumerate(sequence) if p is None}

        context.update({
            "run": run,
            "stock": stock,
            "padding": len(blanks),
            "plan": labels.plan_sheets(stock, len(sequence), start_at, blanks),
            "density_problems": labels.density_problems(run, stock),
            "pdf_url": f"{reverse('label_pdf')}?{request.GET.urlencode()}",
            "calibration_url": (
                f"{reverse('label_calibration_pdf')}?stock={stock.pk}"
            ),
        })

    return render(request, "scarves/label_index.html", context)


@page_meta(
    title="Barcode Labels PDF",
    description="Renders the label sheet described by the query string.",
    category="Reference Sheets",
    note="Returns a PDF. Reached from the Barcode Labels page.",
    show_in_index=False,
)
@login_required
def label_pdf(request):
    form = LabelRunForm(request.GET or None)
    if not form.is_valid():
        messages.error(request, "That label run isn't valid — check the form.")
        return redirect("label_index")

    stock = _label_stock_from(form)
    start_at = form.cleaned_data["start_at"] - 1
    run = _label_run_from(form)

    if not run.rows:
        messages.warning(request, "Nothing to print for those settings.")
        return redirect(f"{reverse('label_index')}?{request.GET.urlencode()}")

    # Refuse rather than hand over a sheet of stickers no scanner will read.
    # The failure is otherwise silent until someone is at the till with a
    # queue behind them.
    problems = labels.density_problems(run, stock)
    if problems:
        listed = ", ".join(f"{sku} ({mil:.1f} mil)" for sku, mil in problems[:5])
        messages.error(
            request,
            f"{len(problems)} SKU(s) are too long for {stock.name} and would "
            f"print bars under {labels.MIN_MODULE_MIL} mil: {listed}. Shorten "
            f"the SKU or use wider stock.",
        )
        return redirect(f"{reverse('label_index')}?{request.GET.urlencode()}")

    pdf = labels.render_run(run, stock, start_at)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="labels-{timezone.localdate():%Y%m%d}.pdf"'
    )
    return response


@page_meta(
    title="Label Calibration Sheet",
    description="Outlines at every label position and a one-inch ruler. Print "
                "on plain paper and hold it against a label sheet to check "
                "the geometry and the printer's registration.",
    category="Reference Sheets",
    note="Returns a PDF. Reached from the Barcode Labels page.",
    show_in_index=False,
)
@login_required
def label_calibration_pdf(request):
    from .models import LabelStock

    stock = get_object_or_404(LabelStock, pk=request.GET.get("stock"))
    response = HttpResponse(labels.render_calibration(stock), content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="label-calibration.pdf"'
    return response


# ---------------------------------------------------------------------------
# The booth: photos in, and unidentified sales reconciled.
#
# One page for the crew (`secret/booth/`, PIN — no accounts, same reasoning as
# the hours form) and two for the office: a gallery of what may be posted, and
# the queue of sales Square took that this app couldn't tie to a product.
#
# The two halves share a page because they share a moment: a phone comes out
# at a stall, once, and asking someone to pick the right page first is how you
# get no photos at all.
# ---------------------------------------------------------------------------

#: How far apart a photo and a sale can be and still be the same scarf. Fifteen
#: minutes is the width of a queue at a busy stall: long enough that the report
#: can wait until the customer has walked away, short enough that two sales of
#: the same style rarely both land inside it.
UNMATCHED_WINDOW = timedelta(minutes=15)

#: Same PIN as the hours form, so the same limit — but its own counter, since
#: locking one page has no business locking the other.
BOOTH_PIN_ATTEMPT_LIMIT = HOURS_PIN_ATTEMPT_LIMIT


def _booth_photo_file(upload):
    """The uploaded photo, downscaled, ready to hand to an ImageField.

    Straight through the app rather than the presigned-POST dance the product
    upload page uses: this is one photo taken on a phone with one bar of
    signal, and a page that needs JavaScript to work is a page that sometimes
    doesn't. The shrink is the same one, so a 5MB phone JPEG doesn't reach the
    bucket either way.
    """
    data = upload.read()
    content_type = (getattr(upload, "content_type", "") or "image/jpeg").lower()
    try:
        shrunk = _shrink_image(data)
    except Exception:
        shrunk = None          # unreadable by PIL: keep what was sent
    if shrunk:
        data, content_type = shrunk
    ext = _CONTENT_TYPE_EXT.get(content_type, ".jpg")
    return ContentFile(data, name=f"{uuid.uuid4().hex}{ext}")


@page_meta(
    title="Send a Photo",
    description="Send a photo in from the booth — something worth sharing, or "
                "a colorway nobody could identify that sold anyway. Name and PIN, "
                "no account needed.",
    category="Booth",
    note="Unlisted: hand out the URL, don't advertise it.",
)
def booth_photo(request):
    """The crew's page. No login — a PIN, exactly like the hours form.

    Post/redirect/get with the receipt in the session, so a refresh at the
    stall can't send the same photo twice and a shared phone doesn't leave
    somebody's name in the address bar.
    """
    now = timezone.localtime()

    # "Not you?" — drop the remembered name and PIN, come back to an empty
    # form. A GET with a side effect, but the side effect is this browser's
    # own cookie: idempotent, nothing written, nothing to re-submit.
    if crew.asked_to_forget(request):
        return crew.forget(redirect("booth_photo"))

    saved_pk = request.session.pop("booth_photo_saved", None)
    saved = BoothPhoto.objects.filter(pk=saved_pk).select_related("employee").first() if saved_pk else None

    if request.method == "POST":
        attempts = request.session.get("booth_pin_attempts", 0)
        if attempts >= BOOTH_PIN_ATTEMPT_LIMIT:
            return render(request, "scarves/booth_photo.html", {
                "form": BoothPhotoForm(now=now, user=request.user),
                "locked": True,
                "now": now,
            })

        form = BoothPhotoForm(request.POST, request.FILES, now=now, user=request.user)
        if form.is_valid():
            request.session["booth_pin_attempts"] = 0
            data = form.cleaned_data
            share = data["reason"] == BoothPhoto.REASON_SHARE

            # Only the half that applies is stored. Both halves are always
            # submitted, so a report that changed reason mid-thought would
            # otherwise leave a sharing permission attached to a sale report
            # nobody ever meant to publish.
            photo = BoothPhoto(
                employee=data["employee"],
                reason=data["reason"],
                share_website=share and data["share_website"],
                share_instagram=share and data["share_instagram"],
                people_in_photo=share and data["people_in_photo"],
                people_agreed=share and data["people_agreed"],
                caption=data["caption"] if share else "",
                tag=data["tag"] if share else "",
                sold_at=None if share else data["sold_at"],
                sku_prefix="" if share else data["sku_prefix"],
                note="" if share else data["note"],
            )
            # Built once: the uploaded file is a stream, and reading it a
            # second time yields nothing.
            stored = _booth_photo_file(data["photo"])
            photo.image.save(stored.name, stored, save=False)
            photo.save()
            request.session["booth_photo_saved"] = photo.pk
            # After the PIN has been checked, never before — see crew.remember.
            # Only the crew have a PIN to remember. A signed-in staff member
            # is identified by their login, which outlives any cookie here.
            response = redirect("booth_photo")
            if "pin" in data:
                response = crew.remember(
                    request, response, data["employee"], data["pin"]
                )
            return response

        if form.has_error("pin"):
            request.session["booth_pin_attempts"] = attempts + 1
    else:
        form = BoothPhotoForm(now=now, user=request.user, initial=crew.initial(
            request,
            reason=BoothPhoto.REASON_SHARE,
            sold_at=now.strftime("%Y-%m-%dT%H:%M"),
        ))

    return render(request, "scarves/booth_photo.html", {
        "form": form,
        "saved": saved,
        "now": now,
        # Only meaningful for the crew. Keyed on the PIN field rather than on
        # being signed in, because the note it drives says "name and PIN
        # filled in from this phone" — with no PIN on the page that sentence
        # describes something that didn't happen.
        "remembered": crew.remembered(request)[0] if "pin" in form.fields else None,
        "forget_param": crew.FORGET,
    })


@page_meta(
    title="Booth Photos",
    description="Photos the crew sent in to share, with what each one is "
                "cleared for — website, Instagram, or nothing yet.",
    category="Products",
)
@login_required
def booth_photos(request):
    """The gallery. Reads `shareable`, not the two destination ticks.

    A photo with a recognisable person in it and no answer from them is not
    postable however many boxes the sender ticked, and the badge on the card
    has to say that or the page is worse than no page.
    """
    photos = (
        BoothPhoto.objects.filter(reason=BoothPhoto.REASON_SHARE)
        .select_related("employee")
    )
    return render(request, "scarves/booth_photos.html", {"photos": photos})


def _open_sales_on(day):
    """Unresolved, undismissed sales that Square timestamped on `day` (local)."""
    start = timezone.make_aware(datetime.combine(day, time.min))
    return (
        UnmatchedSale.objects.filter(
            resolved_at__isnull=True,
            dismissed_at__isnull=True,
            sold_at__gte=start,
            sold_at__lt=start + timedelta(days=1),
        )
        .order_by("sold_at")
    )


def _unused_reports():
    """Unidentified-sale photos not yet spoken for by a resolved sale."""
    return (
        BoothPhoto.objects.filter(
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            matched_sales__isnull=True,
        )
        .select_related("employee")
        .order_by("sold_at")
    )


def _review_day(request):
    """The day being reviewed: the query string, else the oldest open sale,
    else today.

    Oldest rather than newest on purpose — the queue is a to-do list, and the
    row most likely to be forgotten is the one furthest back."""
    raw = request.GET.get("day")
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    oldest = (
        UnmatchedSale.objects.filter(
            resolved_at__isnull=True, dismissed_at__isnull=True
        )
        .order_by("sold_at")
        .first()
    )
    return timezone.localtime(oldest.sold_at).date() if oldest else timezone.localdate()


def _resolution_options(reports, cache=None):
    """Products a sale could plausibly be, given the photos near it.

    The reported prefix is the blank, not the colorway — six characters off a
    tag that says `INFI-AEGEAN`. That is exactly the narrowing worth having:
    nobody can read a colorway off a scarf they couldn't name, but the style
    turns a few hundred products into a few dozen. With no prefix reported the
    honest answer is the whole active catalogue rather than a guess.

    **The answer is keyed on the prefixes and nothing else**, so `cache` is a
    dict the caller keeps for one request. That matters more than it sounds:
    the common row has no photo beside it and therefore no prefix, so every
    such row asks the identical question — *the whole active catalogue* — and
    a day with ten of them was running ten copies of the biggest query on the
    page. Rows genuinely differ only when the photos differ.
    """
    prefixes = frozenset(r.sku_prefix for r in reports if r.sku_prefix)
    if cache is not None and prefixes in cache:
        return cache[prefixes]

    products = FinishedProduct.objects.filter(is_active=True)
    if prefixes:
        narrowed = Q()
        for prefix in prefixes:
            narrowed |= Q(sku__istartswith=prefix)
        products = products.filter(narrowed)
    answer = (
        list(products.select_related("raw_product", "recipe").order_by("name")),
        bool(prefixes),
    )
    if cache is not None:
        cache[prefixes] = answer
    return answer


def _like_key(sale):
    """What "another one like this" means for this line, or `None`.

    Two keys, and they are not two precisions of one idea — they apply to
    different populations, which is the thing to keep hold of:

    * **A Square variation id** means Square has a catalog object this app
      doesn't know. That is the unsynced-variation case, and it is the one
      *most* likely to be a real scarf. Precise, and precisely the group where
      dismissing in bulk is expensive: it writes the sales off and the count
      stays wrong with nothing saying so, which is the silence this queue
      exists to break.
    * **A name, with no variation id at all**, is a custom amount somebody
      hand-keyed. Looser key — and the population where a bulk dismissal is
      genuinely safe, because these really are the tips, bags and hats.

    So a name match is scoped to lines that have no variation id. Without
    that, dismissing every `Custom Amount` would sweep up a row that *does*
    carry a Square item — the dangerous group, taken by the safe group's
    button, with nothing on screen saying so.

    A line with neither gets no key and no button, because "all like this"
    would mean "all the nameless ones", which is a grab bag rather than a
    group.
    """
    if sale.square_variation_id:
        return ("item", sale.square_variation_id)
    if sale.name:
        return ("name", sale.name)
    return None


def _like_this_on_day(sale, day):
    """The open lines on `day` that this one stands for, including itself.

    Day-scoped deliberately. Everything the button claims is on the screen
    it was clicked from, so the count beside it can be checked by looking
    rather than trusted.
    """
    key = _like_key(sale)
    if key is None:
        return [sale]

    kind, value = key
    group = _open_sales_on(day)
    if kind == "item":
        group = group.filter(square_variation_id=value)
    else:
        group = group.filter(name=value, square_variation_id="")
    return list(group)


def _dismiss(sales, reason):
    """Mark every one of them dismissed, in one write.

    Nothing is destroyed — dismissal is a timestamp and a sentence, and the
    admin clears both. That is what makes a button covering forty-seven rows
    an acceptable thing to offer at all.
    """
    now = timezone.now()
    UnmatchedSale.objects.filter(pk__in=[s.pk for s in sales]).update(
        dismissed_at=now, dismissed_reason=reason
    )
    for sale in sales:
        sale.dismissed_at = now
        sale.dismissed_reason = reason
    return sales


def _open_unmatched_total():
    """Everything still in the queue, any day. One count, shared by the page
    and by the fragment a dismissal swaps in — a header that kept saying 12
    over a list of 11 is the page contradicting itself."""
    return UnmatchedSale.objects.filter(
        resolved_at__isnull=True, dismissed_at__isnull=True
    ).count()


def _orphan_reports(day, sales=None, reports=None):
    """Photos with no open sale beside them, on `day`.

    Kept on the page rather than filtered out: a report with nothing to match
    is the interesting case — either the sale never reached Square, or it was
    rung up as a product after all and the scarf on the photo is still
    counted as in stock.

    One definition, used by the page and again after a dismissal, because
    dismissing a sale can *make* an orphan: the photo that was sitting beside
    it now has nothing to be beside.
    """
    if sales is None:
        sales = list(_open_sales_on(day))
    if reports is None:
        reports = list(_unused_reports())
    paired = {
        report.pk
        for sale in sales
        for report in reports
        if abs(report.when - sale.sold_at) <= UNMATCHED_WINDOW
    }
    return [
        report for report in reports
        if report.pk not in paired
        and timezone.localtime(report.when).date() == day
    ]


@page_meta(
    title="Unidentified Sales",
    description="Square sold something this app couldn't tie to a product. "
                "Match each one against the photos the booth sent in — paired "
                "by time — so the stock leaves inventory like any other sale.",
    category="Inventory",
    note="Add ?day=YYYY-MM-DD to review another day.",
)
@login_required
def unmatched_sales(request):
    day = _review_day(request)
    sales = list(_open_sales_on(day))
    reports = list(_unused_reports())

    # One cache for the request. Most rows have no photo and so ask the same
    # question, and that question is the whole catalogue.
    options_cache = {}
    # How many lines each row stands for, counted off the list already in
    # memory rather than a query per row — which is the mistake this page had
    # and the reason `options_cache` exists two lines up.
    like_counts = Counter(
        key for key in (_like_key(sale) for sale in sales) if key is not None
    )
    rows = []
    for sale in sales:
        near = [
            report for report in reports
            if abs(report.when - sale.sold_at) <= UNMATCHED_WINDOW
        ]
        options, narrowed = _resolution_options(near, options_cache)
        key = _like_key(sale)
        rows.append({
            "sale": sale,
            "reports": near,
            "options": options,
            "narrowed": narrowed,
            # Offered only when it stands for more than itself: a button
            # reading "dismiss all 1 like this" is the button beside it,
            # wearing a longer label.
            "like_count": like_counts.get(key, 0) if key else 0,
            "like_kind": key[0] if key else "",
        })

    return render(request, "scarves/unmatched_sales.html", {
        "day": day,
        "rows": rows,
        "orphans": _orphan_reports(day, sales, reports),
        "open_total": _open_unmatched_total(),
        "window_minutes": int(UNMATCHED_WINDOW.total_seconds() // 60),
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
    })


@require_POST
@login_required
def resolve_unmatched_sale(request, pk):
    """Match one sale to a product, or say it was never a scarf.

    Resolving moves stock, which looks like a contradiction of the rule that
    back-dated entries never do — it isn't. That rule exists because a
    backfilled kanban card records a bath that was already counted, so
    applying it again would inflate the count. This sale was never applied at
    all: the webhook dropped it, the scarf left the tent, and `number_on_hand`
    has been one too high ever since. The whole point is to apply it late.
    """
    sale = get_object_or_404(UnmatchedSale, pk=pk)
    day = (request.POST.get("day") or "").strip()
    redirect_to = reverse("unmatched_sales") + (f"?day={day}" if day else "")

    if not sale.is_open:
        # A double-tap at pace, or a stale tab. Says where things stand
        # rather than erroring — the page is already in the state they were
        # asking for.
        if _is_htmx(request) and sale.dismissed_at:
            return _dismissed_row(request, [sale], _posted_day(request, sale))
        messages.info(request, "That sale was already dealt with.")
        return redirect(redirect_to)

    if request.POST.get("dismiss") or request.POST.get("dismiss_all"):
        reason = (request.POST.get("dismissed_reason") or "").strip()[:200]
        day_of = _posted_day(request, sale)
        if request.POST.get("dismiss_all"):
            # The clicked row leads, because it is the one the swap targets;
            # the rest ride out-of-band.
            group = [sale] + [
                other for other in _like_this_on_day(sale, day_of)
                if other.pk != sale.pk
            ]
        else:
            group = [sale]
        _dismiss(group, reason)

        if _is_htmx(request):
            # No `messages` on this path: it would sit in the session and
            # surface on some later full page load, describing a row the
            # reader dealt with twenty dismissals ago. The strips that
            # replace the rows are the receipt.
            return _dismissed_row(request, group, day_of)
        if len(group) == 1:
            messages.success(
                request, f"Dismissed “{sale.name or 'that line'}” — not a scarf."
            )
        else:
            messages.success(
                request,
                f"Dismissed {len(group)} lines like “{sale.name or 'that line'}” "
                f"on {day_of:%d %b %Y} — not scarves.",
            )
        return redirect(redirect_to)

    product = get_object_or_404(FinishedProduct, pk=request.POST.get("product_id"))
    report = BoothPhoto.objects.filter(pk=request.POST.get("report_id")).first()

    with transaction.atomic():
        product.number_on_hand = max(product.number_on_hand - sale.quantity, 0)
        product.save(update_fields=["number_on_hand"])

        log = InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_UNMATCHED_SALE,
            quantity=-sale.quantity,
            sale_reference=sale.order_id,
            notes=(
                "Matched by hand from an unidentified sale"
                + (f", reported by {report.employee.name}" if report else "")
                + f" (Square line “{sale.name or 'unnamed'}”)."
            ),
        )
        # created_at is auto_now_add, so the sale's own time can only be set
        # afterwards. It matters: this row is otherwise dated the day someone
        # got round to the queue, which is not the day the scarf sold.
        InventoryLog.objects.filter(pk=log.pk).update(created_at=sale.sold_at)

        sale.resolved_product = product
        sale.resolved_photo = report
        sale.resolved_at = timezone.now()
        sale.save(update_fields=["resolved_product", "resolved_photo", "resolved_at"])

        # The photo is a photo of the scarf, taken by someone who couldn't
        # name it. Filing it against the product is opt-in rather than
        # automatic — a stall snap in bad light is not always what you want
        # the catalogue to show — but when it is, next time it's identifiable.
        if report and request.POST.get("file_photo"):
            next_order = (product.images.aggregate(Max("order"))["order__max"] or 0) + 1
            FinishedProductImage.objects.create(
                finished_product=product,
                image=report.image.name,
                order=next_order,
                alt_text=f"{product.name} (from the booth, {report.when:%d %b %Y})",
            )

    messages.success(
        request,
        f"Matched to {product.name} — {sale.quantity} off the shelf, logged as "
        f"a sale on {timezone.localtime(sale.sold_at):%d %b %Y, %H:%M}.",
    )
    return redirect(redirect_to)


def _is_htmx(request):
    """Whether this came from a swap rather than a navigation."""
    return request.headers.get("HX-Request") == "true"


def _posted_day(request, sale):
    """The day whose page this came off.

    Falls back to the sale's own day rather than today: an absent or
    unreadable value would otherwise gather the wrong day's group and rebuild
    the wrong day's orphan list, and the only symptom is rows appearing or
    vanishing on a page nobody is looking at.
    """
    raw = (request.POST.get("day") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return timezone.localtime(sale.sold_at).date()


def _dismissed_row(request, sales, day):
    """The one-line strips dismissed rows collapse to, plus what they changed.

    The queue gets worked a hundred lines at a time, and what made that slow
    was structural rather than incidental: every dismissal was a full
    navigation that rebuilt every *other* row on the day, each carrying a
    `<select>` of the whole active catalogue. Swapping rows for lines replaces
    all of that with a few hundred bytes.

    `sales[0]` is what the click targeted and the rest ride out-of-band, so a
    "dismiss all like this" empties every one of them off the page in the one
    response — a row left behind would read as one the button missed.
    """
    return render(request, "scarves/partials/unmatched_dismissed.html", {
        "sales": sales,
        "day": day,
        "open_total": _open_unmatched_total(),
        "orphans": _orphan_reports(day),
    })


# ---------------------------------------------------------------------------
# The Sunday close: the app's zeros, checked against the tags in hand.
#
# One page, three steps, in the order the physical work happens — tick the
# tags you're holding, count the bags for the ones you aren't, then say what
# you're holding that nobody predicted. See closing.py for what each answer
# means and why the middle one is the only number anybody types.
#
# A run is a calendar day. There is no "finish" button because the button is
# what doesn't get pressed: the van gets loaded, the phone goes in a pocket,
# and a run left open forever reads the same as one that found nothing. Open
# the page again the same evening and you are back in the same run; open it
# tomorrow and yesterday is a record.
#
# It never reaches Square. The close runs at a field on one bar of signal
# while a van is being packed, and a step that needs the network is a step
# that sometimes doesn't happen — the same reasoning that keeps the booth
# form's toggle in CSS. Reconciling against Square's own counts is a desk job
# for afterwards, and doing it first would be worse than not doing it at all:
# a PHYSICAL_COUNT push overwrites, so it makes the two agree by construction
# and every close comes back clean.
# ---------------------------------------------------------------------------


@page_meta(
    title="Sunday Close",
    description="End-of-weekend check: the app's out-of-stock list against "
                "the tags in hand. Confirm what you're holding, count the "
                "bags for what you aren't, and add tags nobody predicted.",
    category="Inventory",
    note="No login — pick your name and type your PIN.",
)
@require_http_methods(["GET", "POST"])
def close_index(request):
    """Open today's close, or get back into it.

    Resuming has to be exactly as easy as starting, because this is done in a
    car park in the dark and gets interrupted. Today's run is offered back by
    name rather than being something you needed to keep a URL for.
    """
    if crew.asked_to_forget(request):
        return crew.forget(redirect("close_index"))

    today = timezone.localdate()
    existing = CloseRun.objects.filter(day=today).first()

    if request.method == "POST":
        form = CloseStartForm(request.POST, user=request.user)
        if form.is_valid():
            employee = form.cleaned_data["employee"]
            run, _created = closing.run_for_today(employee=employee)
            # Starting is starting to count, said explicitly rather than
            # left to the default — the mode is what every other link and
            # redirect onto this run carries.
            response = redirect(_close_run_url(run, _COUNT))
            # Only after the PIN has been checked — see crew.remember.
            pin = form.cleaned_data.get("pin")
            if pin:
                crew.remember(request, response, employee, pin)
            return response
    else:
        form = CloseStartForm(user=request.user, initial=crew.initial(request))

    return render(request, "scarves/close_index.html", {
        "form": form,
        "today": today,
        "existing": existing,
        "existing_tally": closing.tally(existing) if existing else None,
        # What the list would come out at right now. Said up front because
        # "twelve products to check" and "a hundred and twelve" are different
        # jobs, and knowing which one it is before starting decides whether
        # it happens tonight or in the morning. It is a wider list than the
        # zeros it replaced — everything whose bag the app thinks is empty,
        # not just what it thinks is gone — which is the point: a drifting
        # count is caught while the display is still full, not once the shelf
        # is bare.
        "expected_now": closing.expected_products().count(),
        "recent": CloseRun.objects.exclude(day=today).select_related("employee")[:5],
        "remembered": crew.remembered(request)[0],
        "forget_param": crew.FORGET,
    })


@page_meta(
    title="Fancy Conversions",
    description="Record scarves that had line work added: one colorway goes "
                "down, its fancy counterpart goes up.",
    category="Production",
)
@login_required
@require_http_methods(["GET", "POST"])
def fancy_convert(request):
    """Say that some plain scarves became fancy ones.

    Optional, and safe to be optional — the plain side turns up as an
    overcount on its peg and the fancy side as an undercount on its, so an
    unrecorded conversion still heals. What this buys over the healing is
    *what happened*: the two halves get tied together at the moment somebody
    knows they belong together, which is the only way "how many did we fancy
    this season" is ever answerable.
    """
    blanks = list(fancy.fancy_blanks())
    if not blanks:
        messages.info(
            request,
            "No fancy blanks set up yet — a blank with 'made in a dye bath' "
            "unticked is what a scarf can be converted into.",
        )

    if request.method == "POST" and blanks:
        source = FinishedProduct.objects.filter(
            pk=request.POST.get("source"), is_active=True
        ).select_related("raw_product", "recipe").first()
        blank = RawProduct.objects.filter(
            pk=request.POST.get("blank"), made_in_a_dye_bath=False
        ).first()
        try:
            quantity = int(request.POST.get("quantity") or 0)
        except ValueError:
            quantity = 0

        if source is None or blank is None or quantity < 1:
            messages.error(request, "Pick a colorway, a fancy blank and how many.")
            return redirect("fancy_convert")

        target, shortfall = fancy.convert(source, blank, quantity)
        if target is None:
            messages.error(
                request,
                f"There's no {blank.name} in {source.recipe.name} to convert "
                f"into. Create it first — the colorway has to exist on both "
                f"blanks.",
            )
            return redirect("fancy_convert")

        messages.success(
            request,
            f"{quantity} × {source.recipe.name}: {source.raw_product.name} → "
            f"{target.raw_product.name}.",
        )
        if shortfall:
            # Reported, never refused. Five really did get line work put on
            # them; the plain count was wrong before anybody touched it, and
            # this is the only evidence of that.
            messages.warning(
                request,
                f"The app only had {quantity - shortfall} of the plain "
                f"{source.raw_product.name} — it was under by {shortfall}, "
                f"and is now at zero. Worth a count.",
            )
        return redirect("fancy_convert")

    return render(request, "scarves/fancy_convert.html", {
        "blanks": blanks,
        "sources": fancy.convertible(),
        "recent": InventoryLog.objects.filter(
            source=InventoryLog.SOURCE_FANCY_CONVERSION, quantity__gt=0
        ).select_related("finished_product__recipe", "finished_product__raw_product")[:15],
    })


@page_meta(
    title="Display Map",
    description="Pick a board to say what hangs where. Staff only — editing "
                "the map is a desk job, not something done at the stall.",
    category="Inventory",
)
@login_required
def display_map_index(request):
    """The boards, for editing rather than walking.

    Separate from `restock_index` because they are different jobs for
    different people. Walking a board happens at the stall, on a phone, with
    no account; deciding what hangs where happens sitting down, rarely, and
    is a staff decision — so it gets a login and lives under `private/`.
    """
    return render(request, "scarves/display_map_index.html", {
        "fixtures": DisplayFixture.objects.filter(is_active=True).select_related(
            "raw_product"
        ),
    })


@page_meta(
    title="Display Map (one board)",
    description="A dropdown on every peg: say what hangs there.",
    category="Inventory",
    show_in_index=False,
)
@login_required
@require_http_methods(["GET", "POST"])
def display_map(request, fixture_id):
    """Say what hangs on each peg. **Saving here is not a check.**

    That is the whole reason this is a second page rather than a mode on the
    restock board. Assigning a colorway to a peg is a statement about the
    *map*; ticking a peg is a statement about the *stock*, made by somebody
    standing in front of it. A single Save that quietly did both would record
    forty confirmations nobody made — and those are what the whole restock
    page is built to be trustworthy about.

    So this writes assignments, opens no `RestockPass`, moves no stock, and
    says so on the button.
    """
    fixture = get_object_or_404(DisplayFixture, pk=fixture_id, is_active=True)
    # Read before the form exists. A ModelForm bound to this instance writes
    # the submitted values onto it during `is_valid()`, so by the time the
    # POST branch runs, `fixture.raw_product` is already the *new* blank and
    # comparing against it would say nothing ever changed.
    was_blank_id = fixture.raw_product_id
    positions = list(
        fixture.positions.select_related("finished_product__recipe").order_by(
            "row", "column"
        )
    )

    # **Two controls, chosen by whether the board carries one blank.**
    #
    # A scoped board gets a plain `<select>`: forty colorways is a readable
    # menu and nothing needs typing. A mixed board can't — the scarf rack is
    # a row per scarf type, so its dropdown would have to carry the whole
    # catalogue, which is unreadable *and* renders the same few hundred
    # options once per peg (that page was 936KB).
    #
    # So a mixed board gets a `<datalist>`: native type-ahead, **no
    # JavaScript**, and the list is rendered once for the page instead of
    # once per peg. Without the browser's support it degrades to a text box
    # holding a SKU, which still posts and still resolves.
    choices = FinishedProduct.objects.filter(is_active=True, recipe__isnull=False)
    if fixture.raw_product_id:
        choices = choices.filter(
            Q(raw_product_id=fixture.raw_product_id)
            # A board scoped to one blank can still be carrying a stray, and
            # a menu that omitted it would drop that assignment the first
            # time anybody saved.
            | Q(pk__in=[p.finished_product_id for p in positions if p.finished_product_id])
        )
    choices = list(
        choices.select_related("recipe", "raw_product").order_by(
            "raw_product__name", "recipe__name"
        )
    )
    by_token = {_peg_token(product): product for product in choices}

    # Built from the board as it was *rendered*, so a save that also changes
    # the blank still validates the pegs against the menu the person was
    # actually looking at. The new scope applies from the next load.
    form = DisplayFixtureForm(
        request.POST or None, instance=fixture, prefix="board"
    )

    if request.method == "POST":
        if not form.is_valid():
            return render(request, "scarves/display_map.html", {
                "fixture": fixture, "form": form,
                "rows": _map_rows(fixture, positions),
                "choices": choices,
                "tokens": {p.pk: _peg_token(p) for p in choices},
                "boards": DisplayFixture.objects.filter(is_active=True),
                "unmapped": restock.unmapped_for(fixture),
            })

        # **A save that changes the blank never also assigns pegs.**
        #
        # The menus on screen were built for the *old* blank, and colorway
        # names repeat across blanks — every blank has an Aegean Sea. So
        # somebody who switches the blank and then picks "Aegean Sea" from
        # the stale list gets a different product with an identical label,
        # and nothing on the page looks wrong. That is the worst shape a bug
        # can have here.
        #
        # Telling them to save first would leave the hazard in place for
        # whoever doesn't read it. Refusing instead makes it structurally
        # impossible: the blank is applied, the menus come back rebuilt, and
        # the pegs are untouched and said to be. Works with the script below
        # or without it.
        picked = form.cleaned_data.get("raw_product")
        switching = (picked.pk if picked else None) != was_blank_id
        form.save()

        if switching:
            messages.info(
                request,
                "Blank changed — the colorway menus have been rebuilt for it. "
                "Pegs were left exactly as they were, because the menus you "
                "were looking at belonged to the old blank.",
            )
            return redirect("display_map", fixture_id=fixture.pk)

        changed = 0
        unplaceable = []
        for position in positions:
            if not position.is_home:
                continue
            raw = (request.POST.get(f"peg_{position.pk}") or "").strip()
            wanted = None
            if raw:
                # Only what the page offered is accepted, so the scoping
                # above is a rule and not merely a convenience — a hand-built
                # POST can name anything.
                product = by_token.get(raw)
                if product is None:
                    # Named rather than dropped. A typed box invites a typo,
                    # and a peg that silently stayed as it was reads exactly
                    # like one that saved.
                    unplaceable.append((position, raw))
                    continue
                wanted = product.pk
            if position.finished_product_id != wanted:
                position.finished_product_id = wanted
                position.save(update_fields=["finished_product"])  # signal → slots
                changed += 1

        messages.success(
            request,
            f"Board saved — {changed} peg{'' if changed == 1 else 's'} changed. "
            f"Nothing was counted and no stock moved."
            if changed
            else "Board saved — no pegs changed.",
        )
        if unplaceable:
            messages.error(
                request,
                "Couldn't place "
                + ", ".join(f"“{raw}” (r{p.row}c{p.column})" for p, raw in unplaceable)
                + " — those pegs were left as they were.",
            )
        return redirect("display_map", fixture_id=fixture.pk)

    return render(request, "scarves/display_map.html", {
        "fixture": fixture,
        "form": form,
        "rows": _map_rows(fixture, positions),
        "choices": choices,
        "tokens": {p.pk: _peg_token(p) for p in choices},
        "boards": DisplayFixture.objects.filter(is_active=True),
        "unmapped": restock.unmapped_for(fixture),
    })


def _peg_token(product):
    """What a peg's box holds for one product.

    The SKU, because it is unique, already means `BLANK-DYEBATH` to anybody
    reading it, and is the same string on the sticker and in Square. A pk
    would be a number nobody could check against anything.
    """
    return product.sku or f"#{product.pk}"


def _map_rows(fixture, positions):
    """The grid, with the holes filled in — see `DisplayFixture.grid`."""
    by_cell = {(p.row, p.column): p for p in positions}
    return [
        [by_cell.get((r, c)) for c in range(1, fixture.columns + 1)]
        for r in range(1, fixture.rows + 1)
    ]


@page_meta(
    title="Restock the Display",
    description="Pick a fixture and restock it: fill every peg, confirm each "
                "one, and say where the app was wrong. Open, close, and the "
                "end of every shift.",
    category="Inventory",
)
def restock_index(request):
    """The fixtures, and the last time each was walked.

    The picker for `restock_board`, and the answer to the only question worth
    asking from a distance: when was this board last filled, and by whom. A
    promise nobody has made in six hours is the finding.

    **`?bare=1` adds how long the longest-bare peg has been bare**, and
    nothing else on the page changes. Off by default and linked from nowhere,
    for the reasons in `restock`: it is a length of time attached to whoever
    was walking, and it mostly measures the gap since the last walk rather
    than yarn sitting unsold.
    """
    if crew.asked_to_forget(request):
        return crew.forget(redirect("restock_index"))

    fixtures = []
    for fixture in DisplayFixture.objects.filter(is_active=True).select_related(
        "raw_product"
    ):
        last = fixture.restock_passes.select_related("employee").first()
        fixtures.append({
            "fixture": fixture,
            "last": last,
            "last_summary": restock.summary(last) if last else None,
            # Stated, never judged. "Last full check: yesterday 6:40pm" is
            # what somebody arriving in the morning needs; whether that makes
            # them late depends on things this page cannot see.
            "last_full": restock.last_full_check(fixture),
            # What is waiting, which is what decides which rack to do next.
            # Deliberately *not* the count of colorways with no home: that
            # answers "what should we build one day", and it lives on the
            # board page where the empty pegs are in view.
            "status": restock.board_status(fixture),
        })

    # The order to work the stall in, and it stops where usefulness stops.
    # Most bare pegs first, because that is yarn not selling; then most to top
    # up; then whichever board has gone longest without a full check, a board
    # never fully checked counting as longest. Past that there is nothing to
    # choose between them, so it falls back to the name rather than inventing
    # a fourth criterion.
    #
    # Ordering rather than badging: the top of a list is a recommendation
    # somebody can ignore without being told off.
    fixtures.sort(key=_restock_priority)

    return render(request, "scarves/restock_index.html", {
        "fixtures": fixtures,
        # Typed by hand or not present. See `restock_board` for why it is not
        # a link and does not follow you around.
        "bare_age": request.GET.get("bare") == "1",
        # One trip to the backstock for the whole stall.
        "pull": restock.pull_list(),
        # No "colorways with no home" here. Which colorways belong on a board
        # is the mapper's decision, so that list lives on the editor and is
        # shown to nobody else.
        # The one way the map fails quietly: a colorway with no home
        # contributes no display capacity, so the Sunday close stops asking
        # about it and nothing says why.
        "remembered": crew.remembered(request)[0],
        "forget_param": crew.FORGET,
    })


def _restock_priority(entry):
    """Most bare, then most to top up, then longest since a full check.

    A board with no full check on record sorts as the longest, because that
    is what it is — and a sentinel date rather than `None` so the comparison
    never has two nulls to order.
    """
    status = entry["status"]
    full = entry["last_full"]
    return (
        -status["bare"],
        -status["topup"],
        full.created_at if full else _NEVER,
        entry["fixture"].name,
    )


@page_meta(
    title="Restock a Fixture",
    description="One board, drawn as it hangs: tap each peg you filled.",
    category="Inventory",
    show_in_index=False,
)
@require_http_methods(["GET", "POST"])
def restock_board(request, fixture_id):
    """Walk one board. One form, saved as often as you like.

    Deliberately not an htmx tap-per-peg. Every interaction here would be a
    network round-trip on a phone at a stall on one bar, and the house rule
    that keeps the booth form's toggle in CSS applies with more force to
    forty-two of them: a tap that silently fails to reach the server is a peg
    somebody believes they reported. One form that submits when they say so —
    and submits partially, as many times as they like — is both fewer moving
    parts and more robust.

    An unanswered peg is "not walked yet", never "empty". Same distinction the
    close draws, and for the same reason: the walk gets interrupted.

    **The board opens as names, and `?photos=1` swaps it to pictures.** The
    reasoning is in `restock.board`; what matters here is that the mode rides
    in the URL rather than in a cookie or the session, so a walk in photo mode
    is a link somebody can send and a shared phone can't inherit somebody
    else's board. The POST redirect carries it, because a save that quietly
    dropped you back to text mode is the same tap-and-lose-your-place the
    whole page is built to avoid.

    **`?bare=1` is the opposite of a mode, and is handled the opposite way.**
    It adds how long each bare peg has been bare, it is advertised nowhere,
    and it is deliberately left out of `mode` so no link off this page and no
    POST redirect carries it. A mode should follow you around a circuit; this
    should evaporate the moment you stop asking for it, because a link sent
    mid-walk or a bookmark taken during a demo is exactly how a stopwatch
    ends up in front of the crew. Typing it is the whole of the interface.
    """
    fixture = get_object_or_404(DisplayFixture, pk=fixture_id, is_active=True)
    photos = request.GET.get("photos") == "1"
    mode = "?photos=1" if photos else ""
    bare_age = request.GET.get("bare") == "1"

    if request.method == "POST":
        form = RestockPassForm(request.POST, user=request.user)
        if form.is_valid():
            positions = {
                p.pk: p
                for p in fixture.positions.select_related(
                    "fixture", "finished_product__raw_product"
                )
            }
            answers = _restock_answers(request.POST, positions)
            if answers:
                walk = restock.open_pass(fixture, employee=form.cleaned_data["employee"])
                moved = 0
                for pk, counted in answers.items():
                    check = restock.record(walk, positions[pk], counted=counted)
                    if check is not None and check.applied_log_id is not None:
                        moved += 1
                # A full check is named; a partial one is never counted
                # against. Covering the whole board is worth recognising —
                # afterwards every peg has a fresh baseline, so everything the
                # board predicts is trustworthy — but nine pegs at four
                # o'clock is a completed piece of work, not a failed full
                # check. "17 still to do" is the sentence that would turn this
                # page into a task master.
                full = restock.close_pass(walk)
                done = (
                    "Full check — the whole board."
                    if full
                    else f"{len(answers)} peg"
                    f"{'' if len(answers) == 1 else 's'} confirmed."
                )
                messages.success(
                    request,
                    done
                    + (
                        f" {moved} put right where the app was wrong."
                        if moved
                        else ""
                    ),
                )
                response = redirect(
                    reverse("restock_board", args=[fixture.pk]) + mode
                )
                pin = form.cleaned_data.get("pin")
                if pin:
                    crew.remember(request, response, form.cleaned_data["employee"], pin)
                return response
            messages.info(request, "Nothing ticked, so nothing recorded.")
            return redirect(reverse("restock_board", args=[fixture.pk]) + mode)
    else:
        form = RestockPassForm(user=request.user, initial=crew.initial(request))

    return render(request, "scarves/restock_board.html", {
        "fixture": fixture,
        "rows": restock.board(fixture, photos=photos),
        "form": form,
        "recent": fixture.restock_passes.select_related("employee")[:5],
        "last_full": restock.last_full_check(fixture),
        "homes": len(restock.assigned_homes(fixture)),
        # Every board, so the walk can move from one to the next without
        # going back out to the picker. The stall is walked in one circuit,
        # not board-by-board with a trip to a menu in between.
        "boards": DisplayFixture.objects.filter(is_active=True),
        # The mode, and the querystring that carries it. Every link off this
        # page (the next board, "not you?") appends `mode` so a circuit walked
        # in one mode stays in it.
        "photos": photos,
        "mode": mode,
        # Not folded into `mode` on purpose — it is asked for per page view,
        # never carried. The tiles say a peg is empty either way.
        "bare_age": bare_age,
        "remembered": crew.remembered(request)[0],
        "forget_param": crew.FORGET,
    })


def _restock_answers(post, positions):
    """`{position_pk: counted-or-None}` for every peg somebody answered.

    A number wins over a tick, because typing one is the more deliberate act:
    somebody who ticked the tile and then found the peg wouldn't fill meant
    the number. A peg with neither is absent, which is what leaves a walk
    half-finished instead of recording zeros for the part nobody reached.
    """
    answers = {}
    for pk, position in positions.items():
        if not position.is_home or position.finished_product_id is None:
            continue
        picked = (post.get(f"count_{pk}") or "").strip()
        typed = (post.get(f"more_{pk}") or "").strip()

        # **Both controls mean the same thing: how many there are altogether.**
        # The buttons are the fast path for the bounded case (everything fits
        # on the pegs, so the bag ends up empty); the box is for when it
        # doesn't. The app does the splitting — pegs first, remainder to the
        # bag — because that is what a person does with them.
        #
        # An earlier version had the box mean "how many in the bag" and added
        # the display's capacity on. That assumed the peg started full, and a
        # peg at 1 of 2 breaks it: what you found goes *on the peg*, the bag
        # stays empty, and the total comes out one too high.
        counted = None
        source = picked if picked and picked != "more" else typed
        if source:
            try:
                counted = max(int(source), 0)
            except ValueError:
                counted = None

        if counted is not None:
            answers[pk] = counted
        elif post.get(f"done_{pk}"):
            answers[pk] = None
    return answers


@page_meta(
    title="Sunday Close (one day)",
    description="The tag-by-tag checklist for one day's close.",
    category="Inventory",
    show_in_index=False,
)
@require_http_methods(["GET", "POST"])
def close_run(request, token):
    """One list, one question per row: how many of these are actually here.

    The page used to be two POSTs — tick the tags, then count the bags for
    the ones with no tag — because a tag in hand *was* the answer. It isn't
    any more: holding it says the bag is empty, not that the shelf is, and
    the units still hanging on the display have to be counted or they get
    written off. So there is one step, and every answered row carries a
    number. On a phone that costs about what a tick cost, because the buttons
    only run as high as the display holds.

    A blank row is "not got to yet" rather than zero. That distinction is the
    whole reason a partly-worked close survives the van being loaded.
    """
    # Deliberately no `prefetch_related` on the rows. `sync_expected` below
    # adds rows *after* this query, and a prefetch cache built here would not
    # contain them — so a product that sold out since the last visit would be
    # missing from the page until some later request happened to rebuild the
    # cache. That is the precise failure the sync exists to prevent, wearing a
    # disguise: the list looks complete, and the scarf that went at four
    # o'clock is simply never asked about. `_close_run_page` reads the rows
    # once, fresh, with its own select_related.
    run = get_object_or_404(CloseRun.objects.select_related("employee"), token=token)

    if request.method == "POST":
        if not run.is_open:
            messages.error(request, _CLOSED_RUN_MESSAGE)
            return redirect(_close_run_url(run, _COUNT))

        # Everything still answerable, freshly read: `sync_expected` may have
        # added rows since this page was drawn, and a submit that only knew
        # about the older ones would leave the newcomers out of the form it
        # validates against.
        rows = [r for r in run.rows.all() if not closing.is_frozen(run, r)]
        CountForm = build_close_count_form_class(rows)
        count_form = CountForm(request.POST)
        if not count_form.is_valid():
            # Re-rendered rather than redirected, or the numbers already
            # typed are lost along with the message saying which one was
            # rejected.
            # Everything revealed: the rejected row may be one somebody
            # opened the drawer to correct, and a form that comes back with
            # its error hidden is a form that reads as saved.
            return _close_run_page(
                request, run, count_form=count_form, mode=_COUNT,
                show_answered=True,
            )

        counts = count_form.counts()
        by_pk = {row.pk: row for row in rows}
        recorded = 0
        for pk, counted in counts.items():
            row = by_pk.get(pk)
            if row is None:
                continue
            before = row.finished_product.number_on_hand
            closing.record_count(run, row, counted)
            if counted != before:
                recorded += 1
        if recorded:
            messages.success(
                request,
                f"Trued up {recorded} product{'' if recorded == 1 else 's'} "
                f"the app had wrong.",
            )
        elif counts:
            messages.success(
                request,
                f"Counted {len(counts)} — all agreed with the app. Nothing moved.",
            )
        # Back to the counting list, not the cards. This is worked in several
        # passes across an evening, and defaulting a mid-pile submit into the
        # summary would put the person back through a link every time.
        return redirect(_close_run_url(run, _COUNT))

    # New zeros since the page was last opened get folded in here, so a close
    # started at noon still asks about the scarf that sold out at four.
    closing.sync_expected(run)
    return _close_run_page(request, run)


#: Older than any real timestamp, so a board nobody has fully checked sorts
#: as the one that has gone longest without one — which is true.
_NEVER = datetime.min.replace(tzinfo=dt_timezone.utc)


#: Said the same way wherever a closed day is written to. The van has been
#: unpacked by now, so this is somebody working from a stale tab or a
#: bookmarked URL rather than somebody standing in front of the tags.
_CLOSED_RUN_MESSAGE = (
    "That close is finished — a run covers one day and yesterday's is a "
    "record. Anything still wrong goes through a bulk inventory update, "
    "where the reason gets written down."
)


#: The two readings of one close. **Counting** is the evening's work — one
#: question per row, worked down a physical pile. **Cards** is what the
#: evening leaves behind: the stack of kanban tags that should now be in
#: somebody's hand, and the ones that go back in a bag. They are the same
#: rows read for different purposes, and by the time anybody follows a link
#: off the history page or reopens the run tomorrow, the stack is the only
#: question left.
#:
#: The mode rides in the query string rather than a session or a cookie, so a
#: reading is a link somebody can send, and every link and redirect off the
#: page carries it — the same bargain the restock board's `?photos=1` makes.
_COUNT, _CARDS = "count", "cards"
_CLOSE_MODES = (_COUNT, _CARDS)


def _close_run_url(run, mode=None):
    """This run's URL, in a mode. Reversed by name, never hardcoded."""
    url = reverse("close_run", args=[run.token])
    return f"{url}?mode={mode}" if mode else url


def _close_mode(request, run, rows):
    """Which reading a bare URL gets.

    Counting until something has been counted, cards afterwards. The first
    submit is the moment the page's usefulness changes hands: before it there
    is nothing to check a stack against, and after it the stack is what
    somebody is holding. A finished day is always cards — nothing on it can
    be counted any more, and it is being read rather than worked.

    An explicit `?mode=` always wins, which is what keeps a person mid-count
    counting: every action on that half of the page redirects back carrying
    it, so submitting the fourth of five passes doesn't drop somebody into
    the summary.
    """
    mode = (request.GET.get("mode") or "").strip().lower()
    if mode in _CLOSE_MODES:
        return mode
    if not run.is_open:
        return _CARDS
    if any(row.outcome != CloseRunRow.PENDING for row in rows):
        return _CARDS
    return _COUNT


def _close_run_page(request, run, count_form=None, mode=None, show_answered=None):
    """Render one close, in one of its two readings."""
    rows = list(
        run.rows.select_related(
            "finished_product__recipe", "finished_product__raw_product"
        )
    )
    # Everything still answerable, in one stable order. Deliberately not
    # answered-first: this is worked down a physical pile, and a list that
    # reorders itself under a thumb between submits loses somebody's place.
    # An agreed row stays here with its number showing, because it moved no
    # stock and a bag found under the table at seven has to be able to
    # correct what was answered at four.
    open_rows = [r for r in rows if not closing.is_frozen(run, r)]
    applied_rows = [r for r in rows if r.is_applied]
    confirmed_rows = [r for r in rows if r.outcome == CloseRunRow.CONFIRMED]

    # **Answered rows come off the counting list.** What somebody is looking
    # for on this page is the next thing they have not checked, and on a list
    # twenty-three long a settled row between two unsettled ones is a row
    # that has to be read to be skipped. So the list shows what is left and
    # says how much is behind the button.
    #
    # A reveal rather than a mode: unlike `?mode=`, nothing carries
    # `?answered=1` onward, so it evaporates the moment somebody submits or
    # follows a link — same inversion as the restock board's `?bare=1`. The
    # focused list is what the page is for, and having to re-open the drawer
    # is cheaper than a stale reveal quietly putting the long list back.
    #
    # A finished day is all record and nothing is answerable on it, so there
    # is nothing to hide behind: everything shows.
    if show_answered is None:
        show_answered = (
            not run.is_open or request.GET.get("answered") == "1"
        )
    pending_rows = [r for r in open_rows if r.outcome == CloseRunRow.PENDING]
    form_rows = open_rows if show_answered else pending_rows

    if count_form is None:
        count_form = build_close_count_form_class(form_rows)(
            initial=_count_initial(form_rows)
        )

    # The unexpected-tag search is a plain GET rather than a type-ahead. It
    # is the one place on this page that needs the network, and the network
    # is a field on one bar — a search box that silently does nothing when a
    # request is dropped is worse than one that visibly reloads.
    # Only meaningful without htmx, which swaps the results in and leaves the
    # address bar alone. The plain GET still puts the query here, so the
    # no-script path is unchanged and a search is still a link.
    query = (request.GET.get("q") or "").strip()

    # Read live, off the same rows, after everything this close has applied.
    # Not a new category alongside the outcomes: it is the one test the
    # evening began with, asked again of the numbers it corrected — plus the
    # rows nobody has answered, which are work remaining rather than a card
    # call either way.
    cards, no_cards, uncounted = closing.card_status(rows)

    return render(request, "scarves/close_run.html", {
        "run": run,
        "mode": mode or _close_mode(request, run, rows),
        "count_mode": _COUNT,
        "cards_mode": _CARDS,
        "count_url": _close_run_url(run, _COUNT),
        "cards_url": _close_run_url(run, _CARDS),
        "cards": cards,
        "no_cards": no_cards,
        "uncounted": uncounted,
        "show_answered": show_answered,
        "answered_count": len(applied_rows) + len(confirmed_rows),
        "pending_count": len(pending_rows),
        # The drawer, both ways, with the mode still on them.
        "show_answered_url": _close_run_url(run, _COUNT) + "&answered=1",
        "hide_answered_url": _close_run_url(run, _COUNT),
        "tag_search_url": reverse("close_tag_search", args=[run.token]),
        "query": query,
        "results": search_products(query) if query else None,
        "tally": closing.tally(run),
        "rows": rows,
        "applied_rows": applied_rows,
        "confirmed_rows": confirmed_rows,
        "count_fields": [
            {
                "row": row,
                "field": count_form[f"counted_{row.pk}"],
                "more": count_form[f"more_{row.pk}"],
            }
            for row in form_rows
            if f"counted_{row.pk}" in count_form.fields
        ],
        "count_form": count_form,
    })


def _count_initial(rows):
    """Show an already-given answer back on its own buttons.

    A row answered at exactly what the app believed moved nothing and stays
    editable all evening, so the page has to come back with that answer
    visible — an unmarked row reads as one nobody has reached, and on a list
    worked in three passes across an evening that is how a product gets
    counted twice or skipped.

    An answer above what the display holds lands on "more" with the number in
    the box, which is where it was typed in the first place.
    """
    initial = {}
    for row in rows:
        if row.counted is None:
            continue
        if row.counted <= (row.display_slots or 0):
            initial[f"counted_{row.pk}"] = str(row.counted)
        else:
            initial[f"counted_{row.pk}"] = "more"
            initial[f"more_{row.pk}"] = row.counted
    return initial


def close_tag_search(request, token):
    """The unpredicted-tag search, as a fragment rather than a page load.

    This was a plain form submit on purpose, and the reason it changed is
    worth recording rather than quietly reversing. The original argument was
    that the close runs in a field on one bar of signal, and a search box
    that silently does nothing when a request is dropped is worse than one
    that visibly reloads. That is still true — so the failure is *shown*
    (see the handlers on the page) rather than the round trip being avoided.

    What the argument missed is what the reload costs on the page it is on.
    The search sits below a list of twenty-odd rows, and a full navigation
    throws away the scroll position, so finding the box again is a scrub down
    the page every single time. That is paid on every search; the dropped
    request is paid rarely and is now visible when it happens.

    Still **submit only, never a type-ahead.** One request when somebody has
    finished typing, not one per keystroke on a phone that has one bar — and
    the original objection applies with full force to a request nobody asked
    for.

    The page renders the same partial inline, so the first paint and every
    swap are the same markup. Without htmx the form is an ordinary GET to the
    page itself and works exactly as it did.
    """
    run = get_object_or_404(CloseRun, token=token)
    query = (request.GET.get("q") or "").strip()
    return render(request, "scarves/partials/close_tag_results.html", {
        "run": run,
        "query": query,
        "results": search_products(query) if query else None,
    })


@require_POST
def close_add_tag(request, token):
    """A tag in hand for a product the close didn't predict. Adds, moves nothing.

    The old version adjusted straight to zero on the strength of the tag, and
    had to distinguish two findings to say so honestly. Both go away now: the
    tag says the bag is empty, the display still has units on it, and what
    settles the row is the same count every other row gets. So this puts the
    product on the list and the person counts it like the rest.
    """
    run = get_object_or_404(CloseRun, token=token)
    if not run.is_open:
        messages.error(request, _CLOSED_RUN_MESSAGE)
        return redirect(_close_run_url(run, _COUNT))

    product = (
        FinishedProduct.objects.filter(
            pk=request.POST.get("product_id"), is_active=True
        )
        .select_related("raw_product")
        .first()
    )
    if product is None:
        messages.error(request, "Couldn't find that product — try the search again.")
        return redirect(_close_run_url(run, _COUNT))

    row, created = closing.add_tag(run, product)
    if not created:
        messages.info(request, f"{product.name} was already on this close.")
    else:
        messages.success(
            request,
            f"Added {product.name} to the list — the app has "
            f"{row.on_hand_before} on hand. Count what's on the display and "
            f"say how many.",
        )
    return redirect(_close_run_url(run, _COUNT))


@require_POST
def close_undo(request, token, pk):
    """Take back an answer, from the page, without an account.

    Deliberately reachable by whoever made the mistake. Needing a staff login
    to undo a mis-tap means the person who made it has to go and tell
    somebody, and the cost of that conversation is what gets a wrong count
    left unmentioned instead. See `closing.undo` — the movement is reversed,
    the history is not rewritten.
    """
    run = get_object_or_404(CloseRun, token=token)
    if not run.is_open:
        messages.error(request, _CLOSED_RUN_MESSAGE)
        return redirect(_close_run_url(run, _COUNT))

    row = run.rows.filter(pk=pk).select_related("finished_product").first()
    if row is None:
        # Already undone, most likely a double tap. Says so plainly rather
        # than erroring, because the page is now in the state they wanted.
        messages.info(request, "That one's already been put back.")
        return redirect(_close_run_url(run, _COUNT))

    name = row.finished_product.name
    closing.undo(run, row)
    messages.success(
        request,
        f"Put {name} back the way it was. Nothing to tell anyone about — the "
        f"log keeps both entries.",
    )
    return redirect(_close_run_url(run, _COUNT))


@page_meta(
    title="Close History",
    description="What each Sunday close found: the products the app had "
                "wrong, which way, and by how much.",
    category="Inventory",
)
@login_required
def close_history(request):
    """What the closes have caught, newest day first.

    Reads as a list of failures on purpose — that is the output. Extra tags
    are stock that left without registering: a swapped sale, a hand-keyed
    line, or a webhook that has quietly stopped delivering, which physically
    becomes an extra tag about a week later and is findable here without
    going near Square. Missing tags are the other end of the pipeline, stock
    that arrived without being recorded.
    """
    runs = (
        CloseRun.objects.select_related("employee")
        .prefetch_related(
            "rows__finished_product__recipe",
            "rows__finished_product__raw_product",
        )[:26]
    )
    entries = [{"run": run, "tally": closing.tally(run)} for run in runs]

    # A product that keeps coming back is the useful reading. One weekend's
    # disagreement is noise; the same SKU three weekends running is a cause
    # with a name on it.
    repeats = {}
    for entry in entries:
        for row in entry["tally"]["missing_rows"] + entry["tally"]["extra_rows"]:
            repeats.setdefault(row.finished_product, []).append(row)
    repeat_rows = sorted(
        ({"product": p, "rows": rs} for p, rs in repeats.items() if len(rs) > 1),
        key=lambda d: -len(d["rows"]),
    )

    return render(request, "scarves/close_history.html", {
        "entries": entries,
        "repeat_rows": repeat_rows,
        "found_total": sum(e["tally"]["disagreements"] for e in entries),
        "under_total": sum(e["tally"]["under_units"] for e in entries),
        "over_total": sum(e["tally"]["over_units"] for e in entries),
    })


#: The table, in reading order: what it is, then what it did, then what is
#: left. Every one is sortable — a column you can see and can't sort by is a
#: question the page can obviously answer and won't.
SALES_COLUMNS = [
    ("name", "Product"),
    ("blank", "Style"),
    ("colorway", "Colorway"),
    ("units", "Units sold"),
    ("transactions", "Sales"),
    ("days", "Days"),
    ("value", "Value"),
    ("last", "Last sold"),
    ("on_hand", "On hand / par"),
    ("short", "Short"),
]


def _int_or_none(text):
    """A positive integer off the query string, or None.

    A hand-edited or stale `?blank=` degrades to no filter rather than a 500
    — the catch-all redirect means bad links are ordinary here, and the
    failure worth avoiding is a page that errors instead of answering.
    """
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _hidden(base, *keys):
    """The subset of the page's state one form has to carry for the other."""
    return [(k, base[k]) for k in keys
            if base.get(k) not in (None, "", 0)]


def _sales_href(base, **overrides):
    """`private/sales/` with the current view's state, minus what's overridden.

    Every control on the page carries the whole of the rest of the state —
    sorting a filtered range keeps the filter and the range, and a pill keeps
    the sort. Same rule the colour page's pills follow, for the same reason:
    the useful views are combinations, and a control that resets the others
    means the combination can only be reached by starting again.
    """
    params = dict(base)
    params.update(overrides)
    params = {k: v for k, v in params.items() if v not in (None, "", 0)}
    query = urlencode(params)
    return reverse("sales_report") + (f"?{query}" if query else "")


@page_meta(
    title="Top Sellers",
    description="What sold over a date range, one row per finished product: "
                "units, how many separate sales, and what's left against par. "
                "Today, yesterday, or dates you pick.",
    category="Inventory",
    note="Sortable columns; ?range=today|yesterday|7|30|all or ?from=&to=",
)
@login_required
def sales_report(request):
    """Top sellers over a range, sortable and narrowable.

    The whole of the page's state is in the query string — range, filters,
    sort column and direction — so a particular reading is a link somebody
    can send, and the back button walks back through the questions asked
    rather than dumping you at today's default.

    **`on hand / par` is one column, not two.** The number that matters after
    "twelve of these sold" is not the stock and not the target but the gap
    between them, and putting them side by side in one cell is what makes it
    readable without arithmetic. It is sorted on the shortfall for the same
    reason.

    Nothing here schedules anything. It is a page somebody reads: a colorway
    at the top of this table with nothing left is an argument for raising its
    par, which stays a deliberate decision about demand rather than something
    a report gets to make.
    """
    rng = sales.resolve_range(request.GET)

    q = request.GET.get("q", "").strip()
    category_id = _int_or_none(request.GET.get("category"))
    raw_product_id = _int_or_none(request.GET.get("blank"))

    sort = request.GET.get("sort", "")
    if sort not in sales.SORTS:
        sort = sales.DEFAULT_SORT
    direction = request.GET.get("dir", "")
    if direction not in ("asc", "desc"):
        direction = "desc" if sort in sales.DESCENDING_FIRST else "asc"

    logs = sales.narrow(
        sales.sale_logs(rng), q=q,
        category_id=category_id, raw_product_id=raw_product_id,
    )
    rows = sales.product_rows(logs)
    rows = sales.sort_rows(rows, sort, descending=direction == "desc")

    # The state every link on the page starts from.
    base = dict(rng.querystring())
    base.update({
        "q": q,
        "category": category_id,
        "blank": raw_product_id,
        "sort": sort,
        "dir": direction,
    })

    # Column headings. A heading already sorted flips direction; any other
    # heading opens the way that column reads first — biggest-first for a
    # ranking, A-first for a name.
    columns = []
    for key, label in SALES_COLUMNS:
        if key == sort:
            nxt = "asc" if direction == "desc" else "desc"
        else:
            nxt = "desc" if key in sales.DESCENDING_FIRST else "asc"
        columns.append({
            "key": key,
            "label": label,
            "sorted": key == sort,
            "direction": direction if key == sort else "",
            "href": _sales_href(base, sort=key, dir=nxt),
        })

    return render(request, "scarves/sales_report.html", {
        "range": rng,
        "ranges": [
            {
                "key": key,
                "label": label,
                "on": rng.key == key,
                # "Choose dates" is the form below rather than a link, so it
                # is a pill that only ever shows state.
                "href": None if key == "custom" else _sales_href(
                    base, range=key, **{"from": None, "to": None}
                ),
            }
            for key, label in sales.RANGES
        ],
        "rows": rows,
        "columns": columns,
        "totals": sales.totals(rows),
        "sources": sales.by_source(logs),
        "q": q,
        "category_id": category_id,
        "blank_id": raw_product_id,
        "categories": RawProductCategory.objects.all(),
        "blanks": RawProduct.objects.filter(
            finished_products__isnull=False
        ).distinct().order_by("name"),
        "sort": sort,
        "direction": direction,
        # Each of the two forms carries the state the other one owns, as
        # hidden fields, so submitting either keeps everything already set —
        # typing a colour name must not silently drop back to today.
        "date_hidden": _hidden(base, "q", "category", "blank", "sort", "dir"),
        "filter_hidden": _hidden(base, "range", "from", "to", "sort", "dir"),
        "clear_href": _sales_href(dict(rng.querystring())),
        "any_filter": bool(q or category_id or raw_product_id),
    })
