"""Shared machinery for loading the sales ledger, whatever supplied it.

There are two doors — a Square itemised CSV export and the Orders API — and
they must not grow their own copies of the matching, the de-duplication or
the reconciliation report. The way that drift would show is the worst
available: two totals for the same weekend, differing by a handful of lines,
with nothing on either to say which was right.

So a loader's only job is to turn its source into a list of line dicts. Every
one of these keys is required:

    order_id sold_at line_key event_type category item_name price_point
    sku quantity gross_cents discount_cents net_cents tax_cents
    location device customer_name card_brand

`Matcher.attach` then fills in `finished_product` and `raw_product`, and
`write` puts them away.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from django.db import transaction

from .models import FinishedProduct, RawProduct, Sale, SaleLine


def words(name):
    """A name as its uppercase alphanumeric words.

    The unit of comparison between a Square item name and a blank's name,
    because the two are written to different audiences. Square's item is what
    the till shows a customer — `Noble` — and the blank carries the supplier's
    full description of the same yarn — `Noble - Diamond Extra`. Splitting on
    the punctuation is what lets one be recognised as the head of the other
    without matching on a character count.
    """
    return tuple(word for word in re.split(r"[^A-Z0-9]+", (name or "").upper())
                 if word)


class BlankIndex:
    """Item name to blank: the whole name, or the words it opens with.

    This used to key on `skus.slug`, which is six characters because that is
    what fits on a barcode label — a width borrowed from a place it meant
    something into a place it meant nothing. It turned "is this the same
    name" into "do the first six characters agree", and the answer was right
    for three of the four base yarns by luck of spelling: `Heavenly - Angel`,
    `Homespun - Single & Stunning` and `Artisan - Ethereal Fingering` all
    survive the truncation with their first word intact. **`Noble` is five
    letters**, so `Noble - Diamond Extra` truncated to `NOBLED` and the 179
    lines Square rang up under `Noble` in 2025 — $9,759 of yarn — matched no
    blank at all. They imported unattached and read as a season in which
    Noble sold nothing, which is the failure this ledger is least able to
    show you: `raw_product` is null and a blank filter returns zero rather
    than an error.

    So a match is now the full name, or the blank's opening words. An item
    name that opens two blanks is **refused, not guessed** — `Fancy` is the
    head of both `Fancy Veil` and `Fancy Half Circle Veil`, and filing a
    season's fancy work under whichever row was created first is worse than
    leaving it unmatched, where the report names it and somebody can look.
    """

    def __init__(self, blanks):
        self.exact, self.heads = {}, {}
        ambiguous = set()
        for blank in blanks:
            spelling = words(blank.name)
            self.exact.setdefault(spelling, blank)
            # Every leading run of words short of the whole name. The whole
            # name is an exact match and outranks any other blank's head:
            # a blank called `Shawl` owns the item `Shawl` even if some later
            # `Shawl Extra Long` starts the same way.
            for size in range(1, len(spelling)):
                head = spelling[:size]
                if self.heads.get(head, blank) != blank:
                    ambiguous.add(head)
                self.heads.setdefault(head, blank)
        for head in ambiguous:
            self.heads.pop(head, None)

    def get(self, item_name):
        spelling = words(item_name)
        if not spelling:
            return None
        return self.exact.get(spelling) or self.heads.get(spelling)


class Matcher:
    """Links a line to the catalogue, best evidence first.

    Three tiers, and the order is the order of how much the evidence is
    worth:

    1. **Square's own variation id.** Unambiguous — Square is naming the
       object this app synced to it. Only the Orders API carries it; the CSV
       export does not, which is the single biggest reason to prefer the API.
    2. **The SKU**, when the export bothered to print one. Twenty of the
       thirty-six lines in a 2026 CSV carry none.
    3. **The item name against a blank**, whole or by its opening words —
       see `BlankIndex`. Coarser — it gets the style, not the colorway — but
       it is present on every line of every season, which is what makes the
       old years readable at all.

    All three lookups are built once. Matching a hundred thousand lines a
    query at a time is the mistake the unidentified-sales page already made.
    """

    def __init__(self):
        products = list(FinishedProduct.objects.select_related("raw_product"))
        self.by_variation = {
            product.square_variation_id: product
            for product in products if product.square_variation_id
        }
        self.by_sku = {product.sku: product for product in products if product.sku}
        self.blanks = BlankIndex(RawProduct.objects.all())

        self.hits = Counter()
        self.unmatched = Counter()

    def attach(self, line):
        product = (
            self.by_variation.get(line.get("square_variation_id") or "")
            or self.by_sku.get(line.get("sku") or "")
        )
        if product is not None:
            line["finished_product"] = product
            line["raw_product"] = product.raw_product
            self.hits["variation" if line.get("square_variation_id") in self.by_variation
                      else "sku"] += 1
            return

        blank = self.blanks.get(line["item_name"])
        line["finished_product"] = None
        line["raw_product"] = blank
        if blank is not None:
            self.hits["item"] += 1
        else:
            self.unmatched[line["item_name"]] += 1


class Result:
    def __init__(self):
        self.written = 0
        self.already = 0
        #: Orders already loaded by a *different* pipeline, skipped whole.
        self.foreign = Counter()


def write(lines, source, force=False):
    """Put the lines away, an order at a time.

    **The order is the unit of de-duplication across sources.** A CSV export
    and the Orders API describe the same sale differently — the export
    aggregates identical items onto one line and the API does not — so an
    order loaded twice by two doors would come out as two overlapping sets of
    lines with no way to tell which was double-counted. An order already on
    file from another pipeline is therefore skipped whole and reported, rather
    than merged line by line.

    Within one pipeline the behaviour is unchanged: lines are matched on
    `line_key`, so re-running the same file adds nothing and a file that has
    grown a line since gets just that line.
    """
    result = Result()
    by_order = defaultdict(list)
    for line in lines:
        by_order[line["order_id"]].append(line)

    with transaction.atomic():
        for order_id, order_lines in by_order.items():
            first = order_lines[0]
            sale = Sale.objects.filter(order_id=order_id).first()
            if sale is not None and sale.source != source and not force:
                result.foreign[sale.source] += len(order_lines)
                continue
            if sale is None:
                sale = Sale.objects.create(
                    order_id=order_id,
                    sold_at=first["sold_at"],
                    location=first["location"],
                    device=first["device"],
                    customer_name=first["customer_name"],
                    card_brand=first["card_brand"],
                    source=source,
                )
            for line in order_lines:
                _row, made = SaleLine.objects.get_or_create(
                    sale=sale,
                    line_key=line["line_key"],
                    defaults={
                        "sold_at": line["sold_at"],
                        "event_type": line["event_type"],
                        "category": line["category"],
                        "item_name": line["item_name"],
                        "price_point": line["price_point"],
                        "sku": line["sku"],
                        "square_variation_id": line.get("square_variation_id", ""),
                        "quantity": line["quantity"],
                        "gross_cents": line["gross_cents"],
                        "discount_cents": line["discount_cents"],
                        "net_cents": line["net_cents"],
                        "tax_cents": line["tax_cents"],
                        "finished_product": line.get("finished_product"),
                        "raw_product": line.get("raw_product"),
                        "source": source,
                    },
                )
                result.written += made
                result.already += not made
    return result


def report(command, lines, skipped, matcher, result, dry_run):
    """The reconciliation print, shared so both doors say the same things."""
    from . import seasons
    from .models import FaireDay

    out, style = command.stdout, command.style

    out.write(style.MIGRATE_HEADING("\nRead"))
    out.write(f"  {len(lines)} lines across {len({l['order_id'] for l in lines})} orders")
    if lines:
        out.write(f"  {min(l['sold_at'] for l in lines):%d %b %Y} to "
                  f"{max(l['sold_at'] for l in lines):%d %b %Y}")
    for reason, count in skipped.items():
        out.write(style.WARNING(
            f"  {reason}" if count == 0 else f"  skipped {count}: {reason}"
        ))

    refunds = sum(1 for l in lines if l["event_type"] == SaleLine.REFUND)
    if refunds:
        out.write(f"  {refunds} refund line(s)")

    out.write(style.MIGRATE_HEADING("\nMoney, as the source states it"))
    by_category = defaultdict(int)
    for line in lines:
        by_category[line["category"] or "(uncategorised)"] += line["net_cents"]
    for name, cents in sorted(by_category.items(), key=lambda kv: -kv[1]):
        out.write(f"  {name:<28} {_usd(cents):>13}  net")
    sales = [l for l in lines if l["event_type"] != SaleLine.REFUND]
    refunded = [l for l in lines if l["event_type"] == SaleLine.REFUND]
    gross = sum(l["gross_cents"] for l in sales)
    discount = sum(l["discount_cents"] for l in sales)
    refunds = sum(l["net_cents"] for l in refunded)
    net = sum(l["net_cents"] for l in lines)
    tax = sum(l["tax_cents"] for l in lines)
    # Four numbers, named, because "the total" is four different figures and a
    # season compared against the wrong one is out by whatever the discounts
    # ran to that year.
    out.write(f"  {'':<28} {'':>13}")
    out.write(f"  {'gross, before discounts':<28} {_usd(gross):>13}")
    out.write(f"  {'discounts':<28} {_usd(discount):>13}")
    out.write(f"  {'refunds':<28} {_usd(refunds):>13}")
    out.write(f"  {'NET':<28} {_usd(net):>13}")
    out.write(f"  {'tax, on top':<28} {_usd(tax):>13}")
    out.write(style.HTTP_INFO(
        "  ↑ reconcile against Square's own dashboard for the same range "
        "before trusting anything built on it."
    ))

    out.write(style.MIGRATE_HEADING("\nMatched to the catalogue"))
    out.write(f"  {matcher.hits['variation']} by Square variation id")
    out.write(f"  {matcher.hits['sku']} by SKU")
    out.write(f"  {matcher.hits['item']} by item name (blank only, no colorway)")
    if matcher.unmatched:
        out.write(style.WARNING(f"  {sum(matcher.unmatched.values())} matched nothing:"))
        for name, count in matcher.unmatched.most_common():
            out.write(style.WARNING(f"      {name or '(no item name)'} ×{count}"))
        out.write("    Kept in full — item and price point are text on the row, "
                  "so they still count toward every total.")

    out.write(style.MIGRATE_HEADING("\nPlaced in a season"))
    known = {day.date: day for day in FaireDay.objects.select_related("faire")}
    placed = Counter()
    for line in lines:
        day = known.get(line["sold_at"].date())
        placed[day.faire.year if day else seasons.labor_day_season_for(line["sold_at"].date())] += 1
    for year, count in sorted(placed.items(), key=lambda kv: (kv[0] is None, kv[0])):
        if year is None:
            out.write(style.WARNING(
                f"  {count} lines fall outside any faire — kept, and excluded "
                "from season reporting by construction."
            ))
        else:
            generated = any(day.faire.year == year for day in known.values())
            note = "" if generated else f"  (run generate_faire --year {year})"
            out.write(f"  {year}: {count} lines{note}")

    out.write("")
    if dry_run:
        out.write(style.WARNING("DRY RUN — nothing written."))
        return
    out.write(style.SUCCESS(
        f"Written: {result.written} new lines, {result.already} already on file."
    ))
    for source, count in result.foreign.items():
        out.write(style.WARNING(
            f"  {count} line(s) skipped: their order is already on file from "
            f"'{source}'. An order is the unit — two doors describe the same "
            "sale differently, so merging them line by line would double-count."
        ))
    out.write("No stock was moved and no InventoryLog row was written.")


def _usd(cents):
    return f"${cents / 100:,.2f}"
