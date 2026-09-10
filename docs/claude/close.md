# The Sunday close

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## The Sunday close: the app's empty bags against the tags in hand

`secret/close/` is the end-of-weekend check. The crew keep a product's kanban
tag when the last of it leaves the bag and goes onto the display — **a
statement about the bag, not the shelf.** The app's version of the same
statement is `number_on_hand <= display_slots`, and that is what puts a product
on the list.

Three situations, one shape of answer:

| Situation             | The act                                  | The number   |
|-----------------------|------------------------------------------|--------------|
| tag in hand           | bag's empty — count the display          | `0 … slots`  |
| no tag                | **fill the display**, then count the bag | `slots + rest` |
| tag nobody predicted  | count the display, same as the rest      | `0 … slots`  |

`scarves/closing.py` holds all of it. **Every answered row is a count and the
count is the total.** The tag is no longer the answer; it is what puts the
product in front of somebody. On a phone that costs about what a tick cost,
because the buttons only run as high as the display holds — and a count that
runs past the last button *is* the news that there was a bag after all, which
is the only thing the free-text box is for.

**This replaced "tag in hand means set it to zero", which wrote off the one to
three units still hanging on the pegs.** That is the mechanism in the section
above, wearing its everyday clothes: do it all season and stock moving from
bag to peg reads as sales. `test_counting_the_display_does_not_write_off_what_is_hanging_there`
is the pin.

**Filling the display is part of the count, not a separate chore.** The
restocking and the measurement are the same act, which is what makes the
no-tag answer honest and what leaves the stall full for next weekend. It is
also why this is a *closing* task: the ritual is what makes `number_on_hand <=
display_slots` mean "the bag is empty" all through the following weekend.

**The outcome is the sign of `counted - on_hand_before`**, not what was in
whose hand. So a predicted row can come out as an overcount, and an
unpredicted tag can turn out to agree. Whether a row was predicted is a
separate axis, `CloseRunRow.added_by_tag`, and conflating the two used to put
the wrong thing into the one number this page produces.

**The trigger catches drift earlier than the old one did.** Zero used to be
the trigger because the sale clamp made it discrete. Now a drifting row is
caught when it crosses into the display band — while the pegs are still full
— rather than once the shelf is bare. The clamp is still there as a backstop.

**An overcount is the half the trigger can never find on its own.** An
overstated row never falls into the band to be checked, which is exactly the
shape a swapped sale leaves behind. The unpredicted tag is what finds it, and
it doubles as a **webhook health check**: a dropped sale physically becomes a
tag in somebody's hand about a week later, so a dead integration shows up here
with no cross-check against Square at all.

**`display_slots` is gated on, `par` is not.** Par is a production number and
has no business deciding what gets audited — a colorway nobody plans to make
again can still be on the pegs this weekend. Zero slots means it never goes on
display, so no tag will ever come up for it and the close leaves it alone.

**Absolute counts, never a rate.** Ten corrections in a weekend is ten
corrections whether the list was twelve products long or two hundred.
Nothing computes `4 / 50`, because putting the reassuring number beside the
actionable one is how the actionable one stops being read. The agreements are
stored — an answered row has to stop coming back at somebody working down a
pile — but they are not a denominator, and `tally()` deliberately has no
`rate` key. The two directions are also never summed: a bad intake would
cancel out a dead webhook. **And there is no key for displays left short**,
for the reason in the section above: a number here would be acted on whatever
the caption said.

**An unpredicted tag moves nothing on its own.** It adds the row and stops;
the same count everything else gets is what settles it. The old version
adjusted straight to zero on the strength of the tag, and needed a special
case for "already at zero, so the tag agrees" to avoid booking a fault in the
direction that reads as "the till is losing sales". Nothing is guessed at any
more, so the special case is gone.

**An agreed answer comes back off, and a correction doesn't — by different
means.** A row counted at exactly what the app already believed moved no
stock, so it stays open all evening and a new number simply replaces it. The
worked case is a bag turning up under the table at seven that was counted as
gone at four.

