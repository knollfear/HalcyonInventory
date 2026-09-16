"""How much dye goes in a bath.

**The dye book writes one number per recipe per dye, for a five-skein bath of
yarn, and that is the number somebody types in.** The reality underneath it is
that dye is per skein: a four-skein bath takes four fifths of what is written.
The book is not wrong, it is just recorded at the size the bath usually runs
at, so the app stores the figure as written (`RecipeDye.book_ounces`, checkable
against the page by eye) and divides by the basis whenever it prints one.

The basis lives on `RawProductCategory.dye_book_bath_units` rather than being a
constant, and only yarn has one. That is what keeps the arithmetic off silk:
the ounces on a recipe are the book's *yarn* numbers, and what a silk scarf
drinks is a question nobody has answered. A category with no basis prints no
amount at all, because a number beside a silk bath would be read at the sink as
a measurement somebody took.

So this module answers exactly one question — what goes in *this* pot — and
returns None rather than a best guess wherever it cannot.
"""

from decimal import Decimal


def bath_basis(raw_product):
    """The dye book's bath size for this blank's table, or None.

    None for anything whose category carries no basis, and for a blank with
    no category at all.
    """
    category = getattr(raw_product, "category", None)
    return getattr(category, "dye_book_bath_units", None) if category else None


def bath_amounts(recipe, raw_product, units):
    """`[(RecipeDye, ounces or None)]` for one bath, in slot order.

    Both halves of the pair matter to a caller: the dye is printed whether or
    not there is an amount for it, because a bath with no figures on file
    still needs its dyes listed. An amount of None means "not on file here",
    never zero.
    """
    if recipe is None:
        return []
    basis = bath_basis(raw_product)
    return [
        (rd, rd.ounces_for(units, basis)) for rd in recipe.recipe_dyes.all()
    ]


def format_ounces(value):
    """`Decimal("0.750")` → `"0.75 oz"`. Empty string for None.

    Trailing zeros go because the book writes 1/2 and 3/4 of an ounce, and
    `0.500 oz` on a sheet reads as a precision nobody weighed to.
    """
    if value is None:
        return ""
    value = Decimal(value).normalize()
    if value == value.to_integral_value():
        value = value.quantize(Decimal(1))
    return f"{value:f} oz"
