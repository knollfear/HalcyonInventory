"""Attach ledger lines to the products that did not exist when they imported.

`SaleLine` deliberately keeps `item_name` and `price_point` as text and links
to a product with `SET_NULL`, so a line means something with no link at all.
That is what lets a season import cleanly years before the catalogue caught
up — and it is also how a line ends up permanently unattached for no better
reason than that nobody had made the product yet.

The worked case is the undyed yarns. 2025 sold them under a Square item that
has since been archived, with ten price points — `Baby Yak Cloud`, `Tibetan 3
ply`, `Egyptian Yak` — that matched nothing in this app, because the
passthrough products did not exist. `create_passthrough_products` later made
every one of them. The lines did not change; what they could be matched
against did.

**It matches on the price point, and only for passthroughs.** A price point is
the colorway everywhere else in the catalogue, and a colorway is exactly what
must not be matched this way: nothing before 2026 carries one, so a name that
happens to collide would file a sale under a colour that was not on the cloth.
A passthrough has no colorway at all — its `variation_name` *is* the blank's
name — so the price point and the product name are the same string by
construction.

**Nothing is guessed.** A price point that does not match a product is named
rather than approximated, and `--alias` is how a person supplies the answer:
Square's `Loop de Loop Caramel` against this app's `Lop de Loop caramel` is a
spelling nobody can derive. Fuzzy matching here would quietly attribute one
yarn's revenue to another.

**`--by-item` is the other half of the same staleness**, and it attaches the
*blank* rather than a product. A line whose item name the matcher could not
place at import has `raw_product` null, which is the one kind of gap this
ledger cannot show you: the style filter on `private/seasons/` returns zero
for that blank, and zero reads as "it sold none that year" rather than as
"nobody matched it". The worked case is `Noble`, whose 179 lines and $9,759
of 2025 yarn matched nothing for as long as blanks were keyed on six
characters. Re-running the match is what links them, so this pass is worth
running after anything that adds a blank or changes how one is spelled.

It sets `raw_product` and never `finished_product`: an item name reaches the
style and stops there, and a line that arrived without a colorway must not
acquire one from a report.

Nothing in this command moves stock or writes an `InventoryLog`. The sales
ledger is reporting only.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum

from scarves.models import FinishedProduct, RawProduct, SaleLine
from scarves.salesimport import BlankIndex


class Command(BaseCommand):
    help = (
        "Link unattached SaleLines to passthrough products by matching the "
        "price point against the blank's name."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--item",
            help="Only lines whose Square item name is this, e.g. 'Undyed Yarn'.",
        )
        parser.add_argument(
            "--year",
            type=int,
            help="Only lines sold in this year.",
        )
        parser.add_argument(
            "--alias",
            action="append",
            default=[],
            metavar="PRICE_POINT=PRODUCT",
            help=(
                "Map a price point onto a blank name it does not spell the "
                "same way, e.g. --alias 'Loop de Loop Caramel=Lop de Loop "
                "caramel'. Repeatable."
            ),
        )
        parser.add_argument(
            "--variation",
            action="append",
            default=[],
            metavar="VARIATION_ID=PRODUCT",
            help=(
                "Map a Square variation id straight onto a blank name, for "
                "lines whose price point cannot identify them. Beats --alias "
                "whenever it applies, because the id is evidence and the name "
                "is a coincidence: Square's `Regular` carries nine Angel "
                "Delightful lines from the two days before that variation was "
                "renamed. Repeatable."
            ),
        )
        parser.add_argument(
            "--by-item",
            action="store_true",
            help=(
                "Attach the blank instead, by matching the Square item name "
                "against a RawProduct — for lines that carry no blank at all."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be linked without linking it.",
        )

    def handle(self, *args, **options):
        if options["by_item"]:
            return self.by_item(options)

        def pairs(entries, flag, shape):
            out = {}
            for entry in entries:
                if "=" not in entry:
                    raise CommandError(f"{flag} wants {shape}, got {entry!r}.")
                left, right = entry.split("=", 1)
                out[left.strip()] = right.strip().casefold()
            return out

        aliases = {
            k.casefold(): v
            for k, v in pairs(options["alias"], "--alias",
                              "PRICE_POINT=PRODUCT").items()
        }
        # Not casefolded: a Square id is a token, not a name.
        by_variation = pairs(options["variation"], "--variation",
                             "VARIATION_ID=PRODUCT")

        # Passthroughs only — see the module docstring for why a colorway must
        # never be matched on its price point.
        products = {}
        for product in (
            FinishedProduct.objects
            .filter(recipe__isnull=True, is_active=True)
            .select_related("raw_product")
        ):
            products[product.raw_product.name.strip().casefold()] = product
        if not products:
            raise CommandError(
                "There are no passthrough products to match against. Make "
                "them first — create_passthrough_products, or the Track "
                "button on private/unidentified-sales/."
            )

        lines = SaleLine.objects.filter(finished_product__isnull=True)
        if options["item"]:
            lines = lines.filter(item_name=options["item"])
        if options["year"]:
            lines = lines.filter(sold_at__year=options["year"])

        if not lines.exists():
            self.stdout.write("No unattached lines match that scope.")
            return

        matched, unmatched = {}, {}
        for line in lines.iterator():
            # The variation id first, because it is evidence about which
            # object sold, where the price point is only what it was called
            # at the time — and a variation gets renamed mid-season.
            key = by_variation.get(line.square_variation_id or "")
            if key is None:
                key = (line.price_point or "").strip().casefold()
                key = aliases.get(key, key)
            product = products.get(key)
            if product is None:
                bucket = unmatched.setdefault(
                    (line.price_point or "").strip(), [0, 0]
                )
                bucket[0] += 1
                bucket[1] += line.gross_cents or 0
                continue
            matched.setdefault(product, []).append(line.pk)

        total = sum(len(pks) for pks in matched.values())
        verb = "Would link" if options["dry_run"] else "Linked"

        for product, pks in sorted(matched.items(), key=lambda kv: kv[0].name):
            if not options["dry_run"]:
                # The blank as well as the product: a passthrough's raw row is
                # the pile, and every report that groups by blank reads it.
                SaleLine.objects.filter(pk__in=pks).update(
                    finished_product=product, raw_product=product.raw_product
                )
            self.stdout.write(f"  {product.name:36} {len(pks):>4} line(s)")

        self.stdout.write(self.style.SUCCESS(f"\n{verb} {total} line(s)."))

        if unmatched:
            # Named, with what they are worth, because that is what decides
            # whether an alias is worth writing. A count alone sends nobody
            # anywhere.
            self.stdout.write(self.style.WARNING(
                f"\n{sum(v[0] for v in unmatched.values())} line(s) matched no "
                f"product — give --alias if one of these is a spelling of a "
                f"blank you have:"
            ))
            for name, (n, cents) in sorted(unmatched.items()):
                self.stdout.write(
                    f"  {name or '(no price point)':36} {n:>4} line(s)  "
                    f"${cents / 100:,.2f}"
                )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN — nothing was written."))

    def by_item(self, options):
        """Attach the blank to lines that never got one.

        Deliberately narrower than the passthrough pass above: it only ever
        fills a `raw_product` that is null, so a line already filed under a
        blank — by its SKU, by Square's variation id, or by an earlier run of
        this — is left exactly as it is. Re-matching those would let a
        renamed item quietly move last season's revenue onto a different
        style, which is the kind of edit nothing downstream would question.
        """
        index = BlankIndex(RawProduct.objects.all())

        lines = SaleLine.objects.filter(raw_product__isnull=True)
        if options["item"]:
            lines = lines.filter(item_name=options["item"])
        if options["year"]:
            lines = lines.filter(sold_at__year=options["year"])
        if not lines.exists():
            self.stdout.write("No unattached lines match that scope.")
            return

        matched, unmatched = {}, {}
        for line in lines.iterator():
            blank = index.get(line.item_name)
            if blank is None:
                bucket = unmatched.setdefault((line.item_name or "").strip(), [0, 0])
                bucket[0] += 1
                bucket[1] += line.gross_cents or 0
                continue
            matched.setdefault(blank, []).append(line.pk)

        total = sum(len(pks) for pks in matched.values())
        verb = "Would link" if options["dry_run"] else "Linked"

        for blank, pks in sorted(matched.items(), key=lambda kv: kv[0].name):
            if not options["dry_run"]:
                SaleLine.objects.filter(pk__in=pks).update(raw_product=blank)
            self.stdout.write(f"  {blank.name:36} {len(pks):>4} line(s)")

        self.stdout.write(self.style.SUCCESS(f"\n{verb} {total} line(s) to a blank."))

        if unmatched:
            # Named and valued, because most of these are supposed to match
            # nothing — wax hands, refunds, the discount bin — and the only
            # way to tell those from a blank nobody has made yet is to read
            # the list.
            self.stdout.write(self.style.WARNING(
                f"\n{sum(v[0] for v in unmatched.values())} line(s) matched no "
                f"blank — these are item names this app has no RawProduct for:"
            ))
            for name, (n, cents) in sorted(unmatched.items(), key=lambda kv: -kv[1][1]):
                self.stdout.write(
                    f"  {name or '(no item name)':36} {n:>4} line(s)  "
                    f"${cents / 100:,.2f}"
                )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN — nothing was written."))
