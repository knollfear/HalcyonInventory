import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scarves.models import FinishedProduct


class Command(BaseCommand):
    help = "Create a test order in Square sandbox to trigger the order.updated webhook."

    def add_arguments(self, parser):
        parser.add_argument("finished_product_id", type=int, help="PK of the FinishedProduct to sell")
        parser.add_argument("--qty", type=int, default=1, help="Quantity (default: 1)")

    def handle(self, *args, **options):
        from square.client import Client

        try:
            fp = FinishedProduct.objects.get(pk=options["finished_product_id"], is_active=True)
        except FinishedProduct.DoesNotExist:
            raise CommandError(f"No active FinishedProduct with pk={options['finished_product_id']}")

        if not fp.square_variation_id:
            raise CommandError(f"'{fp.name}' has no square_variation_id — run sync_to_square first.")

        client = Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )

        # Create the order
        order_result = client.orders.create_order(body={
            "order": {
                "location_id": settings.SQUARE_LOCATION_ID,
                "line_items": [
                    {
                        "quantity": str(options["qty"]),
                        "catalog_object_id": fp.square_variation_id,
                    }
                ],
            },
            "idempotency_key": str(uuid.uuid4()),
        })

        if order_result.is_error():
            for e in order_result.errors:
                self.stderr.write(self.style.ERROR(f"{e['category']}: {e['detail']}"))
            return

        order_id = order_result.body["order"]["id"]
        total = order_result.body["order"]["total_money"]["amount"]
        self.stdout.write(f"Order created: {order_id} (total: ${total / 100:.2f})")

        # Pay for the order using sandbox test card nonce
        payment_result = client.payments.create_payment(body={
            "source_id": "cnon:card-nonce-ok",
            "idempotency_key": str(uuid.uuid4()),
            "amount_money": {
                "amount": total,
                "currency": "USD",
            },
            "order_id": order_id,
            "location_id": settings.SQUARE_LOCATION_ID,
        })

        if payment_result.is_error():
            for e in payment_result.errors:
                self.stderr.write(self.style.ERROR(f"{e['category']}: {e['detail']}"))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Payment completed. Square will now fire order.updated webhook for order {order_id}."
        ))
        self.stdout.write("Watch Django admin → InventoryLog for the incoming sale entry.")
