# Stock you order rather than make, and the fancy veils

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Raw inventory: one save, because what goes in is a bill

`private/raw-inventory/<category>/` is the reorder workflow — **you order
these, you don't make them** — and the thing typed into it is a supplier's
invoice. So it is **one form with one Save**, not a submit per row. It used to
be the latter: three nudge buttons and a "set" box per product, each its own
`<form>`, which made a nine-line delivery nine round trips and nine page
rebuilds with the paper in somebody's other hand.

**Two columns, because there are two questions and they are not the same one.**

- **Received** is a delta. The note says twelve arrived, and nobody should
  have to add twelve to the current figure in their head first. Signed, so a
  return to the supplier is a delivery note with a minus in front of it —
  which is what the old row's `-1` button was for.
- **Counted** is an absolute: the shelf holds nine, whatever the app believed.
  That is the shape every correction in this app takes, because an absolute
  heals whatever went unrecorded before it while a delta only works if
  everything before it was right.

Blank means untouched, which is what makes a nine-line bill cheap on a page of
forty products. **Counted wins if both are filled in** — a count is a
measurement, a delivery note is a claim about a change.

**Nothing is applied unless every line reads.** A bill is one document, and
half of one booked in is worse than none because the missing half is invisible
afterwards. An unreadable figure re-renders the page with everything still
typed and names the offending line under its own row — losing nine lines to
one fat-fingered digit is the expensive failure here, and "a line didn't read"
without saying which one is a hunt across forty products.

Rows are written with `save()`, never a queryset `update()`, because the
`post_save` on `RawProduct` is what mirrors the count onto a passthrough's
finished row — one physical pile, one row allowed to count it, and an
`update()` would leave the two disagreeing silently in the direction that
decides when to reorder.

No `InventoryLog` is written here, which is unchanged rather than an omission
introduced with the form. Raw stock is an opening balance that gets counted
and topped up; the finished side is where provenance is tracked.

### The fancy blanks are not on this page, in any of its three modes

Every mode of `private/raw-inventory/` asks a question about a supplier —
book this delivery, order this shortfall, what does one cost — and a fancy
blank has no answer to any of them. It is a plain scarf somebody added line
work to: no delivery of one ever arrives, nothing counts a pile of them
undyed, and its cost is the plain blank's plus `fancying_cost`, derived by
`blank_cost`. So it sat on the bill as a row of boxes that could only be left
empty, on *Plan an order* as a shortage annotated *made here, not ordered*,
and on *Cost & supplier* as a `price` box beside a number nothing reads —
the second door that leaves two copies of one cost disagreeing.

`views/stock.py::_shelf` is the one query all three modes and both saves go
through, and the picker's counts are annotated the same way: **a card that
promises a row the table will not show is worse than one that undercounts**,
because the count is why somebody clicks. The page names the rows it leaves
out and where they went — `private/fancy/` to turn stock into one,
`private/blanks/` for what one *is* — since rows that vanish with no
explanation are rows somebody goes hunting for.

**The test is `bought_in()`, the fancy pairing, not `made_in_a_dye_bath`**,
and that is not a stylistic choice. The flag answers *can a bath produce
this*, which is a different question from *do you buy it* — and it is a typed
answer, so it can be wrong in a way the pairing cannot. `Cotton Pima DK` was
live with it unchecked and nothing pointing at it: a $6.80 yarn from
Wool2dye4, a listing, forty on the shelf. It has since been set right, which
is exactly the point — keyed on the flag, that row would have vanished from
the page its delivery gets booked on for as long as the mistake stood, and
nothing would have said so. Keyed on the pairing, the worst a mis-set row
costs is one line too many on a list. The property
(`RawProduct.is_bought_in`) and the queryset are the same test in two shapes,
pinned together by `FancyBlanksAreOffTheReorderPageTests`.

## `private/raw-inventory/blank/`: what a blank *is*, which had no door

`RawProduct` is not registered in the admin and no page created one, so until
this existed a new blank could only come from a migration or the Square sync.
Adding the first new silk in a year meant opening a shell — and an invoice
carrying something the catalogue had never heard of had nowhere to put it.

The picker lists every blank grouped by the table it sits on, **retired ones
included, listed apart rather than hidden**: retire-don't-delete means the row
is still pointed at by every sale and production run it was part of, and a
retired blank nobody can find is one somebody makes a second copy of.

**The shelf is not on the editor.** An opening count is offered once, at
creation, and then the field is gone — not greyed out, gone, because a
disabled box still reads as *this is where that is changed*. Counting happens
on `private/raw-inventory/`, where two columns say plainly which question is
being answered, and a third box on a third page writing the same number is
the second door that leaves two of them disagreeing.

Three other fields are deliberately absent. `invoice_description` is written
by confirming an invoice line and never typed. `square_item_id` belongs to
the sync — typing one by hand is how a variation ends up pointed at the wrong
shelf, and the sync repairs nothing it did not write. `counted_at` is a
record of somebody physically counting, and a form that could set it could
claim a count nobody did.

**An empty cost is taken, not refused.** `price` is not nullable, so a gap
becomes zero — and the picker names the uncosted blanks rather than counting
them, because those are rows somebody has to go and fix and a bare number
sends nobody anywhere. "I don't know what this costs yet" is most of a first
pass.

### The rows a blank implies, all made from the blank that implies them

**Some blanks are one row and some are two, and which is which is invisible
on the blank.** A plain silk veil is one `RawProduct`. A veil that also comes
fancy is two, because the fancy version is its own blank with its own cost. A
yarn sold undyed is a `RawProduct` *and* a `FinishedProduct` with no recipe,
because the pile and the thing Square sells are different objects.

Neither second row could be made from a page. `scarves/blanks.py` now owns
both, and the editor offers each as a checkbox on the blank that implies it.

**"It also comes in a fancy version" makes the fancy blank and links it.**
The old shape asked for the fancy one to be created first — marked made-here
— and then for somebody to come back and point the plain one at it. That is
the app's bookkeeping in a particular order, announced nowhere, and what it
produced was an empty dropdown that reads as *this is broken* rather than *do
the other one first*.

Making it eagerly is safe, and the asymmetry is the argument: a fancy blank
nobody sells has par 0, no stock and no colorways — it appears on no
production list (nothing can dye it) and asks for no order (nothing buys
it). The cost of the missing row is a page that looks broken; the cost of a
spare one is a line in a picker. Its `price` is zero on purpose and is not a
gap: a fancy blank has no supplier, and what one costs is the plain blank's
cost plus `fancying_cost`, derived by `blank_cost`.

The dropdown survives for one job — pointing at a fancy blank that exists and
is unlinked — and is offered **only when there is such a blank**, since an
empty select is the thing that read as broken.

**"Also sold exactly as it arrives, undyed" makes the sellable row.** This
was the other question with no answer on any page: an undyed yarn needed
`manage.py create_passthrough_products`, which is not a door. Ticking it
creates the null-recipe `FinishedProduct`, priced at the blank's
`suggested_price` — or at a conspicuous **$1.00** when there isn't one, the
same rule and the same reasoning the command has always used, now shared
rather than copied. A deliberate zero is honoured, because a giveaway is a
real product; only a missing price is replaced.

Note what ticking it does *not* say. The blank stays **bought from a
supplier** and stays dyeable: selling a yarn undyed is a fact about a
colorway with no recipe, never about the blank. That is the same confusion
the origin radio above exists to prevent, arriving from the other side.

**"Make it in these colourways" is the expensive one, and the reason this
section exists.** `CLAUDE.md` says a new product is almost never a new
*style*, it is another colour of something that already exists — and the
corollary is what happens when a genuinely new blank does arrive: it needs a
finished product *per colourway*, and there are a few hundred. Forty of
those typed by hand is a job that gets half done.

