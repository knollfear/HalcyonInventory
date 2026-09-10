# Reports: sellers, seasons, the calendar and the weather

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## Top sellers: the first page that reads the log as a dataset

`private/sales/` answers "what sold, and over what dates" — one row per
finished product, which is blank × colorway, the axis the catalogue is
organised on. `scarves/sales.py` does the picking; nothing in it writes.

**Scope is `log_type=SALE` and nothing else**, which is deliberately narrower
than "stock that left the tent". A Sunday close writes an *adjustment* when
the count disagrees, and some of those genuinely are sales nobody registered
— but the app cannot tell which ones, and a guess sitting in the same column
as a till receipt is worse than a gap, because nothing on the row would say
which it was. `close_history` already has that half, and the page links to it
rather than folding the two together.

Two properties of the date column change what a total means, so both are
printed on the page rather than known only here:

- **The date is when the row was written.** A webhook lands within seconds, so
  those agree. `import_square_sales` stamps at import time, so a CSV loaded on
  Monday piles Saturday onto Monday. A resolved unidentified sale is the one
  that goes the other way — back-dated to Square's own sale time.
- **A sale that never reached the app is not here at all**, which is the same
  silence the close exists to catch.

Which is why the page prints a **breakdown by `source`** under the table. That
is the field doing the job it was added for: a range carrying `square_import`
rows where `square_webhook` ones normally sit is a dropped integration and a
CSV loaded afterwards, and a `test` row is a simulated sale somebody left
behind — all of which read as ordinary sales in every other column.

**Transactions is not a row count.** A dye bath yields several units of one
SKU and they leave in ones and twos, so "12 sold across 11 sales" and "12 sold
in one" are different facts about a colorway and only one of them is a
following. Rows carrying no order reference count one each: nothing says two
of them were the same sale, and assuming so deflates the number. `days` is
beside it for the same reason at a coarser grain — a rush and a steady seller
are the same integer in the units column.

**`on hand / par` is one cell, not two columns.** The number worth reading
after "twelve of these sold" is the gap, and side by side it needs no
arithmetic; `short` is the gap, sorted on and **clamped at zero**, because a
product above par is not short by a negative amount — overshoot is bath-size
rounding and means nothing here. Nothing on this page schedules anything: a
colorway at the top with nothing left is an argument for raising its par,
which stays a deliberate decision about demand (see *Display capacity is not
demand*).

**Value is units × today's price and says so on the page.** There is no price
on an `InventoryLog` row, so this cannot be what the till took — a price
changed since, or anything discounted, makes it wrong. It is there to rank
colorways against each other, never to reconcile against Square.

No rates and no averages. Units and transactions are both printed and never
divided into each other, the same bargain the close makes: an average basket
across a stall's worth of colorways moves for reasons nobody can act on, and
it would sit beside the two numbers that can be.

**All of the page's state is query string** — range, filters, sort column,
direction — so a reading is a link somebody can send and every control
carries the rest of the state rather than resetting it (the colour page's
pills, again). `today` and `yesterday` resolve at request time rather than
being links to a fixed date: somebody who taps "today" and sends the link
means today. Sorting happens in Python so the derived columns (shortfall,
value) sort on the same terms as the queried ones, instead of being the two
headings that mysteriously aren't links. An unreadable date contributes
nothing and the heading states the range actually used, which is what stops
the answer being mistaken for the question that was asked.

## Slow sellers: a zero means two opposite things

`private/slow-sellers/` is the other end of `private/sales/`, and a different
question rather than the same one sorted backwards. **A product that sold
nothing has no row to aggregate**, so this starts from the catalogue and
subtracts — a different query, with a different set of things that can go
wrong. `scarves/slowsellers.py` does the picking; nothing in it writes.

**Every zero is printed with the stock beside it**, because a colorway that
sold nothing either sat on the display all weekend or was never out there to
be wanted, and those argue for opposite decisions — stop dyeing it, or dye
more of it. The shop's own answer is that everything on hand goes out, so
stock is a good proxy for "it was on the table". A zero with none is not a
slow seller at all; it is a colorway nobody could buy, and it is the reason
the page exists.

**The unit is the colorway, pooled across every blank it is dyed on**, and a
per-blank view is one click away as the opt-in. A zero on one yarn is not a
dog: the production cadence absorbs it, and it is still earning its place on
the display, where a full colourful stall is worth something no sales column
can show. Pooling is also the unit a recipe gets *retired* in — nobody stops
dyeing a colour for one yarn while the others move.

