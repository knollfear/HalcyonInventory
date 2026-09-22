"""Create the three suppliers already implied by the order URLs, and link up.

The `order_url` on 25 active blanks resolved to exactly three domains —
Wool2dye4 eighteen times, Dharma six, Knomad once. That repetition is the
whole argument for the model, so shipping it empty and asking somebody to
retype what is already in the data would be the wrong way round.

**Matched on the URL's domain, and on nothing else.** A domain is evidence
about who sells a thing; a product name is a coincidence. Anything whose
domain is not in the map below is left unlinked rather than guessed at —
including every notion, which has no URL at all because the supplier is a
person at the next stall. Those get linked by hand, which is the job the
card page exists for.

Reversible: the reverse drops the links and the rows, and nothing else in
the schema depends on them yet.
"""

from django.db import migrations


#: domain -> (name, website). Names as the shop says them, not as the domain
#: spells them, because the name is what prints in the reorder column.
KNOWN = {
    "www.wool2dye4.com": ("Wool2dye4", "https://www.wool2dye4.com/"),
    "wool2dye4.com": ("Wool2dye4", "https://www.wool2dye4.com/"),
    "www.dharmatrading.com": ("Dharma Trading Co.", "https://www.dharmatrading.com/"),
    "dharmatrading.com": ("Dharma Trading Co.", "https://www.dharmatrading.com/"),
    "www.knomadyarn.com": ("Knomad Yarn", "https://www.knomadyarn.com/"),
    "knomadyarn.com": ("Knomad Yarn", "https://www.knomadyarn.com/"),
}


def link(apps, schema_editor):
    from urllib.parse import urlparse

    Supplier = apps.get_model("scarves", "Supplier")
    RawProduct = apps.get_model("scarves", "RawProduct")

    made = {}
    for product in RawProduct.objects.exclude(order_url="").iterator():
        domain = urlparse(product.order_url).netloc.lower()
        known = KNOWN.get(domain)
        if known is None:
            continue
        name, website = known
        if name not in made:
            made[name], _ = Supplier.objects.get_or_create(
                name=name, defaults={"website": website},
            )
        product.supplier = made[name]
        product.save(update_fields=["supplier"])


def unlink(apps, schema_editor):
    Supplier = apps.get_model("scarves", "Supplier")
    RawProduct = apps.get_model("scarves", "RawProduct")

    RawProduct.objects.filter(
        supplier__name__in=[name for name, _ in KNOWN.values()]
    ).update(supplier=None)
    Supplier.objects.filter(
        name__in=[name for name, _ in KNOWN.values()]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scarves", "0051_supplier_alter_rawproduct_order_url_and_more"),
    ]

    operations = [
        migrations.RunPython(link, unlink),
    ]
