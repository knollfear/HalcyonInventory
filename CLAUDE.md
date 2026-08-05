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

## Site map (`/scarves/`)

`scarves/views.py` has a `@page_meta(...)` decorator and a dynamic `index` view
that builds a self-documenting site map by introspecting the URLconf. The map at
`/scarves/` is generated at request time — nothing is hardcoded.

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

**A view taking URL params (e.g. `<int:category_id>`) does not belong on the
map.** It can only render as a card nobody can click. Give it a picker page
instead, decorate the picker, and hide the parameterised view:

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
The map should have **zero** unclickable cards; `SiteMapTests` asserts it.

**Watch the decorator order when adding helpers near a view.** Defining a
function between `@page_meta`/`@login_required` and the `def` they belong to
silently moves the decorators onto the helper — the page still renders, but
unauthenticated and missing from the map. `SiteMapTests` guards this.
