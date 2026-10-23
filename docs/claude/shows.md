# Shows: `private/shows/`

A planner for shows beyond the Labor Day run. Plan a show before deciding;
afterwards, set it against what it really did. Each show is stored so it can be
read again — including the ones passed on, since the next time a show is
offered, what it was expected to make the last time is the first thing worth
reading.

`scarves/shows.py` has the arithmetic, `scarves/views/shows.py` the pages,
and `Show` / `ShowCost` in `models.py` hold what a person types.

## A piece sold is a piece to make again

This is the idea the page is built on, and the reason it is more than a
takings sheet. A show that sells 2,500 pieces has sold 2,500 blanks and
2,500 pieces of dyeing, so its costs come in two kinds:

- **Replacing what sold** — blanks at replacement cost (`blank_cost`, so a
  fancy veil carries its line work), dyeing per piece, bought-in stock at
  cost, and the card fee. These grow with takings, so they are a *share*.
  At the 2026 Labor Day run's mix and prices, before dyeing, about 62¢ of
  every dollar survives them.
- **Being there** — booth fee, crew, miles, lodging, display wear. Fixed by
  the show, paid whether it sells or not.

**Break-even is being there divided by what a dollar keeps.** That one
sentence is why a $10,000 day is hard to lose money on and a $1,500 day is
easy: the share is the same at both, and the fixed half doesn't care. The
page prints it as a day's takings — *the show has to take $X a day just to
break even* — because that is the number to compare against what the show's
organisers say a booth does.

## Three groups, not categories

Silk, dyed yarn, bought in. **Bought in is not "Notions"**: undyed yarn is in
the Yarn category and is sold as it arrives, so it pays no dyeing and is
grouped with the notions (`group_of`). A colorway with no recipe is a
passthrough whatever its category; a line matched only to its blank — silk
rung up without a colour — is still a dyed piece. Fancy silk counts as silk.

## Not a `Faire`

A faire's dates are unique across the app because the booth cannot be in two
places at once — and that is exactly wrong for a plan. Two candidate shows on
the same weekend is what weighing looks like. So a show carries its own
dates and claims none, and **actual takings are read off the ledger by date
only for a show marked going**; a candidate that lost out never reads the
weekend the booth spent elsewhere.

Don't make shows into faires to get the weather or the season pace. If a
show recurs and becomes a fixture, it gets a `Faire` slug of its own then
(`generate_faire --dates`), and the cross-slug rule in `reports.md` still
holds: weekend indexes never compare across events. Comparing *what a show
left over per day* across shows is fine and is the point of the list —
it is economics, not a weekend index.

## Per-piece figures are copied in, then belong to the show

`shows.make` seeds a new show's mix and per-piece prices and blank costs from
the latest Labor Day run with sales on file (`reference`). They are printed
on the list page and editable on the show, because a fiber festival sells
almost no silk and nothing in this season's mix knows that.

**Copied, not derived**, for the same reason the dye statements freeze: a
plan made in January must still say what it said in January after next
season's prices arrive, or "did we plan it right" has no answer. The actual
column is the opposite — it prices what sold at *today's* blank costs, since
a piece sold now is replaced at today's price.

## The dyeing rate starts empty, on purpose

No piece rate is agreed (see the booth-economics notes), and the user chose
not to have the planner offer one. An empty box prices dyeing at nothing and
**says so**: the line reads *not set*, the bottom line is labelled *before
dyeing*, and the list marks it with an asterisk. Zero would be the same
arithmetic and the opposite message — a show that looks better than it is.
Don't prefill it, from the proposed rates or anything else.

## Costs: a rate to plan, a total to reconcile

`ShowCost` is a line per cost rather than a column per cost, because the
list differs per show. Each line plans as a rate — per show, per day open,
per mile (round trip × trips) — and records the actual as a total off the
receipt. **A line with no actual carries its plan into the actual column and
is labelled *as planned***, with a count under the table, so a show half
reconciled never reads as one that cost less.

**Crew is one number a day**, never people × hours × rate. Pay differs and
hours differ; the decomposed version is a fight rather than a figure.

## Actual takings

Read from `SaleLine` over the show's dates. The Square import is
date-scoped already, so *Pull sales from Square* on the show's page runs
`import_square_orders --from --to` over the dates so far. Re-running is the
refresh; the import matches orders already on file.

If nothing is on file the actual column is a dash, never zeros — a show
whose sales were never pulled did not make nothing. A piece sold with no
blank cost on file is priced at the plan's figure and counted beneath the
table.

Online sales landing on a show's dates would be counted as the show's. None
exist today; if a website starts selling, this needs `Sale.location`.
