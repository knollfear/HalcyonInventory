from django import template

register = template.Library()

@register.filter
def get_item(form, key):
    """
    Usage: {{ form|get_item:"field_name" }}
    Returns a BoundField for dynamic form access in templates.
    """
    return form[key]
