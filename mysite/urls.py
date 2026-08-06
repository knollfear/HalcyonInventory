"""mysite URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import Resolver404, include, path, re_path, resolve, reverse
from django.views.generic import RedirectView

urlpatterns = [
    path('accounts/', admin.site.urls),
    path("scarves/", include("scarves.urls")),

    # The public site map is the front door, so the bare root goes there.
    path("", RedirectView.as_view(pattern_name="public_index", permanent=False)),
]

# Serve user-uploaded media from the local filesystem in dev. In production
# USE_S3 is on, so media is served by the bucket (via presigned URLs) and this
# block is skipped.
if settings.DEBUG and not settings.USE_S3:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

def lost_and_found(request):
    """Anything unrecognised lands on the public map rather than a 404 — it's
    the de facto home page, so a stale link or a typo should still arrive
    somewhere useful.

    **Except a missing trailing slash**, which gets sent to the slashed route
    instead. That is nominally `APPEND_SLASH`'s job, but `APPEND_SLASH` lives
    in `CommonMiddleware` and only fires when the URL resolver *fails* — and
    the catch-all below matches everything, so resolution never fails and the
    setting was silently dead. `/scarves/private/colors` went to the home page,
    which reads as a working page, and the setting that was supposed to prevent
    exactly that had no way to run.

    So the append is done here, where it can actually happen. The check is
    "does the slashed path resolve to something that isn't *this* view", since
    asking the resolver plainly would always say yes.

    Only GET and HEAD are redirected. A slashless POST falls through to the map
    rather than being bounced, because a redirect drops the body — the same
    reason Django's own APPEND_SLASH leaves POSTs alone.

    Two consequences of the fallback are worth knowing, because they're the
    cost of having it:
      * a genuinely broken link no longer announces itself; it just redirects,
        so a mistyped {% url %} looks like a working page until someone notices
        they're on the home page;
      * search engines see a soft 404 for every dead URL.
    """
    if request.method in ("GET", "HEAD") and not request.path_info.endswith("/"):
        try:
            match = resolve(request.path_info + "/")
        except Resolver404:
            match = None
        if match is not None and match.func is not lost_and_found:
            query = request.META.get("QUERY_STRING", "")
            return HttpResponseRedirect(
                request.path + "/" + (f"?{query}" if query else "")
            )
    return HttpResponseRedirect(reverse("public_index"))


# static/ and media/ are excluded so a missing asset fails as an asset rather
# than resolving to a page of HTML, which is far more confusing to debug.
#
# This is a URL pattern rather than handler404 on purpose: handler404 is
# bypassed when DEBUG is on, and dev behaving differently from production is
# exactly what this kind of rule shouldn't do.
urlpatterns += [
    re_path(r"^(?!static/|media/).*$", lost_and_found),
]

