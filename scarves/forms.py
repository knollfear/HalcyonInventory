# scarves/forms.py
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from . import blanks, crew
from .labels import (
    BARCODE as LABEL_BARCODE,
    STYLE_CHOICES as LABEL_STYLE_CHOICES,
)
from .skus import slug
from .models import (  # RecipeDye is the through model
    UNCATEGORIZED_BRAND,
    FinishedProduct,
    DisplayFixture,
    BoothPhoto,
    Dye,
    DyeBrand,
    Employee,
    LabelStock,
    Recipe,
    RecipeDye,
    CatalogGroup,
    RawProduct,
    RawProductCategory,
    Supplier,
    TimeEntry,
    dye_match_key,
)

def picker_dyes():
    """Every dye, for a picker to offer.

    Deliberately *not* filtered to `in_stock`. A recipe records what it was
    dyed with, so a jar running out must not make its recipes un-editable —
    but the sharper reason is that these pickers can now create a dye. A dye
    hidden from the list is one somebody types in again, and the second
    `Peacock Blue` is indistinguishable from the first everywhere except the
    two rows it splits its history across. Out of stock is said on the row
    instead.
    """
    return Dye.objects.select_related("brand").all()


def dye_option_attrs(dye):
    """What one dye's `<option>` carries for the type-ahead to read.

    Defined once because it is produced in two places: `DyeSelect` renders it
    into the page, and `dye_create` hands the same thing back for a dye added
    mid-entry so the script can build a matching option without a reload. Two
    copies drift, and the way drift shows here is a dye that is in the list
    but can't be searched for — which reads as "it isn't there", and gets
    typed in a second time.
    """
    # `sort_name` is in the haystack as well as the full name so that typing
    # "peacock" finds "416 Peacock Blue", which by its name alone answers
    # only to its catalog number.
    haystack = " ".join(
        part for part in (dye.name, dye.sort_name, dye.brand.name, dye.sku) if part
    )
    attrs = {
        "data-hex": dye.hex_color or "",
        "data-name": dye.name,
        "data-brand": dye.brand.name,
        "data-sort": dye.sort_name.lower(),
        "data-key": dye_match_key(dye.name),
        "data-search": haystack.lower(),
    }
    if not dye.in_stock:
        attrs["data-out-of-stock"] = "1"
    return attrs


class RecipeCheckboxes(forms.CheckboxSelectMultiple):
    """Colourways as a checkbox each, with the recipe's dyes beside it.

    **Checkboxes rather than a multi-select, because a multi-select loses
    everything to one stray click.** Pick thirty colourways, misclick, and
    all thirty are gone with nothing said — the silent-destructive failure
    this app keeps designing away from, and on a control where the only
    warning is a modifier key nobody told you about. Every click here is
    independent, and nothing has to be held down.

    **The chips are the point, not decoration.** A colourway is a name like
    `Babs` or `Forest Fire`, which says nothing about what it looks like to
    anybody who has not dyed it. The dyes are what a person recognises.

    Each dye gets its own chip, in the recipe's own order, and they are never
    blended into one average colour: a scarf shows each dye distinctly and
    flows between them, so an averaged swatch would be a colour that does not
    exist on the product. A dye with no hex on file gets hatching rather than
    a guess, the same rule `.dyechip` follows everywhere else — a placeholder
    swatch is something somebody reads off the screen as fact.
    """

    option_template_name = "scarves/widgets/recipe_checkbox.html"

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(
            name, value, label, selected, index, subindex=subindex, attrs=attrs
        )
        # `value.instance` is the Recipe; the template needs the object to
        # reach its dyes. Prefetched by the field, so this costs no query.
        option["recipe"] = getattr(value, "instance", None)
        return option


class DyeSelect(forms.Select):
    """The dye picker: a real `<select>` the type-ahead script drives.

    The script hides this and puts a text box in front of it, but the select
    itself is what the form posts and what `ModelChoiceField` validates, so a
    page with no JavaScript is the same form with a longer list — nothing
    about correctness rides on the script running.

    Everything the type-ahead needs rides on the options as data attributes
    rather than a JSON map rendered beside them. That is what lets a dye
    added mid-entry appear in *every* picker on the page: the script appends
    one `<option>` and the option carries its own colour and search text. A
    map is a snapshot of the moment the page rendered, and the dye you just
    typed in is by definition not in it.
    """

    def __init__(self, attrs=None):
        super().__init__(attrs={"class": "dye-select", "data-dye-picker": "1", **(attrs or {})})

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(
            name, value, label, selected, index, subindex=subindex, attrs=attrs
        )
        dye = getattr(value, "instance", None)
        if dye is not None:
            option["attrs"].update(dye_option_attrs(dye))
        return option

    def optgroups(self, name, value, attrs=None):
        """Options in the order a person reads a list of dyes.

        Sorted here rather than in the queryset because the key is
        `Dye.sort_name` — the name with its catalog number taken off — which
        is a Python property, and pushing it into SQL would mean a second
        copy of the same regex living in the database. A few hundred dyes
        sort in microseconds.

        Django gives an ungrouped `<select>` one "group" per option, so the
        sort is over the groups themselves; sorting inside them silently
        does nothing, which looks exactly like the code working.
        """
        def key(group):
            option = (group[1] or [{}])[0]
            option_attrs = option.get("attrs", {})
            return (
                option.get("value") != "",   # the empty "---------" leads
                option_attrs.get("data-out-of-stock", ""),   # then in stock
                option_attrs.get("data-sort", ""),
            )

        groups = sorted(super().optgroups(name, value, attrs), key=key)
        # Re-numbered because the index is what an option's id is built from,
        # and ids that jump around read as a bug in whatever renders them.
        return [(label, options, i) for i, (label, options, _) in enumerate(groups)]


class NewDyeForm(forms.Form):
    """Add a dye from a recipe picker, without leaving the recipe.

    The dye that stops entry is the one that isn't on the list: the recipe is
    in front of you, the jar is in your hand, and the app's answer is "go to
    the admin, make a dye, come back and start the row again". What actually
    happens is the recipe gets typed with the dyes that *were* on the list,
    and the missing one is lost — silently, because the recipe looks filled
    in.

    So this asks for a name and nothing else. Brand and colour are real
    questions with real answers, but they are not answerable at speed with
    wet gloves on, and demanding them here buys a tidy row at the price of no
    row at all. `Dye.needs_review` is what makes the deferred half findable,
    and the admin's dye list, filtered to "needs review", is where it gets
    finished.
    """

    name = forms.CharField(max_length=100)

    def clean_name(self):
        # Collapsed rather than merely stripped: "peacock  blue" and "peacock
        # blue" are the same dye typed twice, and the duplicate check below
        # only sees that if the spacing is normalised first.
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError("Please give the dye a name.")
        return name

    def find_existing(self):
        """The dye this name already refers to, if there is one.

        Matched on `dye_match_key`, so "fire engine red" finds `402 Fire
        Engine Red (Primary)` rather than making a second one beside it. The
        catalog number and the tag are the two things somebody typing from
        memory leaves off, and this is the last check before a duplicate
        exists — the picker's own version of it decides where to put a row
        on a menu, this decides what is in the database.
        """
        wanted = dye_match_key(self.cleaned_data["name"])
        for dye in Dye.objects.select_related("brand"):
            if dye_match_key(dye.name) == wanted:
                return dye
        return None

    def save(self):
        """The dye for this name, and whether it had to be created."""
        existing = self.find_existing()
        if existing:
            return existing, False

        brand, _ = DyeBrand.objects.get_or_create(name=UNCATEGORIZED_BRAND)
        dye = Dye.objects.create(name=self.cleaned_data["name"], brand=brand)
        return dye, True


