"""Production: what to dye (par and the close), the sheets, the crew's return page, and the receipt."""
import logging
from urllib.parse import urlencode
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpResponse
from django.urls import reverse
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages
from django.shortcuts import render, redirect
from django.template.response import TemplateResponse

from ..sitemap import page_meta
from ..models import (
    CloseRun,
    CloseRunRow,
    FinishedProduct,
    InventoryLog,
    ProductionRun,
    RawProductCategory,
    RecipeDye,
)
from .. import (
    closeplan,
    crew,
    dyebill,
    producedsince,
    production,
    sheetscan,
    slowsellers,
)
from ..forms import (
    LabelRunForm,
    PickedBathsField,
    ProducedSinceForm,
    ProductionSheetForm,
)
from ..models import normalize_token
from .common import _is_htmx
from .images import search_products

logger = logging.getLogger(__name__)


@page_meta(
    title="Production Needed",
    description="Finished products that are below par, grouped by recipe and "
                "sorted so out-of-stock items surface first. Record dye baths "
                "inline. Optional ?category=<id> filter.",
    category="Production",
)
@login_required
def production_needed_view(request):
    """What is below par, grouped by colorway — the page beside the sheet.

    **One source of truth with the sheet, and this page did not have it.** It
    used to run its own SQL — `par - number_on_hand`, aggregated per recipe —
    which knew nothing about paper already printed. So a sheet covering a
    colorway's entire shortage left this page still reporting it in full, and
    the two disagreed about the one question they both answer. The planner was
    right and the page a person reads was wrong, which is the worse way round.

    Now both read `production.candidates()`. Same filters, same in-flight
    subtraction, same order — so "the first twenty on this list" and "ask the
    picker for twenty baths" are the same twenty, which is what the ordering
    was already trying to guarantee and could not while the memberships
    differed.

    `include_overshoot=True` and `oven=None`, because this **reports** where
    the sheet **plans**: everything below par is worth reading, whichever box
    it is made in and whether or not a whole bath would overshoot. The
    behind-a-bath rows are badged rather than filtered, and so are the oven
    ones.
    """
    category_id = request.GET.get("category")
    category = None
    if category_id and str(category_id).isdigit():
        category = RawProductCategory.objects.filter(pk=category_id).first()

    # What each colorway has actually sold this season, pooled across its
    # blanks — the same figure `private/slow-sellers/` reports, from the same
    # function, because two answers to "what sold" is how the page that orders
    # by it comes to disagree with the page that reports it.
    rng = slowsellers.season_range({})
    sold = slowsellers.sold_by_recipe(rng)

    # `?demand_par=1` judges every product against twice a day's sales plus
    # one instead of its stored par — the same tick the sheet form has, under
    # the same name, so a link off one page means the same thing on the
    # other. The stored par is not written; untick and it is the old page.
    demand = production.demand_par() if request.GET.get("demand_par") else None

    products = production.candidates(
        category=category,
        include_overshoot=True,
        order=production.ORDER_SOLD,
        oven=None,
        demand_par=demand,
    )

    by_recipe = {}
    for fp in products:
        by_recipe.setdefault(fp.recipe_id, []).append(fp)

    groups = []
    for rid, fps in by_recipe.items():
        # Within a colorway: emptiest shelf first, then biggest gap, then a
        # stable name — `_urgency` without re-deriving it here.
        fps.sort(key=lambda p: (p.number_on_hand > 0, -p.net_shortage, p.name))
        groups.append({
            "recipe_id": rid,
            "recipe_name": fps[0].recipe.name,
            "recipe_obj": fps[0].recipe,
            # A count rather than a flag, because the answer is per blank now:
            # a colour can be oven work on its yarns and microwave on its silk,
            # and a bare badge would say the whole group belongs on an oven
            # sheet when only some of it does. The badge prints the share.
            "oven_count": sum(1 for p in fps if p.oven_dyed),
            # Asked of what is left *after* the paper, like the shortage
            # beside it. The model property answers about the shelf alone and
            # would light up rows a printed sheet already covers.
            "has_behind": any(p.behind_a_bath_net for p in fps),
            "total_shortage": sum(p.net_shortage for p in fps),
            # Printed where it is non-zero, so a number that moved because
            # somebody printed a sheet says so rather than just dropping. A
            # silent subtraction reads as "nothing needed", which is the same
            # confusion in the other direction.
            "in_flight": sum(p.in_flight for p in fps),
            "units_sold": sold.get(rid, 0),
            "items": fps,
        })

    # **Sold, most first, is the default — because par is the number that is
    # wrong.** Par was never dialled in and reads as a uniform remnant, so
    # ordering by shortage ranks this list on a number nobody chose: a
    # colorway that sold three all season outranks one that sold forty, purely
    # because it crossed an arbitrary line first. Sales are measured. Until
    # par means something, they are the better claim on a dye pot.
    #
    # The old ordering stays one click away, and neither is a filter — every
    # group is listed either way, so the sort changes what is read first and
    # never what exists.
    sort = "shortage" if request.GET.get("sort") == "shortage" else "sold"
    if sort == "sold":
        groups.sort(key=lambda g: (-g["units_sold"], -g["total_shortage"],
                                   g["recipe_name"]))
    else:
        groups.sort(key=lambda g: (-g["has_behind"], -g["total_shortage"],
                                   g["recipe_name"]))

    context = {
        "groups": groups,
        "categories": RawProductCategory.objects.all().order_by("name"),
        "selected_category_id": category.pk if category else None,
        "sort": sort,
        "range": rng,
        # The object rather than a flag, so the page can say what the number
        # it is showing was divided by — advice you cannot inspect is a
        # decision in disguise, and this one is a formula.
        "demand_par": demand,
        # So the page can say it is netting off printed sheets rather than
        # leaving somebody to wonder why a colorway went quiet.
        "in_flight_total": sum(g["in_flight"] for g in groups),
    }
    return render(request, "scarves/production_needed.html", context)


