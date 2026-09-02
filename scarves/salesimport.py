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

from collections import Counter, defaultdict

from django.db import transaction

from . import skus
from .models import FinishedProduct, RawProduct, Sale, SaleLine


class Matcher:
    """Links a line to the catalogue, best evidence first.

    Three tiers, and the order is the order of how much the evidence is
    worth:

    1. **Square's own variation id.** Unambiguous — Square is naming the
       object this app synced to it. Only the Orders API carries it; the CSV
       export does not, which is the single biggest reason to prefer the API.
    2. **The SKU**, when the export bothered to print one. Twenty of the
       thirty-six lines in a 2026 CSV carry none.
    3. **The item name against a blank.** Coarser — it gets the style, not
       the colorway — but it is present on every line of every season, which
       is what makes the old years readable at all.

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
        self.blanks = {}
        for blank in RawProduct.objects.all():
            self.blanks.setdefault(skus.slug(blank.name), blank)

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

        blank = self.blanks.get(skus.slug(line["item_name"]))
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
