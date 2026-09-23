# Production: what to dye, the sheet, and the scan back

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Two signals propose, one claim decides

**Read this before adding anything that plans a dye bath.** There are two
answers in this app to "what should we dye", they are both right, and the
thing that stops them fighting is not a rule about which one wins.

- **Par shortages.** `private/production-needed/` and
  `private/production-sheet/` — what is below par, ranked on season sales,
  printed as a dye-room worksheet. The sections below are all about this one.
- **Sunday night's cards.** `private/production-from-close/` — the stack of
  kanban tags the close leaves in somebody's hand, made into a list of baths.
  `scarves/closeplan.py`.

**The close is the shop's own signal and it predates the app.** The crew walk
the display on Sunday night, count what is there, and the evening ends holding
a card for every product whose bag is empty. That stack is the week's work
order; everything gets dyed against it and the week ends with *which of those
did I make*. The par machinery was the app proposing a different loop — and
par was never dialled in, so it asks somebody to trust a number she did not
choose about a shelf she walked past twelve hours ago. It went unused. The
close had already been built for the stock count, and it turned out to be the
model for how the work actually happens rather than one more report.

So both exist, and **the reason they don't compete is that neither of them is
the claim.** A `ProductionRunRow` is: one row, one bath, matched on finished
product, whoever wrote it. A card that lands on any list is accounted for and
drops off the pool; a colorway already out on a par-based sheet never enters
it; `production.in_flight` subtracts a close list's pending baths from the
sheet picker without knowing where they came from. **However it got onto a
list, it is accounted for** — and that sentence is the whole division.

Two consequences to hold onto:

- **Don't add a third planner that keeps its own book.** Anything that plans a
  bath writes a `ProductionRunRow`, or it will quietly plan what somebody else
  already planned. The failure is silent and lands in the dye room.
- **Don't make the two signals into modes.** Neither page hides the other and
  neither one switches the app into a state; both link to the other and say
  what the shared claim does. A mode would make somebody pick a loop before
  knowing which one suits the week, and the answer is genuinely both — par at
  a desk in the off season, cards in the nine weeks when the stall is open.

The one thing that *is* a stored choice is **paper or not**, and it is per
list rather than per person — see *A list is paper or it isn't* below.

## `private/production-from-close/`: Sunday night's cards into baths

`closeplan.cards(close)` is the stack; the page puts a number box beside each
and makes a `ProductionRun` out of what gets typed.

**The pool is frozen at the close.** A card is an answered `CloseRunRow` whose
`counted` came in at or below the `display_slots` that row froze that night —
the app's own words for an empty bag, asked of a number somebody physically
counted.

This is deliberately **not** `closing.card_status()`, which asks the same
question of the **live** `number_on_hand`. Two questions:

| Function | Asks | Reads |
|---|---|---|
| `closing.card_status` | what should be in the stack *now* | live `number_on_hand` |
| `closeplan.cards` | what did Sunday *find* | the row's `counted` |

The live test is right for somebody holding the cards at the end of the
evening. It is wrong here: dye a bath on Wednesday and it flips, so a live
pool would drop rows out from under a half-made plan with nothing anywhere to
say whether a missing card was made, claimed, or never in the stack. Anybody
"fixing" one of these to match the other has broken the other one.

**Pending rows are not cards.** A walk that covered 23 of 40 pegs leaves the
rest unanswered, and an unanswered row is "nobody looked" — never a zero. The
page says how many were never counted and links back into counting, because a
plan off a half-worked close is short by however many pegs got skipped and a
list that reads complete is the silence the close exists to break. Same
refusal `production.stockout_baths` makes.

**Three kinds of card can't go on a dye list**, and they are the same three
`candidates()` refuses: an undyed passthrough (ordered, not made), a fancy
veil (line work on a scarf that already exists), and a retired colorway
(nobody dyes it any more). Those are real cards — the thing genuinely ran out
and the tag is genuinely in a hand — they just cannot be answered by heating
anything. A passthrough only reaches a close through the unpredicted-tag
search, since `expected_products()` already excludes a null recipe, and that
path stays open on purpose.

### The order is the shelf's, not par's

**Empty pegs and last-one-hanging first, then what the colorway sold this
season.** `CRITICAL_AT = 1`, so a count of 0 or 1 leads whatever anything
sold; everything else is ordered on `slowsellers.sold_by_recipe`, the same
function `production-needed` reports from and `candidates()` ranks on.

The band is about the **shelf**, not a fraction of it: `counted` is the total,
display plus bag, so 1 means the last one is hanging there and 0 means it has
gone. Two on a four-peg hook is an empty bag and is *not* critical. A third
value would start being a judgement about how much stock is enough, which is
par's job and par is the number this page exists to route around.

Both numbers print on the card, because a ranking nobody can check by looking
is a ranking they have to trust — the same call `production-needed` makes with
its sold count.

### One close, several lists

Take five cards onto list A and those five are gone. Ten more onto list B, the
balance onto C. `closeplan.claims` is what does it, and it differs from
`production.in_flight` in one deliberate way:

- **`in_flight` counts pending rows only.** Its question is "how much is still
  out being dyed", and an accepted bath has already landed in
  `number_on_hand`, so counting it would subtract it twice.
- **`claims` counts accepted rows too.** Its question is "is this card dealt
  with", and a card she made and reported on Friday is the most dealt-with a
  card gets. Counting pending only would put every finished bath back on the
  pool the moment it was reported, which is the opposite of what reporting
  means.

**Cancelled rows release the claim** in both, which is the same promise the
crew's *not coming* button relies on: the bath never ran, so the card goes
back in the pool.

**The window starts at the close's own day.** A list made last Tuesday was
answering last week's walk, and a card that came up again on Sunday came up
again for a reason — the shelf was counted and it was still empty. The
boundary is the day rather than a timestamp because a close *is* a day
(`CloseRun.day` is unique and locks at midnight), so a list planned that same
evening has to count.

**A claimed card is listed, never hidden.** It sits in its own section under
the pool naming the list it is on, because a card missing with nothing said
reads exactly like a card that was never in the stack. And **adding one back
deliberately is allowed** — *nothing gets planned twice unless she added it*
is a statement about the default, not a refusal, and `make_list` checks
nothing.

### A list is paper or it isn't, and that is asked once

`ProductionRun.reporting` is `paper` or `direct`, chosen when the list is made
and stored on the run.

**The reporting flow is identical either way** — the same rows, the same three
end states, the same `accept_line`, the same page at
`secret/production/<token>/`. What the mode decides is which door the run page
*leads with*: paper offers the printout first, direct offers "say what you
made" first. The other door stays available underneath, because a paperless
list that turns into a three-day session at a sink still has to be printable,
and a printed sheet whose paper got lost still has to be reportable at a desk.

What is not offered is **two equal buttons**, and that is the whole point of
storing it. A page presenting a printout and an on-screen report side by side
asks somebody to decide again every time they open it, and the decision was
already made when the list was created. Default is `paper`, because every
sheet made before this existed was one.

### "If I made more, let me say so"

Two ways, and they are for different things:

- **`another`, beside each row** (`production_run_add_bath`) — the list said
  one bath and the pot ran twice. One click, on a row already on screen with
  its name on it. Offered on accepted rows as well as pending ones, because
  finding out you ran a second pot happens after reporting the first as often
  as before.
- **The catalogue search** — a colour that was never on the list at all.

Both **append a row of one bath** rather than growing an existing row's
quantity, for the reason every row is one bath: three baths where one pot
failed is `5, 5, 0`, and a single row of fifteen cannot say it.
`production.lines_for` folds them back into one question, so the reporting
side sees one colorway with two baths on it — which is what somebody is
standing in front of.

**Reporting still cannot claim more than the list asked for.** `apply_row`
clamps `yielded` to the row's quantity and that stays: a number above the
bath size is somebody answering a different question, and at a sink it is a
typo. "I made more" is a change to the *plan*, made with one of the two
buttons above, and then reported — which is why neither of them is a bigger
number in the yield box.

## `private/production-needed/`: ranked on sales, because par is the broken number

The list of what is below par, grouped by colorway, with a button that books
a bath straight into stock. **It is ordered by units sold this season, most
first**, and that default is a statement about par rather than about sorting.

Par was never dialled in — it reads as a uniform remnant across the
catalogue, not as forty-odd decisions about demand — so ordering this page by
shortage ranks it on a number nobody chose: a colorway that sold three all
season comes out above one that sold forty, purely for crossing an arbitrary
line first. **Sales are measured.** Until par means something, they are the
better claim on a dye pot.

Getting par right is the actual project (see *Display capacity is not demand*
for what par is *for*, and `private/slow-sellers/` for the evidence being
gathered). This ordering is what makes the list usable in the meantime, not a
replacement for that work.

**Par is changed at `private/recipes/<pk>/?par=1`** — a box per blank on the
recipe page, described in `docs/claude/recipes.md` under *Editing par*. That
is the door, and it is the only one besides the admin. When par starts meaning
something this page's default ordering is the thing to revisit; until then it
still ranks on sales, because the ordering is a statement about how much par
has been dialled in and not about how much it *could* be.

**Use par is one click away, and neither ordering filters.** Every colorway
is listed either way, so the choice changes what gets read first and never
what exists — a page that hid the quiet ones would be making the retirement
decision on its own. The third pill, *Par from sales*, is the one that
changes the par as well as the order; it has its own section below.

The figure is `slowsellers.sold_by_recipe`, the same function
`private/slow-sellers/` reports from, **pooled across every blank a colorway
is dyed on** because that is the unit a bath is planned in. One answer to
"what sold": two would let the page that ranks on it disagree with the page
that reports it. It prints beside each colorway so the ranking can be checked
by looking rather than trusted.

A colorway that sold nothing is ranked last, never hidden. It may simply be
new — 2026 is year one for colorway data — and this page is not where that
gets decided.

### "Par from sales": a second par, tried on rather than written

**The third answer to "which shortages first" — `?sort=sales_par` on
`private/production-needed/`, `order=sales_par` on the sheet form — judges
every product against `ceil(2 × units per faire day) + 1` instead of its
stored par, and orders by that shortage.** It is `production.DemandPar`,
`ORDER_SALES_PAR`, and `candidates()` takes the object as `demand_par`.
Nothing is stored — `FinishedProduct.par` is untouched, the recipe page's
par editor is still the only door to that number — and the other two
answers are the old page to the byte. It exists to see what the other
arithmetic would say, side by side with the arithmetic already in use, so
it is a pill and not a migration.

