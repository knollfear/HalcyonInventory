# The sales ledger

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

## The sales ledger: what the till took, kept apart from what the shelf holds

`Sale` and `SaleLine` are a second ledger, imported from Square by
`import_sales_history`, and they exist so seasons can be compared with each
other. **Nothing in them moves stock and nothing in them writes an
`InventoryLog` row.**

**Running the wrong importer is the mistake to guard against.**
`import_square_sales` decrements `number_on_hand` and writes an
`InventoryLog`; it is part of the inventory pipeline and belongs to the
current weekend. `import_sales_history` writes only the reporting ledger, and
is pointed at seasons that in most cases ended years before this app existed.
Aiming the first one at 2021 would wreck every count in the shop.

**Why `InventoryLog` could not be the base for this**, since extending it is
the obvious move and it fails four ways at once:

- `created_at` is when the row was *written*. A webhook lands in seconds so
  those agree, but a CSV loaded on Monday piles Saturday's sales onto Monday
  — and hour-of-day is one of the questions being asked.
- A line the app cannot identify never reaches it; it goes to
  `UnmatchedSale`. A revenue total built on it is silently short.
- There is no money on the row. `private/sales/` values at *today's* price
  and says so on the page, which is fine for ranking and useless for history.
- The product FK is `PROTECT`, so a line item from a season before this app
  existed can never live there at all.

`SaleLine`'s product links are therefore `SET_NULL`, which is the opposite of
what the inventory side does and deliberate: an `InventoryLog` row is *about*
a product, so losing it strands the row, while a `SaleLine` carries
`item_name` and `price_point` as text and means something with no link at all.

**`source` is on every row and nothing branches on it.** The expectation is
that in six or eight years this app's own records replace Square as the
writer. Reports read the table and print the breakdown underneath, the way
`private/sales/` already does with `InventoryLog.source` — so the changeover
is one new writer and no new pages, and the overlap is safe because dedupe is
on the order id rather than on who supplied the row.

### Identity: item and price point, never the SKU, and never the Token

**Twenty of the thirty-six lines in a 2026 itemised export carry no SKU**, and
the older seasons are worse. A SKU-keyed importer would drop most of the
history and report success. Item is the style and price point is the
colorway, and both are on every row of every season — so `line_key` is
`item|price point|occurrence`, and the occurrence counter keeps a second
identical line addressable instead of overwriting the first.

**Square's `Token` column looks like a line id and is not.** It names the
product, so three separate triangle-fringe sales come back carrying one
token; keying on it collapses them into a single line and the revenue goes
missing with nothing to show for it.
`test_a_repeated_token_is_three_sales_not_one` is the pin.

Matching to the catalogue is SKU first, then `skus.slug(item_name)` against
`RawProduct.name` — which works because `sync_to_square` built the Square item
from that name, so `Noble - Diamond Extra` slugs to `NOBLED` at both ends.
Unmatched lines are kept in full and **named and counted in the report**, since
a shorter total is exactly the kind of quiet wrong this ledger exists to avoid.

**Keep `Category`.** The wax hands were on this till through 2024 and are
gone. A season-over-season total that cannot name the categories it counts
reads a discontinued product line as a decline.

**An unknown time zone stops the run** rather than falling back. Square writes
zone names in its own dialect and they are mapped explicitly; a near-miss
shifts every hour-of-day figure by hours and looks exactly like a correct one.

### The work is separate from what triggers it

Three modules, and the split is deliberate rather than tidy:

| Module | Knows about |
|--------|-------------|
| `squareorders.py` | Square's API, and turning an order into line dicts |
| `salesimport.py` | matching those lines to the catalogue and writing them |
| `management/commands/…` | what the operator asked for, and what gets printed |

**Nothing in the first two knows why it is being called.** No argument
parsing, no `self.stdout`, and no `CommandError` — a Square failure raises
`squareorders.SquareUnavailable`, and each caller translates that into
whatever it needs to say. A module that raises `CommandError` is telling its
caller what kind of program it is.

The reason is the next trigger. The webhook already holds a retrieved order,
so the day it writes the ledger it either calls `lines_from_order` or copies
it — and a copy is how two totals for one weekend appear with nothing on
either to say which is right. Same for a queue consumer, or a backfill. The
trigger is expected to change; the work is not.

`WorkIsSeparableFromItsTriggerTests` pins it, including walking the import
graph of both modules and failing on anything from `management`. It was worth
writing: the first version of this split imported `CommandError` without
anybody noticing.

### Two doors into the ledger, and the order is the unit

`import_square_orders` pulls from the **Orders API**; `import_sales_history`
reads an **itemised CSV export**. Both turn their source into line dicts and
hand them to `scarves/salesimport.py`, which owns the matching, the writing
and the reconciliation print. One copy on purpose: two would drift, and the
way drift would show is two totals for the same weekend differing by a handful
of lines with nothing on either to say which was right.

**Prefer the API.** It needs no export step and no file, `--year 2021` reads
the faire calendar for its dates, and its line items carry
`catalog_object_id` — so `Matcher` can match on Square's own variation id
rather than on a SKU most historical lines do not have or an item name that
only reaches the blank. The CSV path stays because it needs no credentials and
loads a file somebody already has.

**Matching is three tiers, best evidence first**: Square variation id, then
SKU, then item name against a `RawProduct`. Nothing unmatched is dropped —
`item_name` and `price_point` are text on the row, so an unmatched line still
counts toward every total, and the report names what it could not place.

