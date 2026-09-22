"""Reports: pages that read and never write. Top and slow sellers, stock value, the season, close history."""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.shortcuts import render

from ..sitemap import page_meta
from ..models import CloseRun, RawProduct, RawProductCategory
from .. import closing, dyebill, sales, seasonreport, slowsellers, stockvalue
from .. import seasons as seasons_mod


@page_meta(
    title="Slow Sellers",
    description="Colorways that sold one or none over a range, with what was "
                "on hand — so a zero that sat on the table is told apart from "
                "a zero nobody could buy.",
    category="Reports",
)
@login_required
def slow_sellers(request):
    """The bottom of the list, which is a different question from the top.

    A product that sold nothing has no row to aggregate, so this starts from
    the catalogue and subtracts — and the number that decides what to do
    about a zero is the stock beside it, not the zero itself.
    """
    rng = slowsellers.season_range(request.GET)

    raw = (request.GET.get("max") or "").strip()
    # A cap somebody typed, because "not worth dyeing" is a judgement about
    # this shop's season and not a constant. Clamped rather than refused: a
    # silly number is a typo, and losing the page to it helps nobody.
    max_units = int(raw) if raw.isdigit() else 1
    max_units = min(max_units, 20)

    category = None
    category_id = request.GET.get("category")
    if category_id and category_id.isdigit():
        category = RawProductCategory.objects.filter(pk=category_id).first()

    # **Colorway is the default, and the per-blank view is the opt-in.**
    # A colour that sells on three yarns and not the fourth is not a dog:
    # production cadence absorbs it, and it is still doing a job on the
    # display, where a full colourful stall is worth something the sales
    # column cannot show. What this page is for is the colour that sells
    # nowhere — and that only exists once the blanks are pooled.
    by_colorway = request.GET.get("group") != "product"
    if by_colorway:
        found = slowsellers.colorway_rows(
            rng, max_units=max_units, category=category
        )
    else:
        found = slowsellers.rows(rng, max_units=max_units, category=category)
    # A reveal, not a mode — nothing carries it onward, the same inversion
    # `?bare=1` makes on the restock board.
    if request.GET.get("never") == "1":
        found = [row for row in found if row.never_out]

    return render(request, "scarves/slow_sellers.html", {
        "rows": found,
        "tally": slowsellers.tally(found),
        "range": rng,
        "max_units": max_units,
        "categories": RawProductCategory.objects.order_by("name"),
        "category": category,
        "never_only": request.GET.get("never") == "1",
        "by_colorway": by_colorway,
        # Everything the toggles have to carry so a click keeps the rest of
        # the reading — the colour page's pills, again.
        "carry": urlencode(
            {k: v for k, v in (
                ("max", max_units),
                ("range", rng.key),
                ("category", category.pk if category else ""),
            ) if v not in ("", None)}
        ),
        # The page's own blind spots, printed under the table: sales with no
        # colorway at all, and sales that all landed on one.
        "unattributed": slowsellers.unattributed(rng, category=category),
        "lopsided": slowsellers.lopsided(rng, category=category),
    })


@page_meta(
    title="Stock Value",
    description="What is on the shelves in dollars — undyed, dyed and "
                "bought-in — at what it cost and at what it is priced to "
                "sell for, with undyed yarn counted in dye baths as well as "
                "skeins.",
    category="Reports",
)
@login_required
def stock_value(request):
    """The shelves as a balance rather than a count.

    Reads three piles that do not overlap, so the totals add up: undyed
    blanks waiting for a bath, dyed colorways, and the passthroughs whose one
    physical pile is valued on its finished row only.

    **Nothing here is a signal.** A bath count off what is on the shelf is
    capacity proposing production, which is the one coupling this app works
    hardest to keep broken, so it is printed and read by nothing.
    """
    found = stockvalue.sections()
    return render(request, "scarves/stock_value.html", {
        "sections": found,
        "totals": stockvalue.totals(found),
    })


