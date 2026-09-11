# Recipes, dyes and colour bands

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Dye entry: the list you can read, and the dye that isn't on it

The dye boxes on `private/quick-recipes/` and on an open row of
`private/recipes/`
are one control, `DyeSelect` plus `partials/dye_picker.html`. Two failures
put it there, and both are quiet.

**The catalog number ruins the alphabet.** Dharma and Jacquard number their
dyes, so `416 Peacock Blue` sorts under 4 — an alphabetical list comes out in
catalog order, and the browser's own type-to-jump matches the number too.
`Dye.sort_name` drops the number for sorting and searching; the number stays
on screen, because it is what is printed on the jar. Sorting happens in
`DyeSelect.optgroups`, not the queryset, so the regex lives in one place.

**A dye that isn't on the list stops entry.** What actually happens then is
the recipe gets typed with the dyes that *were* on the list, and the missing
one is lost — silently, because the recipe looks filled in. So the picker
offers to add one, and `NewDyeForm` takes a name and nothing else. Brand and
colour are real questions, but they are not answerable at speed with wet
gloves on, and demanding them buys a tidy row at the price of no row at all.

Three things make that deferral safe:

- **A new dye has no hex, and blank is honest.** `Dye.hex_color` used to
  default to red, which reaches the rainbow sheet, the games and the
  dye-collection page as a fact nobody typed. Blank contributes nothing
  anywhere instead: no band from `colorbands`, no point in
  `colorutils.recipe_palette`, an empty chip on the production sheet. Same
  bargain the colour classifier makes.
- **`Dye.needs_review` is what makes the unfinished half findable**, and the
  admin's dye list filters on it with `list_editable` for the colour, brand
  and stock flag — a dozen rows on one screen rather than a dozen round
  trips.
- **`import_dyes` finishes them off wholesale** (below).

**Duplicates are the thing to guard.** `dye_match_key` decides whether two
names are the same dye: it drops the catalog number, a trailing `(Primary)`
and a trailing mark, because those are exactly what someone typing from
memory leaves off. Ten of the 84 acid dyes carry the tag, so getting it wrong
duplicates the most-used dyes in the range. The picker checks it to decide
whether to *offer* the add row; `NewDyeForm.find_existing` checks it again on
the way in and hands back the dye that exists rather than erroring — an error
would leave the slot empty and the person retyping a name that was right.

The same reasoning made both pickers offer **out-of-stock dyes**. Hiding them
was survivable while the list was take-it-or-leave-it; now a hidden dye is one
somebody types in again, and the second `Peacock Blue` splits a history that
reads as complete on either row.

The plain `<select>` is still what posts and what `ModelChoiceField`
validates — the script only puts a text box in front of it. With the script
blocked the page is the same form with a longer list. Rows swapped in by htmx
re-enhance on `htmx:afterSwap`, which is why the picker is included *after*
htmx on the showcase.

### `import_dyes`: re-importing the catalog over a live list

`loaddata` cannot do this. The fixtures carry primary keys and `RecipeDye`
points at a dye *by* primary key, so loading `dharma_dyes.json` over a
database whose pks drifted rewrites the dye pk 7 refers to — every recipe
using it changes colour, with no error and no clue, and the symptom is a
scarf on the reference sheet under a band it was never dyed in.

So `import_dyes` matches on content:

```
python manage.py import_dyes scarves/fixtures/dharma_dyes.json \
    --brand "Dharma Acid Dyes" --dry-run
```

The two catalogs on disk are `fixtures/dharma_dyes.json` (84) and
`fixtures/jaquard_dyes.json` (48). They are read here as data, not loaded as
fixtures — see above for why that distinction is the whole point.

- **A colour already on file is skipped**, whatever it is called. That is the
  rule that makes a re-import safe to run twice: a name tidied up by hand
  must not come back under the catalog's version of it.
- **A name already on file with a different colour is a conflict, and is left
  alone and named.** The colour in the database was put there by a person;
  the file is a catalog scrape.
