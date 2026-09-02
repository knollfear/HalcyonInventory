"""Talking to Square's Orders API, and turning an order into ledger lines.

**Separated from the command that currently calls it, on purpose.** The work
and the thing that triggers the work are different concerns: today the trigger
is somebody running `import_square_orders`, and the obvious next trigger is
the webhook — which already holds a retrieved order and would otherwise have
to instantiate a management command or copy this file to use it. Copying is
the drift `salesimport` exists to prevent, and it shows up as two totals for
one weekend.

So the rule this module keeps: **nothing in here knows why it is being
called.** No argument parsing, no printing, no `self.stdout`. A caller hands
in an order and gets line dicts back, in the shape `salesimport` expects.
Swapping a command for a webhook, a queue consumer or a backfill is then a
change to what calls this, and not to this.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .models import SaleLine


class SquareUnavailable(Exception):
    """Square refused a request or answered in a shape we do not understand.

    Its own exception rather than `CommandError`, because a command is only
    one of the things that may be calling this. A webhook raising
    `CommandError` would be a module telling its caller what kind of program
    it is — the callers translate it into whatever they need to say.
    """

#: Square's page size for SearchOrders. The API caps it; asking for more just
#: gets fewer back with a cursor, so this only decides how many round trips.
PAGE = 500

#: Guard against a paging bug turning into an unbounded loop against a live
#: API. A faire weekend is a few hundred orders; a whole season is thousands.
MAX_PAGES = 200

#: Object ids per BatchRetrieveCatalogObjects call. Square caps it at 1000.
BATCH = 500


def client():
    from square.client import Client
    return Client(
        access_token=settings.SQUARE_ACCESS_TOKEN,
        environment=settings.SQUARE_ENVIRONMENT,
    )


def search(client, start, end):
    """Every COMPLETED order closed inside the window, paged through.

    Filtered and sorted on `closed_at` — Square requires the sort field to
    match the filter, and closed_at is when the money was taken rather
    than when the cart was opened.
    """
    zone = timezone.get_current_timezone()
    start_at = timezone.make_aware(datetime.combine(start, time.min), zone)
    end_at = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), zone)

    orders, cursor, pages = [], None, 0
    while pages < MAX_PAGES:
        body = {
            "location_ids": [settings.SQUARE_LOCATION_ID],
            "query": {
                "filter": {
                    "date_time_filter": {
                        "closed_at": {
                            "start_at": start_at.isoformat(),
                            "end_at": end_at.isoformat(),
                        }
                    },
                    "state_filter": {"states": ["COMPLETED"]},
                },
                "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"},
            },
            "limit": PAGE,
        }
        if cursor:
            body["cursor"] = cursor
        result = client.orders.search_orders(body=body)
        if result.is_error():
            raise SquareUnavailable(f"Square refused the order search: {result.errors}")
        payload = result.body or {}
        orders.extend(payload.get("orders") or [])
        cursor = payload.get("cursor")
        pages += 1
        if not cursor:
            break
    else:
        raise SquareUnavailable(
            f"Stopped after {MAX_PAGES} pages — the cursor never ran out, "
            "which means the paging is wrong rather than the season being big."
        )
    return orders


def category_index(client):
    """`(variation id → category name, category id → name)`, built once.

    Order lines carry no category and season totals need one, because the
    wax hands were on this till through 2024 and are gone. Resolved from
    Square's catalogue rather than from the local one: a line for
    something this app never knew about still needs a category, and that
    is exactly the group being excluded.
    """
    items, names, cursor = [], {}, None
    while True:
        result = client.catalog.list_catalog(cursor=cursor, types="ITEM,CATEGORY")
        if result.is_error():
            raise SquareUnavailable(f"Square refused the catalogue list: {result.errors}")
        payload = result.body or {}
        for obj in payload.get("objects") or []:
            if obj.get("type") == "CATEGORY":
                names[obj["id"]] = (obj.get("category_data") or {}).get("name", "")
            elif obj.get("type") == "ITEM":
                items.append(obj)
        cursor = payload.get("cursor")
        if not cursor:
            break

    by_variation = {}
    for item in items:
        data = item.get("item_data") or {}
        # `category_id` is the older shape and `categories` the newer one;
        # the API version in use still answers with either depending on how
        # the item was written, so both are read.
        label = category_name(data, names)
        for variation in data.get("variations") or []:
            by_variation[variation.get("id")] = label
    return by_variation, names


def categories_for_deleted(client, variation_ids, names):
    """Category per variation id, for objects no longer in the catalogue.

    `ListCatalog` returns the living catalogue; this asks about specific
    objects and says `include_deleted_objects`, which is the only way to
    learn what a line sold in 2021 actually was. The parent item comes
    back as a related object, and the category hangs off that.
    """
    found = {}
    ids = sorted(variation_ids)
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        result = client.catalog.batch_retrieve_catalog_objects(body={
            "object_ids": chunk,
            "include_deleted_objects": True,
            "include_related_objects": True,
        })
        if result.is_error():
            raise SquareUnavailable(f"Square refused the catalogue lookup: {result.errors}")
        payload = result.body or {}
        items = {
            obj["id"]: obj for obj in payload.get("related_objects") or []
            if obj.get("type") == "ITEM"
        }
        # A category can be deleted too, in which case its name is not in
        # the list we already have; fall back to the id rather than to
        # blank, so the pill at least groups the lines together.
        for obj in payload.get("objects") or []:
            if obj.get("type") != "ITEM_VARIATION":
                continue
            item = items.get((obj.get("item_variation_data") or {}).get("item_id"))
            if item is None:
                continue
            found[obj["id"]] = category_name(item.get("item_data") or {}, names)
    return {key: value for key, value in found.items() if value}


def lines_from_order(order, categories, skipped):
    order_id = order.get("id") or ""
    when = sold_at(order)
    if not order_id or when is None:
        skipped["order with no id or no timestamp"] += 1
        return []

    tender = (order.get("tenders") or [{}])[0]
    card = ((tender.get("card_details") or {}).get("card") or {})
    seen = Counter()
    out = []

    for item in order.get("line_items") or []:
        name = item.get("name") or ""
        variation_name = item.get("variation_name") or ""
        variation_id = item.get("catalog_object_id") or ""

        # The same key shape the CSV path builds, so the two doors can be
        # compared line for line when checking one against the other.
        base = f"{name}|{variation_name}"
        seen[base] += 1
        gross = cents(item.get("gross_sales_money"))
        discount = -cents(item.get("total_discount_money"))

        out.append({
            "order_id": order_id,
            "sold_at": when,
            "line_key": f"{base}|{seen[base]}",
            "event_type": SaleLine.PAYMENT,
            "category": categories.get(variation_id, ""),
            "item_name": name,
            "price_point": variation_name,
            "sku": "",
            "square_variation_id": variation_id,
            "quantity": Decimal(item.get("quantity") or "1"),
            "gross_cents": gross,
            "discount_cents": discount,
            "net_cents": gross + discount,
            "tax_cents": cents(item.get("total_tax_money")),
            "location": order.get("location_id") or "",
            "device": "",
            "customer_name": "",
            "card_brand": card.get("card_brand") or "",
        })

    for refund in order.get("refunds") or []:
        # A refund is a line of its own rather than a negative adjustment
        # to the sale, so a season total can be read gross or net and the
        # page can say which it is showing.
        amount = -cents(refund.get("amount_money"))
        out.append({
            "order_id": order_id,
            "sold_at": when,
            "line_key": f"refund|{refund.get('id', '')}|1",
            "event_type": SaleLine.REFUND,
            "category": "",
            "item_name": refund.get("reason") or "Refund",
            "price_point": "",
            "sku": "",
            "square_variation_id": "",
            "quantity": Decimal(0),
            "gross_cents": amount,
            "discount_cents": 0,
            "net_cents": amount,
            "tax_cents": 0,
            "location": order.get("location_id") or "",
            "device": "",
            "customer_name": "",
            "card_brand": card.get("card_brand") or "",
        })
    return out


def sold_at(order):
    for field in ("closed_at", "created_at"):
        raw = order.get(field)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def cents(money):
    return int((money or {}).get("amount") or 0)


def category_name(item_data, names):
    """The category label for an item, across the shapes Square answers in.

    `category_id` is the old field and is `None` on everything written
    recently; `reporting_category` is the one the dashboard's own reports use;
    `categories` is the list. All three are read because the catalogue has
    objects of every vintage in it.
    """
    category_id = item_data.get("category_id") or ""
    if not category_id:
        reporting = item_data.get("reporting_category") or {}
        category_id = reporting.get("id") or ""
    if not category_id:
        listed = item_data.get("categories") or []
        category_id = listed[0].get("id", "") if listed else ""
    if not category_id:
        return ""
    return names.get(category_id) or category_id
