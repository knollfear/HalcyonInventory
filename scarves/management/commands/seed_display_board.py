"""Create a display fixture and every peg on it, ready to be assigned.

The board is a rectangle and the pegs are all the same, so there is nothing to
type in one at a time — but the positions have to *exist* before anything can
be hung on them, and creating forty-two rows by hand in an admin inline is how
a board ends up half-mapped.

Idempotent on purpose. Re-running fills in pegs that are missing and leaves
every assignment exactly where it was: the board gets extended (a row added
along the bottom, a hook moved) far more often than it gets built, and a seed
command that clobbered assignments would be one nobody dared run twice.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves.models import DisplayFixture, DisplayPosition, RawProduct


class Command(BaseCommand):
    help = "Create a display fixture and its positions."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Yarn Pegboard")
        parser.add_argument("--rows", type=int, default=7)
        parser.add_argument("--columns", type=int, default=6)
        parser.add_argument(
            "--capacity",
            type=int,
            default=2,
            help=(
                "How many units one peg holds. Two-skein hooks today. This is "
                "capacity and never a production target — see CLAUDE.md."
            ),
        )
        parser.add_argument(
            "--reserve-top-middle",
            type=int,
            default=2,
            help=(
                "How many middle columns of the top row are signage rather "
                "than pegs. The price tag lives there. 0 for none."
            ),
        )
        parser.add_argument("--reserve-label", default="Price tag")
        parser.add_argument(
            "--raw-product",
            default="",
            help=(
                "The blank this board is for, by name. In the shop a board "
                "tends to carry one product in all its colorways, and naming "
                "it is what lets the board report which of that blank's "
                "colorways aren't up anywhere. It never restricts what can be "
                "hung. Leave blank for a mixed board."
            ),
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        rows = options["rows"]
        columns = options["columns"]
        if rows < 1 or columns < 1:
            raise CommandError("A board needs at least one row and one column.")

        reserved = self._reserved_columns(columns, options["reserve_top_middle"])

        raw = None
        if options["raw_product"]:
            raw = RawProduct.objects.filter(name=options["raw_product"]).first()
            if raw is None:
                # Named and refused rather than quietly creating a mixed
                # board: a typo here produces a board that silently never
                # reports its gap, which looks exactly like a board with no
                # gap. Same reason `import_dyes --dry-run` says when --brand
                # would create a new brand.
                raise CommandError(
                    f"No raw product named {options['raw_product']!r}. "
                    f"Known: "
                    + ", ".join(
                        RawProduct.objects.order_by("name").values_list(
                            "name", flat=True
                        )[:12]
                    )
                )

        fixture = DisplayFixture.objects.filter(name=options["name"]).first()
        existing = (
            set(fixture.positions.values_list("row", "column")) if fixture else set()
        )

        to_create = [
            (r, c)
            for r in range(1, rows + 1)
            for c in range(1, columns + 1)
            if (r, c) not in existing
        ]

        self.stdout.write(
            f"{options['name']}: {rows} × {columns}, "
            f"{options['capacity']} per peg"
        )
        if fixture is None:
            self.stdout.write("  fixture: create")
        else:
            self.stdout.write(
                f"  fixture: exists ({len(existing)} positions already)"
            )
        self.stdout.write(f"  positions to create: {len(to_create)}")
        if raw is not None:
            self.stdout.write(f"  for blank: {raw.name}")
        if reserved:
            self.stdout.write(
                f"  reserved on row 1: columns "
                f"{', '.join(str(c) for c in sorted(reserved))} "
                f"({options['reserve_label']})"
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return

        with transaction.atomic():
            if fixture is None:
                fixture = DisplayFixture.objects.create(
                    name=options["name"],
                    rows=rows,
                    columns=columns,
                    capacity_per_position=options["capacity"],
                    raw_product=raw,
                )
            else:
                # Growing a board is the normal reason to re-run. Shrinking it
                # is not done here: pegs would have to be deleted and one of
                # them might have a colorway on it.
                fixture.rows = max(fixture.rows, rows)
                fixture.columns = max(fixture.columns, columns)
                if raw is not None:
                    fixture.raw_product = raw
                fixture.save(
                    update_fields=["rows", "columns", "raw_product"]
                )

            # The pegs themselves arrive with the fixture now — a `post_save`
            # signal creates them, so a board built in the admin works too.
            # What is left for this command is the reserved cells, which are
            # applied to whatever is there rather than only set at creation:
            # after the signal there is nothing left to create, and a version
            # that only handled new rows would silently stop labelling the
            # price tag.
            fixture.ensure_positions()
            if reserved:
                DisplayPosition.objects.filter(
                    fixture=fixture, row=1, column__in=reserved
                ).update(reserved_label=options["reserve_label"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {len(to_create)} position(s) created. "
                f"Hang colorways on them in the admin, or walk the board at "
                f"/scarves/secret/restock/."
            )
        )

    @staticmethod
    def _reserved_columns(columns, how_many):
        """The middle `how_many` columns, centred as well as the width allows.

        An even count on an odd board can't be centred exactly, so it leans
        left rather than raising — the wall is the authority on this and the
        admin can move it in one edit.
        """
        if how_many <= 0:
            return set()
        how_many = min(how_many, columns)
        start = (columns - how_many) // 2 + 1
        return set(range(start, start + how_many))
