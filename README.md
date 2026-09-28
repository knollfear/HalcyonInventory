# Halcyon Inventory

Production software for a hand-dyeing business: silk scarves and wool yarn, dyed
in small batches and sold at a seasonal outdoor faire. It tracks what exists,
decides what to dye next, prints the barcodes that go on it, reads the marked-up
paper that comes back from the dye room, and reconciles the till against all of
it.

It is **in production and in daily use**, running the shop's best year to date.
It is also built while the business runs, which is the most important thing to
know about the code: most of it exists because something went wrong in a dye
room or at a stall, and the commit messages and `docs/` say which thing.

Django 5.0 / Python 3.11 / Postgres 16, deployed on Railway. Roughly 37,500
lines of application code, 30,500 lines of tests, 2,092 tests, 96 templates and
60 documented pages (45 of them listed on the site map).

---

## The one thing to know first

**The catalogue is narrow in styles and very wide in colour.** Five styles cover
about 99% of what gets made — infinity scarves, sash belts, half circle veils,
rectangle veils, triangle fringe scarves — alongside four wool yarn bases. Those
few blanks are multiplied by ~130 dye recipes into 665 sellable products.

The dyeing is the value add, so **colorway, not style, is the axis everything is
organised around.** That single fact explains most of the design:

- a SKU is `BLANK-DYEBATH`, so the style is the *small* half of the identifier;
- the Square catalogue maps ITEM → style, VARIATION → colorway, and sorting
  those variations needs its own pass, because a till list forty colours deep in
  creation order is unusable;
- printed reference sheets are organised per colorway, and by rainbow band
  rather than alphabetically — you never know a scarf's name while holding it,
  but you do know it's *red*;
- a dye bath is one blank plus one recipe, so barcode runs clump by SKU.

A new product here is almost never a new style. It is another colour of
something that already exists.

## The week this models

Nothing in the schema says this either, and every feature is a station on it:

```
Sunday close  →  card stack  →  production sheet  →  dye room  →  labels  →  stall
   count the       what has        what to dye        mark the      print &      sell,
   display         an empty bag    and in what        sheet as       stick        and the
                                   order              you go                      till reports
```

The close is the shop's own ritual and predates the app. An early version tried
to replace it with a computed reorder signal off a stored `par` column; the
correction ran the other way, and the physical loop became the model. Both
signals now coexist and are reconciled by one claim — see
*Two signals propose a dye bath* in [CLAUDE.md](CLAUDE.md).

## Start here

The code is organised by business concern, not by Django convention, and the
`docs/claude/` files are arguments rather than descriptions — they mostly explain
why the *better-looking* version of a feature was tried and abandoned. Suggested
reading order for someone new:

| Read | For |
|---|---|
| [CLAUDE.md](CLAUDE.md) | The rules that apply everywhere. Start with *Display capacity is not demand* and *Self-healing, and eventually concurrent* — they are the two load-bearing ideas. |
| [`scarves/sheetscan.py`](scarves/sheetscan.py) | Reading pen ticks off a photo of a printed sheet. The barcode beside each row doubles as a fiducial *and* a calibration swatch, which is what makes it survive a dye-room light bulb. |
| [`scarves/colorbands.py`](scarves/colorbands.py) | Sorting dyes and photographs into rainbow sections. Opens with the four cases that break naive hue classification. |
| [`docs/claude/labels.md`](docs/claude/labels.md) | Barcode label runs, and why the last sticker of every run says `NEXT RUN: START AT n`. |
| [`scarves/ledger.py`](scarves/ledger.py) | The single door every finished-stock movement goes through. Seven hand-written call sites used to do this and had drifted apart. |
| [`start.sh`](start.sh) | Deployment, and why migrations deliberately do *not* run here. |

## Running it

Docker, not a local virtualenv — there is no Postgres or psycopg on the host by
design.

```sh
docker compose up                      # http://localhost:8000, admin / admin123
```

Tests. The override drops the host port binding so a Postgres already listening
on 5432 for another project doesn't collide:

```sh
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  run --rm web python manage.py test
```

A check that doesn't need the database (`--no-deps` skips the `db` service):

```sh
docker compose run --rm --no-deps web python manage.py check
```

The suite is gated by a **pre-push hook**, which needs installing once per
clone because `.git/hooks` isn't version-controlled:

```sh
scripts/hooks/install.sh
```

It runs the suite only on a push touching `main`, because `main` is what
deploys — a commit is private and amendable, so gating it would charge three
minutes for an act with no consequence, and still wouldn't cover the one that
has. It fails closed when Docker isn't running, on the grounds that "the tests
didn't run" must not look like "the tests passed", and it prints
`git push --no-verify` on every refusal. That escape is deliberate: a gate you
can't open during a Saturday at the stall is a gate that gets uninstalled.