**It is a pill beside *Best sellers first* and *Use par*, not a checkbox
beside them.** The first cut was a tick, and the user called that a
disjointed experience: it silently overrode the order select above it and
sat in a different control from the two answers it was a third of. One
question, three answers, one control — on both pages, so a link off one
means the same on the other.

**What it is answering.** The pages above rank on sales pooled by colorway
and then filter on a par that is the same number for nearly everything, and
the two do not agree about what matters. A best seller sitting one above
par 8 is not short, so it never reaches the list it would top; a colorway
that sold three all season is short by the same rule and does. Ranking on
sales was the fix for par being untrusted, but it only reorders what the
par filter let through, and the filter is where the best seller was lost.
A par that moves with what sold is what makes membership and ordering say
the same thing.

What is deliberately kept, and why:

- **Per product, not pooled.** Par is per product and it is the product's
  own shelf that goes empty. The *ordering* stays pooled by recipe — a bath
  is planned in colorway units — so the two figures on a row still mean
  what they meant.
- **The floor is one.** `ceil(0) + 1`: a colorway that sold nothing asks
  for one, so it can be on the table to be bought. A bath overshoots that
  by construction, so on the default sheet it drops out exactly as a
  stored-par shortage of one does, and `include_overshoot` brings it back
  the same way.
- **The denominator is faire days with sales recorded**
  (`slowsellers.days_with_sales` ∩ traded `FaireDay`s in the season range),
  not calendar days and not the calendar's faire days. A weekend whose
  export has not landed would otherwise read as two days nothing sold and
  halve every rate. Zero days is *not available*: the stored par is used
  and both pages say so in words, because a sheet that silently fell back
  to the other number is a filter working invisibly.
- **The order is shortage from that par, and `order` is ignored.** The
  sales are already in the number, so ranking on them again counts them
  twice — and `_urgency`'s empty-shelf-first would put a colorway that
  sold nothing (par 1, none on hand) above the best seller twelve short.
  The floor of one is what keeps the empty shelf on the list at all; it is
  not what puts it first. Choosing this pill *is* choosing that order, on
  both pages, so they still agree about what "the first twenty" are.
- **The stockout bonus is not added.** A sell-out is a sales event and this
  par is built from sales. The first cut stacked it on the argument that a
  counted zero is an observation where the rate is an estimate; the user
  took it off the same day — see the note under *A Sunday-night zero adds
  a bath*.
- **The SQL prefilter is skipped.** The target is per product and computed
  in Python, so `candidates()` walks every dyeable product in that mode —
  a few hundred rows, once. The `behind_a_bath` prefilter goes with it, and
  `annotate_flight` answers both from the target.

**With nothing to divide by the pill falls back to the stored par**, says
so, and the page takes its default order (best sellers first) rather than
inventing a shortage order off a number the pill did not promise.

**`target_par` is what the row prints, in both modes.** `annotate_flight`
sets it to whichever par the shortage was judged on, so what the row shows
is what it was measured against — and the row that used to print
`par_level` and render blank now prints this. On the sheet a hand-picked
row never went through the planner and shows the stored par; `firstof`
falls through to it, which is safe only because a demand par is never zero.

**The known trap is the same one `oven` has: the flag has to travel.** The
sort pills carry it, the category select is in the same form, and
`partials/production_needed_row.html` posts it as a hidden input with
*Bagged a bath* so the swapped-in row is judged the way the rows around it
were — drop that and a bagged bath comes back measured against the stored
par in the middle of a list measured against sales. On the sheet only the
suggest form needs it: once `items` exists the list is the list.

This sits beside, not against, the argument under *A Sunday-night zero adds
a bath*. That measured a *weekend* of projected demand against par and found
it crossed a bath boundary four times in 333; this asks a different question
— two days of cover plus one, per product — and whether that question is a
better one is what having both on a checkbox is for.

### Recording a bath that a sheet already claimed

Two pages book a bath after the fact rather than off a sheet: the recipe
page's production form and the *Bagged a bath* button above. Both used to
take the blanks off the shelf and add the output, which is right for a bath
nobody planned and wrong for one on a sheet — its blanks came off when the
run was made (*The blanks come off when the run is made*, below), so the
recording took them twice, and the row stayed open, still subtracting from
the planner, for a bath already in a bag. Nothing said so: the shelf read
low and the sheet read unfinished.

`production.report(product, units, ...)` is now the door for both, and it
**honours the claim first**: pending rows for the product are accepted in
sheet order, whole rows only, for as long as the units cover them, and only
what is left is booked as an unplanned bath (blanks off now, output on
through the ledger). "Three baths of this are bagged" with two on a sheet
ticks the sheet's two and records one more, which is what happened, and the
page's message says which sheet was ticked. A report smaller than the next
row leaves that row alone — a short bath is the sheet's own page's job,
because it is the one that can say how short.

## `private/produced-since/`: the receipt, and the one way out

Everything above writes `InventoryLog` rows. Every page that reads them
*consumes* them — the label sheet turns them into stickers, the raw shelf
forecast into a reorder date, `candidates()` subtracts them from a shortage —
and until this page existed **not one of them ever showed the rows.**

What that produced was not a missing feature. It was an unused one. Recording
a dye bath wrote something nobody could read back and nobody could take back,
so every click was a commitment with no receipt, and the rational move was not
to click. The page is two things and the second is why the first matters:

- **The list.** One row per entry since a date, grouped by the day it is
  recorded against, saying what the app believes and where the belief came
  from, with what it thinks is on hand now beside it. Same argument the rest
  of this codebase makes about par and `colorbands`, turned on the app's own
  history: *advice you cannot inspect is a decision in disguise*, and a
  production number nobody could see was the last one still hiding.
- **Take it back.** A retraction writes a compensating `InventoryLog` and
  touches nothing already recorded. Finished stock goes down, the blanks go
  back on the raw shelf, and the entry comes off the list.

**It reads production and nothing else.** An adjustment is a recount — the
Sunday close alone writes dozens every weekend — and a page meant to answer
"did I dye this" must not also be answering "did somebody find a bag of it in
a cupboard". That was the fault `labels.produced_since` had in the same query;
see *A since-run counts dyeing* in `docs/claude/labels.md`.

### The retraction leaves no mark on the page, and that is the feature

Both rows drop out of the list. Reading the page tells you what the app
currently believes and nothing whatever about what anybody got wrong.

This inverts a rule that holds nearly everywhere else in here — the Retire
button collapses a recipe row to a strip rather than letting it vanish,
because *a row that disappeared is indistinguishable from a click that never
arrived*. That rule is about a person not being sure their own action landed.
It does not apply here, because the entry leaving the list **is** the
confirmation, and the flash message says what moved.

What applies instead is the argument the Sunday close's Undo is built on: a
mistake somebody cannot fix themselves is a mistake they have to go and
confess, and that cost is exactly the pressure that gets one left unmentioned.
A struck-through row reading *taken back* pays that cost a different way — it
is a standing note about a mistake, on a page that gets opened every week, and
a correction that leaves one behind is a correction with a price on it. **The
whole point of the button is that using it should be cheaper than saying
nothing.** So there is no confirmation dialog in front of it, no reason field
required, and nowhere in the app that counts how often it is used —
`producedsince.py` says out loud that `retracted_total()` is the obvious
function not to write.

Nothing is hidden from the *record*. Both rows are in `InventoryLog` for good,
linked by `reverses`, and the admin shows them the way it shows everything
else. The claim is narrow and it is the one that matters: reading this page
does not tell you what anybody got wrong.

### What the compensating row does and doesn't move

`producedsince.retract` is the whole of it.

**The finished side is reversed as a delta, never as a restored absolute**,
because a sale can land between the mistake and the Undo. `set_on_hand`
clamps at zero, so the arithmetic degrades the right way, and it writes to the
raw row for a passthrough — one pile, one number.

**The raw side goes back too**, which the close's Undo has no equivalent of
and deliberately shouldn't: there the entry is a recount of a shelf and
implies nothing about blanks. Here it is a claim that a pot was run, so
retracting it claims the blanks were never wet — and raw stock in this shop is
an opening balance counted about once a year (see *Raw stock is an opening
balance* in `docs/claude/stock.md`), so leaving it short would stay wrong
until somebody reached for skeins that had been on the shelf all along.

**How many blanks go back is read off rows, never guessed.** Two cases are not
simply the entry's own quantity:

- **A short bath.** `apply_row` takes the full bath off raw and puts only
  `yielded` onto finished, because the blanks are gone whatever happened in
  the pot. So where a `ProductionRunRow` points at this log, the row's
  `quantity` is what left the shelf — five, not the three that survived.
- **A fancy veil out of a plain bath.** That writes a second entry, for a
  second product, against the *same* pot — and the log's `raw_product` is the
  plain blank rather than the fancy one. Restoring on both would put the bath
  back twice, so a log whose product's blank is not the blank it consumed
  restores nothing and the sibling accounts for the pot.

**A history-only entry moves nothing in either direction.** `private/cards/`
writes log rows and never touches `number_on_hand`, so an Undo on a 2024
kanban card must not quietly take ten scarves off today's shelf.
`HISTORY_ONLY_SOURCES` names that exception rather than the rule, so a new
flow that actually dyes something is covered by default. It **still writes its
compensating row**, at a quantity of zero, for the same reason `apply_row`
writes a log at a yield of zero: `reversals` has to be the single answer to
"has this been taken back", and two guards is how one of them goes stale.

**The `ProductionRunRow` is left alone.** Its `applied_log` is what stops a
re-scanned sheet dyeing the same bath twice on paper, and handing that guard
back to get a tidier-looking row would trade a silent double-count for a
cosmetic one. The sheet goes on saying what the session reported; the ledger
says what was done about it afterwards. The colorway comes back on the next
sheet regardless, because the planner reads `number_on_hand` and the
retraction just reduced it.

**And the guard is checked under a lock.** `is_retracted` read outside the
transaction lets a double-tapped button through twice — five real scarves off
the shelf, silently, for one bath. Same failure as a redelivered Square order,
same fix.

### `rawdemand` had to learn about it too

