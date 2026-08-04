import json
from django.core.management.base import BaseCommand, CommandError
from scarves.models import FinishedProduct, Recipe, RawProduct


class Command(BaseCommand):
    help = "Compare a fixture file against the live DB and report differences."

    def add_arguments(self, parser):
        parser.add_argument("fixture_file", help="Path to the fixture JSON file to compare.")
        parser.add_argument(
            "--create-missing",
            action="store_true",
            help="Create FinishedProducts that exist in the fixture but not in the DB.",
        )

    def handle(self, *args, **options):
        try:
            with open(options["fixture_file"]) as f:
                data = json.load(f)
        except FileNotFoundError:
            raise CommandError(f"File not found: {options['fixture_file']}")

        # Build fixture lookup: recipe pk -> name
        fixture_recipes = {
            o["pk"]: o["fields"]["name"]
            for o in data if o["model"] == "scarves.recipe"
        }

        # Fixture finished products keyed by name
        fixture_fps = {}
        for o in data:
            if o["model"] != "scarves.finishedproduct":
                continue
            name = o["fields"]["name"]
            recipe_name = fixture_recipes.get(o["fields"]["recipe"], "")
            fixture_fps[name] = {
                "name": name,
                "recipe_name": recipe_name,
                "price": o["fields"]["price"],
                "par": o["fields"]["par"],
                "is_active": o["fields"]["is_active"],
                "sku": o["fields"].get("sku", ""),
            }

        # Live DB finished products keyed by name
        live_fps = {
            fp.name: fp
            for fp in FinishedProduct.objects.select_related("raw_product", "recipe").all()
        }

        fixture_names = set(fixture_fps.keys())
        live_names = set(live_fps.keys())

        only_in_fixture = sorted(fixture_names - live_names)
        only_in_live = sorted(live_names - fixture_names)
        in_both = fixture_names & live_names

        # Check for field differences in shared records
        diffs = []
        for name in sorted(in_both):
            fix = fixture_fps[name]
            live = live_fps[name]
            changes = []
            if str(fix["price"]) != str(live.price):
                changes.append(f"price: fixture={fix['price']} live={live.price}")
            if fix["par"] != live.par:
                changes.append(f"par: fixture={fix['par']} live={live.par}")
            if fix["is_active"] != live.is_active:
                changes.append(f"is_active: fixture={fix['is_active']} live={live.is_active}")
            if changes:
                diffs.append((name, changes))

        # Report
        self.stdout.write(f"\n=== ONLY IN FIXTURE (missing from DB) — {len(only_in_fixture)} ===")
        for name in only_in_fixture:
            fp = fixture_fps[name]
            self.stdout.write(f"  {name!r}  recipe={fp['recipe_name']!r}  price=${fp['price']}  active={fp['is_active']}")

        self.stdout.write(f"\n=== ONLY IN DB (not in fixture) — {len(only_in_live)} ===")
        for name in only_in_live:
            fp = live_fps[name]
            self.stdout.write(f"  {name!r}  recipe={fp.recipe.name!r}  price=${fp.price}  active={fp.is_active}")

        self.stdout.write(f"\n=== FIELD DIFFERENCES (in both, values differ) — {len(diffs)} ===")
        for name, changes in diffs:
            self.stdout.write(f"  {name!r}")
            for c in changes:
                self.stdout.write(f"    {c}")

        self.stdout.write(f"\nSummary: {len(fixture_fps)} in fixture, {len(live_fps)} in DB, "
                          f"{len(only_in_fixture)} missing from DB, {len(only_in_live)} extra in DB, "
                          f"{len(diffs)} with field diffs.")

        if options["create_missing"] and only_in_fixture:
            self._create_missing(only_in_fixture, fixture_fps)

    def _create_missing(self, names, fixture_fps):
        self.stdout.write("\n=== CREATING MISSING FINISHED PRODUCTS ===")
        created = skipped = 0

        # Cache live recipes and raw products by name
        recipes = {r.name: r for r in Recipe.objects.filter(is_active=True)}
        raw_products = {rp.name: rp for rp in RawProduct.objects.filter(is_active=True)}

        for name in names:
            fix = fixture_fps[name]
            recipe_name = fix["recipe_name"]

            # Derive raw product name: everything before " - {recipe_name}"
            suffix = f" - {recipe_name}"
            if name.endswith(suffix):
                rp_name = name[: -len(suffix)]
            else:
                self.stdout.write(self.style.WARNING(
                    f"  SKIP {name!r}: can't parse raw product name"
                ))
                skipped += 1
                continue

            recipe = recipes.get(recipe_name)
            raw_product = raw_products.get(rp_name)

            if not recipe:
                self.stdout.write(self.style.WARNING(
                    f"  SKIP {name!r}: recipe {recipe_name!r} not found in DB"
                ))
                skipped += 1
                continue
            if not raw_product:
                self.stdout.write(self.style.WARNING(
                    f"  SKIP {name!r}: raw product {rp_name!r} not found in DB"
                ))
                skipped += 1
                continue

            fp, was_created = FinishedProduct.objects.get_or_create(
                name=name,
                defaults={
                    "raw_product": raw_product,
                    "recipe": recipe,
                    "price": fix["price"],
                    "par": fix["par"],
                    "is_active": fix["is_active"],
                },
            )
            if was_created:
                self.stdout.write(self.style.SUCCESS(f"  CREATED {name!r}"))
                created += 1
            else:
                self.stdout.write(f"  EXISTS  {name!r} (already created)")
                skipped += 1

        self.stdout.write(self.style.SUCCESS(f"Created {created}, skipped {skipped}."))
