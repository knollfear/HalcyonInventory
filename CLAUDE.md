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

## What the shop actually makes

Nothing in the schema says this, and it explains the shape of most of what
follows.

**The dyeing is the value add.** Everything is dyed here — that is the
business, and it is why the colorway rather than the style is the axis
everything is organised around. (The one documented exception is the small
amount of stock sold exactly as it arrives; see *Undyed stock* below, where a
null `recipe` is what marks it.)

Five styles cover about 99% of what gets made and sold, and **the scarves are
silk**:

- infinity scarves
- sash belts
- half circle veils
- rectangle veils
- triangle fringe scarves

Alongside them are **four base yarns** — Heavenly, Homespun, Artisan and
Noble — each carried in roughly **forty colorways**. The yarns are *not*
silk: they are some kind of wool, and **which animal it came from is neither
tracked nor interesting at this level** — llama, alpaca, rabbit, goat, it
varies and nobody here needs to know. Don't add a fiber field to make that
answerable; the question hasn't been asked and a column that starts quietly
collecting one is how a schema grows a dimension nothing reads. There are odd
other things; they are not worth special-casing and no code should assume
they exist.

**The catalogue is narrow in styles and very wide in colour**, and that one
fact drives a lot of the design. A handful of blanks times a few hundred
recipes is what produces the product count, which is why:

- SKUs are `BLANK-DYEBATH` and `private/unidentified-sales/` can narrow on
  the first six characters — the style is the small half of the identifier;
- the Square axis is ITEM = style, VARIATION = colorway, and variation
  ordering matters enough to have its own pass (a till list forty deep is
  unreadable in creation order);
- reference sheets are printed per colorway rather than per style, and the
  by-colour ordering exists at all;
- a dye bath is one blank plus one recipe, so label runs clump by SKU.

Worth stating plainly because the intuition it corrects is the common one: a
new product is almost never a new *style*. It is another colour of something
that already exists.

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

## Dye entry: the list you can read, and the dye that isn't on it

The dye boxes on `private/quick-recipes/` and `private/recipes/?edit=true`
are one control, `DyeSelect` plus `partials/dye_picker.html`. Two failures
put it there, and both are quiet.

**The catalog number ruins the alphabet.** Dharma and Jacquard number their
dyes, so `416 Peacock Blue` sorts under 4 — an alphabetical list comes out in
catalog order, and the browser's own type-to-jump matches the number too.
`Dye.sort_name` drops the number for sorting and searching; the number stays
on screen, because it is what is printed on the jar. Sorting happens in
`DyeSelect.optgroups`, not the queryset, so the regex lives in one place.

**A dye that isn't on the list stops entry.** What actually happens then is
the recipe gets typed with the dyes that *were* on the list, and the missing
one is lost — silently, because the recipe looks filled in. So the picker
offers to add one, and `NewDyeForm` takes a name and nothing else. Brand and
colour are real questions, but they are not answerable at speed with wet
gloves on, and demanding them buys a tidy row at the price of no row at all.

Three things make that deferral safe:

- **A new dye has no hex, and blank is honest.** `Dye.hex_color` used to
  default to red, which reaches the rainbow sheet, the games and the
  dye-collection page as a fact nobody typed. Blank contributes nothing
  anywhere instead: no band from `colorbands`, no point in
  `colorutils.recipe_palette`, an empty chip on the production sheet. Same
  bargain the colour classifier makes.
- **`Dye.needs_review` is what makes the unfinished half findable**, and the
  admin's dye list filters on it with `list_editable` for the colour, brand
  and stock flag — a dozen rows on one screen rather than a dozen round
  trips.
- **`import_dyes` finishes them off wholesale** (below).

**Duplicates are the thing to guard.** `dye_match_key` decides whether two
names are the same dye: it drops the catalog number, a trailing `(Primary)`
and a trailing mark, because those are exactly what someone typing from
memory leaves off. Ten of the 84 acid dyes carry the tag, so getting it wrong
duplicates the most-used dyes in the range. The picker checks it to decide
whether to *offer* the add row; `NewDyeForm.find_existing` checks it again on
the way in and hands back the dye that exists rather than erroring — an error
would leave the slot empty and the person retyping a name that was right.