`rawdemand._entered_production` counted PRODUCTION rows for the raw page's
*Entered, 8wk* column, and a retraction is an ADJUSTMENT it never read — so
without an exclusion an undone bath went on reporting itself as dyed on the
raw shelf's forecast. The column and the function are gone now (see *Par on a
blank* in `docs/claude/stock.md`), but the obligation is not: anything new
that reads these rows inherits it. **The question is never "was a production
row written", it is "does one still stand".**

## Production sheets: paper to the dye room, one scan back

`private/production-sheet/` prints a dye-room worksheet — the next N baths to
run — and `secret/production/<token>/` is how the answer comes back.
`scarves/production.py` picks the baths and draws the PDF.

**This is a tool to facilitate a task in the real world, where a computer is
an ill fit.** That sentence decides most of the arguments below. The dye room
has gloves, water and a sink in it, which makes a phone the wrong thing to be
holding — so the sheet is the work order, a pencil is the input device, and
the phone is picked up once, afterwards, with dry hands.

**Dyeing takes one to three days, and the app used to model it as a moment.**
A bath is dyed, dried, tagged and bagged, and only the last of those is a
scarf that can hang on a peg. Off season the difference costs nothing; in
season it is one to three days in which the app is wrong in a way the close
and the restock walk both read — and, worse, a second sheet printed inside
that window asks for the same baths again, because the stock has not arrived
and the colorway still reads as short. That is what the machinery below is
for. **The fix is deliberately not a WIP state machine**: the paper already
carries the three days perfectly well, and four reports where the job needs
none is how a flow stops being used.

**The tick box means "this amount has been accepted into inventory"** — not
"this bath happened". It is filled in when the bath is bagged and ready for
the booth. That makes a blank box a complete and honest statement rather than
a lost one: 5 of 20 is a sheet that isn't finished, not a sheet that mislaid
fifteen baths. Same wording on the production-needed page's button, which
makes the same claim from the other direction.

**A `ProductionRun` is a record.** It used to be scaffolding — the paper was a
work aid, the `InventoryLog` was the only thing that survived it, and
deleting a useless one was cheap and normal. That paragraph was right for its
time and this one replaces it. Once a sheet has to carry a session that runs
three days, the plan gets edited, baths get called off, and a lot
occasionally comes out of the pot ruined — and **none of that is answerable
from the ledger**, because the ledger only ever says what *entered*
inventory. A cancelled bath and a bath nobody ever printed leave the same
trace there, which is none. So the rows are kept, `ProductionRunAdmin` is
read-only, and nothing deletes — the same bargain `CloseRun` makes.

This is also the surface a bill will eventually hang off, which is the other
reason the rows have to mean something on their own. **Don't build that on
`submitted_by`**: it is filled from the remembered-PIN cookie as a record of
who replied, and it is not a claim about who did the work.

**The row is a bath, and the reporting line is a colorway.** A bath is one
blank plus one recipe yielding `number_per_dye_bath` units of a single SKU,
and that is still what a `ProductionRunRow` is — it is where scrap is
answered from, what `applied_log` stops counting twice, and what striking
releases.

**But the sheet that comes back groups them.** Three baths of Artisan
Cabernet is not three groups of five, it is one group of fifteen: one line,
one tick box, one number. The crew are standing in front of one pile that
came out of one colour, and three boxes for it are three marks for one
answer, three chances to tick the wrong line, and three lines a photograph
has to resolve.

**The blank is the other half of the identity.** Artisan Peacock and Noble
Peacock came out of two different pots and stay two lines. That falls out of
grouping on `finished_product` — blank × colorway, the axis the catalogue is
already organised on — so it needed no new concept. `production.Line` and
`lines_for` are the fold; the order is first-appearance, because `plan_baths`
already clumps a recipe's baths together and the order between recipes is the
urgency it chose.

**The work sheet does not group.** Its boxes hold a pot at a point in a
one-to-three day process, and three baths of Cabernet genuinely are three
pots that dry separately — one row of boxes cannot say two are dry and one is
still wet.

**A short line loses whole baths first.** Ten of fifteen almost certainly
means one pot failed, not that three each lost 1.67, so `accept_line` fills
greedily — 5, 5, 0 — and the loss lands on a bath. Spreading it evenly would
invent a bad afternoon out of one ruined lot, in the only place a scrap
question can be answered from. Nobody at a sink knows which pot it was and
nothing asks them.

**One product instance across a line, and this one is sharp.** Every row of a
line points at the same colorway and `select_related` hands each its own copy
— so applying several in one pass had each read `number_on_hand` as it was
before any of them ran, add its own yield, and save. Last write wins and two
baths of three vanish, with no error, a closed run and every log present.
That could not happen while a tick was one bath in one request, which is
exactly why it arrived with the grouping.

So production is not a column of counts to be entered — it is a handful of
answers in the same order as the paper, which makes reporting recognition
rather than transcription.

**A row ends as pending, accepted at n ≥ 0, or cancelled**, and `closed`
derives from "nothing pending". There is no run-level retired flag, because
retiring a sheet *is* cancelling its remainder — so closed has one meaning
and cannot disagree with its own rows.

**Cancelled and binned are different, and both are needed.** Cancelled means
the bath never ran, so the blanks it was holding go back on the shelf and the
claim on the planner is released — the colorway comes back on the next sheet.
Checked with an actual of 0 means the bath ran and the lot was binned, so the
blanks really are gone and the claim stands. Same code path, `yielded` at 0.

**Yield asks one number: how many came out.** The bath consumes its blanks in
full whatever happened in the pot — dye four, ruin one, four blanks are still
gone — and they were already paid for when the run was created, so `apply_row`
puts `yielded` onto finished and touches raw not at all. The difference is a
loss rather than a discrepancy, and needs no correction on the raw side.

### The money is frozen when the bath is accepted, because a bill is as-of

Accepting a bath is already the load-bearing step — the sheet is checked in,
stock moves — and it is the intended payment handoff: a closed run carries its
rows, their yields and a date, so nothing new has to be recorded to pay off
it. `apply_row` therefore writes two more figures onto the row as it accepts
it, and `scarves/dyebill.py` only adds them up.

- `unit_blank_cost` — what one blank cost, then.
- `output_retail` — the whole bath's output at the prices then: plain units at
  theirs and any fancy ones at theirs.

**This is the one place in the app that stores what it could derive**, and the
exception is deliberate. Everything else answers "what is true now", where a
live read is right and a stored copy goes stale. A statement answers *what was
this session worth when it happened*, and a derived answer to that is wrong in
a way nobody could see — a supplier increase or a spring reprice would rewrite
what every past week earned, silently, including weeks already paid for. Same
bargain `quantity` already makes with the printed sheet, one step further: the
paper says how big the bath was, and these say what it cost and what it made.

The asymmetry between the two is real. Cost is **per unit** and multiplied by
`quantity`, because a bath eats its blanks whether or not the pot came good and
however its output was finished. Retail is a **total**, because a split bath's
output sells at two prices and no single unit price describes it.

**Rows accepted before this land null, and null is not zero** — but they are
not left blank either. 133 baths of real work closed before the columns
existed, and a page that showed them earning nothing would be wrong in a more
misleading way than an estimate is. So `dyebill` values them at *today's*
prices, under two rules: **nothing is written back** — the columns stay null,
so a guess can never be mistaken for a record afterwards — and **the estimate
is marked wherever it prints**, per session and in the totals. It moves when
the price list moves, which is right for an estimate and would be a bug in a
record; that is exactly why the two live in different fields.

**A retracted bath is off the statement.** *Take it back* leaves the row and
its `applied_log` exactly as written, so the question is whether an entry still
stands, not whether one was made — `dyebill` reads the reversal. Getting that
wrong stops being an inventory error the moment a statement is a bill: it pays
for a bath somebody has already said did not happen.

