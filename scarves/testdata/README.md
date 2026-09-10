# Test photographs

Real photographs, kept because the thing they test is optics and a synthetic
one cannot stand in for it: a cleanly rendered QR decodes at two pixels per
module, while a real one fails from focus, curl, glare and the angle a page
was lying at. Every attempt to reproduce `marked_sheet_run5.jpg`'s failures
with a generated image produced a picture that decoded on the first pass.

## `marked_sheet_run5.jpg`

An iPhone 16 Pro photograph (3024×4032) of a marked reporting sheet for
production run 5, twelve rows, all of them filled in by hand.

**Its token is dead.** The sheet's code is legible in the photograph and in
its QR, so it was rotated in the admin before this file was committed — this
repository is public. Anything else committed here has to clear the same bar:
a production sheet carries a token that opens a page which moves stock with no
login, plus a page of stock levels. Check the code is dead before adding one,
and check it the way a stranger would — fetch the URL, against a control that
is known-bad, because a 404 you did not verify is a 404 you assumed.

## `screenshot_sheet_run5.jpg`

The same sheet, but a screenshot exported through Preview rather than a camera
file — 1308×1732, and the EXIF gives it away: no Make or Model, XResolution
144. It is here because it is the *other* failure, and because mistaking it
for a photograph produced a wrong conclusion that survived until the camera
original turned up.

At this size a 7.7 mil barcode module lands on 1.18 pixels against the two
zbar needs, so **no row can be read from it at all** — thirteen decode
attempts, including 4x upscaling with unsharp masking, read zero. The QR
survives, so the sheet still names itself and the boxes get tapped by hand.
That is `too_small_for_rows` and `named_but_unread`, which exist so the app
can tell somebody to stop retaking a picture that can never work.