The same reasoning made both pickers offer **out-of-stock dyes**. Hiding them
was survivable while the list was take-it-or-leave-it; now a hidden dye is one
somebody types in again, and the second `Peacock Blue` splits a history that
reads as complete on either row.

The plain `<select>` is still what posts and what `ModelChoiceField`
validates — the script only puts a text box in front of it. With the script
blocked the page is the same form with a longer list. Rows swapped in by htmx
re-enhance on `htmx:afterSwap`, which is why the picker is included *after*
htmx on the showcase.

### `import_dyes`: re-importing the catalog over a live list

`loaddata` cannot do this. The fixtures carry primary keys and `RecipeDye`
points at a dye *by* primary key, so loading `dharma_dyes.json` over a
database whose pks drifted rewrites the dye pk 7 refers to — every recipe
using it changes colour, with no error and no clue, and the symptom is a
scarf on the reference sheet under a band it was never dyed in.

So `import_dyes` matches on content:

```
python manage.py import_dyes scarves/fixtures/dharma_dyes.json \
    --brand "Dharma Acid Dyes" --dry-run
```

The two catalogs on disk are `fixtures/dharma_dyes.json` (84) and
`fixtures/jaquard_dyes.json` (48). They are read here as data, not loaded as
fixtures — see above for why that distinction is the whole point.

- **A colour already on file is skipped**, whatever it is called. That is the
  rule that makes a re-import safe to run twice: a name tidied up by hand
  must not come back under the catalog's version of it.
- **A name already on file with a different colour is a conflict, and is left
  alone and named.** The colour in the database was put there by a person;
  the file is a catalog scrape.
- **A name on file with *no* colour gets filled in** — colour, catalog number
  and brand — which is precisely the picker's half-finished row being
  completed.
- Name matching is scoped to the brand being imported plus the uncategorized
  pile. Jacquard's `Peacock Blue` is a different jar from Dharma's 416.

`--dry-run` prints the whole plan and says when `--brand` would create a new
brand, because a typo there splits the range across two brands with nothing
to say they belong together. It reads either shape: a Django fixture, or a
plain `{name: hex}` map, which is what a fresh scrape off a supplier's page
looks like before anyone has made it into anything.

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
- **Yellow ends at 61 degrees, not 70.** Sorted by hue the catalogue has an
  empty corridor from 69.2 to 79.3 — no dye lives there — so a boundary
  anywhere inside it classifies nothing, which is how 70 survived without ever
  being examined. Just below sit five jars everyone reads as green: Lichen
  (62.4), Chartreuse Neon (62.6), Avocado (64.9), Radioactive (66.6) and
  Chartreuse (69.2). The nearest true yellow is Fluorescent Lemon at exactly
  60.0, so 61 takes the cluster and leaves the yellows a degree of room.
  Beware that `band_for_hsl` holds a **second, unrelated 70** — the cream rule
  that keeps Ivory out of yellow. Moving one with the other is the mistake.

- **Rainbow is a section, and it is the one that isn't a hue.** It exists for
  the reason pink and brown do: it is what somebody says out loud about a
  scarf. Nobody asks for one with red, orange, yellow, green and blue in it —
  they ask whether you have a rainbow, and then which rainbow. Both answers
  without it are bad and the shop was living with the worse one: claim every
  band and four colorways print in all eight sections, each time as the least
  useful answer to the question that section asks; claim none and they print
  nowhere, which is what was happening — two of the four filed as
  confirmed-with-no-bands, arrived at by giving up, and seventeen active
  products in no section at all.

  The classifier folds a spread of **five or more** chromatic bands into
  `rainbow` alone, neutrals included. Five sits in an empty corridor the same
  way the yellow/green boundary at 61 does: confirmed colorways in stock top
  out at four bands, and both that reach four are emphatically not rainbows
  (Forest Fire, Mooney), so the line reclassifies nothing that exists. The
  fold lives in `bands_from_dyes` and `bands_from_image` and **not** in
  `color_bands_save` — same division as the neutral rule, so a warm rainbow
  that genuinely reads red can claim both, because that is a judgement about
  the scarf. Nothing ever classifies a *single* colour as rainbow; it is a
  property of a set, which is why it is out of `CHROMATIC`.

  Its printed tab draws the spectrum in stripes with a white label plate
  rather than picking a stand-in colour — a tab is read fanned, from the edge,
  and one flat colour there would be a lie in the one place tabs are used
  without reading. The classification page's chip does the same in CSS. The
  three or four kinds of rainbow are separate recipes and need nothing: a
  "kind of rainbow" field would be the fiber-field mistake.

