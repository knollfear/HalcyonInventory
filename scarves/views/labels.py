"""Barcode labels: the run picker, the PDF and the calibration sheet."""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.contrib import messages
from django.shortcuts import render, redirect

from ..sitemap import page_meta
from ..models import LabelStock
from .. import labels
from ..forms import LabelRunForm


# ---------------------------------------------------------------------------
# Barcode labels: pick a dataset, see exactly what it will use up, print.
#
# The picker and the preview are one page on purpose. Labels cost a sheet at a
# time and a run can't be un-printed, so every number that matters — how many
# stickers, how many sheets, which rows, what to write on the sheet afterwards
# — is on screen before the PDF is ever built.
# ---------------------------------------------------------------------------


def _label_stock_from(form):
    """The chosen stock, with any per-print offset override applied in memory.

    The offsets live on the model as a printer's saved registration, which
    assumes you print on a printer you own. Printing at a shop inverts that: a
    different machine every time, no chance to calibrate first, and no
    computer to hand when the first sheet comes out 2mm high. So the override
    rides in the query string — adjust it on a phone, re-download, print
    again, without going near the admin. Never saved: a correction for one
    store's machine on one day is not a property of the paper.
    """
    stock = form.cleaned_data["stock"]
    for field in ("x_offset_mm", "y_offset_mm"):
        override = form.cleaned_data.get(field)
        if override is not None:
            setattr(stock, field, override)
    return stock


def _label_run_from(form):
    """Build the run described by a valid LabelRunForm."""
    data = form.cleaned_data
    style = data.get("style") or labels.BARCODE
    if data["dataset"] == LabelRunForm.ITEMS:
        return labels.specific_items(data["items"], style=style)
    if data["dataset"] == LabelRunForm.SINCE:
        # Same two narrowing controls as the on-hand run, because a weekly
        # run is usually one pile of one kind of thing — a yarn session, or
        # the silk off one weekend — and printing the other half's stickers
        # is a sheet somebody has to sort through afterwards.
        return labels.produced_since(
            data["since"],
            extra=data["extra"],
            style=style,
            category=data.get("category"),
            raw_products=data.get("raw_products"),
        )
    return labels.inventory_run(
        extra=data["extra"],
        category=data.get("category"),
        raw_products=data.get("raw_products"),
        include_zero=data.get("include_zero", False),
        style=style,
    )


@page_meta(
    title="Barcode Labels",
    description="Print Code128 stickers for stock you're adding to inventory "
                "— everything produced since a date, or everything on hand. "
                "Shows the sheet layout and which rows it uses before you "
                "print.",
    category="Reference Sheets",
)
@login_required
def label_index(request):
    """Picker and preview in one. Submits to itself by GET, so a run is a URL."""
    submitted = bool(request.GET)
    form = LabelRunForm(request.GET or None)

    context = {"form": form, "submitted": submitted}

    if submitted and form.is_valid():
        stock = _label_stock_from(form)
        start_at = form.cleaned_data["start_at"] - 1  # UI is 1-indexed
        run = _label_run_from(form)
        sequence = run.sequence(stock.columns)
        blanks = {i for i, p in enumerate(sequence) if p is None}

        context.update({
            "run": run,
            "stock": stock,
            "padding": len(blanks),
            "plan": labels.plan_sheets(stock, len(sequence), start_at, blanks),
            "density_problems": labels.density_problems(run, stock),
            "pdf_url": f"{reverse('label_pdf')}?{request.GET.urlencode()}",
            "calibration_url": (
                f"{reverse('label_calibration_pdf')}?stock={stock.pk}"
            ),
        })

    return render(request, "scarves/label_index.html", context)


@page_meta(
    title="Barcode Labels PDF",
    description="Renders the label sheet described by the query string.",
    category="Reference Sheets",
    note="Returns a PDF. Reached from the Barcode Labels page.",
    show_in_index=False,
)
@login_required
def label_pdf(request):
    form = LabelRunForm(request.GET or None)
    if not form.is_valid():
        messages.error(request, "That label run isn't valid — check the form.")
        return redirect("label_index")

    stock = _label_stock_from(form)
    start_at = form.cleaned_data["start_at"] - 1
    run = _label_run_from(form)

    if not run.rows:
        messages.warning(request, "Nothing to print for those settings.")
        return redirect(f"{reverse('label_index')}?{request.GET.urlencode()}")

    # Refuse rather than hand over a sheet of stickers no scanner will read.
    # The failure is otherwise silent until someone is at the till with a
    # queue behind them.
    problems = labels.density_problems(run, stock)
    if problems:
        listed = ", ".join(f"{sku} ({mil:.1f} mil)" for sku, mil in problems[:5])
        messages.error(
            request,
            f"{len(problems)} SKU(s) are too long for {stock.name} and would "
            f"print bars under {labels.MIN_MODULE_MIL} mil: {listed}. Shorten "
            f"the SKU or use wider stock.",
        )
        return redirect(f"{reverse('label_index')}?{request.GET.urlencode()}")

    pdf = labels.render_run(run, stock, start_at)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="labels-{timezone.localdate():%Y%m%d}.pdf"'
    )
    return response


@page_meta(
    title="Label Calibration Sheet",
    description="Outlines at every label position and a one-inch ruler. Print "
                "on plain paper and hold it against a label sheet to check "
                "the geometry and the printer's registration.",
    category="Reference Sheets",
    note="Returns a PDF. Reached from the Barcode Labels page.",
    show_in_index=False,
)
@login_required
def label_calibration_pdf(request):
    stock = get_object_or_404(LabelStock, pk=request.GET.get("stock"))
    response = HttpResponse(labels.render_calibration(stock), content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="label-calibration.pdf"'
    return response