**An order already on file from a different pipeline is skipped whole.** The
export aggregates identical items onto one line and the API does not, so the
same order loaded through both doors would produce two overlapping sets of
lines with no way to tell which was double-counted. Within one pipeline
nothing changes: lines match on `line_key`, so a re-run adds nothing.

**Order lines carry no category, and category decides whether the wax is in a
season total.** It is resolved in three passes, and the middle one is the
important one:

1. **`ListCatalog`** — but that returns the *living* catalogue, and a season
   five years old is mostly things that have since been deleted.
2. **`BatchRetrieveCatalogObjects` with `include_deleted_objects`**, asked
   about the specific variation ids the first pass could not place. The parent
   item comes back as a related object and the category hangs off that.
   Without this the four base yarns resolved to nothing, which put roughly
   $40k of a $48k yarn year into a bucket labelled "(uncategorised)" — the
   totals were right and the *page* was wrong, which is the harder version.
3. **This app's own `RawProduct.category`**, for anything Square cannot
   answer at all.

`_category_name` reads all three shapes Square names a category in:
`category_id` (old, and null on anything written recently),
`reporting_category` (what the dashboard's own reports use) and `categories`.
Reading only the first two was how this hid.

**`SaleLine.square_variation_id` is kept for exactly this reason.** Item names
get edited and catalogue objects get deleted, so the variation id is the only
durable handle on a line — and it is what lets a category be resolved again
later without re-fetching every order.

**The report names four different totals** — gross before discounts,
discounts, refunds, net — because "the total" is four numbers and a season
compared against the wrong one is out by whatever that year's discounts ran
to. Worth knowing: the old React site's figures are **gross before discounts
and refunds**. Its 2021 total of $93,578 comes back as $93,458 gross,
$89,829 net; the 2022 figures agree to $18.

### Keeping it current

`import_square_orders` is idempotent, so **re-running it is the refresh**.
Three ways to name the window:

- **No arguments at all** — the season running now, first day to today. This
  is the one to run after a weekend and the one to schedule, because a
  scheduled `--year 2026` keeps exiting 0 forever while quietly covering
  nothing from 2027 on. Between seasons it answers with the last season that
  started, since the reason to run it in February is to top up what October
  finished with.
- `--year 2026` — naming the year still works and is the plain way to say it,
  including catching up a season that went a year or two stale.
- `--since 14` — narrows the window to recent days. Re-reading August is
  harmless but it is thousands of orders for the handful that are new.

A window never runs past today. Asking for the future buys nothing here and is
an outright error against the weather archive, so both commands clamp.

**The webhook deliberately does not write this ledger.** It is the one path
that moves stock during a live season, and the cost of a bug in it is real
counts during a weekend that cannot be recounted — against which the benefit
is a few days' freshness on a reporting page. A command that somebody runs is
the cheaper trade. If that ever changes, the thing to fix first is
`salesimport.write`'s foreign-pipeline guard: webhook and API rows are the
same Orders API shape and build the same `line_key`, so they are safe to
merge, and the guard needs to keep firing only against the CSV door.

### What the first full pull turned up

Loading 2021–2026 took 12,939 lines across 11,348 orders, and three things in
it change how the numbers read:

- **The wax and the yarn were swapped.** Wax was 43% of 2021 revenue and
  stops dead after 2024; yarn is zero until 2025 and then takes $49,356 of
  that season. Any total spanning that boundary is comparing two different
  businesses, which is what the category filter is for — and silk grew
  straight through it regardless (50,787 → 82,856 across 2021–2025).
- **Colorway-level history starts in 2026, because before that the shop did
  not sell by colorway at all.** Selling and tracking by colour is the whole
  project; it is not something the older seasons did worse. Nothing before
  2025 has a Square variation id or a SKU, so those seasons read at the
  *blank* only. 2025 looks like an exception and is not: 2,348 of its 2,349
  lines carry a variation id, but its yarn price points are undyed varieties
  — `Baby Yak Cloud`, `Tibetan 3 ply`, `Egyptian Yak` — rather than colours,
  so **no colorway is recoverable there.** Re-running the colorway matcher
  over 2025 links zero lines; that has been measured, so nobody needs to try
  it again.

  **The undyed yarns themselves are a different story, and this paragraph
  used to get it wrong.** It said there was nothing to recover, full stop.
  What was actually true was that no *product* existed to match those price
  points against — and then `create_passthrough_products` made one for every
  undyed yarn, so the same 122 lines became matchable without a single one of
  them changing. `relink_sale_lines --item "Undyed Yarn"` attaches them. The
  lesson generalises past this case: **"nothing matches" is a fact about the
  catalogue on the day it was measured, not a property of the season** — so
  re-check it after anything that adds products.

  The trap this sets is specific and expensive: a colorway query over 2025
  returns **zero rather than an error**, so "it sold none last year" and "the
  concept did not exist last year" produce the same output — and the first
  reading retires a recipe. Compare colorways only from 2026 on; blank- and
  category-level comparisons are sound throughout.

  It follows that **no colorway has a baseline yet.** A slow one is judged
  against other colorways in the same season, never against its own history.
- **A projected weekend is not a counted one.** `Season.total` and
  `traded_days` deliberately exclude projections; an earlier version summed
  every weekend, which made the figure captioned "so far" equal the one
  captioned "on course for" — the page agreeing with itself about a number
  nobody measured. `ProjectionBasisTests` pins it.

The page also states what a projection rests on and warns below a quarter of a
season, because a run extrapolated from its opening weekend moves a long way
on one good Saturday.