- **Neutral only claims a recipe when it is the *only* band.** Black, grey and
  cream are working dyes that ground the colors beside them. Every neutral-ish
  recipe in stock reads as something else too (`turq-mid-black`,
  `grey-forest-navy`), and nobody looks for those under grey.

### Two axes on the confirmation page

`private/colors/` filters on **confirmed-or-not** and **has-an-active-product
-or-not**, and they are separate questions rather than one row of alternatives.
A colorway with no active product prints on no sheet and hangs on no peg, so
confirming its bands changes nothing anybody can see today — still worth doing
eventually, which is why it is filtered rather than dropped. The pair that
matters is `?todo=true&with_products=true`: colorways a customer can ask for
that the sheet is currently leaving out.

Both filters ride in the query string and **every pill carries the other axis**
rather than resetting it, so that pair is two taps and a link somebody can
send. **Counts are scoped to what is on screen** — a pill reading "Unconfirmed
57" over a list of nine is the page contradicting itself, and the number people
act on is the one beside the list they are reading.

Worth knowing where the work actually sits, because it is not where you would
guess: the dye entry has gone mostly into recipes with *no* active product (the
dye book), while the bands have been confirmed on the ones that sell. So bands
are the well-covered signal on sellable colorways and linked dyes are not.

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

## Retire, don't delete

A product that ever sold is pointed at by inventory logs, resolved sales and
production rows, and that history stays interesting long after the thing
stops selling. So **`is_active = False` is how a product goes away**, on
`FinishedProduct`, `RawProduct`, `Recipe` and `Employee` alike. Retiring
takes it out of production planning, the reference sheets, the label runs and
the Square sync without touching a row anyone might want to read later.

This is enforced by the schema rather than left to discipline. Everything
that records *what happened* points at a product with `on_delete=PROTECT` —
`InventoryLog`, `ProductionRunRow`, `UnmatchedSale.resolved_product` — so a
delete of anything with history raises `ProtectedError` instead of taking the
history with it. `RetireDontDeleteTests` pins that.

Two deliberate exceptions:

- **`FinishedProductImage` cascades.** A photo is a depiction, not a record,
  and an orphaned one has nothing left to depict. The `post_delete` signal in
  `signals.py` drops the stored file with it.
- **A product with no history really does delete.** Nothing points at it, so
  there is nothing to preserve — a row typed in by mistake shouldn't need
  retiring.

The same reasoning explains what happens when a `ProductionRun` is deleted:
its rows cascade away, but the `InventoryLog` rows they created are separate
objects and stay. Those baths were really dyed. What goes is the trail from
the sheet to the movement — which is fine, because the run was scaffolding
and the log is the record.

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
`recipe_showcase?edit=true&missing=true`, which can copy dyes from a recipe
that already has them. A count reads as a standing chore; six names read as
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
  symbol.** Three identical barcodes come back as one, and a sheet routinely
  prints the same SKU several times — `plan_baths` groups repeated baths of a
  colorway together on purpose. So `row_code()` carries the row's position as
  well as its SKU (`RAWSIL-STORMY#3`).

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

