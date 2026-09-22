"""The site map: `@page_meta` on a view, and the two directories built from it.

`scarves/private/` lists every decorated page; `scarves/public/` is the same
directory filtered to the `public/` bucket. Both are generated at request time
by walking the URLconf, so a new page appears by being decorated and nothing
else — see *Site map* in `CLAUDE.md` for the conventions.

This is its own module because two things need it and they used to import
each other for it: the views define the pages, and `nav` (the pinned corner)
needs the list of pages to offer. Keeping the map here lets both import
downwards. The URLconf import inside `site_map` stays deferred on purpose:
`urls` imports the views, the views import this, and the map is only ever
built inside a request, long after everything is loaded.
"""
from django.urls import reverse


def page_meta(title, description, category="General", note="", show_in_index=True):
    """
    Attach human-readable metadata to a view so the site map (index) can
    describe it automatically. Add this decorator to any new view and it will
    show up on the site map at /scarves/private/ with no extra wiring.

    note: optional caveat shown under the description (e.g. "POST only",
          "requires ?raw_ids=1,2,3").
    show_in_index: set False to hide a view from the site map. That's the answer
          for a route taking URL params: give it a picker page, list the picker,
          and hide the parameterised view rather than showing a card nobody can
          click.
    """
    def decorator(view_func):
        view_func.page_meta = {
            "title": title,
            "description": description,
            "category": category,
            "note": note,
            "show_in_index": show_in_index,
        }
        return view_func
    return decorator


def site_map(bucket=None):
    """Build the site-map cards by introspecting the URLconf.

    Nothing here is hardcoded: decorate a view with @page_meta and it appears.
    Pass `bucket` ("public") to list only that half — which is what keeps the
    public map from naming staff pages it would only be teasing visitors with.
    """
    from scarves import urls as scarves_urls

    prefix = "/scarves/"
    categories = {}
    seen = set()
    counts = {"public": 0, "private": 0, "secret": 0}

    for entry in scarves_urls.urlpatterns:
        callback = getattr(entry, "callback", None)
        if callback is None:
            continue

        meta = getattr(callback, "page_meta", None)
        if not meta or not meta.get("show_in_index", True):
            continue

        # Same view can be registered under several routes (e.g. the webhook
        # with/without a trailing slash) — only list it once.
        if callback in seen:
            continue
        seen.add(callback)

        # The URL's own first segment is the source of truth for exposure —
        # the same string URLBucketTests checks the view's behaviour against,
        # so a card can't claim to be public while the view demands a login.
        route = str(entry.pattern)
        entry_bucket = route.split("/")[0] or "private"
        if bucket is not None and entry_bucket != bucket:
            continue

        converters = getattr(entry.pattern, "converters", {}) or {}
        params = list(converters.keys())
        needs_params = bool(params)

        url = None
        if entry.name and not needs_params:
            try:
                url = reverse(entry.name)
            except Exception:
                url = None

        item = {
            "title": meta["title"],
            "description": meta["description"],
            "note": meta["note"],
            "name": entry.name or "—",
            "route": prefix + route,
            "url": url,
            "needs_params": needs_params,
            "params": params,
            "bucket": entry_bucket,
        }
        if entry_bucket in counts:
            counts[entry_bucket] += 1
        categories.setdefault(meta["category"], []).append(item)

    # Stable, sorted output: categories alphabetical, items by title.
    grouped = [
        {"name": name, "items": sorted(items, key=lambda i: i["title"])}
        for name, items in sorted(categories.items())
    ]
    total = sum(len(g["items"]) for g in grouped)

    return {
        "grouped": grouped,
        "total": total,
        "public_count": counts["public"],
        "private_count": counts["private"],
        "secret_count": counts["secret"],
    }
