"""The crew's pages: handbook, hours, timesheet, booth photos and the unidentified-sales queue."""
from collections import Counter
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.http import FileResponse
from django.urls import reverse
from django.db import transaction
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages
from django.shortcuts import render, redirect

from ..sitemap import page_meta
from ..models import (
    BoothPhoto,
    FinishedProduct,
    FinishedProductImage,
    InventoryLog,
    RawProductCategory,
    TimeEntry,
    UnmatchedSale,
)
from .. import crew, ledger, passthroughs, timesheets
from ..forms import BoothPhotoForm, CrewHandbookForm, HoursForm
from .common import _is_htmx
from .images import _CONTENT_TYPE_EXT, _shrink_image


# --- Timekeeping -----------------------------------------------------------
#
# Two pages that between them replace a paper bag and a lot of mental
# arithmetic: a public form where somebody reports a day's hours, and a staff
# page that adds a Wednesday–Tuesday week up, in time for Wednesday's pay run.
#
# The form is under public/ because the whole point is that nobody needs an
# account. What guards it is a four-digit PIN, which is enough to stop the
# wrong name being tapped and not much more — see Employee's docstring. The
# weekly sheet, which shows everyone's hours at once, is staff-only.

#: Wrong PINs tolerated per browser session before the form stops answering.
#: A speed bump, not a lock: sessions are cheap to discard. It costs a casual
#: guesser their patience, and the honest case never sees it.
HOURS_PIN_ATTEMPT_LIMIT = 8


@page_meta(
    title="Crew Handbook",
    description=(
        "What the crew needs to know to work the booth — the till, the "
        "look-up books, sending photos in, and reporting hours. Ends by "
        "handing that person their faire pass. No login: name and PIN, the "
        "same as the other crew pages."
    ),
    category="Booth",
    note="Passes are uploaded per employee in the admin.",
)
@require_http_methods(["GET", "POST"])
def crew_handbook(request):
    """The handbook, and the pass at the bottom of it.

    Two submissions to the same view. The first is the name and PIN, which
    unlocks the text; the second is the "Give me my pass" button at the
    bottom, which additionally wants the box ticked. Putting the button at
    the end of the page is the whole of the scroll enforcement, deliberately
    — anything cleverer means JavaScript, and JavaScript failing here means
    somebody standing at the gate without a pass because their phone had one
    bar. A checkbox costs a tap and can't fail closed.

    Nothing is recorded. There is no read-receipt, no timestamp, no per-season
    version: the tick is a speed bump asking somebody to look at the page, not
    evidence to be produced later. Storing it would invite exactly that use.

    The pass is *downloaded and kept*. Coming back for a lost one means coming
    back through this page, which is cheap — `crew.initial` has already filled
    the name and PIN in on the phone that fetched it the first time.
    """
    if crew.asked_to_forget(request):
        return crew.forget(redirect("crew_handbook"))

    def page(form, *, unlocked, no_pass=False):
        return render(request, "scarves/crew_handbook.html", {
            "form": form,
            "unlocked": unlocked,
            "no_pass": no_pass,
            "remembered": crew.remembered(request)[0],
            "forget_param": crew.FORGET,
        })

    if request.method != "POST":
        form = CrewHandbookForm(user=request.user, initial=crew.initial(request))
        # A signed-in person has already answered the only question the gate
        # asks, so it doesn't get asked. Opening straight onto the text is the
        # point of the login — a screen whose single control is "yes, it's
        # me" is the paperwork this was supposed to remove.
        return page(form, unlocked=form.signed_in_as is not None)

    wants_pass = "want_pass" in request.POST
    form = CrewHandbookForm(request.POST, wants_pass=wants_pass, user=request.user)

    if not form.is_valid():
        # A missing tick is not a reason to throw somebody back to the top of
        # a page they just read. Only a name or PIN we can't place re-locks
        # it; everything else re-renders in place with the error on the box.
        identified = not (form.has_error("employee") or form.has_error("pin"))
        return page(form, unlocked=wants_pass and identified)

    employee = form.cleaned_data["employee"]
    # Absent for a signed-in person, whose PIN was never asked for. There is
    # nothing to remember in that case and nothing to remember it for — the
    # session already does this job, and better.
    pin = form.cleaned_data.get("pin")

    def keep(response):
        return crew.remember(request, response, employee, pin) if pin else response

    if not wants_pass:
        # After the PIN has been checked, never before — see crew.remember.
        return keep(page(form, unlocked=True))

    if not employee.pass_pdf:
        # Named, not a dead button. Whoever is looking at this is legitimate
        # and already knows how to reach Michael; what they need is to be
        # told that waiting for the page to work is not the answer.
        return keep(page(form, unlocked=True, no_pass=True))

    # Streamed rather than handed over as a bucket URL. The bucket is private,
    # so an unsigned `.url` would simply 403 — but even where it wouldn't, a
    # link to somebody's pass outlives the page that produced it, and this one
    # dies with the response.
    response = FileResponse(
        employee.pass_pdf.open("rb"),
        as_attachment=True,
        filename=f"{employee.name} - faire pass.pdf",
    )
    return keep(response)


