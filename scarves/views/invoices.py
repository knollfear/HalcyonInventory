"""Supplier invoices: read one, book it, receive it, write it off."""
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages
from django.shortcuts import render, redirect

from ..sitemap import page_meta
from ..models import RawProduct, Supplier, SupplierInvoice, SupplierInvoiceLine
from .. import invoiceread


# --- Invoices -------------------------------------------------------------
#
# The one place in this app where a delivery quantity and a cost share a Save
# button. Everywhere else they are separate modes on purpose (see
# `docs/claude/stock.md`), because a number typed beside the wrong button is
# written nowhere and says nothing about it. What makes the pair safe here is
# the order number: a bill is a document with a name, and a document that has
# already been booked can say so.


@page_meta(
    title="Invoices",
    description="Send in a supplier's invoice — PDF or a photo of it — and it "
                "comes back as a list of blanks, quantities and unit costs to "
                "confirm. Booking one sets what each blank costs to replace "
                "and puts the delivery on the shelf.",
    category="Inventory",
    note="Needs CLAUDE_API_KEY to read a document; the form works without it.",
)
@login_required
@require_http_methods(["GET", "POST"])
def invoice_index(request):
    """The picker, and the door a document comes in through.

    **Uploading reads, and nothing else.** The reading lands as a draft on
    its own page with every figure in a box, because a reading is advice:
    `colorbands` fills the form in and somebody confirms, the sheet scanner
    pre-ticks and somebody submits, and an invoice is the same shape. The
    thing that makes it advice rather than a decision is that the invoice's
    own wording for each line is printed beside the match.

    A draft that is never confirmed has moved nothing, and the list says so.
    """
    if request.method == "POST":
        upload = request.FILES.get("invoice")
        pasted = (request.POST.get("pasted") or "").strip()
        if not upload and not pasted:
            messages.error(
                request,
                "Send a PDF or a photo, or paste the invoice text in — either "
                "one is enough.",
            )
            return redirect("invoice_index")
        draft = (
            _read_into_draft(request, upload) if upload
            else _read_pasted_into_draft(request, pasted)
        )
        return redirect(draft.get_absolute_url())

    invoices = list(
        SupplierInvoice.objects.select_related("supplier")
        .annotate(line_count=Count("lines"))
        .order_by("-created_at")[:60]
    )
    return render(request, "scarves/invoice_index.html", {
        "drafts": [i for i in invoices if i.is_draft],
        # Confirmed and costed, still in a van. The state that did not exist
        # before, and the one somebody actually wants a list of.
        "on_order": [i for i in invoices if i.is_on_order],
        "booked": [i for i in invoices if not i.is_draft and i.received_on],
        # Ended without arriving. Listed rather than hidden: an order that was
        # given up on is a thing that happened, and one you cannot find is one
        # nobody chases with the supplier.
        "written_off": [i for i in invoices if i.is_written_off],
        "can_read": bool(getattr(settings, "CLAUDE_API_KEY", "")),
    })


def _read_into_draft(request, upload):
    """Read the upload and park it as a draft, whatever came back.

    A failed reading still makes a draft: the document is already in hand,
    the page it belongs on is the one with the rows on it, and an error with
    nowhere to sit is an error somebody has to be told twice. The note says
    what happened and the form below it is the same form.
    """
    data = upload.read()
    reading = invoiceread.read(
        data,
        content_type=getattr(upload, "content_type", "") or "",
        filename=upload.name or "",
    )

    invoice = _draft_from_reading(reading)
    invoice.document.save(
        f"{uuid.uuid4().hex}{_upload_suffix(upload.name)}",
        ContentFile(data),
        save=True,
    )
    _park_lines(invoice, reading)

    if reading.error:
        messages.error(request, reading.error)
    return invoice


def _draft_from_reading(reading):
    return SupplierInvoice.objects.create(
        order_number=SupplierInvoice.normalise_number(reading.order_number),
        supplier=_supplier_named(reading.supplier_name),
        ordered_on=reading.ordered_on,
        read_note=reading.note,
    )


