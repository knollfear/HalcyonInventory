"""Raw inventory, suppliers, blanks, the bulk stock take and the fancy conversion: the shelves."""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django import forms
from django.urls import reverse
from django.db import transaction
from django.db.models import F, Count, Sum, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.shortcuts import render, redirect

from ..sitemap import page_meta
from ..models import (
    FinishedProduct,
    InventoryLog,
    RawProduct,
    RawProductCategory,
    SaleLine,
    Supplier,
)
from .. import fancy, ledger, rawdemand
from ..forms import RawProductForm
from .invoices import _remember_wording
from .matrix import _parse_raw_ids


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
    products = RawProduct.prime_on_order(
        RawProduct.objects.active().filter(category=category).order_by("name")
    )

    # Setting the floor is a mode, the same bargain the recipe page's par mode
    # makes and for the same structural reason: par boxes and delivery boxes
    # in one table means a par typed in and then abandoned by pressing **Save
    # this bill**, written nowhere and said nothing about. One mode, one form,
    # one meaning per button.
    #
    # The same argument makes supply a third mode rather than two more boxes
    # on the bill: a cost typed beside a delivery and then lost to the wrong
    # button is the identical failure, and this form is the one somebody sits
    # down to work top to bottom.
    if request.GET.get("supply") == "1":
        return render(request, "scarves/raw_inventory.html", {
            "category": category,
            "products": products,
            "all_categories": RawProductCategory.objects.all().order_by("name"),
            "supply_mode": True,
            "supply_rows": _supply_rows(products),
            "suppliers": Supplier.objects.active().order_by("name"),
            "typed": {},
            "errors": {},
        })

    par_mode = request.GET.get("par") == "1"
    if par_mode:
        outlooks = rawdemand.rows(products)
        return render(request, "scarves/raw_inventory.html", {
            "category": category,
            "products": products,
            "all_categories": RawProductCategory.objects.all().order_by("name"),
            "par_mode": True,
            "outlooks": outlooks,
            # Whether anything on this table is dyed at all, so the page can
            # say what the floor is protecting. Notions are bought and resold
            # as they arrive, and "so the dye room never stops" beside a shelf
            # of yarn bowls names a consequence that cannot follow — which is
            # the sentence somebody reads to decide what number to type.
            # Derived from the rows rather than from the category's name, the
            # same call `_showcase_categories` makes: a table that grows its
            # first dyed blank starts reading the other way with nothing to
            # change.
            "dyed_table": any(o.is_dyed for o in outlooks),
            "typed": {},
            "errors": {},
        })

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
        "par_mode": False,
        "typed": typed,
        "errors": errors,
    })


@require_POST
@login_required
def raw_par_save(request, category_id):
    """Set the working floor on a category's blanks, one save for the lot.

    **The floor is what has to stay on the shelf so the dye room never
    stops** — a level you stay above, not a season's requirement, which is
    derived beside it and never written here. `rawdemand` has the argument for
    keeping the two apart.

    Until this the only door was the Django admin, so `par_level` sat at 100
    on every blank in the shop: a uniform remnant reading as a decision, which
    is the failure this codebase has already had once with `FinishedProduct.par`
    and named at length. The evidence to choose against is printed beside each
    box and **nothing here proposes a number.**

    The rules are the raw-inventory form's own, so one page keeps one
    behaviour: absolute rather than a delta, the whole form read before any of
    it is written, and a box that does not read changes nothing. A blank box
    means untouched — which differs from the recipe page, where every product
    renders a box every time and an empty one could only be a mistake. Here a
    category runs to forty blanks and a visit usually means to change one.

    Writes no `InventoryLog` and moves no stock. A floor is a target, not a
    shelf.
    """
    category = get_object_or_404(RawProductCategory, pk=category_id)
    products = list(
        RawProduct.objects.active().filter(category=category).order_by("name")
    )
    back = f"{reverse('raw_inventory', args=[category.pk])}?par=1"

    changes = []
    for product in products:
        raw_value = (request.POST.get(f"par_{product.pk}") or "").strip()
        if not raw_value:
            continue
        if not raw_value.isdigit():
            messages.error(
                request,
                f"“{raw_value}” isn't a par for {product.name} — nothing was "
                "changed. Par is a whole number, and 0 means no par is set.",
            )
            return redirect(back)
        par = int(raw_value)
        if par != product.par_level:
            changes.append((product, product.par_level, par))

    if not changes:
        messages.info(request, "Every par is already what the form says.")
        return redirect(back)

    with transaction.atomic():
        for product, _old, par in changes:
            product.par_level = par
            # Not a bare `update()`: `mirror_passthrough_stock` hangs off this
            # model's `post_save`, and one pile with two rows counting it is
            # the failure the raw page's own save note describes.
            product.save(update_fields=["par_level"])

    # Named one by one with the number it moved from, like the recipe page's:
    # nothing records a par change, so the message is the only confirmation
    # that the row which moved is the row you meant.
    messages.success(
        request,
        "Par updated: "
        + ", ".join(f"{p.name} {old} → {new}" for p, old, new in changes)
        + ".",
    )
    return redirect(back)


