import csv
from django.core.management.base import BaseCommand, CommandError
from scarves.models import FinishedProduct, InventoryLog


class Command(BaseCommand):
    help = "Import weekend Square sales from a CSV export and update inventory."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to the Square sales CSV export.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without making changes.",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_file"]
        dry_run = options["dry_run"]

        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_path}")

        imported = 0
        skipped_no_sku = 0
        skipped_duplicate = 0
        skipped_not_found = 0
        errors = []

        for row in rows:
            sku = (row.get("SKU") or "").strip()
            transaction_id = (row.get("Transaction ID") or "").strip()
            item_name = (row.get("Item") or "").strip()
            qty_str = (row.get("Qty") or "1").strip()
            date = (row.get("Date") or "").strip()

            if not sku:
                self.stdout.write(
                    f"  SKIP (no SKU): {item_name!r} [{transaction_id or 'no txn id'}]"
                )
                skipped_no_sku += 1
                continue

            try:
                qty = int(float(qty_str))
            except ValueError:
                qty = 1

            # Idempotency: skip if this transaction+SKU already logged
            if transaction_id and InventoryLog.objects.filter(
                sale_reference=transaction_id,
                finished_product__sku=sku,
            ).exists():
                self.stdout.write(f"  DUP  (already imported): {sku} txn={transaction_id}")
                skipped_duplicate += 1
                continue

            try:
                fp = FinishedProduct.objects.get(sku=sku)
            except FinishedProduct.DoesNotExist:
                self.stdout.write(
                    self.style.WARNING(f"  MISS (SKU not found): {sku!r} — {item_name!r}")
                )
                skipped_not_found += 1
                errors.append(sku)
                continue

            if dry_run:
                self.stdout.write(
                    f"  DRY  {sku} → {fp.name}  qty={qty}  "
                    f"on_hand={fp.number_on_hand}→{max(fp.number_on_hand - qty, 0)}"
                )
            else:
                # An undyed passthrough keeps its count on the raw row and this
                # one is the mirror, so writing here is re-derived away by
                # save() — the row snaps back and the import prints OK having
                # moved nothing. `set_on_hand` writes to whichever row actually
                # holds the pile, and clamps at zero. Same reasoning the webhook
                # applies with its own passthrough branch.
                fp.set_on_hand(fp.number_on_hand - qty)
                InventoryLog.objects.create(
                    finished_product=fp,
                    raw_product=fp.raw_product,
                    log_type=InventoryLog.SALE,
                    quantity=-qty,
                    sale_reference=transaction_id,
                    notes=f"Imported from CSV: {date} {item_name}",
                )
                self.stdout.write(f"  OK   {sku} → {fp.name}  -{qty}  on_hand={fp.number_on_hand}")

            imported += 1

        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes saved."))
        self.stdout.write(self.style.SUCCESS(
            f"Done: {imported} imported, {skipped_duplicate} duplicate, "
            f"{skipped_no_sku} skipped (no SKU), {skipped_not_found} SKU not found."
        ))
        if errors:
            self.stdout.write(self.style.WARNING(f"Missing SKUs: {', '.join(errors)}"))
