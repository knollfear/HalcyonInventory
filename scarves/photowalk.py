"""Photographing a display, peg by peg.

**The catalogue has 287 active products and 224 of them have no photo.** The
batch upload page answers "here are forty photos, work out what they are" and
leans on the barcode to do it — which, on a phone, next to a pile, reads about
half the time. This module answers the other question: "I am standing in front
of the display, tell me what to shoot next."

That inverts the identification problem instead of solving it. A peg *is* an
identity: the map already says Artisan — Crocodile hangs at row 3, column 4.
So a photo taken at a known stop needs no barcode, no typing and no search —
the walk itself says what the picture is of, one peg at a time, and the
expensive half of the batch page disappears.

Three consequences worth stating, because they are what the design is for:

- **The stop is the claim, and it beats a barcode that disagrees.** On the
  batch page the blank picker is a coarse statement covering forty photos, so
  a decoded barcode is the better evidence. Here the claim is made per photo,
  at the peg, by somebody looking at the scarf — while a symbol that resolves
  in shot may well belong to the colorway hanging two inches to the left. So
  the peg wins and the disagreement is *reported* rather than silently
  resolved either way.
- **Nothing is stored about progress.** Where you are is `?row=&column=`, so
  the walk is addressable: fifteen photos in, get distracted, come back to
  row 3 column 5 by URL. A stored cursor would be a second place the answer
  lived, and the one it disagrees with is the one somebody is looking at.
- **Reserved spaces are not stops.** The price tag in the middle of the top
  row is not a colorway nobody got round to photographing, and stopping at it
  would ask a question with no answer. Empty pegs *are* stops, because "there
  is nothing hanging here" is worth knowing at the wall and because a photo
  taken there is how a peg gets filled in.

Nothing here writes anything. It says where you are and what is next; the
views do the filing.
"""

from .models import DisplayPosition


def stops(fixture):
    """Every cell of the board worth stopping at, in reading order.

    A stop is a place a photograph could be taken: a peg with a colorway on
    it, or an empty hook. Reserved spaces are skipped — signage is not a
    colorway — and a grid cell with no `DisplayPosition` row is still a stop,
    because on the wall an unassigned peg and one nobody has created yet are
    the same empty hook. That is the same reading `DisplayFixture.grid` takes.
    """
    walk = []
    for row_index, grid_row in enumerate(fixture.grid(), start=1):
        for column_index, position in enumerate(grid_row, start=1):
            if position is not None and not position.is_home:
                continue
            walk.append({
                "row": row_index,
                "column": column_index,
                "position": position,
                "product": position.finished_product if position else None,
            })
    return walk


def stop_at(fixture, row=None, column=None):
    """The stop at `row`/`column`, or the next one after it.

    **Never a miss.** A hand-typed address, a bookmark from before the board
    was rearranged, or a link to a peg that has since become the price tag all
    have to land somewhere — and refusing them would end the walk at exactly
    the moment somebody was trying to resume it. So an address that isn't a
    stop advances to the next one that is, and an address past the end comes
    back as finished rather than as an error.
    """
    walk = stops(fixture)
    if not walk:
        return None, walk
    if row is None or column is None:
        return walk[0], walk

    for stop in walk:
        if (stop["row"], stop["column"]) >= (row, column):
            return stop, walk
    return None, walk


def next_after(walk, stop):
    """The stop after this one, or None at the end of the board."""
    if stop is None:
        return None
    for index, candidate in enumerate(walk):
        if (candidate["row"], candidate["column"]) == (stop["row"], stop["column"]):
            return walk[index + 1] if index + 1 < len(walk) else None
    return None


def position_for(fixture, row, column):
    """The `DisplayPosition` at this cell, created if the grid had a gap.

    Only ever called when somebody has just assigned a colorway to the peg,
    so the row is being created to hold a decision rather than to pad the
    board out. A fixture's empty cells are otherwise left as gaps — see
    `DisplayFixture.grid`.
    """
    position, _ = DisplayPosition.objects.get_or_create(
        fixture=fixture, row=row, column=column
    )
    return position


