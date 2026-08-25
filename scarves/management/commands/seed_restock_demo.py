"""Put the restock board into a known state, so dev shows every case at once.

The board's interesting states are all *relative to time and to the last
walk* — a peg is quiet, or two down, or has been bare for three hours — and
none of them appear on a freshly seeded board. Poking at it by hand then means
inventing sales, backdating a pass, and losing track of which numbers were
real, which is exactly how a demo board starts lying to whoever is reading it.

So this builds the whole thing from nothing, every time. **It always clears
its own previous output first**, and it writes *absolute* stock rather than
applying deltas, so running it twice lands on the same board rather than
drifting further from one. Re-run it whenever the board stops making sense.

    docker compose run --rm --no-deps web python manage.py seed_restock_demo

Everything it writes is tagged `DEMO_NOTE`. On each run it clears its own
rows *and* any sale for a product on this board dated after the baseline —
otherwise a line typed in by hand while poking about lands inside the scripted
window and quietly contradicts it. Older history, other products and the close
are untouched.

**Dev only, with no way round it.** It fabricates sales that never happened
and deletes real ones, so it refuses to run unless `DEBUG` is on — and there
is deliberately no `--force`. An override is the only thing that could ever
point this at the shop's data, and the flag would be added by whoever was in
the biggest hurry. `DEBUG` is off on Railway, so this cannot run there.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from scarves.models import (
    DisplayFixture,
    FinishedProduct,
    InventoryLog,
    RestockCheck,
    RestockPass,
)

#: Stamped on every row this writes, and the handle it deletes them by. A
#: marker in the notes rather than a flag on the model, because the models
#: have no business knowing that a demo mode exists.
DEMO_NOTE = "[seed_restock_demo] fabricated for development"

#: One entry per peg, cycled across the board. Each is
#: (label, stock now, how many sold since the last walk, how long ago).
#: Between them they produce every state a tile can be in, which is the point:
#: a board where only the happy case appears is one nobody can check the
#: interesting cases against.
SCENARIOS = [
    ("quiet",        8, 0, None),                    # checked, nothing sold
    ("topup",        7, 1, timedelta(minutes=25)),   # +1, blue, go refill
    ("topup-2",      6, 2, timedelta(minutes=40)),   # +2, blue
    ("bare-recent",  6, 2, timedelta(minutes=18)),   # empty 18 minutes
    ("bare-hours",   9, 2, timedelta(hours=3)),      # empty 3 hours
    ("quiet-2",     12, 0, None),
    ("cannot-fill",  1, 0, None),                    # amber: nothing to fill it
    ("cannot-fill-sold", 1, 1, timedelta(hours=1)),  # amber with a sale
    ("empty-shelf",  0, 0, None),                    # amber, nothing at all
]


class Command(BaseCommand):
    help = "Build a demo restock board covering every tile state (dev only)."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", default="Yarn Pegboard")
        parser.add_argument(
            "--walked-ago-hours",
            type=float,
            default=4,
            help="How long ago the baseline full check happened.",
        )
        parser.add_argument(
            "--leave-unwalked",
            type=int,
            default=2,
            help=(
                "How many pegs to leave out of the baseline pass. A peg with "
                "no baseline predicts nothing, which has to look different "
                "from a peg that was checked and had nothing to report."
            ),
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        # No escape hatch, on purpose. This writes sales that never happened
        # and deletes real ones, and a --force flag is exactly what somebody
        # in a hurry reaches for. DEBUG is off on Railway, so this is not
        # runnable against the shop's data by any route.
        if not settings.DEBUG:
            raise CommandError(
                "seed_restock_demo fabricates sales and deletes real ones. "
                "It only runs with DEBUG on, and there is no override."
            )

        try:
            fixture = DisplayFixture.objects.get(name=options["fixture"])
        except DisplayFixture.DoesNotExist:
            raise CommandError(
                f"No fixture named {options['fixture']!r}. "
                f"Run seed_display_board first."
            )

        pegs = [
            position
            for grid_row in fixture.grid()
            for position in grid_row
            if position is not None
            and position.is_home
            and position.finished_product_id
        ]
        if not pegs:
            raise CommandError(
                "That board has no colorways on it. Hang some in the admin "
                "(Display fixtures → the board → positions) first."
            )

        walked_at = timezone.now() - timedelta(hours=options["walked_ago_hours"])
        unwalked = options["leave_unwalked"]
        plan = [
            (peg, SCENARIOS[i % len(SCENARIOS)], i >= len(pegs) - unwalked)
            for i, peg in enumerate(pegs)
        ]

        self.stdout.write(f"{fixture.name}: {len(pegs)} pegs")
        self.stdout.write(
            f"  baseline full check {options['walked_ago_hours']}h ago, "
            f"{unwalked} peg(s) left unwalked"
        )
        counts = {}
        for _peg, (label, *_rest), skip in plan:
            key = "unwalked" if skip else label
            counts[key] = counts.get(key, 0) + 1
        for label in sorted(counts):
            self.stdout.write(f"  {counts[label]:>3} × {label}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return

        with transaction.atomic():
            self._clear(fixture, pegs, walked_at)
            walk = self._baseline(fixture, walked_at)
            for peg, scenario, skip in plan:
                self._apply(peg, scenario, walk, walked_at, skip)
            # Recomputed after the checks exist, so a board whose pegs were
            # all covered still reads as the full check it was.
            if not any(skip for _p, _s, skip in plan):
                walk.is_full = True
                walk.save(update_fields=["is_full"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Open /scarves/secret/restock/{fixture.pk}/ — you should "
                f"see quiet pegs, blue +N pegs, red 'empty' pegs and amber "
                f"unfillable ones."
            )
        )

    def _clear(self, fixture, pegs, walked_at):
        """Clear the window this command is about to script.

        Deleting only its own marked rows is not enough, and finding that out
        is what this comment is for: a sale typed in by hand while poking at
        the board survives the purge, lands inside the scripted window, and
        turns a peg the script called quiet into one that reads as bare for
        half an hour. The board then shows a state the script did not intend
        and nobody can tell which numbers were deliberate — which is the exact
        failure a seed command exists to prevent.

        So it owns the window outright: every sale for the products on this
        board, from the baseline onwards, goes. Older history is untouched,
        and so is every product that isn't on the board.

        Blunt, and only defensible because this is dev-only and refuses to run
        with `DEBUG` off.
        """
        RestockCheck.objects.filter(restock_pass__fixture=fixture).delete()
        RestockPass.objects.filter(fixture=fixture).delete()
        InventoryLog.objects.filter(notes=DEMO_NOTE).delete()
        InventoryLog.objects.filter(
            finished_product_id__in={peg.finished_product_id for peg in pegs},
            log_type=InventoryLog.SALE,
            created_at__gte=walked_at,
        ).delete()

    def _baseline(self, fixture, walked_at):
        """The walk everything else is measured from, backdated into place."""
        walk = RestockPass.objects.create(fixture=fixture)
        RestockPass.objects.filter(pk=walk.pk).update(created_at=walked_at)
        walk.refresh_from_db()
        return walk

    def _apply(self, peg, scenario, walk, walked_at, skip):
        """One peg: its stock now, and the story that got it there.

        `RestockCheck` is written directly rather than through
        `restock.record`, because `record` derives `expected` from stock as it
        stands *now* — which is after the sales. What has to be frozen here is
        what went onto the peg *then*, and only a seed script is in a position
        to know both.

        Stock is set absolutely rather than decremented, which is what makes
        the command re-runnable: the fabricated sales are narrative, and the
        number is the number.
        """
        _label, stock, sold, ago = scenario
        product = peg.finished_product
        capacity = peg.fixture.capacity_per_position

        FinishedProduct.objects.filter(pk=product.pk).update(number_on_hand=stock)
        product.refresh_from_db()

        for i in range(sold):
            log = InventoryLog.objects.create(
                finished_product=product,
                raw_product=product.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
                quantity=-1,
                notes=DEMO_NOTE,
            )
            # Spread across the window so the last one dates the peg going
            # bare — `_drained_at` reads the moment the running total reached
            # what was put out, so the times have to be real, not all equal.
            when = timezone.now() - (ago or timedelta(minutes=5))
            when += timedelta(seconds=i * 30)
            InventoryLog.objects.filter(pk=log.pk).update(created_at=when)

        if skip:
            return

        RestockCheck.objects.create(
            restock_pass=walk,
            position=peg,
            finished_product=product,
            row=peg.row,
            column=peg.column,
            result=RestockCheck.AS_PREDICTED,
            # What was on the peg at the time: everything that has sold since,
            # plus whatever is still there, capped at what the peg holds.
            expected=min(stock + sold, capacity),
        )
