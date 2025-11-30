from decimal import Decimal
import random

from django.core.management.base import BaseCommand
from django.db import transaction

from scarves.models import RawProduct, Recipe, FinishedProduct


class Command(BaseCommand):
    help = (
        "Create sample FinishedProduct entries using existing RawProducts and Recipes. "
        "Each RawProduct gets one FinishedProduct, assigned to a Recipe in round-robin."
    )

    def handle(self, *args, **options):
        recipes = list(Recipe.objects.filter(is_active=True).order_by("id"))
        raw_products = list(RawProduct.objects.filter(is_active=True).order_by("id"))

        if not recipes:
            self.stderr.write(
                self.style.ERROR(
                    "No recipes found. Run `create_sample_recipes` or create some recipes first."
                )
            )
            return

        if not raw_products:
            self.stderr.write(
                self.style.ERROR(
                    "No raw products found. Load your yarn/silk fixtures or create some in admin."
                )
            )
            return

        self.stdout.write(
            f"Found {len(raw_products)} raw products and {len(recipes)} recipes. "
            "Creating finished products..."
        )

        created_count = 0
        updated_count = 0

        with transaction.atomic():
            for idx, raw_product in enumerate(raw_products):
                for recipe in recipes:

                    name = f"{raw_product.name} - {recipe.name}"

                    # Determine base price for the finished product
                    if raw_product.suggested_price is not None:
                        price = raw_product.suggested_price
                    else:
                        # Simple fallback: 3x cost if suggested price missing
                        price = (raw_product.price or Decimal("0")) * Decimal("3.0")

                    # Simple rule for "fancy": higher suggested prices are fancy
                    is_fancy = False
                    if raw_product.suggested_price is not None:
                        is_fancy = raw_product.suggested_price >= Decimal("60.00")

                    # Random stock between 0 and 10
                    number_on_hand = random.randint(0, 10)

                    finished, created = FinishedProduct.objects.get_or_create(
                        raw_product=raw_product,
                        recipe=recipe,
                        name=name,
                        defaults={
                            "price": price,
                            "number_on_hand": number_on_hand,
                            "is_fancy": is_fancy,
                            "sku": "",
                            "description": (
                                f"Sample finished product using {raw_product.name} "
                                f"and recipe {recipe.name}."
                            ),
                            "is_active": True,
                        },
                    )

                    if not created:
                        # Update fields if it already existed
                        finished.price = price
                        finished.is_fancy = is_fancy
                        finished.number_on_hand = number_on_hand
                        finished.is_active = True
                        finished.save()
                        updated_count += 1
                    else:
                        created_count += 1

                    self.stdout.write(
                        f"{'Created' if created else 'Updated'} finished product: "
                        f"'{finished.name}' (Raw: {raw_product.name}, Recipe: {recipe.name}, "
                        f"On hand: {number_on_hand})"
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Finished products: {created_count} created, {updated_count} updated."
            )
        )