**Pooled before the threshold, never after.** Regrouping the filtered rows
would keep a colour's dead blank and drop its live one, so a colour selling
twenty on Heavenly and none on Artisan would read as one nobody wants — the
opposite of the truth, and in the direction that gets it retired.

**It reads `SaleLine`, not `InventoryLog`**, which is the opposite of the top
sellers page and deliberate. `sold_at` is Square's own clock, so a
season-scoped window means what it says where `created_at` would land a
Saturday's sales on Monday. And a sale the app could not identify never
reaches `InventoryLog` at all — missing sales inflate a top-sellers list by
nothing, but they *invent* bottom sellers out of products that did sell. The
two pages will not agree to the unit and the page says so.

**Scope is what this app tracks.** Roughly 43% of a season's Square lines tie
to nothing here — accessories, unsynced items, flat-price buttons — and that
is a known gap worked elsewhere, not something this page reasons about.

### Three ways the attribution lies, and only one of them is loud

Worth separating, because they need different handling and only the third is
dangerous:

1. **Untracked** — the line matches nothing. Out of scope, and it announces
   itself by never appearing.
2. **Colourless** — the blank rang up on a flat price button, so the units are
   real and belong to *some* colorway with nothing to say which.
   `unattributed()` counts these per blank and the page prints one line of it
   under the table. A line rather than a panel: it qualifies the list without
   competing with it.
3. **Miscoded** — every sale of a blank landed on one colorway. This is the
   one to watch, because it looks like complete data: one runaway hit and
   forty duds, and both halves are false. The duds are exactly what this page
   reports. Sash Belt is the worked case — 55 of 59 attributed units came
   back as `Amethyst` against 48 colorways in the catalogue.

`lopsided()` finds the third **by arithmetic rather than by name**, so the
next blank to break is caught without anybody remembering to add it: a
majority of a blank's attributed units on one colorway, on a blank carrying
at least ten. Both bounds are needed — a blank with three colorways can
honestly put 60% on one — and it currently flags exactly one thing.

**It reports the numbers and draws no conclusion.** A genuinely popular colour
and a miscoded button produce identical figures, and only somebody who knows
the shop can say which. Same call `colorbands` makes: fill the form in, a
person decides.

## The faire calendar is a rule, not a table

`scarves/seasons.py` holds it and `generate_faire` applies it:

> Labor Day is the first Monday in September. **Week 1's Saturday is nine days
> before it.** Nine weekends follow, Saturday and Sunday — plus Labor Day
> Monday, which always falls in **weekend 2**. Nineteen trading days.

It reproduces every season on record exactly (2017→26 Aug, 2018→25 Aug,
2019→24 Aug, 2021→28 Aug), which is what `FaireCalendarTests` pins. Getting it
wrong is invisible: the pages still render nine weekends, they are simply the
wrong nine.

**This is why nothing is typed.** The React site this replaces died of
hand-feeding — its data is hardcoded in the JS bundle, 2024 stops at week 7,
and the 2022 and 2023 weather rows are byte-identical copies of 2021's. Same
rule the rest of the app follows: never add a step that has to be remembered
to be correct.

**Labor Day drifts over 1–7 September, so week 1 drifts across six calendar
days** — Sat 23 Aug in 2025, Sat 29 Aug in 2026. No comparison between seasons
may key on a calendar date; the weekend index is the axis.

**Weekend 2 has three trading days and every per-weekend total for it reads
about a third high.** Across 2021–2024 it ranks #1, #2, #4 and #5 by weekly
total and #6, #6, #8 and #8 per day — in every season it is one of the
*weakest* stretches of the run, and the Monday is doing the work. Anything
printing a per-weekend figure needs a per-day companion, or it will be acted
on.

**2020 is an absent `Faire`, not a zero one.** `generate_faire` skips it by
name, so every query that walks faires excludes it by construction rather
than by remembering to.

**`FaireDay.traded` is a checkbox with no workflow behind it.** Three days in
twenty-two years — a washed-out weekend in 2023 and one hurricane. It earns
its column because it is the denominator: counting that washout as two traded
days moves 2023's per-day figure by nearly 12%. Do **not** infer a closure
from a day with no sales — a dead terminal looks identical, and the inference
would fire wrongly far more often than the thing it detects. Regenerating a
season never re-opens a day somebody struck.

### More than one faire, and what never gets compared