@require_POST
@login_required
def record_dye_bath(request, pk):
    """*Bagged a bath* on `private/production-needed/`: units straight into stock.

    Through `production.report`, which honours a sheet's claim first: if this
    product has baths open on a sheet, those rows are what gets accepted, and
    only anything beyond them is booked as an unplanned bath. Before that,
    pressing this for a bath that was on a sheet took the blanks off the
    shelf a second time and left the sheet row open.
    """
    finished_product = get_object_or_404(FinishedProduct, pk=pk, is_active=True)
    raw_product = finished_product.raw_product

    qty_str = (request.POST.get("qty") or "").strip()
    qty = int(qty_str) if qty_str.isdigit() else finished_product.bath_size

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"

    outcome = production.report(
        finished_product, qty,
        source=InventoryLog.SOURCE_PRODUCTION_NEEDED,
        notes="Dye bath recorded from production-needed page.",
    )

    # If HTMX request, return the updated row HTML (no redirect)
    if _is_htmx(request):
        finished_product.refresh_from_db()
        finished_product = (
            FinishedProduct.objects
            .select_related("raw_product", "raw_product__category", "recipe")
            .get(pk=finished_product.pk)
        )
        # The row reads `net_shortage`, `behind_a_bath_net`, `stockout_bonus`
        # and `sold_here`, none of which the model carries — and a missing
        # attribute renders as an empty cell rather than raising, so
        # forgetting one is silent. `annotate_flight` sets the first three;
        # `sold_here` is attached by `candidates()` on the page path and has
        # to be set by hand here, since this row never went through it.
        # The row carries the page's par mode as a hidden input, so a bath
        # bagged on a "par from sales" page comes back judged the same way.
        demand = (
            production.demand_par() if request.POST.get("demand_par") else None
        )
        production.annotate_flight([finished_product], demand_par=demand)
        finished_product.sold_here = production.sold_per_blank().get(
            finished_product.pk, 0
        )
        return TemplateResponse(
            request,
            "scarves/partials/production_needed_row.html",
            {"fp": finished_product, "demand_par": demand},
        )

    # Normal browser POST: message + redirect
    raw_product.refresh_from_db(fields=["number_on_hand"])
    messages.success(
        request,
        f"Recorded dye bath for '{finished_product.name}': "
        f"+{qty} finished (now {finished_product.number_on_hand})."
        + (
            f" -{outcome.direct} raw '{raw_product.name}' (now {raw_product.number_on_hand})."
            if outcome.direct else ""
        )
        + _sheets_ticked(outcome),
    )
    return redirect(next_url)


def _sheets_ticked(outcome):
    """The half of a production message that says what a sheet already claimed."""
    if not outcome.rows:
        return ""
    names = ", ".join(str(run) for run in outcome.sheets)
    n = len(outcome.rows)
    return (
        f" {n} bath{'' if n == 1 else 's'} of that {'was' if n == 1 else 'were'} "
        f"on sheet {names} and {'has' if n == 1 else 'have'} been ticked off there."
    )


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


def sheet_list(form):
    """`([(product, baths)], skipped)` — the editable list, however it was seeded.

    One list with two ways to fill it. `items` wins when present, so a
    suggestion seeds the page and every edit after that is the list speaking
    for itself. An old `?baths=20` link still resolves, and now comes back
    editable rather than as something to look at.

    `skipped` is only ever from the suggestion. **A pick is somebody
    deciding**, so a hand-added colorway is never taken off for needing a dye
    she said she was out of — the row is flagged and the ✕ is right there.
    That is the same call the short-blank warning makes: said, not enforced.
    """
    picked = form.cleaned_data.get("items")
    if picked:
        return picked, []
    if not form.cleaned_data.get("baths"):
        return [], []

    plan = production.suggest(
        form.cleaned_data["baths"],
        category=form.cleaned_data.get("category"),
        include_overshoot=form.cleaned_data["include_overshoot"],
        order=form.cleaned_data.get("order") or production.ORDER_SOLD,
        # The pot and the oven suggest from disjoint sets, and the form knows
        # which one it is because the route told it.
        oven=form.is_oven_run,
        # What she has just said she cannot dye today. Read off the same form
        # as everything else, so it rides in the URL and the sheet stays a
        # link somebody can send.
        without_blanks=form.cleaned_data.get("without_blanks"),
        without_dyes=form.cleaned_data.get("without_dyes"),
        # Judged against twice a day's sales plus one rather than the stored
        # par, when ticked. Only the suggestion reads it: once `items` exists
        # the list is the list, and a pick is somebody deciding.
        demand_par=(
            production.demand_par()
            if form.cleaned_data.get("demand_par") else None
        ),
    )
    # Back to one row per colorway. `suggest` returns a bath at a time
    # because that is what the paper prints; the list is edited per colorway,
    # because "three of that one" is how somebody says it.
    counts, order = {}, []
    for bath in plan.baths:
        if bath.product.pk not in counts:
            order.append(bath.product)
        counts[bath.product.pk] = counts.get(bath.product.pk, 0) + 1
    return [(product, counts[product.pk]) for product in order], plan.skipped


def _without(rows, product, oven=False, out_blanks=(), out_dyes=()):
    """`?items=` for the list minus one row, for that row's remove link.

    Server-rendered rather than built in the browser, so removing a row is an
    ordinary link that works with the script blocked — and the address it
    produces is the same sendable URL every other filter here uses.

    **It has to carry `oven`.** The link is a whole new address rather than an
    edit to the form, so anything not in it is dropped — and dropping this one
    turns an oven run back into a dye-room sheet halfway through editing it,
    with the only visible sign being the tray gauge disappearing.
    """
    params = {"items": [f"{p.pk}:{n}" for p, n in rows if p.pk != product.pk]}
    if oven:
        params["oven"] = "1"
    # Same trap as `oven`, one field along: these ticks are not in the list
    # form, so a link that drops them comes back with the panel cleared and
    # the next "Suggest baths" quietly offering the colorways she just said
    # she had no yarn for.
    if out_blanks:
        params["without_blanks"] = [str(raw.pk) for raw in out_blanks]
    if out_dyes:
        params["without_dyes"] = [str(dye.pk) for dye in out_dyes]
    return urlencode(params, doseq=True)


def _tick_groups(things, ticked, group):
    """`[{"name": ..., "items": [{"thing", "checked"}]}]` for a tick panel.

    The two "haven't got" lists are the same shape and want the same
    grouping — headings that match how the shelf is actually laid out, so
    somebody scanning for a blank or a dye is scanning the order they
    already know. `things` arrives pre-sorted; this only breaks it into runs.
    """
    groups = []
    for thing in things:
        name = group(thing)
        if not groups or groups[-1]["name"] != name:
            groups.append({"name": name, "items": []})
        groups[-1]["items"].append(
            {"thing": thing, "checked": thing.pk in ticked}
        )
    return groups


# ---------------------------------------------------------------------------
# From a Sunday close to a production list.
#
# The shop's own loop, which the app spent a long time not modelling: the crew
# walk the display on Sunday night and end the evening holding a stack of
# kanban cards, and that stack is the week's work order. `closeplan.py` has
# the whole argument, including why par and the close can both propose without
# competing — one claim, matched on finished product, whoever wrote it.
# ---------------------------------------------------------------------------


