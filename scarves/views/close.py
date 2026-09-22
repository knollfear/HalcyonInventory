"""The Sunday close: the index, the crew's count page, tag search and undo."""
from urllib.parse import urlencode

from django.urls import reverse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages
from django.shortcuts import render, redirect

from ..sitemap import page_meta
from ..models import CloseRun, CloseRunRow, FinishedProduct, RawProductCategory
from .. import closing, crew
from ..forms import CloseStartForm, build_close_count_form_class
from .common import _is_htmx
from .images import search_products


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
            return redirect(_close_run_url(run, _COUNT, _close_category(request)))

        # Everything still answerable, freshly read. Deliberately the whole
        # run rather than the table on screen: a row that isn't in the POST
        # is one nobody touched, which is exactly what `counts()` already
        # assumes, so the filter needs no say in what a submit may answer.: `sync_expected` may have
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
        # Back to the counting list, not the cards, and back to the table
        # this was submitted from. Worked in several passes across an
        # evening, so defaulting a mid-pile submit into the summary — or
        # onto the other table — would put the person back through a link
        # every time.
        return redirect(_close_run_url(run, _COUNT, _close_category(request)))

    # New zeros since the page was last opened get folded in here, so a close
    # started at noon still asks about the scarf that sold out at four.
    closing.sync_expected(run)
    return _close_run_page(request, run)


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

#: The counting form's DOM id. Every count field carries it as an HTML
#: `form=` attribute, which is what lets a row added mid-session sit beside
#: the search box that found it and still submit with the rest — the row is
#: where somebody is looking, and one Save covers the lot.
_COUNT_FORM = "count-form"


def _close_run_url(run, mode=None, category=None):
    """This run's URL, in a mode and on a table. Reversed by name, never
    hardcoded.

    The category rides alongside the mode and for the same reason: a close is
    walked one table at a time, in several passes across an evening, so every
    action on the page has to come back to the table the person is standing
    at. It is named rather than numbered so a reading is a link somebody can
    read as well as send — `?category=Yarn` says what it does.
    """
    url = reverse("close_run", args=[run.token])
    params = []
    if mode:
        params.append(("mode", mode))
    if category is not None:
        params.append(("category", category.name))
    return f"{url}?{urlencode(params)}" if params else url


def _close_category(request):
    """Which table's rows are on screen, or `None` for all of them.

    Read from the POST first so the undo and add-tag forms can carry it —
    those post to their own URLs and have no query string to inherit.

    An unknown name is no filter rather than an error, the same call the
    recipe page's `?product=` makes: a filter is navigation, the catch-all
    redirect makes stale links ordinary here, and the worst a renamed
    category should do is show more than was asked for.
    """
    wanted = (
        request.POST.get("category") or request.GET.get("category") or ""
    ).strip()
    if not wanted:
        return None
    return RawProductCategory.objects.filter(name__iexact=wanted).first()


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


def _close_category_pills(run, rows, mode, selected):
    """One pill per table, plus All, each counting what is on its own screen.

    **The count is the reading's own number**, not a total that survives the
    filter: on the counting list it is what is left to count, on the cards it
    is the stack that should be in a hand. A pill reading "Yarn 14" over a
    list of nine is the page contradicting itself, and the number people act
    on is the one beside the list they are looking at — the same call
    `private/colors/` makes about its pills.

    Nothing carries `?answered=1`, which is deliberate: the drawer is a
    reveal that evaporates on the next link, and a pill that dragged it along
    would quietly put the long list back on the table somebody just switched
    to.

    **No pills at all when every row is on one table.** A filter offering one
    choice is furniture, the same reason the recipe page draws no product
    chips for a colorway dyed on a single blank.
    """
    present = closing.categories_present(rows)
    if len(present) < 2:
        return []

    def counted(subset):
        if mode == _CARDS:
            return len(closing.card_status(subset)[0])
        return len([
            row
            for row in subset
            if row.outcome == CloseRunRow.PENDING and not closing.is_frozen(run, row)
        ])

    pills = [{
        "label": "All",
        "count": counted(rows),
        "href": _close_run_url(run, mode),
        "selected": selected is None,
    }]
    for category in present:
        subset = closing.in_category(rows, category)
        pills.append({
            "label": category.name,
            "count": counted(subset),
            "href": _close_run_url(run, mode, category),
            "selected": selected is not None and category.pk == selected.pk,
        })
    return pills


def _close_status(run, rows, category, mode, show_answered=False):
    """The numbers that sit above the counting list, computed in one place.

    The page renders these and the add-tag fragment swaps them out of band,
    for the reason the recipe page does the same with its figures: a header
    reading "9 left to count" over a list of ten is the page contradicting
    itself, and it is above the fold, so nobody sees it happen.
    """
    visible = closing.in_category(rows, category)
    open_rows = [r for r in visible if not closing.is_frozen(run, r)]
    pending = [r for r in open_rows if r.outcome == CloseRunRow.PENDING]
    answered = [
        r for r in visible
        if r.is_applied or r.outcome == CloseRunRow.CONFIRMED
    ]
    return {
        "run": run,
        "category": category,
        "category_pills": _close_category_pills(run, rows, mode, category),
        # What this reading is leaving out. Said out loud wherever a filter
        # is on, because the failure to avoid is a table's worth of rows
        # going unasked while the page in front of somebody reads as
        # finished — the same silence the "still to count" list exists to
        # break.
        "hidden_count": len(rows) - len(visible),
        "pending_count": len(pending),
        "answered_count": len(answered),
        "show_answered": show_answered,
        # The drawer, both ways, with the mode and the table still on them.
        "show_answered_url": (
            _close_run_url(run, _COUNT, category) + "&answered=1"
        ),
        "hide_answered_url": _close_run_url(run, _COUNT, category),
    }