A row that *moved* stock is reversed by an explicit **Undo** button, never by
retyping. The reversal has to write a compensating entry, which a number typed
over the top of another has no way to express — and an undo has to be
something somebody meant rather than a side effect of working down the list
again.

**Undo needs no account, and that is the point.** The rule here used to be
that a movement could only be reversed through a bulk inventory adjustment —
which quietly assumed the person holding the phone had a staff login and knew
where that screen was. The crew have neither. What it produced in practice
was an employee who mis-tapped, couldn't fix it, and had to go and tell
somebody. That is a data-quality problem before it is a kindness one: the
cost of admitting a mistake is exactly the pressure that gets one left
unmentioned, and an unreported wrong count is the failure this page exists to
catch.

Nothing is erased. The original `InventoryLog` stays and a compensating entry
is written beside it, so the ledger says a thing happened and was put back —
which is what happened. The reversal is an inverse *delta*, never a restored
absolute, because a sale can land between the mistake and the noticing;
`set_on_hand` clamps at zero so the arithmetic degrades the right way. Scope
is narrow on purpose: it reverses **this close's own movement** on **this
close's own day**, and nothing else. Correcting anything older still goes
through a bulk adjustment.

A found bag lands as `missing`, and the label repays a careful reading: the
tag *was* in hand, so "no tag" is not literally what happened. What the
outcome records is the **direction** — the app was under, the
stock-arrived-unrecorded end of the pipeline — and the direction is what is
being counted.

### The close is also the stockout measure

A `CloseRunRow` answered at **`counted == 0`** is the record that a product
was sold out on a Sunday night. That is the measure of a stockout in this
shop, and it needs nothing built to collect — the close already produces it
every weekend as a by-product of truing the count.

**It counts weekends, not units, and never a rate.** One `(product, close)`
pair at zero is one observation. A colorway sold out across two weekends
counts twice, and **that is right whether or not a bath was run in between**:
the customer could not buy it on either night, which is the whole of what is
being measured. A restock that sells out again is a better *outcome* than
making none — more was sold — but it is not a different *reading*, and trying
to separate them would mean estimating what would have sold, which nothing
here can know. Same bargain as "absolute counts, never a rate" and
`closing.tally()` having no denominator.

**Only an answered row counts.** A walk that covered 23 of 40 pegs leaves the
rest `pending`, and a pending row is "nobody looked", never "not sold out" —
so the count is over rows that came back, and the silence stays silent rather
than being read as a zero in either direction.

**It cannot be backfilled.** A close is a physical count made on the night,
and `CloseRun.day` locks at midnight, so a weekend nobody closed is a weekend
whose stockouts are simply not knowable. That is the cost of the measure being
free, and it is the reason the ritual matters more than any report built on
it: the series starts at the first close somebody actually does.

This is deliberately **not** derived from `number_on_hand` hitting zero in
`InventoryLog`. That number is the app's belief, which is exactly what the
close exists to check — reading the stockout off it would be the app marking
its own homework, and the sale clamp means it reaches zero and stays there
whether or not anybody looked at the peg.

### One table at a time

`?category=` narrows the close to one of the shop's tables — Yarn, Silk,
whatever `RawProductCategory` holds — because that is the axis the evening is
physically walked on. The yarn boards are one circuit and the silk racks are
another, and a forty-row list that mixes them makes somebody standing at one
read past the other on every pass down the pile.

**It filters the reading and never the run.** `sync_expected` still folds in
every emptied bag, every row stays on the close, and nothing about what gets
asked about changes. That distinction is what makes it safe to hide rows on a
page whose whole job is to be complete — and it is only safe because of two
further things: **the pill for the table nobody selected still counts what is
left on it**, and a filtered list says out loud how many rows it is leaving
out. A page reading as finished while a table's worth of rows has never been
asked about is exactly the silence the "still to count" list exists to break.

