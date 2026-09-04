"""Diff this app's prices against Square's, and settle the disagreements.

A price can be changed in two places, and only one of them writes anything
down. `sync_to_square --update` sends `FinishedProduct.price` at every
variation Square already has, so a price typed into the dashboard during a
busy morning survives exactly until the next `--update` — at which point it
is replaced with no record that it ever differed, and the till starts
charging a number nobody chose.

Nothing here fixes that by remembering. The diff is a read, the resolution
is explicit, and `--update` grows its own guard (see `sync_to_square`) so a
divergence has to be settled rather than driven over.

    python manage.py compare_square_prices                 # read-only diff
    python manage.py compare_square_prices --pull          # Square wins
    python manage.py compare_square_prices --push --sku X  # we win, named
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scarves.models import FinishedProduct


class Command(BaseCommand):
    help = "Compare finished-product prices with Square's, and resolve them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pull",
            action="store_true",
            help=(
                "Take Square's price for every disagreement in scope. The "
                "usual direction: the dashboard is what gets edited when the "
                "stall is open and this app is not."
            ),
        )
        parser.add_argument(
            "--push",
            action="store_true",
            help=(
                "Send this app's price to Square for every disagreement in "
                "scope. Narrow it with --sku unless you are certain the whole "
                "list belongs to us."
            ),
        )
        parser.add_argument(
            "--sku",
            action="append",
            default=[],
            dest="skus",
            help="Limit a --pull or --push to this SKU. Repeatable.",
        )
        parser.add_argument(
            "--changed-since",
            metavar="YYYY-MM-DD",
            help=(
                "Limit a --pull or --push to variations Square itself last "
                "changed on or after this date. This is how 'the ones I "
                "edited last week' gets said without anyone having written "
                "the list down."
            ),
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Step through the disagreements one at a time and choose each.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Say what --pull or --push would do, and do none of it.",
        )

    # -- reading ----------------------------------------------------------

    def _preflight(self, client):
        if not settings.SQUARE_ACCESS_TOKEN:
            raise CommandError("SQUARE_ACCESS_TOKEN is not set.")
        result = client.locations.list_locations()
        if result.is_error():
            self._fail("Could not reach Square", result)

    def _fail(self, label, result):
        detail = "; ".join(
            f"{e.get('category')}: {e.get('detail')}"
            for e in (result.errors or [])
        ) or "no detail given"
        raise CommandError(f"{label}: {detail}")

    def _square_objects(self, client, variation_ids):
        """Every variation Square holds for the ids given, by id.

        A swallowed error here would come back as an empty answer, and an
        empty answer is indistinguishable from a catalogue that agrees with
        us on every price — which is the one conclusion this command must
        never reach by accident.
        """
        objects = {}
        for i in range(0, len(variation_ids), 100):
            result = client.catalog.batch_retrieve_catalog_objects(body={
                "object_ids": variation_ids[i:i + 100],
            })
            if result.is_error():
                self._fail("Could not read Square's catalogue", result)
            for obj in result.body.get("objects", []):
                objects[obj["id"]] = obj
        return objects

    @staticmethod
    def _square_price(obj):
        """Square's price as a Decimal, or None if it holds no fixed price.

        Variable pricing is not a price of zero — it is the till asking the
        cashier. Pulling it as a number would set a real product to whatever
        `None` coerced to, so it is reported instead.
        """
        data = obj.get("item_variation_data") or {}
        money = data.get("price_money") or {}
        amount = money.get("amount")
        if amount is None:
            return None
        return (Decimal(amount) / 100).quantize(Decimal("0.01"))

    def _rows(self, client):
        """`(agreed, differing, variable, stale)` for the whole live catalogue."""
        products = list(
            FinishedProduct.objects.filter(
                is_active=True,
                square_variation_id__gt="",
            ).select_related("raw_product", "recipe")
        )
        if not products:
            raise CommandError(
                "No active products carry a Square variation ID — there is "
                "nothing to compare. Run sync_to_square --relink first."
            )

        objects = self._square_objects(
            client, [fp.square_variation_id for fp in products]
        )

        agreed, differing, variable, stale = [], [], [], []
        for fp in products:
            obj = objects.get(fp.square_variation_id)
            if obj is None:
                stale.append(fp)
                continue
            square_price = self._square_price(obj)
            if square_price is None:
                variable.append(fp)
            elif square_price == fp.price:
                agreed.append(fp)
            else:
                differing.append((fp, obj, square_price))

        # Newest Square edit first: the run of rows changed on one afternoon
        # is the thing being looked for, and sorting by name would scatter it
        # through the list.
        differing.sort(
            key=lambda row: (row[1].get("updated_at") or "", row[0].sku or ""),
            reverse=True,
        )
        return agreed, differing, variable, stale

    # -- reporting --------------------------------------------------------

    def _report(self, agreed, differing, variable, stale):
        self.stdout.write("")
        self.stdout.write(
            f"{'SKU':<16} {'PRODUCT':<34} {'HERE':>9} {'SQUARE':>9}   SQUARE LAST CHANGED"
        )
        self.stdout.write("-" * 96)
        for fp, obj, square_price in differing:
            changed = (obj.get("updated_at") or "")[:10] or "unknown"
            self.stdout.write(
                f"{fp.sku or '(none)':<16} {str(fp)[:34]:<34} "
                f"{fp.price:>9.2f} {square_price:>9.2f}   {changed}"
            )
        if not differing:
            self.stdout.write("  (none — every price agrees)")

        self.stdout.write("")
        self.stdout.write(
            f"{len(agreed)} agree, {len(differing)} disagree, "
            f"{len(variable)} on variable pricing, {len(stale)} unknown to Square."
        )

        # Both of these are reasons a row can never be settled by this
        # command, so they are named rather than folded into a count nobody
        # can act on.
        if variable:
            self.stdout.write(self.style.WARNING(
                "Square holds no fixed price for: "
                + ", ".join(fp.sku or str(fp) for fp in variable[:8])
                + (" …" if len(variable) > 8 else "")
            ))
        if stale:
            self.stdout.write(self.style.WARNING(
                "Square does not recognise the variation ID on: "
                + ", ".join(fp.sku or str(fp) for fp in stale[:8])
                + (" …" if len(stale) > 8 else "")
                + " — run sync_to_square --relink."
            ))

    # -- scoping ----------------------------------------------------------

    def _in_scope(self, differing, skus, changed_since):
        """The subset a --pull or --push is allowed to touch.

        Unscoped means all of them, deliberately: the common case really is
        one direction for the whole list, and making that say `--all` would
        be ceremony. What is not allowed is a scope that matches nothing —
        that is a mistyped SKU, and applying it as "no rows" reads on screen
        exactly like a clean catalogue.
        """
        rows = differing
        if skus:
            wanted = {s.strip().upper() for s in skus}
            rows = [r for r in rows if (r[0].sku or "").upper() in wanted]
            unmatched = wanted - {(r[0].sku or "").upper() for r in differing}
            if unmatched:
                raise CommandError(
                    "No disagreement on file for: " + ", ".join(sorted(unmatched))
                )
        if changed_since:
            rows = [
                r for r in rows
                if (r[1].get("updated_at") or "")[:10] >= changed_since
            ]
            if not rows:
                raise CommandError(
                    f"Square changed none of the disagreeing prices on or "
                    f"after {changed_since}."
                )
        return rows

    def _choose(self, differing):
        """Walk the disagreements and ask about each one.

        Returns `(pull, push)`. Anything skipped stays disagreeing, which is
        the honest outcome for a row somebody could not decide about at the
        time — it will be on the next diff, and `--update` will refuse it.
        """
        pull, push = [], []
        total = len(differing)
        for i, row in enumerate(differing, start=1):
            fp, obj, square_price = row
            changed = (obj.get("updated_at") or "")[:10] or "unknown"
            self.stdout.write("")
            self.stdout.write(f"[{i}/{total}] {fp}  ({fp.sku or 'no SKU'})")
            self.stdout.write(f"       here   ${fp.price:.2f}")
            self.stdout.write(f"       Square ${square_price:.2f}   changed {changed}")
            answer = ""
            while answer not in {"s", "h", "k"}:
                answer = input("       [s]quare wins / [h]ere wins / s[k]ip: ").strip().lower()
            if answer == "s":
                pull.append(row)
            elif answer == "h":
                push.append(row)
        return pull, push

    # -- resolving --------------------------------------------------------

    def _pull(self, rows, dry_run):
        if not rows:
            return
        self.stdout.write("")
        for fp, _obj, square_price in rows:
            self.stdout.write(
                f"  {fp.sku or '(none)':<16} {fp.price:>8.2f} -> {square_price:>8.2f}"
            )
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would take Square's price for {len(rows)} product(s)."
            ))
            return
        for fp, _obj, square_price in rows:
            fp.price = square_price
            # save(), not a queryset update(): the model fills a blank SKU and
            # settles a passthrough's count on the way through, and an
            # update() would skip both.
            fp.save(update_fields=["price"])
        self.stdout.write(self.style.SUCCESS(
            f"Took Square's price for {len(rows)} product(s)."
        ))

    def _push(self, client, rows, dry_run):
        """Send our price for the rows named, and change nothing else.

        Same rule the ordering pass follows and for the same reason: this
        never *builds* a variation. It takes the object as Square returned
        it, replaces the amount, and sends that back — so a field this app
        does not model (an item option, a location override, a description
        typed in the dashboard) cannot be dropped by being absent from a
        payload we assembled ourselves.
        """
        if not rows:
            return
        objects = []
        for fp, obj, square_price in rows:
            payload = {k: v for k, v in obj.items() if k != "updated_at"}
            data = dict(payload.get("item_variation_data") or {})
            data["pricing_type"] = "FIXED_PRICING"
            data["price_money"] = {
                "amount": int(fp.price * 100),
                "currency": (data.get("price_money") or {}).get("currency", "USD"),
            }
            payload["item_variation_data"] = data
            objects.append(payload)
            self.stdout.write(
                f"  {fp.sku or '(none)':<16} {square_price:>8.2f} -> {fp.price:>8.2f}  (Square)"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would send {len(objects)} price(s) to Square."
            ))
            return

        sent = 0
        for i in range(0, len(objects), 100):
            chunk = objects[i:i + 100]
            result = client.catalog.batch_upsert_catalog_objects(body={
                "idempotency_key": str(uuid.uuid4()),
                "batches": [{"objects": chunk}],
            })
            if result.is_error():
                self._fail("Price update failed", result)
            sent += len(chunk)
        self.stdout.write(self.style.SUCCESS(
            f"Sent {sent} price(s) to Square."
        ))

    # -- entry point ------------------------------------------------------

    def handle(self, *args, **options):
        from square.client import Client

        pull, push, interactive = options["pull"], options["push"], options["interactive"]
        if sum(map(bool, (pull, push, interactive))) > 1:
            raise CommandError(
                "Choose one of --pull, --push or --interactive. Two directions "
                "in one run would settle a row twice and the second would win "
                "silently."
            )

        client = Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )
        self._preflight(client)

        agreed, differing, variable, stale = self._rows(client)
        self._report(agreed, differing, variable, stale)

        if not differing:
            return
        if not (pull or push or interactive):
            self.stdout.write("")
            self.stdout.write(
                "Read-only. Settle these with --pull (Square wins), --push "
                "(we win), or --interactive."
            )
            return

        if interactive:
            to_pull, to_push = self._choose(differing)
            self._pull(to_pull, options["dry_run"])
            self._push(client, to_push, options["dry_run"])
            return

        rows = self._in_scope(differing, options["skus"], options["changed_since"])
        if pull:
            self._pull(rows, options["dry_run"])
        else:
            self._push(client, rows, options["dry_run"])