def _close_run_page(request, run, count_form=None, mode=None, show_answered=None):
    """Render one close, in one of its two readings, for one of its tables."""
    rows = list(
        run.rows.select_related(
            "finished_product__recipe",
            "finished_product__raw_product__category",
        )
    )
    mode = mode or _close_mode(request, run, rows)

    # **The table, and it narrows the reading rather than the run.** A close
    # is walked one table at a time — the yarn boards are one circuit and the
    # silk racks another — and a forty-row list that mixes them asks somebody
    # standing at one to read past the other on every pass. So the rows are
    # filtered here, at the last moment, and nowhere else: `sync_expected` has
    # already folded in every emptied bag, every row is still on the run, and
    # the pill for the table nobody is standing at still counts what is left
    # on it. Hiding is only safe because of that last part, and because an
    # absent field was already how a half-worked close works — `counts()`
    # records the rows somebody answered, so a row that isn't in the POST is
    # one nobody touched rather than an answer of any kind.
    category = _close_category(request)
    all_rows, rows = rows, closing.in_category(rows, category)
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
        count_form = build_close_count_form_class(form_rows, _COUNT_FORM)(
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
        "mode": mode,
        "count_mode": _COUNT,
        "cards_mode": _CARDS,
        "count_url": _close_run_url(run, _COUNT, category),
        "cards_url": _close_run_url(run, _CARDS, category),
        **_close_status(run, all_rows, category, mode, show_answered),
        "cards": cards,
        "no_cards": no_cards,
        "uncounted": uncounted,
        "count_form_id": _COUNT_FORM,
        "tag_search_url": reverse("close_tag_search", args=[run.token]),
        "query": query,
        "results": search_products(query) if query else None,
        "tally": closing.tally(run, rows),
        "rows": rows,
        "applied_rows": applied_rows,
        "confirmed_rows": confirmed_rows,
        "count_fields": _count_field_entries(count_form, form_rows),
        "count_form": count_form,
    })


def _count_field_entries(count_form, rows):
    """A row and its two fields, ready to render.

    One shape, built once, so the page's rows and a row swapped in beside the
    search are the same markup — the drift to avoid is a swapped-in row that
    posts something the rendered one doesn't.
    """
    return [
        {
            "row": row,
            "field": count_form[f"counted_{row.pk}"],
            "more": count_form[f"more_{row.pk}"],
        }
        for row in rows
        if f"counted_{row.pk}" in count_form.fields
    ]


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
        # The form sends the table along with the query, so the buttons this
        # renders carry it too — the swapped copy and the inline one have to
        # post the same thing or they drift.
        "category": _close_category(request),
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
    category = _close_category(request)
    if not run.is_open:
        messages.error(request, _CLOSED_RUN_MESSAGE)
        return redirect(_close_run_url(run, _COUNT, category))

    product = (
        FinishedProduct.objects.filter(
            pk=request.POST.get("product_id"), is_active=True
        )
        .select_related("raw_product__category")
        .first()
    )
    if product is None:
        messages.error(request, "Couldn't find that product — try the search again.")
        return redirect(_close_run_url(run, _COUNT, category))

    row, created = closing.add_tag(run, product)
    # **A tag typed in at the yarn boards stays put, whatever it is.** The
    # search is over the whole catalogue, so a silk scarf found there is an
    # ordinary thing to add — and `closing.in_category` keeps every
    # hand-added row on every table, so it does not need the page moved to
    # its own to be visible. Making somebody switch tables to answer a tag
    # they are holding is the app arguing with the person who found it.
    if created:
        note = (
            f"Added {product.name} — the app has {row.on_hand_before} on "
            f"hand. Count what's on the display and say how many."
        )
    else:
        note = f"{product.name} was already on this close."

    if _is_htmx(request):
        # **A swap, not a navigation.** This is late in a long evening and
        # the form above is half-filled: a redirect throws away every answer
        # typed but not yet saved and lands the person back at the top of a
        # page they were at the bottom of. So the new row arrives beside the
        # search that found it, carrying `form=` so it still submits with the
        # rest, and the counts above it are never re-rendered and so cannot
        # be lost.
        #
        # The double-add this used to guard against with a full page load is
        # handled where it always really was: `closing.add_tag` hands back
        # the row that exists rather than making a second one, so the worst a
        # repeated tap does is say so.
        return render(request, "scarves/partials/close_tag_added.html", {
            "run": run,
            "note": note,
            "row": row if created else None,
            "count_fields": _count_field_entries(
                build_close_count_form_class([row], _COUNT_FORM)(
                    initial=_count_initial([row])
                ),
                [row],
            ) if created else [],
            "count_form_id": _COUNT_FORM,
            **_close_status(
                run,
                list(
                    run.rows.select_related(
                        "finished_product__raw_product__category"
                    )
                ),
                category,
                _COUNT,
            ),
        })

    if created:
        messages.success(request, note)
    else:
        messages.info(request, note)
    return redirect(_close_run_url(run, _COUNT, category))


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
    category = _close_category(request)
    if not run.is_open:
        messages.error(request, _CLOSED_RUN_MESSAGE)
        return redirect(_close_run_url(run, _COUNT, category))

    row = run.rows.filter(pk=pk).select_related("finished_product").first()
    if row is None:
        # Already undone, most likely a double tap. Says so plainly rather
        # than erroring, because the page is now in the state they wanted.
        messages.info(request, "That one's already been put back.")
        return redirect(_close_run_url(run, _COUNT, category))

    name = row.finished_product.name
    closing.undo(run, row)
    messages.success(
        request,
        f"Put {name} back the way it was. Nothing to tell anyone about — the "
        f"log keeps both entries.",
    )
    return redirect(_close_run_url(run, _COUNT, category))