**No rate lives anywhere.** A statement says what the blanks cost and what the
output is priced at, and the labour line is absent because no rate has been
agreed — a number in a column reads as a decision. The planner carries the
same two figures for a list being built (`dyebill.estimate`, derived from
today's prices, and blind to yield on purpose), so the size of a session is
readable before anybody lights a pot. That is where a rate would eventually
say what the work pays before it starts.

### The blanks come off when the run is made, not when the bath is reported

**A run is an intent to make something, and the yarn it needs is spoken for
from that moment.** `production.open_rows` is the only door that creates a
`ProductionRunRow`, and it takes the rows' blanks off `number_on_hand` as it
creates them. `cancel_row` is the only door that releases one, and it puts
them back. Nothing else creates or destroys a row — a third site would claim
nothing, and the shelf would drift down by exactly the yarn it forgot.

Two things depend on the claim landing at the start, and neither survives one
that lands at the end:

- **Planning happens in passes.** Half a week goes on a list, the shelf is
  read again, and the rest is planned against what is left. A list that has
  not moved the count lets the next one plan the same skeins twice, and the
  shortage is discovered in the dye room.
- **Ordering has a lead time.** A run planned Monday and finished Friday that
  takes a blank under its floor has to say so on Monday. By Friday the window
  to order and have the yarn on hand has gone. `raw_shortage` reads
  `number_on_hand`, so the claim is what makes the reorder fire in time.

**So `number_on_hand` means unclaimed yarn, not skeins on the shelf.** The two
differ by whatever is on open sheets, and the gap between planning a bath and
dyeing it is never more than about a week. `private/raw-inventory/?plan=1`
prints the claimed figure beside the count, so a number that fell without a
delivery or a recount has its reason on the row. The shelf total is not
printed: nobody acts on it, and a blank reading empty is far more likely to be
a stale number than a bare shelf. The collection list deliberately does **not** add a sheet's own
claim back into the belief it prints: the count is clamped at zero, so doing
so would turn `fetch 12 · we think 2` into `we think 14` and make an empty
shelf look full. A shortage is caught on the picker by `short_blanks`, before
the claim, where the arithmetic is still unclamped.

The worked example, end to end: **150 on the shelf, plan 50, and it reads 100.
Deliver 40 to inventory and it still reads 100, because those blanks were
paid for at planning. Call off two baths and their 10 go back: 110.**

Counting the shelf while baths are in flight would read high by the claim, and
that is left alone deliberately rather than reconciled: a stock count does not
happen with dyeing in progress, and the most that goes in mid-week is a
delivery — which is a *delta*, and correct whatever is claimed.

**Always write the `InventoryLog`, even at a yield of zero.** That keeps
`applied_log` the single answer to "has this row moved anything", and two
guards is how one of them goes stale. Nothing downstream minds:
`labels.produced_since` already filters `quantity__gt=0`. There is
deliberately **no `FAILED_BATH` log type** — a total loss is a partial loss at
the end of its range, and a separate type would record 5→0 and 5→3 in
different shapes, so any scrap question would have to union two of them.

**The ledger says what entered inventory; the row says what the session
found.** Raw movements have never been ledgered here, so scrap is answered
from `ProductionRunRow`, not from `InventoryLog` — the same split the close
makes when `closing.tally()` reads `CloseRunRow`.

**`ProductionRunRow.applied_log` is what stops a bath counting twice.** The
return URL is printed on paper that can be re-scanned, the button can be
double-tapped, and somebody who remembers one more bath will reopen the page
and submit again. All three are normal. `production.apply_row()` is a no-op on
a row that already has a log — same failure as the Square webhook and
redelivered orders, same fix. Un-ticking is deliberately **not** the inverse:
once stock has moved, taking it back is an inventory adjustment with a reason
attached, not a checkbox on a page with no login.

**A printed code can be killed two ways, and they are different acts.** Edit
the token in the admin and the old code stops resolving at all — use it when
the sheet is finished with. **Revoke** it (`revoked_at`, an admin action) and
the token stays on the record, the door shuts, and the crew's page returns 410
saying so — use it when somebody may still be holding the paper, because "that
code has been revoked" is a sentence where "no such run" is a hunt for a
character they think they mistyped. The token was read-only until somebody
needed to kill one; orphaning the paper is a real cost and *exactly* what is
wanted when a code has got out, and a rule that only stops the deliberate case
is not a guard. Neither touches the work: baths are still accepted and
cancelled from the staff page, and `is_revoked` is deliberately outside the
four run states, so a revoked sheet's pending baths go on being subtracted
from the plan.

Worth knowing what a leaked token is worth, since it decides how much any of
this matters: the code is `NN-adjective-animal` over 104 × 100 × 100 = **1.04M
combinations, 20 bits**. With five sheets live that is one in 208,000 a guess
— a few hours of scripted requests for a coin flip. It stays words rather than
a UUID because somebody types it off paper when the QR won't read, which is
the entire reason the fallback exists, and because what a guess wins is
production recorded against one sheet: visible on that sheet's own page and
correctable. A UUID would change that arithmetic and nothing about the failure
that actually happened, which was a code printed on a page somebody
photographed.

**One QR for the sheet, not one per row.** Twenty codes would be twenty scans
to record what is one session's work. The token in that URL is what
authorises the return — the same bargain as the other `secret/` pages, scoped
to a single sheet instead of standing open forever, and it means the crew
report production without accounts. No PIN: you reached the page by scanning
something you are holding, so a PIN would be friction with nothing behind it.
**The QR on the paper is the permanent door**, which is what makes every list
of sheets in the app pure UI — losing one from a list costs nothing.

### Two codes in the print, and which page each is allowed on

The run code (`OPEN THIS SHEET`, in the header of the collection page and the
reporting sheet) and the upload code (`SEND A PHOTO`, in the collection page's
footer). They do different jobs and they are labelled, because two unlabelled
QRs on one sheet is a choice with consequences and nothing to make it by.

**The upload code is on the collection page precisely because that page is
never photographed.** The reporting sheet is the obvious home for it — it is
the page in her hand when the session ends — and it is the one page it must
not go on: that sheet is what gets photographed, so a second code lands in
every shot, and `_read` keeps the **first** QR that yields a token. Which of
the two named the run would be a coin flip, and the losing outcome is not a
retry — it is a sheet the app confidently cannot find. The collection page is
also where the paper already is: it goes to the shelf and stays in the dye
room for the session.

The reporting sheet says in its instructions where the other code is, since
moving a door without a sign leaves somebody hunting the page that used to
have it.

**`sheetscan.token_in` was the latent half of that, and is fixed regardless.**
It took the last non-empty segment of *any* URL, so
`/secret/production/upload/` read as the token `upload` and a shipping label
or a phone screen at the edge of a shot read as whatever its URL ended in — a
stray code did not merely fail, it **won**. It resolves against the URLconf
now and accepts only the `production_run` route, which is also the guard the
URLconf itself already had (`upload/` is registered ahead of the token route
for this exact collision). Note that resolution does not *fail* for an unknown
path — `mysite` ends in a catch-all — so the check is on the route's name.

`quantity` is frozen when the sheet prints rather than read back off the raw
product. The paper says `x4` and the paper is what somebody worked from; if
the bath size is edited next week, that row still has to mean what it said.

### Four states, and only one of them changes behaviour

`production.py` derives all four in one place, off the rows rather than off
`submitted_at` — which survives as a record of the first reply and decides
nothing:

| Filter        | Means                                          |
|---------------|------------------------------------------------|
| `closed`      | nothing pending: every row accepted or cancelled |
| `unreported`  | nothing accepted at all                        |
| `overdue`     | open past `OVERDUE_AFTER` (10 days)            |
| `counted`     | open and not overdue                           |

**`counted` is the one that does anything**: those sheets' pending baths are
subtracted in `candidates()`, so a second sheet doesn't re-ask for the first
one's work. Everything else is a list somebody reads.

**Overdue must be visible, not silent.** The age bound stops a lost sheet's
unaccepted rows suppressing colorways forever — but the moment a sheet stops
claiming its baths is the moment those colorways start being asked for again,
and if that happened quietly a session genuinely still in progress would get
re-dyed behind somebody's back. So the picker names overdue sheets and never
truncates them. "Why are these open after ten days" is the question; accept or
retire are the two answers. No escalation and no count of how often it
happens — the same bargain `_drained_at` makes on the restock board.

**Automatic retirement is gone.** Printing a sixth sheet used to close the
oldest (`retire_superseded_runs`, newest-five). The reasoning was right — five
sheets out at once means the reporting loop has already failed — and the
remedy was the app guessing: closing an unanswered sheet silently decided its
session never happened, and a sheet with four baths still drying looks exactly
like a sheet somebody abandoned. `MAX_OPEN_RUNS` survives as `RUNS_LISTED`, a
display limit on the picker's convenience list and nothing more.

**The code on the paper is enough to call a bath off.** It was already enough
to *accept* one, which moves stock — so requiring a staff login to say "this
isn't happening", which moves nothing, had the permissions backwards. The crew
reporting a session is also the only person who knows the rest isn't coming: a
lost sheet, a session that stopped. So `secret/production/<token>/` carries a
per-row **not coming** and a **The rest isn't coming**, and both are safe to
reach with a token for the same reason cancel-all is a button at all — they
hand claims back rather than putting stock on the books.

Two rules keep that honest. **A cancel never banks whatever was ticked**: each
cancel branch does only its own work, because a cancel that also accepted a
half-entered row would move stock this page has no way to take back, and the
cost of the alternative is a re-tick. And **a cancel can be undone with no
account**, which is free here in a way undoing an acceptance is not — nothing
moved, so nothing is erased or compensated. That is the Sunday close's Undo
argument exactly: a fix somebody cannot make is a mistake they have to go and
tell somebody about, and that cost is what gets one left unmentioned.

Adding a bath stays staff-side, because it needs the catalogue search and is a
planning decision rather than a report on the session.

**Cancel-all yes, accept-all never.** Direction decides which bulk actions are
allowed. Cancelling moves nothing into inventory and hands a claim back, so
the worst case is being asked about those colorways again. Accepting puts
stock on the books for piles nobody looked at, and every one it gets wrong is
then wrong on the pegs, at the close and in Square. So accepting costs a mark
per row — the same rule as the restock board's missing "check all", where the
cost *is* the evidence. Somebody reasonable will ask for accept-all after a
long session; the reasoning is in `production_run_cancel_remaining` so it
doesn't have to be reconstructed.

### The sheet is editable, and prints as two documents

The planner only sees shortages against par, and there are real reasons to dye
something that isn't one — an order taken at the stall, room left in a
session already under way. A plan nobody can edit gets worked around on paper, and
then the paper and the app disagree about what the session was. So the run
page has a type-ahead to add a bath and a `strike` beside each pending row.
Adding **appends**, because `order` is the position on a printed sheet and
renumbering would make an existing printout disagree about which row is
which; striking **cancels rather than deletes**, because a line on a printed
sheet the app has never heard of is worse than a line it can explain.

The type-ahead is the same `product_search` endpoint the uploader and the
label picker use, on a third `?mode=` template swap. Submit-only and never a
type-ahead per keystroke, and with the script blocked `q` lands in the URL and
the page renders the very partial the fragment returns — the same call the
close's tag search makes, for the same reason.

**One print, three documents, in the order the job happens:**

1. **Collection page** — the blanks, then the dyes. One walk to the shelf.
   It also carries the **upload code** in a footer; see *Two codes in the
   print* below for why it is on this page and not on the one that comes back.
2. **Work sheet** — `WORK_BOXES` (4) blank boxes per bath, with a ruled line
   over each column. This lives in the dye room for the whole session.
3. **Reporting sheet** — name, expected, a ruled space for the actual, and
   the tick box. This is the one that comes back.

**The work sheet carries no barcodes and no QR, and that absence is the
feature.** It is the only thing stopping the wrong sheet being photographed —
a marked-up working copy read as a report would tick baths still on a drying
line. With nothing on it to decode, a photo of it cannot name a run. It says
the run number and code in plain text so a person can match the two halves,
and says "do not photograph this page" out loud.

**The stage boxes carry no printed names, and that is the decision.** The
obvious version prints DYED / DRIED / TAGGED / BAGGED across the top, and
that is the app telling somebody how to do a job it does not do — the stages
are hers, they vary with what is in the pot, and a printed name is an
instruction whether or not it was meant as one. There is a ruled line over
each column instead, which is an invitation. Nothing stores what she writes,
reads it back, or knows how many stages a bath "should" have; the one
transition the app cares about has its own box on the reporting sheet. **The
work sheet is optional and says so** — it exists because a column of boxes
holds twenty baths at different points across three days, not because
anything needs it back.

**Nothing in the PDF reads the run's state.** Every row prints, accepted or
cancelled or neither. A reprint mid-session is the same document as the first
print, not a rendering of what has happened since — **information flows paper
→ app**, and the moment a PDF starts hiding rows that have come back, two
sheets for one run disagree about how many baths are on it, with the one that
disagrees being the one already in somebody's hand. Striking a row edits what
the app will ask for *next*; it does not edit the paper already printed.

### What lands on the sheet

**Two ways to start, and then one list.** Either the app suggests baths from
what is below par, or somebody says what they already know they are dyeing —
an order taken at the stall, a colour worth trying, room beside a pot already
being heated. Neither of those last three is a shortage, and a planner that
can only answer "what is below par" cannot express any of them. It is also
**the only way to plan a session when nothing is short**, which is exactly
when there is time for one: the picker used to refuse to create a run at all.

**The two are entry points, not modes, and they compose** — suggest twenty,
add the two you promised somebody, drop the one you have no blanks for. Both
are on screen with no toggle. An earlier version made them modes and it came
out wrong in a way worth recording: a suggested list you could only look at,
an editable picked list, and a read-only preview of the first sitting
underneath the editable copy of the second. **A suggestion you cannot change
is a suggestion somebody works around on paper**, which is the whole failure
the editable sheet exists to prevent.

**They stack, full width each, rather than sitting in two columns.** The
columns were spending the page on a rule down the middle and then wrapping
every field in both halves — five questions in a column half a page wide,
and a tick whose explanation came out eight lines of two words. Stacked, the
three questions sit in one row and the two ticks in another, the tick panel
gets the width that turns seventy-seven dyes into four short columns, and the
`h2` rule from `base_internal` already draws the divider the middle rule was
drawing by hand.

**A tab strip was the other way to get that width, and it is the one to
refuse.** Tabs are a mode switch wearing different clothes: whichever half is
not in front is a click away and, more to the point, out of mind. These two
are meant to be used in the same sitting. The cost of stacking is scrolling,
which is cheap; the cost of tabs is the composition, which is the feature.
Note that the CSS-only version (two hidden radios and `~` selectors) works
fine and needs no script — *being buildable is not the objection*.

So **every row is editable however the list was seeded** — a bath count and a
✕ per row. `items=<pk>:<baths>` is the canonical list and rides in the query
string, so a sheet is a link somebody can send; the ✕ is a server-rendered
link to the list *without* that row, which is why removing one works with the
script blocked. A count box can't edit half of a `pk:n` pair, so each row also
posts `qty-<pk>` and the form applies it as an override — membership and order
stay with `items`, and only the count comes from the box. Editing to zero
removes the row, because typing it away has to mean what the ✕ means.

**Every edit is a round trip, and the round trip is small.** The bath total,
the short-blank warning and the dye collection plan all read the list, so a
client-side edit would leave them describing it as it was a moment ago — a
short dye list sends somebody to the shelf for the wrong things. Changing a
count, removing a row and adding a colorway all `hx-get` the page and swap
`partials/sheet_plan.html` into `#sheet`.

**The view returns that partial when `HX-Request` is set**, and the page
`{% include %}`s the same one. Both halves of that matter. The first version
used `hx-select="#sheet"` against the full page, which renders the whole
shell — every CSS rule, both start panels — and throws all but the table
away; that is a page load wearing a swap's clothes, and it is the thing to
avoid repeating. The second is what keeps one renderer, so a swapped view
cannot disagree with a refreshed one, the same call `production_needed.html`
and `recipe_showcase.html` make about their rows.

**Print submits the list form itself** (`formmethod="post"` on a button that
belongs to it), so whatever is in the count boxes right now is what gets
printed. It used to be a second form carrying the server's copy, which meant
a count typed but not yet synced never reached it — and an "Update the list"
button existed purely to close that gap, doing nothing at all whenever htmx
was working. The form stays a GET so Enter in a count box, and the add
button's no-script fallback (`formmethod="get"`), are harmless refreshes
rather than a sheet nobody asked for.

**Nothing here runs JavaScript of ours.** Every control is an ordinary
element with an htmx attribute on it and a working fallback underneath: the
✕ is a link with an `href`, the count box sits in a GET form with an "Update
the list" button, and a search result is a `<button form="sheet-list"
name="add">` that submits the list with one more parameter. htmx intercepts
the click when it is there; the form posts when it isn't.

That fallback pair is also what fixed a real bug worth remembering. The first
picker built its list in the browser with a click handler, and a later rework
deleted the table that handler wrote into — so clicking a result did nothing
at all and the network panel stayed silent, because nothing was ever
requested. A control that is a form submit cannot fail that way: either it
navigates or it was never clicked.

The count is **baths, not scarves** — two baths of a blank yielding four print
as `4 ×` twice on the planner, because a row is a bath and a bath is what
somebody physically does. The list shows what they make beside it so nobody
multiplies. The *reporting* sheet is the one place that folds them back
together, for the reason above: planning is done in pots, answering is done
in piles.

**Nothing about a pick is filtered on par, and none of it is subtracted for
what is already in flight.** `plan_baths` derives what is needed and so must
not double-ask; a pick is somebody deciding, and asking for a colorway a live
sheet already covers may be exactly what was meant. What *is* refused is a
product that cannot be dyed at all — an undyed passthrough or a fancy veil —
and the search shows those greyed out with the reason rather than hiding them,
the same call the label picker makes about a missing SKU.

The type-ahead is a fourth `?mode=` on `product_search` (`plan`). It can't
share `mode=sheet`, whose results post straight onto an existing run, and it
can't share `mode=labels`, which greys out anything without a SKU — a SKU
prints a sticker, it doesn't dye a bath.

**The suggestion is ordered by sales, and so is `private/production-needed/`.**
They have to agree: somebody reads that list, then asks the picker for the
first twenty baths, and if the two sort differently they get twenty that are
not the ones they were looking at — with nothing on either page to say so.
`production.ORDER_SOLD` is the default on both and `ORDER_PAR` is the choice,
for the reason par is not trusted: it was never dialled in, so ranking a dye
session by shortage ranks it on a number nobody chose.

**Sales are the primary key, not the only one.** `_urgency` is still the
tie-break, so an empty shelf leads among colorways that sell alike — a
customer cannot buy a zero — while a colour nobody buys no longer jumps the
queue for being emptier. That combination is what the old ordering got
backwards.

For the shortage dataset, the default is `FinishedProduct.behind_a_bath` — products where a whole bath still
lands at or under par, which is where a session's work is fully used. The
checkbox widens it to everything below par, including the ones a bath takes
*past* par. That second group isn't sloppiness: a bath is a fixed size, so
overshoot is rounding rather than overproduction, and those shortages get
rounded away next time the recipe runs anyway. Worth printing when the session
has capacity spare, not when it doesn't — hence a checkbox rather than a
judgement baked into the query.

Baths of one recipe print together, because one mix and one pot serve several
loads and that is what makes the session cheaper. Order *between* recipes is
urgency, and an empty shelf leads: zero is the only state a customer can see,
where half par is just a shorter stack.

The picker **says when there aren't enough blanks** for what it's about to
ask for, and prints anyway. The order may already be placed, and refusing
would be the app arguing with someone who can see the shelf.

**In-flight baths are subtracted.** `candidates()` takes off what the
`counted` sheets have already asked for, which is the whole reason the four
states exist — without it a sheet printed on Saturday and another on Monday
both ask for the same colorway, because the stock has not arrived yet and it
still reads as short. Pending rows only: an accepted one is already in
`number_on_hand`, and counting it here would subtract the same bath twice.

**A sheet stays on the working list until every bath on it is settled.** It
used to drop off on the first tick, on the reasoning that one tick means
somebody is working from it. That reads one answered bath as a finished
session, which for a three-day process is most of a sheet's life — and the
crew's fallback list is exactly where somebody holding a part-worked sheet
goes when the QR won't scan.

The picker's list of live sheets is a convenience, "sheets you might still be
working from", not a queue to be worked off — the QR on the paper is the
permanent door, so losing one from a list costs nothing. It is truncated at
`RUNS_LISTED`. The overdue list beside it is the opposite and is never
truncated: those sheets are asking for something.

### "I haven't got that today": the one filter a person types in

**The sheet can ask for a bath that cannot be dyed, and until now nothing on
the page could say so.** A yarn box empties, a dye jar runs out, and the
shortage arithmetic knows about neither — so twenty baths come off the
printer, somebody walks to the shelf, and two of them are not going to
happen. The tick panel on `private/production-sheet/` is where she says
which.

**It is the last question the suggest form asks, directly above its button,
and there is only one button.** The first build hung it under the two
starts with a *Suggest baths again* of its own, which made the whole thing
an afterthought in the literal sense: plan, look at the plan, remember the
empty jar, say so, plan a second time. Two buttons that suggest is also two
buttons that mean the same thing, and the page then has to explain which one
you want. What she has no yarn for is a condition on the session, known on
the way in — so it belongs with the bath count and the oven tick, and the
sheet is suggested once.

**Why the app cannot answer this itself, which is the part worth keeping.**
Both numbers exist and neither is good enough to plan against. Raw stock is
an opening balance seeded from a count and topped up off invoices — *the one
pile nothing recounts on its own*, per `docs/claude/stock.md` — and
`Dye.in_stock` is a flag set in the Django admin and nowhere else, which is
to say almost never. A sheet filtered on either would drop colorways on a
belief no one had checked, and the sheet that came out would look entirely
normal. **A wrong filter is worse than no filter precisely because its
output is unremarkable.** What is reliable is the person who can see both
shelves.

So `production.blocked_reasons()` takes two sets of things she has just
ticked and returns reasons in words — `no Sash Belt`, `out of 608 Pink`.
`candidates()` hangs them on each product as `blocked_by` and **never
filters on them**, because it also feeds `private/production-needed/` and a
colorway that cannot be dyed this afternoon is still short; hiding it there
would hide the shortage along with the bath. `suggest()` is what acts:
blocked rows go to `Suggestion.skipped`, unblocked ones fill the sheet.

**Nothing is stored, and that is the decision rather than a shortcut.** The
obvious build is a flag on the dye and the blank, set once and honoured
until cleared — and it fails the rule in `CLAUDE.md` about steps that have
to be remembered, in the direction that is hardest to see. Marking is easy
and prompted by the empty jar in your hand; *un*marking is prompted by
nothing at all. The failure is a colour that quietly stops being suggested
for a month, on every sheet, with each sheet looking fine. Re-ticking two
boxes next week is the cheaper mistake. It rides in the query string like
every other piece of state here, so a planned sheet is still a link somebody
can send.

**Four rules follow, and they are the same four this file keeps arriving at.**

- **What it left off is named, never just subtracted.** Ticking a dye
  changes which colorways come out *and* how many baths each of the rest
  gets, so a list that silently reshuffled would be a filter working
  invisibly. `skipped` prints under the starts with the reason, the
  shortage and what it sold, so the cost of the tick lands on the same
  screen as the sheet it shortened. It
  folds into a disclosure past ten, but the **count stays in the summary** —
  what is allowed behind a click is the eleven names, which are there to
  troubleshoot a suggestion that came back looking wrong, never the fact
  that a tick cost eleven colorways.
- **A blocked colorway takes no place in the limit.** Twenty baths means
  twenty that can be dyed, not twenty minus the ones there is no yarn for.
- **A pick is never refused, only flagged.** The hand-picked half is
  somebody deciding, and the row stays on the sheet with `out of Fuchsia`
  under the colorway — the same call the short-blank warning makes, one
  column over from a ✕ she can click if she agrees.
- **Top-ups are the one place it filters rather than flags.** Nothing in
  that panel is short, so a top-up is a free choice among colorways, and
  offering one that cannot be dyed today is offering nothing.

**Both tick lists are the whole shelf, and neither knows what has been
suggested.** They are fixed catalogues grouped the way the shelves are —
blanks by category, dyes by brand — and the panel reads the same on the way
in as it does after twenty baths have been planned. The first build drew the
dyes from the collection list for the sheet in front of her, on the argument
that 132 checkboxes is a list nobody reads; what that actually did was
**make the panel a reaction to a suggestion rather than something you can
tell it.** "Out of J purple" is known standing in the dye room before
anything is planned, and a list that filled itself in only after a plan
existed asked her to plan first. It also moved the boxes around underneath
her as the sheet re-planned, so undoing a tick meant hunting for it. Fixed
catalogues are also what let the panel sit above the button rather than
below it: there is nothing for it to wait on.

Neither list is the whole table, though, and the cut is the same both times:
**a box that can only do nothing is worse than no box, because ticking it
looks like saying something.** The blanks are the 25 that are dyed at all —
no passthroughs, no fancy veils. The dyes are the 77 of 132 that at least one
*active* recipe calls for; the other 55 cannot block a bath. Both cuts live
in the form's querysets, which also means a tick is always reachable to
undo, because the list it came from never changes. Dyes file under the colour
name with the catalog number stripped (`425 Amethyst` under A) and print it
anyway — the number is what is on the jar, the name is what she asks for.

**The ticks travel with the list, and that is the known trap.** They are not
inputs of `#sheet-list`, so the ✕ links, the search form and the list form
each carry them explicitly — exactly as `oven` is carried, and it goes wrong
the same way: the page comes back with the panel cleared and the next
suggestion cheerfully offers the colorways she just said she had no yarn
for. `_without()` is the one to check first.

### A Sunday-night zero adds a bath — the one demand signal in the planner

**On the chopping block.** User, 2026-09-22: *"I think that was a failed
experiment if I am honest. It made sense in our heads. Let's leave it, but
it is on the chopping block."* It still runs on the stored-par path and is
switched off under *Par from sales*. Nothing below has been re-argued;
read it as the reasoning that was, and don't extend it.

`production.stockout_baths()` reads the **latest** close and adds one bath to
every product answered at `counted == 0`. That is the whole of the planner's
response to what sold, and everything about its shape was arrived at by
killing better-looking versions of it first.

**A bath is atomic, and that decides more than it looks like it does.** There
is no half bath — you could, but the labour makes it not worth doing — so
every target here is really a bath count, and a rule whose output has to be
*rounded* into baths is claiming a precision with nowhere to land. Measured
on the live catalogue: a weekend of demand, projected from season-to-date
sales, crosses a bath boundary against par for **4 products out of 333**, and
all four have no par set at all. Heavenly Cabernet at 6.4 units of cover and
at 10 units availability-corrected both round to two baths, which is what par
already said. So the obvious feature — derive a per-product target from the
sales rate — is not weak here, it is *unrepresentable*, and the arithmetic
that proves it is the reason this is a bath rule instead. (The *Set par
from sales* tick, above, is that feature offered as a comparison rather
than a rule: it writes nothing, and it is measured at twice a day plus one
rather than a weekend against par, which is a different quantity.)

Adding exactly `bath_size` adds exactly one bath, always, since
`ceil((n + b) / b) == ceil(n / b) + 1`. Nothing rounds.

**It fires on an observed event, never on inferred silence.** A `CloseRunRow`
at zero is a physical count, on a named night — `n = 1` is allowed to decide
something because nothing is estimating a rate from it. The mirror-image rule
is the one to be careful about, because it looks equally sensible and is not:
*skip a bath, this only sold one all season and the stock outlasts the
season.* On the live catalogue that flagged 76 products and **35% of the
baths being asked for** — and not one of the 76 survives a 95% bound on its
own sales figure, because long cover requires a low count by construction, so
the skip set is always made of products with nought to two observations. All
76 had seven or fewer on hand, so one customer with an armful clears any of
them. The sales ordering also already buries them: only **2 of the next 20
baths** the planner prints are in that set. Absence of sales over a handful
of trading days is not evidence of absence; a counted zero is not absence at
all.

**Par and the close are separate circuits, and this is the thing to check
before touching either.** `closing.expected_products()` gates on
`display_slots`, never on par — with one exception that is not about
production: a product at par zero with **nothing on hand** is left off,
because there is no belief there to check and the row can only come back
zero. Everything below concerns products with stock or a par, so changing par
changes nothing about which rows come up to be counted or which come back
zero. Dropping par to 4 would
not make this fire more often; it would cut the ask by two-thirds and
**silently delete every bath-5 product from the default sheet**, because
`behind_a_bath` is `shortage >= bath_size` and par 4 caps the shortage at 4.
Nothing would error. The sheet would just stop offering Artisan, Heavenly and
Noble.

**The bonus rides the ask and is never written into `par`.** Par stays a
deliberate human decision; a number that moved on its own is the failure this
codebase keeps naming. It is printed as its own line on the row — *+5 — sold
out at the close* — for the same reason `in_flight` is: a shortage that grew
has to say why as clearly as one that shrank.

Four things keep it from running away. Only the **latest** close proposes, so
each Sunday supersedes the last. `in_flight()` nets off a bath already
claimed by printed paper. **Answered rows only** — a pending row is "nobody
looked", never a zero, and the close is routinely worked in passes. And the
SQL prefilter had to widen (`Q(par__gt=0, number_on_hand__lt=F("par")) |
Q(pk__in=stockout)`), because a product sitting *at* par that still sold out
would never have reached the arithmetic — which is exactly the case the rule
exists for, since par being adequate on paper is what selling out disproves.

**And par turns out to be better than "a uniform remnant".** Across all 333
active dyed products there are nine distinct `(par, bath size)` pairs and
every one lands between **1.0 and 2.0 baths**. That is not a number nobody
chose — it is a MOQ floor with one bath of headroom, and it is the right
instrument for a signal this thin: you hold 4 because 4 survives ordinary
lumpiness, not because anyone computed twenty days of cover. The only
outliers are the bath-5 yarns at par 8, sitting at 1.6 baths where everything
else is at 2. Par still isn't dialled in *per product* — that is the
off-season question, where the window is 19 trading days rather than 5 and
the counts are large enough to mean something.

**`sold_per_blank()` is on the row because pooled ranking is blind in one
specific way.** A bath is planned in colorway units, so the sheet ranks on
what the *colorway* sold across every blank — correct, and it stays. What it
cannot see is one blank of a hot colour sitting on a full shelf having sold
none of its own; it rides up the list on its siblings. That is one or two
rows in the first twenty, which is too few for a rule and too many to leave
unsaid, so **both counted facts ride on the row and a person strikes it** —
which the sheet has always allowed. Nothing filters on them. It also keeps
the Sash Belt problem visible rather than acting on it: that blank sells ~85
units a season with **no colorway attribution at all**, so every one of its
48 colorways reads zero, and a skip rule would have quietly cancelled pots
for the colours that may actually be selling.

### The oven: a second session, planned to the box instead of to the work

**What actually happens, because the word "bath" invites the wrong picture.**
Nothing is dunked in a vat of dye. A yarn skein is zip-tied so it cannot
tangle, rinsed until it soaks, laid in a pan or a box, and the dye is applied
as it goes in — then the **oven or the microwave**, then dried and finished.
Silk is the same without the zip ties and **always the microwave**, then washed
in Synthrapol and softener and dried on gentle. So a *bath* here is a batch,
and the two sessions this app plans are oven work and microwave work. Anywhere
this file or the code says "pot", "stovetop" or "sink", it is describing a shop
that does not exist.

**The microwave is what sets `number_per_dye_bath`,** and it is a bulk limit
rather than a piece count: Rectangle Veil is 3, most silk is 4, the yarn bases
are 5, and Sash Belt is 8 because a 5x72 belt is at most a third of a 54x108
veil by weight. That is why the capacity needs no expression of its own — it is
already inside the bath — and why **only the oven is a box to plan to.** A
microwave session is bounded by how much work there is.

Some colorways are made in an oven rather than the microwave. The oven holds
**fifteen trays and one tray is one bath**, so `production.OVEN_TRAYS` is a
row count and nothing here needs tray arithmetic of its own.

**That equivalence is not a coincidence, and reading it as one is the mistake
to avoid.** The work was broken down from the physical limits *first* — the
size of a tray, what a pot holds, what one person can handle at once — and the
bath is the unit those constraints define. `number_per_dye_bath` is downstream
of the tray, not a batch size that happens to divide into it. So the absence
of arithmetic here is the schema being right rather than the feature being
lucky, and it is why an appliance could be added by counting rows.

**The equivalence is invariant, not just currently true.** Bulk is absorbed by
`number_per_dye_bath` — a chunky yarn simply fits fewer skeins in a tray, and
the catalogue already runs 3, 4 and 5 across its dyeable blanks (Artisan,
Heavenly and Noble at 5, Homespun and most others at 4). And anything that
would want two trays is **two baths**, not one bath spanning two. So a bath
never becomes a fraction or a multiple of a tray, whatever arrives on the
shelf; the ratio has nowhere to come from. That is why `OVEN_TRAYS` can stay a
row count rather than a capacity to divide into, and why nothing here should
ever grow a trays-per-bath field.

The lesson for the next appliance is therefore stronger than "check whether a
slot is a unit of work": **let the physical constraint define the unit in the
first place.** A capacity that has to be mapped onto a unit chosen for other
reasons is a sign the breakdown happened in the wrong order, and the
arithmetic that follows is the cost of that rather than an inherent
complication.

**It is the opposite planning problem from the microwave.** A microwave session
is bounded by how much work there is; an oven session is bounded by the box.
Running the oven is an *event* — it heats once whether it comes out full or
empty — so the picker plans *to* fifteen rather than to a number somebody
types, and the gap is the number the page is actually about.

**`FinishedProduct.oven_dyed` is the axis, because the technique is a property
of the blank *and* the colour.** It was on `Recipe` first, on the reasoning
that an oven colorway is oven-dyed on every blank it is dyed on, so flagging
the colour answers it once instead of a few hundred times. That is half true,
and the missing half is silent: **silk is never oven-dyed — it always goes in
the microwave** — so a colour dyed on silk and on yarn is oven work on one and
not the other, and a flag on the colour said "oven" for both. That took the
silk off the microwave sheet as well as putting it on a sheet for an appliance
it never enters, and the row looked like every other row either way.

It is the same shape as `RawProduct.made_in_a_dye_bath`, reached by the same
argument: a category test ("silk means microwave") is right today and wrong the
day something arrives that breaks it, and it breaks quietly. A typed answer per
pair is always right and can be looked at. Migration 0047 carried the old flags
over — every product of an oven colorway except the silk — and the category
test lives *there*, applied once to the rows as they stood, rather than in any
code that runs again.

**The entry cost is real and is paid on the row somebody is already saving.**
The recipes page draws one box per blank on the open colorway, labelled with
the blank's name, riding the Save that is already there — so a colour made in
the oven on three yarns is three ticks in one pass, and a mixed colorway is
expressible rather than flattened to whichever answer a single box held. The
badge on a closed row says `oven` when they agree and `oven on 2 of 5` when
they do not, because a summary that hides the disagreement hides exactly the
case the flag was moved to express. `private/production-needed/` prints the
share the same way, and the product admin carries the column for a bulk pass.

**It is typed and never derived, and there is a rule that makes that
tempting**: a colour name goes in the oven (Ochre, Cabernet), an idea doesn't
(Wasteland, Forest Fire). Don't build the classifier. It is a rule about the
world rather than about the string, and every version of it is confidently
wrong on the cases that decide it — matching the dye catalogue's own 145
colour words puts **Forest Fire** in the colour bucket, along with Twilight
Forest, Midnight Plum and Blue Eyes; word count doesn't rescue it, because
`Burnt Orange` and `Electric Violet` are two words and *are* colours while
`Sunset` is one and isn't; and the dye-book shorthand (`russet-cab-black`, 35
of them) is neither kind of name. The better correlate is a single dye in the
recipe — which is uncomputable today, since 91 of 162 active recipes have no
dyes recorded. The admin list shows `dye_count` beside the tick so that
correlate is *visible* without the app asserting it. Wrong here is silent: the
colorway lands on the other session's sheet and the row reads like any other.
Exceptions will exist that nobody knows about today, which is the whole
argument for a flag.

