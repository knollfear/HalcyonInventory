# The crew: hours, the booth, the handbook and the PIN

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Timekeeping: the pay week, and the two totals

The hours form (`secret/hours/`) and the timesheet (`private/timesheet/`)
replace a paper bag and a lot of mental arithmetic. Three things are load-
bearing and none of them are obvious from the models.

**The pay week runs Saturday to Friday.** No date library assumes that, so
every "which week is this?" question goes through `timesheets.week_start()`
rather than being worked out at the call site. Getting it wrong is invisible:
the page still renders seven columns, they're just the wrong seven, and the
totals belong to a week nobody is paying for. `PayWeekTests` pins it.

**Hours are self-reported, not clocked.** Nobody enters a start and end time;
they pick a decimal off a quarter-hour dropdown. That's a deliberate trade —
the arithmetic disappears, and in exchange there's no start time to check a
claim against. What replaces it is review: the sheet flags long days, long
weeks, revised figures and anything reported more than a week late, and a
person signs the week off. Those flags aren't errors and must not be styled
as errors; a 13-hour day at a festival is entirely normal.

The picker is a *rendering* of the rule, not the rule itself. `hours` is a
`DecimalField` validated against "a quarter-hour between 0.25 and 14" —
originally a `ChoiceField`, which compares submitted strings and so decided
`9.5` and `9.50` were different answers and only accepted one.

**Scope is booth hours during festival days — nothing else.** Production
help (dyeing, prep, anything back at the shop) is deliberately not tracked
here. There is no employer field, no work-type field, and no "kind of work"
dimension anywhere in `TimeEntry`, `HoursForm` or `timesheets.py`; an earlier
draft had one and it was removed on purpose.

**Don't add one back as a schema change.** Whether a second kind of work
belongs in these totals is a payroll question, and it has to be answered
before the field exists — a column that quietly starts collecting a second
kind of work makes every total on the timesheet mean something different
depending on who typed it, with nothing on the page to say so. Until then a
single unqualified total is the honest output, and the page says "booth
hours" rather than "hours" so it can't be misread later.

## The booth: photos in, and unidentified sales

`secret/booth/` is one page the crew uses for two things, because there is one
moment when a phone comes out at a stall and asking someone to pick the right
page first is how you get no photos at all. The reason picks which half of the
form is stored — the view keeps only that half, so a report that changed reason
mid-thought can't leave a sharing permission attached to a sale report.

**No login, a PIN, same as the hours form.** The crew has no accounts, and
giving them accounts would hand the production pages to seasonal staff. The
alternative — roles and permissions across ~30 hand-written views — is a real
project, and the trigger for starting it is staff needing to *see* more than
one page, not this. Until then the `Employee` PIN does what it does on the
hours form: it stops the wrong name being tapped, and it is not a secret.

### Signed in? Then no name picker and no PIN

The crew get the name-and-PIN pair; a staff login gets neither, because a
login is a stronger claim than four digits and being asked to pick your own
name off a list on a page you already authenticated for is what makes an app
feel like paperwork.

`Employee.user` is the link, and it is blank for almost everybody — the crew
are deliberately account-less. It only exists so the few people with a login
are recognised.

| Who                      | Name picker | PIN |
|--------------------------|-------------|-----|
| Not signed in (the crew) | yes         | yes |
| Signed in                | no          | no  |

**A signed-in person is never asked who they are** — not here, not on the
close, not on the handbook. `crew.employee_for` is the one resolver all three
use, and an unlinked login gets a row made for it, named after the username
and linked, so it happens once per person.

That third state used to exist and be defended: an unlinked login fell back
to the name picker, on the grounds that the app genuinely doesn't know which
`Employee` a login is and guessing would put somebody else's name on a
sharing permission. The premise is still true and the conclusion was still
wrong. What it produced was somebody signed in being shown a list of their
colleagues and asked which one they were, on a page they had already
authenticated for — and the picker is itself a thing to mis-tap, so it was
*worse* for attribution than having no picker at all. The resolver doesn't
guess; it makes a row for the person demonstrably signed in.

**A created row has a blank PIN**, and that is load-bearing rather than
unfinished. These people sign in through Django, so a PIN would be a second
credential for the same person that nobody has been told. `clean_pin` wants
four digits, so nothing submittable matches a blank one — the row cannot be
used to walk in through the name-and-PIN door. Blank-PIN rows are kept off
the name pickers for the same reason: picking one is a dead end. Type a PIN
into the admin and the row joins the picker, which is the whole of what that
field now means.