- **A name on file with *no* colour gets filled in** — colour, catalog number
  and brand — which is precisely the picker's half-finished row being
  completed.
- Name matching is scoped to the brand being imported plus the uncategorized
  pile. Jacquard's `Peacock Blue` is a different jar from Dharma's 416.

`--dry-run` prints the whole plan and says when `--brand` would create a new
brand, because a typo there splits the range across two brands with nothing
to say they belong together. It reads either shape: a Django fixture, or a
plain `{name: hex}` map, which is what a fresh scrape off a supplier's page
looks like before anyone has made it into anything.

## Rainbow bands: never print an unconfirmed guess

`Recipe.color_bands` says which sections of the rainbow reference sheet a
colorway prints in. A recipe claims one or more and prints in every one it
claims — a red-and-orange scarf appears under both, on purpose. That follows
from the dyes not being blended (same principle as `colorutils`): averaging a
recipe to one band files it under a color that appears nowhere on the cloth.

`scarves/colorbands.py` classifies a color using **three axes**: hue picks the
band, saturation asks whether it's a color at all, lightness catches the tints
and shades nobody names by hue. Hue alone is confidently wrong a lot — it calls
`#000000` red and `#000001` blue, `Slate` blue and `Ivory` orange.

**Nothing in `colorbands` ever writes to the database.** It fills the form in;
a person decides. `bands_confirmed_at` is null until someone confirms on
`/scarves/private/colors/`, and the sheet must skip those rows. The reason is
the failure mode: a wrong band is *silent*. You look in the orange section, the
scarf isn't there, and nothing tells you it was filed under red — so an
unreviewed guess is worse than no entry. Roughly 85–90% of swatch hexes and 4
in 5 photo dominants come out right, which is nowhere near good enough to print
unread.

Two judgements are baked in and are not bugs:

- **No indigo.** The blues in stock run 219–248°, the violets 254–277°. Indigo
  has no territory between them, so the section would be empty or arbitrary.
  Pink and brown are sections instead, because both are what someone actually
  says out loud about a scarf.
- **Yellow ends at 61 degrees, not 70.** Sorted by hue the catalogue has an
  empty corridor from 69.2 to 79.3 — no dye lives there — so a boundary
  anywhere inside it classifies nothing, which is how 70 survived without ever
  being examined. Just below sit five jars everyone reads as green: Lichen
  (62.4), Chartreuse Neon (62.6), Avocado (64.9), Radioactive (66.6) and
  Chartreuse (69.2). The nearest true yellow is Fluorescent Lemon at exactly
  60.0, so 61 takes the cluster and leaves the yellows a degree of room.
  Beware that `band_for_hsl` holds a **second, unrelated 70** — the cream rule
  that keeps Ivory out of yellow. Moving one with the other is the mistake.

- **Rainbow is a section, and it is the one that isn't a hue.** It exists for
  the reason pink and brown do: it is what somebody says out loud about a
  scarf. Nobody asks for one with red, orange, yellow, green and blue in it —
  they ask whether you have a rainbow, and then which rainbow. Both answers
  without it are bad and the shop was living with the worse one: claim every
  band and four colorways print in all eight sections, each time as the least
  useful answer to the question that section asks; claim none and they print
  nowhere, which is what was happening — two of the four filed as
  confirmed-with-no-bands, arrived at by giving up, and seventeen active
  products in no section at all.

  The classifier folds a spread of **five or more** chromatic bands into
  `rainbow` alone, neutrals included. Five sits in an empty corridor the same
  way the yellow/green boundary at 61 does: confirmed colorways in stock top
  out at four bands, and both that reach four are emphatically not rainbows
  (Forest Fire, Mooney), so the line reclassifies nothing that exists. The
  fold lives in `bands_from_dyes` and `bands_from_image` and **not** in
  `color_bands_save` — same division as the neutral rule, so a warm rainbow
  that genuinely reads red can claim both, because that is a judgement about
  the scarf. Nothing ever classifies a *single* colour as rainbow; it is a
  property of a set, which is why it is out of `CHROMATIC`.

  Its printed tab draws the spectrum in stripes with a white label plate
  rather than picking a stand-in colour — a tab is read fanned, from the edge,
  and one flat colour there would be a lie in the one place tabs are used
  without reading. The classification page's chip does the same in CSS. The
  three or four kinds of rainbow are separate recipes and need nothing: a
  "kind of rainbow" field would be the fiber-field mistake.

