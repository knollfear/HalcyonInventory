"""Pay-week arithmetic and the weekly summary the timesheet page renders.

The pay week runs **Saturday to Friday**, which is not what any of Python's
date helpers assume, so every "which week is this in" question goes through
`week_start()` rather than being worked out at the call site. Getting it
wrong is invisible — the page still renders seven columns, they're just the
wrong seven, and the totals quietly belong to a week nobody is paying for.

Everything here counts one thing: hours running the booth during festival
days. There is no work-type dimension on purpose — deciding that a second
kind of work belongs in these totals is a payroll question, not a schema
one, and until it's answered a single unqualified total is the honest output.

Nothing here touches the database except `week_summary()`, and nothing here
writes to it at all. The page reports what people typed; it doesn't correct
it.
"""
from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal

#: Saturday. `date.weekday()` counts Monday as 0, so Saturday is 5.
WEEK_START_WEEKDAY = 5

#: Thresholds that earn a row a flag on the timesheet. None of these are
#: errors — a 13-hour day at a festival is entirely possible. They mark the
#: handful of rows worth a second look, so "check for reasonableness" is
#: something the page does rather than something you hold in your head.
LONG_DAY_HOURS = Decimal("12")
LONG_WEEK_HOURS = Decimal("55")
LATE_REPORT_DAYS = 7


def week_start(day: date) -> date:
    """The Saturday that begins the pay week containing `day`.

    A Saturday is its own week start; every other day walks backwards.
    """
    return day - timedelta(days=(day.weekday() - WEEK_START_WEEKDAY) % 7)


def week_days(start: date) -> list[date]:
    """The seven dates of the pay week beginning at `start`, Saturday first."""
    return [start + timedelta(days=offset) for offset in range(7)]


def week_end(start: date) -> date:
    """The Friday closing the pay week beginning at `start`."""
    return start + timedelta(days=6)


def parse_week(value: str | None, today: date) -> date:
    """Read a `?week=YYYY-MM-DD` parameter into a pay-week start.

    Any day inside a week is accepted and snapped to that week's Saturday, so
    the page can be linked with a date somebody actually cares about ("the
    Sunday we were rained out") and still land on the right sheet. Anything
    unreadable falls back to the current week rather than erroring — a bad
    query string should show you this week, not a stack trace.
    """
    if value:
        try:
            return week_start(datetime.strptime(value.strip(), "%Y-%m-%d").date())
        except ValueError:
            pass
    return week_start(today)


def week_summary(start: date) -> dict:
    """Everything the timesheet page shows for one pay week.

    One table, one total, one meaning: hours spent running the booth. This
    knows nothing about production help or any other kind of work, and the
    total is only correct as long as that stays true — anything else reported
    through the same form makes this number quietly ambiguous.

    Shaped for the template: a row per employee who reported anything, each
    carrying one cell per day in column order, so the table body is a plain
    nested loop and days nobody worked are still empty cells rather than
    missing ones.
    """
    from .models import TimeEntry

    days = week_days(start)
    entries = (
        TimeEntry.objects
        .filter(work_date__gte=start, work_date__lte=week_end(start))
        .select_related("employee")
        .order_by("employee__name", "work_date")
    )

    # OrderedDict rather than a plain dict keyed later: the queryset is
    # already sorted by name, so insertion order is the display order.
    by_employee = OrderedDict()
    for entry in entries:
        by_employee.setdefault(entry.employee, {})[entry.work_date] = entry

    rows = []
    for employee, entries_by_day in by_employee.items():
        cells = []
        for day in days:
            entry = entries_by_day.get(day)
            cells.append({
                "day": day,
                "entry": entry,
                "flags": _entry_flags(entry) if entry else [],
            })
        total = sum((e.hours for e in entries_by_day.values()), Decimal("0"))
        rows.append({
            "employee": employee,
            "cells": cells,
            "total": total,
            "flags": ["long week"] if total > LONG_WEEK_HOURS else [],
        })

    return {
        "start": start,
        "end": week_end(start),
        "days": days,
        "rows": rows,
        "total": sum((row["total"] for row in rows), Decimal("0")),
        "day_totals": [
            sum(
                (
                    cell["entry"].hours
                    for row in rows
                    for cell in row["cells"]
                    if cell["day"] == day and cell["entry"]
                ),
                Decimal("0"),
            )
            for day in days
        ],
    }


def _entry_flags(entry) -> list[str]:
    """Short reasons this entry is worth a second look, or an empty list."""
    flags = []
    if entry.hours > LONG_DAY_HOURS:
        flags.append("long day")
    if entry.was_revised:
        flags.append("revised")
    if entry.reported_late_by > LATE_REPORT_DAYS:
        flags.append(f"reported {entry.reported_late_by}d later")
    return flags
