"""Helpers more than one view module reaches for."""
from datetime import datetime, timezone as dt_timezone


def _is_htmx(request):
    """Whether this came from a swap rather than a navigation."""
    return request.headers.get("HX-Request") == "true"
#: Older than any real timestamp, so a board nobody has fully checked sorts
#: as the one that has gone longest without one — which is true.
_NEVER = datetime.min.replace(tzinfo=dt_timezone.utc)