When the QR itself can't be read, the upload page asks for the code printed
beside it (`42-brisk-wombat` — words, because someone types this off paper;
`normalize_token` makes case and punctuation irrelevant). That is nearly
always a soft photo rather than a wrong sheet, so it is a way through rather
than an interrogation. Rows in the photo that aren't on the named run are
reported too — expected to be empty forever, but the matched marks would
otherwise land there unremarked.

The photo is **not stored**. It's read in the request and discarded — an
input to a form, not a record. The record is the inventory log.

**Marking is positive only.** Tick what you did; never cross out what you
didn't. Pen through a Code128 sometimes still decodes and sometimes doesn't,
so the signal that matters would ride on the unreliable mark, and an unmarked
row would stop meaning anything definite.

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

## Display capacity is not demand

The northstar for everything below, and the thing most likely to be undone by
a well-meaning change.

**The app tracks total stock. Backstock is derived and never stored.**
`FinishedProduct.number_on_hand` counts what is hanging on the display *and*
what is in the bag behind it, together, so that moving a skein from bag to peg
changes nothing anywhere. `display_slots` says how many homes a product has
when the display is full — pegs times what a peg holds, or spots on the pole —
and `backstock` is `number_on_hand - display_slots`, read rather than recorded.

The reason is a failure that was actually happening. Store a backstock number
and the shop's own furniture starts ordering dye baths: build a new rack, fill
it from the bags, and a backstock tracker reads empty and calls for
production, when nothing sold and the stock simply moved across the stall. The
display grows every year. Every year that growth billed itself as demand and
got paid for in dyeing — seven to nine weeks of it, at the pace of whatever
sold that week.

Three rules follow, and they are load-bearing:

- **Par is the production trigger, and par is about demand.** Nothing derives
  production from display capacity, and nothing here writes par.
- **Par is held fixed. It never moves because the shop got a new rack, table
  or pegboard.** Raising a product's par is still how you ask for more of it,
  but it is a rare, deliberate, evidence-backed decision about *that
  product's* demand — years of data showing rainbow scarves sell as fast as
  they can be made. Not a response to furniture.
- **A hole in the display is not a make-more signal.** `display_hole` exists
  and nothing consumes it. A hook that holds four is worth having precisely so
  that three is allowed to be enough; wiring the gap to a dye bath puts
  capacity straight back on the path to production. It is a merchandising
  reading — and once the display is mapped, a way to see a bare peg from a
  desk instead of by walking the stall.

The goal all of this serves is **a flat year rather than a fast week**:
pre-dyeing as much of a season as possible instead of dyeing each week what
sold in the last one. Anything that makes production react to this week's
sales is a regression, whatever else it improves.

Two things are deferred on purpose and should stay that way until they're
asked for. Par today bakes in display plus backstock; decomposing it into
expected sales plus a desired buffer is a real question and today's answer
works. And stock will eventually be **geographically scattered** — a storage
locker, online fulfilment — so don't bake in the assumption that one number
describes one place.

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

So the supply stays unplannable and the **demand becomes answerable**: sales
land in `InventoryLog` like everything else, and at the end of a season "what
did fancy sell" is a query.

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

## Self-healing, and eventually concurrent

The governing assumption for anything that moves stock: **nobody will
document the movement, and the system has to be right anyway.**

The worked case is fancying. Roughly a hundred scarves left plain stock to
have extra line work added, turning them into Fancy Veils — a different
product, a higher price, and the thing that makes the sale. Not one of those
transfers was recorded. The obvious fix is a transfer form; what a transfer
form actually produces is no form *and* the same hundred scarves, because the
discipline it depends on is the discipline that was already missing.

So neither half is asked for. Each heals on its own:

- the **plain colorway** shows up as an overcount — its peg won't fill, or
  fills at zero — on a restock walk or as an extra tag at the close;
- the **Fancy Veil** shows up as an undercount, when more turns up on its own
  peg than the app expects, possibly weeks later and on a different board.

The two never meet, and they do not have to. This is *eventually concurrent,
not guaranteed concurrent*: two independent absolute counts converging over
passes, rather than one transaction that has to be right at the moment it
happens.

