"""Move a fancy blank's cost out of `price` and into `fancying_cost`.

`price` means what a supplier charges. A fancy blank has no supplier — it is
a plain blank somebody added line work to — so its whole cost sat in `price`
as a second copy of the silk, and the copy went stale the moment the plain
blank's price moved. By the time anybody looked, the three fancy blanks stood
$8.96, $13.78 and $17.30 above plain-plus-fancying, each by a different
amount, and nothing in the app could show that.

After this, each fact has one home: the plain blank carries the silk, the
fancy blank carries the line work, and `RawProduct.blank_cost` adds them on
every read.

The fancying figure is the same for every fancy blank because that is how the
work is actually priced — one rate to fancy one piece, whatever it started
as. A blank that needs its own is edited afterwards.
"""
from decimal import Decimal

from django.db import migrations

FANCYING = Decimal("25.00")


def split_cost(apps, schema_editor):
    RawProduct = apps.get_model("scarves", "RawProduct")
    for blank in RawProduct.objects.filter(plain_counterparts__isnull=False).distinct():
        blank.fancying_cost = FANCYING
        blank.price = Decimal("0")
        blank.save(update_fields=["fancying_cost", "price"])


def rejoin_cost(apps, schema_editor):
    """Put the whole cost back in `price`, derived rather than remembered."""
    RawProduct = apps.get_model("scarves", "RawProduct")
    for blank in RawProduct.objects.filter(plain_counterparts__isnull=False).distinct():
        plain = blank.plain_counterparts.first()
        base = plain.price if plain else Decimal("0")
        blank.price = (base or Decimal("0")) + (blank.fancying_cost or Decimal("0"))
        blank.fancying_cost = None
        blank.save(update_fields=["fancying_cost", "price"])


class Migration(migrations.Migration):

    dependencies = [("scarves", "0044_rawproduct_fancying_cost")]

    operations = [migrations.RunPython(split_cost, rejoin_cost)]