One guess is allowed: a same-named row that isn't linked yet gets linked
rather than duplicated. An exact username against an exact employee name is
narrow, and the alternatives are an `IntegrityError` on a unique name or two
rows for one person.

**The fields are removed from the form, not hidden in the template.** A field
that is present but invisible is one a hand-built POST can still fill in, and
here that would mean attributing a photo to whoever the sender named.

### One reason, one half, and no request to switch

The radio hides the half that doesn't apply, in CSS (`form:has(input[value=…]:checked)`),
not with an htmx swap. The fields are already on the page, this gets used on
a phone on one bar of signal, and a toggle that needs the network is a toggle
that sometimes doesn't happen — the same reasoning that keeps the photo
upload off the presigned-POST path. A browser without `:has()` shows both
halves, which is what the page did before, so the fallback is the old
behaviour rather than a form with nothing in it.

None of this is load-bearing for correctness: the view already stores only
the half the reason matches. It's the difference between a form that looks
like it's asking two unrelated things and one that asks the question you
picked.

### Sharing permission is two questions, not one

A tick from the sender is the **sender's** permission. It is not the permission
of whoever is in the photo, and the form refuses a submission that confuses the
two: someone recognisable, plus a destination ticked, requires "I asked them
and they said yes". Untick the destinations and the photo still sends — "here,
your call" is a legitimate answer and must not be blocked.

Website and Instagram are separate ticks on purpose: one is a shop page, the
other is a feed with an audience and a comment box, and people do say yes to
one and no to the other. The gallery reads `BoothPhoto.shareable`, never the
two destination flags, so the awkward case can't be posted by reading the wrong
checkbox. None of this is legal advice; it is a record of who agreed to what,
and when, which is the part that is worth anything later.

### An unidentified sale must not vanish

`square_webhook` used to `continue` past any line item it couldn't tie to a
`FinishedProduct`. That meant a scarf nobody could name was rung up, walked out
of the tent, and left no trace: Square had the money, this app still had the
stock, and nothing in either said they disagreed. **The silence was the whole
failure** — the count was wrong and looked fine.

The reason on the booth form is **"A colorway nobody could identify"**, not
"a scarf" — it is one problem wearing two coats. A skein of yarn and a silk
scarf raise exactly the same question ("which colorway is this?"), and a
label naming only one of them invites a hesitation over whether the other
counts. (The undyed yarns sit outside this and need no special wording: in
practice they are unmistakable on the table.)

Now every unplaceable line becomes an `UnmatchedSale`, whatever the reason (no
`catalog_object_id` at all, an unsynced variation, a custom amount). Erring
toward capture is cheap: a row that turns out not to be a scarf is dismissed in
one click, and **dismissal has to exist** — a queue that can only grow stops
being read.

`private/unidentified-sales/` pairs each open sale with booth photos taken
within **±15 minutes** — the width of a queue at a busy stall. The reported
first six characters of the barcode are the *blank*, not the colorway
(`BLANK-DYEBATH`), which is exactly the narrowing worth having: nobody can read
a colorway off a scarf they couldn't name, but the style is on the tag and it
turns a few hundred products into a few dozen. With no prefix reported the page
offers the whole catalogue rather than pretending to have narrowed it.

**Resolving moves stock, and that is not a violation of "back-dated entries
never move stock".** That rule exists because a backfilled kanban card records
a bath that was already counted. This sale was never applied at all — the
webhook dropped it — so `number_on_hand` has been one too high ever since, and
applying it late is the entire point. The `InventoryLog` row is dated at
Square's sale time rather than the moment someone got round to the queue.

Filing the photo against the product is **opt-in** on the same form: a stall
snap in bad light isn't always what the catalogue should show, but when it is,
the scarf nobody could name becomes identifiable next time.

Related, and fixed while the loop was being closed: the webhook now skips a
line it has already logged for that order. Square sends `order.updated` more
than once and `COMPLETED` is not a one-shot state, so a redelivery used to
decrement the same sale again.

**The queue is worked at pace — a hundred lines, read and clicked — and two
costs were in the way.** Both were structural rather than incidental, which
is why they are worth writing down.

The page asked the **same catalogue-sized question once per row**. With no
photo beside a sale there is no reported barcode, so the honest answer is the
whole active catalogue — and that is the *common* row, so every one of them
asked the identical question and got a separate query. `_resolution_options`
now takes a per-request cache keyed on the reported prefixes and nothing
else, so rows that genuinely differ still narrow and the rest share one
answer.

