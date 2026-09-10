# Production: what to dye, the sheet, and the scan back

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

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

**Furthest below par first is one click away, and neither ordering filters.**
Every colorway is listed either way, so the choice changes what gets read
first and never what exists — a page that hid the quiet ones would be making
the retirement decision on its own.

The figure is `slowsellers.sold_by_recipe`, the same function
`private/slow-sellers/` reports from, **pooled across every blank a colorway
is dyed on** because that is the unit a bath is planned in. One answer to
"what sold": two would let the page that ranks on it disagree with the page
that reports it. It prints beside each colorway so the ranking can be checked
by looking rather than trusted.

A colorway that sold nothing is ranked last, never hidden. It may simply be
new — 2026 is year one for colorway data — and this page is not where that
gets decided.

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
the bath never ran: nothing was decremented, so no blanks were consumed, and
the claim on the planner is released so the colorway comes back on the next
sheet. Checked with an actual of 0 means the bath ran and the lot was binned,
so the blanks really are gone. Same code path, `yielded` at 0.

**Yield asks one number: how many came out.** The bath consumes its blanks in
full whatever happened in the pot — dye four, ruin one, four blanks are still
gone — so `apply_row` takes `quantity` off raw and puts `yielded` onto
finished, and the difference is a loss rather than a discrepancy. Raw is
allowed to heal at the next booth count; only raw speeds a reorder, while
finished reaches the peg, the close and Square.

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
something that isn't one — an order taken at the stall, room left in a pot
already being heated. A plan nobody can edit gets worked around on paper, and
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
add the two you promised somebody, drop the one you have no blanks for. They
sit side by side with no toggle. An earlier version made them modes and it
came out wrong in a way worth recording: a suggested list you could only look
at, an editable picked list, and a read-only preview of the first sitting
underneath the editable copy of the second. **A suggestion you cannot change
is a suggestion somebody works around on paper**, which is the whole failure
the editable sheet exists to prevent.

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

### A Sunday-night zero adds a bath — the one demand signal in the planner

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
that proves it is the reason this is a bath rule instead.

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
`display_slots`, never on par — so changing par changes nothing about which
rows come up to be counted or which come back zero. Dropping par to 4 would
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

Some colorways are made in an oven rather than in a pot. The oven holds
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

**It is the opposite planning problem from the dye room.** A stovetop session
is bounded by how much work there is; an oven session is bounded by the box.
Running the oven is an *event* — it heats once whether it comes out full or
empty — so the picker plans *to* fifteen rather than to a number somebody
types, and the gap is the number the page is actually about.

**`Recipe.oven_dyed` is the axis, because the technique is a property of the
colour.** An oven colorway is oven-dyed on every blank it is dyed on, so
flagging the recipe answers it once instead of a few hundred times. It is a
checkbox on each open row of `private/recipes/`, riding the Save that is
already there — the person filling in a colorway's dyes is the person who
knows which box it is made in, and a control with its own button would be a
second trip through every row. It is `list_editable` in the admin too.

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
colorway on a dye-room sheet sends somebody to a sink to make a thing that is
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

Drop one and the sheet turns back into a dye-room sheet mid-edit, with the
loudest symptom being a tray gauge that quietly stops being drawn. Drop it on
the Print path and the run is *stored* as a dye-room sheet, after which the
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
That was built as a refusal first and it was wrong. `Recipe.oven_dyed` is
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

### Photographing a marked sheet

`secret/production/upload/` takes a photo of a marked sheet and hands the
reading to that run's own page, already ticked. `scarves/sheetscan.py` does
the reading.

**One upload page for every run, not one per run.** Camera first: the photo
is what says which sheet this is, so there is nothing to navigate to before
taking it. That is what makes the QR do real work — it isn't a second
presentation of something the address bar already proved, it is the only
thing that names the sheet. Bookmark the upload page and the whole job is:
mark the paper, open it, shoot.

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