class RecipeDyesForm(forms.Form):
    """Edit just the dye assignments of one existing recipe.

    Unlike QuickRecipeRowForm this never touches the recipe's name and never
    creates recipes — it is for filling in dyes on records that already exist.

    Offers every dye rather than the in-stock ones — see `picker_dyes` for
    why that matters more than it used to.
    """

    SLOTS = 5  # RecipeDye.order validates 1..5

    #: Rides on the same row and the same Save as the dyes, because it is the
    #: same pass down the same list — the person filling in a colorway's dyes
    #: is the person who knows whether it goes in the oven, and a second
    #: control with its own button would be a second trip through 162 rows.
    #:
    #: The oven boxes are built in `__init__`, one per product of the
    #: colorway, because the answer is per blank-and-colorway pair — see
    #: `FinishedProduct.oven_dyed`. A single box here would be the flag back
    #: on the recipe wearing a different hat: it could not say "oven on the
    #: yarns, microwave on the silk", and saving the row would flatten a
    #: mixed colorway to whatever the one box happened to hold.
    OVEN_PREFIX = "oven_"

    #: The dye-book amount that rides beside each slot, in ounces for a full
    #: book bath — five skeins of yarn. Stored as written; every reader
    #: divides by the basis to get the bath in front of it. See
    #: `scarves/dyeamounts.py`.
    AMOUNT_PREFIX = "oz"

    def __init__(self, *args, products=(), **kwargs):
        super().__init__(*args, **kwargs)
        # Held so `save` writes exactly the rows the form drew a box for. A
        # product added while the row sat open is simply not part of this
        # picture, which is better than a save that silently answers for it.
        self.products = list(products)
        for product in self.products:
            self.fields[f"{self.OVEN_PREFIX}{product.pk}"] = forms.BooleanField(
                required=False,
                label=product.raw_product.name,
            )
        queryset = picker_dyes()
        for i in range(1, self.SLOTS + 1):
            self.fields[f"dye{i}"] = forms.ModelChoiceField(
                queryset=queryset,
                required=False,
                label=f"Dye {i}",
                widget=DyeSelect,
            )
            # Beside its own dye, because the book has them side by side and
            # a column of amounts detached from the dyes is five things to
            # line up by eye — the same argument that put the swatch inside
            # the dye pill.
            #
            # Optional on purpose. The backlog is dyes with no recipe at all,
            # and requiring an amount to save a dye would stop the entry the
            # picker exists to keep moving.
            self.fields[f"{self.AMOUNT_PREFIX}{i}"] = forms.DecimalField(
                required=False,
                min_value=0,
                max_digits=5,
                # A tenth, because that is what the scale reads. A box that
                # takes hundredths asks for a figure nobody can weigh out.
                decimal_places=1,
                label=f"Ounces {i}",
                widget=forms.NumberInput(attrs={
                    "step": "0.1",
                    "min": "0",
                    "class": "ozbox",
                    "aria-label": f"Ounces of dye {i} per 5-skein bath",
                }),
            )

    @property
    def oven_fields(self):
        """`(product, bound field)` per product, for a template to lay out."""
        return [
            (product, self[f"{self.OVEN_PREFIX}{product.pk}"])
            for product in self.products
        ]

    @property
    def dye_fields(self):
        """Just the dye slots, for a template that wants to lay them out.

        `{% for field in form %}` renders *every* field, and `oven_dyed` is a
        declared attribute while the slots are added in `__init__` — so Django
        orders it first and the row came out with a stray checkbox in front of
        the dye boxes, the same field a second time. Two inputs sharing one
        name is worse than untidy: unticking the visible one while the stray
        stays ticked still posts `on`.
        """
        return [self[f"dye{i}"] for i in range(1, self.SLOTS + 1)]

    @property
    def dye_slots(self):
        """`(dye field, amount field)` per slot, so the two stay paired.

        The template lays them out together; separating them into two loops
        is how the third amount comes to sit under the fourth dye.
        """
        return [
            (self[f"dye{i}"], self[f"{self.AMOUNT_PREFIX}{i}"])
            for i in range(1, self.SLOTS + 1)
        ]

    def clean(self):
        cleaned = super().clean()
        chosen = [cleaned.get(f"dye{i}") for i in range(1, self.SLOTS + 1)]
        picked = [d.pk for d in chosen if d]
        if len(picked) != len(set(picked)):
            self.add_error(None, "Please don't select the same dye more than once.")
        return cleaned

    def selected_dyes(self):
        """The chosen dyes in slot order, gaps removed."""
        return [dye for dye, _oz in self.selected_rows()]

    def selected_rows(self):
        """`(dye, ounces or None)` in slot order, empty slots removed.

        An amount typed into a slot with no dye in it is dropped with the
        slot, which is the only sensible reading: ounces of nothing.
        """
        rows = []
        for i in range(1, self.SLOTS + 1):
            dye = self.cleaned_data.get(f"dye{i}")
            if dye:
                rows.append((dye, self.cleaned_data.get(f"{self.AMOUNT_PREFIX}{i}")))
        return rows

    @transaction.atomic
    def save(self, recipe):
        """Replace the recipe's dyes with the selected ones.

        Replace rather than merge, matching QuickRecipeRowForm: it makes the
        form a straightforward picture of the final state, and lets a row be
        cleared by emptying every slot.
        """
        # Replaced wholesale, so the amount has to come back through the
        # form with its dye — a row rebuilt without one would silently drop
        # the ounces every time somebody saved a band or an oven box.
        RecipeDye.objects.filter(recipe=recipe).delete()
        for order, (dye, ounces) in enumerate(self.selected_rows(), start=1):
            RecipeDye.objects.create(
                recipe=recipe, dye=dye, order=order, book_ounces=ounces,
            )

        # An unticked checkbox posts nothing, so each box reads as False on a
        # row that was never touched — which is correct here only because
        # every box is rendered with its product's current value as `initial`
        # and the whole row is always submitted together. Same bargain the dye
        # slots make: the form is a picture of the final state, not a diff.
        #
        # Written per product with `save(update_fields=...)` rather than one
        # queryset update, because `FinishedProduct.save` is what keeps a
        # passthrough's mirrored count honest — the rule in
        # `docs/claude/stock.md` about never reaching for `update()` on these
        # rows.
        for product in self.products:
            want = bool(self.cleaned_data.get(f"{self.OVEN_PREFIX}{product.pk}"))
            if product.oven_dyed != want:
                product.oven_dyed = want
                product.save(update_fields=["oven_dyed"])
        return recipe


class QuickRecipeRowForm(forms.Form):
    # The slots a row offers. Named once so the template can loop over them
    # and clean()/save() can read them in order — adding a fifth dye is a
    # matter of adding the field and this name.
    DYE_FIELDS = ("dye1", "dye2", "dye3", "dye4")

    name = forms.CharField(max_length=150, required=False)  # allow blank rows
    # Every dye, not just the in-stock ones — see `picker_dyes`. This page
    # used to offer in-stock only, which was harmless while the list was
    # take-it-or-leave-it and becomes a duplicate factory now that a missing
    # dye can be typed in.
    dye1 = forms.ModelChoiceField(queryset=picker_dyes(), required=False, widget=DyeSelect)
    dye2 = forms.ModelChoiceField(queryset=picker_dyes(), required=False, widget=DyeSelect)
    dye3 = forms.ModelChoiceField(queryset=picker_dyes(), required=False, widget=DyeSelect)
    dye4 = forms.ModelChoiceField(queryset=picker_dyes(), required=False, widget=DyeSelect)

    @property
    def dye_fields(self):
        """The bound dye fields, in slot order, for the template to render."""
        return [self[name] for name in self.DYE_FIELDS]

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get("name") or "").strip()
        dyes = [cleaned.get(f) for f in self.DYE_FIELDS]

        # If totally empty row: OK (skip)
        if not name and not any(dyes):
            cleaned["_skip"] = True
            return cleaned

        # If partially filled: require name
        if not name:
            self.add_error("name", "Please enter a recipe name for this row.")
            return cleaned

        # Optional: prevent duplicate dyes in the same recipe row
        chosen = [d.pk for d in dyes if d]
        if len(chosen) != len(set(chosen)):
            self.add_error(None, "Please don’t select the same dye more than once in a recipe.")
        return cleaned

    @transaction.atomic
    def save(self):
        if self.cleaned_data.get("_skip"):
            return None

        name = self.cleaned_data["name"].strip()
        recipe, _created = Recipe.objects.get_or_create(name=name, defaults={"is_active": True})

        # Replace existing dyes each save (predictable + fast entry)
        RecipeDye.objects.filter(recipe=recipe).delete()

        dyes = [self.cleaned_data.get(f) for f in self.DYE_FIELDS]
        order = 1
        for dye in dyes:
            if not dye:
                continue

            # --- IMPORTANT: adjust this block to match your RecipeDye fields ---
            # Variant A (common): RecipeDye has (recipe, dye, order) only
            RecipeDye.objects.create(recipe=recipe, dye=dye, order=order)

            # Variant B (also common): RecipeDye requires ratio/parts (e.g. parts=IntegerField)
            # RecipeDye.objects.create(recipe=recipe, dye=dye, order=order, parts=1)

            order += 1

        return recipe