- **Neutral only claims a recipe when it is the *only* band.** Black, grey and
  cream are working dyes that ground the colors beside them. Every neutral-ish
  recipe in stock reads as something else too (`turq-mid-black`,
  `grey-forest-navy`), and nobody looks for those under grey.

**There are two places to confirm bands now**, and they differ in one
deliberate way. `private/colors/` is the dedicated pass, where Confirm is the
only button and a suggestion therefore arrives **pre-ticked**. The recipe
showcase's editor row carries the same chips beside the dyes, where Save is
about the dyes, so a suggestion arrives **unticked** and ticking nothing leaves
the colorway unconfirmed. See *`private/recipes/`: one editor open at a time*.

### Two axes on the confirmation page

`private/colors/` filters on **confirmed-or-not** and **has-an-active-product
-or-not**, and they are separate questions rather than one row of alternatives.
A colorway with no active product prints on no sheet and hangs on no peg, so
confirming its bands changes nothing anybody can see today — still worth doing
eventually, which is why it is filtered rather than dropped. The pair that
matters is `?todo=true&with_products=true`: colorways a customer can ask for
that the sheet is currently leaving out.

Both filters ride in the query string and **every pill carries the other axis**
rather than resetting it, so that pair is two taps and a link somebody can
send. **Counts are scoped to what is on screen** — a pill reading "Unconfirmed
57" over a list of nine is the page contradicting itself, and the number people
act on is the one beside the list they are reading.

Worth knowing where the work actually sits, because it is not where you would
guess: the dye entry has gone mostly into recipes with *no* active product (the
dye book), while the bands have been confirmed on the ones that sell. So bands
are the well-covered signal on sellable colorways and linked dyes are not.

### One category, two orderings

A category builds two sheets off **one picker** — `public/reference-sheet/` —
because Yarn and Silk is how the stall is laid out, and a sheet is printed per
table. The card carries a button each, not a second directory:

- `<id>/` — **by colorway**. A page per colorway, carrying the barcodes of
  every style dyed in it. Answers "what does this colorway look like, and what
  can I scan it as?".
- `<id>/by-color/` — **by colour**. The same pages, ordered by the rainbow and
  repeated once in *every* band the colorway claims. Answers the question a
  customer actually asks: "what have you got in red?".

The category comes ahead of the ordering in the path on purpose: it puts the
by-colour route under the existing picker, which is what
`PickerPageConventionTests` checks and what keeps the pair one card on the site
map instead of two.

Duplication is the feature, not waste. A red-and-blue scarf that printed once
would be *missing* from one of the two sections it is genuinely in, and the
absence is silent (see above). The cost is bounded by how many bands a recipe
claims — the picker prints the page count of each sheet, so the ratio is
visible before you build the PDF.

The picker also **says how many colorways the colour sheet will leave out**,
rather than quietly printing a shorter sheet. Unclassified means printed in no
section at all, and the only symptom on paper is a scarf nobody can find.

**Each page carries its band twice**: named under the title, and as a colored
tab in a fixed slot down the right edge. The tab is what makes a printed stack
usable — fan it and the sections show from the edge, which is how someone at
the stall finds red without reading. Slots are keyed to the band, not to the
page, so a gap means "this category has nothing in green" rather than "the tabs
shifted". Tab text flips black or white by luminance, because the sheet gets
photocopied.

