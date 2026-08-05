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

Views that need URL params (e.g. `<int:category_id>`) are detected automatically.
Give them a `param_links` callable and the card lists one real link per value
instead of a dead "needs params" badge:

```python
@page_meta(
    title="Raw Inventory (by category)",
    description="...",
    category="Inventory",
    param_links=lambda: _category_param_links(),
)
def raw_inventory_view(request, category_id):
    ...
```

It takes no arguments, returns `[(label, reverse_kwargs), ...]`, and is called
at request time so it sees current data. Without it the card falls back to the
non-clickable "needs params" form. Failures are swallowed — the site map lists
every other page and must never be the thing that 500s.

**Watch the decorator order when adding helpers near a view.** Defining a
function between `@page_meta`/`@login_required` and the `def` they belong to
silently moves the decorators onto the helper — the page still renders, but
unauthenticated and missing from the map. `SiteMapTests` guards this.