def _park_lines(invoice, reading):
    """The reading's lines, parked as the draft's own.

    The page is the draft rather than a session, so a refresh, a phone dying
    or somebody coming back to it after supper all leave the work where it
    was.
    """
    for line in reading.lines:
        SupplierInvoiceLine.objects.create(
            invoice=invoice,
            raw_product_id=line.raw_product_id,
            description=line.description,
            quantity=line.quantity or 0,
            line_total=line.line_total,
            unit_cost=line.unit_cost or Decimal("0"),
        )


def _read_pasted_into_draft(request, text):
    """The same draft, from text somebody copied out of an email.

    **Most of these orders are an email before they are ever a document.**
    Getting a screenshot of one off a phone and into a file picker is more
    work than selecting the message and pressing copy — and text is the
    easiest thing there is to read: no photograph, no scan, no column that
    wrapped in the wrong place.

    What gets pasted is whatever was copied, headers and signature and all.
    Asking somebody to tidy it first would be asking them to do the reading.
    """
    reading = invoiceread.read_text(text)
    invoice = _draft_from_reading(reading)
    invoice.pasted_text = text
    invoice.save(update_fields=["pasted_text"])
    _park_lines(invoice, reading)
    if reading.error:
        messages.error(request, reading.error)
    return invoice


def _upload_suffix(name):
    name = (name or "").lower()
    for suffix in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"):
        if name.endswith(suffix):
            return suffix
    return ".bin"


def _supplier_named(name):
    """Match a read supplier name to one already on file, or nobody.

    Deliberately weak: an exact-ish name or nothing. Creating a supplier from
    a string off a document would fork `Wool2dye4` into three spellings the
    first time an invoice header wrapped, which is the colorway-shorthand
    mistake with a different noun.
    """
    cleaned = " ".join((name or "").split())
    if not cleaned:
        return None
    for supplier in Supplier.objects.all():
        if supplier.name.lower() == cleaned.lower():
            return supplier
        if supplier.name.lower() in cleaned.lower():
            return supplier
    return None


@page_meta(
    title="Invoice",
    description="One invoice: the lines it bought, what each cost, and what "
                "booking it moved.",
    category="Inventory",
    show_in_index=False,
)
@login_required
def invoice_detail(request, invoice_id):
    """A draft under review, or the record of one that was booked.

    Two states, one page, because they are the same document either side of
    a decision. The booked side prints what each line changed — the price
    before and the price now — so a cost on a blank can be traced back to the
    piece of paper it came off. A number whose basis is hidden reads as a
    fact, and this app has been bitten by that once already.
    """
    invoice = get_object_or_404(
        SupplierInvoice.objects.select_related("supplier"), pk=invoice_id
    )
    lines = list(invoice.lines.select_related("raw_product__category"))

    if not invoice.is_draft:
        return render(request, "scarves/invoice_detail.html", {
            "invoice": invoice,
            "lines": lines,
            "total": sum((l.line_total or Decimal("0")) for l in lines),
            "today": timezone.localdate(),
            # State in the query string rather than a session flag, so the
            # adjusted form is a URL somebody can land on directly — and so
            # the htmx swap and a plain page load agree about what is showing.
            "adjust": request.GET.get("adjust") == "1",
        })

    return render(request, "scarves/invoice_review.html", _review_context(invoice, lines))


