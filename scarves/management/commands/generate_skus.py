import re

from django.core.management.base import BaseCommand

from scarves.models import FinishedProduct


def _slugify(text, max_len):
    """Uppercase alphanumeric only, truncated."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())[:max_len]


def _make_sku(fp, existing):
    raw = _slugify(fp.raw_product.name, 6)
    recipe = _slugify(fp.recipe.name, 6)
    base = f"{raw}-{recipe}"
    sku = base
    counter = 2
    while sku in existing:
        sku = f"{base}{counter}"
        counter += 1
    return sku


class Command(BaseCommand):
    help = "Auto-generate SKUs for FinishedProducts that don't have one yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help=(
                "Regenerate SKUs even for products that already have one. "
                "Asks first: a SKU that has been printed on a label is "
                "physically out in the world and cannot be recalled."
            ),
        )
        parser.add_argument(
            "--noinput", "--no-input",
            action="store_false", dest="interactive", default=True,
            help="Skip the --overwrite confirmation prompt.",
        )

    def handle(self, *args, **options):
        overwrite = options["overwrite"]

        # Overwriting was harmless while SKUs only lived in the database.
        # Since /scarves/private/labels/ exists, the string is also on
        # stickers stuck to scarves and in Square's catalogue, and neither
        # can be rewritten from here. The failure surfaces weeks later at the
        # till, so it gets a prompt rather than a line in --help.
        if overwrite:
            at_risk = (
                FinishedProduct.objects.filter(is_active=True)
                .exclude(sku="")
                .count()
            )
            if at_risk:
                self.stdout.write(self.style.WARNING(
                    f"--overwrite will change {at_risk} SKU(s) that already "
                    f"exist.\n"
                    f"Any label already printed and stuck to a scarf keeps the "
                    f"OLD code, and so does Square until you re-sync. Those "
                    f"items will scan to nothing."
                ))
                if options["interactive"]:
                    if input("Type 'yes' to continue: ") != "yes":
                        self.stdout.write("Aborted. Nothing changed.")
                        return

        existing_skus = set(
            FinishedProduct.objects.exclude(sku="").values_list("sku", flat=True)
        )

        qs = FinishedProduct.objects.filter(is_active=True).select_related(
            "raw_product", "recipe"
        )
        if not overwrite:
            qs = qs.filter(sku="")

        generated = 0
        for fp in qs:
            sku = _make_sku(fp, existing_skus)
            fp.sku = sku
            fp.save(update_fields=["sku"])
            existing_skus.add(sku)
            self.stdout.write(f"  {fp.name} → {sku}")
            generated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Generated {generated} SKU(s)."
        ))
