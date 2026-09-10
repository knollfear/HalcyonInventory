# Barcode labels and photographing stock

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Barcode labels: the sheet is the state

`private/labels/` prints Code128 stickers for stock going onto the shelf —
"everything produced since a date" (the weekly job) or "everything on hand"
(bulk re-label). `scarves/labels.py` picks the items and draws the PDF;
`LabelStock` holds the paper.

**A dye bath is one blank plus one recipe**, so it yields 3–5 units of a
*single* SKU. That's why the sheet is sorted by SKU and nothing cleverer: the
stickers come off in the same clumps the scarves come off the rack. A run is
~20 products × 3–5 ≈ 60–100 labels, against 80 per sheet.

**The run prints its own "start here next time" sticker.** A weekly run leaves
a part-used sheet nearly every time, and a sheet nobody can confidently resume
gets binned — which reads as waste whatever it cost. So the last label of every
run is a dated marker reading `NEXT RUN: START AT n`. It costs one label and it
puts the state on the paper, where it survives a cleared cache, a second laptop
and a week in a drawer. The browser's `localStorage` pre-fills the same number
as a convenience, and the page says so: **if the two disagree, the sheet wins.**

Two cases print no marker, both on purpose: a run ending exactly on a sheet
boundary (nowhere to put it but a fresh sheet, which starts at 1 anyway), and a
continuous roll (`LabelStock.is_continuous`, a 1 × 1 grid — no sheet to resume,
so a marker would cost a label per run and be read by nobody).

**Never print a barcode below `MIN_MODULE_MIL`.** Bars too dense to scan are a
*silent* failure: the sticker looks right and fails at the till with a queue
behind it. `density_problems()` runs before rendering and the view refuses the
whole job, naming the SKUs. This is also why the stock is 1.75in wide — a
13-character `SLUG6-SLUG6` SKU comes out at 7.7 mil there, where the 1in stock
first considered would have printed 4.5 mil. Barcodes are sized **per label**,
not per run, so a short SKU gets fat bars instead of matching the longest one.

**Three datasets, and the hand-picked one is additive.** "Specific items I
pick" is a type-ahead you add "this SKU, this many" rows to — deliberately not
tick-to-exclude on the preview table, because unticking 297 rows to keep 3 is
worse than typing 3. It reuses the upload page's `product_search` endpoint via
`?mode=labels`, which swaps the result template — the search is identical,
only the click behaviour differs.

**A SKU is write-once.** It has been printed since the reference sheets
existed, it's now on stickers stuck to scarves, and Square holds it to
identify a variation — this app can rewrite none of those. So
`FinishedProduct.save()` only ever *fills a blank* SKU, never changes one, and
`generate_skus --overwrite` states how many it would change and asks for
confirmation (`--noinput` for scripts). The symptom of getting it wrong is an
item scanning to nothing at the till weeks later.

**SKUs are assigned at creation** (`FinishedProduct.save()` → `scarves/skus.py`).
Generation used to live only in `generate_skus`, so anything made through the
admin, the bulk matrix or a shell had no barcode until somebody remembered to
run it — which is how unprintable products accumulated. The command stays, for
backfill. Fixtures are unaffected: `loaddata` goes through
`save_base(raw=True)` and never calls `save()`, so a deliberately blank SKU
stays blank (`FixtureSkuTests`). In tests and data repair, a blank SKU can now
only be made with a queryset `update()`.

Related ordering, for a fresh sync: **`generate_skus` first, then
`sync_to_square`.** The sync omits the `sku` key entirely when it's blank, so
syncing first creates Square variations with no SKU and nothing to scan. The
update path does send it, so a re-sync afterwards repairs that — but only if
someone knows to run one.

**Only known SKUs can be printed, and a product with no SKU is shown
disabled rather than hidden.** There's no free-text barcode entry: an unknown
code scans fine and then Square finds nothing, which fails at the till with a
customer waiting and no way to tell a typo from a missing product. The
legitimate case barely exists here — nothing sells through Square without
being a `FinishedProduct`, and production is recorded in the app before labels
are printed — so the escape hatch is "add the product", which has to happen
anyway. A no-SKU product still appears in the results, greyed out and saying
`run generate_skus`, because filtering it out silently means someone searches,
doesn't see it, and never learns why.

