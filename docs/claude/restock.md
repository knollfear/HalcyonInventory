# Restocking the display

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Restocking the display: the promise, and why it comes first

`secret/restock/` is the operational half of the display, and it is **not** a
smaller version of the close. A restock is a **repeatable promise that a task
was completed** — the board is full — made at open, at close, and at minimum
at the end of every shift. It has no end date. The close trues the card pile
against what the app expected and exists only while the electronic system is
earning trust; it should get *less* important over time, not more.

**Restocking generates the cards, so it runs before the close.** The crew keep
a product's kanban tag when the last of it leaves the bag to fill a peg. If
restocking was behind all day, most of the evening's cards don't exist until
somebody walks the board — and a close run first is checking against a pile
that hasn't finished being made, where every late card reads as an unpredicted
tag.

**The app predicts the shortage; the paper confirms it.** Knowing you can't
fill Aegean Sea comes from `number_on_hand < display_slots`, never from
reading the card pile. Driving the walk off the cards would collapse two
measurements into one, and then a disagreement between them means nothing.
Same shape as `colorbands` and the sheet scanner: the app fills the form in, a
person decides.

### The board, and what one tap means

`DisplayFixture` is a grid plus `capacity_per_position`; `DisplayPosition` is
one peg. Orientation is data — whether the yarn board reads 6×7 or 7×6 is a
thing to check against the wall, not to decide in code. A position with a
`reserved_label` and no product is **not a home** (the price tag sits in the
middle of the top row), which is deliberately distinct from a real peg nobody
has assigned yet.

`expected_fill` is `min(stock, capacity)`, greedy in position order — what a
person does, filling the first peg then the next until the bag runs out.
Spreading evenly would ask for a gap on every peg of a colorway instead of a
gap on the last one.

**A tap confirms "as predicted", and a peg the app knows is empty still gets
tapped.** You did the job; the walk confirmed it. Treating an expected gap as
an exception would make an ordinary evening read as a list of problems.

**The board reads as names, and `?photos=1` swaps it to pictures.** Photo mode
used to arrive by accident — a tile showed a photo wherever the product
happened to have one — so a board came out half pictures and half text and
neither mode was ever chosen. The questions a walk asks are words and numbers
(what to put out, what the bag should hold, how long this peg has been bare),
a name is what the peg's own label says, and on a phone a photograph takes the
room those answers need. Names is also the mode that **prints**: the stall's
connection is slow enough that photo tiles land after they were wanted, and
paper needs no connection at all — so the print rules drop the leads, the
count panels and the sign-off and leave the grid. The mode lives in the URL
rather than a cookie, and every link off the page carries it (including the
POST redirect), so a circuit walked in one mode stays in it. Text is still the
fallback *inside* photo mode, because half the catalogue has no picture and a
grey box names nothing.

**Every number on a tile is a checkable claim, and every one is the present
tense.** A peg says what is hanging on it, how many to put out, and what is
in the bag behind it — `1/2, +1, bag 6` is "one hanging up, add one, six in
the bag". Never a total: a total needs the peg counted, the bag counted and
the two added, which is not something anybody falsifies at a glance, while
each of these is one look.

**The tile is the board as it is, not as it will be once somebody has done
the work.** Both figures used to be after-states — `fill/capacity` for the
peg and `on_hand - display_slots` for the bag — so between them they
described a board in a condition nobody had said had been reached. Ticking
the box is what says the top-up happened; the numbers are what it was before.

The fraction was the louder half of that. `fill` and `capacity` are equal on
every peg that is not short, so it could only ever read `2/2` — a constant
wearing the costume of a reading — and `2/2` over `+1` says full and
put-one-out in the same breath. Printing `on_peg` also puts the badge's own
basis on the tile: `+1` is derived from "the app thinks one is hanging
there", so if that belief is wrong it is now visible and `count it` is one
tap away. Advice you cannot inspect is a decision in disguise.