@page_meta(
    title="Report Hours",
    description=(
        "Where staff report the hours they worked: name, PIN, how long, which "
        "day. No login — the URL is the whole way in, so it's meant to be "
        "bookmarked or put on a card at the stall, not linked publicly."
    ),
    category="Payroll",
)
@require_http_methods(["GET", "POST"])
def hours_entry(request):
    """Report one day's hours, and what they were for.

    Reporting the same day **and the same kind of work** twice is a
    correction, not a second shift — the database won't take two rows for a
    person, a date and a kind, so the second submission asks before it
    overwrites the first. Getting that wrong in the other direction is the
    expensive mistake: a double-tapped Submit that quietly books sixteen
    hours is exactly the kind of thing that survives all the way to payroll.

    The kind is part of that key, which is the whole reason it can be asked
    for at all. Left out of it, somebody reporting a morning of dyeing on a
    day they had already reported a booth shift would be shown an overwrite
    warning for work that has nothing to do with it — and, confirming it,
    would replace the booth hours instead of adding to the day.
    """
    today = timezone.localdate()

    # "Not you?" — drop the remembered name and PIN, come back to an empty
    # form. A GET with a side effect, but the side effect is this browser's
    # own cookie: idempotent, nothing written, nothing to re-submit.
    if crew.asked_to_forget(request):
        return crew.forget(redirect("hours_entry"))

    # Post/redirect/get. The success state is carried in the session rather
    # than the URL so a refresh can't re-submit and a shared screen doesn't
    # leave somebody's name in the address bar.
    saved_pk = request.session.pop("hours_entry_saved", None)
    saved = None
    if saved_pk:
        saved = TimeEntry.objects.filter(pk=saved_pk).select_related("employee").first()

    if request.method == "POST":
        attempts = request.session.get("hours_pin_attempts", 0)
        if attempts >= HOURS_PIN_ATTEMPT_LIMIT:
            return render(request, "scarves/hours_entry.html", {
                "form": HoursForm(today=today),
                "locked": True,
                "today": today,
            })

        form = HoursForm(request.POST, today=today)
        if form.is_valid():
            request.session["hours_pin_attempts"] = 0
            employee = form.cleaned_data["employee"]
            work_date = form.cleaned_data["work_date"]
            hours = form.cleaned_data["hours"]
            kind = form.cleaned_data["kind"]

            existing = TimeEntry.objects.filter(
                employee=employee, work_date=work_date, kind=kind
            ).first()

            # An unconfirmed overwrite bounces back with the old figure shown.
            # The confirm token is the previous hours value, so a stale form
            # left open in another tab can't confirm away a number it never
            # displayed.
            if existing and request.POST.get("confirm_replace") != str(existing.hours):
                return render(request, "scarves/hours_entry.html", {
                    "form": form,
                    "existing": existing,
                    "today": today,
                })

            entry, _created = TimeEntry.objects.update_or_create(
                employee=employee,
                work_date=work_date,
                kind=kind,
                defaults={"hours": hours},
            )
            request.session["hours_entry_saved"] = entry.pk
            # After the PIN has been checked, never before — see crew.remember.
            return crew.remember(
                request, redirect("hours_entry"), employee, form.cleaned_data["pin"]
            )

        if form.has_error("pin"):
            request.session["hours_pin_attempts"] = attempts + 1
    else:
        initial = crew.initial(request, work_date=today)
        remembered_employee = crew.remembered(request)[0]
        if remembered_employee is not None:
            initial["kind"] = _last_kind(remembered_employee)
        form = HoursForm(today=today, initial=initial)

    return render(request, "scarves/hours_entry.html", {
        "form": form,
        "saved": saved,
        "saved_week": _employee_week(saved) if saved else None,
        "today": today,
        "remembered": crew.remembered(request)[0],
        "forget_param": crew.FORGET,
    })