**It partitions both ways, and that is the load-bearing half.** An oven
colorway on a microwave sheet sends somebody to make a thing that is
not made there — the failure `made_in_a_dye_bath` already exists to stop, one
technique further in, and just as silent, because the row looks like every
other row. So `candidates(oven=...)` returns two disjoint populations and
defaults to the pot, which is what keeps every existing caller meaning what it
meant.

**A checkbox on the one picker, not a page of its own.** It is the first
question on the settings form, because it changes what every answer under it
means. This was built as a second route first and that was wrong: it is the
same job end to end — the same editable list, the same collection page, the
same three printed documents, the same QR coming back — so a second page is a
second thing to find and a second copy to keep in step. Nobody should have to
remember a different URL because of which appliance they are using.

**What that costs is that the tick has to be re-carried on every round trip**,
where a path carried it for free and could not be dropped. Four carriers, and
each miss is silent:

- the hidden `oven` in `#sheet-list`, which covers qty edits, a search
  result's add (via `hx-include`) and Print, since all three submit that form;
- the ✕ links, which build a whole new address in `_without`;
- the search form's own hidden copy, for a no-script Find;
- the checkbox itself.

Drop one and the sheet turns back into a microwave sheet mid-edit, with the
loudest symptom being a tray gauge that quietly stops being drawn. Drop it on
the Print path and the run is *stored* as a microwave sheet, after which the
run page refuses the oven colorways that are actually on it.
`test_every_control_on_the_page_carries_the_tick` walks the rendered page and
pins all of it, rather than trusting four templates to each remember.