One sold, one on the peg, more in the bag is **situation normal** — a top-up,
not an exception. An emptied bag is what generates a kanban card, and that is
the close's business, not the board's.

**The badge is the work, and for a long time it was a sales counter.** It read
`+11` on Avocado — a hook holding two, nothing in the bag — because it printed
every sale since somebody last tapped that peg, which was ten days and two
weekends earlier and included a bath of five that went out and sold again in
between. Sales are a fact about the peg; they are not an instruction, and a
tile that says `+11` over `0/2` is asking for something nobody can carry.

`restock.refill_plan` is the number instead, and its three bounds are three
ways the old badge lied:

- **The peg.** `fill` caps it. A two-skein hook cannot need eleven.
- **The bag.** Bounded explicitly rather than left to fall out of the
  arithmetic. `fill` derives from `number_on_hand`, so the two normally agree
  — and they come apart exactly when the app believes more is hanging up than
  it believes it owns, which is what a close or a bulk adjustment writing the
  total down with no sale to explain it produces. Then the pegs' want outruns
  what is behind them and somebody is sent to a bag that cannot answer.
- **The colorway.** Sales drain *once* across a colorway's pegs, not off each
  of them. Pastel Rainbow hangs on two pegs and sold two: the old badge said
  `+2` on both and asked for four back.

Across the two live boards that took the total ask from 164 skeins to 69.

A sale is attributed to the first peg, in the order `_allocate` fills them,
that was already stocked when it happened — a sale before a peg's last walk
cannot have come off it, because that walk filled it afterwards. Which of two
pegs gets the badge is arbitrary; the total is not, and the total is what
somebody carries from the bag. A peg nobody has walked has no baseline, is
assumed to be as the app allocated it, and asks for nothing — a quiet tile
still means "checked, nothing to do" rather than "no idea".

The sparse-walk half of this heals on its own as walks get regular, because
the window is the gap between taps. The three bounds do not: they were wrong
at any walk frequency.

**Work wins the tile colour, and won't-fill is not the same question.** The
class used to be `{% if short %}…{% elif needs_refill %}`, so `short`
(`fill < capacity`) took the tile outright — and a peg with nothing on it and
one skein in the bag came out amber, under a caption reading "nothing you do
at the board fixes it". It is a job: put the one out and move on. The two
facts are independent and only one of them is an instruction, so **blue means
there is something to carry from the bag** and amber means there is not and
the peg still won't be full. Nothing is lost by the flip, because the tile's
own `1/2` already says where the work leaves you.

Blue is a transient state and that is the check on it: put the skein out, tap
the tile, and the frozen `expected` makes the next build read 1 on the peg
and 0 in the bag — `put_out` falls to zero and the peg settles back to amber.
A tile that stayed blue after the job would send somebody to an empty bag on
the next pass, which is this same bug wearing the opposite coat.

**The pull list reads `put_out` too.** `_to_bring` took `min(sold, capacity)`
— the peg bound on its own, with the bag and the colorway missing — so it
survived the tile being fixed and went on feeding the picker's unit count and
`pull_list()`, which is the armful somebody physically carries to the stall.
It asked for 49 skeins off the Artisan wall against the tiles' 46. A pull
list is checked by opening the bag, which is the one place an overstatement
is found out with the walk already made, so it is now the same field the tile
prints: one answer, because two is how they disagree.

**The count wins the badge, and `bare` is its colour.** `empty` used to
replace the number outright. That was right while it competed with a sales
figure and wrong the moment the badge became the work: the tile read `empty`
over `2/2` — which is a contradiction to read, since the fraction is what the
peg holds when the job is *done* rather than what is on it now — and it
dropped the one number somebody carries to the bag.