Everything about each row is already decided by the blank, which is what
makes it bookkeeping rather than a decision: the name is `{blank} -
{colourway}`, par is `finished_par_default`, display slots is
`display_slots_default`, price is the blank's `suggested_price` (or the same
conspicuous $1), and the SKU fills itself in `FinishedProduct.save()`.

**Checkboxes with the recipe's dyes beside each name**, not a multi-select.
Two reasons, and the first is the one that matters: a multi-select loses
thirty picks to one stray click with nothing said, which is the
silent-destructive failure this app keeps designing away from — and its only
warning is a modifier key nobody has been told about. Every click here is
independent.

The second is that a colourway is a name like `Babs` or `Forest Fire`, which
says nothing about what it looks like to anybody who has not dyed it. So each
recipe's dyes show as a chip each, **in the recipe's own order and never
blended into one average** — a scarf shows each dye distinctly and flows
between them, so an averaged swatch would be a colour that is not on the
product. A dye with no hex gets hatching rather than a guess, the rule
`.dyechip` follows everywhere. A recipe with no dyes on file says so rather
than rendering blank, because silence there reads as a colourway with no
colour instead of a gap in the dye book — production has dyes on most active
recipes, so this is the exception there; a development copy of the database
is usually half empty and looks much worse than the real thing.

Measured rather than assumed: 162 colourways render in **3 queries** (two
prefetch levels, flat in the number of rows) and 91 KB, 12.7 KB gzipped.

Two things it deliberately does *not* do. **Retired colourways are not
offered** — a colour somebody decided to stop making, offered again on a new
blank, is the retired-recipe failure with a fresh door onto it. And
**`oven_dyed` is left off on every one**: it is a typed flag about this
blank in this colour, the same colour is oven in one yarn and not in
another, and inheriting it from another blank's row would be a guess that
reads as a fact.

The save says what it did — *2 colourways made, 1 already existed* — because
a form that quietly creates forty products is a form nobody can check, and
the skipped count matters as much as the made one: picking a colourway the
blank already has is the ordinary way somebody adds the next few.

**No box ever un-makes anything.** Unticking does not delete a fancy blank, a
sellable row or a colourway — those are rows with history, and retiring is
how something goes away.

### One flag, two readings: where a blank comes from

`made_in_a_dye_bath` is the **fancy marker and nothing else**, and the
catalogue says so plainly: every undyed yarn has it **True**. Those blanks
are dyed into colorways *and* separately sold exactly as they arrive — and
the second half is a colorway with no recipe (see *Undyed stock* below),
never a statement about the blank.

The editor first asked it as a checkbox, *a dye bath can produce this*. For
`Superwash Merino Zebra DK — 10 x 100g SKEINS`, sold undyed, the honest
answer to that question looks like **no**. Answering it that way would have
marked the yarn fancy, dropped it off every production list, and routed its
baths to a counterpart blank that does not exist — silently, and with the
page agreeing.

So the question is asked as what it actually decides, with two named origins:

- **Bought from a supplier** — it arrives on a delivery, and dye baths make
  colorways of it. Nearly everything.
- **Made here from another blank** — a bought one with extra work added,
  which today means the fancy veils.

Nobody calls Superwash Merino Zebra DK *made here from another blank*. The
ambiguity is gone because the words no longer mention dyeing — and the
picker's badge moved the same way, from *no bath makes this* (true of the
blank, and sounding true of every undyed yarn) to *made here, not bought*.

**The fancy pairing has an order, and the form now says it.** The dropdown
can only offer blanks already marked made-here, so the fancy one is created
first and the plain one is pointed at it afterwards. With none on file the
field used to render as an empty select, which reads as *this is broken*
rather than *do the other one first*; it now says which, and is disabled
until there is something to choose. A made-here blank is not asked the
question at all: a veil becomes a fancy veil, and a fancy veil becomes
nothing.

That also makes "a blank can't be its own fancy version" **structural** — it
is excluded from its own queryset and the field is absent on the blanks that
could self-reference — so the `clean()` check for it was removed rather than
left as code that can never run. A guard that cannot fire reads as the thing
keeping the rule true.

### Booking one into existence off an invoice

An invoice line that matches nothing gets **new blank from this line**,
prefilled from the line and opened in a new tab — the tab because leaving the
review page would lose every other correction typed into it. The parameters
are named after the fields they fill (`?name=&price=&supplier=`), so the link
still reads a year later.

`invoice_description` rides along hidden and is applied on save, which keeps
the promise that only a confirmation writes it: creating the blank *is* the
confirmation. The line is then matched exactly, before any reading, every
time it arrives again.

**The name is trimmed and the wording is not**, which is the whole
distinction between the two fields. `Angel DK - 10 x 100g SKEINS` is how
many came in a box; that name would go on reference sheets, barcode labels
and the Square till, where the pack size is noise on all three and wrong the
day the supplier changes it. So `invoiceread.product_name` takes a trailing
pack clause off the *name* prefill, and the wording keeps every character —
it is a key, and a tidied copy would quietly stop matching.

The trim needs a separator before the count, which is what keeps it off the
one case that must survive: `Machine Hemmed 8mm Habotai Scarves 21" x 76"
Circle` has an `x` and two numbers and is a **size**, not a pack. Strip that
and the blank loses the thing that tells it from every other habotai. It
errs toward trimming otherwise — a wrong trim costs one correction in a box
somebody is already looking at, a missed one costs a pack size printed on a
hundred labels.

## `?supply=1`: the two columns the page printed and could not fill

`private/raw-inventory/<category>/?supply=1` is the third mode, beside
**Receive stock** and **Set par**: a cost box and a supplier-link box per
blank, one Save for the lot.

**It exists because the page had been showing both figures since it was
written and offered no way to set either.** Every row printed a *Cost* and a
*Supplier page* link — the two facts a reorder actually needs — and the only
door to either was the Django admin, one product per screen. Twenty notions
is twenty round trips through a form built for a developer, which is why
every imported one still read `$0.00`. The import cannot know a cost, and
nothing since had made it cheap to say.

**Why a third mode and not two more boxes on the bill.** The same argument
that split par off: a cost typed beside a delivery and then lost to the
wrong button is the identical failure, written nowhere and said nothing
about. One mode, one form, one meaning per button.

**The ordering is the advice, and it is the only thing here that is.** This
mode sorts by lifetime revenue, biggest first, where the other two sort by
name. That is deliberate rather than inconsistent: the bill and the count
are worked row by row against a piece of paper or a shelf, where
alphabetical is what lets you find the line in your hand. A costing pass is
worked top-down until the hour runs out — and the hour is not evenly worth
spending. One season put half its notion revenue into two products and left
eleven of nineteen under $100 for the year. Alphabetical puts the $1 buttons
first.

Revenue is lifetime rather than this season, because a supplier link does
not expire with the faire and a blank that earned well last year and has not
shipped this one is exactly the row worth stopping on.

**A blank box is untouched, and this one matters more than on the other two
modes.** `RawProduct.price` is not nullable, so a stored zero and a cost
nobody knows are the same row. Reading an empty box as a decision would turn
a gap into a claim of free — and "I don't know this one" is most of a first
pass.

**Nothing is written unless every line reads**, the bill form's refusal for
the bill form's reason: half a costing pass applied is worse than none,
because the half that failed is invisible afterwards and the half that
landed looks complete. Losing a page of looked-up suppliers to one missing
`https://` is the expensive failure here, so a refusal re-renders with
everything still typed and names the offending row under itself.

**The margin prints beside the box**, which is the payoff made visible where
the typing happens. An uncosted row says *not costed* rather than showing
the full asking price as margin — that is the one wrong answer that reads as
good news, and it would be on every row of a shelf nobody has costed yet.

It writes no `InventoryLog` and moves no stock. A cost is a fact about a
supplier, not about a shelf.

## `Supplier`: who you buy from, which is not where you buy it