Hiding is structurally safe for the reason a half-worked close already is:
`counts()` records only the rows somebody answered, so a row absent from the
POST is one nobody touched, never an answer of any kind.

**Every count is scoped to what is on screen**, including the score in the
banner — `tally()` takes an optional row subset for this. Seven put right,
printed over a list of three, is the page contradicting itself; the number
people act on is the one beside the list they are reading. Same call
`private/colors/` makes about its pills.

The choices are derived from the rows, not from a list of names, so a shop
that grows a third table gets a third pill with nothing to change. **A run
whose rows all sit on one table draws no filter at all** — a filter offering
one choice is furniture, the same reason the recipe page draws no product
chips for a colorway dyed on one blank.

Two things carry it and one thing overrides it:

- **It rides alongside `mode=` and every action carries it back** — saving
  counts, undo, the tag search. A close is worked in several passes across an
  evening, so a submit that landed back on the other table would cost a tap
  every time. Undo and add-tag post to their own URLs and have no query string
  to inherit, so the forms carry a hidden field. It is named rather than
  numbered (`?category=Yarn`) so a reading is a link somebody can read as well
  as send, and an unknown name is no filter rather than an error.
- **`?answered=1` is still a reveal and the pills do not carry it.** A pill
  that dragged the drawer along would put the long list back on the table
  somebody just switched to.
- **A hand-added tag is on every table.** The unpredicted-tag search is over
  the whole catalogue, so a silk scarf found at the yarn boards is an ordinary
  thing to add — and being made to switch tables to answer a tag you are
  holding is the app arguing with the person who found it. A row somebody put
  there on purpose isn't on a table at all, it is in their hand, so
  `in_category` keeps `added_by_tag` rows whatever is selected. The cost is
  that `All` stops equalling the tables summed once an unpredicted tag exists,
  and that is the right way round: each pill promises exactly what its own
  list holds, which is the count somebody can check by looking.

### The counting list: what's left, and the search

**Answered rows come off the counting list.** What somebody is looking for
here is the next thing they have *not* checked, and on a list twenty-three
long a settled row between two unsettled ones has to be read in order to be
skipped — a cost paid on every pass down the pile, and the pile is worked in
several passes across an evening. So the list shows what is left, says how
many are behind it, and `?answered=1` opens the drawer. Undo is in there, so
the link says so: a fix nobody can find is a fix that gets left unmentioned,
which is what the undo button exists to prevent.

A **reveal, not a mode** — nothing carries `?answered=1` onward, so it
evaporates on the next submit or link, the same inversion `?bare=1` makes on
the restock board. Two exceptions, both because there is nothing to focus on:
a finished day is all record, and **a rejected answer always reveals
everything**, because a form that comes back with its error hidden reads as
one that saved.

Hiding is safe because an absent field is already how a half-worked close
works: `counts()` records only the rows somebody answered, so a row that
isn't in the POST is one nobody touched — never an answer of any kind.

**The tag search is an htmx submit, and the reasoning that made it a page
load is only half-wrong.** The recorded argument was that this runs in a
field on one bar, and a search box that silently does nothing when a request
is dropped is worse than one that visibly reloads. That half stands, and is
kept: an indicator while the request is out, and a sentence naming a dropped
request — because a search that didn't arrive otherwise looks exactly like a
search that found nothing, which on this page reads as *the scarf isn't in
the app* and sends somebody off to solve the wrong problem.

What the argument never weighed is what the reload costs on the page it is
on. The box sits under twenty-odd rows, and a full navigation throws away the
scroll position, so every search was paid for with a scrub back down the
page. That is paid every time; a dropped request is rare and is now visible.

Still **submit-only, never a type-ahead** — the original objection applies
with full force to a request nobody asked for. Without htmx the form is the
ordinary GET it always was, `q` lands in the URL, and the page renders
`partials/close_tag_results.html` inline — the same partial the fragment
returns, so the two cannot drift into the swapped copy posting somewhere the
inline one doesn't.