Red also stopped being an alarm. `_drained_at` fires when sales since the
last walk reach what went out on the peg, and with walks ten days apart that
is just "this colorway sold through": **18 of the 39 pegs on the Artisan wall
at once**, every one with two waiting in the bag. So it is a hint about which
peg to do first, not a warning, and carrying it as the badge's colour costs
nothing. `empty` survives only for a peg that is bare with nothing to put on
it — rare, since `bare_since` is computed only when the peg can be filled at
all, but reachable when an earlier peg of the same colorway has taken the bag.

**The finding that actually happens is "it says the bag has some, and the bag
is empty."** Nobody counts a bag of twelve reliably and nothing asks them to.
An empty bag is different in kind: noticed without counting, constant, and
exact. So it is the one-tap exception — and "couldn't fill the peg" is a
*special case* of it rather than a second finding, because a peg cannot fail
to fill unless the bag ran out.

An empty bag bounds the total at what the pegs hold, so every answer where the
bag ends up empty is a button (0, 1 or 2 on a two-skein hook), and past that
there is a bag and no ceiling, so it is typed.

**Both controls mean the same thing — how many there are altogether — and the
app splits it**: pegs first, remainder to the bag. That is what a person does
with an armful of skeins.

Worth knowing what this replaced, because the wrong version reads perfectly
well. The box used to ask "how many are in the bag" and add `display_slots` on
— which assumes the peg started full. A peg at **1 of 2** breaks it: what you
find goes *onto the peg*, the bag stays empty, and the total comes out one
over. Asking the total and deriving the halves has no such gap.

**Direction is never a button**:
it is the sign of the delta, because a button naming it could disagree with
the number typed under it and then one of them is wrong with nothing to say
which.

**Adjustments are per product, never per peg.** A colorway on three pegs
raises one correction; by the second peg there is nothing left to fix, which
is why a repeat is a no-op rather than three adjustments for one discovery.

### It is not a task master

**A walk covering 23 of 40 pegs is accepted whole, and nothing scores it.**
Refusing it loses 23 real answers or buys 17 manufactured ones from somebody
tapping through a validator — far more expensive than the peg nobody looked
at. Unanswered pegs keep their older baseline, which `last_walked` handles per
position.

**Nothing counts walks, tracks a skipped peg, or reports completeness.** How
often a board gets restocked measures nothing worth knowing — five passes in
five minutes is a good afternoon — and there is deliberately no "17 still to
do" anywhere. `RestockPassAdmin` is read-only and a pegs-per-hour column would
change what the page is for.

**A full check is also the reset**, and that is by design: every peg gets a
fresh baseline, so every badge clears at once. It is what open and close are
for, and it is also the honest way to quiet a board that has got noisy.

Which is why there must be **no "check all" button.** A full check is the only
claim on this page anybody could make falsely — tap everything, the board goes
quiet, and nothing was walked. What stops that is that it costs forty
individual taps with a name attached. One button would make the same claim
free, and it is precisely the convenience somebody reasonable asks for after
the third morning. The cost *is* the evidence.

The one thing said out loud is **a spot with no scarf on it and something
behind it to fix that with.** That is yarn that could be selling and isn't,
which is the only version anybody cares about. The badge goes red and the
picker counts how many — a state and a count of work available, with no
threshold and no escalation. Whether it matters depends on how busy the stall
is and whether anyone is free, neither of which the app can see, so it states
the fact and a person decides. Same rule `colorbands` follows.

**Bare is "nothing out", not "cannot be filled", and those were one test.**
It used to key on `not short` — `fill == capacity` — so a peg with nothing on
it and one skein in the bag was denied the badge purely because the bag could
not fill it to the top. Wrong question: *one of two on a hook is fine,
because there is something there and a customer can see it and buy it.* None
out is a bare spot whatever the hook holds. Three pegs on the Artisan wall
were in that state reading as ordinary work, and at capacity one it is the
whole rack — a veil spot that sold its scarf is empty, and 24 of 42 were.

Still only where something can be done about it: a spot with nothing on it
**and nothing behind it** stays amber, because that is not yarn that could be
selling, it is a decision about what gets dyed, and nothing carried to the
board fixes it.