**Which is why corrections are absolute counts, never deltas.** "There are two
of these" heals regardless of what went unrecorded in between; "take two off"
only works if everything before it was right. `record_count`, `record` and
`set_on_hand` all take a total for this reason.

**The rule that follows: never add a step that has to be remembered to be
correct.** A flow that only works when somebody does the extra thing does not
work — it just fails somewhere less visible, and usually silently. If a
movement can go unrecorded, assume it will, and make sure something later
counts the pile.

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

**Every number on a tile is a checkable claim.** A peg says what to put out
and what the bag should hold afterwards (`on_hand - display_slots`), never a
total — a total needs the peg counted, the bag counted and the two added,
which is not something anybody falsifies at a glance. The bag figure is read
straight off the bag as the work finishes, so a tap confirms both halves of
the app's belief rather than only the visible one.

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

The one thing said out loud is `_drained_at`: **a peg reckoned to have run
bare while stock is still behind it.** That is yarn that could be selling and
isn't, which is the only version anybody cares about. The tile says `empty`
and the picker counts how many — a state and a count of work available, with
no threshold and no escalation. Whether it matters depends on how busy the
stall is and whether anyone is free, neither of which the app can see, so it
states the fact and a person decides. Same rule `colorbands` follows.

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

**Unlike a `ProductionRun`, this is a record.** A production run is
scaffolding: the `InventoryLog` is what survives it. Here the count of things
found wrong *is* the deliverable, which is why the rows are kept, why the
admin is read-only, and why nothing deletes.

### It never reaches Square

The close runs at a field on one bar of signal. A step that needs the network
is a step that sometimes doesn't happen — the same reasoning that keeps the
booth form's toggle in CSS, and why the unexpected-tag search is a plain form
submit rather than a type-ahead.

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
are recognised.

| Who                      | Name picker | PIN |
|--------------------------|-------------|-----|
| Not signed in (the crew) | yes         | yes |
| Signed in                | no          | no  |

**A signed-in person is never asked who they are** — not here, not on the
close, not on the handbook. `crew.employee_for` is the one resolver all three
use, and an unlinked login gets a row made for it, named after the username
and linked, so it happens once per person.

That third state used to exist and be defended: an unlinked login fell back
to the name picker, on the grounds that the app genuinely doesn't know which
`Employee` a login is and guessing would put somebody else's name on a
sharing permission. The premise is still true and the conclusion was still
wrong. What it produced was somebody signed in being shown a list of their
colleagues and asked which one they were, on a page they had already
authenticated for — and the picker is itself a thing to mis-tap, so it was
*worse* for attribution than having no picker at all. The resolver doesn't
guess; it makes a row for the person demonstrably signed in.

**A created row has a blank PIN**, and that is load-bearing rather than
unfinished. These people sign in through Django, so a PIN would be a second
credential for the same person that nobody has been told. `clean_pin` wants
four digits, so nothing submittable matches a blank one — the row cannot be
used to walk in through the name-and-PIN door. Blank-PIN rows are kept off
the name pickers for the same reason: picking one is a dead end. Type a PIN
into the admin and the row joins the picker, which is the whole of what that
field now means.

One guess is allowed: a same-named row that isn't linked yet gets linked
rather than duplicated. An exact username against an exact employee name is
narrow, and the alternatives are an `IntegrityError` on a unique name or two
rows for one person.

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

The reason on the booth form is **"A colorway nobody could identify"**, not
"a scarf" — it is one problem wearing two coats. A skein of yarn and a silk
scarf raise exactly the same question ("which colorway is this?"), and a
label naming only one of them invites a hesitation over whether the other
counts. (The undyed yarns sit outside this and need no special wording: in
practice they are unmistakable on the table.)

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

## The crew handbook: read it, then take your pass

`secret/handbook/` is what the crew are given instead of being told things at
eight in the morning on the first day. It covers the till, the look-up books,
sending photos in and reporting hours, and it ends by handing that person
their faire pass as a PDF.

