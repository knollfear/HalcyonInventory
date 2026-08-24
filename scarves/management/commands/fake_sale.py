from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves.models import FinishedProduct, InventoryLog


class Command(BaseCommand):
    help = "Simulate a sale webhook for local testing — decrements inventory and writes an InventoryLog."

    def add_arguments(self, parser):
        parser.add_argument("finished_product_id", type=int, help="PK of the FinishedProduct to sell")
        parser.add_argument("--qty", type=int, default=1, help="Quantity sold (default: 1)")

    def handle(self, *args, **options):
        pk = options["finished_product_id"]
        qty = options["qty"]

        try:
            fp = FinishedProduct.objects.get(pk=pk, is_active=True)
        except FinishedProduct.DoesNotExist:
            raise CommandError(f"No active FinishedProduct with pk={pk}")

        before = fp.number_on_hand

        with transaction.atomic():
            fp.number_on_hand = max(fp.number_on_hand - qty, 0)
            fp.save(update_fields=["number_on_hand"])

            InventoryLog.objects.create(
                finished_product=fp,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_TEST,
                quantity=-qty,
                sale_reference="FAKE-SALE-TEST",
                notes="Simulated sale via fake_sale management command.",
            )

        self.stdout.write(self.style.SUCCESS(
            f"Sold {qty}x '{fp.name}': {before} → {fp.number_on_hand} on hand."
        ))
        self.stdout.write(f"InventoryLog created (type=sale, qty=-{qty}, ref=FAKE-SALE-TEST)")