**The state is its own field, because the timestamp could not carry it.**
`bare_since` is a *moment*, and `_drained_at` has no sale to point at when
the peg was already empty at the last walk — so reading the state off the
timestamp dropped exactly the pegs that had been bare longest. `bare` is the
fact; `bare_since` is the `?bare=1` half and is allowed to be null under it.

**How long it has been bare is behind `?bare=1` on both pages, advertised
nowhere.** An elapsed time on a peg reads as a stopwatch on whoever is
walking, however carefully it is worded — on the one page in the app that
deliberately scores nothing, `empty 6 hours` is a number with somebody's name
beside it. And it mostly is not measuring what it looks like: the clock starts
at the last walk, so a long one usually means nobody has been round with the
phone, not that a peg stood bare all afternoon. Accusing *and* wrong is worse
than absent.

Unlike `?photos=1` it is **not** folded into `mode`, so no link off the page
and no POST redirect carries it. That inversion is the point: a mode should
follow you round a circuit, and this should evaporate the moment you stop
typing it, because a link sent mid-walk or a bookmark taken during a demo is
exactly how it gets back in front of the crew. `board()` computes `bare_since`
either way — `board_status` needs it to count bare pegs, and that count is not
a time.

### No JavaScript, and no htmx either

A tile is a `<label>` wrapping a checkbox and the tick shows through
`:has(input:checked)` — the same mechanism as the close's count buttons and
the booth form's reason toggle. A tap-per-peg htmx call would put a network
round-trip behind each of forty-odd interactions on a phone at a stall on one
bar, and a tap that silently fails to reach the server is a peg somebody
believes they reported. One form, saved whenever, saved partially as often as
they like. An unticked peg is "not walked yet", never "empty".

The **map editor** is the exception. Drag-and-drop over the picture is a desk
job on wifi, and that is where JavaScript earns its keep — the no-JS rule is
about the field, not about the app. Until then the grid is hand-built in the
admin inline, because the board gets built once and then barely changes.

### Copying a layout, and where not to

The four yarn boards are one pattern repeated per base: Heavenly's r3c4 and
Homespun's r3c4 carry the same colorway. So the second, third and fourth
boards are the first one retyped, and `copy_board_layout --from … --to …`
does it — same peg, same colorway, other blank.

**Only where the pattern really is shared.** The silk racks are arranged by
what looks right next to what, and copying onto one produces a
plausible-looking layout that is wrong everywhere at once — harder to spot and
undo than an empty board.

Two refusals in it are the point. **Nothing is created**: a colorway the
target blank doesn't have yet is named and its peg left empty, because
inventing the product means inventing a price. And **occupied pegs are left
alone** unless `--overwrite` is passed — a half-laid-out board is usually
somebody's work in progress, and it is the one mistake here the editor can't
undo.

### `display_slots` is written by the map

The map is the source; `display_slots` is what everything reads. One writer —
a `post_save`/`post_delete` signal on `DisplayPosition` — rather than two
numbers that agree until somebody edits one. Same bargain `save()` makes with
SKUs.

A product on no fixture keeps whatever capacity it had rather than being
zeroed, because zero means "never goes on display" and would quietly drop it
off the close, which is a different claim from "nobody has mapped this yet".

### Which colorways belong on a board is the mapper's call

`unmapped_for` lists a board's blank's colorways that have no home anywhere,
and it appears **on the editor and nowhere else** — not on the crew's board,
not on the picker. Deciding what ought to hang on a board is somebody's
decision, and putting that list in front of the crew tells the wrong people
about work they have no part in while quietly asserting the app knows what
should be there.

A **mixed board gets nothing at all.** Without a blank there is no such
question to answer, and inventing one would be the app claiming
responsibility nobody gave it — the scarf rack is a row per scarf type, and
which colorways belong on it is not derivable.

It is offered rather than warned about: colorways with none on hand are
tagged as such, because "hang it" and "dye some first" are different jobs.