def _review_context(invoice, lines, typed=None, errors=None):
    """The draft as a form: one row per line, plus room for the ones it missed.

    **Removing a row is unticking it**, not a delete button. A row that
    disappears is indistinguishable from a click that never arrived, and this
    is a list somebody is checking against a piece of paper in their other
    hand — the wrong row vanishing silently is the failure that costs the
    whole pass. Unticked rows stay on screen, wording and all, and simply
    don't get booked.

    Adding one is five spare rows at the bottom. A reader that misses a line
    must not be the end of the job: the page has to be able to say something
    the document said and the reading didn't.
    """
    typed = typed or {}
    products = list(
        RawProduct.objects.active()
        .select_related("category").order_by("category__name", "name")
    )

    def row(key, line=None, spare=False):
        entered = typed.get(key)
        if entered is not None:
            product = entered["product"]
            return {
                "key": key,
                "description": entered["description"],
                "product_id": int(product) if product.isdigit() else None,
                "quantity": entered["quantity"],
                "unit_cost": entered["unit_cost"],
                "book": entered["book"],
                "current": None,
                "spare": spare, "remembered": False,
            }
        if line is None:
            return {"key": key, "description": "", "product_id": None,
                    "quantity": "", "unit_cost": "", "book": False,
                    "current": None, "spare": spare, "remembered": False}
        return {
            "key": key,
            "description": line.description,
            "product_id": line.raw_product_id,
            "quantity": line.quantity or "",
            "unit_cost": line.unit_cost if line.unit_cost else "",
            # Pre-ticked only when the reader found a blank for it. An
            # unmatched line ticked by default would be a row that fails to
            # save with an error somebody has to go and read; unticked, it is
            # a row asking a question.
            "book": bool(line.raw_product_id),
            "current": line.raw_product,
            "spare": spare,
            # What the blank would be called, with the pack clause off. The
            # link below carries it; the wording it carries alongside stays
            # exact, because that one is a key.
            "suggested_name": invoiceread.product_name(line.description),
            # Whether this match is a past confirmation rather than a
            # reading. Printed, because a matched row whose basis is
            # invisible is a row nobody checks — and these two deserve
            # different amounts of checking.
            "remembered": bool(
                line.raw_product_id
                and line.raw_product.invoice_description
                and invoiceread.normalise_description(line.description)
                == invoiceread.normalise_description(
                    line.raw_product.invoice_description
                )
            ),
        }

    rows = [row(str(line.pk), line) for line in lines]
    spare = [f"new{n}" for n in range(1, 6)]
    rows += [row(key, spare=True) for key in spare]

    return {
        "invoice": invoice,
        "rows": rows,
        "products": products,
        "suppliers": Supplier.objects.active().order_by("name"),
        "already": _already_booked(invoice),
        "errors": errors or [],
        "read_lines": len(lines),
    }


def _already_booked(invoice):
    """The booked invoice carrying this order number, if there is one.

    This is the whole safety story. `number_on_hand` is a delta on this page,
    and a delta is only ever as good as the certainty that it hasn't been
    applied before — raw stock is the one pile nothing recounts on its own,
    so a double-booked delivery sits there until somebody walks the shelf
    with a clipboard, which for undyed yarn may be never.
    """
    number = SupplierInvoice.normalise_number(invoice.order_number)
    if not number:
        return None
    return (
        SupplierInvoice.objects.filter(order_number=number, booked_at__isnull=False)
        .exclude(pk=invoice.pk)
        .select_related("supplier")
        .first()
    )