Printed tabs replaced buying physical index tabs. They cost nothing (the sheets
already print in colour), and they can't be lost, misapplied, or forgotten by
whoever reprints the sheet — the same reasoning that puts `NEXT RUN: START AT
n` on the label sheet instead of in `localStorage`.

## `private/recipes/`: one editor open at a time, and editing is not a mode

The colorway list, and where the dye backlog gets filled in. **Every row
carries its own Edit button and the page has one state.** It used to have two,
behind `?edit=true`, and that was a decision demanded before the job: you came
to look at a colorway, found a dye you wanted to change, and had to go back to
a pill and reload the page into a different version of itself to be allowed to
touch it. Nothing about a mode was earning that — the pickers are per row
either way.

**What the mode was really protecting was the render cost, and that is fixed
at the root instead.**

Worth recording the arithmetic, because the instinct is to look for a repeated
query and the repeated query was the small half. A `DyeSelect` offers the whole
catalogue, and each `<option>` carries the colour and search text the
type-ahead reads off it — about **225 bytes**, so one picker is **~30 KB** and
a row is five of them. 162 rows is **810 copies of the same list**: 121,000
options, and 810 identical `SELECT * FROM dye` because Django's
`ModelChoiceField.queryset` setter calls `.all()`, which clones and drops the
result cache.

**The queries were noise and the markup was everything.** The profile put ~97%
of the request inside form-widget rendering. And 27 MB gzips to 1.15 MB, so the
wire was never the problem either — what hurt was the two things that don't
compress: the server CPU building the options, and the browser building 121,000
DOM nodes.

So **a row renders its editor only when asked for**. Opening one is an htmx
swap of `recipe_row`, or `?row=<pk>`, which the Edit control carries as its
`href`. Measured, at the real catalogue size:

| | before | after |
|---|---|---|
| the list | 13.58s, 819 queries, 27 MB | 0.04s, 9 queries, 0.23 MB |
| one row open | — | 0.11s, 15 queries, 0.40 MB |

**`recipe_row` is closed by default and opens on `?edit=1`**, which makes one
endpoint both halves of the toggle. Closed is the default because of what a
dropped parameter does either way: lose it and you get the row as the page
already reads, which is harmless, where an editor default would spring five
pickers open on a row nobody asked to change.

**Cancel closes the row, and closing *is* the reset.** Nothing on an open row
was written — the pickers hold a form, and a form thrown away leaves the recipe
exactly as the closed row shows it. It used to re-render the pickers at their
stored values, which reached the same place by a longer route and left the row
looking like it was still mid-edit.

**There is no copy-dyes-from picker.** It sat above the dye boxes on every open
row, offering to prefill from another colorway, and it answered a question
nobody was asking there: the row is for *this* colorway, and a second recipe
named on it read as though it were part of the record.

**`?row=` is parity, not a promise.** Save and Reset here have always been htmx
buttons, so a script-blocked visitor could never write a dye on this page
anyway. What the parameter preserves is that the way in is a *link with an
`href`* — which cannot fail the way a click handler on a table that got
reworked underneath it can, the bug the production picker already has on record
— and that a row is reachable and readable without the script. The dye form
that really does post without one is `private/quick-recipes/`.

**A saved row comes back closed**, the same as a cancelled one. Two reasons,
and the second is the one that matters: pickers that reappear identical are the
weakest confirmation there is, where the closed row shows the dye chips and
swatches just recorded, so the save is checked by looking at the colours. And a
pass down 162 rows that left every editor open would put back, one row at a
time, exactly the weight the page stopped paying up front.

The htmx and dye-picker scripts load on every visit rather than behind a mode,
because any row can be opened at any time. No picker is on the page until one
is, so the cost is the script itself and nothing else.

**The row partial takes one `row` variable**, not eight loose ones on the
`{% include %}` tag. The failure mode of the loose list is a key added in the
view and forgotten on the tag, which renders as an empty string rather than an
error — a missing chip row would look exactly like a colorway with no bands.

### The rainbow chips ride the row's Save, and arrive unticked

