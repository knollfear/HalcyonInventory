"""The Square webhook: a sale at the till becomes a ledger row here."""
import base64
import hashlib
import hmac
import json
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from ..models import FinishedProduct, InventoryLog, UnmatchedSale
from .. import ledger


def _verify_square_signature(request):
    signature = request.headers.get("x-square-hmacsha256-signature", "")
    key = getattr(settings, "SQUARE_WEBHOOK_SIGNATURE_KEY", "")
    if not signature or not key:
        return False
    url = settings.SQUARE_WEBHOOK_URL
    payload = url + request.body.decode("utf-8")
    expected = base64.b64encode(
        hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(signature, expected)


@csrf_exempt
def square_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    if not _verify_square_signature(request):
        return HttpResponse(status=403)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if payload.get("type") != "order.updated":
        return HttpResponse(status=200)

    order_data = payload.get("data", {}).get("object", {}).get("order_updated", {})
    if order_data.get("state") != "COMPLETED":
        return HttpResponse(status=200)

    order_id = order_data.get("order_id")

    from square.client import Client
    client = Client(
        access_token=settings.SQUARE_ACCESS_TOKEN,
        environment=settings.SQUARE_ENVIRONMENT,
    )

    result = client.orders.retrieve_order(order_id)
    if result.is_error():
        return HttpResponse(status=500)

    order = result.body.get("order", {})
    line_items = order.get("line_items", [])
    sold_at = _order_sold_at(order)

    with transaction.atomic():
        for item in line_items:
            variation_id = item.get("catalog_object_id")
            qty = int(item.get("quantity", "0"))
            if qty == 0:
                continue

            fp = None
            if variation_id:
                fp = FinishedProduct.objects.filter(
                    square_variation_id=variation_id, is_active=True
                ).first()

            if fp is None:
                # Not a product this app knows: rung up as a generic item, sold
                # as a custom amount, or a variation that was never synced.
                # This used to `continue`, which meant a scarf nobody could
                # name left the tent and nothing anywhere recorded it — Square
                # had the money, this app still had the stock, and neither said
                # they disagreed. Now it goes in a queue a person empties.
                UnmatchedSale.objects.get_or_create(
                    order_id=order_id,
                    line_uid=item.get("uid") or "",
                    defaults={
                        "name": item.get("name") or "",
                        "variation_name": item.get("variation_name") or "",
                        "square_variation_id": variation_id or "",
                        "quantity": qty,
                        "amount_cents": (
                            item.get("total_money", {}).get("amount") or 0
                        ),
                        "sold_at": sold_at,
                    },
                )
                continue

            # Square sends order.updated more than once for an order, and
            # COMPLETED is not a one-shot state — so without this a re-delivery
            # decrements the same sale again. One line item is one row: a
            # genuine second sale of the same product arrives on its own order.
            already = InventoryLog.objects.filter(
                finished_product=fp,
                sale_reference=order_id,
                log_type=InventoryLog.SALE,
            ).exists()
            if already:
                continue

            # The ledger locks the row that holds the count — the raw one for
            # a passthrough, whose finished row is only a mirror — and writes
            # the sale in the same call. Two webhooks for one weekend's rush
            # arrive together, and a read-modify-write here was unlocked.
            ledger.move(
                fp, -qty,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
                notes="Square sale via webhook.",
                sale_reference=order_id,
            )

    return HttpResponse(status=200)


def _order_sold_at(order):
    """When Square says the order happened, not when we heard about it.

    The reconciliation screen pairs a sale with a photo taken within fifteen
    minutes of it, so this timestamp is the join key — using receipt time
    instead would drift by however long the webhook took to arrive, or by a
    whole redelivery.
    """
    for field in ("closed_at", "created_at"):
        raw = order.get(field)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return timezone.now()
