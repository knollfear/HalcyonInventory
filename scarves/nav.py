"""The pinned pages in the corner of every staff page.

Navigation here is hub-and-spoke: every page is reached from the site map and
left the same way. That is right for a directory of thirty-odd pages nobody
memorises, and wrong for the four somebody opens every day — those cost two
clicks each, forever, and the second click is a page nobody wanted to look at.

So a handful of pages get pinned beside the "← Site map" link that is already
in the corner of every internal page. The map stays the hub; this is the
shortcut past it.

**Pinned, never ranked.** The obvious version counts visits and promotes the
top four on its own, and it fails three ways at once. It is self-reinforcing —
a page in the top four is one click away, so it gets opened more, so it stays,
while one that drops out becomes two clicks away and gets opened less. It
counts the wrong thing, because a page you open forty times in an afternoon
because it is awkward outranks one you depend on weekly (`restock` already says
this out loud: five passes in five minutes is a good afternoon, and it counts
nothing). And navigation that reorders itself cannot be learned — the whole
value of a fixed nav is that the third pill is always the third pill, and a set
that reshuffles costs a read on every glance.

Most of all it would be a ranking with nowhere to disagree with it, which is
the `par` failure exactly: a derived number that reads as a fact because it
came out of a counter. See CLAUDE.md, *The app advises, a person decides*.

**The counter still exists, one step back.** Visits are counted, and the count
is shown on `private/navigation/` beside each page as evidence for a decision
somebody makes by ticking a box. That is the bargain `colorbands` makes with
the rainbow sheet and the sheet scanner makes with the tick boxes: fill the
form in, and let a person confirm.

**Route names, never paths.** `mysite/urls.py` ends in a catch-all that
redirects anything unmatched to the public map, so a stale *path* in a nav
would land on a working-looking page with nothing to say it was wrong. A route
name either reverses or it does not, and one that does not is dropped *and
named* on the navigation page.

Nothing here writes to the database and nothing here is a claim about identity
or permission — every page a pin can point at is `@login_required` in its own
right, so a forged cookie buys a link to a page the browser could already
reach. That is why these are plain cookies where `crew.py` signs its own: there
is nothing to protect, and defensive parsing is what actually matters.
"""

#: The pinned set. `name` or `name:Label`, comma separated.
PIN_COOKIE = "scarves_nav"

#: How often each page has been opened. `name:count`, comma separated.
SEEN_COOKIE = "scarves_nav_seen"

#: The query string that sets a nav, and the value that clears one.
PARAM = "nav"
FORGET = "forget"

#: Four. The corner is shared with the site map link and this has to stay a
#: glance rather than a menu — past about four it is a second site map, worse
#: organised than the one it sits next to.
MAX_PINS = 4

#: A cap on the counter cookie, not on anything a person sees. Cookies ride on
#: every request, so an unbounded tally of every page ever opened would be paid
#: for on requests that never read it. The least-opened entry drops out.
MAX_SEEN = 40

#: Long enough to cover an off-season. Nothing here holds authority, so this is
#: a convenience deadline rather than a security one.
MAX_AGE = 60 * 60 * 24 * 365

#: Long enough to name a page, short enough that it cannot be used to write a
#: sentence into the corner of every page.
MAX_LABEL = 24


def pinnable():
    """Every page a pin may point at: `{name, title, category, url}`.

    Derived from the staff site map rather than a list kept here, so a page
    decorated with `@page_meta` is pinnable the day it exists and a page that
    goes away stops being offered — the same bargain the map itself makes.

    Two exclusions fall out of that for free rather than being written down:
    a page with no `@page_meta` (POST endpoints, htmx fragments, the webhook)
    is not on the map, and a parameterised page reverses to nothing, so
    `raw_inventory_view` cannot be pinned while its picker can.

    Imported lazily because `views` imports this module. Same shape as
    `_site_map`'s own deferred import of the URLconf.
    """
    from .views import _site_map

    return [
        {
            "name": item["name"],
            "title": item["title"],
            "category": group["name"],
            "url": item["url"],
        }
        for group in _site_map()["grouped"]
        for item in group["items"]
        if item["url"] and item["name"] != "—"
    ]


def _clean_label(text):
    """A label somebody typed into a `?nav=` link, made safe to put in a pill.

    Length is the whole of it — Django escapes the rest on the way out. The
    cap is what stops the corner of every staff page becoming somewhere to
    write a sentence.
    """
    return " ".join(text.split())[:MAX_LABEL]


