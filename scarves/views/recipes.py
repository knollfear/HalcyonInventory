"""Colorways: dye entry, the rainbow bands, the showcase, the recipe page, par, and the kanban cards."""
from urllib.parse import urlencode
from datetime import date, datetime, time
from io import BytesIO

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse
from django.db import transaction
from django.db.models import Count, Sum, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages
from django.shortcuts import render, redirect
from django.db.models import Prefetch

from ..sitemap import page_meta
from ..models import FinishedProduct, InventoryLog, RawProductCategory, RecipeDye
from .. import colorbands, production
from ..forms import NewDyeForm, QuickRecipeRowForm, RecipeDyesForm, dye_option_attrs
from ..models import Recipe
from .production import _sheets_ticked


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

    scope = Recipe.objects.active()
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
        Recipe.objects.active().filter(finished_products__is_active=True)
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


def _showcase_recipes(missing_only=False, category=None):
    """Active recipes with their dyes and their finished products.

    Finished products (and a photo) come along because the recipe name alone is
    often not enough to know what a colorway actually was — looking at the
    scarf is how you identify the dyes.

    **`category` narrows which recipes are listed and never which products a
    listed recipe shows.** Category is which table at the stall, and a
    colorway dyed on both a yarn and a silk is one colorway — so it belongs on
    both tables' lists, and on either of them it has to show the whole colorway
    or the row would be a partial answer to "what is this colour on". A recipe
    filtered *within* would put a different set of products under the same name
    depending on how you arrived, with nothing on the row to say so.
    """
    recipes = (
        Recipe.objects.active()
        .prefetch_related(
            Prefetch(
                "recipe_dyes",  # ← matches your related_name
                queryset=RecipeDye.objects.select_related("dye").order_by("order", "id"),
            ),
            Prefetch(
                "finished_products",
                queryset=FinishedProduct.objects.active()
                .prefetch_related("images")
                .order_by("name"),
            ),
        )
        .order_by("name")
    )
    if missing_only:
        recipes = recipes.filter(recipe_dyes__isnull=True)
    if category is not None:
        # `distinct()` because a colorway on three yarns matches three times.
        recipes = recipes.filter(
            finished_products__is_active=True,
            finished_products__raw_product__category=category,
        ).distinct()
    return recipes


def _showcase_categories(missing_only=False):
    """The tables that have any of this list's colorways on them.

    Derived from the rows rather than from a list of names, so a shop that
    grows a third table gets a third pill with nothing to change — the same
    call the Sunday close makes. A list whose colorways all sit on one table
    draws no pills at all, because a filter offering one choice is furniture.

    Verified by iterating, never by counting: `.count()` wraps a `distinct()`
    in a subquery and reports the right number while the query itself returns
    a row per match. That is how the season page shipped eleven thousand
    pills.
    """
    scope = RawProductCategory.objects.filter(
        raw_products__finished_products__is_active=True,
        raw_products__finished_products__recipe__is_active=True,
    )
    if missing_only:
        scope = scope.filter(
            raw_products__finished_products__recipe__recipe_dyes__isnull=True
        )
    return list(scope.order_by("name").distinct())


def _recipe_read_only_row(recipe, saved=False, missing_only=False, category=None):
    """One showcase row with its editor closed.

    Every row carries its Edit button, because **editing is not a mode.** It
    used to be — `?edit=true` swapped the whole page into a different thing —
    and that was one decision too many in front of a one-row job: you came to
    look at a colorway, found you wanted to change a dye, and had to go back
    up to a pill and reload the page to be allowed to. `edit_mode` now means
    only *this row is showing its pickers*, which is true of at most one row
    at a time and is nobody's mode.
    """
    return {
        "recipe": recipe,
        "edit_mode": False,
        "saved": saved,
        "edit_row_url": _edit_row_url(
            recipe, missing_only=missing_only, category=category
        ),
        "band_dots": _band_dots(recipe),
    }


def _band_dots(recipe):
    """The stored bands as coloured dots, for a row that isn't open.

    Only what a person confirmed — a closed row draws no guesses, for the same
    reason the reference sheet skips an unconfirmed colorway: a dot nobody
    agreed to is indistinguishable from one somebody did.
    """
    stored = set(recipe.color_bands or [])
    return [
        {"slug": slug, "label": label, "color": color}
        for slug, label, color in colorbands.BANDS
        if slug in stored
    ]


