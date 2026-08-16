# scarves/forms.py
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.db import transaction
from django.utils import timezone

from .models import Dye, Employee, Recipe, RecipeDye, RawProduct  # RecipeDye is your through model

class RecipeDyesForm(forms.Form):
    """Edit just the dye assignments of one existing recipe.

    Unlike QuickRecipeRowForm this never touches the recipe's name and never
    creates recipes — it is for filling in dyes on records that already exist.

    Deliberately offers *all* dyes, not just in_stock ones: this records what a
    recipe historically used, and a dye going out of stock must not make its
    recipes un-editable. (Every dye is in stock today, so this is a latent trap
    rather than a current bug.)
    """

    SLOTS = 5  # RecipeDye.order validates 1..5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Dye.objects.select_related("brand").order_by("brand__name", "name")
        for i in range(1, self.SLOTS + 1):
            self.fields[f"dye{i}"] = forms.ModelChoiceField(
                queryset=queryset, required=False, label=f"Dye {i}"
            )

    def clean(self):
        cleaned = super().clean()
        chosen = [cleaned.get(f"dye{i}") for i in range(1, self.SLOTS + 1)]
        picked = [d.pk for d in chosen if d]
        if len(picked) != len(set(picked)):
            self.add_error(None, "Please don't select the same dye more than once.")
        return cleaned

    def selected_dyes(self):
        """The chosen dyes in slot order, gaps removed."""
        return [
            d
            for d in (self.cleaned_data.get(f"dye{i}") for i in range(1, self.SLOTS + 1))
            if d
        ]

    @transaction.atomic
    def save(self, recipe):
        """Replace the recipe's dyes with the selected ones.

        Replace rather than merge, matching QuickRecipeRowForm: it makes the
        form a straightforward picture of the final state, and lets a row be
        cleared by emptying every slot.
        """
        RecipeDye.objects.filter(recipe=recipe).delete()
        for order, dye in enumerate(self.selected_dyes(), start=1):
            RecipeDye.objects.create(recipe=recipe, dye=dye, order=order)
        return recipe


class QuickRecipeRowForm(forms.Form):
    # The slots a row offers. Named once so the template can loop over them
    # and clean()/save() can read them in order — adding a fifth dye is a
    # matter of adding the field and this name.
    DYE_FIELDS = ("dye1", "dye2", "dye3", "dye4")

    name = forms.CharField(max_length=150, required=False)  # allow blank rows
    dye1 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)
    dye2 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)
    dye3 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)
    dye4 = forms.ModelChoiceField(queryset=Dye.objects.filter(in_stock=True), required=False)

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

    Booth hours only. There is deliberately no "what kind of work" field: see
    TimeEntry for why adding one is a payroll decision before it is a schema
    decision.
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
        self.fields["employee"].queryset = Employee.objects.filter(is_active=True)
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


class LabelRunForm(forms.Form):
    """What to print, how many, and where on the sheet to start.

    A GET form: the picker and the preview are the same page, so a run is a
    URL you can re-open, bookmark or hand to someone. Nothing here is
    remembered server-side — see the note in `scarves.labels` about why the
    browser holds the last cutoff rather than a table of past runs.
    """

    SINCE = "since"
    INVENTORY = "inventory"
    DATASET_CHOICES = [
        (SINCE, "Produced since a date"),
        (INVENTORY, "Everything on hand"),
    ]

    dataset = forms.ChoiceField(
        choices=DATASET_CHOICES,
        initial=SINCE,
        label="What to print",
    )
    since = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Produced on or after",
        help_text="Defaults to the last time you printed from this browser.",
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

        from .models import LabelStock, RawProduct, RawProductCategory

        self.fields["category"].queryset = RawProductCategory.objects.order_by("name")
        self.fields["raw_products"].queryset = RawProduct.objects.filter(
            is_active=True
        ).select_related("category").order_by("category__name", "name")

        stocks = LabelStock.objects.filter(is_active=True)
        self.fields["stock"].queryset = stocks
        self.fields["stock"].empty_label = None
        first = stocks.first()
        if first:
            self.fields["stock"].initial = first.pk

    def clean_extra(self):
        return self.cleaned_data.get("extra") or 0

    def clean(self):
        cleaned = super().clean()

        if cleaned.get("dataset") == self.SINCE and not cleaned.get("since"):
            self.add_error("since", "Pick the date to count production from.")

        stock, start_at = cleaned.get("stock"), cleaned.get("start_at")
        if stock and start_at and start_at > stock.labels_per_sheet:
            self.add_error(
                "start_at",
                f"{stock.name} has {stock.labels_per_sheet} labels per sheet — "
                f"there is no label {start_at}. A used-up sheet is a fresh one "
                f"starting at 1.",
            )

        return cleaned
