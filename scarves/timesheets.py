"""Pay-week arithmetic and the weekly summary the timesheet page renders.

The pay week runs **Wednesday to Tuesday**, which is not what any of Python's
date helpers assume, so every "which week is this in" question goes through
`week_start()` rather than being worked out at the call site. Getting it
wrong is invisible — the page still renders seven columns, they're just the
wrong seven, and the totals quietly belong to a week nobody is paying for.

The boundary is set by when the money moves: pay goes out through Square on
Wednesday, so a week has to be *closed* by then. Ending it Tuesday means
Tuesday's hours are the last ones in and Wednesday morning's payment covers
a week that is already complete, with nothing arriving after the run. It
also still keeps a faire weekend whole — Saturday, Sunday and a Labor Day
Monday all fall inside one Wednesday-to-Tuesday week.

Hours come in two kinds — booth and dyeing — and **both are paid at the same
hourly rate**, which is what keeps one grand total honest. The kind is a
reporting split, not a pricing one: nothing here multiplies it by anything,
and the number at the bottom of the page is still the number payroll wants.

The split exists so the sheet can be *read*. "Thirty hours" is a different
week depending on whether it was all booth or half of it was dyeing, and the
person signing the week off is the one who knows which of those is right.
If the two rates ever stop matching, the grand total is the first thing that
has to go — see `TimeEntry`.

Nothing here touches the database except `week_summary()`, and nothing here
writes to it at all. The page reports what people typed; it doesn't correct
it.
"""
from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal

#: Wednesday. `date.weekday()` counts Monday as 0, so Wednesday is 2.
WEEK_START_WEEKDAY = 2

#: Thresholds that earn a row a flag on the timesheet. None of these are
#: errors — a 13-hour day at a festival is entirely possible. They mark the
#: handful of rows worth a second look, so "check for reasonableness" is
#: something the page does rather than something you hold in your head.
LONG_DAY_HOURS = Decimal("12")
LONG_WEEK_HOURS = Decimal("55")
LATE_REPORT_DAYS = 7


def week_start(day: date) -> date:
    """The Wednesday that begins the pay week containing `day`.

    A Wednesday is its own week start; every other day walks backwards.
    """
    return day - timedelta(days=(day.weekday() - WEEK_START_WEEKDAY) % 7)


def week_days(start: date) -> list[date]:
    """The seven dates of the pay week beginning at `start`, Wednesday first."""
    return [start + timedelta(days=offset) for offset in range(7)]


def week_end(start: date) -> date:
    """The Tuesday closing the pay week beginning at `start`, and the last
    day paid by the Square run the next morning."""
    return start + timedelta(days=6)


def parse_week(value: str | None, today: date) -> date:
    """Read a `?week=YYYY-MM-DD` parameter into a pay-week start.

    Any day inside a week is accepted and snapped to that week's Wednesday, so
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

    One table, one grand total, and a split inside each person: a sub-row per
    kind of work they reported. The grand total spans both kinds because both
    are paid the same hourly rate — see the module docstring for what has to
    change if that stops being true.

    Shaped for the template: a row per employee who reported anything,
    carrying a sub-row per kind, each with one cell per day in column order,
    so the table body is a plain nested loop and days nobody worked are still
    empty cells rather than missing ones. An employee who only ever reports
    one kind still gets one sub-row rather than a different layout — the
    label is the point of the split, and a table that changes shape depending
    on the data is one nobody can scan.

    A person's sub-rows appear in `KIND_CHOICES` order, not in the order they
    happened to report them, so Booth is above Dyeing for everybody and the
    column of labels reads straight down.
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
        by_employee.setdefault(entry.employee, {}).setdefault(
            entry.kind, {}
        )[entry.work_date] = entry

    rows = []
    for employee, by_kind in by_employee.items():
        # A person's whole day, across kinds. "Long day" is a question about
        # the person, not about one row — split into a morning of dyeing and
        # an afternoon at the booth, a fourteen-hour day is two unremarkable
        # entries, and flagging each in isolation would never mention it.
        day_totals_for_person = {
            day: sum(
                (
                    entries_by_day[day].hours
                    for entries_by_day in by_kind.values()
                    if day in entries_by_day
                ),
                Decimal("0"),
            )
            for day in days
        }

        kinds = []
        for kind, label in TimeEntry.KIND_CHOICES:
            entries_by_day = by_kind.get(kind)
            if not entries_by_day:
                continue
            cells = []
            for day in days:
                entry = entries_by_day.get(day)
                cells.append({
                    "day": day,
                    "entry": entry,
                    "flags": (
                        _entry_flags(entry, day_totals_for_person[day])
                        if entry else []
                    ),
                })
            kinds.append({
                "kind": kind,
                "label": label,
                "cells": cells,
                "total": sum(
                    (e.hours for e in entries_by_day.values()), Decimal("0")
                ),
            })

        total = sum((k["total"] for k in kinds), Decimal("0"))
        rows.append({
            "employee": employee,
            "kinds": kinds,
            "total": total,
            "flags": ["long week"] if total > LONG_WEEK_HOURS else [],
        })

    all_cells = [
        cell for row in rows for kind in row["kinds"] for cell in kind["cells"]
    ]

    return {
        "start": start,
        "end": week_end(start),
        "days": days,
        "rows": rows,
        "total": sum((row["total"] for row in rows), Decimal("0")),
        "kind_totals": [
            {
                "kind": kind,
                "label": label,
                "total": sum(
                    (
                        k["total"]
                        for row in rows
                        for k in row["kinds"]
                        if k["kind"] == kind
                    ),
                    Decimal("0"),
                ),
            }
            for kind, label in TimeEntry.KIND_CHOICES
        ],
        "day_totals": [
            sum(
                (
                    cell["entry"].hours
                    for cell in all_cells
                    if cell["day"] == day and cell["entry"]
                ),
                Decimal("0"),
            )
            for day in days
        ],
    }


def _entry_flags(entry, day_total: Decimal) -> list[str]:
    """Short reasons this entry is worth a second look, or an empty list.

    `day_total` is everything this person reported on this day, across kinds,
    so a long day gets flagged whether it arrived as one entry or two. Every
    entry on such a day carries the flag: the point on the page is the day,
    and marking only the larger half invites reading it as the larger half's
    problem.
    """
    flags = []
    if day_total > LONG_DAY_HOURS:
        flags.append("long day")
    if entry.was_revised:
        flags.append("revised")
    if entry.reported_late_by > LATE_REPORT_DAYS:
        flags.append(f"reported {entry.reported_late_by}d later")
    return flags