#: What each written field is called in the save message. Named rather
#: than derived: "order_url" is a column name, and the message is the
#: only confirmation that the row which moved is the row meant.
_SUPPLY_LABELS = {
    "price": "cost",
    "order_url": "link",
    "supplier": "supplier",
}


@require_POST
@login_required
def raw_supply_save(request, category_id):
    """Set what a blank costs and where it is reordered from, in one pass.

    **These two columns have been on this page since it existed and neither
    could be filled in from it.** The page prints a cost and a *Supplier
    page* link on every row — the two facts a reorder actually needs — and
    the only door to either was the Django admin, one product per screen.
    Twenty notions is twenty round trips through a form built for a
    developer, which is why every one of them still reads $0.00: the import
    cannot know a cost, and nothing since has made it cheap to say.

    So this is the bill form's shape applied to the other two columns: every
    row on one page, blank means untouched, and **one Save for the lot**. A
    costing pass is one sitting the way a delivery is one document.

    **Nothing is written unless every line reads**, the same refusal the bill
    makes and for the same reason — half a costing pass applied is worse than
    none, because the half that failed is invisible afterwards and the
    numbers that landed look complete. The offending row is named under
    itself rather than only in the banner.

    **A price of zero is taken at its word, and blank is what means
    untouched.** `RawProduct.price` is not nullable, so the two cannot be
    told apart once stored — which is exactly why the form must not read an
    empty box as a decision. Somebody clearing a box means "I don't know
    this one", and writing 0 there would turn a gap into a claim.

    Writes no `InventoryLog` and moves no stock: a cost is a fact about a
    supplier, not about a shelf.
    """
    category = get_object_or_404(RawProductCategory, pk=category_id)
    products = list(
        RawProduct.objects.active().filter(category=category).order_by("name")
    )
    back = f"{reverse('raw_inventory', args=[category.pk])}?supply=1"

    changes, errors = [], []
    for product in products:
        cost_raw = (request.POST.get(f"cost_{product.pk}") or "").strip()
        url_raw = (request.POST.get(f"url_{product.pk}") or "").strip()
        fields = {}

        if cost_raw:
            try:
                cost = Decimal(cost_raw.lstrip("$").replace(",", ""))
            except (InvalidOperation, ValueError):
                errors.append(f"{product.name}: “{cost_raw}” isn't a cost.")
                continue
            if cost < 0:
                errors.append(f"{product.name}: a cost can't be negative.")
                continue
            # Two decimal places, because that is what money has and what a
            # supplier's invoice prints. Not the by-feel rounding the dye
            # amounts take — there is no physical tool setting the
            # resolution here, only the currency.
            cost = cost.quantize(Decimal("0.01"))
            if cost != product.price:
                fields["price"] = cost

        # A select, so the only values offered are rows that already exist.
        # Blank means untouched, like every other box on this form.
        supplier_raw = (request.POST.get(f"supplier_{product.pk}") or "").strip()
        if supplier_raw.isdigit():
            chosen = Supplier.objects.filter(pk=supplier_raw).first()
            if chosen is None:
                errors.append(f"{product.name}: that supplier no longer exists.")
                continue
            if chosen.pk != product.supplier_id:
                fields["supplier"] = chosen

        if url_raw:
            validator = URLValidator()
            try:
                validator(url_raw)
            except ValidationError:
                errors.append(
                    f"{product.name}: “{url_raw}” isn't a link. It needs the "
                    f"https:// on the front."
                )
                continue
            if url_raw != product.order_url:
                fields["order_url"] = url_raw

        if fields:
            changes.append((product, fields))

    if errors:
        # Re-rendered rather than redirected, so nothing typed is lost — the
        # bill form's bargain, and the reason it exists: losing a page of
        # looked-up suppliers to one missing https:// is the expensive
        # failure on this form.
        for problem in errors:
            messages.error(request, problem)
        messages.error(
            request,
            "Nothing was saved — a costing pass goes in whole or not at all. "
            "Fix the line named above and save again.",
        )
        return render(request, "scarves/raw_inventory.html", {
            "category": category,
            "products": products,
            "all_categories": RawProductCategory.objects.all().order_by("name"),
            "supply_mode": True,
            "supply_rows": _supply_rows(products),
            "suppliers": Supplier.objects.active().order_by("name"),
            "typed": {
                product.pk: {
                    "cost": (request.POST.get(f"cost_{product.pk}") or "").strip(),
                    "url": (request.POST.get(f"url_{product.pk}") or "").strip(),
                }
                for product in products
            },
            "errors": {},
        })

    if not changes:
        messages.info(request, "Nothing filled in, so nothing changed.")
        return redirect(back)

    with transaction.atomic():
        for product, fields in changes:
            for name, value in fields.items():
                setattr(product, name, value)
            # `save()` rather than `update()`: `mirror_passthrough_stock`
            # hangs off this model's `post_save`, and the page's own save
            # note has the argument.
            product.save(update_fields=list(fields))

    messages.success(
        request,
        f"Saved {len(changes)} product{'' if len(changes) == 1 else 's'}: "
        + ", ".join(
            f"{p.name} ({', '.join(_SUPPLY_LABELS[f] for f in fields)})"
            for p, fields in changes
        )
        + ".",
    )
    return redirect(back)