def _showcase_url(*, missing=False, category=None, row=None):
    """Every link on the showcase, built in one place.

    Each control carries the rest of the page's state rather than resetting
    it — switching table mid-edit must not drop you back to read-only, and
    opening a row must not drop the table you were reading. That is the same
    rule `private/colors/` pills and `private/sales/` follow, and the reason
    it is one function is that four templates each remembering to re-add three
    parameters is four chances to drop one silently.

    Reversed and absolute rather than a bare `?…`, because the row partial is
    also rendered by the fragment endpoints — where a relative query string
    would resolve against `/row/` and land nowhere.
    """
    params = {}
    if missing:
        params["missing"] = "true"
    if category is not None:
        params["category"] = category.name
    if row is not None:
        params["row"] = row
    url = reverse("recipe_showcase")
    if params:
        url += "?" + urlencode(params)
    if row is not None:
        url += f"#recipe-row-{row}"
    return url


def _edit_row_url(recipe, missing_only=False, category=None):
    """The no-script way into one row's editor."""
    return _showcase_url(missing=missing_only, category=category, row=recipe.pk)


def _oven_products(recipe):
    """The colorway's products, in the order its oven boxes are drawn.

    By blank name, because that is what the boxes are labelled with and a
    list somebody reads twice should not change order between reads.
    """
    return list(
        recipe.finished_products.active()
        .select_related("raw_product")
        .order_by("raw_product__name", "pk")
    )


def _recipe_row_context(recipe, form=None, saved=False, missing_only=False,
                        category=None):
    """One showcase row with its editor open."""
    products = _oven_products(recipe)
    if form is None:
        initial = {}
        for i, rd in enumerate(
            recipe.recipe_dyes.all()[: RecipeDyesForm.SLOTS], start=1
        ):
            initial[f"dye{i}"] = rd.dye_id
            # The stored amount, in the units the book writes it in. Without
            # this the box renders empty on a recipe that has one, and the
            # next Save down the row clears it — the same trap the oven boxes
            # were carrying.
            initial[f"{RecipeDyesForm.AMOUNT_PREFIX}{i}"] = rd.book_ounces
        # Without this a box renders unticked on a product that is flagged,
        # and the next Save on that row silently un-flags it.
        for product in products:
            initial[f"{RecipeDyesForm.OVEN_PREFIX}{product.pk}"] = product.oven_dyed
        form = RecipeDyesForm(initial=initial, products=products)
    oven = [p for p in products if p.oven_dyed]
    return {
        "recipe": recipe,
        "form": form,
        "edit_mode": True,
        # The badge is a summary of several answers now, so it has to say
        # whether they agree: a colorway that is oven work on its yarns and
        # microwave on its silk is the case the flag was moved to express,
        # and a bare "oven" tag would hide exactly that.
        "oven_count": len(oven),
        "oven_all": bool(oven) and len(oven) == len(products),
        "product_count": len(products),
        "saved": saved,
        "edit_row_url": _edit_row_url(
            recipe, missing_only=missing_only, category=category
        ),
        **_editor_band_chips(recipe),
    }


def _editor_band_chips(recipe):
    """The rainbow chips as this row draws them — which is not how
    `private/colors/` draws them, and the difference is deliberate.

    **The classifier's reading is shown unticked here.** On the colour page a
    suggestion arrives pre-ticked, because that page has exactly one button
    and pressing it is answering the one question the page asks. Here the
    button says Save and its subject is the dyes, so a pre-ticked guess would
    be confirmed by a click that was about something else — and a wrong band
    is the silent kind of wrong: you look in the orange section, the scarf
    isn't there, and nothing says it was filed under red.

    So a dashed chip means "the dyes read as this, tick it if that's right"
    rather than "a machine has already ticked this for you". Solid ticks are
    only ever what a person stored.
    """
    from_dyes = colorbands.bands_from_dyes(recipe)
    return {
        "chips": _band_chips(
            recipe.color_bands or [],
            # No point suggesting at a colorway somebody has already ruled on.
            [] if recipe.bands_confirmed else from_dyes,
        ),
        "bands_suggested": [] if recipe.bands_confirmed else from_dyes,
    }