**The pass is the reason anyone comes back**, and that is the design. A page
of instructions is read once and never again; a page you have to return to
when you lose your pass is a page whose contents stay reachable. It is also
the one crew page that can be relied on to be found, which makes it the right
place to list the other `secret/` URLs — they are off the public map and off
the staff map the crew can't see, so their only way in is a bookmark or a
text message.

**The gate is not protecting the passes.** A pass is a barcode and a
photograph, trivially faked by anyone who wanted one, and everybody who can
reach this URL is getting one anyway. The name and PIN are there to pick
*whose* PDF comes back. That is why there is no attempt-throttle here unlike
the hours form: the thing behind that lock is worth locking, this one isn't,
and a lockout lands on somebody standing at the gate without a pass.

**The button at the bottom is the whole of the scroll enforcement**, plus a
checkbox. Anything cleverer means JavaScript, and JavaScript failing here
means a crew member at the gate on one bar of signal with no pass — the same
reasoning that keeps the booth form's toggle in CSS. A checkbox costs a tap
and cannot fail closed.

**Nothing is recorded.** No read-receipt, no timestamp, no per-season
version. The tick is a speed bump asking somebody to look at the page, not
evidence to be produced later, and storing it would invite exactly that use —
which a checkbox on an unauthenticated page cannot support. The pass is
downloaded and kept; fetching it again just means coming back, and
`crew.initial` has already filled the form in on the phone that fetched it
the first time.

**A missing PDF names a person to contact rather than showing a dead
button.** Nothing the reader can do will make the page work, so the page has
to stop them waiting on it. `EmployeeAdmin` carries a `Pass` column for the
other side of that: the useful question is "who is going to reach the bottom
and be told to contact me", and it is only answerable by looking down the
whole roster at once.

**The PDF is streamed, never linked.** The bucket is private, so an unsigned
`.url` would simply 403 — but even where it wouldn't, a link to somebody's
pass outlives the page that produced it.

### Write it as if it were always true

The handbook carries **no temporal language**: no "new this year", no "we've
changed", no "previously". The reader may have worked here for years and
still never have seen any of it, and a page half-describing a world that has
moved on gives nobody a way to tell which half. A revised date at the top
carries all the time information there is. Sections run in the shape of a day
— the till, the books, photos, hours — rather than in the shape of the
software, because someone reading it cold is working out what happens to
them.

Two things in it are worth keeping accurate because they are the ones that
cost money when wrong: **the variation is the colorway** and a wrong one
balances perfectly at the till while corrupting the count of two colours, and
**the pay week runs Saturday to Friday**, which is what keeps a faire weekend
inside a single pay week instead of splitting it across two.

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

That makes `_reorder_variations` the most dangerous call in the file. **An
ITEM upsert replaces the variation list outright, so a variation missing from
it is deleted**, taking its Square ID, its stock and the sale history's link
to it. One rule keeps that safe, and it is worth stating plainly:

> The pass never *builds* a variation. It reads the item as Square has it,
> permutes the list Square returned, and sends that back.

Everything else follows from it. An item that comes back with no variations is
skipped rather than sent — an answer we didn't understand looks exactly like
an item with nothing under it, and only one of those is safe to echo. A
variation with no `name` is skipped too: it is named by an item option, whose
values already decide the order, and sorting it on the empty string would
bunch it at the top and fight whatever set that.

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

## Photographing stock: the barcode misses, and the pile doesn't

`private/images/upload/` files a photo against a `FinishedProduct` by reading
the Code128 on its tag. In a real session of forty photos **about half of them
don't read** — a phone camera, a small symbol, whatever the light is doing —
and each miss then costs a product name typed out in full, one-handed, next to
the pile.

**So the page asks what you are photographing, once.** A session is a pile of
*one blank*: forty half circle veils, then forty sash belts. That is the blank
half of `BLANK-DYEBATH`, it is known before the first shot, and it stays true
for the whole pile — so it is typed once instead of forty times. A failed
decode comes back with `HALFCI-` already in the search box and the search
already run, which has narrowed a few hundred products to that blank's forty
colorways; two or three letters of a colour name finish it.