The bands are editable here as well as on `private/colors/`, because the
colorway is in front of you and its dyes are in the boxes above — that is the
moment somebody can answer which sections of the sheet it prints in. One Save
for dyes, oven flag and bands: the same argument the oven checkbox already
made, which is that three controls with three buttons is three trips through
162 rows.

**But the classifier's reading is shown unticked here, and that is the
difference from the colour page.** There, Confirm is the only button, so a
pre-ticked suggestion is answering the page's one question. Here Save's subject
is the dyes, so a pre-ticked guess would be confirmed by a click aimed at
something else — and a wrong band is the silent kind of wrong. A dashed chip
therefore means "the dyes read as this, tick it if that's right" rather than "a
machine has already ticked this for you". Solid ticks are only ever what a
person stored.

**Ticking nothing leaves the colorway unconfirmed.** `_save_editor_bands` will
not manufacture a confirmation out of silence: an empty answer here is
indistinguishable from somebody who opened the row to fix a dye and never
looked at the chips, and *confirmed with no bands* is a state this app has been
in before, arrived at by giving up, which prints the colorway in no section at
all. The deliberate "this belongs in no section" answer still exists — it is
`private/colors/`, where Confirm is the only button and pressing it means
exactly that. A colorway **already** confirmed keeps its stamp and can be
cleared back to nothing, so saving dyes on one is idempotent rather than a
quiet un-confirmation.

The caption under the chips is drawn **only while there is a guess on screen to
explain**. A colorway somebody has already ruled on is not being asked
anything, and a line of help under its own ticks reads as a warning about them.

A closed row draws **confirmed bands only**, and badges the rest `bands
unconfirmed`, for the same reason the reference sheet skips them: a chip nobody
agreed to is indistinguishable from one somebody did. Showing it on the list is
what makes the work findable — a page that offers the function without saying
which rows still need it makes you open every row to find out.

**Bands and dyes share one column, and the dye's hex rides inside its own
pill.** There were three columns for this: dye names, dye swatches, and later
the bands. The swatch and its name sat in *different table cells*, so a
five-dye recipe asked somebody to count across a gap to work out which colour
was which — inside the pill there is nothing to line up. And they answer one
question between them, so they are one column: **bands first, because they are
the answer, then the dyes, which are the ingredients.**

**The band chips are filled and the dye pills are not**, which is what keeps
them apart at a glance now that they sit together — near-identical markup in
adjacent cells became near-identical markup in the same cell. It is also the
language the editor already speaks: a filled chip is one a person ticked. The
dots are deliberately large for their pills, because the colour is the thing
being read on a page about colour and the name beside it is the caption. A dye
with no hex on file still gets hatching rather than a colour, same as in the
picker — a placeholder swatch is a guess somebody then reads off the screen as
fact.

**Edit gets its own column at the trailing edge**, so the button is in the same
place on every row. Above the chips it moved down the cell as a colorway gained
dyes.

**Render only `form.dye_fields` in the pickers, never `{% for field in form %}`.**
That renders every field, and `oven_dyed` is a declared attribute while the dye
slots are added in `__init__` — so Django orders it first, and the row came out
with a stray checkbox in front of the dye boxes which was the *same field* the
oven label below already renders. Two inputs sharing one name is worse than
untidy: unticking the visible one while the stray stays ticked still posts
`on`.

### Which table at the stall

`?category=Yarn` narrows the list to colorways with an active product on that
table. Category means which table, which is why the reference sheets print per
category.

**It narrows which colorways are listed and never which products a listed
colorway shows.** A colour dyed on a yarn and on a silk is one colour: it
appears under both tables, and on either of them the row shows the whole
colorway. Filtering *within* the row would put a different set of products
under the same recipe name depending on how you arrived, with nothing on the
row to say so.

