from django import template

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