@page_meta(
    title="Dye Statements",
    description="What each closed dye session was worth — baths, units, the "
                "blanks at what they cost and the output at what it is priced "
                "at, frozen when the session closed rather than re-read from "
                "today's prices.",
    category="Reports",
)
@login_required
def dye_statements(request):
    """One statement per run, priced as it was when it closed.

    The figures are read off the rows, not recomputed: `apply_row` freezes a
    bath's cost and retail when it is accepted, because this is what a bill
    for the dyeing is drawn from and a bill is against the prices at the time.

    A bath whose entry was taken back afterwards is off the statement — the
    question is whether an entry still stands — and a bath accepted before the
    figures were kept is counted and left unvalued rather than valued at zero.
    """
    found = dyebill.statements()
    return render(request, "scarves/dye_statements.html", {
        "statements": found,
        "totals": dyebill.totals(found),
    })


@page_meta(
    title="Close History",
    description="What each Sunday close found: the products the app had "
                "wrong, which way, and by how much.",
    category="Reports",
)
@login_required
def close_history(request):
    """What the closes have caught, newest day first.

    Reads as a list of failures on purpose — that is the output. Extra tags
    are stock that left without registering: a swapped sale, a hand-keyed
    line, or a webhook that has quietly stopped delivering, which physically
    becomes an extra tag about a week later and is findable here without
    going near Square. Missing tags are the other end of the pipeline, stock
    that arrived without being recorded.
    """
    runs = (
        CloseRun.objects.select_related("employee")
        .prefetch_related(
            "rows__finished_product__recipe",
            "rows__finished_product__raw_product",
        )[:26]
    )
    entries = [{"run": run, "tally": closing.tally(run)} for run in runs]

    # A product that keeps coming back is the useful reading. One weekend's
    # disagreement is noise; the same SKU three weekends running is a cause
    # with a name on it.
    repeats = {}
    for entry in entries:
        for row in entry["tally"]["missing_rows"] + entry["tally"]["extra_rows"]:
            repeats.setdefault(row.finished_product, []).append(row)
    repeat_rows = sorted(
        ({"product": p, "rows": rs} for p, rs in repeats.items() if len(rs) > 1),
        key=lambda d: -len(d["rows"]),
    )

    return render(request, "scarves/close_history.html", {
        "entries": entries,
        "repeat_rows": repeat_rows,
        "found_total": sum(e["tally"]["disagreements"] for e in entries),
        "under_total": sum(e["tally"]["under_units"] for e in entries),
        "over_total": sum(e["tally"]["over_units"] for e in entries),
    })


#: The table, in reading order: what it is, then what it did, then what is
#: left. Every one is sortable — a column you can see and can't sort by is a
#: question the page can obviously answer and won't.
SALES_COLUMNS = [
    ("name", "Product"),
    ("blank", "Style"),
    ("colorway", "Colorway"),
    ("units", "Units sold"),
    ("transactions", "Sales"),
    ("days", "Days"),
    ("value", "Value"),
    ("last", "Last sold"),
    ("on_hand", "On hand / par"),
    ("short", "Short"),
]


def _int_or_none(text):
    """A positive integer off the query string, or None.

    A hand-edited or stale `?blank=` degrades to no filter rather than a 500
    — the catch-all redirect means bad links are ordinary here, and the
    failure worth avoiding is a page that errors instead of answering.
    """
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _hidden(base, *keys):
    """The subset of the page's state one form has to carry for the other."""
    return [(k, base[k]) for k in keys
            if base.get(k) not in (None, "", 0)]


def _seasons_href(base, **overrides):
    """`private/seasons/` with the current reading's state, minus overrides.

    Every control carries the rest of the state — switching to per-day keeps
    the focused season, the categories and the faire. Same rule the colour
    page's pills and the sales report's headings follow: the useful views are
    combinations, and a control that resets the others means a combination can
    only be reached by starting over.
    """
    params = dict(base)
    params.update(overrides)
    categories = params.pop("cat", None) or []
    pairs = [(k, v) for k, v in params.items() if v not in (None, "", 0)]
    pairs += [("cat", name) for name in categories]
    query = urlencode(pairs)
    return reverse("season_report") + (f"?{query}" if query else "")


