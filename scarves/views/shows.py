"""The show planner: plan a show, and afterwards see whether it was worth it.

The arithmetic is in `scarves/shows.py`. These pages store what a person
types — the plan, the costs, what the costs really came to — and read the
takings off the sales ledger, so a show is a record somebody can come back
to rather than a sum done once on the back of an envelope.
"""
from io import StringIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import CommandError, call_command
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import shows
from ..forms import NewShowForm, ShowCostFormSet, ShowForm
from ..models import SaleLine, Show
from ..sitemap import page_meta


#: The plan form's fields, in the four groups the page lays them out in.
FIELD_GROUPS = [
    ["name", "location", "decision", "first_day", "last_day", "days_open",
     "round_trip_miles", "trips"],
    ["sales_per_day", "silk_share", "yarn_share", "card_fee_percent"],
]

#: The per-piece inputs as rows: what one sells for, its blank, its dyeing.
#: Bought-in stock has no dyeing, so its row has no third box.
PIECE_ROWS = [
    ("Silk", "silk_price", "silk_blank", "silk_dyeing"),
    ("Dyed yarn", "yarn_price", "yarn_blank", "yarn_dyeing"),
    ("Bought in", "bought_price", "bought_cost", None),
]


def _row(show):
    planned = shows.plan(show)
    return {
        "show": show,
        "plan": planned,
        "actual": shows.actual(show),
        "break_even": shows.break_even_per_day(show),
    }


@page_meta(
    title="Shows",
    description=(
        "Plan a show beyond the Labor Day run — what it might take, what "
        "replacing the pieces it sells costs, and what being there costs — "
        "and afterwards set it against what it really did."
    ),
    category="Shows",
)
@login_required
def shows_index(request):
    """Every show on file, newest first, and the form to start another."""
    if request.method == "POST":
        form = NewShowForm(request.POST)
        if form.is_valid():
            show = shows.make(**form.cleaned_data)
            return redirect("show_detail", show_id=show.pk)
    else:
        form = NewShowForm()
    return render(request, "scarves/shows_index.html", {
        "rows": [_row(s) for s in Show.objects.prefetch_related("costs")],
        "form": form,
        "basis": shows.reference(),
    })


@page_meta(
    title="Show",
    description="One show's plan and costs, and what it really took.",
    category="Shows",
    show_in_index=False,
)
@login_required
def show_detail(request, show_id):
    show = get_object_or_404(Show, pk=show_id)
    if request.method == "POST":
        form = ShowForm(request.POST, instance=show)
        costs = ShowCostFormSet(request.POST, instance=show)
        if form.is_valid() and costs.is_valid():
            with transaction.atomic():
                form.save()
                lines = costs.save(commit=False)
                for gone in costs.deleted_objects:
                    gone.delete()
                after = max((c.order for c in show.costs.all()), default=-1) + 1
                for line in lines:
                    if line.pk is None:
                        line.order, after = after, after + 1
                    line.save()
            messages.success(request, f"Saved {show.name}.")
            return redirect("show_detail", show_id=show.pk)
    else:
        form = ShowForm(instance=show)
        costs = ShowCostFormSet(instance=show)

    today = timezone.localdate()
    on_file = SaleLine.objects.filter(
        sold_at__date__range=(show.first_day, show.last_day)
    ).count()
    context = _row(show)
    planned, actual = context["plan"], context["actual"]
    context.update({
        "form": form,
        "fields": [[form[name] for name in names] for names in FIELD_GROUPS],
        "pieces": [
            (label, *(form[name] if name else None for name in names))
            for label, *names in PIECE_ROWS
        ],
        "costs": costs,
        # Plan and actual are read from the same cost lines in the same
        # order, so they pair up by position.
        "groups": list(zip(planned.groups, actual.groups if actual else [None] * len(planned.groups))),
        "cost_rows": list(zip(planned.costs, actual.costs if actual else [None] * len(planned.costs))),
        "keep_cents": round(shows.keep_share(show) * 100),
        "started": show.first_day <= today,
        "on_file": on_file,
    })
    return render(request, "scarves/show_detail.html", context)


@require_POST
@login_required
def show_pull_sales(request, show_id):
    """Fetch the show's dates from Square into the ledger.

    The same import the Labor Day run is refreshed with, over the show's
    dates. Re-running it is harmless — it matches orders already on file —
    so the button is also the refresh after a late weekend.
    """
    show = get_object_or_404(Show, pk=show_id)
    today = timezone.localdate()
    if show.first_day > today:
        messages.error(request, "The show hasn't started, so there is nothing to pull.")
        return redirect("show_detail", show_id=show.pk)
    end = min(show.last_day, today)
    window = (show.first_day, end)
    before = SaleLine.objects.filter(sold_at__date__range=window).count()
    try:
        call_command(
            "import_square_orders",
            "--from", f"{show.first_day:%Y-%m-%d}", "--to", f"{end:%Y-%m-%d}",
            stdout=StringIO(),
        )
    except CommandError as exc:
        messages.error(request, f"Square didn't answer: {exc}")
        return redirect("show_detail", show_id=show.pk)
    added = SaleLine.objects.filter(sold_at__date__range=window).count() - before
    if added:
        messages.success(request, f"Pulled {added} sale line{'' if added == 1 else 's'} from Square.")
    else:
        messages.success(request, "Square has nothing new for those dates.")
    return redirect("show_detail", show_id=show.pk)
