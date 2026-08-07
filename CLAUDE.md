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