**`private/production-needed/` deliberately does *not* filter on it**, and
badges instead — the badge links to `?oven=1`, which is the picker with the
tick already on. What is below par is a fact worth reading whichever way the
colour is made, and dropping the oven ones would leave a shortage visible from
nowhere. But the sheet *does* filter, so without the badge the two pages
disagree silently — somebody reads that list, asks the picker for the first
twenty, and gets a different set. The badge is that sentence, and it links to
the session that can answer the shortage.

**Filling the box past par is the one place the app suggests making something
that is not short, and it is not the display-capacity mistake wearing a hat.**
Worth being precise, because it is the thing most likely to be misread later.
What makes furniture-driven production bad is that it is *unbounded* and
*mistaken for demand*: a new rack gets filled from the bags, a backstock figure
reads empty, and nothing sold. The oven is neither. It is a fixed cost per
heating, so the last eleven trays are the cheapest of the year — the argument
`include_overshoot` already makes about a bath being a fixed size, at the
scale of a session — and it is bounded absolutely by a box nobody can enlarge.
It also runs *with* the northstar: a flat year means pre-dyeing as much of a
season as possible, and an oven run in February that comes out full is exactly
that. What would be the regression is topping up with whatever sold last
weekend, which is why the ranking is season sales pooled by colorway.