def _last_kind(employee) -> str:
    """The kind of work this person reported last, for the form to open on.

    Somebody paid by the hour to dye reports dyeing most days running, and a
    field that resets to Booth every time is a tap they pay for being the
    less common case. Same argument as the remembered name and PIN: the form
    fills itself in, and the person still submits it.

    The tradeoff is real and is why this is a *prefill* and not a default
    with memory: on the day a dyer picks up a booth shift, the form opens on
    the wrong answer. That costs nothing in pay — both kinds are the same
    hourly rate — and the radio is on screen with its choice showing, which
    is the whole reason the field is radios rather than a dropdown.
    """
    last = (
        TimeEntry.objects
        .filter(employee=employee)
        .order_by("-work_date", "-created_at")
        .values_list("kind", flat=True)
        .first()
    )
    return last or TimeEntry.BOOTH


def _employee_week(entry):
    """One employee's pay week around a just-saved entry, for the receipt.

    Showing the week back is the cheapest error check there is: the person
    who worked the days is the only one who can look at Wednesday through
    Tuesday and say "that's not right" while it's still easy to fix — and
    the window for that closes Tuesday, because Wednesday it gets paid.

    Every kind of work is in it, not just the one just reported. A week with
    two kinds in it is exactly the week worth reading back, and filtering to
    the kind in hand would show somebody a total that is missing hours they
    know they worked.
    """
    start = timesheets.week_start(entry.work_date)
    entries = list(
        TimeEntry.objects
        .filter(
            employee=entry.employee,
            work_date__gte=start,
            work_date__lte=timesheets.week_end(start),
        )
        .order_by("work_date")
    )
    return {
        "start": start,
        "end": timesheets.week_end(start),
        "entries": entries,
        "total": sum((e.hours for e in entries), Decimal("0")),
    }


@page_meta(
    title="Timesheet",
    description=(
        "Everyone's hours for one Wednesday–Tuesday pay week, split into "
        "booth and dyeing, totalled per person and per day, with the rows "
        "worth a second look flagged."
    ),
    category="Payroll",
    note="Defaults to this week; ?week=YYYY-MM-DD picks another.",
)
@login_required
def timesheet(request):
    """The week, added up.

    Takes its week from the query string rather than a URL parameter, which
    is what keeps it a single page with no picker to maintain — every week
    that has ever existed is one link away from this one.
    """
    today = timezone.localdate()
    start = timesheets.parse_week(request.GET.get("week"), today)
    summary = timesheets.week_summary(start)

    return render(request, "scarves/timesheet.html", {
        "summary": summary,
        "previous_week": start - timedelta(days=7),
        "next_week": start + timedelta(days=7),
        "is_current_week": start == timesheets.week_start(today),
        "today": today,
        "hours_entry_url": request.build_absolute_uri(reverse("hours_entry")),
    })


# ---------------------------------------------------------------------------
# The booth: photos in, and unidentified sales reconciled.
#
# One page for the crew (`secret/booth/`, PIN — no accounts, same reasoning as
# the hours form) and two for the office: a gallery of what may be posted, and
# the queue of sales Square took that this app couldn't tie to a product.
#
# The two halves share a page because they share a moment: a phone comes out
# at a stall, once, and asking someone to pick the right page first is how you
# get no photos at all.
# ---------------------------------------------------------------------------

#: How far apart a photo and a sale can be and still be the same scarf. Fifteen
#: minutes is the width of a queue at a busy stall: long enough that the report
#: can wait until the customer has walked away, short enough that two sales of
#: the same style rarely both land inside it.
UNMATCHED_WINDOW = timedelta(minutes=15)

