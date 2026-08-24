"""Remembering who is holding the phone, on the two `secret/` PIN pages.

The hours form and the booth form both open with "choose your name" and "type
your PIN". That is four taps and four digits before the actual reporting
starts, every single time, and the moment it is being asked for is the worst
one available: a scarf has just sold, there is a queue, and the phone is out
for about ten seconds. Friction there doesn't produce a late report, it
produces no report — and the whole point of `secret/booth/` is that the
report happens at all.

So the name and the PIN ride in a cookie and the pages open pre-filled.

**The cookie fills the form in; it never stands in for the PIN check.** Both
forms still compare the submitted PIN against `Employee.pin` on every POST,
exactly as before — this module writes `initial`, and nothing else. That is
deliberate and it is the whole safety argument: a cookie that *authorised*
would mean a found phone submits with no check anywhere, and it would need
its own expiry story, its own revocation, its own tests. A cookie that only
types for you can be wrong, stale, or forged and the worst outcome is the
same error message a typo gets.

Which is also why the PIN itself is in there rather than some token standing
for it. Signing stops tampering, not reading — `get_signed_cookie` is
readable by anyone holding the device. The PIN was never a secret (see
CLAUDE.md: it stops the wrong name being tapped, it does not guard anything),
so putting it somewhere readable-with-the-device costs nothing it was
protecting, and it keeps one code path instead of two.

Everyone uses their own phone; there is no shared stall tablet. That is what
makes remembering the *name* safe as well as the PIN — on a shared device,
pre-filling a name is how one person's hours get filed under another, and
this module would need a much louder confirmation than a link. If a tablet
ever appears, that is the thing to revisit first.
"""

from django.core.signing import BadSignature

from .models import Employee

#: One cookie for both pages: same person, same PIN, and someone who has just
#: sent a photo shouldn't have to re-introduce themselves to the hours form.
COOKIE = "scarves_crew"

#: Long enough to cover a winter with no festivals in it. The cookie holds no
#: authority (see above), so the expiry is a convenience deadline rather than
#: a security one — the cost of it lasting too long is nil, and the cost of it
#: expiring over the off-season is the retyping this exists to remove.
MAX_AGE = 60 * 60 * 24 * 365

#: The query string that forgets. Personal phones make this rare, but "rare"
#: isn't "never": phones get lent, handed over, and replaced.
FORGET = "forget"


def remembered(request):
    """`(employee, pin)` for the person this device belongs to, or `(None, None)`.

    Resolved against the database on every read rather than trusted, because
    a cookie written last season can outlive the facts in it. Three ways it
    can be stale, and each one is dropped quietly rather than shown as an
    error on a page nobody has typed into yet:

    - tampered or unsigned  -> forget the lot
    - employee gone or now inactive -> forget the lot
    - PIN has since changed -> keep the name, drop the PIN

    That last one is the useful split. The name is still right, so the person
    lands on a page that knows who they are and asks for the one thing that
    actually changed.
    """
    try:
        raw = request.get_signed_cookie(COOKIE, default=None, max_age=MAX_AGE)
    except BadSignature:
        return None, None
    if not raw:
        return None, None

    pk, _, pin = str(raw).partition(":")
    if not pk.isdigit():
        return None, None

    employee = Employee.objects.filter(pk=int(pk), is_active=True).first()
    if employee is None:
        return None, None

    return employee, (pin if pin == employee.pin else None)


def initial(request, **extra):
    """Form `initial` with the remembered name and PIN filled in.

    Everything else the caller wants is passed through, so a view never has
    to build the dict in two places and can't accidentally overwrite one
    half of it.
    """
    employee, pin = remembered(request)
    values = dict(extra)
    if employee is not None:
        values["employee"] = employee.pk
        if pin:
            values["pin"] = pin
    return values


def remember(request, response, employee, pin):
    """Write the cookie, after a submission whose PIN has already been checked.

    Only ever called on the success path. Remembering a PIN that failed would
    pre-fill the wrong answer forever, which is worse than remembering
    nothing: the page opens looking ready and rejects whatever is submitted,
    and the person has no way to tell that the field they didn't type in is
    the problem.
    """
    response.set_signed_cookie(
        COOKIE,
        f"{employee.pk}:{pin}",
        max_age=MAX_AGE,
        httponly=True,             # nothing on these pages reads it from JS
        samesite="Lax",
        secure=request.is_secure(),
    )
    return response


def forget(response):
    """Drop the cookie. Same attributes, or the browser keeps the old one."""
    response.delete_cookie(COOKIE, samesite="Lax")
    return response


def asked_to_forget(request):
    """Whether this GET is the "not you?" link."""
    return request.method == "GET" and FORGET in request.GET


def employee_for(user):
    """The `Employee` a signed-in user *is* — created if they are new here.

    A login is a stronger claim than four digits, so a signed-in person is
    never asked to pick their own name off a list or type a PIN. That much
    the booth form already did. What it did not do was cover the person with
    a login and no `Employee` row, who fell back to the picker — and being
    shown a list of your colleagues' names and asked which one you are is
    exactly the paperwork a login was supposed to have settled.

    So an unlinked login gets a row made for it, named after the username and
    linked, which means it only ever happens once. The rows this creates are
    real employees; they were always going to need one the moment anybody
    wanted to hand them a pass or read their hours.

    **The created row has no PIN**, deliberately. These people authenticate
    through Django's own user management, so a PIN would be a second
    credential for the same person, invented by the app, that nobody has been
    told. Blank means "this one signs in" — and because `clean_pin` rejects
    anything that isn't four digits, a blank PIN cannot be submitted, so the
    row can never be used to walk in through the name-and-PIN door.

    One judgement is baked in: a same-named row that isn't linked yet gets
    linked rather than duplicated. That is a guess, and it is the one guess
    allowed here — matching an exact username against an exact employee name
    is narrow, and the alternatives are an `IntegrityError` on a unique name
    or a second row for a person who already has one.
    """
    if user is None or not user.is_authenticated:
        return None

    employee = Employee.objects.filter(user=user).first()
    if employee is not None:
        # Returned even when inactive: retiring somebody doesn't stop them
        # being who they are, and the pages that shouldn't serve them filter
        # on `is_active` themselves.
        return employee

    name = (user.get_username() or "").strip()
    if not name:
        return None

    employee, created = Employee.objects.get_or_create(
        name=name,
        defaults={"user": user},
    )
    if not created and employee.user_id is None:
        employee.user = user
        employee.save(update_fields=["user"])
    return employee