@page_meta(
    title="Suppliers",
    description="Who each blank is bought from — the shops with a website and "
                "the people at the next stall alike. Pick one for its card.",
    category="Inventory",
)
@login_required
def supplier_index(request):
    """The picker, which exists so the card is reachable by clicking.

    Carries the blank count and whether a lead time is known, so the page
    answers "who have I not filled in yet" without a click — the same shape
    `raw_inventory_index` uses.
    """
    suppliers = (
        Supplier.objects.active()
        .annotate(blanks=Count("raw_products", filter=Q(raw_products__is_active=True)))
        .order_by("name")
    )
    return render(request, "scarves/supplier_index.html", {
        "suppliers": suppliers,
        # Named rather than counted: these are the rows somebody has to go
        # and fix, and a bare number sends nobody anywhere. The notions are
        # all of them today, because a person at the next stall has no URL
        # for the backfill to match on.
        "unlinked": list(
            RawProduct.objects.active().filter(supplier__isnull=True)
            .select_related("category").order_by("category__name", "name")
        ),
    })


@page_meta(
    title="Supplier",
    description="One supplier: how to reach them, how long they take, and "
                "every blank bought from them.",
    category="Inventory",
    show_in_index=False,
)
@login_required
def supplier_detail(request, supplier_id):
    """One supplier's card — and the thing a people-supplier's blanks link to.

    This is the other half of `RawProduct.reorder_link`. A blank with its own
    product page links there; a blank whose supplier is a person links here,
    because here is where the phone number is. Without it the reorder column
    had nothing to offer for the twenty notions and simply rendered empty.
    """
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    blanks = RawProduct.prime_on_order(
        RawProduct.objects.active().filter(supplier=supplier)
        .select_related("category").order_by("category__name", "name")
    )
    return render(request, "scarves/supplier_detail.html", {
        "supplier": supplier,
        "blanks": blanks,
        # What this supplier is short of right now, so the card answers the
        # question somebody opened it to ask. Read off the blanks already in
        # memory rather than a query per row.
        "short": [b for b in blanks if b.raw_shortage],
    })