#: Same PIN as the hours form, so the same limit — but its own counter, since
#: locking one page has no business locking the other.
BOOTH_PIN_ATTEMPT_LIMIT = HOURS_PIN_ATTEMPT_LIMIT


def _booth_photo_file(upload):
    """The uploaded photo, downscaled, ready to hand to an ImageField.

    Straight through the app rather than the presigned-POST dance the product
    upload page uses: this is one photo taken on a phone with one bar of
    signal, and a page that needs JavaScript to work is a page that sometimes
    doesn't. The shrink is the same one, so a 5MB phone JPEG doesn't reach the
    bucket either way.
    """
    data = upload.read()
    content_type = (getattr(upload, "content_type", "") or "image/jpeg").lower()
    try:
        shrunk = _shrink_image(data)
    except Exception:
        shrunk = None          # unreadable by PIL: keep what was sent
    if shrunk:
        data, content_type = shrunk
    ext = _CONTENT_TYPE_EXT.get(content_type, ".jpg")
    return ContentFile(data, name=f"{uuid.uuid4().hex}{ext}")


@page_meta(
    title="Send a Photo",
    description="Send a photo in from the booth — something worth sharing, or "
                "a colorway nobody could identify that sold anyway. Name and PIN, "
                "no account needed.",
    category="Booth",
    note="Unlisted: hand out the URL, don't advertise it.",
)
def booth_photo(request):
    """The crew's page. No login — a PIN, exactly like the hours form.

    Post/redirect/get with the receipt in the session, so a refresh at the
    stall can't send the same photo twice and a shared phone doesn't leave
    somebody's name in the address bar.
    """
    now = timezone.localtime()

    # "Not you?" — drop the remembered name and PIN, come back to an empty
    # form. A GET with a side effect, but the side effect is this browser's
    # own cookie: idempotent, nothing written, nothing to re-submit.
    if crew.asked_to_forget(request):
        return crew.forget(redirect("booth_photo"))

    saved_pk = request.session.pop("booth_photo_saved", None)
    saved = BoothPhoto.objects.filter(pk=saved_pk).select_related("employee").first() if saved_pk else None

    if request.method == "POST":
        attempts = request.session.get("booth_pin_attempts", 0)
        if attempts >= BOOTH_PIN_ATTEMPT_LIMIT:
            return render(request, "scarves/booth_photo.html", {
                "form": BoothPhotoForm(now=now, user=request.user),
                "locked": True,
                "now": now,
            })

        form = BoothPhotoForm(request.POST, request.FILES, now=now, user=request.user)
        if form.is_valid():
            request.session["booth_pin_attempts"] = 0
            data = form.cleaned_data
            share = data["reason"] == BoothPhoto.REASON_SHARE

            # Only the half that applies is stored. Both halves are always
            # submitted, so a report that changed reason mid-thought would
            # otherwise leave a sharing permission attached to a sale report
            # nobody ever meant to publish.
            photo = BoothPhoto(
                employee=data["employee"],
                reason=data["reason"],
                share_website=share and data["share_website"],
                share_instagram=share and data["share_instagram"],
                people_in_photo=share and data["people_in_photo"],
                people_agreed=share and data["people_agreed"],
                caption=data["caption"] if share else "",
                tag=data["tag"] if share else "",
                sold_at=None if share else data["sold_at"],
                sku_prefix="" if share else data["sku_prefix"],
                note="" if share else data["note"],
            )
            # Built once: the uploaded file is a stream, and reading it a
            # second time yields nothing.
            stored = _booth_photo_file(data["photo"])
            photo.image.save(stored.name, stored, save=False)
            photo.save()
            request.session["booth_photo_saved"] = photo.pk
            # After the PIN has been checked, never before — see crew.remember.
            # Only the crew have a PIN to remember. A signed-in staff member
            # is identified by their login, which outlives any cookie here.
            response = redirect("booth_photo")
            if "pin" in data:
                response = crew.remember(
                    request, response, data["employee"], data["pin"]
                )
            return response

        if form.has_error("pin"):
            request.session["booth_pin_attempts"] = attempts + 1
    else:
        form = BoothPhotoForm(now=now, user=request.user, initial=crew.initial(
            request,
            reason=BoothPhoto.REASON_SHARE,
            sold_at=now.strftime("%Y-%m-%dT%H:%M"),
        ))

    return render(request, "scarves/booth_photo.html", {
        "form": form,
        "saved": saved,
        "now": now,
        # Only meaningful for the crew. Keyed on the PIN field rather than on
        # being signed in, because the note it drives says "name and PIN
        # filled in from this phone" — with no PIN on the page that sentence
        # describes something that didn't happen.
        "remembered": crew.remembered(request)[0] if "pin" in form.fields else None,
        "forget_param": crew.FORGET,
    })