## Deploying

Railway, from `main`, with separate production and development environments.

Migrations run in Railway's `preDeploy` step, not in [`start.sh`](start.sh), and
that is the whole deployment strategy: `preDeploy` **gates** the release, so a
migration that fails means the new version never goes live and the old one keeps
serving. Running them in the entrypoint instead would buy an outage — gunicorn
booting against a mismatched schema, or ten retries with no server at all — and
would race between replicas.

`collectstatic` is the one thing that runs in both places, because `preDeploy`
runs in a throwaway container whose filesystem never reaches the one serving
traffic. It is also allowed to fail: an unstyled site beats no site. That
asymmetry is deliberate and documented in `start.sh` rather than tidied up.

## Backing up

Railway's own backups and point-in-time recovery are features only available on a higher tier of paid-plan that this
project isn't on, so dumps are pulled down to a machine in the house instead:

```sh
scripts/backup-db.sh                    # dump + integrity check + prune
scripts/backup-db.sh --verify-restore   # also restore it and count the rows back
```

It runs `pg_dump` inside a container pinned to the server's major version,
because a mismatched client refuses to dump and writes nothing. Credentials are
read from the Railway CLI at run time and never stored. A full cycle including
the restore test takes about ten seconds against a 25 MB database.

The copy being *elsewhere* is the point, not that it's cheaper: platform-held
backups share an account with the thing they protect. What these dumps do not
hold is the product photography, which lives in a bucket — a known gap, written
down rather than half-solved.

## Conventions, and the tests that enforce them

Most of the rules here are executable, because the failure mode of each one is
silent:

- **`URLBucketTests`** — the first path segment (`private/`, `public/`,
  `secret/`, `webhooks/`) must match what the view actually does. This caught an
  endpoint accepting anonymous POSTs that created recipes.
- **`LedgerTests.test_every_stock_writer_goes_through_the_ledger`** — reads the
  source files and fails on a direct assignment to `number_on_hand`.
- **`PickerPageConventionTests`** — a page at `foo/<int:id>/` must have a `foo/`
  picker listing the choices, or the site map grows a card nobody can click.
- **`BaseTemplateTests`** — catches a page-level `{% block style %}` missing its
  `{{ block.super }}`, whose only symptom is a perfectly valid 200 with no CSS.
- **`RetireDontDeleteTests`** — anything recording *what happened* points at its
  product with `on_delete=PROTECT`. Products retire with `is_active=False`; they
  don't delete, because the history outlives the product.
- **`UnknownRouteTests`** — there is no 404 here. `mysite/urls.py` ends in a
  catch-all redirecting to the public map, so a typo in a `{% url %}` tag lands
  on a working-looking page. This pins that real routes still resolve.

Two navigation notes that follow: route **names** are the stable interface
(nothing hardcodes a path, including in tests), and the site map at
`/scarves/private/` is generated at request time by introspecting the URLconf,
so a new `@page_meta` page appears on it without an edit.

## Known gaps

Kept here deliberately, because the alternative to writing them down is
rediscovering them:

- **Raw stock has no ledger.** Finished stock moves through `scarves/ledger.py`
  and nowhere else; undyed blanks still move in three hand-written places. The
  replacement (`RawStockMove`) is designed and not built.
- **No CI.** The suite runs locally in about three minutes and a pre-push hook
  gates `main`, which covers the case it exists for while the committer is one
  person. What a hook can't do is prove the tests ran on a machine that isn't
  this one, and `--no-verify` walks past it by design. The tradeoff flips the
  moment a second person commits.
- **A single replica, and no load to justify a second.**
- **`ALLOWED_HOSTS = ["*"]`**, leaning on Railway's edge routing rather than
  saying which hosts are real.
- **`secret/` is not a security boundary** and never claimed to be — it means
  unlisted, not protected. Since this repo is public, those routes are readable
  here, so the per-employee PIN inside each page is doing all of the actual
  work. That was always the design; the repo being public makes it certain
  rather than likely.

## Why the docs read like that

The `docs/claude/` files argue with the reader, and the commit messages are
sentences (`Par 0 means par 0, and every page says what paper already claims`).
Both are on purpose. A feature here is usually a correction of something that
failed quietly — a dye room sent to make a colour nobody sells any more, a
production signal driven by how many pegs the stall happens to own — and a
description of the resulting code tells you nothing about the mistake it
prevents. So the files record the reasoning, and specifically the version that
looked better and was wrong, because that is the version somebody will propose
again.