**Extras don't apply to a hand-picked run.** The bulk datasets add spares
because their counts are derived and slack is cheap; here somebody typed the
number, so printing more than they asked would surprise. The page hides the
extras box for that dataset rather than leaving a control that does nothing —
the same `data-when` mechanism that hides the date box when printing
everything on hand.

Adding a fourth dataset stays cheap: everything downstream (SKU sort, marker
placement, density guard, sheet plan) operates on `LabelRun.rows`, which is
just product-and-quantity pairs. It's one function returning a `LabelRun` and
one branch in `_label_run_from`.

**The whole-catalogue export starts each blank on a fresh row; nothing else
does.** SKUs read `BLANK-DYEBATH` and sort alphabetically, so a stack of
sheets is already grouped by blank — padding to a row boundary at each change
just stops the seam falling mid-row when the stack gets split up. It's keyed
on the SKU prefix, not the finished product: 266 products would cost a partial
row each, several hundred labels.

Off for every other run, and that's a ratio judgement rather than a
preference. The padding is ~20 labels either way. Across the 31 sheets of a
full export that rounds to nothing; across a 3-sheet weekly run of ~20
products over ~80 labels it's a quarter of the job.

**A label is barcode and SKU text, and that's the whole design.** Branding
lives on the printed hang tags, which are a separate physical thing — so
there's no artwork to fit here, and the austerity is the decision rather than
an unfinished job. It also keeps the stock small and the printing black and
white, which is what makes a weekly run cheap.

**Nobody here owns a printer.** Sheets get printed at a copy shop from a PDF
emailed off a phone, which breaks two assumptions the offsets were built on:
you can't calibrate the machine beforehand, and you have no computer with you
when a sheet comes out 2mm high. So `x_offset_mm`/`y_offset_mm` can be
overridden from the query string (`_label_stock_from`) — adjust on the phone,
re-download, reprint — and an override is **never** written back, because a
correction for one store's machine on one day is not a property of the paper.

For the same reason every sheet prints **registration ticks down the left
margin, one per row of die-cuts**. A dialog left on "fit to page" takes a few
percent off, and that failure is progressive — at 98% the first row is out by
a twentieth of a millimetre and the twentieth is out by five, cut clean
through. A short ruler cannot catch it (98% of an inch is half a millimetre,
unreadable); twenty die-cuts spread over ten inches can, and the two faults
separate by eye: ticks drifting further off toward the bottom means scaling,
ticks all off by the same amount means registration and a nudge fixes it.
Ticks stay left of the first column and the caption sits on the liner, so this
costs no labels. Rolls and stocks without the margin skip it.

The calibration sheet carries a **millimetre vernier through the first
label's corner** so dialling the nudge in is one measurement rather than a
loop — lay it over a label sheet against a light, read the corner off the
scales, type those numbers in. The scales are labelled with the nudge to
*enter*, not the error observed, because a sign error there doubles the
problem it was meant to fix.

**`LabelStock` is eight numbers, not an uploaded template.** Vendors ship
templates as .docx/.pdf, and recovering die geometry from one means
reverse-engineering Word's cell rounding — with 2mm of error costing a sheet to
discover. Instead the numbers are transcribed off the vendor's spec page into
the admin. `overflow_in()` and `clean()` reject geometry that runs off the
sheet (what a transposed pitch digit looks like), `LabelStockGeometryTests`
re-checks every seeded stock, and the calibration route prints outlines plus a
one-inch ruler on plain paper to catch printer registration and a print dialog
left on "fit to page".

## Photographing stock: the barcode misses, and the pile doesn't

`private/images/upload/` files a photo against a `FinishedProduct` by reading
the Code128 on its tag. In a real session of forty photos **about half of them
don't read** — a phone camera, a small symbol, whatever the light is doing —
and each miss then costs a product name typed out in full, one-handed, next to
the pile.

