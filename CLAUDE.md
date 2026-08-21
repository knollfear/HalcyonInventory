# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Running & testing

This project runs in Docker, not locally (no psycopg / Postgres on the host).
Use the `web` service for one-off commands. The repo dir is mounted into the
container, so edits are picked up live.

- Django check: `docker compose run --rm --no-deps web python manage.py check`
- `--no-deps` skips the `db` service (host port 5432 is often already taken by
  another project's Postgres). Commands that don't hit the DB (`check`, importing
  modules, rendering views via `RequestFactory`) work fine without it.
- Don't try to run `manage.py` on the host — settings require PG env vars and
  psycopg, neither of which is available locally.

## URL layout: `private/`, `public/`, `secret/`, `webhooks/`

The first path segment under `/scarves/` says who a route is for, so exposure
is readable straight off the URL:

| Prefix      | Means                                | Example                            |
|-------------|--------------------------------------|------------------------------------|
| `private/`  | staff — every view `@login_required` | `/scarves/private/raw-inventory/`  |
| `public/`   | no login, and advertised             | `/scarves/public/games/match/`     |
| `secret/`   | no login, but unlisted               | `/scarves/secret/hours/`           |
| `webhooks/` | machine-to-machine, unauthenticated  | `/scarves/webhooks/square`         |

**Every new route goes in one of the four.** `URLBucketTests` fails on a route
that doesn't, and — more importantly — checks the prefix against what the view
actually does: anything `@page_meta` under `private/` must redirect an anonymous
GET, and anything under `public/` or `secret/` must serve one. That check is
what caught `bulk_recipe_matrix_entry` accepting anonymous POSTs that created
recipes.

### `secret/` — no login, no advertising

`secret/` is for a page real people use without an account, that customers
should never trip over: the hours form is the worked example. Concretely it
means **listed on the staff site map, filtered off the public one**, so the
person handing the URL out can always find it and a visitor browsing the shop
never sees it.

The obvious mistake is reading `secret/` as a security boundary. It isn't —
anyone with the URL is in, and the URL will end up in browser histories, on a
card at the stall, in a text message. Whatever actually guards the page has to
live *in the page* (the hours form uses a per-employee PIN). `secret/` only
promises that the app doesn't publish the link.

Because it isn't gated, `URLBucketTests` asserts a `secret/` route serves an
anonymous GET — a login redirect here locks out exactly the people it was
built for, and the only symptom is silence. The same test treats `secret/` as
private when checking the public map: its titles must not appear there.

Two deliberate exceptions to "unauthenticated ⇒ `public/` or `secret/`":

- `webhooks/square` stays put. Its URL is registered in the Square dashboard,
  so moving it here without changing it there drops sale events silently.
- The reference sheets are under `public/` on purpose — photos, names and
  barcodes, the same things printed and laid on the stall table.

`/scarves/` itself redirects to the site map at `/scarves/private/`.

### There is no 404

`mysite/urls.py` ends in a catch-all that redirects any unmatched URL to
`/scarves/public/`, the de facto home page. `/` goes there too. `static/` and
`media/` are excluded, so a missing asset still 404s as an asset.

**This changes how a mistake looks.** A typo in a `{% url %}` tag, a renamed
route, a stale link — none of them error any more. They quietly land on the
public map, which reads as a working page. If a link "goes to the home page
for no reason", suspect a bad route before anything else. `UnknownRouteTests`
pins that real routes still resolve, since the failure mode of getting this
wrong is every page silently becoming the home page.

Route **names** are the stable interface. Everything reverses by name, so
moving a path is a one-line edit in `urls.py` — don't hardcode paths in
templates or tests (`reverse()` them, including in regexes).

## Site map (`/scarves/private/`)

`scarves/views.py` has a `@page_meta(...)` decorator and a dynamic `index` view
that builds a self-documenting site map by introspecting the URLconf. The map is
generated at request time — nothing is hardcoded.

There are **two** maps, both built by the shared `_site_map()` helper:

- `/scarves/private/` — the staff directory. Lists everything, and badges each
  card `public`, `private` or `secret` from the route's own first path segment,
  so a card can't claim an exposure the URL contradicts.
- `/scarves/public/` — the same directory filtered to `public/`, and public
  itself. **Filtering happens in the view, not the template**, so a staff page
  never reaches that template to be hidden by it. `URLBucketTests` checks every
  private page's title is absent from the public map, derived from the private
  map rather than a hardcoded list.

A map never lists itself (`show_in_index=False` on both); the staff map links to
the public one from its header instead.

**Convention: every new GET-able page view must be decorated with `@page_meta`.**
Apply it as the outermost decorator (above `@login_required`) so the metadata
lands on the final callback:

```python
@page_meta(
    title="Human Title",
    description="One or two sentences on what the page shows/does.",
    category="Production",   # groups cards on the site map
    note="Requires ?foo=bar",  # optional caveat (params, query string, etc.)
)
@login_required
def my_view(request):
    ...
```

Do **not** add `@page_meta` to:
- POST-only / action endpoints (e.g. `record_dye_bath`, `adjust_raw_stock`)
- HTMX fragment endpoints
- webhooks (e.g. `square_webhook`)

The site map is for pages a user can GET and see. Omitting the decorator keeps
those endpoints off the map automatically (no metadata = skipped). Use
`show_in_index=False` only when a GET page should exist but stay hidden.

### Rule: every `foo/<int:some_id>/` page needs a `foo/` picker

If you add a GET-able page at `foo/<int:some_id>/`, **always** add `foo/` as a
page that lists the choices. Otherwise the only way in is to already know an id,
and the site map is left with a card nobody can click. Decorate the picker with
`@page_meta` and hide the parameterised view with `show_in_index=False`:

```python
@page_meta(title="Raw Inventory", description="Pick a category…", category="Inventory")
@login_required
def raw_inventory_index(request):        # listed on the map
    ...

@page_meta(title="Raw Inventory (by category)", ..., show_in_index=False)
@login_required
def raw_inventory_view(request, category_id):   # reached from the picker
    ...
```

`raw_inventory_index` and `reference_sheet_index` are the two worked examples.
The map should have **zero** unclickable cards.

This is enforced, not just documented — `PickerPageConventionTests` walks the
URLconf and fails if any `@page_meta` view takes URL params without a picker at
its parent path. `SiteMapTests` separately asserts no card is unclickable. The
rule only applies to pages: POST-only actions (`record_dye_bath`,
`adjust_raw_stock`) and HTMX fragments take params freely, because they carry no
`@page_meta` and were never listed.

**Watch the decorator order when adding helpers near a view.** Defining a
function between `@page_meta`/`@login_required` and the `def` they belong to
silently moves the decorators onto the helper — the page still renders, but
unauthenticated and missing from the map. `SiteMapTests` guards this.

## Rainbow bands: never print an unconfirmed guess

`Recipe.color_bands` says which sections of the rainbow reference sheet a
colorway prints in. A recipe claims one or more and prints in every one it
claims — a red-and-orange scarf appears under both, on purpose. That follows
from the dyes not being blended (same principle as `colorutils`): averaging a
recipe to one band files it under a color that appears nowhere on the cloth.

`scarves/colorbands.py` classifies a color using **three axes**: hue picks the
band, saturation asks whether it's a color at all, lightness catches the tints
and shades nobody names by hue. Hue alone is confidently wrong a lot — it calls
`#000000` red and `#000001` blue, `Slate` blue and `Ivory` orange.

**Nothing in `colorbands` ever writes to the database.** It fills the form in;
a person decides. `bands_confirmed_at` is null until someone confirms on
`/scarves/private/colors/`, and the sheet must skip those rows. The reason is
the failure mode: a wrong band is *silent*. You look in the orange section, the
scarf isn't there, and nothing tells you it was filed under red — so an
unreviewed guess is worse than no entry. Roughly 85–90% of swatch hexes and 4
in 5 photo dominants come out right, which is nowhere near good enough to print
unread.

Two judgements are baked in and are not bugs:

- **No indigo.** The blues in stock run 219–248°, the violets 254–277°. Indigo
  has no territory between them, so the section would be empty or arbitrary.
  Pink and brown are sections instead, because both are what someone actually
  says out loud about a scarf.
- **Neutral only claims a recipe when it is the *only* band.** Black, grey and
  cream are working dyes that ground the colors beside them. Every neutral-ish
  recipe in stock reads as something else too (`turq-mid-black`,
  `grey-forest-navy`), and nobody looks for those under grey.

### One category, two orderings

A category builds two sheets off **one picker** — `public/reference-sheet/` —
because Yarn and Silk is how the stall is laid out, and a sheet is printed per
table. The card carries a button each, not a second directory:

- `<id>/` — **by colorway**. A page per colorway, carrying the barcodes of
  every style dyed in it. Answers "what does this colorway look like, and what
  can I scan it as?".
- `<id>/by-color/` — **by colour**. The same pages, ordered by the rainbow and
  repeated once in *every* band the colorway claims. Answers the question a
  customer actually asks: "what have you got in red?".

The category comes ahead of the ordering in the path on purpose: it puts the
by-colour route under the existing picker, which is what
`PickerPageConventionTests` checks and what keeps the pair one card on the site
map instead of two.

Duplication is the feature, not waste. A red-and-blue scarf that printed once
would be *missing* from one of the two sections it is genuinely in, and the
absence is silent (see above). The cost is bounded by how many bands a recipe
claims — the picker prints the page count of each sheet, so the ratio is
visible before you build the PDF.

The picker also **says how many colorways the colour sheet will leave out**,
rather than quietly printing a shorter sheet. Unclassified means printed in no
section at all, and the only symptom on paper is a scarf nobody can find.

**Each page carries its band twice**: named under the title, and as a colored
tab in a fixed slot down the right edge. The tab is what makes a printed stack
usable — fan it and the sections show from the edge, which is how someone at
the stall finds red without reading. Slots are keyed to the band, not to the
page, so a gap means "this category has nothing in green" rather than "the tabs
shifted". Tab text flips black or white by luminance, because the sheet gets
photocopied.

Printed tabs replaced buying physical index tabs. They cost nothing (the sheets
already print in colour), and they can't be lost, misapplied, or forgotten by
whoever reprints the sheet — the same reasoning that puts `NEXT RUN: START AT
n` on the label sheet instead of in `localStorage`.

## Inventory log dates: print `log.when`, never `log.created_at`

`InventoryLog.created_at` is always a full timestamp, but it is not always
*known* to that precision. `date_precision` says how much of it is real:

| Precision | Source                                  | Shown as              |
|-----------|-----------------------------------------|-----------------------|
| `exact`   | recorded in the app                     | `01 Aug 2026, 21:36`  |
| `day`     | back-dated entry, day known             | `15 Sep 2024`         |
| `month`   | old kanban card reading e.g. `9/2024`   | `Sep 2024`            |

A `month` row is stored on the 1st **so that it sorts** — that day is padding,
not a record. Rendering `{{ log.created_at }}` would show a date nobody ever
wrote down. Always use `{{ log.when }}`, which says no more than is known.

The same rule governs input: `parse_card_date()` refuses anything it can't
read rather than guessing, and never promotes a month to a day.

**Back-dated entries never move stock.** The recipe page's production form and
the whole card-backfill flow write log rows only when the date is in the past —
that yarn was counted or sold long ago, and adding it to `number_on_hand` would
inflate current inventory by however far back the records go.

## Production sheets: paper to the dye room, one scan back

`private/production-sheet/` prints a dye-room worksheet — the next N baths to
run — and `secret/production/<token>/` is how the answer comes back.
`scarves/production.py` picks the baths and draws the PDF.

**This is a tool to facilitate a task in the real world, where a computer is
an ill fit.** That sentence decides most of the arguments below. The dye room
has gloves, water and a sink in it, which makes a phone the wrong thing to be
holding — so the sheet is the work order, a pencil is the input device, and
the phone is picked up once, afterwards, with dry hands.

**A `ProductionRun` is not a source of truth.** It is scaffolding for a
physical job. What closes the loop is the `InventoryLog` rows and the stock
they moved; the run is only how the paper and the phone found each other.
Treating it as a record to be preserved, audited, or worked off as a queue is
the mistake — that road leads to retention rules, reconciliation screens and a
backlog nobody reads, none of which the job needs.

**The row is a bath, not a scarf.** A bath is one blank plus one recipe
yielding `number_per_dye_bath` units of a single SKU, so production is not a
column of counts to be entered — it is a handful of yes/no answers. "We only
got through 10 of the 20" is ten ticked boxes, nobody adds anything up, and
the form on the phone is the same rows in the same order, which makes
reporting recognition rather than transcription.

**One QR for the sheet, not one per row.** Twenty codes would be twenty scans
to record what is one session's work. The token in that URL is what
authorises the return — the same bargain as the other `secret/` pages, scoped
to a single sheet instead of standing open forever, and it means the crew
report production without accounts. No PIN: you reached the page by scanning
something you are holding, so a PIN would be friction with nothing behind it.
`ProductionRun.submitted_by` is filled from the remembered-PIN cookie when the
phone knows a name, purely as a record.

**`ProductionRunRow.applied_log` is what stops a bath counting twice.** The
return URL is printed on paper that can be re-scanned, the button can be
double-tapped, and somebody who remembers one more bath will reopen the page
and submit again. All three are normal. `production.apply_row()` is a no-op on
a row that already has a log — same failure as the Square webhook and
redelivered orders, same fix. Un-ticking is deliberately **not** the inverse:
once stock has moved, taking it back is an inventory adjustment with a reason
attached, not a checkbox on a page with no login.

`quantity` is frozen when the sheet prints rather than read back off the raw
product. The paper says `x4` and the paper is what somebody worked from; if
the bath size is edited next week, that row still has to mean what it said.

### What lands on the sheet

Default is `FinishedProduct.behind_a_bath` — products where a whole bath still
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

**A sheet leaves the outstanding list as soon as one row is reported.** One
tick means somebody is working from it and the loop is closing; after that the
QR code is how you get back to it, which is as findable as it needs to be.
Adding the rest later still works.

**Only the newest `MAX_OPEN_RUNS` (5) stay outstanding — printing a sixth
retires the oldest.** Five out at once already means the reporting loop has
stopped working, and the two ways out of that (never reconciling, or
abandoning the lot and starting over) are both bad. But *blocking* the sixth
print is the wrong medicine: it deadlocks exactly when the paper has gone
missing, which is the same moment a sheet gets abandoned in the first place.
Keeping the newest five never deadlocks.

Retiring costs nothing precisely because a run isn't a record — it is closed
with a note rather than deleted, it applies no stock, and if it turns out to
matter the PDF reprints in one click. The list on the picker is a convenience,
"sheets you might still be working from", not a queue to be worked off.

### The dye collection page

The sheet's first page is a shelf list: every dye the run needs, with a
colour chip, the brand, and how many baths want it. One walk to the shelf
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
`recipe_showcase?edit=true&missing=true`, which can copy dyes from a recipe
that already has them. A count reads as a standing chore; six names read as
an afternoon with a payoff attached, and every one added shows up on every
sheet afterwards. That framing is the point — the backlog gets filled in by
somebody with other demands on their time, so the app's job is to make the
next increment look small and worth it.

Dyes marked out of stock are called out in both places. A missing dye is a
bath that can't run, and finding that out at the sink is the expensive
version of finding it out here.

### Reading the marked sheet from a photo

`secret/production/<token>/` takes a photo of the marked paper and comes back
with the same list already ticked. `scarves/sheetscan.py` does the reading.

**It applies nothing.** The scan fills the form in and a person submits it —
the same rule `colorbands` follows, and the whole safety argument. It also
means the photo path can never be *worse* than tapping: at worst it saves
zero taps and they tap them anyway.

**The barcode does the hard part.** Every row prints one a fixed distance
from its box, so a decoded symbol gives the row's identity *and* the
position, scale and orientation of everything beside it. Finding a tick box
is then arithmetic rather than the general checkbox-recognition problem.
Geometry comes from `production.box_geometry()`, the same constants the PDF
draws with, because a scanner with its own copy would drift — and the symptom
of drift is the worst available: the sample window lands on blank paper and
every box reads empty, which is indistinguishable from a careful person who
ticked nothing.

Two subtleties in that geometry, both of which bite silently:

- **Quiet zones don't scale.** reportlab pins them at a quarter inch, so a
  drawn symbol is wider than `BARCODE_WIDTH` by a margin that depends on the
  value. Scale is worked out from `bars_width()`, never the target width.
- **A decoder returns one result per distinct symbol, not per printed
  symbol.** Three identical barcodes on a page come back as one. A sheet
  routinely prints the same SKU several times — `plan_baths` groups repeated
  baths of a colorway together on purpose — so `row_code()` puts the row's
  position in the barcode as well as its SKU (`RAWSIL-STORMY#3`). Without
  that, four marked baths of one colorway report as one.

**Ink, not colour.** Each barcode is full-black bars on full-white paper a
couple of centimetres from its own box, so it doubles as a calibration
swatch: the dark and light ends of *this row*, under this light, at this
exposure. The box is scored on where it falls between them, which is a ratio
and survives white balance, a tungsten bulb and a glare on one corner. Red,
blue, green and pencil all sit far nearer black than paper; **yellow does
not** and never will, which is why the sheet says "any pen but yellow".
Anything between the thresholds is reported `unsure` rather than guessed.

**The QR binds; it does not authorise.** Reaching this page at all means
holding the run's token — that is the whole of the authorisation, the bargain
`secret/` makes everywhere here, and it is already spent by the time a photo
is uploaded. The QR in the photo adds no permission. What it adds is evidence
that the paper in the picture is the run the URL claims.

So the two failures are not equal, and are treated differently:

| In the photo              | What it means                    | What happens |
|---------------------------|----------------------------------|--------------|
| A QR that doesn't match   | positive evidence of a wrong sheet | refused, nothing read |
| No readable QR            | no evidence either way           | marks read, page says it wasn't confirmed |

The second is deliberately not a refusal. Glare, a torn corner, a third-
generation photocopy and a hurried frame are all ordinary, and turning any of
them into "start again" spends a real person's patience to buy nothing —
they hold the token either way.

It is said out loud rather than passed over, because the evidence is weak
*here* in a way it wouldn't be elsewhere: row codes repeat across runs, and
consecutive sheets tend to be near-identical (print one, don't report it,
print another tomorrow and it lists much the same work). "The row codes
matched" is not much of a check, so the confirmation step is doing the real
work and the page has to point at it.

**Re-reading the same photo ticks nothing new.** `rows_to_tick()` skips rows
already applied, and because a mark maps to exactly one row it can't go
looking for another row with the same SKU to land on instead.

The photo is **not stored**. It's read in the request and discarded — it is
an input to a form, not a record, and the record is the inventory log.

**Marking is positive only.** Tick what you did; never cross out what you
didn't. Crossing out is the tempting shorthand and it's wrong twice: pen
through a Code128 sometimes still decodes and sometimes doesn't, so the
signal that matters rides on the unreliable mark, and an unmarked row stops
meaning anything definite.

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

## Timekeeping: the pay week, and the two totals

The hours form (`secret/hours/`) and the timesheet (`private/timesheet/`)
replace a paper bag and a lot of mental arithmetic. Three things are load-
bearing and none of them are obvious from the models.

**The pay week runs Saturday to Friday.** No date library assumes that, so
every "which week is this?" question goes through `timesheets.week_start()`
rather than being worked out at the call site. Getting it wrong is invisible:
the page still renders seven columns, they're just the wrong seven, and the
totals belong to a week nobody is paying for. `PayWeekTests` pins it.

**Hours are self-reported, not clocked.** Nobody enters a start and end time;
they pick a decimal off a quarter-hour dropdown. That's a deliberate trade —
the arithmetic disappears, and in exchange there's no start time to check a
claim against. What replaces it is review: the sheet flags long days, long
weeks, revised figures and anything reported more than a week late, and a
person signs the week off. Those flags aren't errors and must not be styled
as errors; a 13-hour day at a festival is entirely normal.

The picker is a *rendering* of the rule, not the rule itself. `hours` is a
`DecimalField` validated against "a quarter-hour between 0.25 and 14" —
originally a `ChoiceField`, which compares submitted strings and so decided
`9.5` and `9.50` were different answers and only accepted one.

**Scope is booth hours during festival days — nothing else.** Production
help (dyeing, prep, anything back at the shop) is deliberately not tracked
here. There is no employer field, no work-type field, and no "kind of work"
dimension anywhere in `TimeEntry`, `HoursForm` or `timesheets.py`; an earlier
draft had one and it was removed on purpose.

**Don't add one back as a schema change.** Whether a second kind of work
belongs in these totals is a payroll question, and it has to be answered
before the field exists — a column that quietly starts collecting a second
kind of work makes every total on the timesheet mean something different
depending on who typed it, with nothing on the page to say so. Until then a
single unqualified total is the honest output, and the page says "booth
hours" rather than "hours" so it can't be misread later.

## The booth: photos in, and unidentified sales

`secret/booth/` is one page the crew uses for two things, because there is one
moment when a phone comes out at a stall and asking someone to pick the right
page first is how you get no photos at all. The reason picks which half of the
form is stored — the view keeps only that half, so a report that changed reason
mid-thought can't leave a sharing permission attached to a sale report.

**No login, a PIN, same as the hours form.** The crew has no accounts, and
giving them accounts would hand the production pages to seasonal staff. The
alternative — roles and permissions across ~30 hand-written views — is a real
project, and the trigger for starting it is staff needing to *see* more than
one page, not this. Until then the `Employee` PIN does what it does on the
hours form: it stops the wrong name being tapped, and it is not a secret.

### Signed in? Then no name picker and no PIN

The crew get the name-and-PIN pair; a staff login gets neither, because a
login is a stronger claim than four digits and being asked to pick your own
name off a list on a page you already authenticated for is what makes an app
feel like paperwork.

`Employee.user` is the link, and it is blank for almost everybody — the crew
are deliberately account-less. It only exists so the few people with a login
are recognised. The degradation matters:

| Who                        | Name picker | PIN |
|----------------------------|-------------|-----|
| Not signed in (the crew)   | yes         | yes |
| Signed in, `user` linked   | no          | no  |
| Signed in, not linked      | **yes**     | no  |

The third row is the honest one. Without a link the app genuinely does not
know which `Employee` a login is, and guessing would put somebody else's name
on a sharing permission — so it still asks, and only drops the PIN.

**The fields are removed from the form, not hidden in the template.** A field
that is present but invisible is one a hand-built POST can still fill in, and
here that would mean attributing a photo to whoever the sender named.

### One reason, one half, and no request to switch

The radio hides the half that doesn't apply, in CSS (`form:has(input[value=…]:checked)`),
not with an htmx swap. The fields are already on the page, this gets used on
a phone on one bar of signal, and a toggle that needs the network is a toggle
that sometimes doesn't happen — the same reasoning that keeps the photo
upload off the presigned-POST path. A browser without `:has()` shows both
halves, which is what the page did before, so the fallback is the old
behaviour rather than a form with nothing in it.

None of this is load-bearing for correctness: the view already stores only
the half the reason matches. It's the difference between a form that looks
like it's asking two unrelated things and one that asks the question you
picked.

### Sharing permission is two questions, not one

A tick from the sender is the **sender's** permission. It is not the permission
of whoever is in the photo, and the form refuses a submission that confuses the
two: someone recognisable, plus a destination ticked, requires "I asked them
and they said yes". Untick the destinations and the photo still sends — "here,
your call" is a legitimate answer and must not be blocked.

Website and Instagram are separate ticks on purpose: one is a shop page, the
other is a feed with an audience and a comment box, and people do say yes to
one and no to the other. The gallery reads `BoothPhoto.shareable`, never the
two destination flags, so the awkward case can't be posted by reading the wrong
checkbox. None of this is legal advice; it is a record of who agreed to what,
and when, which is the part that is worth anything later.

### An unidentified sale must not vanish

`square_webhook` used to `continue` past any line item it couldn't tie to a
`FinishedProduct`. That meant a scarf nobody could name was rung up, walked out
of the tent, and left no trace: Square had the money, this app still had the
stock, and nothing in either said they disagreed. **The silence was the whole
failure** — the count was wrong and looked fine.

Now every unplaceable line becomes an `UnmatchedSale`, whatever the reason (no
`catalog_object_id` at all, an unsynced variation, a custom amount). Erring
toward capture is cheap: a row that turns out not to be a scarf is dismissed in
one click, and **dismissal has to exist** — a queue that can only grow stops
being read.

`private/unidentified-sales/` pairs each open sale with booth photos taken
within **±15 minutes** — the width of a queue at a busy stall. The reported
first six characters of the barcode are the *blank*, not the colorway
(`BLANK-DYEBATH`), which is exactly the narrowing worth having: nobody can read
a colorway off a scarf they couldn't name, but the style is on the tag and it
turns a few hundred products into a few dozen. With no prefix reported the page
offers the whole catalogue rather than pretending to have narrowed it.

**Resolving moves stock, and that is not a violation of "back-dated entries
never move stock".** That rule exists because a backfilled kanban card records
a bath that was already counted. This sale was never applied at all — the
webhook dropped it — so `number_on_hand` has been one too high ever since, and
applying it late is the entire point. The `InventoryLog` row is dated at
Square's sale time rather than the moment someone got round to the queue.

Filing the photo against the product is **opt-in** on the same form: a stall
snap in bad light isn't always what the catalogue should show, but when it is,
the scarf nobody could name becomes identifiable next time.

Related, and fixed while the loop was being closed: the webhook now skips a
line it has already logged for that order. Square sends `order.updated` more
than once and `COMPLETED` is not a one-shot state, so a redelivery used to
decrement the same sale again.

**The queue is set to no notification.** Jiminy offers three settings per
thing — none, a digest, or told when it happens — and this one is none. That
is a choice, not a limitation: the queue is reconciliation on a weekly
rhythm, so the count is already wrong by the time a row lands and stays wrong
at exactly the same rate whether it's seen in ten minutes or on Monday.
Nothing downstream is waiting on it. Told-when-it-happens would buy nothing
and cost something real — an alert that turns out not to matter is how
someone learns to ignore the next one that does.

Which setting fits follows from the rhythm of the work, so ask that question
per thing rather than assuming this answer generalises. A label run waiting
on a print shop is time-bound and would want telling; this isn't.

What makes none safe here is that the queue can't quietly empty itself. Every
unplaceable line lands in it, dismissal keeps it readable, and it is still
there on Monday. That is the whole of the guarantee, and it is enough.

## The PIN is remembered, and remembering is not authorising

Both `secret/` pages open with "choose your name" and "type your PIN". That is
the price of having no accounts, and it is charged at the worst possible
moment: a scarf has just sold, the queue is moving, the phone is out for about
ten seconds. Friction there doesn't produce a late report, it produces **no**
report — which makes the page worth nothing. So `scarves/crew.py` keeps the
name and PIN in a signed cookie and both pages open pre-filled.

**The cookie fills the form in; it never stands in for the PIN check.** Both
forms still compare the submitted PIN against `Employee.pin` on every POST,
unchanged. The cookie writes `initial` and nothing else. That is the whole
safety argument, and it is the same shape as `colorbands`: it fills the form
in, the check still happens. A cookie that *authorised* would mean a found
phone submits with nothing checked anywhere, and it would need its own expiry,
revocation and threat model. A cookie that only types for you can be stale,
wrong or forged and the worst outcome is the error message a typo already
gets. `CrewCookieTests` pins that a POST carrying the cookie and the wrong PIN
still fails.

Which is why the PIN itself is in there rather than a token standing for it.
Signing stops tampering, not reading — anyone holding the device can read the
cookie. The PIN was never a secret (it stops the wrong name being tapped), so
storing it somewhere readable-with-the-device costs nothing it was protecting,
and it keeps one code path instead of two.

A cookie outlives the facts in it, so every read resolves against the database
and drops what no longer holds — quietly, because this is a page nobody has
typed into yet and an error on it is noise. Three cases, and the split in the
third is the useful one:

| Cookie says                    | What happens              |
|--------------------------------|---------------------------|
| tampered / unsigned            | forget the lot            |
| employee gone or now inactive  | forget the lot            |
| PIN has since changed          | **keep the name**, drop the PIN |

The name is still right, so the page still knows who this is and asks for the
one thing that actually changed.

**Everyone uses their own phone.** There is no shared stall tablet, and that
is what makes remembering the *name* safe rather than dangerous — on a shared
device a pre-filled name is exactly how one person's hours get filed under
another, silently, and correctly built it would need a much louder confirmation
than a link. If a tablet ever appears, revisit this before anything else.

Even on personal phones the pages **say** the fields were filled in for you and
offer `?forget=1`. Phones get lent, handed over and replaced; a pre-filled name
that nothing mentions is unrecoverable by the person looking at it. The link is
a GET with a side effect, which is fine here because the side effect is this
browser's own cookie — idempotent, nothing written, nothing to re-submit.

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

### Colour bands are not synced, on purpose

`Recipe.color_bands` stays local. The POS never displays custom attributes, so
pushing them would put data in Square that no one can see and that then has to
be kept in step. The question they answer — "what sold in red?" — is a local
join from a sale back to `recipe.color_bands`, which needs nothing at the
Square end beyond the variation ID already stored.

## Templates: three layers, and the `block.super` trap

Every page template inherits from a shared skeleton. Nothing extends
`base.html` directly — pages pick the layer matching their URL bucket:

```
base.html                 doctype, <head>, CSS custom properties, body blocks
├── base_internal.html    private/ pages: house style, messages, ← Site map
└── base_public.html      public/ pages: embed style, htmx, no staff chrome
```

A page supplies `{% block title %}`, `{% block heading %}` and
`{% block content %}`, plus `{% block head %}`, `{% block body_attrs %}` or
`{% block scripts %}` when it needs them. It should not write a `<!doctype>`,
a `<h1>`, a messages loop, or a back link — the layer does all four.

**Page-specific CSS must open with `{{ block.super }}`:**

```django
{% block style %}
  {{ block.super }}          {# without this the whole house style vanishes #}
  :root { --column: 1100px; }
  .my-thing { ... }
{% endblock %}
```

Forget that line and the page still renders, still returns 200, still passes
the smoke test — it just comes out as unstyled HTML. `BaseTemplateTests`
checks a marker from `base.html` survives into every rendered page, which is
the only way this failure gets noticed.

Prefer re-pointing a custom property (`--column`, `--accent`) over restating
rules. Widths are per-page and expected to vary; colours generally aren't.

**Partials under `templates/scarves/partials/` extend nothing.** They're htmx
swap targets and embed payloads, so they carry no shell and inherit styling
from whatever page they land in. When a page renders the same row markup that
a fragment endpoint returns, `{% include %}` the partial rather than copying
it — `production_needed.html` and `recipe_showcase.html` both do this, after
both had drifted from their partials.

## Barcode labels: the sheet is the state

`private/labels/` prints Code128 stickers for stock going onto the shelf —
"everything produced since a date" (the weekly job) or "everything on hand"
(bulk re-label). `scarves/labels.py` picks the items and draws the PDF;
`LabelStock` holds the paper.

**A dye bath is one blank plus one recipe**, so it yields 3–5 units of a
*single* SKU. That's why the sheet is sorted by SKU and nothing cleverer: the
stickers come off in the same clumps the scarves come off the rack. A run is
~20 products × 3–5 ≈ 60–100 labels, against 80 per sheet.

**The run prints its own "start here next time" sticker.** A weekly run leaves
a part-used sheet nearly every time, and a sheet nobody can confidently resume
gets binned — which reads as waste whatever it cost. So the last label of every
run is a dated marker reading `NEXT RUN: START AT n`. It costs one label and it
puts the state on the paper, where it survives a cleared cache, a second laptop
and a week in a drawer. The browser's `localStorage` pre-fills the same number
as a convenience, and the page says so: **if the two disagree, the sheet wins.**

Two cases print no marker, both on purpose: a run ending exactly on a sheet
boundary (nowhere to put it but a fresh sheet, which starts at 1 anyway), and a
continuous roll (`LabelStock.is_continuous`, a 1 × 1 grid — no sheet to resume,
so a marker would cost a label per run and be read by nobody).

**Never print a barcode below `MIN_MODULE_MIL`.** Bars too dense to scan are a
*silent* failure: the sticker looks right and fails at the till with a queue
behind it. `density_problems()` runs before rendering and the view refuses the
whole job, naming the SKUs. This is also why the stock is 1.75in wide — a
13-character `SLUG6-SLUG6` SKU comes out at 7.7 mil there, where the 1in stock
first considered would have printed 4.5 mil. Barcodes are sized **per label**,
not per run, so a short SKU gets fat bars instead of matching the longest one.

**Three datasets, and the hand-picked one is additive.** "Specific items I
pick" is a type-ahead you add "this SKU, this many" rows to — deliberately not
tick-to-exclude on the preview table, because unticking 297 rows to keep 3 is
worse than typing 3. It reuses the upload page's `product_search` endpoint via
`?mode=labels`, which swaps the result template — the search is identical,
only the click behaviour differs.

**A SKU is write-once.** It has been printed since the reference sheets
existed, it's now on stickers stuck to scarves, and Square holds it to
identify a variation — this app can rewrite none of those. So
`FinishedProduct.save()` only ever *fills a blank* SKU, never changes one, and
`generate_skus --overwrite` states how many it would change and asks for
confirmation (`--noinput` for scripts). The symptom of getting it wrong is an
item scanning to nothing at the till weeks later.

**SKUs are assigned at creation** (`FinishedProduct.save()` → `scarves/skus.py`).
Generation used to live only in `generate_skus`, so anything made through the
admin, the bulk matrix or a shell had no barcode until somebody remembered to
run it — which is how unprintable products accumulated. The command stays, for
backfill. Fixtures are unaffected: `loaddata` goes through
`save_base(raw=True)` and never calls `save()`, so a deliberately blank SKU
stays blank (`FixtureSkuTests`). In tests and data repair, a blank SKU can now
only be made with a queryset `update()`.

Related ordering, for a fresh sync: **`generate_skus` first, then
`sync_to_square`.** The sync omits the `sku` key entirely when it's blank, so
syncing first creates Square variations with no SKU and nothing to scan. The
update path does send it, so a re-sync afterwards repairs that — but only if
someone knows to run one.

**Only known SKUs can be printed, and a product with no SKU is shown
disabled rather than hidden.** There's no free-text barcode entry: an unknown
code scans fine and then Square finds nothing, which fails at the till with a
customer waiting and no way to tell a typo from a missing product. The
legitimate case barely exists here — nothing sells through Square without
being a `FinishedProduct`, and production is recorded in the app before labels
are printed — so the escape hatch is "add the product", which has to happen
anyway. A no-SKU product still appears in the results, greyed out and saying
`run generate_skus`, because filtering it out silently means someone searches,
doesn't see it, and never learns why.

**Extras don't apply to a hand-picked run.** The bulk datasets add spares
because their counts are derived and slack is cheap; here somebody typed the
number, so printing more than they asked would surprise. The page hides the
extras box for that dataset rather than leaving a control that does nothing —
the same `data-when` mechanism that hides the date box when printing
everything on hand.

Adding a fourth dataset stays cheap: everything downstream (SKU sort, marker
placement, density guard, sheet plan) operates on `LabelRun.rows`, which is
just product-and-quantity pairs. It's one function returning a `LabelRun` and
one branch in `_label_run_from`.

**The whole-catalogue export starts each blank on a fresh row; nothing else
does.** SKUs read `BLANK-DYEBATH` and sort alphabetically, so a stack of
sheets is already grouped by blank — padding to a row boundary at each change
just stops the seam falling mid-row when the stack gets split up. It's keyed
on the SKU prefix, not the finished product: 266 products would cost a partial
row each, several hundred labels.

Off for every other run, and that's a ratio judgement rather than a
preference. The padding is ~20 labels either way. Across the 31 sheets of a
full export that rounds to nothing; across a 3-sheet weekly run of ~20
products over ~80 labels it's a quarter of the job.

**A label is barcode and SKU text, and that's the whole design.** Branding
lives on the printed hang tags, which are a separate physical thing — so
there's no artwork to fit here, and the austerity is the decision rather than
an unfinished job. It also keeps the stock small and the printing black and
white, which is what makes a weekly run cheap.

**Nobody here owns a printer.** Sheets get printed at a copy shop from a PDF
emailed off a phone, which breaks two assumptions the offsets were built on:
you can't calibrate the machine beforehand, and you have no computer with you
when a sheet comes out 2mm high. So `x_offset_mm`/`y_offset_mm` can be
overridden from the query string (`_label_stock_from`) — adjust on the phone,
re-download, reprint — and an override is **never** written back, because a
correction for one store's machine on one day is not a property of the paper.

For the same reason every sheet prints **registration ticks down the left
margin, one per row of die-cuts**. A dialog left on "fit to page" takes a few
percent off, and that failure is progressive — at 98% the first row is out by
a twentieth of a millimetre and the twentieth is out by five, cut clean
through. A short ruler cannot catch it (98% of an inch is half a millimetre,
unreadable); twenty die-cuts spread over ten inches can, and the two faults
separate by eye: ticks drifting further off toward the bottom means scaling,
ticks all off by the same amount means registration and a nudge fixes it.
Ticks stay left of the first column and the caption sits on the liner, so this
costs no labels. Rolls and stocks without the margin skip it.

The calibration sheet carries a **millimetre vernier through the first
label's corner** so dialling the nudge in is one measurement rather than a
loop — lay it over a label sheet against a light, read the corner off the
scales, type those numbers in. The scales are labelled with the nudge to
*enter*, not the error observed, because a sign error there doubles the
problem it was meant to fix.

**`LabelStock` is eight numbers, not an uploaded template.** Vendors ship
templates as .docx/.pdf, and recovering die geometry from one means
reverse-engineering Word's cell rounding — with 2mm of error costing a sheet to
discover. Instead the numbers are transcribed off the vendor's spec page into
the admin. `overflow_in()` and `clean()` reject geometry that runs off the
sheet (what a transposed pitch digit looks like), `LabelStockGeometryTests`
re-checks every seeded stock, and the calibration route prints outlines plus a
one-inch ruler on plain paper to catch printer registration and a print dialog
left on "fit to page".