`Faire.slug` is the event across years, `Faire.year` is the instance, and
`rule` says how its days are known. A second rule-based faire is one function
and one entry in `seasons.RULES`; a faire whose dates are announced is
`manual`, and `generate_faire --dates` numbers them by the gaps between them
(so Sat–Sun is one weekend, and Sat–Sun–Mon is too).

**Comparison never crosses slugs, and no page should offer it.** Week 1 of one
faire against week 1 of another is not asked here and the number could not
answer it — a weekend index counts position within *that* run, so two faires
of different lengths and audiences share nothing but the integer. Scope every
comparison to one slug and let the years vary.

`FaireDay.date` is unique across all faires, not just within one. That is a
real constraint rather than a convenience: the booth is in one place at a
time, so a date belongs to at most one faire, and making it a database fact
means a sale is placed by date alone. A genuine overlap should fail loudly,
because being in two places is a decision somebody has to make.

## `private/seasons/`: the pace page, and the two things it refuses to draw

`scarves/seasonreport.py` picks and folds; the view renders. Nothing writes.

**The chart is server-rendered SVG and the page runs no script.** It prints,
it works with scripts blocked, and there is nothing on it that can fail
silently — the same bargain the restock board and the close make, for a desk
page rather than a field one.

**Seasons are ordered, so they are drawn on one hue light-to-dark**, not as a
rainbow of categorical colours: recency then reads without a legend lookup.
The focused season is the only line carrying a second hue, and **every line is
labelled at its own end**, so colour never carries identity alone.

**Per trading day sits beside per weekend, and that is not a nicety.** Weekend
2 carries Labor Day Monday, so its weekly total runs about a third above its
neighbours for a reason that has nothing to do with trade — across 2021-2024
it ranks first or second by weekly total and sixth or eighth per day. A page
offering only the weekly figure would be read, and acted on. The trading-day
count prints in its own row so the three is never invisible.

Two things the page will not draw, both because the drawn version would read
as evidence:

- **A weekend still ahead is projected; a weekend already past with no lines
  is a gap and is left empty.** The second is almost always an import nobody
  ran, and a projection sitting where a missing import should be is a number
  that looks like a measurement. `Weekend.to_come` and `Weekend.is_gap` are
  the split, and the page names the gaps in a warning.
- **A weekend with nothing known breaks the line rather than dropping to
  zero.** Joining across it would draw a season that traded nothing.
  `_segments` keeps a lone point too — an earlier version dropped any single
  point that was not last, so two non-adjacent weekends drew no line at all,
  which reads as a season that took nothing.

**A gap only exists inside a season that otherwise arrived.** For a season
nobody has imported, every weekend flagged says nine times over what one
sentence says once, and buries the real gaps in the half-loaded seasons. So a
season with no lines at all is reported once, as a note.

**A `distinct()` on this ledger needs an explicit `.order_by()`.** `SaleLine`
carries a default `Meta.ordering`, and Django puts ordering columns into the
SELECT — so `values_list("category", flat=True).distinct()` de-duplicates on
`(category, sold_at, item_name)` and returns one row per line. That shipped,
and rendered the category filter as **eleven thousand pills**. Aggregates are
unaffected: `values().annotate()` drops the default ordering, which is why the
totals were right the whole time.

Worth knowing how it hid, because the same trap is waiting for the next
`distinct()` here: **`.count()` wraps the query in a subquery and reports the
correct number**, so checking the count says four and iterating says twelve
thousand. Verify a `distinct()` by iterating it, never by counting it.

**The page reports what happened and never why.** There is no annotation
field, no commentary column, and no place to record a reason a weekend was
soft — and there should not be. A season figure is read by the people who
made the stock, so a cause attached to a dip is an accusation with a number
behind it, and the causes available are almost never supported: a single
season's back half, with a product line arriving the same year, is one
observation with an unmodelled variable in it. Print the figure and let a
person say what they think happened. Same bargain `colorbands` makes, and the
same reason `closing.tally()` has no `rate` key.

**Categories are a filter because the wax hands were on this till through 2024
and are gone.** A total that cannot say what it counts reads a discontinued
product line as a decline. All of the page's state — faire, focus year, mode,
metric, categories — rides in the query string, and every control carries the
rest of it.

Season figures are scoped to the weekends actually reported: `Season.per_day`
divides by the traded days of the weekends that have data, not by the whole
run, or a half-imported season reads as a catastrophe.