`RawProduct.order_url` answered *where do I buy this exact blank* and there
was nothing that answered *who am I buying it from*. The second question had
no home at all, so everything true of a supplier — the contact, how long
they take, that they ship in packs — was written nowhere.

**The evidence was the repetition.** 25 active blanks carried an
`order_url`, and those URLs resolved to **three** domains: Wool2dye4
eighteen times, Dharma six, Knomad once. Eighteen copies of one supplier is
eighteen places to edit and eighteen chances to edit only some.

**It does not replace `order_url`**, and collapsing the two would be a real
loss. Those URLs are *product* pages —
`wool2dye4.com/suri-silk-cloud-mini.html` — so a blank keeps its single
click to the thing being reordered. `RawProduct.reorder_link` holds the
preference: the product page wins, the supplier's card is the fallback,
`None` when nothing is known. One accessor, because two pages print that
column and a rule restated twice is a rule that drifts.

**The fallback is the whole reason this exists.** A good deal of what this
shop resells comes from a person at the next stall — the yarn bowls from
Wild Yam Pottery, the yarn cards from a neighbour — and they have no store
page and never will. Before this the reorder column simply rendered empty
for all twenty notions. The fix is a card of our own to link at, not a text
field pretending to be a URL: `private/suppliers/<id>/` carries the contact,
and `private/suppliers/` is its picker, per the rule that a parameterised
page always gets one.

**One blank, several listings — `order_url` is one per line.** The yarn
cutters are the case: three or four Amazon listings, $1 to $3 each, sold
for $7 and indistinguishable at the till. The crew ring every one as *yarn
cutter* whatever the till offers, so a product per listing would produce
four counts nobody records and one real total — the number that looks
precise and was never rung is exactly what this app is built to avoid. One
product, then, and the column widens to hold a line per listing.
`RawProduct.order_urls` is the list, `reorder_link` is the first line as it
always was, and `extra_order_urls` are the rest, printed as small numbered
links beside *Supplier page* on the two pages that carry the column. The
blank editor takes the lines in a textarea and refuses any that is not a
link, naming it. The `?supply=1` box **adds** a link rather than replacing
the list, because that box is for filling a gap in a hurry and typing a
fifth must not throw away four — the editor is where a wrong one comes
out. Not a table of links with a label and a price: the listing itself
carries both, and the invoice records what was actually paid. If a listing
ever earns its own row — customers asking for one cutter by look — it gets
one, the way a colorway does.

**Retire, don't delete.** `supplier` is `PROTECT`, like everything else that
records what happened. A supplier with blanks pointing at it is one you have
bought from.

**A null `lead_time_days` means nobody has said, and nothing derives a date
from it.** Zero would mean "arrives today", and an order-by date computed
off a guess is the par mistake with a delivery on the end of it. The picker
prints *lead time not known* rather than a zero.

**The backfill ships with it** (`0052`), creating the three suppliers the
data already implied and linking the 25 blanks. Shipping an empty model and
asking somebody to retype what is already in the catalogue would be the
wrong way round. **It matches on the URL's domain and on nothing else** — a
domain is evidence about who sells a thing, a product name is a coincidence
— so anything unmatched is left for a person, which is every notion.

### What is deliberately *not* here yet

**Pack size.** `RawProduct.notes` is not free text: it holds ordering data
in two shapes, the silk's variant (`45" x 104" Semicircle`) and the yarn's
pack line (`Package cost: $87.10 for 10 skeins`). So a blank is bought ten
or twenty-five at a time, and the reorder page's *38 to order* is not an
order anybody can place.

**Be careful how large a problem that is called, because it is a small
one.** The pack sizes are known — they are simply known by the person
ordering rather than by the app, and he converts on the way to the basket
without noticing. Nothing has gone wrong because of this and nothing is
likely to. `price` is the pack line divided by hand and two of six blanks
are a penny out, which is **not** the fancy-blank-cost failure wearing a
different hat: that one drifted $8.96 to $17.30 across rows nobody could
reconcile, and this one rounds. Do not cite it as evidence of anything.

The honest case for recording it is smaller and still worth acting on: it
is one person's knowledge, held nowhere else, and writing it down costs
almost nothing. That is a different argument from a correctness fix, and
overselling it is how a nice-to-have gets built ahead of something that
matters.

It belonged with **invoice ingestion**, and that has now happened — see
*Invoices* below. A `SupplierInvoiceLine` carries the quantity, the total and
a per-unit that derives rather than being typed, so the pack lands as a
by-product exactly as predicted. Note what that did *not* change: no field
was added to `RawProduct`, and `price` still means one thing, what one costs
to buy again today.

**Delivery history** is the other half of that and is still not a ledger. A
booked invoice records what one document moved, which is genuinely more than
was there before — but it is the *document's* record, kept because a cost
needs a basis and a delivery needs an identity. It is not an `InventoryLog`
for raw stock, nothing reconstructs `number_on_hand` from it, and a shelf
topped up through the bill form still leaves no trace anywhere. Raw stock
remains an opening balance that gets counted and topped up. Reversing that
is a decision to argue for on its own.

## Invoices: the one place a cost and a delivery share a button

`private/invoices/` takes a supplier's invoice — a PDF, a photo of the
paper, or text pasted out of the email — reads it, and hands back a page of
rows to confirm. Booking it writes the unit cost onto each blank; the
quantity reaches the shelf when the goods do, which is the same click if they
are already here and a second one later if they are not (see *Ordered, then
received*).

**That pairing is a deliberate exception to the rule two sections up**, which
splits the bill, par and supply into three modes precisely so that a number
typed beside the wrong button can't be lost. Here they share one Save, and
what makes that safe is not carefulness — it is the **order number**.

### The order number is the whole design

Everything this shop orders arrives with one. It is the only fact that makes
a bill *identifiable* rather than just a list of quantities, and a received
quantity is a delta:

- a delta is only ever as good as the certainty it hasn't been applied
  before;
- raw stock is the one pile nothing recounts on its own (see `counted_at`
  above), so a double-booked delivery does not heal;
- it would sit there inflating the shelf until somebody walked it with a
  clipboard, and then under-order for a season.

So the number is required to book, normalised (`  w2d-1183 ` and `W2D-1183`
are one order), and a second attempt at one already booked stops the page
dead with a banner that names the first: when it was booked, how many lines,
and a link to it.

**It is loud, not locked.** A tick on the banner books it anyway. Two
suppliers can pick the same number, a number can be mistyped, an order can
genuinely be re-sent — and a page that simply refuses gets worked around by
typing a `-2` on the end, which teaches nobody anything and leaves the app
believing a fiction. The property that was needed is that it cannot happen
*quietly*.

### What is on order is not under par

**Par is an order signal, not a stock reading.** A blank whose replacement is
on a supplier's van is not under par in any way that should make a page
flash: a second order will not make the first arrive faster. (Better shipping
might — and that is a new order, placed deliberately, not a signal firing
again.) So `RawProduct.raw_shortage` subtracts `on_order`, exactly as
`production.in_flight` subtracts baths already on a sheet. However it got
claimed, it is accounted for.

**It is not added to `number_on_hand`, and that line is the important one.**
That number is what a production run claims its blanks from, so yarn in
transit must stay out of it or the dye room gets sent after skeins that are
not in the building. On order and on the shelf are different facts, and only
one of them can start a bath.

**The risk this takes on is an order that never arrives** — the signal would
stay suppressed for good, silently, which is the worst shape a bug can take
here. Two things answer it, and neither is automatic. Every page printing a
shortage prints what is holding it down — *250 on order since 11 Sep*, and
*past their usual lead time* when the supplier's `lead_time_days` says so; a
null lead time claims nothing, since nobody has said. And when it really is
not coming, `written_off_on` closes it and hands the signal back (above).

That amber note is the whole guard, and it is the right shape: a suppressed
signal with a visible reason is advice, and one without is the `par` mistake
wearing a delivery note.