**Dismissal is an htmx swap**, replacing the row with a one-line strip. The
cost it removes is the same one again: a full navigation rebuilt every
*other* row on the day, each carrying that `<select>`. The row still collapses
to something rather than vanishing — a row that disappeared is
indistinguishable from a click that never arrived, and at pace that is the
mistake somebody makes twice. Two things ride out-of-band with it, because a
header still reading "12 open in total" over eleven rows is the page
contradicting itself: the count, and the orphan-photo list, since dismissing
a sale can *make* an orphan out of the photo that was beside it.

**"Dismiss all like this" is day-scoped, and the key it matched on is in the
button text.** Two keys, and they are not two precisions of one idea — they
apply to different populations, which is the thing to hold onto:

- **A Square variation id** means Square has a catalog object this app doesn't
  know: the unsynced-variation case, and the group *most* likely to be real
  scarves. Precise, and precisely where a bulk dismissal is expensive — it
  writes the sales off and the count stays wrong with nothing saying so, which
  is the silence this queue exists to break.
- **A bare name with no variation id** is a hand-keyed custom amount, and that
  is the group that genuinely never was a scarf. Looser key, safer population.

So **a name match is scoped to lines with no variation id**. Without that,
dismissing every `Custom Amount` sweeps up a row that *does* carry a Square
item — the dangerous group taken by the safe group's button, silently.
`test_a_name_never_sweeps_up_a_line_carrying_a_square_item` is the pin. A line
with neither gets no button: "all like this" would mean "all the nameless
ones", which is a grab bag rather than a group, and one standing only for
itself gets none either, because that is the Dismiss button beside it wearing
a longer label.

The count is **on the button and computed off the day's rows already in
memory** — never a query per row, which is the mistake the same page had two
paragraphs up. Day-scoped means everything the button claims is on the screen
it was clicked from, so the number can be checked by looking rather than
trusted. Every row it takes leaves the page in the one response: the clicked
row is the swap target and the rest are found by id, because a row left behind
reads as one the button missed.

**No undo on the strip**, unlike the Sunday close's. The difference is who is
holding the phone: the close has no login and its users have no accounts, so
a fix they cannot make is a movement that goes unmentioned. This page is
staff, at a desk, already signed in — and dismissal destroys nothing, it sets
a timestamp the admin clears. **Matching stays a full navigation**, because it
moves stock and the sentence saying what moved is worth a page.

The button is an ordinary submit underneath, so with the script blocked the
page posts and redirects exactly as it did.

**The queue is set to no notification.** Jiminy offers three settings per
thing — none, a digest, or told when it happens — and this one is none. That
is a choice, not a limitation: the queue is reconciliation on a weekly
rhythm, so the count is already wrong by the time a row lands and stays wrong
at exactly the same rate whether it's seen in ten minutes or on Monday.
Nothing downstream is waiting on it. Told-when-it-happens would buy nothing
and cost something real — an alert that turns out not to matter is how
someone learns to ignore the next one that does.

Which setting fits follows from the rhythm of the work, so ask that question
per thing rather than assuming this answer generalises. A label run waiting
on a print shop is time-bound and would want telling; this isn't.

What makes none safe here is that the queue can't quietly empty itself. Every
unplaceable line lands in it, dismissal keeps it readable, and it is still
there on Monday. That is the whole of the guarantee, and it is enough.

## The crew handbook: read it, then take your pass

`secret/handbook/` is what the crew are given instead of being told things at
eight in the morning on the first day. It covers the till, the look-up books,
sending photos in and reporting hours, and it ends by handing that person
their faire pass as a PDF.

**The pass is the reason anyone comes back**, and that is the design. A page
of instructions is read once and never again; a page you have to return to
when you lose your pass is a page whose contents stay reachable. It is also
the one crew page that can be relied on to be found, which makes it the right
place to list the other `secret/` URLs — they are off the public map and off
the staff map the crew can't see, so their only way in is a bookmark or a
text message.

**The gate is not protecting the passes.** A pass is a barcode and a
photograph, trivially faked by anyone who wanted one, and everybody who can
reach this URL is getting one anyway. The name and PIN are there to pick
*whose* PDF comes back. That is why there is no attempt-throttle here unlike
the hours form: the thing behind that lock is worth locking, this one isn't,
and a lockout lands on somebody standing at the gate without a pass.