### `?blank=` — the style is the axis that reaches every season

The question is "rectangle veils, year on year, in dollars and units", and the
style is the only cut of the catalogue that can answer it. `SaleLine.raw_product`
is matched off the **item name**, which every season carries; a colorway needs a
SKU or a Square variation id and nothing before 2025 has either. So the filter
reads back to 2021 where a colorway query would return zero and mean "the
concept did not exist" — the trap already recorded above, here avoided by not
offering the cut that springs it. Dollars and units both answer under it,
because the metric toggle was already there.

Deliberately **not on `private/sales/`**, which is where somebody will look
first. That page reads `InventoryLog`, which begins when this app did — 678
sale rows, all 2026 — so it has exactly one year and could not draw a second.
Its money column is units × *today's* price and says so. Both halves of the
ask therefore have to come off the ledger. `private/sales/` does have a blank
filter, labelled **Style**, and the parameter here is named `blank` to match
it.

A select rather than pills: twenty-five blanks are a paragraph of pills, and
the categories already have a row. Single-choice, because the question is
about one style; the categories stay multi-select and the two compose.

**Narrowing breaks the "no lines" shorthand, and that is the whole of the
work.** Unfiltered, a weekend with nothing against it can only be a weekend
nobody imported — which is what `has_data`, `is_gap`, `Season.total` and
`traded_days` were all reading. Filtered, it is nearly always a weekend where
that style did not sell, and the two must not render the same: one is a hole
in the data and the other is a measurement of zero. The consequences ran
deeper than the cell:

- a quiet weekend would be named as a **missing import** in the warning, which
  sends somebody to re-run an importer that has nothing to fetch;
- it would **drop out of `traded_days`** — the denominator — so every per-day
  figure on the page would read high, silently and in the flattering direction;
- the chart would **break its line** across it rather than drawing zero, which
  is the "season that traded nothing" failure `_segments` already exists to
  avoid, arriving by a different door.

So `Weekend` carries `imported_lines` beside `lines`: the first counted with no
filter at all, the second under whatever is in force. `has_data` reads the
first, `sold_nothing` is the pair, and the cell prints a grey `0`.

**The category pills could already reach this**, which is where it would have
been found eventually and at a worse moment. The wax hands left the till after
2024, so counting only them makes 2025 and 2026 carry no lines whatever — two
seasons reading as exports nobody loaded, on the filter that exists precisely
to stop a discontinued product line being read as a decline.
`test_a_category_absent_from_a_season_is_not_a_missing_import` is the pin.

**Nothing is projected for a style that banked nothing.** A dashed tail drawn
along zero reads as a forecast rather than as the absence of one.

## Weather: fetched once, from a free archive, and never at render time

`scarves/weather.py` reads Open-Meteo's historical archive — no key, no
account, back to 1940, which matters because the point is filling in seasons
that happened years ago. `fetch_weather` stores it; `DayWeather` holds it.

**Nothing reads the network when a page renders.** A report that makes an HTTP
call is a report that sometimes does not render, and the weather on a weekend
three years ago is not going to change.

**Three reasons a day can have no reading, and they are not the same.**
Conflating them is how somebody gets told to come back later for a weekend in
2027:

| State | What it means | What the command does |
|-------|---------------|-----------------------|
| still ahead | the day has not happened | never requests it — asking the archive for the future is what returns a 400 |
| awaiting the archive | the day just passed | names it; the archive lags about five days |
| on file | fetched | skipped, unless `--force` |

**Cloud and humidity are averaged over opening hours (10:00–19:00), not the
whole day.** Fog at four in the morning is not weather anybody stood in. This
makes cloud read lower than the whole-day figure the old React site carried —
same sky, different question. Temperature and rain are daily aggregates and
match that site closely: fetched 2021 comes out 78/71/71/73/64/67/68/62/58
against its hand-collected 78.05/71.7/72.25/74.45/66.35/68.1/68.9/63.6/60.35,
which is the evidence that the manual row can stop being typed.

**Coordinates live on the `Faire`, and there is no zip lookup.** Turning a zip
into a latitude and longitude means another service and another failure mode,
for a value typed once per faire and never again — and the archive grid is
about nine kilometres across, so anywhere in the right town is the right
answer. Passing `--lat`/`--lon` once saves them onto the faire.

`DayWeather` cascades from its day for the same reason a product image
cascades from its product: it describes that day, and with the day gone there
is nothing left to describe.
