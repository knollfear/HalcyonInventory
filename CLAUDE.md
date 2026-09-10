# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Read the topic file before you touch the thing

The rules below apply everywhere and are here because breaking one is *silent*.
Everything else — the reasoning behind each feature, and the better-looking
version of it that was tried and failed — lives in `docs/claude/`. These are
preconditions, not a table of contents: read the file **before** editing what it
covers, because most of what is in them is an argument against the change you
were about to make.

| Before touching | Read |
|---|---|
| the pinned nav, `scarves/nav.py`, `?nav=` | `docs/claude/nav.md` |
| dye entry, the dye picker, colour bands, `private/recipes/`, a recipe page | `docs/claude/recipes.md` |
| the planner, production sheets, the oven, `sheetscan`, `secret/production/` | `docs/claude/production.md` |
| raw inventory, undyed yarn, notions, passthroughs, fancy veils | `docs/claude/stock.md` |
| the display map, `DisplayFixture`, `secret/restock/` | `docs/claude/restock.md` |
| `secret/close/`, `closing.py`, kanban tags | `docs/claude/close.md` |
| top/slow sellers, `private/seasons/`, the faire calendar, weather | `docs/claude/reports.md` |
| `Sale`/`SaleLine`, importing history from Square | `docs/claude/sales-ledger.md` |
| `sync_to_square`, variation order, prices, catalogue images | `docs/claude/square.md` |
| hours, `secret/booth/`, unidentified sales, the handbook, the crew PIN | `docs/claude/crew.md` |
| barcode labels, `LabelStock`, photographing stock | `docs/claude/labels.md` |

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
- POST-only / action endpoints (e.g. `record_dye_bath`, `color_bands_save`)
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
`color_bands_save`) and HTMX fragments take params freely, because they carry no
`@page_meta` and were never listed.

**Watch the decorator order when adding helpers near a view.** Defining a
function between `@page_meta`/`@login_required` and the `def` they belong to
silently moves the decorators onto the helper — the page still renders, but
unauthenticated and missing from the map. `SiteMapTests` guards this.

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

**A retired *recipe* only recently started meaning this.** The promise above
held for a retired product and quietly did not for a retired colorway: its
finished products stay active, so `production.candidates()` and
`production_needed_view` kept asking for it — they tested that a recipe
*existed*, not that anybody still dyed it. The symptom was a dye room sent to
make a colour somebody had decided to stop making, with nothing anywhere
saying why. Both now filter `recipe__is_active=True`; the reference sheets and
label runs always did. `Retire` on each open row of `private/recipes/` is
the button, and it collapses the row to a strip with an **Undo** rather than
letting it vanish — a row that disappeared is indistinguishable from a click
that never arrived, and this one sits beside Save on a list a couple of
hundred rows long. Undo is the plain inverse here, unlike the close's, because
nothing moved.

**A `ProductionRun` no longer deletes at all**, and the paragraph that used to
sit here explained why deleting one was cheap: the rows cascaded, the
`InventoryLog` rows stayed, and all that was lost was the trail from the sheet
to the movement. That held while a run was scaffolding. It stopped holding
when the run became the only account of what a session was *asked* to do —
the ledger records what entered inventory, so a cancelled bath and a bath
nobody printed leave the same trace there, which is none. Retiring a sheet is
cancelling what is left on it; see the production-sheet section.

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

**Back-dated entries never move stock.** The card-backfill flow writes log
rows only — that yarn was counted or sold long ago, and adding it to
`number_on_hand` would inflate current inventory by however far back the
records go.

**`private/cards/` is the only door to that**, and the recipe page's
production form no longer has a second one. It used to carry an optional
back-date, folded into a disclosure *below* the submit button, which made one
button mean two materially different things — move stock, or write history and
don't — with the switch deciding which under it and closed by default.
Somebody reading top to bottom reached the button before learning the option
existed. The card page is better at the job anyway: a kanban card is a column
of dates and bath counts, `parse_card_date` reads a month-only date honestly,
and *nothing* on that page can move current stock, so the guarantee is
structural rather than conditional. The recipe form now means exactly one
thing: baths dyed now, stock moves. It links to the cards where the
disclosure used to be, because removing an option without saying where it went
leaves somebody hunting a page that no longer has it.

**The card is the unit of digitising, and one product at a time is the
feature.** Cards get typed up as they are handled on the way back, one in the
hand at a time — so the page asking for exactly that card's dates and bath
counts is the shape of the job, not a limitation of it. The obvious
improvement is to let a whole dye session go in at once, across the two or
three blanks it covered; **don't build it.** Recovering which entries on which
cards belonged to one session means collating dates across a stack by hand
before typing anything, which is more work than the typing and produces a
grouping nothing downstream reads. Write the dates off the card and move on.

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

## The app advises, a person decides — and the tell is a number that looks like a fact

The rule is everywhere in here already: `colorbands` fills the form in and
somebody confirms, the sheet scanner pre-ticks and somebody submits, the photo
walk orders the candidates and somebody picks, the restock board predicts a
shortage and somebody walks it. What is worth writing down is **how it gets
broken**, because it was broken for a long time in the production sheet and
nobody noticed.

**It breaks wherever the input looks authoritative enough that nobody thinks
of it as advice.** `colorbands` got an override because a hue classifier is
visibly fallible — the need is obvious. `par` got none, because par is a
number in a database column and reads as a recorded fact. So the sheet derived
a work order from it, offered no way to change the answer, and the only
choices were print it or don't. That is the computer deciding.

The asymmetry inside this app is the giveaway. `number_on_hand` is also the
app's belief, and it has an elaborate apparatus for a person to overrule it —
the Sunday close, the restock walk, bulk adjustments, all absolute counts. Par
had nothing, and par was never dialled in. **The number that looked least like
an estimate was the one most in need of a human.**

Two things follow, and both are cheap:

- **Every derived answer needs somewhere to disagree with it.** The production
  sheet is now editable end to end — strike a row, add a bath the shortage
  query cannot see, or build the list by hand from "I know what to dye" — and
  the picker offers a suggestion rather than a verdict.
- **Advice you cannot inspect is a decision in disguise.** An override is
  necessary and not sufficient: if the basis is hidden there is nothing to
  judge. Hence the sold count printed beside each colorway on
  `private/production-needed/`, the stock printed beside every zero on
  `private/slow-sellers/`, and the belief printed beside the requirement on
  the collection sheet — `12 (we think 2 on hand)`.

Worth asking of anything new here: what is this page's par? Which stored
number is it treating as ground truth that is really somebody's estimate, and
where is the person's way to say otherwise?

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

## `Reports`: the pages that read and never write

`Top Sellers`, `Slow Sellers`, `Season Pace` and `Close History` share a
category because they are the same kind of page: they read, they render, and
none of them writes a row. They used to sit under `Inventory` beside the
pages that actually move stock — `Raw Inventory`, `Bulk Inventory Update`,
`Sunday Close`, the restock board — which made one category mean two things
and a site map worth less than the sum of its cards.

The line is whether the page can change the numbers it shows you.
`ReportsAreTheirOwnCategoryTests` pins the grouping and checks the modules
behind them contain no writes at all.

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