@page_meta(
    title="Recipe Showcase",
    description="Gallery of active recipes with their dyes, colour swatches and "
                "the finished products made from them. Edit mode fills in "
                "missing dyes, including copying them from a recipe that "
                "already has them.",
    category="Recipes",
    note="Add ?missing=true for just the backlog, ?category=Yarn for one table.",
)
@login_required
def recipe_showcase(request):
    """The colorway list, with one row's dye pickers open at a time.

    **Editing is not a mode.** Every row carries its own Edit button and the
    page has one state. It used to have two, behind `?edit=true`, and that was
    a decision demanded before the job: you came to look at a colorway, found
    a dye you wanted to change, and had to go back to a pill and reload the
    whole page to be allowed to touch it. Nothing about a mode was earning
    that — the pickers are per row either way.

    **What the mode was really protecting was the render cost, and that is
    fixed at the root instead.** A `DyeSelect` offers the whole dye catalogue,
    and each `<option>` carries the colour and search text the type-ahead
    reads off it — about 225 bytes, so one picker is ~30 KB of markup and a
    row is five of them. Rendering all 162 rows' pickers was 810 copies of the
    same list: 121,000 options, 27 MB, 810 identical queries for the dyes
    (Django's `ModelChoiceField.queryset` setter calls `.all()`, which clones
    and drops the result cache), and thirteen seconds of server render before
    anybody could type. None of that list is *read* until a row is opened, and
    a pass down this page opens a handful.

    So a row renders its editor only when asked for — an htmx swap of
    `recipe_row`, or `?row=<pk>` with the script blocked, which is the same
    door the ✕ on the production sheet uses.

    `?row=` is parity rather than a promise: Save and Reset here have always
    been htmx buttons, so a script-blocked visitor could never write a dye on
    this page anyway. What the parameter preserves is that the way in is a
    *link with an `href`* — which cannot fail the way a click handler on a
    table that got reworked underneath it can — and that a row stays reachable
    and readable without the script. The dye form that really does post
    without one is `private/quick-recipes/`.
    """
    missing_only = request.GET.get("missing") == "true"

    # Named rather than numbered so a reading is a link somebody can read as
    # well as send, and an unknown name is no filter rather than an error —
    # the same call `secret/close/` makes about its table pills.
    categories = _showcase_categories(missing_only=missing_only)
    wanted = request.GET.get("category") or ""
    category = next((c for c in categories if c.name == wanted), None)

    recipes = list(_showcase_recipes(missing_only=missing_only, category=category))

    # The one row to render open. Unreadable or unknown falls back to none of
    # them rather than erroring: a filter is navigation, and the worst a stale
    # link should do here is show the list it was a link into.
    try:
        open_pk = int(request.GET.get("row", ""))
    except (TypeError, ValueError):
        open_pk = None

    rows = []
    for recipe in recipes:
        maker = (
            _recipe_row_context if recipe.pk == open_pk else _recipe_read_only_row
        )
        rows.append(
            maker(recipe, missing_only=missing_only, category=category)
        )

    # Scoped to what is on screen. "91 of 162 have no dyes" printed over a
    # list of forty is the page contradicting itself, and the number people
    # act on is the one beside the list they are reading — the rule
    # `private/colors/` pills and the close's banner already follow.
    if category is None:
        total = Recipe.objects.active().count()
        without = Recipe.objects.active().filter(recipe_dyes__isnull=True).count()
    else:
        in_table = _showcase_recipes(category=category)
        total = in_table.count()
        without = in_table.filter(recipe_dyes__isnull=True).count()

    return render(
        request,
        "scarves/recipe_showcase.html",
        {
            "rows": rows,
            "missing_only": missing_only,
            "categories": categories,
            "category": category,
            "total_count": total,
            "missing_count": without,
            # Every control carries the rest of the state rather than
            # resetting it, so switching table mid-edit doesn't drop you back
            # to read-only and switching mode doesn't drop the table.
            "url_all_recipes": _showcase_url(category=category),
            "url_missing": _showcase_url(missing=True, category=category),
            "url_every_table": _showcase_url(missing=missing_only),
            "category_links": [
                {
                    "name": c.name,
                    "on": category is not None and c.pk == category.pk,
                    "url": _showcase_url(missing=missing_only, category=c),
                }
                for c in categories
            ],
        },
    )