### Ordered, then received or never — three dates, not a status

**Most of these documents are order confirmations, not delivery notes.** The
paste that proved the reader was `Order #135819 Sep 14`, sent the day it was
placed. Booking one used to put the goods on the shelf the moment it was
read, and `number_on_hand` is what a production run **claims its blanks
from** — so the dye room could be sent to start baths against 350 skeins
still in a van. That is the same coupling *Display capacity is not demand*
exists to keep broken, arriving by a different door.

So a booking splits in two, and the split follows what is actually knowable:

- **The cost lands at order time**, always. It is the replacement cost — what
  one would cost to buy again today — and it is true the moment the order is
  placed. Holding it back until the box arrives would leave the catalogue
  quoting a price nobody charges any more.
- **The stock lands when the box does.** One click on the invoice card
  replays the lines somebody already settled, and adds them.

**`received_on` is a nullable date, not a status field**, because every other
not-yet-happened in this app is one: a null `booked_at` is a draft, a null
`counted_at` is a shelf nobody counted, a null `lead_time_days` is nobody
having said. A date answers *when* as well as *whether*, which an enum never
can.

**There is a third ending, and it is a third date rather than a status.** An
order can also never arrive — lost, undeliverable, cancelled — and
`written_off_on` records that, with a free-text `written_off_note` for why.
It closes the order and **moves no stock**, which is exactly why it cannot
be folded into `received_on`: a received date says goods are on a shelf, and
putting one there would claim a delivery that never happened. Two mutually
exclusive events, each with its own date; `SupplierInvoice.state` derives
the word from them so a status and a date can never disagree.

**What the write-off really undoes is the suppression.** An open order takes
a blank's shortage off the reorder page (below), so one that is never coming
would hold that signal down for good — a page that looks fine and an order
nobody places. Nothing expires an order on its own, because a rule deciding
when an order is dead would be a rule deciding to reorder; a person presses
it, and the confirmation names which blanks just went back below par.

The costs stand when an order is written off. They were true when it was
placed and are still what the supplier charges — `price` is the replacement
cost, not a record of this document — and unwinding them would revert to a
price nobody can name.

**Receiving a written-off order clears the write-off**, because a parcel
turning up three weeks after it was given up on is completely ordinary. Same
button, no undo step first.

The question is asked **once, on the review page, as a radio** — not two
submit buttons. Pressing Enter in a text field submits a form without any
button's value, and the two answers differ by whether a shelf moves; a
control that can silently fail to be sent is not where that question belongs.

Receiving is its own endpoint and refuses a second go the way booking does:
the order number guards the document, `received_on` guards the event.

**A short delivery is adjusted on the way in.** Ordered 80 and received 50
are both true, so the line keeps both: `quantity` is what the document
billed, `received_quantity` is what reached the shelf, and null means all of
it — the ordinary case, and why it is nullable rather than a copy written on
every line.

This paragraph used to say the opposite: mark it received and correct the
shelf on `private/raw-inventory/` afterwards, on the grounds that a
partial-receipt flow would be "a second way to write the same number". That
was wrong, and worth recording as wrong. It is the *same* door with the
right number in it — the absolute-count argument is about healing what went
unrecorded afterwards, not about refusing a figure somebody is standing
there holding. What the old rule actually built was a step that has to be
remembered to be correct, which `CLAUDE.md` says never to add: mark 80,
then remember to go to another page and set the shelf to 50. The second half
is the half that gets skipped, and the shelf is then wrong with nothing
saying so.

**The boxes stay out of the way until asked for**, because the whole order
usually turns up. *Some of it didn't turn up* is a plain link to `?adjust=1`
with `hx-get` layered on — the pattern `production_run_detail` already uses —
so a blocked script costs a page load rather than the feature. It does not
change what Save means; it changes the numbers Save is given. One unreadable
figure receives nothing, the bill form's bargain.

**Nothing tracks the refund or the outstanding 30.** That is settled with the
supplier by a person. What the app needs is a shelf that matches the room —
and the order stops holding the reorder signal down, so the 30 that never
came shows up as an ordinary shortage.

**What this now makes answerable, and is not built:** *what is on order*.
`SupplierInvoice.is_on_order` and the picker's own section are the whole of
it today. A reorder page that said "38 to order — 100 already coming since
14 Sep" is the obvious next use and a real one, since ordering against a
number that ignores what is already in transit is how a thing gets ordered
twice. It is not built because nothing has asked for it yet.

### Replacement cost, and nothing more elaborate

`RawProduct.price` means **what one would cost to buy again today**. An
invoice overwrites it flat. There is no weighted average, no FIFO layer, no
cost history to reconstruct: if it cost a dollar and costs two now, the
shelf is worth two and the dollar is not a number anything here reads.

The only trace of the change is on the line — `previous_price` beside
`unit_cost` — and that exists so the page can print *was $24.99* next to the
new figure. A cost you cannot check is the `par` failure again.

**The per-unit figure is derived, not typed.** Line total over quantity, to
the cent, because the pack is what the document actually states. A printed
unit price is only a fallback for when the total or the quantity can't be
read.

### The reading is advice, and is built to be argued with

The model gets the document and the catalogue of active blanks, and answers
one question per line: which of these is this, how many, at what each. That
is all. Then:

- every figure lands in a box, and **the invoice's own wording for the line
  is printed beside the match** — a suggestion whose evidence is hidden is
  not something anybody can agree or disagree with;
- a line it couldn't match comes through as *not matched* with a dropdown,
  never as a guess;
- **removing a row is unticking it**, not a delete button: a row that
  vanishes is indistinguishable from a click that never arrived, on a list
  somebody is checking against paper;
- five spare rows at the bottom take the lines it missed, so a bad reading
  is never the end of the job;
- a reading that fails completely — no key, a bad photo, an API having an
  afternoon — still opens the same form with the spare rows. **The worst
  case of the whole feature is the job as it was done before it existed.**

There is deliberately no confidence score and no second pass. A reading that
is 80% right and openly editable gets read; one that is 95% right and arrives
looking settled gets rubber-stamped.

### `invoice_description`: the one fact no reading can reach

`Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle` is an **Infinity**.
Nothing derives that. Not the name, not the fibre, not any amount of
cleverness — it is a fact somebody knows about their own catalogue, and the
only honest way to get it is to be told.

But it only has to be told **once**, and once told it is worth more than a
reading: an exact match is a certainty where a model is a suggestion. So
`RawProduct.invoice_description` holds the supplier's own wording, and
`invoiceread.known_match` applies it **before** the model's answer is
believed — where the two disagree, the remembered one wins.

**Nothing types it in**, which is the part worth keeping. It is written by
confirming an invoice line against a blank — the choice somebody was making
anyway — so the field is `editable=False` and the recording is a by-product
of a job already being done. The page says *remembered from a past invoice*
beside those rows, because a remembered match and a read one deserve
different amounts of checking and a row that doesn't say which gets neither.

Three rules hold it together:

- **Exact or nothing.** Normalised for case, runs of spaces and the two kinds
  of quote mark — the same order arriving once as a PDF and once as an email
  is the case that actually turns up — and nothing else. No fuzzy matching,
  no scoring, no nearest neighbour: *a wrong match here arrives looking
  unremarkable and gets confirmed*, which is worse than the dropdown it
  saved.
- **One wording belongs to one blank.** Confirming it against a blank takes
  it off whatever else held it, so a mis-match heals the next time somebody
  gets it right. Two blanks both claiming a line would make the suggestion
  depend on row order.
- **A blank description forgets nothing.** Clearing the box means "I have
  nothing to say about the wording", not "discard what you knew".

The wordings are also handed to the model with the catalogue. That is
redundant for the exact matches, which are applied afterwards regardless —
it is there for the *near* misses, where a supplier reworded the line by
one word and the alias no longer fires.