**The button at the bottom is the whole of the scroll enforcement**, plus a
checkbox. Anything cleverer means JavaScript, and JavaScript failing here
means a crew member at the gate on one bar of signal with no pass — the same
reasoning that keeps the booth form's toggle in CSS. A checkbox costs a tap
and cannot fail closed.

**Nothing is recorded.** No read-receipt, no timestamp, no per-season
version. The tick is a speed bump asking somebody to look at the page, not
evidence to be produced later, and storing it would invite exactly that use —
which a checkbox on an unauthenticated page cannot support. The pass is
downloaded and kept; fetching it again just means coming back, and
`crew.initial` has already filled the form in on the phone that fetched it
the first time.

**A missing PDF names a person to contact rather than showing a dead
button.** Nothing the reader can do will make the page work, so the page has
to stop them waiting on it. `EmployeeAdmin` carries a `Pass` column for the
other side of that: the useful question is "who is going to reach the bottom
and be told to contact me", and it is only answerable by looking down the
whole roster at once.

**The PDF is streamed, never linked.** The bucket is private, so an unsigned
`.url` would simply 403 — but even where it wouldn't, a link to somebody's
pass outlives the page that produced it.

### Write it as if it were always true

The handbook carries **no temporal language**: no "new this year", no "we've
changed", no "previously". The reader may have worked here for years and
still never have seen any of it, and a page half-describing a world that has
moved on gives nobody a way to tell which half. A revised date at the top
carries all the time information there is. Sections run in the shape of a day
— the till, the books, photos, hours — rather than in the shape of the
software, because someone reading it cold is working out what happens to
them.

Two things in it are worth keeping accurate because they are the ones that
cost money when wrong: **the variation is the colorway** and a wrong one
balances perfectly at the till while corrupting the count of two colours, and
**the pay week runs Saturday to Friday**, which is what keeps a faire weekend
inside a single pay week instead of splitting it across two.

## The PIN is remembered, and remembering is not authorising

Both `secret/` pages open with "choose your name" and "type your PIN". That is
the price of having no accounts, and it is charged at the worst possible
moment: a scarf has just sold, the queue is moving, the phone is out for about
ten seconds. Friction there doesn't produce a late report, it produces **no**
report — which makes the page worth nothing. So `scarves/crew.py` keeps the
name and PIN in a signed cookie and both pages open pre-filled.

**The cookie fills the form in; it never stands in for the PIN check.** Both
forms still compare the submitted PIN against `Employee.pin` on every POST,
unchanged. The cookie writes `initial` and nothing else. That is the whole
safety argument, and it is the same shape as `colorbands`: it fills the form
in, the check still happens. A cookie that *authorised* would mean a found
phone submits with nothing checked anywhere, and it would need its own expiry,
revocation and threat model. A cookie that only types for you can be stale,
wrong or forged and the worst outcome is the error message a typo already
gets. `CrewCookieTests` pins that a POST carrying the cookie and the wrong PIN
still fails.

Which is why the PIN itself is in there rather than a token standing for it.
Signing stops tampering, not reading — anyone holding the device can read the
cookie. The PIN was never a secret (it stops the wrong name being tapped), so
storing it somewhere readable-with-the-device costs nothing it was protecting,
and it keeps one code path instead of two.

A cookie outlives the facts in it, so every read resolves against the database
and drops what no longer holds — quietly, because this is a page nobody has
typed into yet and an error on it is noise. Three cases, and the split in the
third is the useful one:

| Cookie says                    | What happens              |
|--------------------------------|---------------------------|
| tampered / unsigned            | forget the lot            |
| employee gone or now inactive  | forget the lot            |
| PIN has since changed          | **keep the name**, drop the PIN |

The name is still right, so the page still knows who this is and asks for the
one thing that actually changed.

**Everyone uses their own phone.** There is no shared stall tablet, and that
is what makes remembering the *name* safe rather than dangerous — on a shared
device a pre-filled name is exactly how one person's hours get filed under
another, silently, and correctly built it would need a much louder confirmation
than a link. If a tablet ever appears, revisit this before anything else.

Even on personal phones the pages **say** the fields were filled in for you and
offer `?forget=1`. Phones get lent, handed over and replaced; a pre-filled name
that nothing mentions is unrecoverable by the person looking at it. The link is
a GET with a side effect, which is fine here because the side effect is this
browser's own cookie — idempotent, nothing written, nothing to re-submit.