The usual four rules apply, and each one is a mistake this app has made
somewhere else: the pills are **derived from the rows** rather than a list of
names, so a third table needs no code; a list whose colorways all sit on one
table **draws no pills**, because a filter offering one choice is furniture;
**counts are scoped to what is on screen**, since "91 of 162" printed over a
list of forty is the page contradicting itself; and **every control carries the
rest of the state** — the mode pills, the table pills and every row's
`edit_row_url` — built in one `_showcase_url` because four templates each
remembering to re-add three parameters is four chances to drop one silently.
The `.distinct()` matters: a colorway on three yarns joins three rows, and
without it the page prints it three times, each separately editable. Verify
that by iterating, never by counting — `.count()` wraps a `distinct()` in a
subquery and reports the right number while the query returns a row per match,
which is how the season page shipped eleven thousand pills.

## The recipe page: one colorway, and which blank you're reading

`private/recipes/<pk>/` is a colorway end to end — every blank it is dyed on,
and the inventory history underneath. **There is no per-finished-product page
anywhere in this app**, which is what the history's chips are for: a colorway
on four blanks otherwise gives one interleaved column, and "what has this one
actually done" has no answer.

Chips over `?product=<pk>`, not a table per product. The combined view is the
one that shows a dye session across three bases *as* a session, and splitting
by default would give that up to solve the other half. In the query string
like every other filter here, so a reading is a link somebody can send, and an
unreadable or unknown id falls back to the whole colorway rather than erroring
— a filter is navigation, and the worst a stale link should do is show more
than was asked for.

Two counting rules come straight off the colour page's pills. **Every figure
above the history follows the filter** — a lifetime total that disagrees with
the list under it is the page contradicting itself — and the notice saying
which product is in force sits *above* the figures, because it governs them.
**Each chip counts the rows it will show**, so a chip promising 42 can't land
on a list of nine. Both counts come from one grouped query, never one per
chip.

An empty filtered history says *which* silence it is: "nothing recorded for
this one, the rest of the colorway may have moved" is a different fact from
"nothing anywhere", and only one of them has somewhere else to look. No chips
at all when there is a single product, because a filter offering one choice is
furniture.

**A chip click swaps, it doesn't navigate.** The chips sit under a long page,
so a link meant landing back at the top and scrolling down again for every
filter — paid on every click, the same shape of cost the unidentified-sales
queue had. `recipe_history` returns the chips and their rows for the primary
swap, and **the figures and the focus note ride out-of-band with them**: those
are above the fold and follow the same filter, so leaving them behind puts a
colorway-wide total over a one-product list. That is the contradiction the
scoping rule exists to prevent, made worse by being invisible until somebody
scrolls up.

`hx-push-url` carries the **page** URL, never the fragment's — pushing the
fragment's would leave an address in the bar that renders a bare table on
reload, and the sendable-link property is the reason the filter is in the
query string at all. The chips keep their `href`, so with the script blocked
they are the ordinary links they always were.

### Editing par: a mode, and the only place par has ever been editable

`?par=1` turns the finished-products table from the production form into a par
form. Par is the number this app treats as a fact and never was one — it reads
across the catalogue as a uniform remnant rather than as forty decisions about
demand, which is why `private/production-needed/` orders on sales instead and
says so on the page. Until this the only ways to change one were the Django
admin and a bulk action that writes **every** colorway on a blank at once. The
number most in need of a person had the fewest doors.

**It is a mode, and the showcase's "editing is not a mode" does not apply.**
That ruling was about render cost: there, opening a row *is* the job, and the
mode was charging a page reload for permission to do it. Here the two jobs are
different jobs, and the reason for the mode is structural rather than
performance. Bath boxes and par boxes would live in the same rows, so one
table carrying both means a par typed in and then abandoned by pressing
**Record production** — written nowhere, said nothing about. That is the
back-date disclosure again: one table, two meanings, and the losing one
silent. In par mode the bath boxes are not rendered and the form's action is
the par endpoint. One mode, one form, one meaning per button.