class HoursForm(forms.Form):
    """The public hours form: who you are, your PIN, how long, which day.

    Four fields, one screen, no login. Everything it rejects, it rejects for
    a reason it can state — a form that silently accepts 96 hours or a shift
    next Tuesday costs more to unpick later than it saves now.

    Five fields now, because the form asks what the work was. It used to be
    booth hours only and it used to say so — which meant somebody paid by the
    hour to dye had a form in front of them that told them their work did not
    belong on it. The rule the restriction was protecting (don't collect a
    second kind of work without deciding what it means for payroll) held; the
    answer arrived, and it is that both kinds are paid the same hourly rate.
    So the kind is recorded and the total still adds up to one payable number.
    """

    #: The picker runs in quarter-hours, which is how payroll rounds anyway
    #: and how people already describe a shift ("half nine to six, half hour
    #: for lunch"). Fine enough to be honest, coarse enough to stay one tap.
    STEP = Decimal("0.25")
    MIN_HOURS = Decimal("0.25")
    MAX_HOURS = Decimal("14")

    #: How far back the form will take a day. Long enough to catch up after a
    #: weekend that got away, short enough that a month-old figure has to come
    #: through a person instead of being typed from memory.
    MAX_BACKDATE_DAYS = 21

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),   # set in __init__, see below
        empty_label="— choose your name —",
        label="Your name",
    )
    pin = forms.CharField(
        max_length=4,
        label="Your PIN",
        widget=forms.TextInput(attrs={
            # inputmode + pattern get the numeric keypad on a phone without
            # type="number", which brings spinner arrows and strips leading
            # zeros — and half these PINs start with one.
            "inputmode": "numeric",
            "pattern": "[0-9]*",
            "autocomplete": "off",
            "placeholder": "····",
        }),
    )
    # Two radios rather than a dropdown: this is a two-option question on a
    # phone, and a <select> costs a tap to open and a tap to choose where
    # radios cost one. It is also the only field whose current value has to
    # be readable without opening anything — the wrong one submitted is the
    # mistake this field can make.
    kind = forms.ChoiceField(
        choices=TimeEntry.KIND_CHOICES,
        initial=TimeEntry.BOOTH,
        label="What were you doing?",
        widget=forms.RadioSelect(),
    )
    # A decimal validated against the rule, rendered as a picker — not a
    # ChoiceField. A ChoiceField compares the submitted *string* to the option
    # strings, so "9.5" and "9.50" are different answers and only one of them
    # validates. The constraint is "a quarter-hour between 15 minutes and 14
    # hours"; the dropdown is how it's asked, not what it means.
    hours = forms.DecimalField(
        max_digits=4,
        decimal_places=2,
        label="Hours worked",
        widget=forms.Select(),   # choices set in __init__
    )
    work_date = forms.DateField(
        label="Day worked",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        # Popped before super(), which would otherwise reject the kwarg. Passing
        # `today` in is what lets the tests pin a date instead of racing one.
        self.today = kwargs.pop("today", None) or timezone.localdate()
        super().__init__(*args, **kwargs)

        # Only active rows, and evaluated per-instance rather than at import
        # time so somebody hired this morning is on the list without a redeploy.
        self.fields["employee"].queryset = Employee.objects.active()
        self.fields["hours"].widget.choices = self.hour_choices()
        self.fields["work_date"].widget.attrs.update({
            "min": (self.today - timedelta(days=self.MAX_BACKDATE_DAYS)).isoformat(),
            "max": self.today.isoformat(),
        })

    @classmethod
    def hour_choices(cls):
        """Quarter-hour options from 15 minutes to a very long day."""
        choices = [("", "— how long? —")]
        value = cls.MIN_HOURS
        while value <= cls.MAX_HOURS:
            choices.append((cls.canonical(value), cls.describe_hours(value)))
            value += cls.STEP
        return choices

    @staticmethod
    def canonical(value: Decimal) -> str:
        """`Decimal('9.50')` -> `'9.5'`; `Decimal('1.00')` -> `'1'`.

        `:f` rather than plain `str()` on the normalised value, which would
        render 10 as `1E+1` and put a hole in the middle of the picker.
        """
        return f"{value.normalize():f}"

    @classmethod
    def describe_hours(cls, value: Decimal) -> str:
        """`Decimal('9.50')` -> `'9.5 hours'`; `Decimal('1.00')` -> `'1 hour'`."""
        return f"{cls.canonical(value)} hour{'' if value == 1 else 's'}"

    def clean_hours(self):
        """The rule the picker is a rendering of.

        Checked here rather than trusted to the dropdown, because a hand-built
        POST doesn't go near the dropdown.
        """
        hours = self.cleaned_data["hours"]
        if hours < self.MIN_HOURS or hours > self.MAX_HOURS:
            raise forms.ValidationError(
                f"Hours have to be between {self.canonical(self.MIN_HOURS)} and "
                f"{self.canonical(self.MAX_HOURS)}. If that's really the shift, "
                f"ask a manager to enter it."
            )
        if hours % self.STEP != 0:
            raise forms.ValidationError("Round to the nearest quarter hour.")
        return hours

    def clean_pin(self):
        pin = (self.cleaned_data.get("pin") or "").strip()
        if not pin.isdigit() or len(pin) != 4:
            raise forms.ValidationError("Your PIN is four digits.")
        return pin

    def clean_work_date(self):
        work_date = self.cleaned_data["work_date"]
        if work_date > self.today:
            raise forms.ValidationError(
                "That day hasn't happened yet — hours go in after the shift."
            )
        oldest = self.today - timedelta(days=self.MAX_BACKDATE_DAYS)
        if work_date < oldest:
            raise forms.ValidationError(
                f"That's more than {self.MAX_BACKDATE_DAYS} days ago. Ask a "
                f"manager to add it for you."
            )
        return work_date

    def clean(self):
        cleaned = super().clean()
        employee = cleaned.get("employee")
        pin = cleaned.get("pin")

        # Only checked when both arrived intact; otherwise the field errors
        # already say what's wrong and a PIN error on top is just noise.
        if employee and pin and pin != employee.pin:
            self.add_error("pin", "That PIN doesn't match the name you picked.")

        return cleaned


class PickedBathsField(forms.Field):
    """Hand-picked baths, parsed from repeated `items=<pk>:<baths>`.

    Same wire format as the label page's picked items, and `parse_label_items`
    reads both — the shape is "a product and a count", and having two parsers
    for one shape is how they drift.

    What the count *means* differs, and that is the thing to hold onto: on the
    label page it is stickers, here it is **baths**. Two baths of a colorway
    whose blank yields four is eight scarves, and the sheet says `4 ×` twice
    rather than `8 ×` once, because a row is a bath and a bath is what
    somebody physically does.
    """

    widget = forms.MultipleHiddenInput

    #: Per colorway, and a **typo guard rather than a policy** — it exists to
    #: catch a slip, not to have an opinion about the session.
    #:
    #: Ten because two digits in this box is almost always a number somebody
    #: meant to delete half of: a stray keystroke turns 2 into 12 or 5 into
    #: 15, and both read as perfectly ordinary plans. That is a sharper test
    #: than "twenty is unusual", which describes the mistake without catching
    #: it — and nothing real is on the other side of the line. Even a full
    #: oven is fifteen trays of *different* colours; nobody wants fifteen
    #: baths of one.
    MAX_PER_ITEM = 10

    def clean(self, value):
        if not value:
            return []

        wanted = parse_label_items(value)
        if not wanted:
            raise forms.ValidationError("Couldn't read the picked baths.")
        if any(not 1 <= n <= self.MAX_PER_ITEM for n in wanted.values()):
            raise forms.ValidationError(
                f"Baths per colorway run from 1 to {self.MAX_PER_ITEM}."
            )

        found = {
            p.pk: p
            for p in FinishedProduct.objects.active().filter(pk__in=wanted)
            .select_related("raw_product", "recipe")
        }
        if set(wanted) - set(found):
            raise forms.ValidationError(
                "Some picked colorways are no longer active. Remove them and "
                "re-add."
            )
        # A passthrough has no recipe and a fancy veil isn't made in a bath.
        # Caught here rather than filtered out of the search, so the answer is
        # "that isn't dyed" rather than the product silently not existing.
        undyeable = [
            p.name for p in found.values()
            if p.recipe_id is None or not p.raw_product.made_in_a_dye_bath
        ]
        if undyeable:
            raise forms.ValidationError(
                f"{', '.join(sorted(undyeable))} isn't made in a dye bath, so "
                f"it can't go on a production sheet."
            )
        return [(found[pk], n) for pk, n in wanted.items()]


class HaventGotField(forms.ModelMultipleChoiceField):
    """A set of things somebody has ticked, read leniently from the URL.

    Lenient for the same reason `parse_label_items` is: this rides in the
    query string beside a hand-built list of baths, and a stale pk in a link
    somebody sent on — a blank retired since, a dye merged away — must not
    invalidate the whole form and take the list down with it. An id that
    names nothing is a tick that isn't there.
    """

    widget = forms.CheckboxSelectMultiple

    def clean(self, value):
        pks = [int(v) for v in (value or []) if str(v).isdigit()]
        return self.queryset.filter(pk__in=pks) if pks else self.queryset.none()


class ProductionSheetForm(forms.Form):
    """What goes on a printed production sheet.

    **Two ways to start, and then one list.** Either the app suggests baths
    from what is below par, or somebody says what they already know they are
    dyeing. Both produce the same thing — a list of colorways and bath counts
    — and from that point it is one editable list, because the two questions
    only differ in how the first draft got made.

    That is the whole shape, and the earlier version got it wrong by making
    them two *modes*: a suggested list you could only look at, and a picked
    list you could edit, with a read-only preview of one sitting underneath
    the editable copy of the other. A suggestion you cannot change is a
    suggestion you have to work around on paper.

    `items` wins when present. A suggestion seeds the list and the moment
    anything is edited the list is what the page is about — so an old
    `?baths=20` link still works and now comes back editable.
    """

    #: A day's dyeing, generously. High enough that nobody hits it planning a
    #: real session, low enough that a typo can't produce a hundred-page PDF.
    MAX_BATHS = 60

    baths = forms.IntegerField(
        min_value=1,
        max_value=MAX_BATHS,
        initial=20,
        required=False,
        label="How many baths?",
        help_text="Most urgent first — what a whole bath still leaves at or under par.",
    )
    items = PickedBathsField(required=False)
    oven = forms.BooleanField(
        required=False,
        label="This is an oven run",
        help_text=(
            "Some colorways are made in the oven rather than in a pot. Tick "
            "this and the sheet plans an oven session instead: only oven "
            "colorways, and planned to the fifteen trays the oven holds, "
            "because one heating costs the same whether it comes out full or "
            "not. One tray is one bath."
        ),
    )
    order = forms.ChoiceField(
        choices=[
            ("sold", "Best sellers first"),
            ("par", "Furthest below par first"),
        ],
        initial="sold",
        required=False,
        label="Which shortages first?",
        help_text=(
            "Sales are measured; par was never dialled in, so ordering by "
            "shortage ranks a session on a number nobody chose. This matches "
            "what production-needed lists, so asking for the first N baths "
            "gives the N you were just looking at."
        ),
    )
    category = forms.ModelChoiceField(
        queryset=RawProductCategory.objects.none(),   # set in __init__
        required=False,
        empty_label="Everything",
        label="Just one category?",
    )
    #: **A second par, tried on rather than written.** The list is ranked on
    #: what a colorway sold and filtered on a par that is the same number
    #: for nearly everything, so the best seller sits one above par and
    #: never reaches the list it would top. This measures every product
    #: against twice a day's sales plus one instead. Nothing is stored:
    #: `FinishedProduct.par` is untouched, and unticking it is the old way.
    demand_par = forms.BooleanField(
        required=False,
        label="Set par from sales",
        help_text=(
            "Instead of the stored par, judge each product against twice "
            "what it sells in a faire day, rounded up, plus one. Sales are "
            "this season's; the stored par is not changed. Ordered furthest "
            "below that par first, whatever is chosen above, and without "
            "the close's sold-out bath — the sales are already in the number."
        ),
    )
    include_overshoot = forms.BooleanField(
        required=False,
        label="Include ones a bath would take past par",
        help_text=(
            "Off, it only suggests products where a whole bath still lands at "
            "or under par. On, it also suggests the ones that are short by "
            "less than a bath — overshoot is a bath being a fixed size, not "
            "overproduction, and those shortages get rounded away next time "
            "the recipe is dyed anyway."
        ),
    )

    #: **What the shelf says today, and it is not stored anywhere.**
    #:
    #: Raw stock is an opening balance nothing recounts on its own, and a
    #: dye's `in_stock` flag is set in the admin and nowhere else, so the
    #: planner cannot look for holes in either and be right. The person in
    #: the dye room can see both, and this is where she says so.
    #:
    #: Deliberately **per sheet rather than remembered**. A stored "out of
    #: Fuchsia" has to be cleared to stop being true, and nothing would ever
    #: prompt for that — the colour would go on quietly not being suggested,
    #: with the sheets that came out looking entirely normal. Two ticks next
    #: week is the cheaper failure, and it rides in the URL like every other
    #: piece of state here, so a planned sheet is still a link somebody can
    #: send.
    without_blanks = HaventGotField(
        queryset=RawProduct.objects.none(),   # set in __init__
        required=False,
        label="Blanks you haven't got",
    )
    without_dyes = HaventGotField(
        queryset=Dye.objects.none(),          # set in __init__
        required=False,
        label="Dyes you're out of",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-instance, so a category added this morning is selectable
        # without a redeploy — same reasoning as the employee pickers.
        self.fields["category"].queryset = RawProductCategory.objects.order_by("name")
        # Only blanks that are dyed at all: a passthrough or a fancy veil was
        # never going to be suggested, so offering it as something to rule out
        # is a tick that can only do nothing.
        self.fields["without_blanks"].queryset = (
            RawProduct.objects.active().filter(made_in_a_dye_bath=True)
            .select_related("category")
            .order_by("category__name", "name")
        )
        # **Every dye a recipe actually uses, and no more.** The shelf holds
        # a hundred and thirty-two; the fifty-five that no live colorway
        # calls for cannot block a bath, so a box beside one is a tick that
        # can only do nothing — the same reason a passthrough is not offered
        # as a blank. The list does not depend on what has been suggested:
        # "out of J purple" is something she knows walking in, and a panel
        # that only filled itself in after a suggestion asked her to plan
        # before she could say what she hadn't got.
        self.fields["without_dyes"].queryset = (
            Dye.objects.filter(recipe_dyes__recipe__is_active=True)
            .distinct()
            .select_related("brand")
        )

        # **One page, one tick.** The oven is a different session — only oven
        # colorways, and planned to the box rather than to the work — but it
        # is the same job with the same list, the same editing and the same
        # three printed documents, so it is a checkbox rather than a second
        # picker to find and keep in step.
        #
        # Read off the raw data rather than `cleaned_data`, because the count
        # box below has to be relabelled before anything is validated.
        if self.is_bound and self.data.get("oven"):
            from . import production

            # The oven is planned *to* its capacity rather than to a number
            # somebody chose. Still editable, because a session with a reason
            # to run short is a session somebody has a reason for.
            self.fields["baths"].label = "How many trays?"
            self.fields["baths"].help_text = (
                f"The oven holds {production.OVEN_TRAYS}, and one tray is one "
                f"bath. Shortages first; anything left over can be topped up "
                f"below."
            )

    @property
    def is_oven_run(self) -> bool:
        """Whether this is an oven run, safe to read before validation.

        **Deliberately not named `oven`.** A property of that name sits later
        in the class body than the `oven = forms.BooleanField(...)` above it
        and simply overwrites it, so the metaclass never collects the field
        and `{{ form.oven }}` renders nothing — a page with no way to tick the
        thing on, while every behavioural test still passes because this
        reads `self.data` directly. That is exactly how it shipped once.
        """
        if self.is_bound:
            return bool(self.data.get("oven"))
        return False

    @property
    def asked_anything(self) -> bool:
        """Whether this form is a question at all, rather than a bare page.

        `add` counts. Clicking a search result on an empty page is a question
        — it is how the first row gets there — and leaving it out meant the
        page came back with the row on it and no total and no print button.
        """
        return bool(
            self.data.get("baths")
            or self.data.getlist("items")
            or self.data.get("add")
        )

    def _edited(self, product, current):
        """The bath count for one row, after any inline edit.

        The list rides as `items=<pk>:<n>` because that is what a remove link
        and the print POST need — one value naming both the row and its size.
        A number box cannot edit half of that, so each row also posts
        `qty-<pk>`, and this is where the two meet.

        Kept as an override rather than replacing `items` outright so the
        canonical list stays one thing: membership and order come from
        `items`, and this only ever changes a count.
        """
        typed = (self.data.get(f"qty-{product.pk}") or "").strip()
        if not typed.isdigit():
            return current
        return min(int(typed), PickedBathsField.MAX_PER_ITEM)

    def _added_product(self):
        """The colorway an "add" button just asked for, or `None`.

        A distinct parameter from `items` rather than another entry in it,
        because the two would fight: the list posts `items=<pk>:<n>` *and* a
        `qty-<pk>` box, so a second `items=<pk>:1` for something already on
        the list would be summed and then immediately overwritten by the
        box's older number. The add would look like it did nothing — which is
        exactly how this arrived.
        """
        raw = (self.data.get("add") or "").strip()
        if not raw.isdigit():
            return None
        return (
            FinishedProduct.objects.filter(pk=int(raw), is_active=True)
            .select_related("raw_product", "recipe")
            .first()
        )

    def clean(self):
        """The list, in the order the three inputs have to be applied.

        **Edits first, then the add.** They touch the same row when somebody
        clicks a colorway already on the list, and doing it the other way
        round lets the count box's older number overwrite the bump — which
        reads on screen as the add having done nothing at all.
        """
        cleaned = super().clean()
        picked = list(cleaned.get("items") or [])

        # 1. the count boxes. Zero is how a row is removed by typing rather
        #    than by the ✕, and it must not become a bath of nothing.
        picked = [(product, self._edited(product, n)) for product, n in picked]
        picked = [(product, n) for product, n in picked if n > 0]

        # 2. the add. Bump if it is already there, append if it isn't, which
        #    is what clicking the same colorway twice means. Order is kept so
        #    a list doesn't reshuffle under somebody.
        added = self._added_product()
        if added is not None:
            if added.recipe_id is None or not added.raw_product.made_in_a_dye_bath:
                self.add_error(
                    "items",
                    f"{added.name} isn't made in a dye bath, so it can't go "
                    f"on a production sheet.",
                )
            else:
                for index, (product, n) in enumerate(picked):
                    if product.pk == added.pk:
                        picked[index] = (product, n + 1)
                        break
                else:
                    picked.append((added, 1))

        if picked and sum(n for _, n in picked) > self.MAX_BATHS:
            self.add_error(
                "items",
                f"That is more than {self.MAX_BATHS} baths. A sheet is one "
                f"session's work.",
            )

        cleaned["items"] = picked
        return cleaned


def parse_label_items(raw_values):
    """`["12:3", "7:1", "12:2"]` -> `{12: 5, 7: 1}`.

    Lenient on purpose: it backs both validation and re-rendering the picked
    list after a failed submit, and losing someone's hand-built list because
    an unrelated field was wrong is worse than ignoring a malformed row.
    """
    wanted = {}
    for raw in raw_values or []:
        pk, _, qty = str(raw).partition(":")
        if not pk.isdigit():
            continue
        qty = int(qty) if qty.isdigit() else 1
        wanted[int(pk)] = wanted.get(int(pk), 0) + qty
    return wanted


class LabelItemsField(forms.Field):
    """A hand-picked list of items, parsed from repeated `items=<pk>:<qty>`.

    Lives in the query string like everything else on the label page, so a
    hand-built run stays a re-openable URL and survives the preview round
    trip. Adding the same product twice sums rather than replacing — that's
    what someone typing it twice means, and silently dropping the first entry
    would be a missing sticker nobody notices until the till.
    """

    widget = forms.MultipleHiddenInput

    def clean(self, value):
        if not value:
            return []

        wanted = parse_label_items(value)
        if not wanted:
            raise forms.ValidationError("Couldn't read the picked items.")
        if any(not 1 <= qty <= 99 for qty in wanted.values()):
            raise forms.ValidationError("Label counts per item run from 1 to 99.")

        found = {
            p.pk: p
            for p in FinishedProduct.objects.filter(pk__in=wanted)
            .select_related("raw_product", "recipe")
        }
        if set(wanted) - set(found):
            raise forms.ValidationError(
                "Some picked items no longer exist. Remove them and re-add."
            )
        return [(found[pk], qty) for pk, qty in wanted.items()]


class LabelRunForm(forms.Form):
    """What to print, how many, and where on the sheet to start.

    A GET form: the picker and the preview are the same page, so a run is a
    URL you can re-open, bookmark or hand to someone. Nothing here is
    remembered server-side — see the note in `scarves.labels` about why the
    browser holds the last cutoff rather than a table of past runs.
    """

    SINCE = "since"
    INVENTORY = "inventory"
    ITEMS = "items"
    DATASET_CHOICES = [
        # Named for what it counts. "Added to stock" was the label while this
        # also read recounts, and it is exactly the wording that made the
        # widening look reasonable.
        (SINCE, "Dyed since a date"),
        (INVENTORY, "Everything on hand"),
        (ITEMS, "Specific items I pick"),
    ]

    dataset = forms.ChoiceField(
        choices=DATASET_CHOICES,
        initial=SINCE,
        label="What to print",
    )
    style = forms.ChoiceField(
        choices=LABEL_STYLE_CHOICES,
        initial=LABEL_BARCODE,
        required=False,
        label="What goes on each sticker",
        help_text="Two independent runs over the same product set — print "
                  "either, or both. Barcodes still carry the SKU underneath "
                  "and are what lets a photo file itself against a product, "
                  "since that reads the bars. A recipe-name label is what you "
                  "can read while applying it, and the recipe is what gets "
                  "confused — nobody mixes up a rectangle and a half-circle.",
    )
    since = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Dyed on or after",
        help_text="Counts dye baths and nothing else — a recount at the "
                  "Sunday close or on the restock walk is stock that already "
                  "has stickers, and it used to be counted here. Stock "
                  "counted in for the first time is a job for "
                  "‘everything on hand’ or a picked list. Defaults "
                  "to the last time you printed from this browser.",
    )
    category = forms.ModelChoiceField(
        queryset=None, required=False, empty_label="All categories",
    )
    raw_products = forms.ModelMultipleChoiceField(
        queryset=None, required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Blanks",
        help_text="Tick to narrow to particular blanks. Nothing ticked means "
                  "all of them — it never means print nothing.",
    )
    include_zero = forms.BooleanField(
        required=False,
        label="Include products with none on hand",
        help_text="Off by default: with extras switched on, every dormant SKU "
                  "would otherwise use labels.",
    )
    items = LabelItemsField(required=False, label="Picked items")
    extra = forms.IntegerField(
        min_value=0, max_value=20, initial=0, required=False,
        label="Extra labels per product",
        help_text="Added to each product's count — 3 produced with 2 extra "
                  "prints 5. Leave at 0 for exact.",
    )
    stock = forms.ModelChoiceField(queryset=None, label="Label stock")
    x_offset_mm = forms.DecimalField(
        required=False, max_digits=5, decimal_places=2,
        min_value=Decimal("-10"), max_value=Decimal("10"),
        label="Nudge right (mm)",
        help_text="Overrides the stock's saved offset for this print only. "
                  "Blank uses the saved one.",
    )
    y_offset_mm = forms.DecimalField(
        required=False, max_digits=5, decimal_places=2,
        min_value=Decimal("-10"), max_value=Decimal("10"),
        label="Nudge up (mm)",
    )
    start_at = forms.IntegerField(
        min_value=1, initial=1,
        label="Start at label",
        help_text="Read it off the marker sticker on the part-used sheet. "
                  "A fresh sheet is 1.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["category"].queryset = RawProductCategory.objects.order_by("name")
        self.fields["raw_products"].queryset = RawProduct.objects.filter(
            is_active=True
        ).select_related("category").order_by("category__name", "name")

        stocks = LabelStock.objects.active()
        self.fields["stock"].queryset = stocks
        self.fields["stock"].empty_label = None
        first = stocks.first()
        if first:
            self.fields["stock"].initial = first.pk

    @property
    def items_value(self):
        """The picked list, for rendering the rows back onto the page.

        Falls back to a lenient parse when the form is invalid, so a mistake
        in some other field doesn't wipe a list somebody just built by hand.
        """
        if not self.is_bound:
            return []
        if self.is_valid():
            return self.cleaned_data.get("items") or []

        # Same extraction the field itself uses, rather than assuming a
        # QueryDict — a plain dict of lists has to behave identically or this
        # fallback quietly returns nothing exactly when it's needed.
        field = self.fields["items"]
        raw = field.widget.value_from_datadict(
            self.data, self.files, self.add_prefix("items")
        )
        if isinstance(raw, str):
            raw = [raw]
        wanted = parse_label_items(raw)
        found = {
            p.pk: p
            for p in FinishedProduct.objects.filter(pk__in=wanted)
            .select_related("raw_product", "recipe")
        }
        return [(found[pk], qty) for pk, qty in wanted.items() if pk in found]

    def clean_extra(self):
        return self.cleaned_data.get("extra") or 0

    def clean(self):
        cleaned = super().clean()

        dataset = cleaned.get("dataset")
        if dataset == self.SINCE and not cleaned.get("since"):
            self.add_error("since", "Pick the date to count production from.")
        if dataset == self.ITEMS and not cleaned.get("items"):
            self.add_error(
                "items", "Search for an item and add it before printing."
            )

        stock, start_at = cleaned.get("stock"), cleaned.get("start_at")
        if stock and start_at and start_at > stock.labels_per_sheet:
            self.add_error(
                "start_at",
                f"{stock.name} has {stock.labels_per_sheet} labels per sheet — "
                f"there is no label {start_at}. A used-up sheet is a fresh one "
                f"starting at 1.",
            )

        return cleaned


class ProducedSinceForm(forms.Form):
    """A date and, optionally, a kind of thing. That is the whole page.

    A GET form, so the view somebody is looking at is a URL she can bookmark
    or paste into a message — and the parameters are named after the fields
    they fill in, which is the rule the rest of the app's query-string state
    follows.

    `since` has a default rather than being required. A page that refuses to
    render until somebody picks a date is a page that answers "what does the
    app think I made" with a form, and the entire reason it exists is that
    the answer was not visible anywhere.
    """

    #: Long enough to cover a dye session plus the week or two before it gets
    #: typed up, short enough that the page opens on something readable. It
    #: is a starting view and not a claim about anything — the presets and the
    #: box are both right there.
    DEFAULT_DAYS = 30

    since = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Recorded on or after",
        help_text="Dates are the date recorded against the entry, which for "
                  "a kanban card typed up later is the date on the card.",
    )
    category = forms.ModelChoiceField(
        queryset=None, required=False, empty_label="Yarn and silk",
        label="Kind",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = RawProductCategory.objects.order_by("name")
        # So the box shows the window the page is actually using. An empty
        # box beside a month of entries is the form disagreeing with the
        # list, and the way that gets discovered is somebody pressing Show
        # on a blank field and wondering why nothing changed. Only affects
        # the unbound form — submitted data always wins.
        self.fields["since"].initial = self.default_since()

    @classmethod
    def default_since(cls):
        return timezone.localdate() - timedelta(days=cls.DEFAULT_DAYS)

    def clean_since(self):
        """Blank means the default window, never "everything ever".

        An unbounded page would pull in every kanban card back to 2024 and
        bury this season's dozen entries under them.
        """
        return self.cleaned_data.get("since") or self.default_since()


class BoothPhotoForm(forms.Form):
    """Send a photo in from the booth: who you are, the photo, and why.

    No login — identity is a name off a list plus the same four-digit PIN the
    hours form uses. That is deliberate and is not a security boundary: it
    stops somebody tapping the wrong name and stops idle mischief from whoever
    finds the URL. What it buys is that the crew can actually use the page,
    which a login would prevent for exactly the people it is for.

    The reason picks which half of the form matters. Both halves are always
    submitted; the view stores only the half that applies, so a report that
    changes reason mid-thought can't leave a stray sharing permission behind.
    """

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),   # set in __init__, as HoursForm does
        empty_label="— choose your name —",
        label="Your name",
    )
    pin = forms.CharField(
        max_length=4,
        label="Your PIN",
        widget=forms.TextInput(attrs={
            "inputmode": "numeric",
            "pattern": "[0-9]*",
            "autocomplete": "off",
            "placeholder": "····",
        }),
    )
    photo = forms.ImageField(
        label="The photo",
        # capture="environment" opens the rear camera straight away on a
        # phone, but still allows picking from the roll — which matters,
        # because half of these are taken first and sent later.
        widget=forms.ClearableFileInput(attrs={
            "accept": "image/*",
            "capture": "environment",
        }),
    )
    reason = forms.ChoiceField(
        choices=BoothPhoto.REASON_CHOICES,
        widget=forms.RadioSelect,
        label="What's this for?",
    )

    # --- share -------------------------------------------------------------
    share_website = forms.BooleanField(required=False, label="OK to share on the website")
    share_instagram = forms.BooleanField(required=False, label="OK to share on Instagram")
    people_in_photo = forms.BooleanField(
        required=False, label="Someone recognisable is in this photo"
    )
    people_agreed = forms.BooleanField(
        required=False, label="I asked them and they said yes"
    )
    caption = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    tag = forms.CharField(required=False, max_length=200, label="Anyone to tag")

    # --- unidentified sale -------------------------------------------------
    sold_at = forms.DateTimeField(
        required=False,
        label="When did it sell?",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
    )
    sku_prefix = forms.CharField(
        required=False,
        max_length=20,          # generous input, trimmed to six in clean
        label="First 6 of the barcode",
        help_text="The bit before the dash, if the tag is still on it. Leave "
                  "blank if you can't read it.",
        widget=forms.TextInput(attrs={"autocapitalize": "characters", "autocomplete": "off"}),
    )
    note = forms.CharField(required=False, max_length=200, label="Anything else worth saying")

    def __init__(self, *args, **kwargs):
        self.now = kwargs.pop("now", None) or timezone.localtime()
        # Who is already signed in, if anyone. The crew never are — this is
        # for the handful of people with a staff login, who should not be
        # asked to prove themselves twice on the same page.
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.active()

        self.signed_in_as = None
        if user is not None and user.is_authenticated:
            # A login is a stronger claim than a four-digit PIN, so asking for
            # the PIN on top of it buys nothing. The field goes rather than
            # being hidden, because a field that is present but not shown is
            # one a bad POST can still fill in.
            del self.fields["pin"]
            # Resolves to a row, creating one for a login that has never been
            # seen here — see crew.employee_for. This used to fall back to the
            # picker instead, on the grounds that an unlinked login genuinely
            # isn't any known employee and guessing would put the wrong name
            # on a permission. Still true, and the resolver doesn't guess: it
            # makes a row for the person who is actually signed in. What the
            # old behaviour produced was a signed-in person being shown their
            # colleagues' names and asked which one they were.
            self.signed_in_as = crew.employee_for(user)
            if self.signed_in_as is not None:
                del self.fields["employee"]

    def clean_pin(self):
        pin = (self.cleaned_data.get("pin") or "").strip()
        if not pin.isdigit() or len(pin) != 4:
            raise forms.ValidationError("Your PIN is four digits.")
        return pin

    def clean_sku_prefix(self):
        """Trim to the six characters that mean something.

        Run through the same slug the SKU was built with, so someone typing
        `infi-` or `Infi 6` lands on the same prefix the barcode carries.
        """
        return slug(self.cleaned_data.get("sku_prefix"))

    def clean_sold_at(self):
        sold_at = self.cleaned_data.get("sold_at")
        if sold_at is None:
            return None
        if timezone.is_naive(sold_at):
            sold_at = timezone.make_aware(sold_at)
        if sold_at > self.now + timedelta(minutes=5):
            raise forms.ValidationError(
                "That's in the future — when did the scarf actually sell?"
            )
        return sold_at

    def clean(self):
        cleaned = super().clean()

        # A signed-in staff member has no employee *field* to have filled in,
        # so the answer is put where every caller already looks for it rather
        # than making the view ask a second question.
        if self.signed_in_as is not None:
            cleaned["employee"] = self.signed_in_as

        # No PIN field means a signed-in staff member, already authenticated
        # by something stronger. With one, it is the crew and the rule is
        # unchanged.
        if "pin" in self.fields:
            employee = cleaned.get("employee")
            pin = cleaned.get("pin")
            if employee and pin and pin != employee.pin:
                self.add_error("pin", "That PIN doesn't match the name you picked.")

        reason = cleaned.get("reason")

        # The one rule worth refusing a submission over. The two destination
        # ticks record the *sender's* permission, and the sender cannot give
        # permission for the person in the picture — so if there is one, and
        # somewhere to post it, the answer has to be on the record.
        if reason == BoothPhoto.REASON_SHARE:
            posting = cleaned.get("share_website") or cleaned.get("share_instagram")
            if posting and cleaned.get("people_in_photo") and not cleaned.get("people_agreed"):
                self.add_error(
                    "people_agreed",
                    "Someone's in this photo and you've ticked somewhere to "
                    "post it — ask them first. If they'd rather not, untick "
                    "the sharing boxes and send it anyway; it just won't be "
                    "posted.",
                )

        if reason == BoothPhoto.REASON_UNIDENTIFIED and not cleaned.get("sold_at"):
            # Reported straight after the sale is the normal case, so the
            # moment the form was sent is the better default than nothing —
            # it is what the ±15 minute match is looking for.
            cleaned["sold_at"] = self.now

        return cleaned


