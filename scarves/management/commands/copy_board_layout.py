"""Lay one board out the same way as another, colorway for colorway.

The four yarn boards are the same pattern repeated per base — Heavenly's peg
r3c4 and Homespun's r3c4 carry the same colorway — so the second, third and
fourth boards are the first one retyped. That is the whole of what this saves,
and it is worth saving forty times over.

**Only where the pattern really is shared.** The silk racks are arranged by
what looks right next to what, and copying onto one would produce a
plausible-looking layout that is wrong everywhere at once — harder to spot and
undo than an empty board. If the source and target don't share an order, don't
use this.

    python manage.py copy_board_layout --from "Heavenly" --to "Homespun" --dry-run

Both boards must name the blank they carry: the mapping is "same peg, same
colorway, other blank", and without a target blank there is nothing to map
onto. Nothing is created — a colorway the target blank doesn't have yet is
**named and skipped**, because inventing the product would mean inventing a
price, and a silently created row at a guessed price is the expensive kind of
helpful.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves.models import DisplayFixture, FinishedProduct


class Command(BaseCommand):
    help = "Copy one board's colorway layout onto another blank's board."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="source", required=True)
        parser.add_argument("--to", dest="target", required=True)
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help=(
                "Replace colorways already on the target's pegs. Off by "
                "default: a half-laid-out board is usually somebody's work in "
                "progress, and quietly overwriting it is the one mistake here "
                "that can't be undone from the page."
            ),
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        source = self._board(options["source"])
        target = self._board(options["target"])
        if source.pk == target.pk:
            raise CommandError("Source and target are the same board.")
        if target.raw_product_id is None:
            raise CommandError(
                f"{target.name!r} doesn't say which blank it carries, so "
                f"there is nothing to map the colorways onto. Set it on the "
                f"board's editor page first."
            )

        # One query for the target blank's colorways rather than one per peg.
        available = {
            product.recipe_id: product
            for product in FinishedProduct.objects.filter(
                is_active=True,
                raw_product_id=target.raw_product_id,
                recipe__isnull=False,
            ).select_related("recipe")
        }
        target_pegs = {
            (p.row, p.column): p for p in target.positions.select_related("fixture")
        }

        copied, occupied, off_grid = [], [], 0
        missing = {}
        for position in source.positions.select_related(
            "finished_product__recipe"
        ).filter(finished_product__isnull=False):
            if not position.is_home:
                continue
            peg = target_pegs.get((position.row, position.column))
            if peg is None or not peg.is_home:
                off_grid += 1
                continue
            if peg.finished_product_id and not options["overwrite"]:
                occupied.append(peg)
                continue

            recipe = position.finished_product.recipe
            product = available.get(recipe.id)
            if product is None:
                missing.setdefault(recipe.name, 0)
                missing[recipe.name] += 1
                continue
            copied.append((peg, product))

        self.stdout.write(
            f"{source.name} → {target.name} ({target.raw_product.name})"
        )
        self.stdout.write(f"  pegs to set : {len(copied)}")
        if occupied:
            self.stdout.write(
                f"  already used: {len(occupied)} (left alone; --overwrite to replace)"
            )
        if off_grid:
            self.stdout.write(f"  off the target's grid: {off_grid}")
        if missing:
            # Named, not counted. "Six missing" is a chore; six colorways with
            # names is an afternoon — the same framing the production sheet
            # uses for recipes with no dyes on file.
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(missing)} colorway(s) {target.raw_product.name} "
                    f"doesn't have yet, so those pegs stay empty:"
                )
            )
            for name in sorted(missing):
                self.stdout.write(f"    - {name}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return

        with transaction.atomic():
            for peg, product in copied:
                peg.finished_product = product
                peg.save(update_fields=["finished_product"])  # signal → slots

        self.stdout.write(
            self.style.SUCCESS(f"Done: {len(copied)} peg(s) laid out.")
        )

    @staticmethod
    def _board(name):
        board = DisplayFixture.objects.filter(name=name).first()
        if board is None:
            raise CommandError(
                f"No board named {name!r}. Known: "
                + ", ".join(
                    DisplayFixture.objects.order_by("name").values_list(
                        "name", flat=True
                    )
                )
            )
        return board
