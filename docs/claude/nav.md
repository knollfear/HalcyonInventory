# Navigation: the pinned pages

Part of the project guidance in `CLAUDE.md`, which carries the rules that apply everywhere. Read this file before touching anything it covers.

### The pinned pages: a shortcut past the hub, never a ranking

Hub-and-spoke is right for a directory of thirty-odd pages nobody memorises
and wrong for the four somebody opens every day — those cost two clicks each,
forever, and the second click is a page nobody wanted to look at. So
`base_internal.html` carries a few pinned pages beside the `← Site map` link
that was already in the corner. `scarves/nav.py` holds all of it.

**The pins grow leftward and the map link keeps its exact corner.** That is
the whole reason they go on that end: whatever anybody has already learned
about where the way out is stays true. The page you are on renders dimmed
rather than dropped, because a set that silently loses whichever one you are
looking at changes shape as you move through it, which is the thing a fixed
nav exists not to do.

**Pinned, never ranked**, and this is the load-bearing decision. The obvious
version counts visits and promotes the top four on its own. It fails four ways:

- **It is self-reinforcing.** A page in the top four is one click away, so it
  gets opened more, so it stays; one that drops out becomes two clicks away
  and gets opened less. The set freezes around whatever the first week looked
  like.
- **It counts the wrong thing.** A page opened forty times in an afternoon
  because it is awkward outranks one somebody depends on weekly. The restock
  board already says this out loud — five passes in five minutes is a good
  afternoon — which is why it counts nothing.
- **Navigation that reorders itself cannot be learned.** The value of a fixed
  nav is that the third pill is always the third pill; a set that reshuffles
  costs a read on every glance, which is worse than the site map it was meant
  to save you from.
- **It is `par` again**: a derived number that reads as a fact because it came
  out of a counter, with nowhere to disagree with it. See *The app advises, a
  person decides*.

**The counter still exists, one step back.** Visits are counted onto a cookie
by `NavMiddleware`, and the count prints beside each page on
`private/navigation/` as evidence for a decision somebody makes by ticking a
box. Fill the form in, a person confirms — the same bargain `colorbands` makes
with the rainbow sheet. It orders that one list and reads nowhere else; it
never chooses a pin, and `test_the_count_never_chooses_what_is_pinned` is the
pin on that.

Four things are excluded from the tally, and each is a different way it would
stop meaning "pages she opens": a non-200 or non-GET (a login redirect is not
a visit), a route the site map doesn't list (so POST endpoints and the webhook
never appear in a list of pages to pin), **an htmx fragment** — the recipe
showcase would otherwise out-count every page in the app by the width of an
afternoon's dye entry — and an anonymous request. It is middleware rather than
a call per view because thirty-odd views each having to remember is the rule
this app runs on: never add a step that has to be remembered to be correct.

**A `?nav=` link is the dashboard, and there is nothing stored.**
`?nav=recipe_showcase:Recipes,color_classify:Colours` sets the pins for whoever
opens it, so several links kept in a note are several purpose-made navs — one
for a dye day, one for a close — with no admin screen and nothing to manage.
The optional `:Label` exists because `@page_meta` titles are written for a site
map card and a pill has room for a word; `encode` drops a label that is just
the title again, so a renamed page follows its new name rather than being
frozen under the one it had the day the link was made.

**Route names, never paths.** There is no 404 here, so a stale path in a nav
would land on the public map looking like a working page. A route name either
reverses or it does not — and one that does not is **dropped and named** on the
navigation page, never quietly, because a nav that came back a pill short with
no explanation is the collection sheet's missing-dye problem: you act on the
list and nothing says it was incomplete.

**Both doors land on `private/navigation/`, which is the only writer.** A link
arrives there rather than applying silently from wherever it was opened, so the
page can *say what it did* and offer `?nav=forget` — the crew cookie's argument
that a pre-filled thing nothing mentions is unrecoverable by the person looking
at it.

**What can be pinned is what the staff site map lists**, derived rather than
kept in a list here, which excludes two groups for free: anything with no
`@page_meta`, and anything parameterised, since it reverses to nothing.
`secret/` pages are pinnable — they are on the staff map already — and this
leaks nothing, because `restock_board`, `hours_entry` and `booth_photo` extend
`base_public.html`, so no staff nav ever renders on a page the crew reach.

The cookies are plain where `crew.py` signs its own. Nothing here is a claim
about identity or permission: every page a pin points at is `@login_required`
in its own right, so a forged cookie buys a link to a page the browser could
already reach. Defensive parsing is what actually matters, and a malformed
cookie gives an empty nav rather than an error on every staff page.
