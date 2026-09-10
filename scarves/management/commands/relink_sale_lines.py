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

Nothing in this command moves stock or writes an `InventoryLog`. The sales
ledger is reporting only.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum

from scarves.models import FinishedProduct, SaleLine


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
            "--dry-run",
            action="store_true",
            help="Print what would be linked without linking it.",
        )

    def handle(self, *args, **options):
        aliases = {}
        for entry in options["alias"]:
            if "=" not in entry:
                raise CommandError(
                    f"--alias wants PRICE_POINT=PRODUCT, got {entry!r}."
                )
            left, right = entry.split("=", 1)
            aliases[left.strip().casefold()] = right.strip().casefold()

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