@page_meta(
    title="Season Pace",
    description="This season against the ones behind it, indexed on the "
                "weekend of the run rather than the calendar date. Dollars or "
                "units, cumulative, per weekend, or per trading day.",
    category="Reports",
    note="?faire=&year=&mode=cum|weekend|day&metric=net|units&cat=&blank=",
)
@login_required
def season_report(request):
    """Season against season on the weekend axis.

    **Per trading day is offered beside per weekend because weekend 2 carries
    Labor Day Monday.** Its weekly total runs about a third above its
    neighbours for a reason that has nothing to do with trade, and a page
    offering only the weekly figure would be read and acted on.

    **A projection is drawn only over weekends still ahead.** A past weekend
    with no lines is a gap — almost certainly an import nobody ran — and is
    named as one rather than filled in, because a projection sitting where a
    missing import should be is a number that looks like evidence.

    Everything is in the query string so a reading is a link somebody can
    send, and the categories are there because the wax hands were on this till
    through 2024 and are gone: a total that cannot say what it counts reads a
    discontinued line as a decline.
    """
    slugs = seasonreport.faire_slugs()
    if not slugs:
        return render(request, "scarves/season_report.html", {"no_faires": True})

    slug = request.GET.get("faire") or ""
    if slug not in {name for name, _count in slugs}:
        slug = slugs[0][0]

    every_category = seasonreport.categories_on_file()
    categories = [c for c in request.GET.getlist("cat") if c in every_category]

    # `?blank=` is the style, and it is named the same thing it is named on
    # `private/sales/` so a habit formed on one page works on the other. An
    # id that is not a blank with sales against it falls back to no filter
    # rather than erroring — a filter is navigation, and a stale link should
    # show more than was asked for rather than break.
    every_blank = seasonreport.blanks_on_file()
    blank_id = _int_or_none(request.GET.get("blank"))
    blank = next((b for b in every_blank if b.pk == blank_id), None)
    blank_id = blank.pk if blank else None

    # A palette is a mode: it follows you round the page the way `?photos=1`
    # follows a restock circuit, so every link carries it. Unknown values fall
    # back rather than erroring — it decides colours and nothing else.
    palette = request.GET.get("palette", "")
    if palette not in seasonreport.PALETTE_KEYS:
        palette = seasonreport.DEFAULT_PALETTE

    metric = request.GET.get("metric", "")
    if metric not in seasonreport.METRIC_KEYS:
        metric = seasonreport.DEFAULT_METRIC
    mode = request.GET.get("mode", "")
    if mode not in seasonreport.MODE_KEYS:
        mode = seasonreport.DEFAULT_MODE

    seasons = seasonreport.build(slug, categories or None, blank_id)
    for season in seasons:
        seasonreport.metric_of(season, metric)

    with_data = [s for s in seasons if s.has_any_data]
    known_years = [s.year for s in with_data]

    # Default focus is the most recent season that has anything in it — the
    # one somebody opening the page is asking about. An unreadable or unknown
    # year falls back rather than erroring: a filter is navigation, and a
    # stale link should show something rather than break.
    focus_year = _int_or_none(request.GET.get("year"))
    if focus_year not in known_years:
        # Falling back to the newest season with sales in it is the ordinary
        # case. With none imported at all it still focuses the newest season,
        # because the calendar and the weather are real data worth showing
        # before any export has been loaded — a page that waits for sales to
        # say anything is a page that looks broken on the day it ships.
        focus_year = known_years[-1] if known_years else (
            seasons[-1].year if seasons else None
        )

    focus = next((s for s in seasons if s.year == focus_year), None)
    priors = [s for s in with_data if s.year != focus_year]
    projected_total = seasonreport.project(focus, priors) if focus else None

    shares = seasonreport.share_of_season(with_data)
    weekend_numbers = sorted({w.number for s in seasons for w in s.weekends})

    base = {
        "faire": slug,
        "year": focus_year,
        "mode": mode,
        "metric": metric,
        "palette": palette,
        "blank": blank_id,
        "cat": categories,
    }

    return render(request, "scarves/season_report.html", {
        "slugs": slugs,
        "slug": slug,
        "seasons": seasons,
        "with_data": with_data,
        "empty_seasons": [s for s in seasons if not s.has_any_data],
        "focus": focus,
        "focus_year": focus_year,
        "priors": priors,
        "projected_total": projected_total,
        "shares": shares,
        "weekend_numbers": weekend_numbers,
        # The trading-day row is a property of the run, not of any one
        # season, so it is read off whichever season is on screen first.
        "shape": focus or (seasons[0] if seasons else None),
        "labor_day_weekend": seasons_mod.LABOR_DAY_WEEKEND,
        "weather_any": bool(focus and any(w.has_weather for w in focus.weekends)),
        "chart": seasonreport.chart(seasons, focus_year, mode, metric),
        "mode": mode,
        "metric": metric,
        "palette": palette,
        "modes": seasonreport.MODES,
        "metrics": seasonreport.METRICS,
        "every_category": every_category,
        "categories": categories,
        "every_blank": every_blank,
        "blank": blank,
        "blank_id": blank_id,
        # A select rather than a row of pills: there are twenty-five blanks
        # in the ledger, which is a paragraph of pills and a page of them
        # once the categories are there too. The form carries the rest of
        # the reading as hidden fields so choosing a style keeps the faire,
        # the focused year, the mode, the metric and the categories.
        "blank_hidden": _hidden(
            base, "faire", "year", "mode", "metric", "palette"
        ) + [("cat", name) for name in categories],
        "clear_blank": _seasons_href(base, blank=None),
        "sources": seasonreport.source_breakdown(
            slug, categories or None, blank_id
        ),
        "is_money": metric == seasonreport.METRIC_NET,
        "href": {
            "base": base,
            "clear_categories": _seasons_href(base, cat=[]),
        },
        "mode_links": [
            {"key": key, "label": label, "on": key == mode,
             "href": _seasons_href(base, mode=key)}
            for key, label in seasonreport.MODES
        ],
        "palettes": seasonreport.PALETTES,
        "palette_links": [
            {"key": entry["key"], "label": entry["label"],
             "on": entry["key"] == palette,
             "href": _seasons_href(base, palette=entry["key"])}
            for entry in seasonreport.PALETTES
        ],
        "metric_links": [
            {"key": key, "label": label, "on": key == metric,
             "href": _seasons_href(base, metric=key)}
            for key, label in seasonreport.METRICS
        ],
        "year_links": [
            {"year": s.year, "on": s.year == focus_year,
             "href": _seasons_href(base, year=s.year)}
            for s in with_data
        ],
        "faire_links": [
            {"slug": name, "count": count, "on": name == slug,
             "href": _seasons_href(base, faire=name, year=None)}
            for name, count in slugs
        ],
        "category_links": [
            {"name": name, "on": name in categories,
             "href": _seasons_href(
                 base,
                 cat=[c for c in categories if c != name] if name in categories
                     else sorted(categories + [name]),
             )}
            for name in every_category
        ],
    })