def label_for(product):
    """What to photograph, said the way the shelf says it: blank, colorway.

    The blank comes first because that is what the walk is scoped to — a
    yarn board is one base in forty colours — so the colorway is the word
    that changes from peg to peg and reads last.
    """
    if product is None:
        return ""
    colorway = product.recipe.name if product.recipe_id else "undyed"
    return f"{product.raw_product.name} — {colorway}"


# --------------------------------------------------------------------------
# What might be hanging on an empty peg
#
# The walk's second job, and on a fresh board it is the *main* one: set the
# display up, walk it once, and come away with both the photos and a filled-in
# map. That only works if naming the colorway at each peg is quick, which is
# what the ordering below is for.
#
# **Colour orders the list; it never picks.** A band set is not an identity —
# dozens of colorways classify as blue-and-green — so this moves the right
# answer near the top and nothing more. The person is holding the scarf.
# --------------------------------------------------------------------------

#: Ordering tiers. Exact first, then any superset, then any overlap, then the
#: rest alphabetically.
EXACT, SUPERSET, OVERLAP, REST = 0, 1, 2, 3


def rank(products, photo_bands):
    """Products ordered by how their bands sit against the photo's.

    The tiers are the whole rule:

    - **exact** — the colorway claims precisely the bands the photo shows
    - **superset** — it claims all of them *and others*. Blue+green+five sits
      here just like blue+green+one: the count of extras is not a penalty,
      because a scarf with a lot going on is not a worse match for the blue
      and green in the photo, it is a scarf with a lot going on.
    - **overlap** — at least one band in common
    - **rest** — no overlap, or no confirmed bands to compare. Alphabetical,
      which is what the list was before any of this.

    Unconfirmed colorways land in `rest` rather than being guessed at, for the
    reason the rainbow sheet skips them: `bands_confirmed_at` is what says a
    person agreed, and an unreviewed guess ordering the list would look
    exactly like a reviewed one.
    """
    wanted = set(photo_bands or [])
    ranked = []
    for product in products:
        bands = set(product.recipe.color_bands or []) if _confirmed(product) else set()
        if not wanted or not bands:
            tier = REST
        elif bands == wanted:
            tier = EXACT
        elif wanted <= bands:
            tier = SUPERSET
        elif wanted & bands:
            tier = OVERLAP
        else:
            tier = REST
        ranked.append({
            "product": product,
            "bands": sorted(bands),
            "tier": tier,
        })
    ranked.sort(key=lambda row: (row["tier"], row["product"].name))
    return ranked


def _confirmed(product):
    """Whether this colorway's bands are a person's answer, not a guess."""
    return bool(
        product.recipe_id
        and product.recipe.bands_confirmed_at
        and product.recipe.color_bands
    )


def candidates(fixture, photo_bands, limit=12):
    """What might be on this board's empty peg, best guess first.

    **Scoped to the board's blank when it has one**, which is the same
    narrowing the batch page's picker makes and a much stronger one: a yarn
    board is one base in forty colours, so the answer is one of forty rather
    than one of a few hundred. A mixed board — the scarf rack is a row per
    style — has no such scope, so it offers the ranking over everything and
    leans on the search box underneath for the rest.

    Retired colorways are left out: a peg being filled in now is a decision
    about what hangs there this season.
    """
    from .models import FinishedProduct

    products = FinishedProduct.objects.filter(
        is_active=True, recipe__isnull=False
    ).select_related("recipe", "raw_product")
    if fixture.raw_product_id:
        products = products.filter(raw_product_id=fixture.raw_product_id)

    ranked = rank(list(products), photo_bands)
    return ranked[:limit], len(ranked)


def rankable(fixture):
    """How many of a board's candidate colorways have confirmed bands.

    Said on the page rather than left to be inferred. A list that fell back to
    alphabetical because nothing had been confirmed looks identical to one
    where the photo simply matched nothing — and the fix for the first is a
    trip to `private/colors/`, which nobody makes if they don't know.
    """
    from .models import FinishedProduct

    products = FinishedProduct.objects.filter(
        is_active=True, recipe__isnull=False
    ).select_related("recipe")
    if fixture.raw_product_id:
        products = products.filter(raw_product_id=fixture.raw_product_id)
    products = list(products)
    return sum(1 for p in products if _confirmed(p)), len(products)