**Adding a tag is a swap too, and the argument that kept it a navigation was
half-right in the same way.** It was: the row has to visibly appear in the
list above, and a page that came back looking unchanged is how the same tag
gets added three times. What that never weighed is what a redirect costs
*here* — this is late in a long evening with the counting form above
half-filled, so it threw away every answer typed but not yet saved and landed
somebody at the top of a page they were at the bottom of. Paid on every tag.
The double-add it was guarding against was never really its doing anyway:
`closing.add_tag` hands back the row that exists rather than making a second
one, so a repeated tap is a sentence, not a duplicate.

So the row arrives **beside the search that found it**, appended to
`#added-rows` directly under the results, and nothing above is re-rendered —
which is the whole of why nothing above can be lost. **Alphabetical order is
given up to do it**, deliberately: a row appended to the far end of a
twenty-row list is two screens from the person who just asked for it, and the
next page load sorts it back into place.

The mechanism is an HTML `form=` attribute, not a script. Every count field
carries the counting form's id (`build_close_count_form_class(rows, form_id)`),
so a row rendered outside that form still submits with it. **The counting
form is rendered even when there is nothing to count**, because "counted the
list, now working through tags" is an ordinary state and a `form=` naming a
form that isn't on the page names nothing.

**One Save, fixed to the bottom of the screen.** There were briefly two — one
under the counting list and one under the added rows — which read as two
forms and never were. The second existed because the first was a scroll away
from every row above it, which is the actual complaint; a button that follows
the screen answers it and makes the copy unnecessary. The bar sits after
`#added-rows` in the markup so `#added-rows:empty + .savebar:not(.always)`
can drop it when there is genuinely nothing to save, `always` being set
whenever the counting list has rows.

Three things ride back out of band: the notice, the table pills and the "N
left to count" line. The last two are above the fold and would otherwise sit
there contradicting the list — the same rule the recipe page's figures
follow. Both containers render unconditionally, empty if they have nothing to
say, because an out-of-band swap with nowhere to land is dropped silently. A
dropped add says so on the page for the reason a dropped search does, and
more so: this one has a row riding on it.

### Two readings: counting, then the cards

The question a close *ends* on is not the one it is worked on, and `?mode=`
says which one is on screen. **`count`** is the evening's work — one question
per row, down a physical pile, undo, and the unpredicted-tag search.
**`cards`** is what the evening leaves in somebody's hand: the stack of kanban
tags that should now exist, against the ones that go back in a bag.

**A bare URL opens `cards` once anything has been counted**, and always on a
finished day. That is the moment the page's usefulness changes hands — before
the first submit there is no stack to check, and afterwards the stack is the
only question left, which is what a link off `private/closes/` is following.
Every action on the counting half redirects back carrying `mode=count`
(including the tag search, which is a GET), because a close is worked in
several passes across an evening and dropping into the summary after each
submit costs a tap back every time. "Carry on with today's close" on the
picker points at `count` for the same reason.

**The card lists are blind to what the true-up did**, deliberately. Which way
the app was wrong is the counting list's business and `close_history`'s
output; a stack of forty tags is checked by name. For the same reason they do
**not** split on `added_by_tag` — whether a row was predicted or turned up as
an unexpected tag says where the evening's work came from, not what should be
in the stack at the end of it, and reading the stack through that split asks
somebody to do the subtraction in their head while holding the cards.

So `closing.card_status()` applies **one test to every answered row alike,
live**: `number_on_hand <= display_slots`, which is the app saying every unit
is hanging on the pegs and the bag behind them is empty. That is the same
claim `expected_products()` opened the evening by predicting, asked again of
the numbers the close has since corrected. Exactly on the display is a card —
nothing is behind it.

`display_slots` is the **row's frozen copy**, not the product's live one, for
the reason it was frozen: the pegs held what they held that night, and a board
rebuilt on Monday must not retrospectively change which tags were in a hand on
Sunday.

