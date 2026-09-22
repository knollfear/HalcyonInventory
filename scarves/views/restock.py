"""The display map and the restock walk."""
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.shortcuts import render, redirect

from ..sitemap import page_meta
from ..models import DisplayFixture, FinishedProduct
from .. import crew, restock
from ..forms import DisplayFixtureForm, RestockPassForm
from .common import _NEVER


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
        "fixtures": DisplayFixture.objects.active().select_related(
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
    choices = FinishedProduct.objects.active().filter(recipe__isnull=False)
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
                "boards": DisplayFixture.objects.active(),
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
        "boards": DisplayFixture.objects.active(),
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
    for fixture in DisplayFixture.objects.active().select_related(
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
        "boards": DisplayFixture.objects.active(),
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