### A draft has moved nothing

An unconfirmed invoice is a draft (`booked_at` is null) with its rows parked
in the database rather than a session, so a refresh or a night's sleep
doesn't lose the corrections. It sets no cost and moves no stock, the picker
says so, and discarding one really does delete it — the `CLAUDE.md`
exception for a row with no history to protect.

Two smaller decisions worth not re-litigating. **Nothing is booked unless
every ticked line reads**, the bill form's refusal for the bill form's
reason. And **booking never touches `counted_at`**: a delivery note is a
claim about a change, a count is a measurement, and only one of them means
somebody looked at a shelf.

### Pasted text is a first-class door

Most of these orders are an email long before they are ever a file. Getting
a screenshot off a phone and into a file picker is more work than select-all
and copy, and text is the easiest of the three to read — no photograph, no
scan, no column that wrapped. Paste whatever came, headers and signature
included; it is kept on the invoice as the basis, the way a document is.

### Which model, and how to find out

`settings.CLAUDE_MODEL` (env `CLAUDE_MODEL`) picks it; the default is
`claude-opus-5`. This runs on a one-page document a couple of times a month,
so a year of it costs less than one mis-booked delivery, and reading a table
off a photograph and matching it to a catalogue is where a better model earns
its keep.

`manage.py read_invoice --model <id>` reads one document with a different one
without changing the setting, which is how to answer "is a cheaper one good
enough" off real paperwork rather than by arguing about it. It prints the
tokens every time and a dollar figure when it knows the rate — **with the
rate and the month it came from printed beside it**, because a hardcoded
price table goes stale silently and an unlabelled price is a number somebody
quotes back a year later.

Measured on one real 8-line Wool2Dye4 order: **Opus ≈ $0.045**, **Sonnet ≈
$0.022**, both reading it correctly. Haiku read a 3-line order correctly for
about a third of Sonnet's tokens.

**`effort` is learned rather than listed.** Haiku 4.5 rejects the parameter
where the 5-family takes it, and a hardcoded table of which model accepts
which parameter is wrong the week after it is written — so a model that
refuses costs one retry the first time and skips it thereafter. Before that,
picking a cheaper model silently turned the whole feature off: every read
came back as an error, and the page said only that something had gone wrong.

### Trying it without building a habit on it

`manage.py read_invoice <path>` reads a file — PDF, photo, or a `.txt` of a
pasted email — and prints what came back. It writes no row. That is the
bench for deciding whether the reading is worth confirming, which is a
question about real invoices rather than about code.

## Par on a blank is two numbers, and only one of them is stored

`private/raw-inventory/<category>/?plan=1` is the second mode of the page —
**Plan an order**: the shelf, the floor, what sold, what the rest of the
season is on track to sell, what that leaves to buy, and the links to buy it
from. A row lights up when the shelf is below par or short of the season
(`Outlook.needs_order`), so a forty-row table can be read for the rows that
matter. It writes nothing.

**It used to be the par-setting mode**, a box per blank writing `par_level`
beside the evidence for choosing it, and the boxes went on 22 September 2026
because the person taking over the ordering asked for them to go: this table
is for deciding what to buy, and a form under every figure was in the way of
that. The reason the boxes existed still holds — until they did the only door
was the Django admin, and **`par_level` was 100 on all 26 blanks**, a uniform
remnant reading off the page as though somebody had decided it — but the door
is `private/raw-inventory/blank/<id>/` now, which has a par field of its own,
and each row's figure links to it. One door rather than two. Par is a rare
decision; ordering is a weekly one.

Two columns went with the form. **Cost and *Reorder from* came off the bill
mode**: a delivery note is typed off the paper in the other hand and never
needs a link, and the links are on the tab where the order is placed.
**The *Entered, 8wk* column came off the plan mode**: it counted production
rows written in the last eight weeks, which measured typing rather than
dyeing, and nobody reading the page could say what it was for. The lesson it
carried — a retracted bath is not a bath, `reversals__isnull=True` — lives
with `labels.produced_since` and in `CLAUDE.md`.

**The two numbers.** Both get called par and they are not the same:

- The **floor** (`par_level`) is what stays on the shelf so the dye room never
  stops. A level you stay above, checked weekly; what `raw_shortage` and the
  category page's below-par count already mean.
- The **season requirement** is what the rest of the run will consume. A
  number you fill to once, not a level you hold. `scarves/rawdemand.py`
  derives it and **nothing stores it**: it moves every weekend the ledger
  grows, and a copy in a column would go stale silently and then be read as a
  decision.

They sit in separate columns and **are never added**. A floor plus a
requirement is a number that means nothing and would be ordered against.

**A blank box is untouched, not zero** — which differs from the recipe page,
where every product renders a box each visit so an empty one could only be a
slip. Here a category runs to forty blanks and a visit means to change one.
`0` is still a real answer, and it is how you say there is no par.

### What the forecast is, and the soft spot printed beside it

**On track for is a straight line: sales per faire day so far this season,
times the faire days left.** It replaced the share-of-season model
`seasonreport` draws its dashed tail with — what fraction of a complete prior
season the banked weekends took, applied to this season — which is the
better estimator and was unreadable here. Half the catalogue had no complete
prior season and printed a dash; the other half printed a number nobody
could check against anything. A rate times a day count is arithmetic the
person ordering can do in their head, and both factors print under the
figure (`2.0/day × 6 days left`). The cost is that early in the run one
weekend swings it, and a wet weekend pulls it down — said on the page,
because the person reading it knows which weekend it was.

The denominator is faire days rather than calendar days (two a week, three
on Labor Day weekend), and **only the days of weekends actually imported**:
a weekend nobody has loaded would otherwise divide the rate by days it has
no sales for. A weekend where *this* blank sold none still counts — that is
a measurement. The days to come are every traded day after today, whatever
has been imported. Before the first counted day the column is a dash, not a
zero; after the run it is 0, which is the honest answer to "how many more
this season" and the wrong question to ask in December. Next season's order
is a different calculation, and not built.

The reorder question for anything dyed is not "how many will we sell" but
"how many will we **dye**", and those differ by everything already on a peg.
So the *to order* column is `on track for − (raw on hand + claimed + finished
on hand)`, in baths and dollars as well as units.

**Claimed yarn is added back here and nowhere else.** A blank on an open sheet
has left `raw on hand` — see *The blanks come off when the run is made* in
`docs/claude/production.md` — and has not yet reached the finished side, so it
falls between the two and the season would ask for skeins that are already in
the building. The *floor* is the opposite question and deliberately reads the
claimed-out number: yarn spoken for cannot keep the dye room going next week.

**Dyed and undyed stock both offset the buy, and they are not equally good at
it.** Undyed is fungible — a skein becomes whatever sells — and dyed is not,
so netting a blank's finished count against a blank's forecast quietly assumes
every colorway is as good as every other. The unsold-colorway figure is
printed for that reason and **never deducted**: the reading is unreliable in a
known direction, because a colorway shows as unsold both when it did not sell
and when the line carried no colorway at all, which is most of a Sash Belt's
season. Deducting on that basis orders yarn against a gap in the data.

**The forecast is in units, never dollars.** `seasonreport` projects money
because that is what a season is judged on; yarn is bought by the skein. The
two came apart for real in 2025, when a mid-season reprice lifted takings
while silk lost about a fifth of its unit velocity — a dollar-shaped forecast
would have ordered against the price change.

**A blank with no complete prior season gets a dash, not a zero.** "Nothing
more will sell" and "nothing here can say" are different answers and only one
is a reason not to order. The count of prior seasons prints with the figure,
because yarn has one behind it and silk has five.

### `counted_at`: raw is the one pile nothing recounts on its own