Three things keep `production.top_ups` advice rather than a decision:
**nothing is added** (a `+` per row, and the list only changes because
somebody clicked one); **the sold figure prints beside each one**, so the
basis of the ranking is checkable by looking; and **the panel disappears once
the box is full**, so it only answers a question the page is already asking.

**The gap is stated and never enforced.** A sheet at nine trays is a session
somebody has a reason for, and refusing to print it would be the app arguing
with a person who can see the calendar — the same call the short-blank warning
makes. Over fifteen is said out loud too, and still prints.

**Nothing about the oven is enforced, including which colorways go in it.**
That was built as a refusal first and it was wrong. `oven_dyed` is
typed by a person, from a rule with exceptions nobody knows about today —
which is the whole reason it is a flag — so refusing on it is the app
enforcing somebody's own provisional data back at them, at the exact moment
they are trying to say the data is wrong. Both search pickers keep a
mismatched colorway **clickable** and say what it is; `production_run_add_row`
warns and adds. The cost of being wrong is a row on a sheet, and a row is
strikeable.

The line is **who decided**: an undyed passthrough and a fancy veil stay
refused everywhere, because how a thing comes into being is a fact rather than
a judgement. A flag somebody typed is not that.

**A sheet can be an oven load plus a couple of pots on the side**, and that
falls out of the same decision. If the oven is running and two other colours
want a pot the same afternoon, two sheets for one afternoon is overhead for
overhead — so the tick says what kind of session to *suggest*, never what the
sheet is allowed to hold.

That creates one counting trap, and it is fixed rather than lived with: **only
the oven baths take tray space.** Fifteen trays plus two pots is not seventeen
trays, and a gauge reading `17 of 15` tells somebody to take out work the oven
was never holding. The picker and the run page both count oven rows for the
gauge and say the pots separately.

So the full list of what an oven run will not let you do is: nothing. Ten
trays print, sixteen print with the box's capacity said out loud, and a pot
colorway joins an oven sheet with a warning. The only cap anywhere near this
is `PickedBathsField.MAX_PER_ITEM`, which is a **typo guard rather than a
policy**: ten, because two digits in that box is almost always a number
somebody meant to delete half of — a stray keystroke turns 2 into 12 or 5
into 15, and both read as ordinary plans. That is a sharper test than "twenty
is unusual", which describes the mistake without catching it.

It is per colorway, and it does not bind the oven: a full oven is fifteen
trays of *different* colours, and nobody wants fifteen baths of one. The
count box's `max` reads the constant rather than restating it, so the client
limit cannot drift into being looser than the one that validates it.

**The three printed documents needed no changes.** The work sheet's stage
boxes carry no printed names on purpose, so oven stages already fit — which is
the decision in *The sheet is editable, and prints as two documents* paying
for itself.

### The collection page: blanks, then dyes

The sheet's first page is a shelf list, in the order the work happens: the
**blanks** to carry to the dye room, then the **dyes** to carry to them.

Blanks are one line per raw product with the total the sheet's baths consume.
**Nothing is filtered on stock.** A blank the app believes is out is far more
likely to be a number nobody has updated than an empty shelf, and leaving it
off would turn a stale count into a bath that never got dyed. The belief is
printed beside the requirement — `12 (we think 2 on hand)` — so a real
shortage is still visible, but the instruction is what to fetch, and the
sheet says so in its own header.

The dyes follow, each with a colour chip, the brand, and how many baths want
it. One walk to the shelf
instead of twenty, and the dyes several colorways share are exactly the ones
you don't want a second trip for. It's a page of its own rather than a block
above the rows so a long list can't squeeze them, and so it can be carried to
the shelf on its own — collection and dyeing are different jobs.

Counts are per *bath*, not per recipe, because "get the black out" and "get a
lot of the black out" are different instructions.

**A recipe with no dyes on file contributes nothing to that list**, which is
the one way this feature could do harm. Most recipes are in exactly that
state right now. An unannounced short list is worse than no list — you
collect what it says, walk to the dye room, and find baths whose requirements
were never written down — so both the sheet and the screen state how many
baths aren't covered and name the recipes.

The two say it differently on purpose. **On paper it's a warning**, because
the person at the shelf needs to know the list is short. **On screen it's an
invitation**: the missing recipes are listed by name and linked to
`recipe_showcase?missing=true`, which is the backlog with a dye picker one
click into each row. A count reads as a standing chore; six names read as
an afternoon with a payoff attached, and every one added shows up on every
sheet afterwards. That framing is the point — the backlog gets filled in by
somebody with other demands on their time, so the app's job is to make the
next increment look small and worth it.