**The switch is a pill pair, not a button, and `.chip` is what this page calls
that class.** Modes and filters throughout the app are a set of alternatives
with the one in force filled — the colour page's two axes, the showcase's
table pills, and the history chips at the bottom of this very page. A mode
switch is that, not an action, so it wears the same control.

Both modes therefore stay on screen, which is the half a lone button could not
do. `Edit par` on its own says nothing about there being two ways to read the
table, and it needs a second control to leave by: a **Done** beside an unsaved
form, which discards a retuned colorway on exactly the click that felt like
keeping it — the failure this mode exists to prevent, reintroduced at the top
of the page it was removed from the middle of. With a pair you leave by
pressing the mode you are going back to, labelled with where it goes.

Note the class names differ by page and that is the existing state, not a
decision made here: seven templates define their own `.pill`, this page calls
the same thing `.chip`. `.btn` / `.btn-quiet` in `base_internal.html` are the
other family — a control that *acts* — and the showcase's `Edit dyes` is one
of those (an `<a class="btn editlink">`, primary weight because it is the only
action on a read-only row).

The usual rules, each already made somewhere else in here:

- **Absolute, never a delta.** "Par is 12" heals whatever the row said before;
  "add four" only works if it was right. Same reason `set_on_hand` and
  `record_count` take totals.
- **A blank box is refused, not read as 0.** `0` is a real answer — it is how
  you say there is no par, and it is what `production.candidates()` filters on
  — so guessing at an empty field would drop a product out of planning
  silently. Same bargain `parse_card_date` makes with a date it cannot read.
- **One bad box changes nothing.** The whole form is read before any of it is
  written, exactly as `record_recipe_production` beside it does, so an
  unreadable value cannot leave half a colorway retuned.
- **Writing par moves no stock and writes no `InventoryLog` row**, and posting
  `baths_` to the par endpoint or `par_` to the production one does nothing.
  Both directions are pinned, because the guarantee is what lets the two forms
  share a table.
- **The message names each change and where it came from** — *Sage 8 → 12*.
  Nothing records a par change, so that message is the only confirmation that
  the row which moved is the row you meant.

**Two read-only facts ride beside the box and neither proposes a value.**

`sold_this_season` is `production.sold_per_blank()` — the same figure
`private/production-needed/` ranks on, not a second answer to "what sold". It
is there because advice you cannot inspect is a decision in disguise, and here
the *person* is the decider: par with nothing beside it is a box you fill in
from memory. It comes off the till ledger while the lifetime figures at the
top of the page come off the stock log, so the page **says which is which**
under the table. Two legitimate answers to two different questions read as one
page disagreeing with itself unless each names its question.

The other is par restated in baths — *= 2.0 baths of 5*. That is the unit a
dye pot is actually spent in: across the live catalogue every distinct
`(par, bath size)` pair lands between 1.0 and 2.0 baths, so par is a MOQ floor
with a bath of headroom rather than days of cover, and a par landing mid-bath
claims a precision with nowhere to put it. **It is the number in the box in
another unit, never a recommendation about it.** A small script keeps it
following the box as you type — left alone it would report the *stored* par
under a live one, which is worse than printing nothing. Server-rendered on
load, so with the script blocked it is correct and simply doesn't move.

**Nothing here suggests a par, and that is load-bearing rather than an
omission.** A par derived from display capacity is the failure *Display
capacity is not demand* exists to prevent; a par derived from a sales rate is
unrepresentable at this bath size anyway (`docs/claude/production.md` has the
arithmetic — it crosses a bath boundary for 4 products out of 333). Par moves
because a person decided it should.

**One context builder, `_recipe_history()`, serves both renderers.** Two would
drift, and the drift shows as a swapped-in view disagreeing with the one a
refresh produces — which reads as the app being wrong about the numbers rather
than about the rendering. Same reason the fragment renders the same partial
the page does.

A swap that never arrives leaves the previous table sitting there looking
answered, so both the wait and the failure are said out loud. The listener is
on the `#history` container rather than the chips, because the chips are
themselves replaced by every swap.