**A row nobody counted is a third list, not a marked-up card.** Its number is
the app's belief rather than that night's finding, so filing it under a card
either way would put an unchecked claim into a list whose whole job is to be
checked — the stack would agree with itself. And the two are different jobs:
finishing the counting is work remaining, settling the stack is what the
counting was for, so *Still to count* sits apart with a link back into
counting. It stays on the page rather than being dropped, and its count is
repeated in the header, because two card lists that read as complete when
they are short is exactly the silence this page exists to break.

### A run is a calendar day, and yesterday's is a record

`CloseRun.day` is unique. Reopening the page the same evening lands back in
the same run and picks up where it stopped, which is what the job needs — it
happens in a car park in the dark and gets interrupted. Come back tomorrow
and nothing on that day can be counted or adjusted any more; a correction goes
through `private/bulk-inventory/` with a reason attached.

There is deliberately **no finish button**, because the button is exactly
what doesn't get pressed: the van gets loaded, the phone goes in a pocket,
and a run left open forever reads identically to one that found nothing. The
day is the boundary instead, and rows still `pending` when it ends are what
distinguishes "got through the pile" from "walked away" — which is why the
history page has a column for them.

`is_open` compares against `timezone.localdate()`, so a run locks at midnight
rather than when anyone leaves. Considered and accepted rather than softened
to a 4am cutoff: still packing at midnight means several other things have
already gone wrong, and the cost is only that the next run rechecks what was
already checked. The truing-up itself is unchanged.

Expect the page to be worked in **several passes across an evening** — four
o'clock, seven, and once more before leaving — rather than in one sitting.
That is the usage the row states and the growing list are shaped around, and
it is what caught the prefetch bug below.

**The expected list only ever grows.** `sync_expected` folds in newly emptied
bags each time the page is opened, so a close started at noon still asks about
the scarf whose bag ran out at four. Rows already on the run are never removed or
rewritten — `on_hand_before` is what the disagreement was measured against,
and re-reading it would read back the number this close already corrected. A
row freezes `display_slots` for the same reason the production sheet freezes
its bath size: the buttons said "0, 1 or 2" because that is what the pegs held
that night.

**Read the rows live — don't `prefetch_related` them on the way in.** The
sync adds rows *after* the run is fetched, so a prefetch cache built at that
point won't contain them, and the new products stay invisible until some
later request happens to rebuild it. That is the failure the sync exists to
prevent, wearing a disguise: the page reads as a complete list, and the scarf
that went at four o'clock is simply never asked about. This is a real bug
that was caught by working the page in three passes across one evening, which
is how it actually gets used. `close_history` still prefetches, correctly —
it reads past runs and mutates nothing.

**This is a record**, and so, now, is a `ProductionRun` — the comparison that
used to sit here had the close as the exception. Here the count of things
found wrong *is* the deliverable, which is why the rows are kept, why the
admin is read-only, and why nothing deletes. The production sheet arrived at
the same place by a different route: what a session was asked to do, and what
it found, are answerable from nowhere else.

### It never reaches Square

The close runs at a field on one bar of signal. A step that needs the network
is a step that sometimes doesn't happen — the same reasoning that keeps the
booth form's toggle in CSS, and why the unexpected-tag search is submit-only
and never a type-ahead (see *The counting list* below for what that search
does do, and why it stopped being a page load).

Reconciling against Square's own counts is a desk job for afterwards, and
doing it *first* would be worse than not doing it at all: `_push_inventory`
sends `PHYSICAL_COUNT`, an absolute set that overwrites, so a push makes app
and Square agree by construction and every close comes back clean — which
reads like good news. If anything is ever scheduled to run `sync_to_square`
on a timer, check it can't fire between the weekend and the close.

Run `import_square_sales` **before** any physical true-up, never after.

The worked case is a day the webhooks were down while Square itself was fine:
no sales recorded, no bag ever reads as empty, and the crew finish the day
holding a fistful of tags for products the app still believes have backstock. The
recovery is import first, then close — and `WebhookOutageRecoveryTests` pins
both halves of what happens if you don't.