@page_meta(
    title="Production From a Close",
    description="Turn Sunday night's stack of kanban cards into a list of "
                "baths to dye. Empty pegs and last-one-hanging first, then "
                "best sellers. Cards already on a list drop off, so several "
                "lists off one close can't plan the same thing twice.",
    category="Production",
    note="Plans from the latest close. Optional ?close=<id> for an older one.",
)
@login_required
@require_http_methods(["GET", "POST"])
def production_from_close(request):
    """Her page, and the one the whole close was secretly for.

    **Preview by GET, create by POST**, the same bargain the sheet picker
    makes: browsing leaves nothing behind, and the moment a list exists so
    does the claim on every card in it.

    **Which close is query-string state** (`?close=`), defaulting to the
    latest, so there is one route and no picker to invent — and an older
    close is still plannable, because a card that never got made does not
    stop being a card on Monday.

    The choice of paper or no paper is asked *here*, once, and stored on the
    run. That is the one thing about a list that cannot be both, and asking
    it at creation is what keeps the run page from offering two doors every
    time it is opened.
    """
    close = None
    asked = (request.GET.get("close") or request.POST.get("close") or "").strip()
    if asked.isdigit():
        close = CloseRun.objects.filter(pk=int(asked)).first()
    if close is None:
        close = closeplan.latest_close()

    if close is None:
        return render(request, "scarves/production_from_close.html", {
            "close": None,
            "closes": [],
        })

    stack = closeplan.cards(close)
    pool, listed = closeplan.partition(stack)

    if request.method == "POST":
        picks, problems = closeplan.parse_picks(request.POST, pool)
        for problem in problems:
            messages.error(request, problem)
        if problems:
            # Nothing recorded, and said out loud — a list that came back one
            # row short with no explanation is worse than one refused.
            messages.info(request, "Nothing was made into a list — fix those.")
        elif not picks:
            messages.info(
                request,
                "No baths entered, so no list was made. Put a number beside "
                "the cards you are going to dye.",
            )
        else:
            reporting = (
                ProductionRun.DIRECT
                if request.POST.get("reporting") == ProductionRun.DIRECT
                else ProductionRun.PAPER
            )
            run = closeplan.make_list(close, picks, reporting=reporting)
            baths = run.rows.count()
            messages.success(
                request,
                f"List #{run.pk}: {baths} bath{'' if baths == 1 else 's'} "
                f"across {len(picks)} "
                f"colorway{'' if len(picks) == 1 else 's'}. "
                + (
                    "Print it when you are ready."
                    if run.is_on_paper else
                    "Say what you made on this page when the week is done."
                ),
            )
            return redirect("production_run_detail", pk=run.pk)

        return redirect(f"{reverse('production_from_close')}?close={close.pk}")

    return render(request, "scarves/production_from_close.html", {
        "close": close,
        # Every close, so an older one is one click away. Short list by
        # nature — one per weekend — so it needs no paging and no picker.
        "closes": CloseRun.objects.order_by("-day")[:12],
        "pool": pool,
        "listed": listed,
        "critical_count": sum(1 for card in pool if card.is_critical),
        "lists": closeplan.lists_for(close),
        "max_baths": closeplan.MAX_BATHS_PER_CARD,
        "still_to_count": close.rows.filter(
            outcome=CloseRunRow.PENDING
        ).count(),
        "paper": ProductionRun.PAPER,
        "direct": ProductionRun.DIRECT,
    })


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

    **The oven is a tick on this page, not a page of its own.** It is a
    different session — only oven colorways, planned to the box rather than
    to the work — but it is the same job end to end: the same editable list,
    the same collection page, the same three printed documents, the same QR
    coming back. A second picker would be a second thing to find and a second
    copy to keep in step.

    What that costs is that the tick has to survive every round trip, where a
    route carried it for free. Every edit here re-renders from the server, so
    `oven` rides as a hidden input inside `#sheet-list` — the same form that
    carries `items` — and the ✕ links carry it in their query string. Miss one
    and the sheet quietly turns back into a dye-room sheet mid-edit, which is
    the one way this arrangement can go wrong.
    """
    sheet_url = reverse("production_sheet_index")

    if request.method == "POST":
        form = ProductionSheetForm(request.POST)
        if form.is_valid():
            rows, _skipped = sheet_list(form)
            baths = production.baths_from_picks(rows)
            if not baths:
                messages.warning(request, "Nothing needs dyeing for those settings.")
                return redirect(f"{sheet_url}?{request.POST.urlencode()}")

            with transaction.atomic():
                run = ProductionRun.objects.create(
                    category=form.cleaned_data.get("category"),
                    included_overshoot=form.cleaned_data["include_overshoot"],
                    # Frozen onto the run for the reason the category and the
                    # bath sizes are: a reprint has to say what the paper
                    # said, and it is what the run page reads to decide what
                    # may be added to this sheet later.
                    oven=form.is_oven_run,
                )
                # Through `open_rows` rather than straight to `bulk_create`,
                # because creating the run is what claims its yarn — see the
                # note there. A sheet planned on Monday has to have moved the
                # shelf before the next list is planned against it on Tuesday.
                production.open_rows(
                    run, [(bath.product, bath.quantity) for bath in baths]
                )
                # Nothing is retired here any more. Printing a sixth sheet
                # used to close the oldest, which quietly decided that a
                # session nobody had answered for never happened — and the
                # baths on it stopped being asked for at the same moment,
                # with nothing said. Old sheets now age out of the plan on
                # their own and get named on the picker instead.

            return redirect("production_run_detail", pk=run.pk)
    else:
        form = ProductionSheetForm(request.GET or None)

    # Read off the form so it survives whichever way the page was reached,
    # and before validity is known — the search results and the count box's
    # label both need it.
    oven = form.is_oven_run

    rows, skipped = [], []
    out_blanks, out_dyes = [], []
    if form.is_bound and form.is_valid():
        rows, skipped = sheet_list(form)
        # Lists rather than querysets: they are walked several times below
        # (the ticks, the row flags, the ✕ links) and re-evaluating the same
        # two selects on every pass is a query per row for nothing.
        out_blanks = list(form.cleaned_data["without_blanks"])
        out_dyes = list(form.cleaned_data["without_dyes"])
    baths = production.baths_from_picks(rows)

    # The search is a plain GET form with htmx layered on, so with the script
    # blocked `q` lands in the URL and the results render inline from the very
    # partial the fragment endpoint returns — the same call the run page and
    # the close's tag search make.
    q = (request.GET.get("q") or "").strip()

    # An htmx edit gets the fragment and nothing else — a few hundred bytes
    # of table rather than a whole page the browser would throw most of away.
    # Same context either way, and the page includes the same partial, so
    # there is one renderer and a swapped view cannot disagree with a
    # refreshed one.
    template = (
        "scarves/partials/sheet_plan.html"
        if _is_htmx(request)
        else "scarves/production_sheet_index.html"
    )

    # One tray is one bath, so the box's capacity is a row count and there is
    # no tray arithmetic anywhere. The gap is what the page is *for* on an
    # oven run: a heating costs the same whether it comes out full or not.
    #
    # **Only the oven baths take tray space.** A session is allowed to be an
    # oven run *and* two pots on the side — forcing that onto two sheets is
    # overhead for overhead — so the sheet holds both and the gauge counts
    # what actually goes in the box. Counting every row instead would read
    # "17 of 15" for fifteen trays plus two pots, which is the page telling
    # somebody to take out work the oven was never holding.
    oven_baths = [b for b in baths if b.product.oven_dyed]
    pot_baths = len(baths) - len(oven_baths)
    tray_gap = production.OVEN_TRAYS - len(oven_baths) if oven else 0
    # Built here rather than counted in the template, which cannot range.
    # `True` is a tray with something in it; the tail is what would go in
    # empty. An over-full sheet has no empty tail and says so in words.
    tray_slots = (
        [True] * len(oven_baths) + [False] * max(tray_gap, 0) if oven else []
    )

    # One lookup for the whole list rather than one per row, and the same
    # function `candidates()` uses so the picker and the page that feeds it
    # cannot report different sales for the same blank.
    per_blank = production.sold_per_blank()

    # **Both tick lists are the whole shelf, and neither one knows what has
    # been suggested.** They are fixed catalogues grouped the way the
    # physical shelves are — blanks by category, dyes by brand — so the panel
    # is the same on the way in as it is after twenty baths have been
    # planned. That is the point: "out of J purple" is something she knows
    # standing in the dye room, and a list that only filled itself in once a
    # suggestion existed would ask her to plan before she could say what she
    # hadn't got. It also makes the tick stable — the boxes do not move
    # around underneath her as the list re-plans, and a tick can always be
    # undone in the place she made it.
    #
    # Neither list is the full table. A passthrough is never dyed and a dye
    # no live recipe calls for can never block a bath, so both are left out
    # by the form's querysets: a box that can only do nothing is worse than
    # no box, because ticking it looks like saying something.
    ticked_blanks = {raw.pk for raw in out_blanks}
    ticked_dyes = {dye.pk for dye in out_dyes}
    blank_groups = _tick_groups(
        form.fields["without_blanks"].queryset,
        ticked_blanks,
        group=lambda raw: raw.category.name if raw.category_id else "Uncategorised",
    )
    # Sorted in Python because `sort_name` is a property — it drops the
    # catalog number off the front, so `425 Amethyst` files under A where
    # somebody looking for a purple will actually look for it.
    dye_groups = _tick_groups(
        sorted(form.fields["without_dyes"].queryset,
               key=lambda dye: (dye.brand.name, dye.sort_name)),
        ticked_dyes,
        group=lambda dye: dye.brand.name,
    )

    # Flags for the picked half. One query for the lot rather than walking
    # `recipe_dyes` per row, which the picked list does not prefetch.
    blocked_dyes = {}
    if out_dyes and rows:
        for link in RecipeDye.objects.filter(
            recipe_id__in={product.recipe_id for product, _ in rows
                           if product.recipe_id},
            dye_id__in=[dye.pk for dye in out_dyes],
        ).select_related("dye"):
            blocked_dyes.setdefault(link.recipe_id, []).append(link.dye)

    def row_flags(product):
        """Why this picked row can't be dyed today — said, never acted on."""
        flags = []
        if product.raw_product_id in ticked_blanks:
            flags.append(f"no {product.raw_product.name}")
        flags += [f"out of {dye.name}"
                  for dye in blocked_dyes.get(product.recipe_id, [])]
        return flags

    return render(request, template, {
        "form": form,
        "oven": oven,
        # So the suggestion can say which par it was measured against.
        "demand_par": (
            production.demand_par()
            if form.is_bound and form.data.get("demand_par") else None
        ),
        "sheet_url": sheet_url,
        "oven_trays": production.OVEN_TRAYS,
        "tray_gap": tray_gap,
        "tray_slots": tray_slots,
        # So the count box's own limit cannot drift from the one that
        # validates it — a client cap of 20 over a server cap of 10 is a
        # number somebody can type and then be refused for.
        "max_per_item": PickedBathsField.MAX_PER_ITEM,
        "oven_bath_count": len(oven_baths),
        "pot_bath_count": pot_baths,
        # Offered, never added. The panel is empty once the box is full, so
        # it only ever answers a question the page is already asking — and
        # each suggestion prints what it sold, because the ranking has to be
        # checkable by looking rather than trusted.
        "top_ups": (
            production.top_ups(
                [product for product, _ in rows],
                tray_gap,
                category=form.cleaned_data.get("category") if form.is_bound and form.is_valid() else None,
                # Filtered here rather than flagged: a top-up is a free
                # choice among colorways that are not short, so one that
                # cannot be dyed today is not a choice at all.
                without_blanks=out_blanks,
                without_dyes=out_dyes,
            )
            if oven else []
        ),
        "baths": baths,
        "plan": production.dye_plan_for_baths(baths),
        # The two tick lists and what they have already ruled out.
        "blank_groups": blank_groups,
        "dye_groups": dye_groups,
        "out_blanks": out_blanks,
        "out_dyes": out_dyes,
        "excluded_any": bool(out_blanks or out_dyes),
        # Named, not just subtracted. A sheet that came back shorter with no
        # word about why is a filter working invisibly, which is the failure
        # the override was built to avoid in the first place.
        "skipped": skipped,
        "bath_count": len(baths),
        # What this session is worth before it starts — the same two figures a
        # statement reports when it closes, so the size of the work is
        # readable at the point somebody decides to do it. Derived here,
        # frozen there: nothing has happened yet, and it is deliberately
        # blind to yield.
        "estimate": dyebill.estimate(baths),
        # Only a form that actually asked a question gets an answer below.
        # Keyed on validity rather than "was anything submitted", or a typo in
        # the bath count reads back as "nothing needs dyeing" — which is a
        # different and much more alarming statement.
        "submitted": form.is_bound and form.is_valid() and form.asked_anything,
        "short_blanks": production.short_blanks(baths),
        # One list, with a remove link per row. Paired here rather than in the
        # template because "the list without this row" is a query-string
        # question, and the template has no business assembling one.
        "rows": [
            {
                "product": product,
                "baths": n,
                # What this blank sold, beside what it holds. The suggestion
                # is ranked on the *colorway's* pooled sales, which is the
                # right unit for choosing a pot and is blind to a blank that
                # is already full — so the two numbers ride on the row and a
                # person strikes it. Nothing here filters on them.
                "sold_here": per_blank.get(product.pk, 0),
                # What those baths actually make. Worked out here rather than
                # left to a template filter: baths are the unit of work and
                # scarves are the unit everybody thinks in, and the page has
                # to show both without anybody multiplying.
                "makes": product.bath_size * n,
                # A pick is somebody deciding, so this is a flag and not a
                # filter — the row stays on the sheet and says what is
                # missing.
                "blocked_by": row_flags(product),
                "without": _without(rows, product, oven, out_blanks, out_dyes),
            }
            for product, n in rows
        ],
        "asked": form.is_bound and form.asked_anything,
        "q": q,
        "search_results": search_products(q) if q else None,
        # Two lists, because they ask for different things. Live sheets are a
        # convenience — "what you might still be working from" — and are
        # truncated, since a long one is just noise.
        # Filtered to this kind of session. A sheet you might still be
        # working from is the point of the list, and a dye-room sheet is not
        # something anybody is working from at the oven — mixing them makes
        # the list longer without making it more useful.
        "open_runs": (
            production.counted_runs()
            .filter(oven=oven)
            .prefetch_related("rows")[:production.RUNS_LISTED]
        ),
        # Overdue sheets are the actionable list and are never truncated.
        # These have stopped claiming their baths, so the colorways on them
        # are being asked for again — which is fine if the paper is lost and
        # wrong if the session is still going. Either way somebody has to
        # say which, and the only way that happens is if the page says so.
        "overdue_runs": (
            production.overdue_runs().filter(oven=oven).prefetch_related("rows")
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
        ProductionRun.objects.select_related("close_run").prefetch_related(
            "rows__finished_product__recipe",
            "rows__finished_product__raw_product",
            "rows__finished_product__recipe__recipe_dyes__dye__brand",
        ),
        pk=pk,
    )

    # The add box is a plain GET form, so with the script blocked `q` lands
    # in the URL and the results render inline from the very partial the
    # fragment endpoint returns. Two copies of that markup would drift, and
    # the way it would show is the swapped-in version posting somewhere the
    # inline one doesn't.
    q = (request.GET.get("q") or "").strip()

    # A struck row hands its tray back, so what fills the box is what is
    # still live on the sheet — the same claim `in_flight` makes about a
    # pending bath, asked of one run rather than of the planner. And only the
    # oven rows count: a sheet is allowed to carry an oven load plus a couple
    # of pots, and the pots were never in the box.
    live_rows = [row for row in run.rows.all() if not row.is_cancelled]
    live = sum(
        1 for row in live_rows
        if row.finished_product.oven_dyed
    )
    alongside = len(live_rows) - live

    return render(request, "scarves/production_run_detail.html", {
        "run": run,
        # Still built for both modes. A direct list has a token and a page
        # like any other — the crew page *is* the reporting flow, and the
        # only difference between the modes is whether anybody reaches it by
        # scanning paper. What `reporting` decides is which door the page
        # offers, not whether the other one exists: a list started without
        # paper that turns into a dye-room session still has to be printable,
        # and a printed one still has to be reportable at a desk.
        "crew_url": _crew_run_url(request, run),
        "plan": production.dye_plan_for_run(run),
        "bath_count": run.rows.count(),
        "oven_trays": production.OVEN_TRAYS,
        "live_trays": live,
        "alongside_baths": alongside,
        "tray_gap": production.OVEN_TRAYS - live if run.oven else 0,
        "q": q,
        "search_results": search_products(q) if q else None,
    })


@require_POST
@login_required
def production_run_add_row(request, pk):
    """Put another bath on a sheet.

    The planner picks what it can see — a shortage against par — and there
    are reasons to dye something it cannot: an order taken at the stall, a
    colour somebody wants to try, a bath being run anyway that has room
    beside it. A plan nobody can edit gets worked around on paper, and then
    the paper and the app disagree about what happened.

    Appended rather than slotted in, because `order` is the position on a
    printed sheet and renumbering the rest would make an existing printout
    disagree with the page about which row is which.
    """
    run = get_object_or_404(ProductionRun, pk=pk)
    product = get_object_or_404(
        FinishedProduct, pk=request.POST.get("product"), is_active=True
    )

    # **Two refusals, and the line between them is who decided.**
    #
    # An undyed passthrough is ordered rather than made, and a fancy veil is
    # line work on a scarf that already exists — neither is answerable by
    # heating anything, so those stay refused. They are facts about how the
    # thing comes into being.
    #
    # A mismatched oven flag is *not* that, and it used to be refused here.
    # `FinishedProduct.oven_dyed` is typed by a person, from a rule about the
    # world ("a colour name goes in the oven"), and it was made a flag precisely
    # because there will be exceptions nobody knows about today. Refusing on
    # it means the app enforcing somebody's own provisional data back at
    # them, at the moment they are trying to say the data is wrong — and the
    # cost of being wrong here is a row on a sheet, which is strikeable. So
    # it is said and allowed, like the short-blank warning and the tray gap.
    if product.recipe is None:
        why = "it is undyed — it gets ordered, not made."
    elif not product.raw_product.made_in_a_dye_bath:
        why = "it isn't made in a dye bath."
    else:
        why = None

    if why:
        messages.warning(request, f"{product.name} can't go on this sheet — {why}")
        return redirect("production_run_detail", pk=run.pk)

    mismatch = (
        product.oven_dyed != run.oven
        and (
            "That one is made in the oven and this is a microwave sheet"
            if product.oven_dyed else
            "That one is made in the microwave and this is an oven run"
        )
    )

    # A bath is a fixed size, so `open_row` freezing `bath_size` onto the row
    # is the only honest quantity — the same number the planner would have.
    # It claims the blanks too: a bath added by hand is as much an intent to
    # make something as one the planner proposed.
    row = production.open_row(run, product)
    added = (
        f"Added {row.quantity} × {product.name} to run {run.pk}. "
        f"Reprint the sheet, or write it on the bottom."
    )
    if mismatch:
        # Said rather than refused — and said loudly, because the likely cause
        # is a flag that needs changing rather than a row that needs striking.
        messages.warning(request, f"{added} {mismatch} — added anyway.")
    else:
        messages.success(request, added)
    return redirect("production_run_detail", pk=run.pk)


@require_POST
@login_required
def production_run_add_bath(request, pk, row_id):
    """One more bath of a colorway already on this list.

    **"If I made more, let me say so."** The list said one bath and the pot
    ran twice — an ordinary thing, and the only way to say it used to be the
    catalogue search, which is the right tool for a colour that was never on
    the list and a poor one for a row already on screen with its name on it.

    Takes a row rather than a product, because that is what the button is
    next to; the bath goes on the end of the list, as its own row, for the
    reason every row is one bath — see `closeplan.add_bath`.
    """
    run = get_object_or_404(ProductionRun, pk=pk)
    row = get_object_or_404(run.rows.select_related("finished_product"), pk=row_id)

    added = closeplan.add_bath(run, row.finished_product)
    messages.success(
        request,
        f"Another bath of {row.finished_product.name} — "
        f"{added.quantity} more, on the end of the list.",
    )
    return redirect("production_run_detail", pk=run.pk)


@require_POST
@login_required
def production_run_strike_row(request, pk, row_id):
    """Take one bath off a sheet — it isn't going to happen.

    Cancelling rather than deleting, for the reason the whole model changed:
    the run is a record now. A row that was on the paper and then called off
    is a thing that happened to the plan, and deleting it would leave a sheet
    in somebody's hand with a line on it the app has never heard of.
    """
    run = get_object_or_404(ProductionRun, pk=pk)
    row = get_object_or_404(run.rows, pk=row_id)

    if production.cancel_row(row):
        messages.success(
            request,
            f"Took {row.quantity} × {row.finished_product.name} off run "
            f"{run.pk}. Nothing moved, and it goes back on the next sheet.",
        )
    else:
        # Already accepted, so stock has moved and this is an adjustment with
        # a reason attached rather than an edit to a plan.
        messages.warning(
            request,
            f"That bath is already in stock — correcting it is an inventory "
            f"adjustment, not an edit to the sheet.",
        )
    return redirect("production_run_detail", pk=run.pk)


@require_POST
@login_required
def production_run_cancel_remaining(request, pk):
    """Call off every bath on this sheet that nobody has answered for.

    Retiring a sheet *is* this — there is no run-level retired flag, so
    "closed" always means "no row is still pending" and cannot disagree with
    the rows underneath it.

    **There is no accept-all, and there is not going to be one.** The two
    directions look symmetrical and are not:

    - Cancelling moves nothing into inventory. It gives up a claim, so the
      colorways come straight back onto the next sheet and the worst case is
      that somebody is asked about them again.
    - Accepting puts stock on the books. A sheet accepted wholesale asserts
      that twenty baths came out at full yield, which is a claim about twenty
      physical piles of scarves that nobody looked at — and every one of them
      is then wrong on the pegs, at the close, and in Square.

    So accepting has to cost a mark per row. That is the same bargain the
    restock board makes by refusing a "check all" button: the cost *is* the
    evidence, and it is precisely the convenience somebody reasonable will
    ask for after the third long session. The answer is no, and this comment
    is here so the reasoning does not have to be reconstructed.
    """
    run = get_object_or_404(ProductionRun, pk=pk)

    cancelled = 0
    with transaction.atomic():
        # Read the rows fresh inside the transaction: `cancel_row` refuses a
        # row that has already moved stock, and it can only see that on an
        # object that has not gone stale.
        for row in run.rows.select_for_update():
            if production.cancel_row(row):
                cancelled += 1

    if cancelled:
        messages.success(
            request,
            f"Called off {cancelled} bath{'' if cancelled == 1 else 's'} on "
            f"run {run.pk}. Nothing moved, and those colorways go back on "
            f"the next sheet.",
        )
    else:
        messages.info(request, f"Nothing left to call off on run {run.pk}.")
    return redirect("production_run_detail", pk=run.pk)


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
    pdf = production.render_sheet(
        run,
        _crew_run_url(request, run),
        # Absolute, and built off this request rather than a setting, for the
        # same reason the run's URL is: whatever host the sheet was printed
        # from is the host the phone scanning it can reach.
        request.build_absolute_uri(reverse("production_upload")),
    )
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
        # Everything with a bath still to settle, overdue included: a sheet
        # the office has stopped counting on is exactly the one somebody is
        # standing there holding, and leaving it off this list would mean the
        # session that finally came back had nowhere to report to.
        "runs": production.open_runs().prefetch_related("rows")[:20],
    })