@require_POST
@login_required
def invoice_book(request, invoice_id):
    """Confirm a draft: set what each blank costs, and put the delivery on.

    **Two things happen per line and they are different kinds of claim.**
    The unit cost is written flat onto the blank — it is the replacement
    cost, what one would cost to buy again today, so the old number is simply
    gone and no history of it is kept anywhere but on the line itself. The
    quantity is *added*, because a delivery is a change rather than a
    measurement.

    That delta is the dangerous half, and the order number is what guards it:
    an order already booked stops this dead and names the invoice it clashes
    with. It can be overridden with a tick, because two suppliers can pick
    the same number and a page that simply refuses gets worked around by
    typing a `-2` on the end — but it cannot happen quietly, which is the
    only property that was ever needed.

    **Nothing is applied unless every ticked line reads**, the bill form's
    refusal for the bill form's reason: half a delivery booked is worse than
    none, because the half that landed looks complete. `counted_at` is left
    alone throughout — a delivery note is a claim about a change and a count
    is a measurement, and only one of them means somebody looked at a shelf.
    """
    invoice = get_object_or_404(SupplierInvoice, pk=invoice_id)
    if not invoice.is_draft:
        messages.info(request, "That invoice was already booked.")
        return redirect(invoice.get_absolute_url())

    invoice.order_number = SupplierInvoice.normalise_number(
        request.POST.get("order_number")
    )
    invoice.ordered_on = _read_date(request.POST.get("ordered_on"))
    # **Has it turned up, or is it still coming?** A radio rather than two
    # submit buttons: pressing Enter in a text field submits a form without
    # any button's value, and the two answers differ by whether a shelf
    # moves. A control that can silently fail to be sent is not where that
    # question belongs.
    arrived = request.POST.get("arrival", "received") != "ordered"
    received_on = _read_date(request.POST.get("received_on")) or timezone.localdate()
    supplier_raw = (request.POST.get("supplier") or "").strip()
    invoice.supplier = (
        Supplier.objects.filter(pk=supplier_raw).first() if supplier_raw.isdigit() else None
    )

    keys = request.POST.getlist("row")
    typed = {
        key: {
            "product": (request.POST.get(f"product_{key}") or "").strip(),
            "quantity": (request.POST.get(f"quantity_{key}") or "").strip(),
            "unit_cost": (request.POST.get(f"cost_{key}") or "").strip(),
            "description": (request.POST.get(f"description_{key}") or "").strip(),
            "book": request.POST.get(f"book_{key}") == "1",
        }
        for key in keys
    }

    booked, errors = [], []
    if not invoice.order_number:
        errors.append(
            "This needs the supplier's order number. It is what makes booking "
            "the same delivery twice something the page can see."
        )

    products = {
        p.pk: p for p in RawProduct.objects.active()
    }
    for key in keys:
        row = typed[key]
        if not row["book"]:
            continue
        label = row["description"] or f"row {key}"
        if not row["product"].isdigit() or int(row["product"]) not in products:
            errors.append(f"{label}: pick which blank this line bought.")
            continue
        product = products[int(row["product"])]
        quantity = _read_count(row["quantity"])
        if quantity is None:
            errors.append(f"{label}: “{row['quantity']}” isn't a quantity.")
            continue
        cost = _read_money(row["unit_cost"])
        if cost is None:
            errors.append(f"{label}: “{row['unit_cost']}” isn't a cost.")
            continue
        booked.append((product, quantity, cost, row["description"]))

    if not errors and not booked:
        errors.append(
            "Nothing is ticked, so there is nothing to book. Tick the lines "
            "that arrived, or discard the draft."
        )

    # Last, so it is the message somebody is left holding rather than one of
    # four. It is the only error on this form that is about the document
    # itself rather than about a box.
    clash = _already_booked(invoice)
    if clash and request.POST.get("confirm_duplicate") != "1":
        when = f"{clash.ordered_on:%d %b %Y}" if clash.ordered_on else "an earlier date"
        errors.append(
            f"ORDER {invoice.order_number} HAS ALREADY BEEN BOOKED — "
            f"{clash.lines.count()} lines, dated {when}. Booking it again "
            f"adds those quantities to the shelf a second time. If this "
            f"really is a different delivery, tick the box on the warning "
            f"and save again."
        )

    if errors:
        invoice.save(update_fields=["order_number", "ordered_on", "supplier"])
        for problem in errors:
            messages.error(request, problem)
        lines = list(invoice.lines.select_related("raw_product__category"))
        return render(
            request,
            "scarves/invoice_review.html",
            _review_context(invoice, lines, typed=typed, errors=errors),
        )

    now = timezone.now()
    with transaction.atomic():
        # Locked per row, because these are `+=`: two people booking two
        # invoices for the same blank at once would otherwise both read the
        # same shelf and the second would write over the first. A sheet read
        # one shelf twice at 150 and left it five high once already.
        locked = {
            p.pk: p
            for p in RawProduct.objects.select_for_update().filter(
                pk__in=[product.pk for product, _, _, _ in booked]
            )
        }
        invoice.lines.all().delete()
        for product, quantity, cost, description in booked:
            shelf = locked[product.pk]
            previous = shelf.price
            # **The cost lands now either way.** It is knowable the moment
            # the order is placed, it is what one would cost to buy again
            # today, and holding it back until the box arrives would leave
            # the catalogue quoting a price nobody charges any more.
            shelf.price = cost
            fields = ["price"]
            if arrived:
                shelf.number_on_hand = shelf.number_on_hand + quantity
                fields.append("number_on_hand")
            # **The confirmation is the recording.** Nobody types a supplier's
            # wording in; picking the blank for a line is the choice that was
            # being made anyway, and this is the by-product. Next time that
            # exact line arrives it needs no reading at all.
            if _remember_wording(shelf, description):
                fields.append("invoice_description")
            # `save()` rather than `update()`: `mirror_passthrough_stock`
            # hangs off this model's `post_save`, and one physical pile is
            # only allowed one row that counts it.
            shelf.save(update_fields=fields)
            SupplierInvoiceLine.objects.create(
                invoice=invoice,
                raw_product=shelf,
                description=description,
                quantity=quantity,
                line_total=(cost * quantity).quantize(Decimal("0.01")),
                unit_cost=cost,
                previous_price=previous,
            )
        invoice.booked_at = now
        invoice.received_on = received_on if arrived else None
        invoice.booked_by = request.user if request.user.is_authenticated else None
        invoice.save(update_fields=["order_number", "ordered_on", "supplier",
                                    "received_on", "booked_at", "booked_by"])

    moved = sum(1 for _, _, _, _ in booked)
    messages.success(
        request,
        f"Booked order {invoice.order_number}: {moved} line"
        f"{'' if moved == 1 else 's'}, "
        + ", ".join(f"{q} × {p.name}" for p, q, _, _ in booked)
        + (". Costs are set; nothing is on the shelf until you mark it "
           "received." if not arrived else "."),
    )
    return redirect(invoice.get_absolute_url())


