# Syncing to Square

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Syncing to Square: what goes up, and what deliberately doesn't

`sync_to_square` runs in modes, and each one returns rather than falling
through: `--check` (credentials only), `--images`, `--inventory-only`,
`--update`, or the bare run that upserts the catalogue and then pushes stock.
Every failure path raises `CommandError` rather than printing and returning,
because a bare `return` exits 0 — on a schedule that reads as a successful
run, and a catalogue that quietly stopped syncing looks exactly like one that
had nothing to do.

Ordering for a fresh account is in the labels section: **`generate_skus`
first, then `sync_to_square`**, because the sync omits the `sku` key entirely
when it's blank.

### `--images`: photos go on the variation, and only once

A photo is of one colorway, so it is attached to the **ITEM_VARIATION**, not
the ITEM. An item here is a style (`Silk Scarf`) and every variation under it
looks completely different — one photo on the item would pick a winner and
mislabel everything else.

**`FinishedProductImage.square_image_id` is the whole point.** Square's
`CreateCatalogImage` appends to the object's `image_ids` and has nothing that
says "you already sent me this"; without a local record, every re-run stacks
another copy of the same photo on the same variation. So the ID is written the
moment Square answers, before the next photo starts — the run can die anywhere
and what got through is already recorded.

Two consequences fall out of that:

- **A success with no ID back stops the run.** Square has the photo, we have
  nothing to record, and continuing would upload it again next time. It's the
  one case here where a success is worse than an error.
- **The first photo to land on a variation is its primary**, and later ones
  are not, so a re-run can't displace the picture the POS shows.

It's a mode of its own because it's slow: no batch endpoint, one multipart
request per photo, and the bucket is private so the bytes go bucket → this
process → Square rather than being handed over as a URL Square could fetch.
That has no business running on the schedule that pushes stock counts.

Three things it can't send are **named and counted, never silently dropped** —
from Square's end all three look identical (a product with no picture):
products Square has never seen (run the plain sync first), images that are
only an external URL with no file in the bucket, and files missing from the
bucket. That last one is caught narrowly on purpose: `S3Storage` raises
`FileNotFoundError` only on a 404 and re-raises every other `ClientError`, so
one missing object skips one photo while bad credentials still stop the run.

### Variation order: the only lever is position, and it deletes

The POS lists an item's variations in catalogue order, and a new variation is
appended — so the colourways at the till end up in the order the dye baths
happened, which is nobody's mental model of a colour. At a stall with a queue
that means reading the whole list every sale.

`ordinal` is the field that decides it and **it is read-only**: on a write
Square assigns each variation's ordinal from its *position* in the parent
item's `variations` list. There is nothing to set. The only way to reorder is
to send the whole ITEM back with the list in the order you want — which is
what dragging the handles in the dashboard does.

That makes `_make_items_till_ready` the most dangerous call in the file. **An
ITEM upsert replaces the variation list outright, so a variation missing from
it is deleted**, taking its Square ID, its stock and the sale history's link
to it. One rule keeps that safe, and it is worth stating plainly:

> The pass never *builds* a variation. It reads the item as Square has it,
> permutes the list Square returned, and sends that back.

Everything else follows from it. An item that comes back with no variations is
skipped rather than sent — an answer we didn't understand looks exactly like
an item with nothing under it, and only one of those is safe to echo. A
variation with no `name` is left where it is: it is named by an item option,
whose values already decide the order, and sorting it on the empty string
would bunch it at the top and fight whatever set that. (The item may still be
rewritten for the other reason below — the list simply goes back unpermuted.)

Items already in order are not rewritten, because otherwise every scheduled
run bumps every version to change nothing. The sort is `casefold` and stable,
so equal names keep the order Square has instead of churning.

**Except that "already in order" can't be read off the names**, and this is
the trap the live catalogue was actually in. Square assigns ordinals only
when a parent item's variation list is written — and a variation added on its
own, via the `ITEM_VARIATION` path, never is. That path is how every
colourway after the first reached Square, so most variations here had
`ordinal: None`. The API still hands them back in name order, so the item
*reads* as sorted from `list_catalog`, while the till has no positions to go
on and shows them in the order they were created. That gap is the reported
symptom, and comparing names alone skipped exactly the items that had it.

So an item missing any ordinal is rewritten even when the permutation is a
no-op: that write is the only thing that assigns one. `Undyed Yarn`, created
as a whole ITEM in one upsert, had ordinals 0–9 and was correctly left alone
— which is what made the difference visible.

**It runs at the end of a normal sync, not only on demand.** The run that
creates a variation is the run that appends it, and `--update` renames
variations when a recipe is renamed — those are the two moments the order
breaks, so the fix belongs at both. A `--reorder` mode exists for fixing a
catalogue that already drifted (which is how this arrived), the same way
`generate_skus` stayed around for backfill after SKUs moved into `save()`.

Chunking counts *objects*, not items: variations ride inline, so a hundred
items is closer to a thousand objects and the batch limit counts the children.

Items themselves need nothing — the POS already lists those alphabetically.