@require_POST
@login_required
def recipe_retire(request, pk):
    """Stop using a colorway, from the bulk editing list.

    **Retire, don't delete.** A recipe that ever sold is pointed at by
    inventory logs, resolved sales and production rows, and that history stays
    interesting long after the colour stops being made — so this is
    `is_active = False` and nothing else. The schema enforces the same thing
    from the other side: everything recording what happened points at a
    product with `on_delete=PROTECT`.

    **The row collapses to a strip rather than vanishing.** A row that
    disappeared is indistinguishable from a click that never arrived, and on a
    list this long that is the mistake somebody makes twice — the same call
    the unidentified-sales queue makes about a dismissed row.

    **And the strip carries Undo**, which the dismissal queue deliberately
    does not. The difference is what the click costs if it was wrong: a
    dismissed sale sets a timestamp, where retiring a colorway takes it off
    the production sheets, the reference sheets, the label runs and the Square
    sync at once. On a page of a hundred and sixty rows, next to a Save
    button, a mis-click is ordinary — and a fix somebody has to go and find in
    the admin is the kind that gets left unmentioned.
    """
    recipe = get_object_or_404(Recipe, pk=pk)
    Recipe.objects.filter(pk=recipe.pk).update(is_active=False)
    recipe.refresh_from_db()
    return render(request, "scarves/partials/recipe_retired.html", {"recipe": recipe})


@require_POST
@login_required
def recipe_restore(request, pk):
    """Put a retired colorway back, and hand the whole row back with it.

    Nothing was destroyed, so this is the plain inverse — unlike the close's
    Undo, which has to write a compensating entry because stock moved.
    """
    recipe = get_object_or_404(Recipe, pk=pk)
    Recipe.objects.filter(pk=recipe.pk).update(is_active=True)
    recipe = Recipe.objects.prefetch_related(
        "recipe_dyes__dye", "finished_products__images"
    ).get(pk=pk)
    return render(
        request,
        "scarves/partials/recipe_row.html",
        {"row": _recipe_read_only_row(recipe)},
    )


@login_required
def recipe_row(request, pk):
    """One showcase row, re-rendered — **closed unless `?edit=1` asks.**

    Closed is the default because of what a dropped parameter does either
    way: lose it here and you get the row as it reads on the page, which is
    harmless. Were the editor the default, the same slip would spring five
    pickers open on a row nobody asked to change.

    That makes this one endpoint both halves of the toggle. Edit opens it,
    Reset closes it — and closing *is* the reset, because nothing on the row
    was written: the pickers hold a form, and a form thrown away leaves the
    recipe exactly as the closed row already shows it. Reset used to
    re-render the pickers back to their stored values, which is the same
    outcome reached by a longer route, and it left the row looking like it
    was still mid-edit.
    """
    recipe = get_object_or_404(
        Recipe.objects.prefetch_related("recipe_dyes__dye", "finished_products__images"),
        pk=pk,
    )
    maker = (
        _recipe_row_context if request.GET.get("edit") == "1" else _recipe_read_only_row
    )
    return render(
        request, "scarves/partials/recipe_row.html", {"row": maker(recipe)}
    )


def _save_editor_bands(request, recipe):
    """Store the rainbow chips that rode in on the recipe row's Save.

    **This will not manufacture a confirmation out of silence.** Ticking
    nothing on an unconfirmed colorway leaves it unconfirmed, because an empty
    answer here is indistinguishable from somebody who opened the row to fix a
    dye and never looked at the chips — and `confirmed with no bands` is a
    state this app has been in before, arrived at by giving up, which prints
    the colorway in no section of the reference sheet at all.

    The deliberate "this colorway belongs in no section" answer still exists;
    it is `private/colors/`, where Confirm is the only button and pressing it
    means exactly that.

    A colorway already confirmed keeps its stamp, so saving dyes on one is
    idempotent rather than a quiet un-confirmation.
    """
    picked = colorbands.sort_bands(
        b for b in request.POST.getlist("bands") if b in colorbands.BAND_SLUGS
    )
    if not picked and not recipe.bands_confirmed:
        return
    recipe.color_bands = picked
    recipe.bands_confirmed_at = timezone.now()
    recipe.save(update_fields=["color_bands", "bands_confirmed_at"])