def parse(value, allowed=None):
    """`(pins, dropped)` from a `?nav=` string or the cookie's own copy.

    `pins` are `{name, label, url}` in the order given; `dropped` names the
    entries that went nowhere, which is what the navigation page reports
    rather than quietly handing back a shorter nav. A silently short answer
    here is the same failure the collection sheet's missing-dye list exists to
    prevent: you act on the list, and nothing says it was incomplete.

    Everything about it degrades rather than raises. A cookie outlives the
    facts in it — a route gets renamed, a page gets retired — and this is read
    on every staff page render, so the only safe failure is a shorter nav and
    a note somewhere a person will look.
    """
    if allowed is None:
        allowed = {p["name"]: p for p in pinnable()}

    pins, dropped, seen = [], [], set()
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, label = chunk.partition(":")
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        page = allowed.get(name)
        if page is None:
            dropped.append(name)
            continue
        if len(pins) >= MAX_PINS:
            dropped.append(name)
            continue
        pins.append({
            "name": name,
            # The page's own title carried alongside, so `encode` can tell a
            # label somebody typed from one that is just the title again —
            # and drop the second kind, which is what keeps a renamed page
            # following its new name instead of being frozen under the one it
            # had the day the link was made.
            "title": page["title"],
            "label": _clean_label(label) or page["title"],
            "url": page["url"],
        })
    return pins, dropped


def encode(pins):
    """The `?nav=` string for a set of pins — the link somebody sends.

    A custom label is carried; a label that is just the page's own title is
    not, so the common case stays short and a renamed page follows its own
    name rather than being frozen under the one it had the day the link was
    made.
    """
    parts = []
    for pin in pins:
        name = pin["name"]
        label = pin.get("label") or ""
        title = pin.get("title") or ""
        parts.append(f"{name}:{label}" if label and label != title else name)
    return ",".join(parts)


def pinned(request, allowed=None):
    """What the corner of this request's page should show."""
    pins, _ = parse(request.COOKIES.get(PIN_COOKIE, ""), allowed=allowed)
    return pins


# --- the counter -------------------------------------------------------
#
# Evidence for a person's decision, and nothing else reads it. It is not a
# score, there is no page that ranks anybody by it, and it never chooses what
# is in the nav — see the module docstring for why that last one matters.

def read_seen(request):
    """`{route name: times opened}`, defensively."""
    counts = {}
    for chunk in (request.COOKIES.get(SEEN_COOKIE, "") or "").split(","):
        name, _, raw = chunk.strip().partition(":")
        if not name:
            continue
        try:
            counts[name] = max(0, int(raw))
        except ValueError:
            continue
    return counts


def bump(counts, name):
    """One more visit to `name`, with the tally kept inside `MAX_SEEN`."""
    counts = dict(counts)
    counts[name] = counts.get(name, 0) + 1
    if len(counts) > MAX_SEEN:
        # Drop the least-opened, never the just-opened one — which is why the
        # name being counted is excluded from the candidates.
        coldest = min(
            (n for n in counts if n != name), key=lambda n: (counts[n], n)
        )
        del counts[coldest]
    return counts


def encode_seen(counts):
    return ",".join(f"{n}:{c}" for n, c in sorted(counts.items()))


def countable(request, response):
    """The route name to count for this request, or None.

    Four things have to be true, and each exclusion is a different way the
    tally would stop meaning "pages she opens":

    * a **GET that rendered** — a redirect to the login page is not a visit;
    * a **route the site map lists**, so POST endpoints and the webhook never
      appear in a list of pages to pin;
    * **not an htmx fragment**, because a swapped row is not somewhere you
      went. The recipe showcase alone would otherwise out-count every page in
      the app by the width of an afternoon's dye entry;
    * **somebody signed in**, since an anonymous request is a login redirect
      wearing the URL of the page it wanted.
    """
    if request.method != "GET" or response.status_code != 200:
        return None
    if request.headers.get("HX-Request"):
        return None
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return None

    match = getattr(request, "resolver_match", None)
    if match is None or not match.url_name:
        return None
    meta = getattr(match.func, "page_meta", None)
    if not meta or not meta.get("show_in_index", True):
        return None
    return match.url_name


class NavMiddleware:
    """Counts page opens onto a cookie. Writes nothing else, anywhere.

    A middleware rather than a call in each view because there are thirty-odd
    views and a counter that has to be remembered at each of them is a counter
    that is wrong — the rule the whole app runs on. It is also why this counts
    and does not *decide*: something that runs on every request should be the
    cheapest, dullest thing in the stack.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        name = countable(request, response)
        if name:
            response.set_cookie(
                SEEN_COOKIE,
                encode_seen(bump(read_seen(request), name)),
                max_age=MAX_AGE,
                samesite="Lax",
            )
        return response


def context(request):
    """Template context processor: the pills for `base_internal.html`.

    Read on every staff page render, so it must not raise on anything — a
    malformed cookie gives an empty nav and the site map link that was always
    there.
    """
    try:
        pins = pinned(request)
    except Exception:
        pins = []
    return {"nav_pins": pins}