**Stock survives either order**, because `set_on_hand` clamps at zero. What
doesn't survive is the ledger: close first and the same physical sale is
booked twice, once as a `sunday_close` adjustment and once as a
`square_import` sale. The import's dedupe cannot prevent that — it matches on
`sale_reference`, and a close adjustment has none, because the close was
never told an order id. Teaching it to guess would mean suppressing a real
sale whenever the guess was wrong, which is the more expensive error.

One consequence worth expecting: done in the right order, a catastrophic day
shows **zero disagreements** on the close, because the import had already
booked the sales before anyone counted a peg. The incident is not lost — it is
in `InventoryLog` as a run of `square_import` rows on a day that normally
carries `square_webhook` ones, which is `source` doing precisely the job it
was added for.

### An adjustment is a measurement, not a confession

The `InventoryLog` is **not a record of mistakes to be kept short.** It is the
only account of how stock actually moves through this shop, and much of that
movement is genuinely unknown until it shows up there — a hundred scarves
fancied without a word, a colorway that quietly walks off the rack, a webhook
that stopped delivering in June.

This has consequences for anything built on top of it, and they run against
the instinct:

- **Never design to minimise adjustments.** A flow that produces fewer
  correction rows because it made correcting harder has not improved the
  stock, it has hidden the movement. Make recording cheap instead — the
  restock walk's one tap, the fancy page's one number, the close's buttons.
- **Never put friction in front of one to discourage it.** The Undo button
  needs no account for exactly this reason: the cost of admitting a mistake
  is what gets one left unmentioned, and an unmentioned movement is the only
  kind that is actually lost.
- **Nothing deletes an `InventoryLog`.** An undone mis-tap keeps both rows,
  because the wrong number really was live for that window and something
  could have read it. A ledger that never mentions the excursion answers "why
  did Square briefly show 50 of these" with silence.
- **Read it as a dataset, not a scorecard.** `source` exists so the flows can
  be counted against each other; `closing.tally()` counts what a close put
  right. Neither is a grade. This sits alongside "absolute counts, never a
  rate" rather than against it: the rate was refused because a denominator
  buries the actionable number, not because the numerator is shameful.

The story the log tells is worth more than a tidy one would be.

### `InventoryLog.source`: provenance you can count

Provenance was already being recorded, in `notes`, as a readable English
sentence — and that stays, because a person reading one row wants the
sentence. But *counting* rows is a different question, and answering it off
prose means a `LIKE` over wording nothing promised to hold still: one
reworded message drops rows out of a total with nothing to show it happened.

So every `InventoryLog` names the flow that wrote it — `sunday_close`,
`bulk_update`, `production_sheet`, `square_webhook`, and the rest. That is
what makes "are the close's corrections going up or down, and how do they
compare with the ways stock is meant to move" a `values("source")` away.
`InventoryLogSourceTests` walks the AST of every module that creates one and
fails on a site that forgot, because a missing source doesn't error — it just
drops out of every total, which is the same silence the notes-matching had.

Blank means "predates the field", and it was deliberately **not** back-filled
by pattern-matching the old notes: a guessed provenance counts identically to
a recorded one, and there would be nothing on the row to say which it was.

**Auditing `sunday_close`: sum the quantities, don't count the rows.** An
undone mis-tap leaves a matched pair that nets to zero, so a sum is already
the right answer while a row count reads two corrections that never happened.
The count of things a close actually put right lives on `CloseRunRow`, not
here — `closing.tally()` is what answers that, and an undo removes the row
from it.

Keeping the pair is deliberate, against the intuition that a mistake
corrected in ten seconds was "zero events". It wasn't zero events to the
*system*: the wrong number was live for that window, and a Square inventory
push, a production sheet or another phone could have read it. A ledger that
never mentions the excursion leaves "why did Square briefly show 50 of these"
with no answer anywhere — the silent kind of wrong. Nothing else in this app
deletes an `InventoryLog` either.