The finished side heals. A restock walk and the Sunday close both put an
absolute count against what the app believed, and the *Self-healing* section
of `CLAUDE.md` is about exactly that. **Undyed stock has no equivalent.** It
falls when a run is planned, so dyeing that went through a sheet is already
off the count — but a bath nobody planned through one is not, and a week of
that leaves the count reading high. Ordering against it then under-buys
precisely when the dye room has been busiest. That is not a hypothetical:
entry runs in bursts, days or weeks behind the work.

So `RawProduct.counted_at` records when the shelf was last actually looked at,
and the par page prints it on the row. **Only an absolute count sets it** —
the page already draws that line, a count is a measurement and a delivery note
is a claim, and this is the same distinction with a date on it. **A count that
agrees still counts**: what was learned is that somebody looked today, and
dating the count to whenever the number last happened to *move* would put a
steady blank's count in another season.

**What the row prints is the date, not a verdict**, and the difference was
learned the hard way. `count_is_stale` used to badge a shelf whenever a bath
had been *entered* since it was counted. That was a real question while raw
stock fell at reporting time — an entry then meant skeins had left the shelf
after somebody counted it. Claiming a run's blanks at creation ended it: the
skeins leave the count when the sheet is made, so an entry written afterwards
says nothing about the count, and `number_on_hand` already has the claim taken
off it.

The flag then fired on the ordinary healthy path — plan a sheet, count the
shelf, report the baths a few days later — which meant it fired on the best
data in the system. Four yarn blanks were counted by hand on 13 September 2026,
the most accurate that shelf has ever been, and read `stale` two days later
with nothing wrong with any of them.

The thing it was reaching for is baths dyed without a sheet, and that is
exactly what nothing here can see. A flag that cannot see its subject should
not draw a warning, because a warning that is always on is one nobody reads —
and the cost is the real signal beside it. So `never_counted` says the one
thing the data supports, the row prints when somebody last looked, and the
judgement stays with the person placing the order.

The column that looks like a dyeing rate is labelled **entered** for the same
reason. These rows are written when a session is typed up, not when the dye
was mixed, so a quiet fortnight there is a fortnight nobody entered — which is
never a reason to order less.

**The order must not wait on the backlog.** The buying decision needs one
fresh number and it is cheap to get: count the shelf, type it in the Counted
column, then read the season figure. Four yarn blanks is a five-minute job,
and it is deliberately not the same job as catching up on production entry.
A flow that only works once somebody is caught up is a flow that fails
silently, which is the rule the whole *Self-healing* section is built on.

## Undyed stock: one pile, two rows, and the axes swapped

A few yarns are sold exactly as they arrive — no dye step, straight from
supplier to customer. They break two assumptions, and both breaks are worth
understanding before touching them.

**`FinishedProduct.recipe` is null for these, and null is the marker.** Not a
sentinel "Undyed" recipe row. Every dyed-only query in the app joins through
that FK, so a null row drops out of production planning, the rainbow sheets,
the colour pages and the games *by construction*. A sentinel would need each
of those to remember to exclude it, and a forgotten exclusion is silent — an
undyed skein filed under a colour it doesn't have. A forgotten null check, by
contrast, raises. Loud beats silent.

Three queries don't join through recipe and so needed explicit exclusions:
`production.candidates()`, `production_needed_view` and `card_backfill_index`.
Without the first two the sheet prints `4 × ` with no colorway and sends
somebody to the dye room to make something that arrives in a box; without the
third you are offered a kanban card to backfill for a dye bath that never
happened.

### One pile, and only one row may count it

This is the part that bites. For anything dyed, the raw blank and the finished
item are **two** piles, and the dye bath is the event that moves one to the
other. For a passthrough they are the same physical skein. Two
independently-maintained counts for one pile drift, silently, and in the
direction that matters most — knowing when to reorder is the entire reason
this stock is tracked.

So the **raw row holds the count** and the finished row mirrors it:

- `mirror_passthrough_stock` (a `post_save` on `RawProduct`) is the mirror's
  only writer. A signal rather than calls at each site, because the raw count
  moves from several places and forgetting one gives a number that looks fine
  and isn't.
- `FinishedProduct.save()` re-derives it too, covering the row being
  *created*, when there was no passthrough for the signal to find.
- `ledger.move()` and `ledger.count()` write to whichever row actually
  holds it, and every sale, bath and stock take goes through one of them.
  Writing the finished count directly means `save()` re-derives it, the
  number snaps back, and the count looks like it never happened — which is
  why there is one door (see *Finished stock moves through `ledger`* in
  `CLAUDE.md`). `FinishedProduct.set_on_hand()` is the same rule without
  the ledger row, kept for shells and fixtures.

Everything downstream keeps reading `FinishedProduct.number_on_hand` and gets
the right answer without knowing a passthrough exists — the Square inventory
push, the "everything on hand" label run, any report.

Shortfall shows up on `private/raw-inventory/`, already the reorder workflow
and where it belongs: **you order these, you don't make them.**

### Making them: `create_passthrough_products`

Creating one is two rows — a `RawProduct` for the pile and a
`FinishedProduct` for the thing Square sells — and the second is mechanical,
so `create_passthrough_products --group "Undyed Yarn"` does the batch. Same
name, no recipe, price off the raw product, `par=0` because the par that
matters for these lives on `raw_product.par_level`. It skips raw products
that already have a passthrough, so running it twice creates nothing new, and
`--dry-run` shows the batch first.

**Pricing, and why the fallback is $1.00.** There is an older helper,
`_default_finished_name`'s neighbour `_default_price_for_raw`, which falls
back to cost × 3. That's the wrong shape here twice over: a plausible price
can reach a customer without anyone looking at it, and it bottoms out at
**zero** whenever a blank has no cost recorded. Zero is the dangerous kind of
wrong — valid, syncs, and rings up free at the till with a queue behind it.
A pound is obviously wrong and gets fixed.

**Null and zero are different, and the schema already says so.**
`suggested_price` is nullable: null means nobody set a price, zero means
somebody set it to zero, and a giveaway is a real product. So only a *missing*
price is replaced. A deliberate zero is taken at its word and reported,
because free is the one price nobody notices until it has been charged.

### Category is Yarn, not a category of its own

Category means "which table at the stall" — that is why reference sheets print
per category — and undyed skeins sit on the yarn table. Forking the category
would also break the day an undyed *silk* appears. The distinction actually
needed is "this can't be dyed", which keys on the null recipe, not on where it
sits in the shop.

### `CatalogGroup`: the item is "Undyed Yarn", the variations are the blanks

Everywhere else the Square ITEM is the blank and each VARIATION is a colorway.
Undyed stock inverts it: there is no colorway, and the thing a customer picks
between is the yarn. Same two axes, swapped.

`CatalogGroup` names that shared item and `RawProduct.catalog_group` points at
it. Blank means "I am my own item", which is every scarf blank and stays the
default, so nothing about the dyed path changed.
`FinishedProduct.variation_name` is the colorway when there is one and the
blank's name when there isn't.

Two places must read the group rather than the raw product's own
`square_item_id`: building new variations, and `--update` (`_item_id_for`).
Reading the raw product there sends a blank item id, which moves the variation
to nowhere. Losing a group's id is the expensive one — the next run creates a
second "Undyed Yarn" and splits the shelf across two items — so `_record_ids`
writes it before anything else.

SKUs keep the `BLANK-DYEBATH` shape with `UNDYED` as the second half, because
`private/unidentified-sales/` reads the first six characters as the blank and
a SKU with no dash would narrow to nothing.

Worth knowing where these came from: last season they were rung up as a
hand-keyed price, which carries no `catalog_object_id` at all — so every one
landed in `private/unidentified-sales/` (or, before that existed, vanished).
Selling them as real variations fixes that at the source.

### Two `Undyed Yarn` items in Square, and why that is not a split to merge

Square holds two items called `Undyed Yarn`, which reads like the split-shelf
failure `CatalogGroup` exists to prevent. It is not one — it is a
**succession**, and the difference decides what to do about it:

| | `OP74Y24ZJYEJIBCDYWYRTE6H` | `5P34LJXSW6TTZCVYXNJX7MOF` |
|---|---|---|
| state | live, all locations | **archived already** |
| variations | 11, real prices, SKUs | 10, all $0.00, no SKUs |
| sale lines | 50, the 2026 season | 122, the 2025 season, ending 19 Oct |

The archived one is last season's, retired when the yarns were re-created as
real variations with SKUs. **Do not merge them.** Square has no merge, and
doing it by hand means an ITEM upsert — which replaces the variation list
outright, so the 2025 variations would be deleted and 122 sale lines would
lose the only durable handle they have on what was sold. It would also put
ten $0.00 variations under a live item, and free is the one price nobody
notices until it has been charged.

Nothing needs archiving either; it already is, so it cannot be rung up. What
was actually wrong was **this app's own tooling listing it as untracked**,
which is what invited the merge — so `passthroughs.untracked()` skips
archived items and says how many it passed over rather than dropping them
silently.

The reporting half is real, and it is fixed by linking rather than merging:
`relink_sale_lines --item "Undyed Yarn"` attaches the 2025 lines to the
passthrough products by price point. One alias is needed, because Square
spells it `Loop de Loop Caramel` where this app spells it `Lop de Loop
caramel` — which is exactly why that command refuses to fuzzy-match. The
near-miss is between two real yarns, and a guess would file one's revenue
under the other.

### Notions: the other passthrough, and the marker that is *not* the fancy one

The bits and bobs — yarn bowls, spindles, needle cases, stitch markers — are
ordered and resold with no processing at all. They are passthroughs like the
undyed yarns, arrived at from the other direction: an undyed yarn is a blank
that skipped the dye bath, a notion never had one to skip.

**The null `recipe` is the only marker one needs.** Do **not** reach for
`RawProduct.made_in_a_dye_bath = False`, however exactly it seems to describe
"no dye bath makes this". That is the *fancy veil* marker, and
`fancy.fancy_blanks()` filters on precisely it — so setting it here puts a
yarn bowl on `private/fancy/` as a thing a silk scarf can be converted into.
The two flags answer different questions and this one already has its answer:
a null recipe drops the row out of every dyed-only query by construction.
`test_a_notion_is_never_marked_as_the_fancy_veils_are` is the pin.

**The row is worth little; the Square ids are the point.** `square_webhook`
matches a line's `catalog_object_id` against
`FinishedProduct.square_variation_id`, so a tracked notion stops arriving in
`private/unidentified-sales/` at all — the queue drains by the thing that
filled it becoming identifiable, rather than by anyone working it. Carrying
the parent `square_item_id` is what stops the next `sync_to_square` deciding
the item is new and creating a second one beside it.

So `scarves/passthroughs.py` **never creates anything in Square**. It writes
down what Square already has.

**Two triggers, one module**, the usual split. A *Track* control on each queue
row makes one from the sale in front of you, taking the price from what the
customer was actually charged divided by the line quantity. And
`import_square_passthroughs` does a batch.

**The batch never decides what is a product**, and this is the load-bearing
refusal. Run over everything the app does not recognise, it would create
`Women's Haircut`, `Shampoo Style`, `Shipping` and `$2 Refund` — all real
items on this till — alongside the discontinued wax line and nineteen legacy
silk blanks. So the population is a Square *category* somebody curated:
assign the notions to `Notions` in the dashboard and the command imports
exactly those. `--list` prints what is untracked, grouped by Square's own
category, which is how you see what still needs assigning. The app follows a
decision rather than making one — `colorbands` again, with the form filled in
by the dashboard.

**A multi-variation item becomes a `CatalogGroup` and one raw product per
variation**, never one raw product carrying several passthrough rows.
`mirror_passthrough_stock` keeps a passthrough's finished count in step with
its raw one, and two finished rows on a single raw product would mirror the
same pile and disagree about it silently. Same shape the undyed yarns already
use, reached by the same argument.

A new notion starts at `par = 0` and `display_slots = 0` — it is ordered
rather than made, and it does not hang on a peg, so the close leaves it alone
and the shortfall shows up on `private/raw-inventory/` where a bought-in thing
belongs. Cost starts at zero because the sale cannot say what it cost, and
zero is honest *there* in a way it never is on the selling side: a cost of
nothing overstates margin on a report nobody reads yet, where a price of
nothing rings up free at the till with a queue behind it.

**Resolving a passthrough sale must go through the ledger.** Writing
`number_on_hand` directly is correct for anything dyed and wrong here — the
finished row is a mirror, `save()` re-derives it, the number snaps back, and
the sale reads as though it never happened. `resolve_unmatched_sale` did
exactly that, harmlessly, right up until this feature made passthroughs
reachable from it.

#### `Notions` is a category of its own, and yarn was not

This reverses the call made one section up for the undyed yarns, so it is
worth saying why the two differ rather than leaving the inconsistency to be
discovered and tidied away.

Category means **which table at the stall**, which is what makes an undyed
skein belong under `Yarn`: it sits on the yarn table beside the dyed ones,
and the distinction actually needed there — *this can't be dyed* — already
keys on the null recipe. A yarn bowl is not on the yarn table in the sense
that matters here. It has a different supplier, a different reorder rhythm,
and no relationship at all to a colorway, so the one thing the category
buys is the one thing it should buy: `private/raw-inventory/Notions` is a
shelf you can read without forty skeins in front of it.

Nothing in the schema changed for it. `RawProductCategory` was always
expandable without code — the row carries the Square category id, and
`import_square_passthroughs --category Notions` fills the table from
whatever the dashboard has been assigned to it.

**Leave `dye_book_bath_units` null.** It already is by default, and the
model's own help text has the argument: setting a basis claims the dye
book's yarn figures apply to that fibre, which is the one way to get a
printed amount wrong quietly. There is no fibre here at all.

**The reorder page had two sentences written for a blank that goes through a
bath**, and a table where nothing is dyed is what surfaced them. Both are
chosen off the rows rather than off the category's name, so a table that
grows its first dyed blank starts reading the other way with nothing to
change:

- the shortfall sub-line printed `0 baths · $684` beside a yarn bowl.
  `Outlook.baths` correctly returns 0 for a passthrough; *printing* it is
  the mistake — zero of a thing that does not happen, on the page whose
  whole job is to say what to buy. It reads `38 to order · $684` now, which
  is the shortfall in the unit it is actually ordered in.
- the par lead said the floor is "what stays on the shelf so the dye room
  never stops". No par on this table has ever kept a dye room going, and
  that sentence is the one somebody reads to decide what number to type.

**A notion's sales history is reachable and worth linking.** These sold for
years before the app had a row for them, so `relink_sale_lines --by-item`
attaches the old lines to the new blanks by item name — 226 of them across
eleven notions on the first pass. Without it *Sold this season* prints a
zero, and a zero on that page is a reason not to order.

## Fancy veils: countable, not plannable

A fancy veil is an already-dyed scarf with extra line work added. It costs
more, and more to the point it is often what makes the sale. Two things about
it break assumptions the rest of the app is built on.

**It has a colorway and still cannot be dyed into existence.** Undyed
passthroughs drop off every production query by construction — a null recipe
fails the dyed-only test each of them makes — but a fancy veil *is* a
colorway, so it sails straight through. Hence
`RawProduct.made_in_a_dye_bath`, set False on the blank, checked in
`production.candidates()`, `production_needed_view` and
`card_backfill_index`. Sending somebody to the dye room for one is asking for
a thing that isn't made there.

Not a category. Category means "which table at the stall" — that is why
reference sheets print per category — and forking it to express "can't be
dyed" breaks the day a fancy *shawl* exists, silently, on the dye list. Same
argument the undyed yarns already settled.