@page_meta(
    title="Booth Photos",
    description="Photos the crew sent in to share, with what each one is "
                "cleared for — website, Instagram, or nothing yet.",
    category="Products",
)
@login_required
def booth_photos(request):
    """The gallery. Reads `shareable`, not the two destination ticks.

    A photo with a recognisable person in it and no answer from them is not
    postable however many boxes the sender ticked, and the badge on the card
    has to say that or the page is worse than no page.
    """
    photos = (
        BoothPhoto.objects.filter(reason=BoothPhoto.REASON_SHARE)
        .select_related("employee")
    )
    return render(request, "scarves/booth_photos.html", {"photos": photos})


def _open_sales_on(day):
    """Unresolved, undismissed sales that Square timestamped on `day` (local)."""
    start = timezone.make_aware(datetime.combine(day, time.min))
    return (
        UnmatchedSale.objects.filter(
            resolved_at__isnull=True,
            dismissed_at__isnull=True,
            sold_at__gte=start,
            sold_at__lt=start + timedelta(days=1),
        )
        .order_by("sold_at")
    )


def _unused_reports():
    """Unidentified-sale photos not yet spoken for by a resolved sale."""
    return (
        BoothPhoto.objects.filter(
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            matched_sales__isnull=True,
        )
        .select_related("employee")
        .order_by("sold_at")
    )


def _review_day(request):
    """The day being reviewed: the query string, else the oldest open sale,
    else today.

    Oldest rather than newest on purpose — the queue is a to-do list, and the
    row most likely to be forgotten is the one furthest back."""
    raw = request.GET.get("day")
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    oldest = (
        UnmatchedSale.objects.filter(
            resolved_at__isnull=True, dismissed_at__isnull=True
        )
        .order_by("sold_at")
        .first()
    )
    return timezone.localtime(oldest.sold_at).date() if oldest else timezone.localdate()


def _resolution_options(reports, cache=None):
    """Products a sale could plausibly be, given the photos near it.

    The reported prefix is the blank, not the colorway — six characters off a
    tag that says `INFI-AEGEAN`. That is exactly the narrowing worth having:
    nobody can read a colorway off a scarf they couldn't name, but the style
    turns a few hundred products into a few dozen. With no prefix reported the
    honest answer is the whole active catalogue rather than a guess.

    **The answer is keyed on the prefixes and nothing else**, so `cache` is a
    dict the caller keeps for one request. That matters more than it sounds:
    the common row has no photo beside it and therefore no prefix, so every
    such row asks the identical question — *the whole active catalogue* — and
    a day with ten of them was running ten copies of the biggest query on the
    page. Rows genuinely differ only when the photos differ.
    """
    prefixes = frozenset(r.sku_prefix for r in reports if r.sku_prefix)
    if cache is not None and prefixes in cache:
        return cache[prefixes]

    products = FinishedProduct.objects.active()
    if prefixes:
        narrowed = Q()
        for prefix in prefixes:
            narrowed |= Q(sku__istartswith=prefix)
        products = products.filter(narrowed)
    answer = (
        list(products.select_related("raw_product", "recipe").order_by("name")),
        bool(prefixes),
    )
    if cache is not None:
        cache[prefixes] = answer
    return answer