def _supply_rows(products):
    """Each blank with what it has earned, so an hour goes where the money is.

    **The ordering is the whole point of the mode.** Twenty notions is an
    hour of looking suppliers up, and that hour is not evenly worth
    spending: this catalogue put half a season's notion revenue into two
    products and left eleven of nineteen under $100 for the year. A costing
    pass worked alphabetically spends the same effort on $1 buttons as on
    the line that actually earns — and the buttons come first.

    So this one sorts by revenue and the other two modes stay on name. That
    is deliberate rather than inconsistent: the bill and the count are
    worked row by row against a piece of paper or a shelf, where
    alphabetical is what lets you find the line in your hand. This one is
    worked top-down until the hour runs out.

    **Revenue is lifetime, not this season.** A supplier link does not
    expire with the faire, and a product that earned well last year and has
    not shipped this one is exactly the row worth stopping on.

    Read in one aggregate off `SaleLine` rather than through `seasonreport`,
    which builds a whole season per blank. The precision a projection buys
    is worth nothing to a column that only sorts attention.
    """
    found = {
        row["raw_product"]: row
        for row in SaleLine.objects
        .filter(raw_product__in=products, event_type=SaleLine.PAYMENT)
        .values("raw_product")
        .annotate(units=Sum("quantity"), cents=Sum("gross_cents"))
    }
    rows = []
    for product in products:
        hit = found.get(product.pk) or {}
        # `blank_cost` rather than `price`, so a fancy blank's derived cost
        # is the one that shows — `price` is the supplier's number and a
        # fancy veil has no supplier. See `FancyBlankCostHasOneHomeTests`.
        cost = product.blank_cost or Decimal(0)
        ask = product.suggested_price
        rows.append({
            "blank": product,
            "units": int(hit.get("units") or 0),
            "revenue": Decimal(hit.get("cents") or 0) / Decimal(100),
            # None, never zero, when either half is missing. An uncosted row
            # would otherwise show its full asking price as margin, which is
            # the one wrong answer that reads as good news.
            "margin": (ask - cost) if (cost and ask is not None) else None,
        })
    rows.sort(key=lambda r: (-r["revenue"], r["blank"].name))
    return rows


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
        blank = entry["product"]
        before = blank.number_on_hand
        counted = "set_to" in entry
        after = entry["set_to"] if counted else max(before + entry["delta"], 0)

        # **A count that agrees is still a count.** The row is written anyway
        # when somebody typed an absolute, because what has just been learned
        # is that the shelf was looked at today — and that is the whole of
        # what `counted_at` records. Skipping it would date the count to
        # whenever the number last happened to change, which on a blank that
        # has held steady is a date from another season.
        if after == before and not counted:
            continue

        blank.number_on_hand = after
        if counted:
            blank.counted_at = timezone.now()
        blank.save()
        if after == before:
            applied.append(f"{blank.name} counted {after}, unchanged")
        else:
            applied.append(f"{blank.name} {before}→{after}")
    return applied