def _row_named(run, value):
    """One of this run's rows, by pk, or `None`.

    Scoped to the run and parsed defensively: the id arrives from a button on
    a page with no login, so an unreadable one has to degrade to doing
    nothing rather than raising — and one belonging to another sheet must not
    resolve at all.
    """
    if not (value or "").isdigit():
        return None
    return run.rows.filter(pk=int(value)).first()


def _line_named(run, value):
    """One of this run's lines, by its tick key, or `None`.

    Same defensiveness as `_row_named` and for the same reason — these
    buttons are on a page reachable with a code off a piece of paper.
    """
    if not (value or "").isdigit():
        return None
    key = int(value)
    return next(
        (line for line in production.lines_for_run(run) if line.key == key), None
    )


def _note_reporter(request, run):
    """Stamp who replied and when, without deciding anything.

    A cancel is a reply too — somebody picked the phone up and said the rest
    isn't coming. `submitted_at` is a record of that and nothing more; what
    is open, overdue or finished is read off the rows.
    """
    if run.submitted_at is None:
        run.submitted_at = timezone.now()
    employee, _pin = crew.remembered(request)
    if employee is not None and run.submitted_by_id is None:
        run.submitted_by = employee


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
        if row.pk in wanted and not row.is_accepted
    }

    def number(name):
        value = request.GET.get(name, "")
        return int(value) if value.isdigit() else 0

    return {
        "read": number("read"),
        "filled": number("filled"),
        "unsure": number("unsure"),
        "strays": number("strays"),
        # Every row on the sheet, because the sheet prints every row. It
        # renders what the run asked for and reads no state back, so this is
        # what a photograph of it could have contained.
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
        upload = request.FILES["sheet"]
        data = upload.read()
        scan = sheetscan.read_sheet(data)
        _log_sheet_photo(upload, data, scan)
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


def _log_sheet_photo(upload, data, scan):
    """Log the photograph. **This is logging, and every property follows.**

    Naming it right settles the design questions in one go, which is why it is
    worth saying plainly: it is not a feature, not a record, and not state.
    A scan is optics — focus, curl, glare, the angle a page was lying at — and
    "it didn't work" with the evidence discarded is a bug report nobody can act
    on. So the input gets logged, exactly like a request body would be.

    Everything else is what logging is:

    * **Nothing points at it.** No model, no row, no admin. A pointer would be
      application state describing a diagnostic, and it would go stale the
      moment the bucket's lifecycle rule deleted the object under it.
    * **The sink owns retention**, not the app — `set_bucket_lifecycle` puts
      the week on the bucket. A log that has to be rotated by the program that
      writes it stops being rotated the first time that program stops running.
    * **It has a level**, which is `KEEP_SHEET_PHOTOS`, and turning it off
      changes nothing else because nothing reads it back.
    * **It never affects the request.** A bucket having a bad afternoon must
      not cost somebody the reading of a sheet they are standing there
      holding, so the failure is swallowed and logged.
    * **The line and the object carry the same summary**, so the ordinary log
      is usually enough on its own and names the object to fetch when it
      isn't. A log line pointing at a blob you have to open to learn anything
      is half a log.

    The key is `sheetscan.photo_key` and holds what a row would have.
    """
    if not getattr(settings, "KEEP_SHEET_PHOTOS", False):
        return
    summary = (
        f"width={scan.width} rows={len(scan.marks)} filled={len(scan.filled)} "
        f"unsure={len(scan.unsure)} token={scan.qr_token or '-'}"
        + (f" error={scan.error}" if scan.error else "")
    )
    try:
        name = upload.name or ""
        suffix = name[name.rfind("."):] if "." in name[-6:] else ".jpg"
        key = default_storage.save(
            sheetscan.photo_key(scan, timezone.now(), suffix), ContentFile(data)
        )
        logger.info("sheet photo %s  %s", key, summary)
    except Exception:
        # Still says what was read. The picture is the better evidence and the
        # summary is the evidence that survives the sink being unreachable —
        # which is the moment somebody most wants to know what happened.
        logger.exception("could not keep the sheet photo  %s", summary)


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
    if run is None or run.is_revoked:
        # Named either way, because "that sheet's code was revoked" and "that
        # is not a code I know" both end the same way for somebody standing
        # there with a photo — and neither should read as the upload silently
        # doing nothing.
        held["unknown_run"] = token[:40]
        held["revoked"] = bool(run is not None and run.is_revoked)
        request.session["production_photo"] = held
        return None

    filled = set(held.get("codes") or [])
    lines = production.lines_for_run(run)
    codes = {production.line_code(line) for line in lines}
    ticked = [
        line.key for line in lines
        if production.line_code(line) in filled and not line.is_accepted
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
        "strays": len(filled - codes),
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

    # **Revoked says so, rather than 404ing.** The code on the paper is right
    # and the door is shut, which is a different thing from a code that was
    # mistyped — and only one of them is worth trying again. A sheet reading
    # "no such run" sends somebody to squint at `18-tranquil-bobcat` for a
    # character they got wrong, which is a hunt with nothing at the end of it.
    #
    # It confirms to anybody holding the code that the code was real. That is
    # the trade, and it is the right way round: the person most likely to be
    # holding it is the crew, and the door is shut either way.
    if run.is_revoked:
        return render(
            request,
            "scarves/production_revoked.html",
            {"run": run},
            status=410,
        )

    if request.method == "POST":
        # **Calling baths off is a separate submit from accepting them, and
        # never a side effect of one.** The crew reporting a session is the
        # only person who knows the rest isn't coming — a lost sheet, a
        # session that stopped — so they must be able to say it. But a
        # cancel button that also banked whatever happened to be ticked
        # would move stock somebody hadn't finished entering numbers for,
        # and that is the one direction this page cannot undo.
        #
        # So each cancel branch does only its own work. Ticks left on screen
        # are lost, which costs a re-tick; the alternative costs an
        # inventory adjustment nobody on this page can make.
        if "cancel" in request.POST:
            # **A line is called off whole.** The crew are answering about one
            # pile of scarves, so "not coming" means the colorway isn't
            # coming — releasing every open bath of it back to the planner.
            # Striking one bath of three is a planning decision and stays on
            # the staff run page, where the person making it can see the
            # sheet.
            line = _line_named(run, request.POST.get("cancel"))
            cancelled = 0
            if line is not None:
                with transaction.atomic():
                    for row in line.pending:
                        if production.cancel_row(row):
                            cancelled += 1
            _note_reporter(request, run)
            run.save(update_fields=["submitted_at", "submitted_by"])
            request.session["production_run_note"] = (
                {"cancelled": cancelled} if cancelled else {}
            )
            return redirect("production_run", token=run.token)

        if "uncancel" in request.POST:
            line = _line_named(run, request.POST.get("uncancel"))
            if line is not None:
                with transaction.atomic():
                    for row in line.rows:
                        production.uncancel_row(row)
            return redirect("production_run", token=run.token)

        if "cancel_rest" in request.POST:
            cancelled = 0
            with transaction.atomic():
                for row in run.rows.select_for_update():
                    if production.cancel_row(row):
                        cancelled += 1
                _note_reporter(request, run)
                run.save(update_fields=["submitted_at", "submitted_by"])
            request.session["production_run_note"] = {"cancelled": cancelled}
            return redirect("production_run", token=run.token)

        ticked = set(request.POST.getlist("done"))
        applied = units = lost = fancied = 0
        with transaction.atomic():
            # **One tick per colorway, not per bath.** Three baths of Artisan
            # Cabernet are one line of fifteen with one box; `accept_line`
            # spreads what came out across the open baths, whole pots first,
            # because ten of fifteen means one failed rather than three
            # coming up short together.
            for line in production.lines_for_run(run):
                if str(line.key) not in ticked or line.is_accepted:
                    continue
                # The tick is the claim and the number is its size. Blank or
                # unreadable means the whole line, which is what a tick on
                # its own has always meant — so a phone that never gets as
                # far as the number box still reports exactly what it used to.
                typed = (request.POST.get(f"yielded-{line.key}") or "").strip()
                yielded = int(typed) if typed.isdigit() else None
                # Only offered where the blank has a fancy counterpart, so an
                # absent value is the overwhelmingly common answer of none.
                typed_fancy = (request.POST.get(f"fancy-{line.key}") or "").strip()
                fancy_units = int(typed_fancy) if typed_fancy.isdigit() else 0
                production.accept_line(line, yielded=yielded, fancy=fancy_units)
                applied += 1
                units += line.yielded
                fancied += line.fancy_yield
                lost += line.loss

            _note_reporter(request, run)
            run.save(update_fields=["submitted_at", "submitted_by"])

        # `baths` is now a count of colorways accepted rather than of pots.
        # The page says "lines" for it, because a number captioned baths that
        # counts something else is the kind of quiet wrong this app spends
        # most of its comments on.
        request.session["production_run_applied"] = {
            "baths": applied, "units": units, "lost": lost, "fancy": fancied,
        }
        request.session.pop("production_photo", None)
        return redirect("production_run", token=run.token)

    applied = request.session.pop("production_run_applied", None)
    note = request.session.pop("production_run_note", None)
    scan, prefilled = _photo_reading(request, run)
    employee, _pin = crew.remembered(request)
    return render(request, "scarves/production_run.html", {
        "run": run,
        # Built here rather than walked in the template: the grouping is a
        # judgement about what one answer covers, and a template working it
        # out inline would be the second place the rule lived.
        "lines": production.lines_for_run(run),
        "just_applied": applied,
        "just_noted": note,
        "remembered": employee,
        "scan": scan,
        # Pre-ticked from the photo, if there was one and the sheet has
        # identified itself. Kept in the session so the upload can
        # post/redirect/get like everything else here — a refresh must not
        # re-send a phone photo over a stall's signal.
        "prefilled": prefilled,
    })


# ---------------------------------------------------------------------------
# Produced since: the receipt for everything the production flow writes.
#
# Every other page that reads these rows consumes them — the label sheet turns
# them into stickers, the raw shelf into a reorder date, the planner subtracts
# them from a shortage — and none of them showed the rows. What that produced
# was not a missing feature but an unused one: recording a dye bath wrote
# something nobody could read back and nobody could take back, so every click
# was a commitment with no receipt, and the safe move was not to click.
# ---------------------------------------------------------------------------


#: Windows the page offers as one click. Keyed on what somebody actually asks
#: — the last session, the last month, the season — rather than on round
#: numbers for their own sake.
PRODUCED_SINCE_PRESETS = (
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("Last year", 365),
)


@page_meta(
    title="Produced Since",
    description="Every dye bath the app has recorded since a date, grouped by "
                "day, with what it believes is on hand now. Take an entry "
                "back if it didn't happen — nothing is deleted, the "
                "correction is written beside it.",
    category="Production",
    note="Reads production entries only, not recounts. Optional ?since= and "
         "?category=.",
)
@login_required
def produced_since_view(request):
    """The record, and the one button that can change it.

    **Not a Report**, even though it is mostly a list: the pages in that
    category are the ones that cannot change the numbers they show you, and
    this one can. Filed under Production with the pages that write the rows
    it reads.

    Everything is in the query string, so the view somebody is looking at is
    a URL — which matters more here than usual, since the answer to "what
    does it think I made" is a thing she will want to send to somebody.
    """
    form = ProducedSinceForm(request.GET or None)
    # An unparseable `?since=` should not blank the page — fall back to the
    # default window and let the field show its own error.
    since = (
        form.cleaned_data.get("since")
        if form.is_valid()
        else timezone.localdate() - timedelta(days=ProducedSinceForm.DEFAULT_DAYS)
    )
    category = form.cleaned_data.get("category") if form.is_valid() else None

    today = timezone.localdate()
    presets = [
        {
            "label": label,
            "days": days,
            "url": "?" + urlencode(
                {
                    "since": (today - timedelta(days=days)).isoformat(),
                    **({"category": category.pk} if category else {}),
                }
            ),
            "is_current": since == today - timedelta(days=days),
        }
        for label, days in PRODUCED_SINCE_PRESETS
    ]

    return render(request, "scarves/produced_since.html", {
        "form": form,
        "record": producedsince.build(since, category=category),
        "since": since,
        "category": category,
        "presets": presets,
        # The sheet that consumes these rows, with the same cutoff already
        # filled in. The two answers have to agree, and the way somebody
        # checks that they do is by opening both.
        "labels_url": (
            f"{reverse('label_index')}?"
            + urlencode(
                {
                    "dataset": LabelRunForm.SINCE,
                    "since": since.isoformat(),
                    "start_at": 1,
                    **({"category": category.pk} if category else {}),
                }
            )
        ),
    })


@require_POST
@login_required
def produced_since_retract(request, log_id):
    """Say one entry didn't happen. POST only, so no `@page_meta`.

    The reasoning for the mechanism is in `producedsince.retract`. The
    reasoning for the *button* is the Sunday close's Undo, and it is not
    really about inventory: a mistake somebody cannot fix themselves is a
    mistake they have to go and confess, and that cost is exactly what gets
    one left unmentioned instead. An unreported wrong number does more damage
    than any number of corrections, so there is no confirmation step in front
    of this and nothing counts how often it is used.
    """
    log = get_object_or_404(
        InventoryLog.objects.select_related(
            "finished_product", "finished_product__raw_product", "raw_product"
        ),
        pk=log_id,
        log_type=InventoryLog.PRODUCTION,
    )
    reversal = producedsince.retract(log, note=request.POST.get("reason", ""))

    product = log.finished_product
    # The window comes back as a query string rather than a `next` URL, so
    # this can only ever land on its own page. Taking one entry back must not
    # reset the page to the default month and lose her place; it also must not
    # be a field that decides where a staff session gets sent.
    window = (request.POST.get("window") or "").lstrip("?")
    back = reverse("produced_since") + (f"?{window}" if window else "")

    if reversal is None:
        # Already taken back, which is what a second click usually is —
        # somebody not sure the first one landed. Says so rather than
        # pretending to act, since the row is gone from the list either way
        # and silence would read as the button being broken.
        messages.info(
            request,
            f"That entry for {product.name} was already taken back — "
            f"nothing changed.",
        )
        return redirect(back)

    # The flash is the only place a retraction is ever narrated, and it is
    # narrated to the person who just asked for it and then gone. Nothing
    # persists it, nothing counts it, and the entry is simply no longer on
    # the list — which is the confirmation that it worked.
    if not producedsince.moved_stock(log):
        messages.success(
            request,
            f"Took the {log.quantity} recorded for {product.name} on "
            f"{log.when} off the record. That entry was history only, so no "
            f"stock moved.",
        )
        return redirect(back)

    product.refresh_from_db()
    said = f"Took back {log.quantity} × {product.name} — now {product.number_on_hand} on hand"
    returned = producedsince.blanks_consumed(log)
    if returned:
        raw = log.raw_product
        raw.refresh_from_db()
        said += (
            f", and {returned} {raw.name} back on the raw shelf "
            f"(now {raw.number_on_hand})"
        )
    messages.success(request, said + ".")
    return redirect(back)
