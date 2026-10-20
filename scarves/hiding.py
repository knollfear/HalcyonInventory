"""Which cards this browser has struck off this page, until morning.

`partials/hide_toggle.html` is the control: a checkbox and a label that
remove a `hideable` card. It used to forget on every reload, and that was
argued as the feature — the list is derived from the shelf on every request,
so a hidden row that survived would be a stored decision nobody made. In
practice the list is worked down over an evening with reloads in between (a
bath recorded, a sheet printed), and each one put back every row already
dealt with. So the decision is now stored, but **with a deadline**: it lasts
until 4 a.m. local time, and the next working day starts from the whole list.

Why 4 a.m. rather than midnight: midnight cuts an evening's planning session
in half at whatever point it happens to be, and the hide is for a session.
Whatever was hidden late is back by breakfast, which is the bound that keeps
the original worry small — a colorway that went short after it was hidden
stays out of sight for one evening at most, never a week.

**Per browser, per page, and nothing on the server.** The browser writes the
cookie (the script in `base_internal.html`, because the expiry is 4 a.m.
*local* time and only the browser knows its own clock); this module only
reads it, so the card arrives already hidden instead of flashing up and then
vanishing. The cookie's `path` is the page's own, so it rides only on
requests to that page, and its name carries the path too, so a page below
another one never reads its parent's list. Lose the cookie and the cost is
the list coming back, which is what a refresh used to do anyway.

Nothing here writes to the database or claims anything about anybody, so —
like `nav.py` — it is a plain cookie read defensively rather than a signed one.
"""
import re
from urllib.parse import unquote

#: Every page's cookie starts with this; the rest is the page's path.
PREFIX = "scarves_hide"

#: A bound on what is read, not on anything somebody can do. A cookie is
#: attacker-shaped input on every request to the page.
MAX_IDS = 500


def cookie_name(path):
    """The cookie that holds `path`'s hidden cards."""
    return PREFIX + re.sub(r"[^A-Za-z0-9]+", "_", path)


def hidden(request):
    """The `hide_id`s struck off this page, as strings."""
    raw = request.COOKIES.get(cookie_name(request.path), "")
    return set(unquote(raw).split()[:MAX_IDS])


def context(request):
    """Template context: the cookie's name, and what it currently hides."""
    return {"hide_cookie": cookie_name(request.path), "hidden_cards": hidden(request)}