**There is no par, because supply is opportunistic**: they get made when
there is time and inclination, and you take what you can get. Par means
"produce until you reach this", so any number here would be a plan nobody is
going to follow, and a production list built on it would be fiction. `par = 0`
is the honest value and the app already reads it as "no par set".

**What replaces it is a display, not a plan.** This is the distinction worth
holding onto, because getting it wrong is what cost the project a hundred
units: fancy veils resisted *production planning* correctly, and that
resistance then generalised into not tracking them at all. They are perfectly
countable. They hang on a board, they get walked, and the Sunday close asks
about them — which works precisely because `expected_products()` gates on
`display_slots > 0` rather than on par.

**It asks about the ones that exist, and that qualifier was learned the hard
way.** 204 fancy, triangle-fringe and infinity colorways went in at par zero
with `display_slots` copied from their plain counterparts, on the reasoning
above — and since not one had ever been made, every one of them landed on
every close at zero and stayed there. The close was dominated by rows nobody
could answer, which is worse than not asking: the rows that do carry a number
get read past on every pass. So `expected_products()` now leaves out the pair
`par == 0` **and** nothing on hand, and the first fancy veil that physically
exists is on the next close with nothing to remember. The peg is the other
door and always was — `restock.board()` walks pegs, not beliefs. The argument
is in `docs/claude/close.md`.

So the supply stays unplannable and the **demand becomes answerable**: sales
land in `InventoryLog` like everything else, and at the end of a season "what
did fancy sell" is a query.

### Fancy at production: routed before it was ever plain

**A bath's output can be finished as fancy at the moment it is made.** The
crew's reporting page offers a second number on any row whose blank has a
fancy counterpart: five came out of a bath of five, and one of them went out
fancy. The plain product gets four, the fancy one gets one, and the fancy
veil is never counted as plain at all.

This is the better-evidenced of the two routes, and nearly free. The
conversion page below is retrospective — it has to be noticed, and until it
is, the plain side is overstated. Here the person who made the bath says so
while holding it.

**`RawProduct.fancy_counterpart` is one to one**, set on the plain blank: a
half circle veil becomes a fancy half circle veil and nothing else, because a
veil cannot become a fancy shawl. That is what lets production route without
asking which blank — there is only one answer, so there is no question, which
is the difference from `fancy.target_for` on the conversion page.

**It is silk-only with nothing checking for silk.** No yarn blank has a
counterpart, so no yarn row ever shows the box. A category test would work
today and break the day a fancy shawl exists — the same argument that put
`made_in_a_dye_bath` on the blank instead of forking the category.

**`fancy_yield` is a subset of `yielded`, never an addition.** That keeps
`loss` meaning what it says: a bath is short only if fewer came out than were
asked for, and how they were finished is a different question from whether
they survived. The fancy units are logged as `PRODUCTION`, not as a
conversion, because nothing was converted — that scarf was never plain. The
plain log is still written even at zero, since `applied_log` is what stops a
bath being counted twice.

**Raw is untouched by the split.** A fancy veil is a plain scarf with line
work, so a bath of five eats five plain blanks whichever way they leave.

**Fancy supply now has two sources and a query must read both** —
`ProductionRunRow.fancy_yield` and the conversion rows. The sentence below
that the conversion rows *are* the fancy production history was true when
they were the only route; it isn't any more, and a report reading only
`SOURCE_FANCY_CONVERSION` goes silently short.

The paper does not carry this yet. `sheetscan` reads one tick box per row and
cannot read a number, so fancy is typed on the phone — consistent with the
photo prefilling ticks and never accepting. Whether the reporting sheet grows
a fancy write-in column is open, and depends on the separate question of
whether the scan path earns its keep now that a row carries more than a bit.

### And the supply turns out to be answerable too — retroactively

`private/fancy/` records a conversion: one colorway goes down on its plain
blank, its fancy counterpart goes up, two `InventoryLog` rows tagged
`fancy_conversion` carrying the same sentence from opposite sides.

That is the **one part of fancy worth systematising** — not the planning, the
event. And it pays off twice, because every fancy veil that exists came from
somewhere: **the conversion rows are the fancy production history.** You still
cannot forecast how many you will get, but "how many did we fancy this season,
and in which colorways" stops being a guess. `source` doing its job again.

**The page is optional and must stay optional.** An unrecorded conversion
still heals — plain side overcounts at its peg, fancy side undercounts at
its — which is exactly what makes it safe to offer rather than demand. A
backstop is not a reason to make recording hard, and a required form is the
thing that produces no form.

**Converting more than the app believed is allowed, and reported.** Five
really did get line work put on them; the plain count was wrong before anybody
touched it. So the plain side floors at zero, the fancy side gets all five,
and the discrepancy comes back as a warning — refusing would protect a wrong
number and destroy the only evidence about it.

**Designs are not a dimension.** A few patterns times a few accent colours
times forty colorways is not a catalogue anybody can enumerate, and it is the
fiber-field failure exactly. The axis stays blank × colorway; which pattern is
stitched on a given scarf is a property of that object. If designs must differ
in price, add a **tier** as another blank (Extra Fancy Veil) — finite, and
chosen. `FinishedProduct.is_fancy` was dropped in migration 0029 for this
reason: a boolean cannot carry a price, and it was one well-meaning afternoon
away from becoming the design dimension.

## `private/stock-value/`: the shelves as a balance, and the three piles

The app could say how many of a thing were on hand and never what any of it
was worth, so the only number a year of dyeing had attached to it was the
supplier invoices going out. Stock is the other side of that ledger, and over
twenty years nearly all of it has left the shelf again as takings — which is
the reading the page exists for, and why it leads with a dollar figure rather
than a table.

**Three sections, disjoint by construction, so the totals add up.** Undyed
blanks waiting for a bath, dyed colorways pooled by blank, and the
passthroughs. The third is not a nicety: a passthrough is **one physical pile
with two rows**, and valuing the raw row and its mirroring finished row would
double it. So the pile is valued on the finished row, which is the one
carrying a selling price, and those blanks are held out of the undyed
section — which leaves that section meaning *yarn waiting for a dye bath*,
the only thing a bath count means anything for anyway.

**Claimed yarn is counted here, and this is the only page that adds it back to
a shelf reading.** `number_on_hand` means unclaimed — see *The blanks come off
when the run is made* in `docs/claude/production.md` — and that is right for
ordering and wrong for a valuation: skeins on an open sheet are in the
building and nothing has been spent. The claim prints on the row, so the
count reconciles with what somebody standing at the shelf would find.

**Retail is the asking price and the page says so.** Some of it goes at a
discount, seasons late, and a write-down is an ordinary thing rather than a
failure — but cost alone understates a shelf whose entire value add is the
dyeing, and none of the dye or the labour is in the cost column. Both are
printed for that reason, with the difference labelled as what it is: before
dye, labour and the stall, not profit.

**Undyed yarn has no retail price of its own**, so the figure on those rows is
what the colour sells for once dyed: `suggested_price` when somebody set one,
otherwise the stock-weighted average of what that blank's colorways actually
ring at, and **the row says which**. A derived number with no visible basis is
the `par` failure again — see *The app advises, a person decides* in
`CLAUDE.md`. A blank with neither gets a dash and its units are reported
under the table: nothing is priced and it is worth nothing are different
answers, and only one of them belongs in a total.

**Nothing plans against this page, and the bath column is the thing to watch.**
`units / number_per_dye_bath` is here because "126 baths of undyed" is a size
a person can hold where 565 skeins is not. It is not a work order: dyeing is
planned off par and off the Sunday close, and a bath count derived from what
is sitting on a shelf is capacity proposing production, which is the coupling
*Display capacity is not demand* exists to keep broken. A blank no bath can
produce — a fancy blank — gets a dash rather than a zero in that column, for
the same reason it drops off every production list.