class CloseStartForm(forms.Form):
    """Who is running tonight's close.

    The PIN is asked once, here, and the run's token carries every step after
    it. That is a different bargain from the hours and booth forms, which
    check on every POST — and the reason is what the two are protecting. Those
    attribute a claim to a person: hours that reach payroll, a permission to
    post someone's face. This attributes a stock correction, which is visible
    on the run's own page, reversible through the ordinary adjustment route,
    and made by whoever is holding the physical tags. Asking four digits four
    times while somebody packs a van in the dark is how the close stops
    happening, and a close that doesn't happen is the failure that matters.

    Same degradation as the booth form for the few people with a login: a
    staff session is a stronger claim than four digits, so the PIN goes. The
    name picker only goes when the app actually knows which employee that
    login is.
    """

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),   # set in __init__, as HoursForm does
        empty_label="— choose your name —",
        label="Your name",
    )
    pin = forms.CharField(
        max_length=4,
        label="Your PIN",
        widget=forms.TextInput(attrs={
            "inputmode": "numeric",
            "pattern": "[0-9]*",
            "autocomplete": "off",
            "placeholder": "····",
        }),
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.active()

        self.signed_in_as = None
        if user is not None and user.is_authenticated:
            # Removed, not hidden: a field that is present but invisible is
            # one a hand-built POST can still fill in.
            del self.fields["pin"]
            # Same resolver as the booth form, for the same reason.
            self.signed_in_as = crew.employee_for(user)
            if self.signed_in_as is not None:
                del self.fields["employee"]

    def clean_pin(self):
        pin = (self.cleaned_data.get("pin") or "").strip()
        if not pin.isdigit() or len(pin) != 4:
            raise forms.ValidationError("Your PIN is four digits.")
        return pin

    def clean(self):
        cleaned = super().clean()
        if self.signed_in_as is not None:
            cleaned["employee"] = self.signed_in_as
        if "pin" in self.fields:
            employee = cleaned.get("employee")
            pin = cleaned.get("pin")
            if employee and pin and pin != employee.pin:
                self.add_error("pin", "That PIN doesn't match the name you picked.")
        return cleaned


class DisplayFixtureForm(forms.ModelForm):
    """What a board is called, and what it is for.

    Both live on the editor rather than only in the admin because they are
    the two things somebody discovers wrong *while looking at the grid* — the
    board is mislabelled, or the dropdown is offering the wrong blank's
    colorways — and sending them to a different screen to fix it is how a
    board keeps a name nobody meant.

    `raw_product` is a lens, never a restriction: it decides which colorways
    the dropdown offers first and which ones the board reports as having no
    home. A board carrying a stray from another blank stays valid, and the
    editor keeps that stray on the menu so saving can't quietly drop it.

    `capacity_per_position` is here for the same reason — a scarf rack holds
    one per spot and a pegboard two per hook, and discovering that while
    looking at the grid is the normal way to discover it. Rows and columns
    stay in the admin: those change when the furniture does, which is rare,
    and shrinking a board is worth doing somewhere more deliberate.
    """

    class Meta:
        model = DisplayFixture
        fields = ("name", "raw_product", "capacity_per_position")
        labels = {
            "name": "What this board is called",
            "raw_product": "The blank it carries",
            "capacity_per_position": "How many fit on one peg or spot",
        }
        help_texts = {
            "raw_product": (
                "Leave blank for a mixed board. Setting it scopes the "
                "dropdowns and turns on the 'colorways with no home' report."
            ),
            "capacity_per_position": (
                "Two-skein hooks on the yarn board; one per spot on a scarf "
                "rack. Changing it recalculates every colorway's display "
                "capacity on this board, which is what the Sunday close "
                "reads. It is a ceiling, never a production target — a bigger "
                "hook is somewhere to put stock, not a reason to make more."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["raw_product"].queryset = RawProduct.objects.filter(
            is_active=True
        ).order_by("name")
        self.fields["raw_product"].empty_label = "— mixed board —"


class RestockPassForm(CloseStartForm):
    """Who is making the promise that the display was filled.

    Same name-and-PIN pair as the close, and the same degradation for a staff
    login, because it is the same crew on the same phones. What differs is
    what the signature is *for*: the close attributes a stock correction,
    this attributes a completed task. "The board was full at four, and Sam
    says so" is the whole output of a restock pass, and it is worth nothing
    without the name on it.
    """


class CloseCountFormBase(forms.Form):
    """Shared cleaning for the per-row count fields built below.

    Two controls per row and one number out of them. The buttons cover the
    whole of the ordinary case — a tag in hand means the bag is empty, so the
    answer cannot exceed what the display holds — and the box is there for the
    rows where it can: no tag, display refilled, and some left over.
    """

    #: Row pks this instance was built for, filled in by the factory.
    row_slots = {}

    def counts(self):
        """`{row_pk: total}` for every row somebody actually answered.

        Rows nobody has reached yet are simply absent, which is what keeps a
        half-finished close half-finished instead of recording zeros for the
        part that hasn't been counted.
        """
        out = {}
        for pk in self.row_slots:
            picked = self.cleaned_data.get(f"counted_{pk}")
            typed = self.cleaned_data.get(f"more_{pk}")
            if picked and picked != "more":
                out[pk] = int(picked)
            elif typed is not None:
                # Reached either by tapping "more" or by typing in the box
                # without tapping anything. The second is a thumb on a phone
                # and means exactly what it looks like, so it is taken rather
                # than rejected for the missing tap.
                out[pk] = typed
        return out

    def clean(self):
        cleaned = super().clean()
        for pk in self.row_slots:
            if cleaned.get(f"counted_{pk}") == "more" and cleaned.get(f"more_{pk}") is None:
                self.add_error(
                    f"more_{pk}",
                    "Tapped 'more' — type how many there are altogether.",
                )
        return cleaned


def build_close_count_form_class(rows, form_id=None):
    """One count per unanswered row: how many of these are actually here.

    `form_id` puts an HTML `form=` attribute on every input, which is what
    lets a row live somewhere other than inside the form it submits with.
    The unpredicted-tag search swaps its new rows in beside the search box
    rather than at the far end of a twenty-row list, and this is what keeps
    them part of the one Save. It is set on the page's own rows too, where it
    names the form they are already inside and so does nothing — one shape of
    row, rendered by one partial, wherever it lands.

    **The count is the total** — everything on the display plus whatever is
    left in the bag once the display has been filled. One question for all
    three situations the close puts somebody in, because the physical act
    differs but the quantity being reported does not, and a form that asks
    two different questions depending on what is in your hand is a form
    somebody answers in the wrong box.

    Buttons rather than a keyboard, numbered up to what the display holds.
    That range is not decoration: holding the tag means the bag is empty, so
    the answer is bounded by the display, and a count that runs past the last
    button is itself the news that there was stock in the bag after all. The
    box handles that case and nothing else.

    Every field is optional, because a close gets interrupted. Somebody
    counts four products, the van is loaded, and the rest goes in ten minutes
    later — so a blank means "not yet", not zero, and only the rows with an
    answer are recorded. Required fields here would make the all-or-nothing
    submission the only one available, which on a phone in a field is the
    same as no submission.

    Zero is a real answer and is kept. It means the display is empty and the
    bag behind it is too, which is worth recording as the disagreement it
    usually is.
    """
    fields = {}
    slots_by_pk = {}
    owner = {"form": form_id} if form_id else {}
    for row in rows:
        slots = row.display_slots or 0
        slots_by_pk[row.pk] = slots
        fields[f"counted_{row.pk}"] = forms.ChoiceField(
            required=False,
            choices=[(str(n), str(n)) for n in range(slots + 1)] + [("more", "more…")],
            widget=forms.RadioSelect(attrs=dict(owner)),
            label=row.finished_product.name,
        )
        fields[f"more_{row.pk}"] = forms.IntegerField(
            required=False,
            min_value=0,
            max_value=9999,
            label="How many altogether",
            widget=forms.NumberInput(attrs={
                "inputmode": "numeric",
                "min": 0,
                "placeholder": "total",
                **owner,
            }),
        )
    fields["row_slots"] = slots_by_pk
    return type("CloseCountForm", (CloseCountFormBase,), fields)


class CrewHandbookForm(forms.Form):
    """Who is reading the handbook, so the right pass comes back.

    The name and PIN here are doing a different job from the ones on the
    hours and booth forms, and it is worth being plain about it: they are not
    guarding the passes. A pass is a barcode and a photograph, trivially
    faked by anyone who wanted one, and the people who can reach this page
    are the people who are getting a pass anyway. What the pair actually buys
    is knowing *whose* PDF to hand over, and the PIN stops the wrong name
    being tapped — which here means walking off with somebody else's face.

    So there is deliberately no attempt-throttle, unlike the hours form. The
    thing behind that lock is worth locking; this one isn't, and a lockout
    lands on somebody standing at the gate without their pass.
    """

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),   # set in __init__, as HoursForm does
        empty_label="— choose your name —",
        label="Your name",
    )
    pin = forms.CharField(
        max_length=4,
        label="Your PIN",
        widget=forms.TextInput(attrs={
            "inputmode": "numeric",
            "pattern": "[0-9]*",
            "autocomplete": "off",
            "placeholder": "····",
        }),
    )
    #: Required only on the submission that asks for the pass — see `clean`.
    #: Unlocking the page to read it asks for nothing but the name and PIN,
    #: because ticking "I've read this" before reading it is the one order
    #: the box can't be asked in.
    read_it = forms.BooleanField(
        required=False,
        label="I've read this page",
    )

    def __init__(self, *args, **kwargs):
        # Whether this POST is the "Give me my pass" button rather than the
        # unlock. Popped before super(), which would reject the kwarg.
        self.wants_pass = kwargs.pop("wants_pass", False)
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        # Evaluated per-instance so somebody added this morning is on the
        # list without a redeploy. Rows with no PIN are left off: those are
        # the ones that sign in through Django, and picking one is a dead end
        # because `clean_pin` can never be satisfied for a blank PIN.
        self.fields["employee"].queryset = (
            Employee.objects.active().exclude(pin="")
        )

        self.signed_in_as = None
        if user is not None and user.is_authenticated:
            # A login outranks a four-digit PIN, so asking for the PIN on top
            # of it buys nothing. Both fields *go* rather than being hidden:
            # a field that is present but not shown is one a hand-built POST
            # can still fill in, and here that means handing over somebody
            # else's pass.
            del self.fields["pin"]
            self.signed_in_as = crew.employee_for(user)
            if self.signed_in_as is not None:
                del self.fields["employee"]

    def clean_pin(self):
        pin = (self.cleaned_data.get("pin") or "").strip()
        if not pin.isdigit() or len(pin) != 4:
            raise forms.ValidationError("Your PIN is four digits.")
        return pin

    def clean(self):
        cleaned = super().clean()
        # One of the two answered: the picker, or the login. Normalising here
        # means the view reads `cleaned_data["employee"]` either way and never
        # has to know which door somebody came through.
        employee = cleaned.get("employee") or self.signed_in_as
        cleaned["employee"] = employee

        pin = cleaned.get("pin")
        if "pin" in self.fields and employee and pin and pin != employee.pin:
            self.add_error("pin", "That PIN doesn't match that name.")

        if self.wants_pass and not cleaned.get("read_it"):
            self.add_error(
                "read_it",
                "Tick the box to say you've read the page, then ask again.",
            )
        return cleaned


class RawProductForm(forms.ModelForm):
    """Making a blank, and changing what one is.

    **There was no door at all.** `RawProduct` is not registered in the admin
    and no page created one, so the only blanks that existed were the ones a
    migration or the Square sync put there. Adding the first new silk in a
    year meant a shell — and an invoice that turns up carrying something the
    catalogue has never heard of had nowhere to put it.

    **What is deliberately not on here, and why.**

    `number_on_hand` is an opening balance at creation and nothing
    afterwards. A blank that already exists gets counted on
    `private/raw-inventory/`, where the two columns say plainly which
    question is being answered — *received* is a delta, *counted* is a
    measurement — and a third box on a third page quietly writing the same
    number is exactly the second door that makes two of them disagree.

    `invoice_description` is written by confirming an invoice line, never
    typed, so it is `editable=False` on the model and absent here.
    `square_item_id` belongs to the sync: typing one by hand is how a
    variation ends up pointed at the wrong shelf, and the sync repairs
    nothing it did not write.

    `counted_at` is a record of somebody physically counting, and a form that
    could set it would be a form that could claim a count nobody did.

    The help text is the short version of what is on the model. The long
    arguments live there and on `docs/claude/stock.md`; this is a page for
    somebody adding a blank, not for somebody deciding whether the field
    should exist.
    """

    #: The flag's real question, asked as its real question.
    #:
    #: `made_in_a_dye_bath` is a *fancy* marker and nothing else — the data
    #: says so plainly: every undyed yarn in the catalogue has it **True**,
    #: because those blanks are dyed into colorways and *separately* sold as
    #: they arrive, which is a colorway with no recipe rather than anything
    #: about the blank. Not one row has it False.
    #:
    #: Asked as "a dye bath can produce this" it reads as a question about
    #: dyeing, and the honest answer for a yarn sold undyed looks like *no* —
    #: which would quietly mark it fancy, drop it off every production list
    #: and route baths of it to a blank that does not exist. Two named
    #: origins cannot be misread that way: nobody calls Superwash Merino
    #: Zebra DK "made here from another blank".
    ORIGIN_BOUGHT = "True"
    ORIGIN_MADE_HERE = "False"

    made_in_a_dye_bath = forms.TypedChoiceField(
        label="Where it comes from",
        coerce=lambda value: value == "True",
        empty_value=True,
        choices=(
            (ORIGIN_BOUGHT,
             "Bought from a supplier — it arrives on a delivery, and dye "
             "baths make colorways of it"),
            (ORIGIN_MADE_HERE,
             "Made here from another blank — a bought one with extra work "
             "added, like a fancy veil"),
        ),
        widget=forms.RadioSelect,
        initial=ORIGIN_BOUGHT,
        help_text=(
            "Nearly everything is bought. “Made here” is for a blank no "
            "supplier sells and no dye bath can produce, which today means "
            "the fancy veils — an already-dyed scarf with line work added. "
            "It drops off every production list, because sending somebody to "
            "the dye room for one asks for a thing that isn't made there.\n\n"
            "Selling something undyed, exactly as it arrives, is not this. "
            "That is a colorway with no recipe, and the blank stays bought "
            "from a supplier — which is how every undyed yarn is set."
        ),
    )

    has_fancy_version = forms.BooleanField(
        required=False,
        label="It also comes in a fancy version",
        help_text=(
            "Ticking this makes the fancy blank and links the two. There is "
            "nothing to pick from because the fancy version of this blank is "
            "a thing that only exists to be that — one to one, since a veil "
            "cannot become a fancy shawl.\n\n"
            "Costs nothing to be wrong: a fancy blank nobody sells has par "
            "0, no stock and no colorways, appears on no production list "
            "(nothing can dye it) and asks for no order (nothing buys it). "
            "Unticking it later does not delete it — retire it instead."
        ),
    )

    sold_undyed = forms.BooleanField(
        required=False,
        label="Also sold exactly as it arrives, undyed",
        help_text=(
            "Makes the sellable row — one physical pile, two rows: this "
            "blank is the pile, and a colorway with no recipe is what Square "
            "sells. The count is mirrored between them, so moving a skein "
            "from one to the other changes nothing.\n\n"
            "It is priced at the suggested price above. Leave that empty and "
            "it lands at $1.00, conspicuously, so somebody fixes it before it "
            "reaches a till — zero would sync happily and ring up free."
        ),
    )

    colorways = forms.ModelMultipleChoiceField(
        queryset=Recipe.objects.none(),
        required=False,
        widget=RecipeCheckboxes,
        label="Make it in these colourways",
        # **How to work the control comes first, and nearly alone.** Help
        # text on a field is operating instructions; the argument for why the
        # field exists belongs in `scarves/blanks.py` and
        # `docs/claude/stock.md`, where somebody goes looking for an
        # argument. A multi-select whose reader cannot discover ⌘-click is a
        # control that can only pick one thing, and three paragraphs of
        # reasoning above it do not help with that — they bury it.
        help_text=(
            "Each tick becomes one product, “{blank} - {colourway}”, at the "
            "par and display defaults below. Add more any time — one this "
            "blank already has is skipped, not duplicated. Oven-dyed is left "
            "off on all of them; set it per colourway when you know."
        ),
    )

    opening_count = forms.IntegerField(
        required=False,
        min_value=0,
        label="How many are on the shelf now",
        help_text=(
            "The opening balance, counted once. Blank means none. After this "
            "the shelf is only ever changed by a delivery, a dye bath or a "
            "count on the Raw Inventory page."
        ),
    )

    class Meta:
        model = RawProduct
        fields = (
            "name", "category", "is_active",
            "price", "suggested_price", "supplier", "order_url", "sku",
            "made_in_a_dye_bath", "number_per_dye_bath",
            "fancy_counterpart", "fancying_cost",
            "par_level", "finished_par_default", "display_slots_default",
            "catalog_group", "notes",
        )
        labels = {
            "name": "What it is called",
            "category": "Which table it sits on",
            "is_active": "Still bought and used",
            "price": "What one costs you",
            "suggested_price": "What a finished one sells for",
            "order_url": "The pages you reorder it from",
            "sku": "Supplier's item number",
            "made_in_a_dye_bath": "A dye bath can produce this",
            "number_per_dye_bath": "How many go in one bath",
            "fancy_counterpart": "Its fancy version",
            "fancying_cost": "What the line work costs, per unit (made-here blanks only)",
            "par_level": "Keep at least this many undyed",
            "finished_par_default": "Par given to a new colorway of this",
            "display_slots_default": "Display spots a new colorway gets",
            "catalog_group": "Sell it under a shared Square item",
            "notes": "Anything else",
        }
        help_texts = {
            "price": (
                "Replacement cost — what one would cost to buy again today. "
                "An invoice overwrites this, so it is usually enough to get "
                "it roughly right here."
            ),
            "suggested_price": (
                "Leave empty if it is not decided. Empty and zero are "
                "different answers, and the stock valuation says which."
            ),
            "supplier": "Who you buy it from, which is not the same as where.",
            "order_url": (
                "The product page for this exact blank, not the shop's front "
                "page. One per line if you buy it from more than one listing — "
                "the first is the one the reorder column links to."
            ),
            "number_per_dye_bath": "Five skeins, four scarves — whatever fits the pot.",
            "fancy_counterpart": (
                "Set this on the plain blank, pointing at the fancy one. It "
                "lets a bath of five be reported as four plain and one fancy."
            ),
            "fancying_cost": (
                "Only on a fancy blank. Leave empty on everything you buy: a "
                "fancy veil has no supplier, and its cost is the plain one "
                "plus this."
            ),
            "par_level": (
                "The floor that keeps the dye room working, not a season's "
                "requirement. 0 means no floor."
            ),
            "finished_par_default": (
                "Only applies when a colorway is created. Changing it never "
                "rewrites the par of one that already exists."
            ),
            "display_slots_default": (
                "Display capacity, which is never a production target — a "
                "bigger hook is somewhere to put stock, not a reason to make "
                "more."
            ),
            "catalog_group": (
                "Blank for everything dyed, which is the normal case. Set it "
                "for something sold exactly as it arrives, where the Square "
                "item is the group and this blank is one of its variations."
            ),
        }

        widgets = {
            "order_url": forms.Textarea(attrs={"rows": 3, "placeholder": "https://…"}),
        }

    def clean_order_url(self):
        """Every line has to be a link, and the offending one is named.

        A textarea will take anything, and a line that is not a URL renders
        as a dead link on the reorder column — which looks like a working
        page until somebody clicks it on the day they need to order. Blank
        lines and stray spaces are dropped rather than refused, because
        pasting four links out of a browser produces both.
        """
        lines = RawProduct.split_order_urls(self.cleaned_data.get("order_url"))
        validator = URLValidator()
        for line in lines:
            try:
                validator(line)
            except ValidationError:
                raise ValidationError(
                    f"“{line}” isn't a link. It needs the https:// on the front."
                )
        return "\n".join(lines)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = RawProductCategory.objects.order_by("name")
        self.fields["supplier"].queryset = Supplier.objects.filter(
            is_active=True
        ).order_by("name")
        self.fields["supplier"].empty_label = "— nobody recorded —"
        self.fields["catalog_group"].queryset = CatalogGroup.objects.order_by("name")
        self.fields["catalog_group"].empty_label = "— its own Square item —"
        self.fields["price"].required = False

        # **The fancy pairing, and the order it has to be done in.** The
        # dropdown can only offer blanks that are already marked as made
        # here, so the fancy one exists first and the plain one is pointed at
        # it afterwards. That is not guessable from an empty select, and an
        # empty select reads as "this feature is broken" — so when there are
        # none, the field says what to do instead of offering nothing.
        fancies = RawProduct.objects.filter(made_in_a_dye_bath=False).order_by("name")
        if self.instance.pk:
            fancies = fancies.exclude(pk=self.instance.pk)
        self.fields["fancy_counterpart"].queryset = fancies
        self.fields["fancy_counterpart"].empty_label = "— it has no fancy version —"
        # A fancy blank has no fancy version of its own: a veil becomes a
        # fancy veil, and a fancy veil becomes nothing.
        if self.instance.pk and not self.instance.made_in_a_dye_bath:
            del self.fields["fancy_counterpart"]
            del self.fields["has_fancy_version"]

        # **The dropdown is the rare path now, and only offered when it can
        # do something.** Making the fancy blank is the ordinary answer;
        # pointing at one that already exists is for repairing a link, and an
        # empty select offering nothing is what read as "broken".
        if "fancy_counterpart" in self.fields:
            unlinked = fancies.filter(plain_counterparts__isnull=True)
            if self.instance.pk and self.instance.fancy_counterpart_id:
                self.initial["has_fancy_version"] = True
            if not unlinked.exists():
                del self.fields["fancy_counterpart"]
            else:
                self.fields["fancy_counterpart"].queryset = unlinked
                self.fields["fancy_counterpart"].help_text = (
                    "Only needed to point at a fancy blank that already "
                    "exists and isn't linked to anything. Ticking the box "
                    "above makes a new one instead."
                )
                self.fields["fancy_counterpart"].disabled = False

        # Only live colourways: a retired one is a colour somebody decided to
        # stop making, and offering it here is the dye-room-sent-after-a-
        # retired-recipe failure with a new door onto it.
        # Prefetched, because every row draws its dyes: 162 colourways is
        # 162 queries without it and two with.
        self.fields["colorways"].queryset = (
            Recipe.objects.active()
            .prefetch_related("recipe_dyes__dye")
            .order_by("name")
        )
        if self.instance.pk:
            self.fields["colorways"].help_text += (
                f"\n\n{self.instance.finished_products.active().count()} "
                f"already exist."
            )

        if self.instance.pk:
            self.initial["sold_undyed"] = FinishedProduct.objects.filter(
                raw_product=self.instance, recipe__isnull=True
            ).exists()

        if self.instance.pk:
            # The shelf is not editable here — see the class docstring. The
            # field is dropped rather than shown disabled, because a greyed
            # box still reads as "this is where that is changed".
            del self.fields["opening_count"]

    def clean_price(self):
        """An empty cost is zero, and the page says so rather than refusing.

        "I don't know what this costs yet" is most of a first pass, and
        `price` is not nullable — so the honest thing is to take the gap and
        show the row as *not costed* everywhere it matters, which is what the
        reorder page already does.
        """
        price = self.cleaned_data.get("price")
        return price if price is not None else Decimal("0")

    def clean(self):
        cleaned = super().clean()
        # "A blank can't be its own fancy version" is not checked here, and
        # deliberately: the queryset is the enforcement. It offers only
        # made-here blanks and excludes this one, and the field is dropped
        # altogether on a blank that is itself made here — so both halves of
        # the rule are structural. A `clean()` branch for it would be code
        # that can never run, which is worse than no check: it reads as the
        # thing keeping the rule true.
        if cleaned.get("fancying_cost") is not None and cleaned.get("made_in_a_dye_bath"):
            self.add_error(
                "fancying_cost",
                "Line work costs belong on a made-here blank. Set “where it "
                "comes from” to made here if that is what this is, or leave "
                "the cost empty — `price` is what a supplier charges, and a "
                "fancy veil has no supplier.",
            )
        return cleaned

    def save(self, commit=True):
        """Save the blank, and make whatever second rows it now implies.

        **Both extras are made here rather than asked for elsewhere**, which
        is the whole point: a blank that comes in a fancy version and a yarn
        sold undyed are each two rows, and until now the second row meant
        either a remembered ordering or a management command. Saying it on
        the blank is the decision; the row is bookkeeping.

        Neither is ever un-made. Unticking does not delete a fancy blank or a
        sellable row — those have history, and retiring is how something goes
        away.
        """
        blank = super().save(commit=False)
        opening = self.cleaned_data.get("opening_count")
        if opening is not None and not blank.pk:
            blank.number_on_hand = opening
        if not commit:
            return blank

        # `save()` rather than a queryset update: the `post_save` on this
        # model is what mirrors a passthrough's count onto its finished
        # row, and one physical pile gets exactly one row that counts it.
        blank.save()

        if self.cleaned_data.get("has_fancy_version"):
            blanks.ensure_fancy_counterpart(
                blank, self.cleaned_data.get("fancying_cost")
            )
        if self.cleaned_data.get("sold_undyed"):
            blanks.ensure_undyed_product(blank)
        # Stashed rather than messaged from here: a form has no request, and
        # the view is what tells somebody how many rows it just made.
        self.colorways_made, self.colorways_skipped = blanks.ensure_colorways(
            blank, self.cleaned_data.get("colorways") or []
        )
        return blank