@require_POST
@login_required
def invoice_receive(request, invoice_id):
    """The box turned up. Put what is on the invoice onto the shelf.

    **The second half of a booking, and the only half that moves stock.** The
    lines are already settled — somebody confirmed them, line by line,
    possibly weeks ago — so this replays them rather than asking again. What
    it adds is the one thing that was not knowable then: that the goods are
    in the building.

    Receiving twice is refused the way booking twice is. `received_on` is
    already set, so the page says so and moves nothing; the order number
    guards the document and this guards the event.

    **A short delivery is adjusted here, not corrected afterwards.** Ordered
    80 and received 50 are both true, and the person holding the box knows
    which is which — so the quantities are editable on the way in and the
    line keeps both figures. Sending somebody to mark 80 received and then
    remember to fix the shelf to 50 on another page is the step that has to
    be remembered to be correct, which `CLAUDE.md` says never to add: the
    second half is what gets skipped, and the shelf is then wrong with
    nothing saying so.

    (The earlier reasoning here — that this would be "a second way to write
    the same number" — was wrong. It is the *same* door with the right number
    in it. The absolute-count argument is about healing what went unrecorded
    afterwards, not about refusing a figure somebody can see.)

    Nothing tracks the refund or the outstanding 30. That is settled with the
    supplier by a person; what the app needs is a shelf that matches the
    room.
    """
    invoice = get_object_or_404(SupplierInvoice, pk=invoice_id)
    if invoice.is_draft:
        messages.error(request, "That one is still a draft — confirm it first.")
        return redirect(invoice.get_absolute_url())
    if invoice.received_on is not None:
        messages.info(
            request,
            f"Order {invoice.order_number} was already received on "
            f"{invoice.received_on:%d %b %Y}. Nothing moved.",
        )
        return redirect(invoice.get_absolute_url())

    when = _read_date(request.POST.get("received_on")) or timezone.localdate()
    lines = list(invoice.lines.select_related("raw_product"))
    # A parcel that turns up three weeks after it was given up on is an
    # ordinary thing, so this is the same button and it clears the write-off
    # rather than making somebody undo one first.
    late_arrival = invoice.written_off_on is not None

    try:
      with transaction.atomic():
        locked = {
            p.pk: p
            for p in RawProduct.objects.select_for_update().filter(
                pk__in=[l.raw_product_id for l in lines if l.raw_product_id]
            )
        }
        for line in lines:
            # Blank means "all of it", so an untouched form behaves exactly
            # as it did before this existed.
            typed = (request.POST.get(f"arrived_{line.pk}") or "").strip()
            if typed:
                arrived = _read_count(typed)
                if arrived is None:
                    messages.error(
                        request,
                        f"“{typed}” isn't a quantity, so nothing was received. "
                        f"Fix the line and try again.",
                    )
                    raise _ReceiveRefused()
                if arrived != line.quantity:
                    line.received_quantity = arrived
                    line.save(update_fields=["received_quantity"])
            shelf = locked.get(line.raw_product_id)
            if shelf is None:
                continue
            shelf.number_on_hand = shelf.number_on_hand + line.arrived
            # `save()`, never `update()`: the `post_save` is what mirrors a
            # passthrough's count onto its finished row.
            shelf.save(update_fields=["number_on_hand"])
        invoice.received_on = when
        invoice.written_off_on = None
        invoice.written_off_note = ""
        invoice.save(update_fields=["received_on", "written_off_on",
                                    "written_off_note"])
    except _ReceiveRefused:
        # One unreadable figure receives nothing — the same bargain the bill
        # form makes, and for the same reason: half a delivery booked is
        # worse than none, because the half that landed looks complete.
        return redirect(f"{invoice.get_absolute_url()}?adjust=1")

    short = [l for l in lines if l.came_up_short]
    messages.success(
        request,
        (f"Order {invoice.order_number} turned up after all: "
         if late_arrival else f"Order {invoice.order_number} received: ")
        + ", ".join(f"{l.arrived} × {l.raw_product.name}" for l in lines if l.raw_product_id)
        + " on the shelf."
        + (" Short on "
           + ", ".join(f"{l.raw_product.name} ({l.arrived} of {l.quantity})" for l in short)
           + " — the shelf has what arrived, and the rest is between you and "
             "them." if short else ""),
    )
    return redirect(invoice.get_absolute_url())


