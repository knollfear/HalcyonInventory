"""Daily weather for the faire days, from Open-Meteo's historical archive.

Free, no key, no account, and it reaches back to 1940 — which matters because
the point is to fill in seasons that happened years ago. The reading is
fetched once and stored; see `DayWeather` for why nothing does this at render
time.

**Two things about the archive are worth knowing before trusting a gap.**

- It lags real time by roughly five days, so the weekend just gone is very
  often not there yet. That is a "come back later", not a missing day, and
  the command says which it is rather than writing a blank row.
- Cloud and humidity are hourly variables, so they are averaged over
  **opening hours only** (10:00–19:00 local). Fog at four in the morning is
  not weather anybody stood in. That makes cloud cover here read lower than
  the whole-day figure the old React site carried — same sky, different
  question.

Temperatures come back in Fahrenheit and rain in inches because those are the
units the numbers get discussed in.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

#: Local hours the stall is open, inclusive. Cloud and humidity are averaged
#: across these and nothing else.
OPEN_HOUR = 10
CLOSE_HOUR = 19

DAILY_FIELDS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
]
HOURLY_FIELDS = ["cloud_cover", "relative_humidity_2m"]

SOURCE = "open-meteo"


class WeatherUnavailable(Exception):
    """The archive could not be reached or did not answer in a shape we know."""


def fetch(latitude, longitude, start: date, end: date, timezone_name: str,
          opener=None) -> dict[date, dict]:
    """Readings per day between `start` and `end`, inclusive.

    Days the archive has no answer for are simply absent from the result —
    never present with nulls in them. The caller decides what to say about a
    missing day, and it needs to be able to tell one from a day that was
    genuinely calm and dry.
    """
    query = urllib.parse.urlencode({
        "latitude": f"{latitude}",
        "longitude": f"{longitude}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_FIELDS),
        "hourly": ",".join(HOURLY_FIELDS),
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": timezone_name,
    })
    url = f"{ARCHIVE_URL}?{query}"
    opener = opener or _open
    try:
        payload = json.loads(opener(url))
    except urllib.error.URLError as exc:
        raise WeatherUnavailable(f"could not reach the weather archive: {exc}")
    except json.JSONDecodeError as exc:
        raise WeatherUnavailable(f"the weather archive sent something unreadable: {exc}")

    if payload.get("error"):
        raise WeatherUnavailable(payload.get("reason") or "the weather archive refused the request")

    return _readings(payload)


def _open(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


def _readings(payload):
    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    hourly = _hourly_means(payload.get("hourly") or {})

    out = {}
    for index, stamp in enumerate(days):
        when = date.fromisoformat(stamp)
        values = {
            "high_f": _at(daily, "temperature_2m_max", index),
            "low_f": _at(daily, "temperature_2m_min", index),
            "mean_f": _at(daily, "temperature_2m_mean", index),
            "precipitation_in": _at(daily, "precipitation_sum", index),
        }
        # Every daily field null is the archive saying "not yet" rather than
        # "a day with no weather" — those days stay out of the result so the
        # caller can report them as pending.
        if all(value is None for value in values.values()):
            continue
        values.update(hourly.get(when, {"cloud_pct": None, "humidity_pct": None}))
        out[when] = values
    return out


def _at(block, key, index):
    values = block.get(key) or []
    if index >= len(values) or values[index] is None:
        return None
    return Decimal(str(values[index]))


def _hourly_means(hourly):
    """Average cloud and humidity per day over opening hours."""
    stamps = hourly.get("time") or []
    cloud = hourly.get("cloud_cover") or []
    humidity = hourly.get("relative_humidity_2m") or []

    buckets = {}
    for index, stamp in enumerate(stamps):
        try:
            when = date.fromisoformat(stamp[:10])
            hour = int(stamp[11:13])
        except (ValueError, IndexError):
            continue
        if not OPEN_HOUR <= hour <= CLOSE_HOUR:
            continue
        bucket = buckets.setdefault(when, {"cloud": [], "humidity": []})
        if index < len(cloud) and cloud[index] is not None:
            bucket["cloud"].append(Decimal(str(cloud[index])))
        if index < len(humidity) and humidity[index] is not None:
            bucket["humidity"].append(Decimal(str(humidity[index])))

    return {
        when: {
            "cloud_pct": _mean(values["cloud"]),
            "humidity_pct": _mean(values["humidity"]),
        }
        for when, values in buckets.items()
    }


def _mean(values):
    if not values:
        return None
    return (sum(values) / len(values)).quantize(Decimal("0.1"))