def _like_key(sale):
    """What "another one like this" means for this line, or `None`.

    Two keys, and they are not two precisions of one idea — they apply to
    different populations, which is the thing to keep hold of:

    * **A Square variation id** means Square has a catalog object this app
      doesn't know. That is the unsynced-variation case, and it is the one
      *most* likely to be a real scarf. Precise, and precisely the group where
      dismissing in bulk is expensive: it writes the sales off and the count
      stays wrong with nothing saying so, which is the silence this queue
      exists to break.
    * **A name, with no variation id at all**, is a custom amount somebody
      hand-keyed. Looser key — and the population where a bulk dismissal is
      genuinely safe, because these really are the tips, bags and hats.

    So a name match is scoped to lines that have no variation id. Without
    that, dismissing every `Custom Amount` would sweep up a row that *does*
    carry a Square item — the dangerous group, taken by the safe group's
    button, with nothing on screen saying so.

    A line with neither gets no key and no button, because "all like this"
    would mean "all the nameless ones", which is a grab bag rather than a
    group.
    """
    if sale.square_variation_id:
        return ("item", sale.square_variation_id)
    if sale.name:
        return ("name", sale.name)
    return None


def _like_this_on_day(sale, day):
    """The open lines on `day` that this one stands for, including itself.

    Day-scoped deliberately. Everything the button claims is on the screen
    it was clicked from, so the count beside it can be checked by looking
    rather than trusted.
    """
    key = _like_key(sale)
    if key is None:
        return [sale]

    kind, value = key
    group = _open_sales_on(day)
    if kind == "item":
        group = group.filter(square_variation_id=value)
    else:
        group = group.filter(name=value, square_variation_id="")
    return list(group)


def _dismiss(sales, reason):
    """Mark every one of them dismissed, in one write.

    Nothing is destroyed — dismissal is a timestamp and a sentence, and the
    admin clears both. That is what makes a button covering forty-seven rows
    an acceptable thing to offer at all.
    """
    now = timezone.now()
    UnmatchedSale.objects.filter(pk__in=[s.pk for s in sales]).update(
        dismissed_at=now, dismissed_reason=reason
    )
    for sale in sales:
        sale.dismissed_at = now
        sale.dismissed_reason = reason
    return sales


def _open_unmatched_total():
    """Everything still in the queue, any day. One count, shared by the page
    and by the fragment a dismissal swaps in — a header that kept saying 12
    over a list of 11 is the page contradicting itself."""
    return UnmatchedSale.objects.filter(
        resolved_at__isnull=True, dismissed_at__isnull=True
    ).count()


def _orphan_reports(day, sales=None, reports=None):
    """Photos with no open sale beside them, on `day`.

    Kept on the page rather than filtered out: a report with nothing to match
    is the interesting case — either the sale never reached Square, or it was
    rung up as a product after all and the scarf on the photo is still
    counted as in stock.

    One definition, used by the page and again after a dismissal, because
    dismissing a sale can *make* an orphan: the photo that was sitting beside
    it now has nothing to be beside.
    """
    if sales is None:
        sales = list(_open_sales_on(day))
    if reports is None:
        reports = list(_unused_reports())
    paired = {
        report.pk
        for sale in sales
        for report in reports
        if abs(report.when - sale.sold_at) <= UNMATCHED_WINDOW
    }
    return [
        report for report in reports
        if report.pk not in paired
        and timezone.localtime(report.when).date() == day
    ]


