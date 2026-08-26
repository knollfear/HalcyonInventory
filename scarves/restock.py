"""Restocking the display: filling it, and saying so.

**A restock is a repeatable promise that a task was completed.** It happens
at open — which is where the week's production physically enters the display —
and again at close, and at minimum at the end of every shift. Its job is that
the board is full. That is an operational task with no end date, and it is
deliberately *not* the same thing as the Sunday close, which trues the card
pile against what the app expected and exists only while the electronic
system is earning trust.

Two things follow from keeping them apart.

**Restocking generates the cards, so it has to come first.** The crew keep a
product's kanban tag when the last of it leaves the bag to fill a peg. If
restocking was behind all day then most of the evening's cards do not exist
until somebody does the walk — and a close run before that is checking against
a pile that hasn't finished being made, where every late card reads as an
unpredicted tag.

**The app predicts the shortage; the paper confirms it.** Knowing you cannot
fill Aegean Sea has to come from the app's own numbers, so that the card in
your hand is still an independent witness when the two are compared. Driving
the walk off the card pile would collapse two measurements into one and the
disagreement between them would stop meaning anything.

## What the walk asks

One tap per peg, and the ordinary answer is "as predicted". `expected_fill`
is `min(stock, capacity)` — so a peg the app already knows is empty expects
zero, and confirming it is a **completed job**, not a failure. If the app is
right about everything the whole board is a row of taps.

The two exceptions are mirror images and both write an inventory adjustment:

- **short** — couldn't fill it, and the bag behind it is empty. App was over.
  The worked case is app says 3, one on the peg, nothing in the bag, −2.
- **over** — filled it when the app said there was a gap. App was under, which
  is worth the quick look, because predicting gaps wrongly is how a colorway
  quietly stops being offered.

**Every number on a tile is a checkable claim.** A peg carries what to put
out and what the bag should have left afterwards — `on_hand - display_slots` —
rather than a total. A total is not falsifiable by one look: it needs the peg
counted, the bag counted and the two added. The bag figure is read straight
off the bag at the moment the work finishes, so a tap confirms *both* halves
of the app's belief instead of only the visible one.

## What the board predicts, and the one thing it says out loud

Every sale is already an `InventoryLog` row with a timestamp, so **"two went
out since you last filled this peg" is a fact rather than a forecast** — and
one somebody can falsify by looking at the peg. `last_walked` gives the
baseline per position, together with what actually went onto that peg;
`sale_log` reads the ledger forward from it.

**A peg that needs skeins and a peg that can't be filled are different
signals, and the board keeps them apart.** The first is *work*: go to the bag,
put two back. The second is not work at all — nothing done at the board fixes
it, and it belongs to whoever decides what gets dyed. Colouring them the same
would put jobs and non-jobs in one bucket and make the board a list of
problems instead of a list of things to do.

A peg nobody has walked has no baseline, so it predicts nothing. That is
deliberately distinguishable from "nothing sold": a quiet tile has to mean the
app checked and found nothing, never that it had no idea.

### Partial walks, and the thing this must never become

**A walk that covers 23 of 40 pegs is accepted whole, and nothing anywhere
scores it.** Refusing it would lose 23 real answers or, worse, buy 17
manufactured ones from somebody tapping through to get past a validator —
which is a far more expensive failure than a peg nobody looked at. The
unanswered pegs simply keep their older baseline, which `last_walked` already
handles per position.

**Nothing counts walks, tracks a skipped peg or reports completeness.** How
often a board gets restocked measures nothing worth knowing — five passes in
five minutes is a good afternoon, not a problem — and the moment this page
starts grading the frequency it becomes a task master, which is not what it
is for. There is no "17 still to do" anywhere, and there should not be.

The single thing worth noticing is `_drained_at`: **a peg reckoned to have run
bare while there is still stock behind it.** That is yarn that could be
selling and isn't, which is the only version of this anybody actually cares
about. It shows as a plain elapsed time with **no threshold and no
escalation** — whether an hour matters depends on how busy the stall is and
whether anyone is free, neither of which the app can see. It states the fact;
a person decides. Same rule `colorbands` follows.

The failure worth naming: if the webhook dropped a sale, the board
*under*-reports and the peg is emptier than the tile claims. The walk notices,
which makes this one more place a dead integration surfaces without anybody
querying Square.

**The finding that will actually happen is "it says the bag has some, and the
bag is empty."** Nobody is going to count a bag of twelve reliably, and that
is fine — nothing here asks them to. An empty bag is different in kind: it is
noticed without counting, it happens constantly, and it is *exact*.

So the empty bag is the one-tap exception, and "couldn't fill the peg" is a
special case of it rather than a second thing. You cannot fail to fill a peg
unless the bag ran out — the two findings are one finding, and the bag is the
general form.

**An empty bag bounds the total at what the pegs hold.** With nothing behind
the display the total is whatever got put out, so every possible answer is a
button: 0, 1 or 2 on a two-skein hook, and the most common event on the board
costs one tap. The typed box is left for the opposite discovery — more in the
bag than the tile claims — which is unbounded, rarer, and the only case where
somebody has to count anything.

Adjustments are per **product**, never per peg, because that is where stock
lives. A colorway on three pegs raises one exception, not three.

## What it must never do

Nothing here reads a display gap as a reason to dye. A hook that holds four
exists precisely so that three is allowed to be enough — see the northstar in
CLAUDE.md. Par is the production trigger and this module does not touch it.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import (
    DisplayFixture,
    FinishedProduct,
    InventoryLog,
    RestockCheck,
    RestockPass,
)


def expected_fill(position) -> int:
    """How many of this colorway the app thinks can go on this peg.

    Greedy in position order rather than spread evenly, because that is what
    a person does: fill the first peg, then the next, and stop when the bag
    runs out. Spreading would tell somebody to leave a gap on every peg of a
    colorway instead of a gap on the last one, which is not how a board looks
    and not how anybody restocks.
    """
    product = position.finished_product
    if product is None or not position.is_home:
        return 0

    capacity = position.fixture.capacity_per_position
    homes = [
        p
        for p in product.display_positions.select_related("fixture")
        .order_by("fixture__name", "row", "column")
        if p.is_home and p.fixture.is_active
    ]

    remaining = product.number_on_hand
    for home in homes:
        take = min(remaining, home.fixture.capacity_per_position)
        if home.pk == position.pk:
            return take
        remaining -= take
    return min(product.number_on_hand, capacity)


def last_walked(fixture, within_days=30):
    """`{position_id: (when, how many went out)}` for each peg's last answer.

    Per position rather than per pass, because a walk that got interrupted
    left some pegs unanswered and those have an older baseline than the ones
    beside them. Using the fixture's last pass for all of them would claim
    every peg was checked at eight o'clock when half of them last saw anybody
    on Saturday.

    The frozen `expected` comes back with it, because that is what went onto
    the peg at the time — the only honest starting point for working out what
    is on it now.

    Bounded to a month so the query can't grow with the season. A peg nobody
    has touched in thirty days has no usable baseline anyway, and saying so is
    better than pretending to one.
    """
    since = timezone.now() - timedelta(days=within_days)
    latest = {}
    for check in (
        RestockCheck.objects.filter(
            restock_pass__fixture=fixture,
            position__isnull=False,
            restock_pass__created_at__gte=since,
        )
        .select_related("restock_pass")
        .order_by("restock_pass__created_at")
    ):
        latest[check.position_id] = (check.restock_pass.created_at, check.expected)
    return latest


def sale_log(product_ids, since):
    """`{product_id: [(when, units), ...]}` — sales, oldest first.

    **Straight off the ledger, not forecast.** Every sale is already a
    timestamped row, so what the board says about a peg is arithmetic somebody
    can falsify by looking at it — the same bargain the bag figure makes.

    One query for the whole board. The cutoffs differ per position, so the
    slicing happens in Python; forty pegs would otherwise be forty queries.

    Its failure is the interesting one: a dropped webhook makes the board
    *under*-report, so a peg is emptier than the tile claims and the walk is
    what notices. One more place a dead integration surfaces without anyone
    going near Square.
    """
    if since is None:
        return {}
    out = {}
    for pid, when, quantity in InventoryLog.objects.filter(
        finished_product_id__in=product_ids,
        log_type=InventoryLog.SALE,
        created_at__gte=since,
    ).order_by("created_at").values_list(
        "finished_product_id", "created_at", "quantity"
    ):
        out.setdefault(pid, []).append((when, abs(quantity)))
    return out


def _drained_at(sales, cutoff, went_out):
    """When the peg is reckoned to have gone bare, or `None` if it hasn't.

    Walks the sales forward from the last restock and returns the moment the
    running total reached what was put out. **This is the only thing on the
    board worth raising your voice about**, and even then only barely: an
    empty peg with stock behind it is yarn that could be selling and isn't.

    Nothing here counts walks, scores completeness or tracks a missed peg.
    How many times a board gets restocked is not a measure of anything — five
    in five minutes is a good afternoon — and a page that started grading the
    frequency would be measuring the wrong thing loudly.
    """
    if not went_out:
        return None
    running = 0
    for when, units in sales:
        if when < cutoff:
            continue
        running += units
        if running >= went_out:
            return when
    return None


def board(fixture, photos=False):
    """The fixture as rows of cells, each carrying what the walk needs.

    Built here rather than in the template because the interesting parts are
    judgements — whether the app predicts a gap, and whether anything has sold
    off this peg since it was last filled — and a template working those out
    inline would be the second place each rule lived.

    **`photos` is off unless asked for, and the tiles read as names.** A
    photograph identifies a colorway beautifully and answers none of the
    questions the walk asks: what to put out, what should be left in the bag,
    whether this peg has been bare for two hours. Those are words and numbers,
    and on a phone a picture takes the room they need. The name is also what
    the peg's own label says, so a text tile matches the board being walked.

    Photo mode is a real mode rather than an accident of the catalogue — it
    used to appear only where a product happened to have a picture, which made
    the board half one thing and half the other. It costs a query per peg to
    build (`_first_image` per product), which is the other reason it is opt-in
    and not the default forty-times-over.
    """
    walked = last_walked(fixture)
    on_board = assigned_homes(fixture)
    cutoffs = [walked[p.pk][0] for p in on_board if p.pk in walked]
    sales = sale_log(
        {p.finished_product_id for p in on_board},
        min(cutoffs) if cutoffs else None,
    )

    rows = []
    for grid_row in fixture.grid():
        cells = []
        for position in grid_row:
            if position is None:
                cells.append({"position": None, "kind": "missing"})
                continue
            if not position.is_home:
                cells.append({"position": position, "kind": "reserved"})
                continue
            product = position.finished_product
            if product is None:
                cells.append({"position": position, "kind": "empty"})
                continue
            fill = expected_fill(position)
            # Not "the display has a hole", which is a merchandising reading
            # nothing acts on — this is "there is nothing you can do at the
            # board about this one", which is why it is styled apart from a
            # peg that needs work.
            short = fill < position.fixture.capacity_per_position

            baseline = walked.get(position.pk)
            product_sales = sales.get(product.pk, [])
            sold = bare_since = None
            if baseline is not None:
                cutoff, went_out = baseline
                sold = sum(u for when, u in product_sales if when >= cutoff)
                # Bare *and* fixable. A peg with nothing left to put on it is
                # somebody else's decision about what gets dyed, not a thing
                # to hurry about at the board.
                if not short:
                    bare_since = _drained_at(product_sales, cutoff, went_out)
            cells.append({
                "position": position,
                "kind": "home",
                "product": product,
                "expected": fill,
                "capacity": position.fixture.capacity_per_position,
                # **An empty bag bounds the total at what the pegs hold.**
                # Nothing is behind the display, so the total is whatever got
                # put out, and every possible answer is a button. Nobody
                # types for the finding that actually happens.
                "options": list(range((product.display_slots or 1) + 1)),
                # **What the bag should hold once the peg is full**, not the
                # total. A total is not checkable by any single observation —
                # you would have to count the peg, count the bag and add — so
                # printing one states a prediction nobody standing at the
                # board can falsify. `on_hand - display_slots` can be read
                # straight off the bag at the moment the job is finished,
                # which is what makes the tap worth anything.
                "backstock": product.backstock,
                # Sold off this peg since somebody last filled it, so this is
                # how many to put back. `None` means nobody has ever walked
                # this peg, which is a different thing from "nothing sold" and
                # must not read as a quiet tile.
                "sold": sold,
                # **Two different signals, deliberately kept apart.** A peg
                # that needs skeins and has them behind it is *work* — go to
                # the bag and refill. A peg that can't be filled is not work
                # at all; nothing you do at the board fixes it. Collapsing
                # them would put jobs and non-jobs in the same colour.
                "needs_refill": bool(sold) and not short,
                # When this peg is reckoned to have run bare with stock still
                # behind it. The one thing here worth noticing, and it is
                # shown as a plain elapsed time with no threshold and no
                # escalation — whoever is looking decides whether an hour
                # matters, which depends on things the app cannot see.
                "bare_since": bare_since,
                # The app's own prediction of a gap, and the whole reason the
                # walk is worth doing with a phone rather than by eye: it is
                # what stops somebody going to look for a colorway there was
                # never any of.
                "short": short,
                "image": _first_image(product) if photos else None,
            })
        rows.append(cells)
    return rows


def _first_image(product):
    """The picture for a tile in photo mode, or None to fall back to text.

    Falling back rather than showing a placeholder: a named tile is readable
    and a grey box is not, and half this board will be text until the photos
    catch up. Which is also why text is what the board opens as — see `board`.
    """
    image = product.images.first()
    if image is None:
        return None
    # `FinishedProductImage.url` already knows the bucket-then-external-URL
    # order. Re-deriving it here would be a second copy that drifts, and the
    # way drift shows is a tile that silently falls back to text.
    return image.url or None


def unmapped_for(fixture):
    """Colorways of this board's blank that aren't up on any board.

    **The mapper's own working list, and it appears nowhere else.** Deciding
    what belongs on a board is somebody's job, not the app's, so this is
    offered on the editor — where the person doing that job is sitting, with
    the empty pegs in front of them — and shown to nobody else. It used to
    appear on the crew's board too, which told the wrong person about work
    they had no part in and quietly asserted that the app knew what ought to
    be hanging there.

    A mixed board gets nothing at all. Without a blank there is no such
    question to answer, and inventing one would be the app claiming
    responsibility nobody gave it: the scarf rack is a row per scarf type,
    and which colorways belong on it is the mapper's call.

    Scoped to "no home on **any** active board" rather than "not on this
    one", because a colorway living on a second board is displayed already.

    Includes colorways with no stock, tagged as such — an empty peg for a
    colorway you have none of is a decision about what to dye, and hiding it
    would answer "what's missing?" with only the half that is fixable this
    minute.
    """
    if fixture.raw_product_id is None:
        return FinishedProduct.objects.none()
    return (
        FinishedProduct.objects.filter(
            is_active=True,
            recipe__isnull=False,
            raw_product_id=fixture.raw_product_id,
        )
        .exclude(display_positions__fixture__is_active=True)
        .select_related("raw_product", "recipe")
        .order_by("recipe__name")
    )


def board_status(fixture):
    """What is waiting on this board, in the order it is worth caring about.

    Three counts, deliberately kept apart, because only two of them can be
    acted on by walking over there:

    - **bare** — a peg with nothing on it and stock behind it. Yarn that could
      be selling and isn't, and the reason to do this rack before that one.
    - **topup** — sold some, still has some. Worth doing, not urgent.
    - **unfillable** — nothing to put on it. Counted so a board that *cannot*
      be fixed doesn't look like one that has been neglected, and kept quiet
      because carrying a phone over there changes nothing about it.

    `units` is what to bring from backstock for this board, which is the
    number that decides whether it is an armful or a pocketful.

    Note what is *not* here: nothing about how much of the board was walked,
    when it was last done, or how that compares with anything. These are
    counts of work available, never of work outstanding — see the module
    docstring on why this page is not a task master.
    """
    cells = [cell for row in board(fixture) for cell in row if cell["kind"] == "home"]
    bare = [c for c in cells if c["bare_since"]]
    return {
        "bare": len(bare),
        "topup": sum(1 for c in cells if c["needs_refill"] and not c["bare_since"]),
        "unfillable": sum(1 for c in cells if c["short"]),
        "units": sum(_to_bring(c) for c in cells),
        "oldest_bare": min((c["bare_since"] for c in bare), default=None),
    }


def _to_bring(cell):
    """How many units this peg wants back, capped at what a peg holds.

    `sold` is what left it, so putting that many back restores it — but a peg
    that sold five over two days still only takes what it holds, and asking
    for five would send somebody to the bag for three that have nowhere to go.
    """
    if not cell["needs_refill"]:
        return 0
    return min(cell["sold"] or 0, cell["capacity"])


def pull_list():
    """One trip to the backstock for the whole stall: what to carry, and where.

    Aggregated per product rather than per peg, because a colorway on three
    pegs is one bag to open — the same reason a restock exception corrects
    stock once rather than three times.

    Sorted by SKU, which is `BLANK-DYEBATH` and therefore groups by blank.
    That is how the shelf is arranged and how the label sheets come off, so
    the list reads in the order the bags are actually stood in rather than in
    an order the app found convenient.
    """
    wanted = {}
    for fixture in DisplayFixture.objects.filter(is_active=True).select_related(
        "raw_product"
    ):
        for row in board(fixture):
            for cell in row:
                if cell["kind"] != "home":
                    continue
                units = _to_bring(cell)
                if not units:
                    continue
                entry = wanted.setdefault(
                    cell["product"].pk,
                    {"product": cell["product"], "units": 0, "boards": []},
                )
                entry["units"] += units
                if fixture.name not in entry["boards"]:
                    entry["boards"].append(fixture.name)
    return sorted(
        wanted.values(),
        key=lambda e: (e["product"].sku or "", e["product"].name),
    )


def open_pass(fixture, employee=None):
    """Start a walk. Always a new one — each pass is its own promise."""
    return RestockPass.objects.create(fixture=fixture, employee=employee)


def assigned_homes(fixture):
    """Pegs with a colorway on them — the only ones a walk can answer."""
    return [
        position
        for grid_row in fixture.grid()
        for position in grid_row
        if position is not None and position.is_home and position.finished_product_id
    ]


def close_pass(restock_pass):
    """Mark a pass that answered every peg, and return whether it did.

    **Two jobs were hiding in one page.** A *full check* is the board walked
    end to end, expected at open and at close; a *pass* is a top-up, any time,
    however many pegs. Both are worth doing and they are not the same thing,
    so the complete one gets a name.

    What the name buys is not a score. After a full check every position has a
    fresh baseline, so everything the board predicts — what sold off each peg,
    which ones have run bare — is trustworthy across the whole board. After a
    partial pass some of it is running off a baseline from yesterday, and the
    only way to know which is to record when the board was last covered.

    Note the asymmetry, which is the whole reason this is not a task master:
    **completeness is recognised, incompleteness is never penalised.** Nothing
    counts what a partial pass left out, nothing calls it unfinished, and nine
    pegs at four o'clock is a completed piece of work rather than a failed
    full check.
    """
    if restock_pass.is_full:
        return True
    homes = len(assigned_homes(restock_pass.fixture))
    if homes and restock_pass.checks.count() >= homes:
        restock_pass.is_full = True
        restock_pass.save(update_fields=["is_full"])
        return True
    return False


def last_full_check(fixture):
    """The last time the whole board was covered, or `None`.

    The picker states it as a plain timestamp and stops there. "Last full
    check: yesterday, 6:40pm" tells somebody arriving in the morning what they
    need to know without the app deciding that makes them late — how the day
    is going is not something it can see.
    """
    return (
        fixture.restock_passes.filter(is_full=True)
        .select_related("employee")
        .first()
    )


@transaction.atomic
def record(restock_pass, position, counted=None):
    """One peg's answer. Returns the check, or None if there was nothing to ask.

    `counted` is the **product's** true total and `None` means "as predicted".
    So the ordinary answer carries no number at all, which is what keeps the
    walk to one tap per peg.

    **The direction is the sign of the delta, not a button.** The page offers
    two exceptions because they are two different discoveries to a person
    standing at the board — "I couldn't fill it" and "I filled it when you
    said I couldn't" — but both are answered with a real count, and which one
    it turns out to be is arithmetic. A button that named the direction could
    disagree with the number typed under it, and then one of them would be
    wrong with nothing to say which.

    The count is the product's, never the peg's, because that is where stock
    lives. A colorway on three pegs raises one exception; by the time the
    second peg is answered there is nothing left to correct, which is why a
    repeat is a no-op rather than three adjustments for one discovery.
    """
    if not position.is_home or position.finished_product_id is None:
        return None

    product = FinishedProduct.objects.select_related("raw_product").get(
        pk=position.finished_product_id
    )
    expected = expected_fill(position)

    check, _created = RestockCheck.objects.get_or_create(
        restock_pass=restock_pass,
        row=position.row,
        column=position.column,
        defaults={
            "position": position,
            "finished_product": product,
            "expected": expected,
        },
    )
    # Set once and never again. The page is reopened, the button is
    # double-tapped, and somebody walks the same row twice to be sure — all
    # three are normal, and all three used to be a second adjustment.
    if check.applied_log_id is not None:
        return check

    if counted is None:
        check.result = RestockCheck.AS_PREDICTED
        check.counted = None
        check.save()
        return check

    counted = max(int(counted), 0)
    delta = counted - product.number_on_hand
    check.counted = counted
    if delta > 0:
        check.result = RestockCheck.OVER
    elif delta < 0:
        check.result = RestockCheck.SHORT
    else:
        check.result = RestockCheck.AS_PREDICTED

    if delta:
        was = product.number_on_hand
        product.set_on_hand(counted)
        check.applied_log = _adjustment(
            product,
            delta,
            f"Restock: {product.name} counted at {counted} against the app's "
            f"{was}. The app was {'under' if delta > 0 else 'over'} by "
            f"{abs(delta)}.",
        )
    check.save()
    return check


def _adjustment(product, delta, notes):
    """One stock movement, tagged as a restock's."""
    return InventoryLog.objects.create(
        finished_product=product,
        raw_product=product.raw_product,
        log_type=InventoryLog.ADJUSTMENT,
        source=InventoryLog.SOURCE_RESTOCK,
        quantity=delta,
        notes=notes,
    )


def summary(restock_pass):
    """What the pass promised and what it found.

    Deliberately no completion percentage. The useful readings are how much
    of the board was walked and how often the app was wrong, and putting
    "38 / 42" beside them turns a promise into a score — the same argument
    that keeps a rate off the close.
    """
    checks = list(restock_pass.checks.all())
    return {
        "checked": len(checks),
        "homes": len(assigned_homes(restock_pass.fixture)),
        "is_full": restock_pass.is_full,
        "as_predicted": sum(
            1 for c in checks if c.result == RestockCheck.AS_PREDICTED
        ),
        "short": sum(1 for c in checks if c.result == RestockCheck.SHORT),
        "over": sum(1 for c in checks if c.result == RestockCheck.OVER),
        # Cards the walk should have produced: a peg that couldn't be filled
        # means the bag behind it is empty. "At least", because a peg filled
        # exactly to capacity may have emptied its bag too and the fill number
        # alone cannot say.
        "cards_expected": sum(
            1 for c in checks if c.expected < _capacity(c) or c.result == RestockCheck.SHORT
        ),
    }


def _capacity(check):
    return check.restock_pass.fixture.capacity_per_position
