"""The faire calendar, which is a rule rather than a table somebody types.

One faire matters here — the nine-weekend run in the autumn — and its dates
are derivable, so they are derived. That is not a convenience: the old
spreadsheet-and-React version of this reporting died because every number in
it needed a person to type it every weekend, and a step that has to be
remembered to be correct is one this codebase already refuses to add.

The rule, stated plainly:

- **Labor Day is the first Monday in September.**
- **Week 1's Saturday is nine days before it.**
- **Nine weekends follow, Saturday and Sunday** — plus Labor Day Monday, which
  always falls in weekend 2. Nineteen trading days.

It reproduces every season on record exactly:

    2017  Labor Day Mon 4 Sep   week 1 Sat 26 Aug
    2018            Mon 3 Sep              Sat 25 Aug
    2019            Mon 2 Sep              Sat 24 Aug
    2021            Mon 6 Sep              Sat 28 Aug

Labor Day ranges over 1–7 September, so week 1 drifts across six calendar days
between seasons — Sat 23 Aug in 2025, Sat 29 Aug in 2026. **That drift is why
no comparison between seasons may key on a calendar date.** Everything indexes
on the weekend number, which is what this module exists to supply.

2020 has no faire. It is an absent season rather than a zero one, and the
distinction is structural: nothing generates a Faire for it, so nothing
downstream has to remember to exclude it.

**More than one faire is expected**, and the shape here is what makes that
cheap. A rule is a function from a year to a list of trading days, and
`RULES` is the registry of them — so a second rule-based event is one
function and one entry, while an event whose dates are announced rather than
derived uses `MANUAL` and has its days entered directly. Neither case
disturbs the other, and nothing downstream asks which kind it is reading.
"""

from __future__ import annotations

from datetime import date, timedelta

#: Weekends in a run.
WEEKENDS = 9

#: Days from Labor Day back to week 1's Saturday. Verified against four
#: seasons of recorded week-1 dates; see the module docstring.
WEEK_ONE_OFFSET = 9

#: The weekend that carries Labor Day Monday, 1-based. It is the only weekend
#: with three trading days, which makes every per-weekend total for it about a
#: third larger than its neighbours for a reason that has nothing to do with
#: trade. Anything reporting per-weekend figures needs a per-day companion.
LABOR_DAY_WEEKEND = 2

#: Trading days in a complete run: eight two-day weekends plus one three-day.
SEASON_DAYS = WEEKENDS * 2 + 1


def labor_day(year: int) -> date:
    """The first Monday in September of `year`."""
    first = date(year, 9, 1)
    return first + timedelta(days=(0 - first.weekday()) % 7)


def week_one_saturday(year: int) -> date:
    """The Saturday that opens the run."""
    return labor_day(year) - timedelta(days=WEEK_ONE_OFFSET)


def labor_day_run(year: int) -> list[tuple[date, int, bool]]:
    """Every trading day of `year`'s Labor Day run as `(date, weekend, is_labor_day)`.

    Weekends are 1-based. The Labor Day Monday is emitted in weekend 2, after
    its Saturday and Sunday, so the list is in date order throughout.
    """
    start = week_one_saturday(year)
    monday = labor_day(year)
    days = []
    for index in range(WEEKENDS):
        saturday = start + timedelta(days=7 * index)
        weekend = index + 1
        days.append((saturday, weekend, False))
        days.append((saturday + timedelta(days=1), weekend, False))
        if weekend == LABOR_DAY_WEEKEND:
            days.append((monday, weekend, True))
    return days


#: The rule this faire has always run on. A slug rather than a bare name so
#: it can key both the registry and the model field.
LABOR_DAY_RULE = "labor_day"

#: Days announced rather than derived — a faire whose dates are published each
#: year, or one whose pattern nobody has bothered to write down. Its days are
#: entered instead of generated, and `group_into_weekends` numbers them.
MANUAL = "manual"

#: Rule slug → a function from year to trading days. Adding a second
#: rule-based faire is one function and one line here.
RULES = {
    LABOR_DAY_RULE: labor_day_run,
}

RULE_CHOICES = [
    (LABOR_DAY_RULE, "Labor Day run — nine weekends, generated"),
    (MANUAL, "Dates entered by hand"),
]

#: The slug of the run this app was built around. Other faires name themselves.
DEFAULT_FAIRE_SLUG = "labor-day-run"
DEFAULT_FAIRE_NAME = "Labor Day Run"


def days_for(rule: str, year: int) -> list[tuple[date, int, bool]]:
    """Trading days for `rule` in `year`.

    Raises for `MANUAL`, which has no days to generate — that is the whole
    difference between the two kinds, and a rule that quietly returned an
    empty list would read as a faire that happened and traded nothing.
    """
    try:
        return RULES[rule](year)
    except KeyError:
        raise ValueError(
            f"{rule!r} generates no days. Its dates are entered rather than derived."
        )


def group_into_weekends(days: list[date]) -> list[tuple[date, int]]:
    """Number a hand-entered list of dates into weekends, 1-based.

    Consecutive days are one weekend and a gap starts the next, which gets
    Saturday–Sunday right and gets Saturday–Sunday–Monday right too — the
    Labor Day case, and the reason the test is a gap rather than a weekday.
    """
    numbered = []
    weekend = 0
    previous = None
    for when in sorted(days):
        if previous is None or (when - previous).days > 1:
            weekend += 1
        numbered.append((when, weekend))
        previous = when
    return numbered


def labor_day_season_for(day: date) -> int | None:
    """Which Labor Day run `day` belongs to, or None if it falls outside one.

    Answered by generating the candidate season and looking, rather than by
    comparing against a start and an end — the run has gaps in it (five days
    of every week are not trading days), and a range test would quietly place
    a Wednesday in September inside the season.

    Only the generated rule can be answered this way. A faire whose days were
    entered is answered by looking in `FaireDay`, which is what callers should
    do first; this is the fallback for a season nobody has generated yet.
    """
    for year in (day.year, day.year - 1):
        for when, _weekend, _monday in labor_day_run(year):
            if when == day:
                return year
    return None
