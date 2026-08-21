"""Make the sellable half of each undyed yarn.

Creating one of these is two rows: a `RawProduct` for the pile, and a
`FinishedProduct` for the thing Square sells. The second is mechanical —
same name, no recipe, price off the raw product — so it is worth doing in one
pass rather than typing it out per yarn in the admin.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from scarves.models import CatalogGroup, FinishedProduct, RawProduct

#: Price used when the raw product hasn't got a usable one. Deliberately
#: conspicuous rather than plausible: the alternative on offer was cost times
#: three, a number that might reach a customer without anyone looking at it
#: twice. A pound gets noticed and fixed.
#:
#: A pound rather than nothing, because zero is the dangerous kind of wrong:
#: valid, syncs to Square, and rings the item up free at the till with a queue
#: behind it. Cost-times-three has exactly that floor whenever a blank has no
#: cost recorded, which is the other reason not to use it.
#:
#: **Null and zero are not the same thing here.** `suggested_price` is
#: nullable, so null means nobody set a price and zero means somebody set it
#: to zero — a giveaway is a real product. A deliberate zero is honoured and
#: reported; only a missing price is replaced.
FALLBACK_PRICE = Decimal("1.00")


class Command(BaseCommand):
    help = (
        "Create the FinishedProduct for every raw product in a catalog group "
        "that hasn't got one — for undyed stock sold exactly as it arrives."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--group",
            required=True,
            help="Catalog group name, e.g. 'Undyed Yarn'.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without creating it.",
        )

    def handle(self, *args, **options):
        name = options["group"]
        group = CatalogGroup.objects.filter(name__iexact=name).first()
        if group is None:
            known = ", ".join(CatalogGroup.objects.values_list("name", flat=True))
            raise CommandError(
                f"No catalog group called {name!r}."
                + (f" Known groups: {known}." if known else " There are none yet.")
            )

        raw_products = list(
            RawProduct.objects.filter(catalog_group=group, is_active=True)
            .order_by("name")
        )
        if not raw_products:
            raise CommandError(
                f"{group.name} has no active raw products, so there is nothing "
                f"to create. Check the raw products have their catalog group set."
            )

        # Already spoken for. Keyed on the raw product having *a passthrough*
        # rather than any finished product at all: a blank that is both sold
        # undyed and dyed into colorways is a thing that could exist, and its
        # colorways must not stop the undyed one being made.
        existing = set(
            FinishedProduct.objects
            .filter(raw_product__in=raw_products, recipe__isnull=True)
            .values_list("raw_product_id", flat=True)
        )

        made, skipped, guessed, free = [], [], [], []
        for raw in raw_products:
            if raw.pk in existing:
                skipped.append(raw)
                continue

            price = raw.suggested_price
            if price is None:
                price = FALLBACK_PRICE
                guessed.append(raw)
            elif price == 0:
                # Somebody typed zero. Taken at its word — a giveaway is a
                # real product — but said out loud, because free is the one
                # price nobody notices until it has been charged.
                free.append(raw)

            if options["dry_run"]:
                made.append((raw, price, "(not saved)"))
                continue

            product = FinishedProduct.objects.create(
                name=raw.name,
                raw_product=raw,
                recipe=None,            # never dyed — see FinishedProduct.recipe
                price=price,
                # The par that matters for these lives on the raw product, as
                # `par_level`, because you order them rather than making them.
                # A par here would be a number nothing reads.
                par=0,
            )
            made.append((raw, price, product.sku))

        for raw, price, sku in made:
            self.stdout.write(f"  {raw.name}  ${price}  {sku}")
        if skipped:
            self.stdout.write(
                f"{len(skipped)} already had one: "
                + ", ".join(r.name for r in skipped[:5])
                + (" ..." if len(skipped) > 5 else "")
            )
        if free:
            self.stderr.write(self.style.WARNING(
                f"{len(free)} will ring up free at $0.00, which is what their "
                f"suggested price says: "
                + ", ".join(r.name for r in free)
            ))
        if guessed:
            # Loud, because the whole point of the fallback is being noticed.
            self.stderr.write(self.style.WARNING(
                f"{len(guessed)} had no usable suggested price and were set to "
                f"${FALLBACK_PRICE} — fix these before syncing: "
                + ", ".join(r.name for r in guessed)
            ))

        verb = "Would create" if options["dry_run"] else "Created"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {len(made)} product(s) under {group.name}."
        ))
        if made and not options["dry_run"]:
            self.stdout.write(
                "Next: check the prices, then `sync_to_square --dry-run`."
            )
