"""Track the things Square already sells that this app has never heard of.

The bits and bobs — yarn bowls, spindles, needle cases — are bought and sold
as they arrive, so no dye bath ever created them and nothing ever made the
row. Every sale of one lands in `private/unidentified-sales/`.

**It never decides what is a product.** Run over the whole unknown set, a
command like this would cheerfully create `Women's Haircut`, `Shipping` and
`$2 Refund` as things to keep stock of — all three are real items on this
till. So the population comes from a Square *category* somebody curated:
assign the notions to `Notions` in the Square dashboard, and this creates
exactly those. The app follows a decision rather than making one.

`--list` is the other half, and the one to run first: it prints every item the
app doesn't know, grouped by Square's category, which is how you work out what
still needs assigning.
"""

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scarves import passthroughs
from scarves.models import CatalogGroup, RawProductCategory


class Command(BaseCommand):
    help = (
        "Create passthrough products for the Square items in a given Square "
        "category that this app doesn't track yet."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--category",
            help=(
                "Square category name to import, e.g. 'Notions'. Its items "
                "become passthrough products."
            ),
        )
        parser.add_argument(
            "--into",
            help=(
                "App category (RawProductCategory) the new rows go in. "
                "Defaults to the one whose name matches --category."
            ),
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help=(
                "Print every Square item this app doesn't know, grouped by "
                "Square category, and stop. Run this first."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without creating it.",
        )

    # -- Square reading -------------------------------------------------

    def _client(self):
        from square.client import Client

        return Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )

    def _search(self, client, object_type):
        objects, cursor = [], None
        while True:
            body = {"object_types": [object_type], "limit": 100}
            if cursor:
                body["cursor"] = cursor
            result = client.catalog.search_catalog_objects(body=body)
            if result.is_error():
                # Never read an empty answer as "nothing to do" — that looks
                # identical to a catalogue that agrees, and only one of them
                # is safe to act on. Same rule the sync's version read follows.
                raise CommandError(f"Could not read the catalogue: {result.errors}")
            objects.extend(result.body.get("objects", []) or [])
            cursor = result.body.get("cursor")
            if not cursor:
                return objects

    @staticmethod
    def _category_name(item_data, categories):
        """Square names a category in three shapes; read all of them.

        `category_id` is the old one and is null on anything written
        recently, `reporting_category` is what the dashboard's own reports
        use, and `categories` is the current list. Reading only the first is
        how a whole product line hides in "(uncategorised)".
        """
        if item_data.get("category_id"):
            return categories.get(item_data["category_id"], item_data["category_id"])
        reporting = item_data.get("reporting_category") or {}
        if reporting.get("id"):
            return categories.get(reporting["id"], reporting["id"])
        for entry in item_data.get("categories") or []:
            if entry.get("id"):
                return categories.get(entry["id"], entry["id"])
        return ""

    # -- the command ----------------------------------------------------

    def handle(self, *args, **options):
        client = self._client()
        categories = {
            obj["id"]: (obj.get("category_data") or {}).get("name") or ""
            for obj in self._search(client, "CATEGORY")
        }
        items = self._search(client, "ITEM")

        known_items = passthroughs.tracked_item_ids()
        known_variations = passthroughs.tracked_variation_ids()

        unknown = []
        for obj in items:
            if obj["id"] in known_items:
                continue
            data = obj.get("item_data") or {}
            unknown.append((self._category_name(data, categories), data, obj["id"]))

        if options["list"]:
            return self._list(unknown)

        wanted = options["category"]
        if not wanted:
            raise CommandError(
                "Give --category (the Square category to import) or --list "
                "to see what is untracked."
            )

        target = self._app_category(options["into"] or wanted)
        batch = [
            (data, item_id) for name, data, item_id in unknown
            if name.casefold() == wanted.casefold()
        ]
        if not batch:
            seen = sorted({name for name, _, _ in unknown if name})
            raise CommandError(
                f"No untracked Square items in a category called {wanted!r}. "
                f"Either everything in it is tracked already, or nothing has "
                f"been assigned to it yet. Untracked items currently sit in: "
                + (", ".join(seen) or "no category at all")
                + ". Run --list to see them."
            )

        made, skipped, failed = [], [], []
        for data, item_id in sorted(batch, key=lambda b: b[0].get("name") or ""):
            name = (data.get("name") or "").strip()
            variations = data.get("variations") or []
            if not variations:
                failed.append((name, "Square has no variation under it"))
                continue

            # One variation is one item sold one way: the raw product is its
            # own Square item. Several means the item is a heading and the
            # variations are the things — which is what CatalogGroup is for,
            # and it must be one raw product per variation, because two
            # passthrough rows on one raw product would mirror the same pile.
            group = None
            if len(variations) > 1 and not options["dry_run"]:
                group, _ = CatalogGroup.objects.get_or_create(
                    name=name,
                    defaults={"square_item_id": item_id, "category": target},
                )
                if not group.square_item_id:
                    group.square_item_id = item_id
                    group.save(update_fields=["square_item_id"])

            for variation in variations:
                vdata = variation.get("item_variation_data") or {}
                vid = variation["id"]
                vname = (vdata.get("name") or "").strip()
                label = (
                    f"{name} — {vname}" if len(variations) > 1 and vname else name
                )
                if vid in known_variations:
                    skipped.append(label)
                    continue

                amount = (vdata.get("price_money") or {}).get("amount")
                if not amount:
                    # Variable pricing, or a price of nothing. Named, never
                    # imported at zero: a zero price is valid, syncs, and
                    # rings up free at the till with a queue behind it.
                    failed.append((label, "no fixed price in Square"))
                    continue
                price = (Decimal(amount) / Decimal(100)).quantize(Decimal("0.01"))

                if options["dry_run"]:
                    made.append((label, price, "(not saved)"))
                    continue

                product, created = passthroughs.create(
                    name=label,
                    category=target,
                    price=price,
                    variation_id=vid,
                    item_id=item_id,
                    group=group,
                )
                made.append((label, price, product.sku if created else "(existing)"))
                known_variations.add(vid)

        verb = "Would create" if options["dry_run"] else "Created"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(made)} passthrough product(s):"))
        for label, price, sku in made:
            self.stdout.write(f"  {label:38} ${price:>8}  {sku}")
        if skipped:
            self.stdout.write(f"\n{len(skipped)} already tracked: " + ", ".join(skipped[:8]))
        if failed:
            # Named rather than counted. "3 skipped" sends nobody anywhere.
            self.stdout.write(self.style.WARNING(f"\n{len(failed)} left alone:"))
            for label, why in failed:
                self.stdout.write(f"  {label:38} {why}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN — nothing was written."))

    def _app_category(self, name):
        category = RawProductCategory.objects.filter(name__iexact=name).first()
        if category is None:
            known = ", ".join(RawProductCategory.objects.values_list("name", flat=True))
            raise CommandError(
                f"No app category called {name!r}. Make it first (with its "
                f"Square category id, so the sync files new items under it). "
                f"Known: {known}."
            )
        return category

    def _list(self, unknown):
        by_category = {}
        for name, data, item_id in unknown:
            by_category.setdefault(name or "(no category)", []).append((data, item_id))

        self.stdout.write(
            f"{sum(len(v) for v in by_category.values())} Square item(s) this "
            f"app does not track:\n"
        )
        for category in sorted(by_category):
            entries = by_category[category]
            self.stdout.write(self.style.SUCCESS(f"{category} ({len(entries)})"))
            for data, item_id in sorted(entries, key=lambda e: e[0].get("name") or ""):
                variations = data.get("variations") or []
                prices = [
                    (v.get("item_variation_data") or {}).get("price_money", {}).get("amount")
                    for v in variations
                ]
                money = "/".join(
                    f"${p / 100:.2f}" for p in prices[:3] if p
                ) or "no price"
                self.stdout.write(
                    f"   {(data.get('name') or ''):30} "
                    f"{len(variations)} var{'s' if len(variations) != 1 else ''}  "
                    f"{money:24} {item_id}"
                )
            self.stdout.write("")
