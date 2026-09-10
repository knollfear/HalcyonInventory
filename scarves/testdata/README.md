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