**So the page asks what you are photographing, once.** A session is a pile of
*one blank*: forty half circle veils, then forty sash belts. That is the blank
half of `BLANK-DYEBATH`, it is known before the first shot, and it stays true
for the whole pile — so it is typed once instead of forty times. A failed
decode comes back with `HALFCI-` already in the search box and the search
already run, which has narrowed a few hundred products to that blank's forty
colorways; two or three letters of a colour name finish it.

Three things keep that honest, and they are the same three the rest of the app
makes:

- **It fills the form in; it never files anything.** A read barcode still
  wins outright, whatever the menu says — the barcode is evidence and the
  menu is a statement of intent. Same rule as `colorbands` and the crew
  cookie.
- **It is a prefill, not a filter.** The box is ordinary editable text, so
  shooting something off-pile costs one clear. Nothing is hidden from the
  search, which is what would make an off-pile scarf unfindable.
- **The answer is read at send time, not at page load**, because the pile
  changes partway through a session and a stale answer would quietly prefill
  the wrong blank.

The menu offers only blanks with an active product under them, and the value
is `skus.slug(name)` — the same function the SKU was built from, so a prefill
that stopped matching would mean the SKU rule itself had moved. The submitted
value is run back through `slug` on the way in.

### Walking a display instead: the peg is the identity

`private/images/display/` inverts the identification problem rather than
solving it. A peg *is* an identity — the map already says Artisan — Crocodile
hangs at row 3, column 4 — so a photo taken at a known stop needs no barcode,
no typing and no search. The walk says what to shoot, you shoot it or skip,
and either answer moves to the next peg.

**Two routes through it, and they run in opposite directions.**

- **Map first.** Fill the map in the editor, then walk: each stop names the
  colorway and the photo files itself with nothing typed.
- **Photos first.** Build the fixture in the admin, walk the bare board, and
  at each peg photograph what is hanging there and pick the colorway — which
  goes *on the peg* as well as on the photo. One walk produces the pictures
  and the map together.

The second is what the ranking is for. The photo just taken is classified by
`colorbands` and the candidate list is ordered against each colorway's
confirmed bands: **exact set, then any superset, then any overlap, then the
rest alphabetically.** A superset of five extra bands sits with a superset of
one — the count of extras is not a penalty, because a scarf with a lot going
on is not a worse match for the blue and green in the photo.

Four things hold it honest:

- **Colour orders the list; it never picks.** A band set is not an identity —
  dozens of colorways are blue-and-green — so this moves the answer near the
  top and the person holding the scarf does the rest. Photo dominants
  classify right about 4 in 5 times, which is plenty for ordering a list
  somebody reads and nowhere near enough to file on.
- **Only confirmed bands rank.** An unreviewed guess ordering the list would
  look exactly like a reviewed one, so unconfirmed colorways fall to the
  alphabetical tail — and the card **says how many could be ordered at all**,
  because a list that fell back to alphabetical looks identical to one where
  the photo matched nothing and only one of those has a fix.
- **The stop beats a barcode that disagrees, and the disagreement is
  reported.** The batch page's blank picker is a coarse statement covering
  forty photos, so there a decoded symbol is the better evidence. A stop is
  the opposite: made per photo, at the peg, by somebody looking at the scarf —
  while a symbol that resolves in shot may belong to the colorway hanging two
  inches to the left.
- **An occupied peg is never overwritten**, the same refusal
  `copy_board_layout` makes. Assigning is only ever filling a blank.

**Where you are is the URL and nothing else** (`?row=3&column=5`). Fifteen
photos in, get distracted, come back to the peg — and an address that is no
longer a stop advances to the next one that is, so a bookmark taken before the
board was rearranged resumes rather than failing. Nothing stores progress: a
cursor would be a second place the answer lived, and the one it disagreed with
would be the one somebody was looking at.

Reserved spaces are not stops — the price tag is not a colorway nobody got
round to photographing. Empty pegs are, because a photo taken at one is how
the map finds out what is hanging there.

The uploader itself is one script (`partials/uploader.html`) driven by data
attributes, because the batch page and the walk differ only in what rides
along with the process call and what happens afterwards. A second copy with
two lines changed is what would drift, and the way it would show is one of the
two pages quietly uploading to the wrong place.