class _ReceiveRefused(Exception):
    """One bad figure receives nothing. Rolls the transaction back."""


@login_required
def invoice_receive_lines(request, invoice_id):
    """The per-line quantities, as an htmx fragment.

    No `@page_meta`: it is a fragment, not a page. The button that asks for
    it is a plain link to `?adjust=1` with `hx-get` layered on, so with the
    script blocked the same inputs arrive by a full page load — the pattern
    `production_run_detail` already uses for its product search.
    """
    invoice = get_object_or_404(SupplierInvoice, pk=invoice_id)
    return render(request, "scarves/partials/invoice_receive_lines.html", {
        "invoice": invoice,
        "lines": invoice.lines.select_related("raw_product"),
    })


def _remember_wording(shelf, description):
    """Store the supplier's own words for this blank. Returns whether it moved.

    **One wording belongs to one blank**, so confirming it here takes it off
    whatever else was holding it. That is what makes a mis-match heal: pick
    the wrong blank once and the line is remembered wrongly, but the next
    person to put it right moves the wording rather than leaving two blanks
    both claiming to be `8mm Habotai 21" x 76" Circle` — which would make the
    suggestion depend on row order, and a suggestion that depends on row
    order is one nobody can reason about.

    A blank description changes nothing. Somebody clearing the box means "I
    have nothing to say about the wording", not "forget what you knew".
    """
    wording = (description or "").strip()
    if not wording or wording == shelf.invoice_description:
        return False
    key = invoiceread.normalise_description(wording)
    RawProduct.objects.exclude(pk=shelf.pk).filter(
        invoice_description__iexact=wording
    ).update(invoice_description="")
    # `iexact` catches the ordinary case; the normalised compare catches the
    # curly-quote one, which is the case that actually turns up when the same
    # order arrives once as a PDF and once as an email.
    for other in RawProduct.objects.exclude(pk=shelf.pk).exclude(invoice_description=""):
        if invoiceread.normalise_description(other.invoice_description) == key:
            other.invoice_description = ""
            other.save(update_fields=["invoice_description"])
    shelf.invoice_description = wording
    return True


