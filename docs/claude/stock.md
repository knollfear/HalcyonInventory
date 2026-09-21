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

## Par on a blank is two numbers, and only one of them is stored

`private/raw-inventory/<category>/?par=1` is the second mode of the page — a
box per blank that writes `par_level`, beside the evidence for choosing what
to type. Until it existed the only door was the Django admin, and the number
showed it: **`par_level` was 100 on all 26 blanks**, a uniform remnant reading
off the page as though somebody had decided it. Exactly what
`FinishedProduct.par` was before it got `?par=1` on the recipe page, and the
same fix, for the same reason — see *The app advises, a person decides* in
`CLAUDE.md`.

The mode is a mode for the structural reason the recipe page's is: par boxes
and delivery boxes in one table means a par typed in and then abandoned by
pressing **Save this bill**, written nowhere and said nothing about. Separate
endpoints, and both directions are pinned — posting `par_` to the bill form
does nothing, and posting `received_` to the par form does nothing.

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

### What the forecast is, and the two soft spots printed beside it

The reorder question for anything dyed is not "how many will we sell" but
"how many will we **dye**", and those differ by everything already on a peg.
So the season column is `forecast − (raw on hand + claimed + finished on
hand)`, in baths and dollars as well as units.

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
- `FinishedProduct.set_on_hand()` writes a counted quantity to whichever row
  actually holds it. Use it for stock takes — writing the finished count
  directly means `save()` re-derives it, the number snaps back, and the count
  looks like it never happened.
- The Square webhook decrements the **raw** for a passthrough.

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

**Resolving a passthrough sale must go through `set_on_hand()`.** Writing
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