### An item may not choose a colourway for the cashier

Square has a per-item setting, **"Automatically select first variation"**, and
it does exactly that: the POS puts variation one straight into the cart and
never shows the list. On a one-variation item that is a sensible shortcut,
which is why it exists. On a style carrying forty colourways it is a wrong
answer that nothing downstream can tell from a right one — the receipt, the
stock count, the sales history and the season report all agree, and all of
them are agreeing about a colour nobody bought.

That is not hypothetical. It is why a season of sash belts rang up as
`Amethyst`: first alphabetically under `Sash Belt`, and the only colourway the
till ever offered. The tell was supposed to be an implausible top seller, and
it wasn't one, because a plausible-looking number is exactly what this bug
produces.

The field is `item_data.skip_modifier_screen`, and the reasoning about it is
the same shape as everything else here:

- **The sync says no every run, rather than saying it once.** Nothing in this
  app sets the flag — a person does, in two taps, on a phone, usually
  believing they are speeding the queue up. Clearing it once fixes the
  catalogue until the next time somebody has that thought, and there is no
  event to notice it by. Same rule as the price guard: never add a step that
  has to be remembered to be correct.
- **It is cleared only on items with two or more variations.** One variation
  means there is nothing to choose between, so the flag there means what its
  name says about modifiers and is somebody's deliberate setting — the
  notions and the one-off items are full of it.
- **It rides the pass that was already reading every item.** The ordering
  pass retrieves the whole ITEM and echoes it back, which is the one write
  that can carry this, so `_reordered_item` became `_till_ready_item` and
  returns a *list* of reasons: `resorted`, `positions`, `autoselect`. An item
  needing only the flag cleared is written; an item needing nothing is still
  left alone, so a run stops churning versions once the catalogue agrees.
- **Refusing to sort is not a reason to leave it choosing.** A variation
  named by an item option can't be ordered here, and such an item used to be
  skipped outright. It now goes back with the flag cleared and the variation
  list exactly as Square gave it — unsortable means unpermuted, not rebuilt.
- **The repair is counted and named in the output**, not folded silently into
  the reorder total. This is the one that was ringing up the wrong colourway;
  a quiet fix leaves nobody knowing how long it had been doing it, and that
  count is the only evidence there will ever be.

New ITEM payloads carry `skip_modifier_screen: False` explicitly too, so a
freshly created style is never briefly able to choose before the till-ready
pass reaches it.

### A price has two authors, and only one of them writes it down

A price can be set here or typed into the Square dashboard, and the dashboard
is what gets edited when the stall is open and this app is not. `--update`
sends `FinishedProduct.price` at **every** variation Square already has, so a
price changed in Square survives exactly until the next `--update` — at which
point it is replaced with nothing anywhere to say a different number was ever
there. The till starts charging a figure nobody chose and the only symptom is
a receipt.

**So `--update` refuses a row whose price Square holds differently**, names
it, and carries on with the rest. The read it already makes for `version`
carries `price_money` too, which is why the guard costs no extra call and can
be on by default. `--force-prices` overwrites, for when ours really is right.

`compare_square_prices` is where a divergence gets settled:

```
python manage.py compare_square_prices                    # read-only diff
python manage.py compare_square_prices --pull             # Square wins
python manage.py compare_square_prices --push --sku X     # we win, named
python manage.py compare_square_prices --interactive      # one at a time
```

Four things in it are the same bargains the rest of the app makes:

- **The diff sorts by Square's own `updated_at`, newest first.** Nobody
  writes down which prices they changed during a busy morning, but Square
  did — so the run of rows edited on one afternoon clumps at the top instead
  of scattering through an alphabetical list. `--changed-since` is that
  observation made into a scope.
- **A read failure is never read as agreement.** An empty answer and a
  catalogue that agrees on every price look identical, and only one is safe
  to act on — same reasoning as the version read in `--update` and the
  ordering pass.
- **`--push` never *builds* a variation.** It takes the object as Square
  returned it, replaces the amount, and sends that back, so a field this app
  doesn't model can't be dropped by being absent from a payload assembled
  here. The rule `_make_items_till_ready` already follows, for the same
  reason.
- **Variable pricing is named, not pulled as zero.** It is the till asking
  the cashier, not a price of nothing, and a mistyped `--sku` stops the run
  rather than matching no rows — "nothing to do" reads on screen exactly like
  a catalogue that already agreed.

Worth stating plainly, because the instinct is to fix this by remembering to
check: **nothing here depends on anyone remembering.** The guard is what makes
the divergence impossible to drive over silently; the command is only how it
gets settled once it has been raised. Same rule as everywhere else — never add
a step that has to be remembered to be correct.

### Colour bands are not synced, on purpose

`Recipe.color_bands` stays local. The POS never displays custom attributes, so
pushing them would put data in Square that no one can see and that then has to
be kept in step. The question they answer — "what sold in red?" — is a local
join from a sale back to `recipe.color_bands`, which needs nothing at the
Square end beyond the variation ID already stored.