Three things keep that honest, and they are the same three the rest of the app
makes:

- **It fills the form in; it never files anything.** A read barcode still
  wins outright, whatever the menu says — the barcode is evidence and the
  menu is a statement of intent. Same rule as `colorbands` and the crew
  cookie.
- **It is a prefill, not a filter.** The box is ordinary editable text, so
  shooting something off-pile costs one clear. Nothing is hidden from the
  search, which is what would make an off-pile scarf unfindable.
- **The answer is read at send time, not at page load**, because the pile
  changes partway through a session and a stale answer would quietly prefill
  the wrong blank.

The menu offers only blanks with an active product under them, and the value
is `skus.slug(name)` — the same function the SKU was built from, so a prefill
that stopped matching would mean the SKU rule itself had moved. The submitted
value is run back through `slug` on the way in.

### Walking a display instead: the peg is the identity

`private/images/display/` inverts the identification problem rather than
solving it. A peg *is* an identity — the map already says Artisan — Crocodile
hangs at row 3, column 4 — so a photo taken at a known stop needs no barcode,
no typing and no search. The walk says what to shoot, you shoot it or skip,
and either answer moves to the next peg.

**Two routes through it, and they run in opposite directions.**

- **Map first.** Fill the map in the editor, then walk: each stop names the
  colorway and the photo files itself with nothing typed.
- **Photos first.** Build the fixture in the admin, walk the bare board, and
  at each peg photograph what is hanging there and pick the colorway — which
  goes *on the peg* as well as on the photo. One walk produces the pictures
  and the map together.

The second is what the ranking is for. The photo just taken is classified by
`colorbands` and the candidate list is ordered against each colorway's
confirmed bands: **exact set, then any superset, then any overlap, then the
rest alphabetically.** A superset of five extra bands sits with a superset of
one — the count of extras is not a penalty, because a scarf with a lot going
on is not a worse match for the blue and green in the photo.

Four things hold it honest:

- **Colour orders the list; it never picks.** A band set is not an identity —
  dozens of colorways are blue-and-green — so this moves the answer near the
  top and the person holding the scarf does the rest. Photo dominants
  classify right about 4 in 5 times, which is plenty for ordering a list
  somebody reads and nowhere near enough to file on.
- **Only confirmed bands rank.** An unreviewed guess ordering the list would
  look exactly like a reviewed one, so unconfirmed colorways fall to the
  alphabetical tail — and the card **says how many could be ordered at all**,
  because a list that fell back to alphabetical looks identical to one where
  the photo matched nothing and only one of those has a fix.
- **The stop beats a barcode that disagrees, and the disagreement is
  reported.** The batch page's blank picker is a coarse statement covering
  forty photos, so there a decoded symbol is the better evidence. A stop is
  the opposite: made per photo, at the peg, by somebody looking at the scarf —
  while a symbol that resolves in shot may belong to the colorway hanging two
  inches to the left.
- **An occupied peg is never overwritten**, the same refusal
  `copy_board_layout` makes. Assigning is only ever filling a blank.

**Where you are is the URL and nothing else** (`?row=3&column=5`). Fifteen
photos in, get distracted, come back to the peg — and an address that is no
longer a stop advances to the next one that is, so a bookmark taken before the
board was rearranged resumes rather than failing. Nothing stores progress: a
cursor would be a second place the answer lived, and the one it disagreed with
would be the one somebody was looking at.

Reserved spaces are not stops — the price tag is not a colorway nobody got
round to photographing. Empty pegs are, because a photo taken at one is how
the map finds out what is hanging there.

The uploader itself is one script (`partials/uploader.html`) driven by data
attributes, because the batch page and the walk differ only in what rides
along with the process call and what happens afterwards. A second copy with
two lines changed is what would drift, and the way it would show is one of the
two pages quietly uploading to the wrong place.
