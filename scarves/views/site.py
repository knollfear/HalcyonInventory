"""The two site maps and the pinned-nav editor: the hub every other page hangs off."""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.shortcuts import render, redirect

from .. import nav
from ..sitemap import page_meta, site_map as _site_map


@page_meta(
    title="Site Map",
    description="This page — an auto-generated index of every scarves view.",
    category="Overview",
    show_in_index=False,
)
@login_required
def index(request):
    """The staff directory: every page, each badged with its exposure."""
    context = _site_map()
    context["public_map_url"] = reverse("public_index")
    return render(request, "scarves/index.html", context)


@page_meta(
    title="Navigation",
    description="Choose the handful of pages that sit in the corner of every "
                "staff page, beside the site map link. Shows how often each "
                "one has actually been opened on this device.",
    category="Overview",
    note="A ?nav= link sets the pins and can be sent to somebody.",
)
@login_required
def navigation(request):
    """Pick the pinned pages — and the one place that writes the pin cookie.

    **Two doors, one writer.** A person ticks boxes and saves; or a `?nav=`
    link arrives with the set already in it, which is how one gets sent to
    somebody. Both land here, so there is exactly one piece of code that
    decides what a pin is — and the link lands on a page that *says what it
    did* rather than silently rearranging the corner of somebody's screen.
    That second half is the crew cookie's argument: a pre-filled thing nothing
    mentions is unrecoverable by the person looking at it.

    **A link never fails quietly.** Names that reverse to nothing are dropped
    and listed, because a nav that came back one pill short with no
    explanation is the collection sheet's missing-dye problem — you act on the
    list and nothing says it was incomplete.

    **The visit counts are evidence, not a ranking.** They order nothing and
    choose nothing; they sit beside a checkbox so somebody can see what they
    actually use before deciding. See `nav.py` for why the obvious version —
    promote the top four automatically — is the `par` mistake wearing a
    different hat.
    """
    pages = nav.pinnable()
    allowed = {p["name"]: p for p in pages}

    # **Every write redirects, and that is the whole point of the page.**
    # The corner is rendered by a context processor reading `request.COOKIES`,
    # so a response that sets the cookie *and* renders shows the new pins in
    # the preview and the old ones in the corner — on the one page whose job
    # is to show you what the corner will look like. Post/Redirect/Get makes
    # the next request carry the new cookie, so both agree.
    if request.method == "POST":
        pins, dropped = nav.parse(
            ",".join(request.POST.getlist("pin")[: nav.MAX_PINS]), allowed=allowed
        )
        return _navigation_redirect(pins, "saved", dropped)

    if nav.PARAM in request.GET:
        wanted = request.GET[nav.PARAM]
        if wanted.strip() == nav.FORGET:
            return _navigation_redirect([], "forgotten", [])
        pins, dropped = nav.parse(wanted, allowed=allowed)
        return _navigation_redirect(pins, "from-link", dropped)

    seen = nav.read_seen(request)
    pins = nav.pinned(request, allowed=allowed)
    notice = request.GET.get("done", "")
    # Carried across the redirect rather than held in a session: what could
    # not be pinned belongs to the link that asked for it, and a nav that came
    # back a pill short with nothing to say why is the silence this reports.
    dropped = [
        name for name in request.GET.get("dropped", "").split(",") if name.strip()
    ]

    # Ordered by what somebody has actually opened, most first, because that
    # is the question the page is answering — "what do I use?" — and an
    # alphabetical list of thirty-odd pages answers nothing. The count prints
    # beside each row so the basis of the ordering is checkable by looking,
    # the same call `private/production-needed/` makes about its sold figure.
    pinned_names = {p["name"] for p in pins}
    rows = sorted(
        (
            {**page, "seen": seen.get(page["name"], 0),
             "on": page["name"] in pinned_names}
            for page in pages
        ),
        key=lambda r: (-r["seen"], r["category"], r["title"]),
    )

    return render(
        request,
        "scarves/navigation.html",
        {
            "rows": rows,
            "pins": pins,
            "dropped": dropped,
            "notice": notice,
            "max_pins": nav.MAX_PINS,
            "share": nav.encode(pins),
            "share_url": request.build_absolute_uri(
                f"{reverse('navigation')}?{nav.PARAM}={nav.encode(pins)}"
            ) if pins else "",
            "forget_url": f"{reverse('navigation')}?{nav.PARAM}={nav.FORGET}",
            "unopened": sum(1 for r in rows if not r["seen"]),
        },
    )


def _navigation_redirect(pins, done, dropped):
    """Write the pin cookie, then send the browser round again.

    A plain render would set the cookie on a page already built from the old
    one, so the preview and the corner would disagree — see `navigation`. The
    outcome rides in the query string because there is nowhere else for it to
    ride that a redirect survives, and because it makes the result of a link
    something you can look at rather than something that flashed past.
    """
    params = {"done": done}
    if dropped:
        params["dropped"] = ",".join(dropped)
    response = redirect(f"{reverse('navigation')}?{urlencode(params)}")
    if pins:
        response.set_cookie(
            nav.PIN_COOKIE, nav.encode(pins),
            max_age=nav.MAX_AGE, samesite="Lax",
        )
    else:
        response.delete_cookie(nav.PIN_COOKIE)
    return response


@page_meta(
    title="Public Site Map",
    description="This page — the directory of everything reachable without "
                "logging in.",
    category="Public",
    show_in_index=False,
)
def public_index(request):
    """The same directory, filtered to `public/`, and public itself.

    Deliberately not just `index` with a filter argument: this one has to be
    safe to hand to a stranger, so the filtering happens before anything
    reaches the template rather than inside it.
    """
    return render(request, "scarves/public_index.html", _site_map(bucket="public"))