@require_POST
@login_required
def recipe_dyes_save(request, pk):
    """Save one recipe's dyes, its oven flag and its rainbow bands.

    One Save for all three because they are one pass down one list: the person
    filling in a colorway's dyes is looking at the colorway, which is who can
    say which box it is made in and which sections of the sheet it prints in.
    Three controls with three buttons would be three trips through 162 rows —
    the argument the oven checkbox already made, extended to the chips.

    What keeps that from collapsing two questions into one button is
    `_save_editor_bands`: the bands are only written when somebody ticked
    something, so a save that was about the dyes says nothing about the bands.
    """
    recipe = get_object_or_404(Recipe, pk=pk)
    form = RecipeDyesForm(request.POST, products=_oven_products(recipe))

    if not form.is_valid():
        recipe = Recipe.objects.prefetch_related(
            "recipe_dyes__dye", "finished_products__images"
        ).get(pk=pk)
        return render(
            request,
            "scarves/partials/recipe_row.html",
            {"row": _recipe_row_context(recipe, form=form)},
        )

    form.save(recipe)
    _save_editor_bands(request, recipe)
    recipe = Recipe.objects.prefetch_related(
        "recipe_dyes__dye", "finished_products__images"
    ).get(pk=pk)
    # Closed, not back to the pickers. Two reasons, and the second is the one
    # that matters: a saved row that reappears identical is the weakest
    # possible confirmation, where the closed row shows the dye chips and
    # swatches that were just recorded — you check the save by looking at the
    # colours. And a pass down 162 rows that left every editor open would put
    # back, one row at a time, exactly the 30 KB-per-picker weight this page
    # stopped paying up front. Editing again is one click.
    return render(
        request,
        "scarves/partials/recipe_row.html",
        {"row": _recipe_read_only_row(recipe, saved=True)},
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

    # Par editing is a mode and production entry is the default, which is the
    # opposite of the recipe showcase's ruling that editing is not a mode —
    # deliberately, because the two pages are answering different questions.
    # There, opening a row *is* the job. Here the job is recording a session,
    # par is the occasional visit, and both controls would otherwise sit in
    # one table: a par typed into a live input and then abandoned by pressing
    # Record production would be lost with nothing said. One mode, one form,
    # one meaning per button.
    par_mode = request.GET.get("par") == "1"
    if par_mode:
        # What the till sold this season, per blank — the same function
        # `private/production-needed/` ranks on, not a second answer to the
        # question. It is here because par is the one number in this app that
        # a person is expected to overrule and there was nothing beside it to
        # judge by. It does not propose a par: display capacity must never
        # reach production, and neither may a sales figure on its own.
        sold = production.sold_per_blank()
        for product in products:
            product.sold_this_season = sold.get(product.pk, 0)
            # Par restated in the unit production actually spends. Across the
            # live catalogue every distinct (par, bath size) pair lands
            # between 1.0 and 2.0 baths — par is a MOQ floor with a bath of
            # headroom, not days of cover — so baths are how a change to it
            # reads. It is the number in the box in another unit, never a
            # recommendation about it.
            product.par_baths = round(product.par / product.bath_size, 1)

    context = {"recipe": recipe, "products": products, "par_mode": par_mode}
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
    for product in recipe.finished_products.active():
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
    outcomes = []
    with transaction.atomic():
        for product, baths in entries:
            # `production.report` accepts any baths of this product that are
            # open on a sheet before booking the rest as unplanned — the
            # sheet's blanks came off when it was made, and taking them again
            # here is the double count that used to happen. One log row per
            # product per session rather than one per bath: the bath count is
            # recoverable from quantity, and a single deliberate entry reads
            # better in the history than N identical rows.
            per_bath = product.bath_size
            outcome = production.report(
                product, baths * per_bath,
                source=InventoryLog.SOURCE_RECIPE_PAGE,
                notes=(
                    f"{baths} dye bath{'' if baths == 1 else 's'} × {per_bath}, "
                    f"recorded from the {recipe.name} recipe page."
                ),
            )
            outcomes.append(outcome)
            made += outcome.units

    messages.success(
        request,
        f"Recorded {made} item{'' if made == 1 else 's'} across "
        f"{len(entries)} product{'' if len(entries) == 1 else 's'} "
        f"for {recipe.name}."
        + "".join(_sheets_ticked(outcome) for outcome in outcomes),
    )
    return redirect("recipe_detail", pk=pk)


@require_POST
@login_required
def recipe_par_save(request, pk):
    """Set par on one colorway's finished products, one blank per row.

    **Par is the number this app treats as a fact and never was one.** It
    reads across the catalogue as a uniform remnant rather than as forty
    decisions about demand, `private/production-needed/` orders on sales
    instead of on it and says so, and the production sheet had to be made
    editable end to end because the work order derived from par offered
    nowhere to disagree. Until this there was nowhere to change one either,
    short of the Django admin or a bulk action that writes every colorway on
    a blank at once — so the one number most in need of a person had the
    fewest doors.

    **Nothing here proposes a value.** The season's sold count is printed
    beside the box so the decision can be checked by looking, and that is the
    whole of the help this page gives: par is about demand, and a number that
    moved on its own — off display capacity, off a sales rate, off anything —
    is the failure this codebase keeps naming.

    Absolute values, like every other correction in the app: "par is 12"
    heals whatever the row said before, where "add four" only works if what
    was there was right.

    Reads the whole form before writing any of it, the same as
    `record_recipe_production` — a bad box must not leave half a colorway
    retuned. A blank box is refused rather than guessed at: **0 is how you
    say there is no par**, and it is what `production.candidates()` filters
    on, so treating an empty field as 0 would silently drop the product out
    of planning.
    """
    recipe = get_object_or_404(Recipe, pk=pk)
    back = f"{reverse('recipe_detail', args=[pk])}?par=1"

    changes = []
    for product in recipe.finished_products.active():
        raw_value = (request.POST.get(f"par_{product.pk}") or "").strip()
        if not raw_value.isdigit():
            messages.error(
                request,
                f"'{raw_value}' isn't a par for {product.name} — nothing was "
                "changed. Par is a whole number, and 0 means no par is set.",
            )
            return redirect(back)
        par = int(raw_value)
        if par != product.par:
            changes.append((product, product.par, par))

    if not changes:
        messages.info(request, "Every par is already what the form says.")
        return redirect(back)

    with transaction.atomic():
        for product, _old, par in changes:
            product.par = par
            product.save(update_fields=["par"])

    # Named one by one with the number it moved from. A par change is rare and
    # deliberate and nothing anywhere records it, so the message is the only
    # confirmation that the thing you meant to change is the thing that moved.
    messages.success(
        request,
        "Par updated: "
        + ", ".join(f"{p.name} {old} → {new}" for p, old, new in changes)
        + ".",
    )
    return redirect(back)


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
    per_bath = product.bath_size

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

        # Back to the stack, not to the card just finished. A card is done in
        # one pass — it is in your hand, you type its column, you put it down
        # — so landing back on it asks somebody to find their way to the next
        # one from a page about the last one. The message names the product,
        # so the confirmation survives the move, and the index's typed count
        # for that row has just gone up.
        #
        # Only the success path leaves. A refused card stays put with its
        # rows to fix, and so does an empty submit, which is a slip rather
        # than a finished card.
        messages.success(
            request,
            f"Added {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} "
            f"from {product.name}'s card. Current stock is unchanged.",
        )
        return redirect("card_backfill_index")

    # Every production entry that still stands, however it was written — not
    # just the ones typed off a card. The page exists to stop a bath going in
    # twice, and a bath recorded live at the time is exactly as much of a
    # duplicate as one typed from the card yesterday; showing only the typed
    # ones answers "have I transcribed this card" when the question in front
    # of somebody holding it is "is this date already in".
    #
    # `reversals__isnull=True` for the same reason `producedsince.entries`
    # uses it: the question is never whether a row was written, it is whether
    # one still stands. A bath that was recorded and then taken back is a
    # bath that did not happen, so it must not sit here reading as already
    # entered.
    recorded = list(
        product.inventory_logs
        .filter(log_type=InventoryLog.PRODUCTION, reversals__isnull=True)
        .order_by("-created_at", "-pk")
    )
    for log in recorded:
        # Flagged here rather than compared in the template, so the one place
        # that knows which source is history-only stays one place.
        log.from_card = log.source == InventoryLog.SOURCE_CARD_BACKFILL

    return render(request, "scarves/card_backfill.html", {
        "product": product,
        "per_bath": per_bath,
        "rows": range(CARD_ROWS),
        "recorded": recorded,
        "today": timezone.localdate(),
    })