def _sales_href(base, **overrides):
    """`private/sales/` with the current view's state, minus what's overridden.

    Every control on the page carries the whole of the rest of the state —
    sorting a filtered range keeps the filter and the range, and a pill keeps
    the sort. Same rule the colour page's pills follow, for the same reason:
    the useful views are combinations, and a control that resets the others
    means the combination can only be reached by starting again.
    """
    params = dict(base)
    params.update(overrides)
    params = {k: v for k, v in params.items() if v not in (None, "", 0)}
    query = urlencode(params)
    return reverse("sales_report") + (f"?{query}" if query else "")


@page_meta(
    title="Top Sellers",
    description="What sold over a date range, one row per finished product: "
                "units, how many separate sales, and what's left against par. "
                "Today, yesterday, or dates you pick.",
    category="Reports",
    note="Sortable columns; ?range=today|yesterday|7|30|all or ?from=&to=",
)
@login_required
def sales_report(request):
    """Top sellers over a range, sortable and narrowable.

    The whole of the page's state is in the query string — range, filters,
    sort column and direction — so a particular reading is a link somebody
    can send, and the back button walks back through the questions asked
    rather than dumping you at today's default.

    **`on hand / par` is one column, not two.** The number that matters after
    "twelve of these sold" is not the stock and not the target but the gap
    between them, and putting them side by side in one cell is what makes it
    readable without arithmetic. It is sorted on the shortfall for the same
    reason.

    Nothing here schedules anything. It is a page somebody reads: a colorway
    at the top of this table with nothing left is an argument for raising its
    par, which stays a deliberate decision about demand rather than something
    a report gets to make.
    """
    rng = sales.resolve_range(request.GET)

    q = request.GET.get("q", "").strip()
    category_id = _int_or_none(request.GET.get("category"))
    raw_product_id = _int_or_none(request.GET.get("blank"))

    sort = request.GET.get("sort", "")
    if sort not in sales.SORTS:
        sort = sales.DEFAULT_SORT
    direction = request.GET.get("dir", "")
    if direction not in ("asc", "desc"):
        direction = "desc" if sort in sales.DESCENDING_FIRST else "asc"

    logs = sales.narrow(
        sales.sale_logs(rng), q=q,
        category_id=category_id, raw_product_id=raw_product_id,
    )
    rows = sales.product_rows(logs)
    rows = sales.sort_rows(rows, sort, descending=direction == "desc")

    # The state every link on the page starts from.
    base = dict(rng.querystring())
    base.update({
        "q": q,
        "category": category_id,
        "blank": raw_product_id,
        "sort": sort,
        "dir": direction,
    })

    # Column headings. A heading already sorted flips direction; any other
    # heading opens the way that column reads first — biggest-first for a
    # ranking, A-first for a name.
    columns = []
    for key, label in SALES_COLUMNS:
        if key == sort:
            nxt = "asc" if direction == "desc" else "desc"
        else:
            nxt = "desc" if key in sales.DESCENDING_FIRST else "asc"
        columns.append({
            "key": key,
            "label": label,
            "sorted": key == sort,
            "direction": direction if key == sort else "",
            "href": _sales_href(base, sort=key, dir=nxt),
        })

    return render(request, "scarves/sales_report.html", {
        "range": rng,
        "ranges": [
            {
                "key": key,
                "label": label,
                "on": rng.key == key,
                # "Choose dates" is the form below rather than a link, so it
                # is a pill that only ever shows state.
                "href": None if key == "custom" else _sales_href(
                    base, range=key, **{"from": None, "to": None}
                ),
            }
            for key, label in sales.RANGES
        ],
        "rows": rows,
        "columns": columns,
        "totals": sales.totals(rows),
        "sources": sales.by_source(logs),
        "q": q,
        "category_id": category_id,
        "blank_id": raw_product_id,
        "categories": RawProductCategory.objects.all(),
        "blanks": RawProduct.objects.filter(
            finished_products__isnull=False
        ).distinct().order_by("name"),
        "sort": sort,
        "direction": direction,
        # Each of the two forms carries the state the other one owns, as
        # hidden fields, so submitting either keeps everything already set —
        # typing a colour name must not silently drop back to today.
        "date_hidden": _hidden(base, "q", "category", "blank", "sort", "dir"),
        "filter_hidden": _hidden(base, "range", "from", "to", "sort", "dir"),
        "clear_href": _sales_href(dict(rng.querystring())),
        "any_filter": bool(q or category_id or raw_product_id),
    })
