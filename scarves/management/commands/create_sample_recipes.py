from django.core.management.base import BaseCommand
from django.db import transaction

from scarves.models import Dye, Recipe, RecipeDye


class Command(BaseCommand):
    help = "Create sample recipes using existing dyes in the database."

    def handle(self, *args, **options):
        dyes = list(Dye.objects.all().order_by("id"))

        if len(dyes) < 5:
            self.stderr.write(
                self.style.ERROR(
                    "Not enough dyes to create sample recipes. "
                    "Load your dye fixtures / run your dye import first."
                )
            )
            return

        recipe_specs = [
            {
                "name": "Sunrise Over Silk",
                "description": "Warm yellows and soft oranges for a sunrise feel.",
                "num_dyes": 3,
            },
            {
                "name": "Moonlit Ocean",
                "description": "Deep blues with a hint of teal.",
                "num_dyes": 3,
            },
            {
                "name": "Forest Walk",
                "description": "Greens and browns inspired by a mossy forest.",
                "num_dyes": 4,
            },
            {
                "name": "Berry Patch",
                "description": "Pinks, reds, and purples like a basket of berries.",
                "num_dyes": 4,
            },
            {
                "name": "Stormy Sky",
                "description": "Blues and grays with a moody vibe.",
                "num_dyes": 3,
            },
            {
                "name": "Desert Sunset",
                "description": "Golds and rusts inspired by a desert horizon.",
                "num_dyes": 4,
            },
            {
                "name": "Meadow Flowers",
                "description": "Bright, cheerful mix of floral tones.",
                "num_dyes": 5,
            },
            {
                "name": "Vintage Sepia",
                "description": "Muted browns and olives, old-photo feel.",
                "num_dyes": 2,
            },
            {
                "name": "Gothic Romance",
                "description": "Dark reds, purples, and black for a dramatic look.",
                "num_dyes": 4,
            },
            {
                "name": "Seafoam Breeze",
                "description": "Aquas and light greens, airy and fresh.",
                "num_dyes": 3,
            },
            {
                "name": "Frosted Lilac",
                "description": "Cool purples with icy undertones.",
                "num_dyes": 3,
            },
            {
                "name": "Ember Glow",
                "description": "Firey reds and oranges like glowing embers.",
                "num_dyes": 3,
            },
            {
                "name": "Coffee & Cream",
                "description": "Browns and neutrals, cozy café palette.",
                "num_dyes": 3,
            },
            {
                "name": "Deep Space",
                "description": "Indigo, navy, and black for a cosmic look.",
                "num_dyes": 3,
            },
            {
                "name": "Rainy Sidewalk",
                "description": "Grays and blues with a touch of muted purple.",
                "num_dyes": 4,
            },
            {
                "name": "Rose Garden",
                "description": "Soft pinks and deeper rose tones.",
                "num_dyes": 3,
            },
            {
                "name": "Citrus Grove",
                "description": "Yellows, oranges, and a bit of green.",
                "num_dyes": 4,
            },
            {
                "name": "Lavender Fields",
                "description": "Lavenders, lilacs, and soft greens.",
                "num_dyes": 3,
            },
            {
                "name": "Aurora Borealis",
                "description": "Electric blues, greens, and violets.",
                "num_dyes": 5,
            },
            {
                "name": "Midnight Plum",
                "description": "Dark purples and wine tones.",
                "num_dyes": 3,
            },
        ]

        with transaction.atomic():
            for idx, spec in enumerate(recipe_specs):
                name = spec["name"]
                description = spec["description"]
                num_dyes = spec["num_dyes"]

                # Create or update the Recipe
                recipe, created = Recipe.objects.get_or_create(
                    name=name,
                    defaults={
                        "description": description,
                        "is_active": True,
                    },
                )

                if not created:
                    # If it already existed, update description and clear old dye links
                    recipe.description = description
                    recipe.is_active = True
                    recipe.save()
                    recipe.recipe_dyes.all().delete()

                # Pick some dyes in a deterministic but varied way
                chosen_dyes = []
                for j in range(num_dyes):
                    # a pseudo-random-ish selection pattern that wraps around the dye list
                    index = (idx * 3 + j * 7) % len(dyes)
                    chosen_dye = dyes[index]
                    if chosen_dye not in chosen_dyes:
                        chosen_dyes.append(chosen_dye)

                # Make sure we don't exceed 5 dyes (model constraint)
                chosen_dyes = chosen_dyes[:5]

                # Create RecipeDye entries
                for order, dye in enumerate(chosen_dyes, start=1):
                    RecipeDye.objects.create(
                        recipe=recipe,
                        dye=dye,
                        order=order,
                        ratio=None,  # you can fill this in later if you want
                    )

                self.stdout.write(
                    self.style.SUCCESS(
                        f"{'Created' if created else 'Updated'} recipe '{recipe.name}' "
                        f"with {len(chosen_dyes)} dyes."
                    )
                )

        self.stdout.write(self.style.SUCCESS("Sample recipes created/updated successfully."))