Dyes marked out of stock are called out in both places. A missing dye is a
bath that can't run, and finding that out at the sink is the expensive
version of finding it out here.

### The sheet prints the bath's own amount

Each row of the working copy lists its dyes with the ounces **for that bath**,
not the ounces the dye book writes. The book's figures are for a five-skein
bath; a row is one bath of `number_per_dye_bath` units, and those are often
not the same number. Printing the book figure on a four-skein row would send
it out with a fifth too much dye in it, and nothing would say so until the
colour came out wrong.

`recipe_bath_dyes` does it, over `dyeamounts.bath_amounts`, which is the same
function the recipe page uses — one place where the division happens, because
the paper and the screen disagreeing about what goes in a pot is exactly the
failure `recipe_dye_names` was consolidated to prevent.

A blank whose table carries no dye-book basis — silk — prints its dye names
with no weights at all. See *Dye amounts* in `docs/claude/recipes.md` for why
that is a refusal rather than a gap.

**The collection page still pools dyes by bath count, not by weight, and that
is the job it is for: picking the dyes off the shelf, not weighing them.**
Weighing happens at the sink, over a particular pot, which is where the row's
own scaled figure is printed. "Get a lot of the black out" is what a walk to
the shelf needs.

A total in ounces would also be a sum over recipes whose amounts are mostly
not recorded yet — a confident-looking number quietly missing however many
baths have nothing on file.

### Photographing a marked sheet

`secret/production/upload/` takes a photo of a marked sheet and hands the
reading to that run's own page, already ticked. `scarves/sheetscan.py` does
the reading.

**One upload page for every run, not one per run.** Camera first: the photo
is what says which sheet this is, so there is nothing to navigate to before
taking it. That is what makes the QR do real work — it isn't a second
presentation of something the address bar already proved, it is the only
thing that names the sheet. The whole job is: mark the paper, scan the
collection page's SEND A PHOTO code, shoot.

**That code is on the paper because a bookmark is a step to be remembered.**
The page was reachable by knowing the URL or having bookmarked it, and nothing
in the print said so — the reporting sheet's QR opens that run, which is a
different thing and looks like the only thing. A camera-first page only pays
off if you can get to it without already knowing the address, so the address
is printed on the sheet that is already in the room.

Arriving at a run's URL first and tapping the boxes is the manual path. It
still works and is the fallback, but it means answering by hand the question
the photo would have answered.

**It applies nothing.** The upload page redirects to
`.../<token>/?done=12&done=15`, the boxes come up ticked, and a person
submits. Same rule `colorbands` follows.

**The reading rides in the query string, not the session.** It belongs to
that run's URL, which is what structurally stops one sheet's photo pre-ticking
another sheet's page — the session version needed a hand-written guard for
exactly that. The parameter is named `done` because that is the checkbox's own
name, so the URL is what the form would have serialised. Nothing is lost in
safety: a hand-edited `?done=` can only tick boxes a person could tick anyway.
Ids are parsed defensively so a stale link degrades to an empty form.

**The barcode does the hard part.** Every row prints one a fixed distance
from its box, so a decoded symbol gives the row's identity *and* the
position, scale and orientation of everything beside it. Finding a tick box
is then arithmetic rather than the general checkbox-recognition problem.
Geometry comes from `production.box_geometry()`, the same constants the PDF
draws with — a scanner with its own copy would drift, and drift lands the
sample window on blank paper and reads every box empty, which is
indistinguishable from a careful person who ticked nothing.

Two subtleties there, both of which bite silently:

- **Quiet zones don't scale.** reportlab pins them at a quarter inch, so a
  drawn symbol is wider than `BARCODE_WIDTH` by a margin that depends on the
  value. Scale comes from `bars_width()`, never the target width.
- **A decoder returns one result per distinct symbol, not per printed
  symbol.** Three identical barcodes come back as one, which used to matter
  because a sheet printed the same SKU several times — `plan_baths` groups
  repeated baths of a colorway together on purpose. Grouping the reporting
  sheet removed that collision at its source: a SKU appears on exactly one
  line. `line_code()` still carries the position as well as the SKU
  (`RAWSIL-STORMY#3`), now purely as a check on being pointed at the right
  sheet.

**Ink, not colour.** Each barcode is full-black bars on full-white paper a
couple of centimetres from its own box, so it doubles as a calibration
swatch: the dark and light ends of *this row*, under this light, at this
exposure. The box is scored on where it falls between them, which is a ratio
and survives white balance, a tungsten bulb and a glare on one corner. Red,
blue, green and pencil all sit far nearer black than paper; **yellow does
not** and never will, which is why the sheet says "any pen but yellow".
Anything between the thresholds is `unsure` rather than guessed.

**The likely failure is the photograph, not the sheet.** Soft focus, a
hurried frame, a bad photocopy — and it fails *partially*, taking out some
rows and leaving others. So the run page reports how many rows it read
against how many are on the sheet: a count of what was found reads as a
complete answer unless something says what was missed.

**Decode three times and pool the findings**, because the passes are good at
different things and no single one of them is best. Measured on a real 3024px
iPhone photo of run 5:

| pass | rows | QR |
|---|---|---|
| as-is | 11 | ✗ |
| 2× lanczos | **12** | ✓ |
| 2× lanczos + unsharp | 1 | ✓ |

Four decisions come out of that table:

- **The plain enlargement is the workhorse**, and this is the surprise. It was
  the only pass that read the whole sheet, and it recovered a QR the
  native-resolution pass missed entirely — which on the upload page is the
  difference between a photo that names its run and one that asks somebody to
  type a code off the paper.
- **Sharpening wrecks row barcodes** — twelve down to one. It survives as a
  third pass only because it is what rescued the QR out of a 1308px
  screenshot where every row was hopeless anyway, and pooling lets it
  contribute that without taking anything away.
- **Stopping at the first pass that finds a row is wrong.** That is pass one,
  with eleven rows and no QR, so an early exit throws away both the twelfth
  row and the only thing that names the sheet.
- **A row is claimed when it *scores*, not when it decodes.** A code can
  decode on one pass and still have its box fall outside the frame or refuse
  to score; retiring the code at the moment it decoded means the enlargement
  that would have read it properly never gets its turn. That was two of twelve
  rows.

Each pass carries the scale it is drawn at, because pooling makes every pixel
coordinate ambiguous otherwise: a row found at 2× reports a `top` twice the
size of the same row at 1×, and sorting pooled marks on that puts row one in
the middle of the page.

**Resolution is a floor nothing can lift.** A 7.7 mil module needs about two
pixels to survive and a sheet is 8.5in across, so a photo under ~2200px wide
cannot hold a readable row barcode however it is processed — measured on a
1308px picture where the modules landed on 1.18px and **not one of thirteen
decode attempts read a single row**, 4× upscaling and sharpening included.
Interpolation cannot invent a sample that was never taken. `too_small_for_rows`
says so, because "couldn't read that photo" and "that photo is too small to
hold a row barcode" send somebody to do completely different things — retake
it, or stop retaking it and tap the boxes.

**The QR alone is a good outcome, not a failure.** `named_but_unread` is its
own state: the photo's job on the upload page is to say which sheet this is,
and the boxes are the bonus. A screenshot of a sheet reads no rows at all and
still lands you on the right run with the boxes ready to tap, which is the
whole flow minus the shortcut.

When the QR itself can't be read, the upload page asks for the code printed
beside it (`42-brisk-wombat` — words, because someone types this off paper;
`normalize_token` makes case and punctuation irrelevant). That is nearly
always a soft photo rather than a wrong sheet, so it is a way through rather
than an interrogation. Rows in the photo that aren't on the named run are
reported too — expected to be empty forever, but the matched marks would
otherwise land there unremarked.

**The photo is not stored, unless `KEEP_SHEET_PHOTOS` says otherwise.** It is
an input to a form, not a record — the record is the inventory log.

**That toggle is a temporary measure and is meant to be turned off again.** It
is on to collect real photographs of real sheets, because tuning the scanner
means holding the thing that failed: a scan is optics — focus, curl, glare,
the angle a page was lying at — and "it didn't work" with the input already
discarded is a bug report nobody can act on. Once there are enough to look at,
set it to `0` and the page behaves exactly as it always did.

**It is built as logging, which is what keeps it disposable.** Nothing in the
database points at a stored photo — no model, no row, no admin — so turning
the toggle off leaves nothing behind and nothing to migrate away. What a row
would have carried is in the key instead: `sheetscan.photo_key` gives
`sheet_photos/20260909T220134-18-tranquil-bobcat-w3024-r11-f9-u2.jpg`, so a
listing of the prefix sorts into time order and the ones worth opening say so
in their own names (`r0` read nothing, `unnamed` decoded no QR). The same
summary goes in the log line, because a log line pointing at a blob you have
to open to learn anything is half a log. Writing it can never affect the
request, so a bucket having a bad afternoon cannot cost somebody the reading
of a sheet they are standing there holding.

**Retention belongs to the bucket, not the app.** `set_bucket_lifecycle`
expires the prefix after a week, so the photos age out whether or not anybody
remembers the toggle — a log rotated by the program that writes it stops being
rotated the first time that program stops running. **S3 has no per-object
TTL**: expiry is a bucket rule matched on a prefix, which is why these have a
prefix of their own.

**The scanner is tested against a real photograph**, kept at
`scarves/testdata/marked_sheet_run5.jpg`, because a synthetic one cannot stand
in for what is being tested: every generated QR tried decoded on the first
pass, where the real one needed an enlargement. Focus, curl, glare and the
angle a page was lying at are not reproducible from a matrix.

**Anything committed there has to have a dead token first.** This repository
is public, and a production sheet photographs its own code — which opens a
page that moves stock with no login — alongside a page of stock levels. Rotate
or revoke it in the admin, then check it the way a stranger would: fetch the
URL against a control that is known-bad, because a 404 nobody verified is a
404 somebody assumed.

**Marking is positive only.** Tick what you did; never cross out what you
didn't. Pen through a Code128 sometimes still decodes and sometimes doesn't,
so the signal that matters would ride on the unreliable mark, and an unmarked
row would stop meaning anything definite.
