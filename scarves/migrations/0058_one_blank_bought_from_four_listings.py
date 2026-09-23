# One blank, several listings.
#
# The yarn cutters are four Amazon pages that all ring as one product: the
# crew cannot tell them apart at the till and nobody needs them to, so
# splitting the product to hold four links would invent four counts nobody
# records. The column widens to a line per listing instead; the first line
# is still the headline link, so every blank that has one URL today is
# unchanged.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scarves', '0057_what_turned_up_is_not_always_what_was_ordered'),
    ]

    operations = [
        migrations.AlterField(
            model_name='rawproduct',
            name='order_url',
            field=models.TextField(blank=True, help_text="The product page for this exact blank, when there is one. Not the supplier — that is `supplier`, and it is a separate question: this is 'where do I buy this thing', that is 'who from'.\n\n**One per line, because one blank is often bought from several listings.** The yarn cutters are four Amazon pages that all ring as one product — the crew cannot tell them apart at the till and nobody needs them to — so splitting the product to hold four links would invent four counts nobody records. The first line is the headline link (`reorder_link`); the rest are `extra_order_urls`. Kept as lines in one column rather than a table of links: the listing itself carries the price and the picture, and the invoice records what was actually paid."),
        ),
    ]
