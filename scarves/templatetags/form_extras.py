from django import template

from scarves.dyeamounts import format_ounces

register = template.Library()

@register.filter
def get_item(form, key):
    """
    Usage: {{ form|get_item:"field_name" }}
    Returns a BoundField for dynamic form access in templates.
    """
    return form[key]


@register.filter
def lookup(mapping, key):
    """Usage: {{ mapping|lookup:key }} — a dict read that tolerates a miss.

    `get_item` above raises on a missing key, which is right for a form field
    (a template naming a field that doesn't exist is a bug worth seeing). A
    lookup table is the other case: the display map keys tokens by product pk
    and a peg holding a product the page didn't offer is a real state, not a
    programming error.
    """
    if mapping is None:
        return ""
    return mapping.get(key, "")


@register.filter
def money_cents(value):
    """Integer cents → `$1,234`. Whole dollars, because these are season totals.

    The ledger stores cents so nothing rounds on the way in; every page that
    shows a season figure wants dollars, and doing the division in the
    template rather than the model keeps one storage unit and one display
    unit instead of a third thing in between.
    """
    try:
        dollars = round(float(value) / 100)
    except (TypeError, ValueError):
        return ""
    return f"${dollars:,}"


@register.filter
def plain(value):
    """A number with thousands separators and no decimals."""
    try:
        return f"{round(float(value)):,}"
    except (TypeError, ValueError):
        return ""


@register.filter
def ounces(value):
    """A dye weight as the sheet and the recipe row print it: `0.75 oz`.

    Empty for None, which is what "no amount on file" and "no figures for
    this fibre" both come back as — a zero there would read as "no dye".
    """
    return format_ounces(value)