def _bulk_inventory_picker_products():
    """
    Raw products worth picking on the bulk-inventory page: active, and with at
    least one active finished product. Anything else has no rows to edit.
    """
    return (
        RawProduct.objects.active().filter(finished_products__is_active=True)
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
        for rp in RawProduct.objects.active().filter(id__in=raw_ids).select_related("category")
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
        FinishedProduct.objects.active().filter(raw_product__in=raw_products)
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

                    # The row wins over the form, whole: it is the more
                    # specific answer, and someone who filled one in on this
                    # row meant it to describe this row. Falling back
                    # field-by-field would blend a row's preset with the
                    # form's free text and produce a sentence nobody wrote.
                    reason = bulk_reason(
                        form.cleaned_data.get(f"reason_preset_{fp.id}") or "",
                        form.cleaned_data.get(f"reason_{fp.id}") or "",
                    ) or form_reason
                    # An absolute count through the ledger, which writes to
                    # whichever row holds it — an undyed passthrough's lives
                    # on its raw product.
                    ledger.count(
                        fp, new_val,
                        source=InventoryLog.SOURCE_BULK_UPDATE,
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


# --- Blanks -----------------------------------------------------------------
#
# The catalogue's other half. `private/raw-inventory/` answers how many there
# are and what they cost; this answers what they *are* — and until now nothing
# did. `RawProduct` is not in the admin and no page created one, so a blank
# could only come from a migration or the Square sync.


@page_meta(
    title="Blanks",
    description="Every undyed thing you buy: silk blanks, yarns, notions. "
                "Add a new one, or open one to change what it is — the bath "
                "size, the supplier, the par a new colorway of it gets.",
    category="Inventory",
)
@login_required
def blank_index(request):
    """The picker, and the only door that makes a blank.

    Grouped by category because that is how somebody holds the catalogue —
    the silk table and the yarn table — and because the first question about
    a new blank is which of them it joins.

    Retired blanks are listed last rather than hidden. Retire-don't-delete
    means the row is still pointed at by every sale and every production run
    it was ever part of, and a retired blank you cannot find is one somebody
    creates a second copy of.
    """
    products = list(
        RawProduct.objects.select_related("category", "supplier")
        .order_by("category__name", "name")
    )
    groups = {}
    for product in products:
        if product.is_active:
            groups.setdefault(product.category.name, []).append(product)
    return render(request, "scarves/blank_index.html", {
        "groups": sorted(groups.items()),
        "retired": [p for p in products if not p.is_active],
        # So the page can say what a blank with no cost means, in the one
        # place somebody is looking at all of them at once.
        "uncosted": [p for p in products if p.is_active and not p.price],
    })


@page_meta(
    title="New Blank",
    description="Add an undyed product: a silk blank, a yarn, a notion. "
                "Everything it needs to be orderable, dyeable and sellable.",
    category="Inventory",
    show_in_index=False,
)
@login_required
@require_http_methods(["GET", "POST"])
def blank_new(request):
    """Book a blank into existence.

    **Prefilled from the query string**, so an invoice line carrying
    something the catalogue has never heard of has somewhere to go without
    anything being retyped: `?name=&price=&supplier=&invoice_description=`.
    The parameters are named after the fields they fill, which is the rule
    that keeps a link like this readable a year later.

    `invoice_description` is the one that isn't a form field — it is written
    by confirming an invoice line, never typed — so it is carried through
    hidden and applied on save. That keeps the promise the field makes (only
    a confirmation writes it) while letting the confirmation that *creates*
    the blank count as one.
    """
    initial = {
        field: request.GET[field]
        for field in ("name", "price", "sku", "order_url", "notes")
        if request.GET.get(field)
    }
    if request.GET.get("category", "").isdigit():
        initial["category"] = request.GET["category"]
    if request.GET.get("supplier", "").isdigit():
        initial["supplier"] = request.GET["supplier"]
    wording = (request.GET.get("invoice_description") or "").strip()[:300]

    if request.method == "POST":
        form = RawProductForm(request.POST)
        wording = (request.POST.get("invoice_description") or "").strip()[:300]
        if form.is_valid():
            product = form.save()
            if wording:
                _remember_wording(product, wording)
                product.save(update_fields=["invoice_description"])
            messages.success(
                request,
                f"Created {product.name}."
                + _colorway_note(form)
                + (f" It will be recognised on an invoice as “{wording}” from "
                   f"now on." if wording else ""),
            )
            return redirect("blank_edit", raw_product_id=product.pk)
    else:
        form = RawProductForm(initial=initial)

    return render(request, "scarves/blank_form.html", {
        "form": form,
        "product": None,
        "wording": wording,
        "back": request.GET.get("next", ""),
    })


def _colorway_note(form):
    """What the save just made, said out loud.

    A form that quietly creates forty products is a form nobody can check.
    The skipped count matters as much as the made one: picking a colourway
    the blank already has is the ordinary way somebody adds the next few, and
    silence there reads as a failure.
    """
    made = len(getattr(form, "colorways_made", []))
    skipped = len(getattr(form, "colorways_skipped", []))
    if not made and not skipped:
        return ""
    bits = []
    if made:
        bits.append(f"{made} colourway{'' if made == 1 else 's'} made")
    if skipped:
        bits.append(f"{skipped} already existed")
    return " " + ", ".join(bits).capitalize() + "."


@page_meta(
    title="Blank",
    description="One undyed product: what it is, what it costs, how many go "
                "in a bath, and what a new colorway of it inherits.",
    category="Inventory",
    show_in_index=False,
)
@login_required
@require_http_methods(["GET", "POST"])
def blank_edit(request, raw_product_id):
    """Change what a blank is. **Not how many there are.**

    The shelf is not on this form. Counting happens on
    `private/raw-inventory/`, where two columns say which question is being
    answered, and a third box on a third page writing the same number is the
    second door that leaves two of them disagreeing.

    Retiring is a checkbox here rather than a button, because unlike the
    recipe list this is one blank on its own page — the argument for a
    collapsing row with an Undo was about a list a couple of hundred long
    where a vanished row is indistinguishable from a click that never
    arrived.
    """
    product = get_object_or_404(
        RawProduct.objects.select_related("category", "supplier"), pk=raw_product_id
    )
    if request.method == "POST":
        form = RawProductForm(request.POST, instance=product)
        if form.is_valid():
            form.save()
            messages.success(
                request, f"Saved {product.name}." + _colorway_note(form)
            )
            return redirect("blank_edit", raw_product_id=product.pk)
    else:
        form = RawProductForm(instance=product)

    return render(request, "scarves/blank_form.html", {
        "form": form,
        "product": product,
        "wording": product.invoice_description,
        "colorways": product.finished_products.active().count(),
        "back": "",
    })