@require_POST
@login_required
def invoice_write_off(request, invoice_id):
    """It is not coming. Close the order and move nothing.

    **What this really undoes is the suppression.** An open order takes a
    blank's shortage off the reorder page, so one that never arrives would
    hold that signal down for good, silently — the worst shape a bug takes
    here, because the symptom is a page that looks fine and an order nobody
    places. This is the door that hands the signal back, and a person walks
    through it: nothing here expires an order on its own, because a rule that
    decided when an order was dead would be deciding to reorder.

    **The costs stand.** They were true when the order was placed and they
    are still what the supplier charges; unwinding them would be a partial
    revert to a price nobody can name, and `price` is the replacement cost,
    not a record of this document.

    Reversible, because a parcel that turns up three weeks late is a
    completely ordinary thing: receiving a written-off order clears the
    write-off and puts the goods on the shelf.
    """
    invoice = get_object_or_404(SupplierInvoice, pk=invoice_id)
    if invoice.is_draft:
        messages.error(request, "That one is still a draft — discard it instead.")
        return redirect(invoice.get_absolute_url())
    if invoice.received_on is not None:
        messages.error(
            request,
            "That one arrived — its goods are on the shelf, so there is "
            "nothing to give up on.",
        )
        return redirect(invoice.get_absolute_url())

    invoice.written_off_on = _read_date(request.POST.get("written_off_on")) or timezone.localdate()
    invoice.written_off_note = (request.POST.get("written_off_note") or "").strip()[:200]
    invoice.save(update_fields=["written_off_on", "written_off_note"])

    short = [
        line.raw_product.name
        for line in invoice.lines.select_related("raw_product")
        if line.raw_product_id and line.raw_product.raw_shortage
    ]
    messages.success(
        request,
        f"Order {invoice.order_number} closed as not coming. Nothing moved"
        + (f", and {', '.join(sorted(set(short)))} " 
           f"{'is' if len(set(short)) == 1 else 'are'} back below par."
           if short else "."),
    )
    return redirect(invoice.get_absolute_url())


@require_POST
@login_required
def invoice_discard(request, invoice_id):
    """Throw a draft away. Only a draft, and only because nothing points at it.

    `CLAUDE.md` says retire, don't delete — and names the exception this is:
    a row with no history behind it has nothing to preserve. A draft has
    moved no stock and set no cost, so what is being deleted is a reading,
    not a record.
    """
    invoice = get_object_or_404(SupplierInvoice, pk=invoice_id)
    if not invoice.is_draft:
        messages.error(request, "That one is booked — it is a record now, not a draft.")
        return redirect(invoice.get_absolute_url())
    invoice.delete()
    messages.info(request, "Draft discarded. Nothing had moved.")
    return redirect("invoice_index")


def _read_count(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _read_money(raw):
    try:
        value = Decimal(str(raw).strip().lstrip("$").replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value.quantize(Decimal("0.01")) if value >= 0 else None


def _read_date(raw):
    try:
        return date.fromisoformat((raw or "").strip()[:10])
    except ValueError:
        return None