@page_meta(
    title="Unidentified Sales",
    description="Square sold something this app couldn't tie to a product. "
                "Match each one against the photos the booth sent in — paired "
                "by time — so the stock leaves inventory like any other sale.",
    category="Inventory",
    note="Add ?day=YYYY-MM-DD to review another day.",
)
@login_required
def unmatched_sales(request):
    day = _review_day(request)
    sales = list(_open_sales_on(day))
    reports = list(_unused_reports())

    # One cache for the request. Most rows have no photo and so ask the same
    # question, and that question is the whole catalogue.
    options_cache = {}
    # How many lines each row stands for, counted off the list already in
    # memory rather than a query per row — which is the mistake this page had
    # and the reason `options_cache` exists two lines up.
    like_counts = Counter(
        key for key in (_like_key(sale) for sale in sales) if key is not None
    )
    rows = []
    for sale in sales:
        near = [
            report for report in reports
            if abs(report.when - sale.sold_at) <= UNMATCHED_WINDOW
        ]
        options, narrowed = _resolution_options(near, options_cache)
        key = _like_key(sale)
        rows.append({
            "sale": sale,
            "reports": near,
            "options": options,
            "narrowed": narrowed,
            # A line can only be turned into a product if Square told us what
            # it was called. Offered off the name rather than off the presence
            # of a variation id, because a hand-keyed notion is still a notion
            # — it just won't identify itself next time, and the row says so.
            "can_track": bool(sale.name or sale.variation_name),
            "unit_price": (
                Decimal(sale.amount_cents) / Decimal(100)
                / max(sale.quantity, 1)
            ).quantize(Decimal("0.01")),
            # Offered only when it stands for more than itself: a button
            # reading "dismiss all 1 like this" is the button beside it,
            # wearing a longer label.
            "like_count": like_counts.get(key, 0) if key else 0,
            "like_kind": key[0] if key else "",
        })

    return render(request, "scarves/unmatched_sales.html", {
        "day": day,
        "rows": rows,
        # Which table it sits on. Offered rather than guessed: the app cannot
        # tell a yarn bowl from a silk square from the line Square sent.
        "categories": RawProductCategory.objects.all(),
        "orphans": _orphan_reports(day, sales, reports),
        "open_total": _open_unmatched_total(),
        "window_minutes": int(UNMATCHED_WINDOW.total_seconds() // 60),
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
    })


@require_POST
@login_required
def resolve_unmatched_sale(request, pk):
    """Match one sale to a product, or say it was never a scarf.

    Resolving moves stock, which looks like a contradiction of the rule that
    back-dated entries never do — it isn't. That rule exists because a
    backfilled kanban card records a bath that was already counted, so
    applying it again would inflate the count. This sale was never applied at
    all: the webhook dropped it, the scarf left the tent, and `number_on_hand`
    has been one too high ever since. The whole point is to apply it late.
    """
    sale = get_object_or_404(UnmatchedSale, pk=pk)
    day = (request.POST.get("day") or "").strip()
    redirect_to = reverse("unmatched_sales") + (f"?day={day}" if day else "")

    if not sale.is_open:
        # A double-tap at pace, or a stale tab. Says where things stand
        # rather than erroring — the page is already in the state they were
        # asking for.
        if _is_htmx(request) and sale.dismissed_at:
            return _dismissed_row(request, [sale], _posted_day(request, sale))
        messages.info(request, "That sale was already dealt with.")
        return redirect(redirect_to)

    if request.POST.get("dismiss") or request.POST.get("dismiss_all"):
        reason = (request.POST.get("dismissed_reason") or "").strip()[:200]
        day_of = _posted_day(request, sale)
        if request.POST.get("dismiss_all"):
            # The clicked row leads, because it is the one the swap targets;
            # the rest ride out-of-band.
            group = [sale] + [
                other for other in _like_this_on_day(sale, day_of)
                if other.pk != sale.pk
            ]
        else:
            group = [sale]
        _dismiss(group, reason)

        if _is_htmx(request):
            # No `messages` on this path: it would sit in the session and
            # surface on some later full page load, describing a row the
            # reader dealt with twenty dismissals ago. The strips that
            # replace the rows are the receipt.
            return _dismissed_row(request, group, day_of)
        if len(group) == 1:
            messages.success(
                request, f"Dismissed “{sale.name or 'that line'}” — not a scarf."
            )
        else:
            messages.success(
                request,
                f"Dismissed {len(group)} lines like “{sale.name or 'that line'}” "
                f"on {day_of:%d %b %Y} — not scarves.",
            )
        return redirect(redirect_to)

    if request.POST.get("create_product"):
        # Nothing in the catalogue to match, because the thing was never in
        # it: a bought-in notion that has been rung up at the till for as
        # long as Square has known it. Making the product here rather than
        # sending somebody to the admin is what keeps the queue workable —
        # and the row it makes carries the Square variation id, so the *next*
        # sale of the same thing identifies itself and never reaches this
        # page at all.
        # Parsed defensively rather than filtered on directly: the select's
        # own placeholder posts an empty string, which is the likeliest thing
        # to arrive here, and `filter(pk="")` raises. A 500 on the ordinary
        # mis-click, where the message below is what was wanted.
        category_id = (request.POST.get("category_id") or "").strip()
        category = (
            RawProductCategory.objects.filter(pk=category_id).first()
            if category_id.isdigit() else None
        )
        if category is None:
            messages.error(
                request,
                "Pick which category the new product belongs in before "
                "tracking it — that is which table it sits on.",
            )
            return redirect(redirect_to)
        try:
            product, was_created = passthroughs.create_from_sale(sale, category)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(redirect_to)
        if was_created:
            messages.success(
                request,
                f"Now tracking “{product.name}” at ${product.price} "
                f"({product.sku}) — future sales of it will match on their own.",
            )
        else:
            # Already made from an earlier line of the same item. Said out
            # loud rather than silently reused: the reader asked to create
            # something and did not, and a page that looks identical either
            # way is how the same thing gets made twice by hand.
            messages.info(
                request,
                f"“{product.name}” was already being tracked, so this sale "
                f"went to the product that exists rather than a second one.",
            )
    else:
        product = get_object_or_404(
            FinishedProduct, pk=request.POST.get("product_id")
        )
    report = BoothPhoto.objects.filter(pk=request.POST.get("report_id")).first()

    with transaction.atomic():
        # Through the ledger, which writes to whichever row holds the count
        # — a passthrough's lives on the *raw* product, and this view can
        # create passthroughs, so a direct write here would snap back.
        log = ledger.move(
            product, -sale.quantity,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_UNMATCHED_SALE,
            notes=(
                "Matched by hand from an unidentified sale"
                + (f", reported by {report.employee.name}" if report else "")
                + f" (Square line “{sale.name or 'unnamed'}”)."
            ),
            sale_reference=sale.order_id,
        )
        # created_at is auto_now_add, so the sale's own time can only be set
        # afterwards. It matters: this row is otherwise dated the day someone
        # got round to the queue, which is not the day the scarf sold.
        InventoryLog.objects.filter(pk=log.pk).update(created_at=sale.sold_at)

        sale.resolved_product = product
        sale.resolved_photo = report
        sale.resolved_at = timezone.now()
        sale.save(update_fields=["resolved_product", "resolved_photo", "resolved_at"])

        # The photo is a photo of the scarf, taken by someone who couldn't
        # name it. Filing it against the product is opt-in rather than
        # automatic — a stall snap in bad light is not always what you want
        # the catalogue to show — but when it is, next time it's identifiable.
        if report and request.POST.get("file_photo"):
            next_order = (product.images.aggregate(Max("order"))["order__max"] or 0) + 1
            FinishedProductImage.objects.create(
                finished_product=product,
                image=report.image.name,
                order=next_order,
                alt_text=f"{product.name} (from the booth, {report.when:%d %b %Y})",
            )

    messages.success(
        request,
        f"Matched to {product.name} — {sale.quantity} off the shelf, logged as "
        f"a sale on {timezone.localtime(sale.sold_at):%d %b %Y, %H:%M}.",
    )
    return redirect(redirect_to)


def _posted_day(request, sale):
    """The day whose page this came off.

    Falls back to the sale's own day rather than today: an absent or
    unreadable value would otherwise gather the wrong day's group and rebuild
    the wrong day's orphan list, and the only symptom is rows appearing or
    vanishing on a page nobody is looking at.
    """
    raw = (request.POST.get("day") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return timezone.localtime(sale.sold_at).date()


def _dismissed_row(request, sales, day):
    """The one-line strips dismissed rows collapse to, plus what they changed.

    The queue gets worked a hundred lines at a time, and what made that slow
    was structural rather than incidental: every dismissal was a full
    navigation that rebuilt every *other* row on the day, each carrying a
    `<select>` of the whole active catalogue. Swapping rows for lines replaces
    all of that with a few hundred bytes.

    `sales[0]` is what the click targeted and the rest ride out-of-band, so a
    "dismiss all like this" empties every one of them off the page in the one
    response — a row left behind would read as one the button missed.
    """
    return render(request, "scarves/partials/unmatched_dismissed.html", {
        "sales": sales,
        "day": day,
        "open_total": _open_unmatched_total(),
        "orphans": _orphan_reports(day),
    })
