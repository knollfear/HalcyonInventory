# scarves/forms.py
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.db import transaction
from django.utils import timezone

from .models import (  # RecipeDye is the through model
    BoothPhoto,
    Dye,
    Employee,
    Recipe,
    RecipeDye,
    RawProduct,
    RawProductCategory,
)

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


class ProductionSheetForm(forms.Form):
    """What to put on a printed production sheet.

    Three questions, because a dyeing session is planned in about that much
    detail: how many baths are you good for, which table are you dyeing for,
    and do you want the ones a bath would take past par.
    """

    #: A day's dyeing, generously. High enough that nobody hits it planning a
    #: real session, low enough that a typo can't produce a hundred-page PDF.
    MAX_BATHS = 60

    baths = forms.IntegerField(
        min_value=1,
        max_value=MAX_BATHS,
        initial=20,
        label="How many baths?",
        help_text="One row per dye bath, most urgent first.",
    )
    category = forms.ModelChoiceField(
        queryset=RawProductCategory.objects.none(),   # set in __init__
        required=False,
        empty_label="Everything",
        label="Just one kind of blank?",
    )
    include_overshoot = forms.BooleanField(
        required=False,
        label="Include ones a bath would take past par",
        help_text=(
            "Off, the sheet only lists products where a whole bath still "
            "lands at or under par. On, it also lists the ones that are "
            "short by less than a bath — overshoot is a bath being a fixed "
            "size, not overproduction, and those shortages get rounded away "
            "next time the recipe is dyed anyway."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-instance, so a category added this morning is selectable
        # without a redeploy — same reasoning as the employee pickers.
        self.fields["category"].queryset = RawProductCategory.objects.order_by("name")


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
        from .models import FinishedProduct

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
        (SINCE, "Produced since a date"),
        (INVENTORY, "Everything on hand"),
        (ITEMS, "Specific items I pick"),
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

    @property
    def items_value(self):
        """The picked list, for rendering the rows back onto the page.

        Falls back to a lenient parse when the form is invalid, so a mistake
        in some other field doesn't wipe a list somebody just built by hand.
        """
        from .models import FinishedProduct

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
        self.fields["employee"].queryset = Employee.objects.filter(is_active=True)

        self.signed_in_as = None
        if user is not None and user.is_authenticated:
            # A login is a stronger claim than a four-digit PIN, so asking for
            # the PIN on top of it buys nothing. The field goes rather than
            # being hidden, because a field that is present but not shown is
            # one a bad POST can still fill in.
            del self.fields["pin"]
            self.signed_in_as = Employee.objects.filter(
                user=user, is_active=True
            ).first()
            # The name picker only goes when the app actually knows which
            # employee this login is. Unlinked, it genuinely doesn't — and
            # guessing would put somebody else's name on a permission.
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
        from .skus import slug
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
