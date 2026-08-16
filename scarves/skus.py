"""Where a FinishedProduct's SKU comes from.

Shared by `FinishedProduct.save()` and the `generate_skus` command so there is
one definition of what a SKU looks like. Before this, generation lived only in
the command, so anything created through the admin, the bulk matrix or a shell
had no SKU until somebody remembered to run it — which is how products with no
barcode accumulated.

**A SKU is write-once.** It gets printed onto reference sheets and onto
stickers stuck to scarves, and it is what Square holds to identify a variation.
None of those can be rewritten from here, so nothing in this module ever
changes a SKU that already exists; it only fills a blank one. Regenerating is
possible but deliberately awkward (`generate_skus --overwrite`, which asks
first), because the symptom of getting it wrong is an item scanning to nothing
at the till weeks later.
"""

import re

#: Characters of the raw product and of the recipe that go into a SKU. Six
#: each keeps `SLUG6-SLUG6` at 13 characters, which prints at 7.7 mil on the
#: 1.75in label stock — comfortably scannable. Raising it makes the bars
#: narrower; see MIN_MODULE_MIL in `labels.py` for where that stops working.
PART_LEN = 6


def slug(text, max_len=PART_LEN):
    """Uppercase alphanumerics only, truncated."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())[:max_len]


def base_for(product):
    """The SKU a product wants, before any collision suffix."""
    return f"{slug(product.raw_product.name)}-{slug(product.recipe.name)}"


def unique_sku(product, taken=None):
    """A free SKU for `product`.

    `taken` is an optional in-memory set of SKUs already spoken for, so a
    batch run can avoid a query per product. Left out, uniqueness is checked
    against the database — which is what a single `save()` needs, since the
    other rows it must not collide with are already committed.
    """
    from .models import FinishedProduct

    base = candidate = base_for(product)
    suffix = 2
    while True:
        if taken is not None:
            clash = candidate in taken
        else:
            others = FinishedProduct.objects.filter(sku=candidate)
            if product.pk:
                others = others.exclude(pk=product.pk)
            clash = others.exists()
        if not clash:
            return candidate
        candidate = f"{base}{suffix}"
        suffix += 1
