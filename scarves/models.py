from decimal import Decimal

from colorfield.fields import ColorField
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.core.validators import (
    MinValueValidator,
    MaxValueValidator,
    RegexValidator,
)
from django.utils import timezone

from .colorbands import BAND_CHOICES


class DyeBrand(models.Model):
    """
    Optional normalization of dye brands (Jacquard, Dharma, etc.)
    """
    name = models.CharField(max_length=100, unique=True)

    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Dye(models.Model):
    """
    Represents a dye color you keep in inventory.
    """
    name = models.CharField(max_length=100)
    hex_color = ColorField(default='#FF0000')
    brand = models.ForeignKey(
        DyeBrand,
        on_delete=models.PROTECT,
        related_name="dyes",
    )
    in_stock = models.BooleanField(default=True)
    # Optional extra fields
    sku = models.CharField(
        max_length=50,
        blank=True,
        help_text="Manufacturer or internal SKU",
    )
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ("name", "brand")
        ordering = ["brand__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.brand.name})"


class RawProductCategory(models.Model):
    """
    Silk, yarn, etc. Expandable without code changes.
    """
    name = models.CharField(max_length=50, unique=True)
    square_category_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Square CATEGORY catalog object ID.",
    )

    class Meta:
        verbose_name_plural = "Raw product categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class RawProduct(models.Model):
    """
    Represents an undyed base product: skein of yarn, silk scarf, etc.
    """
    name = models.CharField(max_length=150)
    category = models.ForeignKey(
        RawProductCategory,
        on_delete=models.PROTECT,
        related_name="raw_products",
    )
    price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Your cost for this raw product.",
    )
    suggested_price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        null=True,
        blank=True,
        help_text="Suggested retail price for finished items made from this raw product.",
    )
    number_per_dye_bath = models.PositiveIntegerField(
        default=4,
        help_text="How many of this raw item you normally dye in one bath.",
    )
    order_url = models.URLField(
        blank=True,
        help_text="Where you buy this from (supplier URL).",
    )
    number_on_hand = models.PositiveIntegerField(
        default=0,
        help_text="How many undyed units you currently have.",
    )
    sku = models.CharField(
        max_length=50,
        blank=True,
        help_text="Your internal SKU or supplier's item number.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck if you no longer use this raw product.",
    )
    par_level = models.PositiveIntegerField(
        default=100,
        help_text="Target undyed quantity to keep on hand. 0 = no par set.",
    )
    finished_par_default = models.PositiveIntegerField(
        default=8,
        help_text=(
            "Par given to a new finished product made from this blank. Only "
            "applies at creation — changing it never rewrites the par of a "
            "product that already exists, because that would silently "
            "re-schedule production. Use the raw-product admin action "
            "'Bulk update finished product par' to change existing ones."
        ),
    )
    square_item_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Square CatalogItem ID for this product.",
    )

    class Meta:
        ordering = ["category__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.category.name})"

    @property
    def raw_shortage(self) -> int:
        """
        How many more raw units we need to reach par.
        """
        if self.par_level is None or self.par_level == 0:
            return 0
        return max(self.par_level - self.number_on_hand, 0)

class Recipe(models.Model):
    """
    A dye recipe made from 1–5 dyes.
    Uses a through model (RecipeDye) so we can store order/ratio.
    """
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    dyes = models.ManyToManyField(
        Dye,
        through="RecipeDye",
        related_name="recipes",
    )
    is_active = models.BooleanField(default=True)

    color_bands = ArrayField(
        models.CharField(max_length=12, choices=BAND_CHOICES),
        default=list,
        blank=True,
        help_text=(
            "Which sections of the rainbow reference sheet this colorway is "
            "printed in. A red-and-orange scarf claims both and prints twice."
        ),
    )
    bands_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When a person last confirmed the bands above. Null means nothing "
            "has been confirmed yet, so the sheet must not print this recipe: "
            "an unchecked guess that files a scarf under the wrong color is "
            "worse than leaving it off, because the failure is silent — you "
            "look in orange, it isn't there, and nothing says why."
        ),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def dye_count(self):
        return self.dyes.count()

    @property
    def bands_confirmed(self) -> bool:
        return self.bands_confirmed_at is not None


class RecipeDye(models.Model):
    """
    Join table between Recipe and Dye.
    Allows 1–5 dyes per recipe with ordering and optional ratio/percentage.
    """
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name="recipe_dyes",
    )
    dye = models.ForeignKey(
        Dye,
        on_delete=models.PROTECT,
        related_name="recipe_dyes",
    )
    order = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Order in which this dye appears in the recipe.",
    )
    ratio = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Optional proportion (e.g. grams, % of total).",
    )

    class Meta:
        unique_together = ("recipe", "dye")
        ordering = ["recipe", "order"]

    def __str__(self):
        return f"{self.recipe.name} - {self.dye.name} (#{self.order})"


class FinishedProduct(models.Model):
    """
    A finished, dyed item: specific raw product + specific recipe.
    """
    name = models.CharField(
        max_length=200,
        help_text="Customer-facing name, e.g. 'Stormy Sea Silk Scarf'.",
    )
    raw_product = models.ForeignKey(
        RawProduct,
        on_delete=models.PROTECT,
        related_name="finished_products",
    )
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.PROTECT,
        related_name="finished_products",
    )
    price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Selling price of the finished item.",
    )
    number_on_hand = models.PositiveIntegerField(
        default=0,
        help_text="How many finished items of this kind you have.",
    )
    par = models.PositiveIntegerField(
        default=8,
        help_text="How many finished items of this kind you expect to have.",
    )
    is_fancy = models.BooleanField(
        default=False,
        help_text="Mark as fancy to distinguish premium designs."
    )
    sku = models.CharField(
        max_length=50,
        blank=True,
        help_text="SKU/code for the finished product.",
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    square_variation_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Square CatalogItemVariation ID for this finished product.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("raw_product", "recipe", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def shortage(self) -> int:
        """
        How many more we need to reach par.
        Negative/zero => at or above par.
        """
        if self.par is None:
            return 0
        return max(self.par - self.number_on_hand, 0)

    @property
    def bath_size(self) -> int:
        """How many of this come out of one dye bath. Never 0 — `record_dye_bath`
        already treats a missing bath size as 1, and this has to agree with it."""
        return self.raw_product.number_per_dye_bath or 1

    @property
    def behind_a_bath(self) -> bool:
        """True when a whole dye bath still lands at or under par.

        This is the production page's red highlight, and it is a question about
        the *next bath*, not about the shelf being empty. With par 8 and a bath
        of 4: at 5 on hand a bath overshoots to 9, so the shortage of 3 is going
        to be rounded away whenever this recipe next gets dyed anyway — nothing
        to plan around. At 4 on hand a bath lands exactly on 8, and every step
        below that ends the bath still short. Those are the ones worth walking
        to, because they're where a session's work is fully used.

        Overshoot is normal and expected (a bath is a fixed size), so 'below par'
        on its own marks nearly everything and picks out nothing.
        """
        if self.par is None:
            return False
        return self.shortage >= self.bath_size


class FinishedProductImage(models.Model):
    """
    An image for a finished product. `image` holds an uploaded file (stored on
    the configured default storage — the S3 bucket in production, local disk in
    dev). `image_url` is kept as an optional fallback for externally-hosted
    images. Use the `url` property to get whichever is set.
    """
    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.CASCADE,
        related_name="images",
    )
    image = models.ImageField(
        upload_to="finished_products/",
        blank=True,
        help_text="Uploaded image file (stored in the bucket).",
    )
    image_url = models.URLField(
        blank=True,
        help_text="Optional: URL of an externally-hosted image.",
    )
    alt_text = models.CharField(
        max_length=200,
        blank=True,
        help_text="Alt text / description for accessibility.",
    )
    order = models.PositiveSmallIntegerField(
        default=1,
        help_text="Ordering of images in galleries.",
    )

    class Meta:
        ordering = ["finished_product", "order"]

    @property
    def url(self):
        """Presigned bucket URL for the uploaded file, else the external URL."""
        if self.image:
            return self.image.url
        return self.image_url

    def __str__(self):
        return f"Image for {self.finished_product.name} (#{self.order})"


class InventoryLog(models.Model):
    """
    Records any change to finished product inventory: production runs, sales, or manual adjustments.
    Positive quantity = items added. Negative quantity = items removed.
    """
    PRODUCTION = "production"
    SALE = "sale"
    ADJUSTMENT = "adjustment"
    LOG_TYPE_CHOICES = [
        (PRODUCTION, "Production"),
        (SALE, "Sale"),
        (ADJUSTMENT, "Adjustment"),
    ]

    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.PROTECT,
        related_name="inventory_logs",
    )
    raw_product = models.ForeignKey(
        RawProduct,
        on_delete=models.PROTECT,
        related_name="inventory_logs",
        null=True,
        blank=True,
    )
    log_type = models.CharField(
        max_length=20,
        choices=LOG_TYPE_CHOICES,
        default=PRODUCTION,
    )
    quantity = models.IntegerField(
        help_text="Items added (positive) or removed (negative).",
    )
    sale_reference = models.CharField(
        max_length=100,
        blank=True,
        help_text="Square order ID for sale entries.",
    )
    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    DATE_PRECISION_CHOICES = [
        (EXACT, "Exact time"),
        (DAY, "Day only"),
        (MONTH, "Month only"),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    date_precision = models.CharField(
        max_length=10,
        choices=DATE_PRECISION_CHOICES,
        default=EXACT,
        help_text=(
            "How much of created_at is real. Back-filled entries from the "
            "old kanban cards often record only a month — this stops the app "
            "displaying a day nobody ever wrote down."
        ),
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.log_type} {self.quantity:+d} × {self.finished_product.name} on {self.when}"

    @property
    def when(self) -> str:
        """The date, said no more precisely than it is actually known.

        A card reading "9/2024" gets stored on the 1st so it sorts, but that
        day is an artefact of storage, not something anyone recorded — so it
        is never shown.
        """
        local = timezone.localtime(self.created_at)
        if self.date_precision == self.MONTH:
            return local.strftime("%b %Y")
        if self.date_precision == self.DAY:
            return local.strftime("%d %b %Y")
        return local.strftime("%d %b %Y, %H:%M")


class ProductImageUpload(models.Model):
    """
    Tracks a photo uploaded (direct-to-bucket via presigned POST) and its
    journey to being filed against a FinishedProduct. Matched automatically by
    decoding the barcode in the image; if that fails the uploader assigns it
    manually on the upload screen. This row is state/audit only — there is no
    operator review queue.
    """
    STATUS_PENDING = "pending"
    STATUS_MATCHED = "matched"
    STATUS_ASSIGNED = "assigned"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_MATCHED, "Matched (barcode)"),
        (STATUS_ASSIGNED, "Assigned (manual)"),
        (STATUS_FAILED, "Failed"),
    ]

    key = models.CharField(
        max_length=255,
        unique=True,
        help_text="Object key in the bucket.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    detected_sku = models.CharField(
        max_length=50,
        blank=True,
        help_text="SKU decoded from the barcode, if any.",
    )
    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="image_uploads",
    )
    product_image = models.ForeignKey(
        FinishedProductImage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def preview_url(self):
        """URL of the uploaded file, so it can be shown while the uploader
        assigns it manually: presigned GET from the bucket, or the local
        storage URL in dev."""
        from django.conf import settings
        if not self.key:
            return ""
        if not settings.USE_S3:
            from django.core.files.storage import default_storage
            return default_storage.url(self.key)
        from .s3utils import presigned_get
        return presigned_get(self.key)

    def __str__(self):
        return f"{self.key} ({self.status})"


class Employee(models.Model):
    """Someone who works the booth during festival hours and reports their own.

    "Works the booth" is the whole roster this covers — it is not a list of
    everyone who helps out. See TimeEntry for why the boundary matters.

    Deliberately not a `django.contrib.auth` User. A seasonal crew would mean
    an account, a password and a reset request per person, all to protect a
    number they tell you anyway — so identity on the hours form is a name off
    a list plus a four-digit PIN.

    Be clear about what the PIN is for: it stops somebody tapping the wrong
    name, and it stops idle mischief from whoever finds the URL. It is not a
    secret and does not pretend to be one. The real control is that a person
    reads the week before it goes to payroll.
    """
    name = models.CharField(max_length=100, unique=True)
    pin = models.CharField(
        max_length=4,
        validators=[RegexValidator(r"^\d{4}$", "The PIN must be exactly four digits.")],
        help_text=(
            "Four digits, handed to the employee. Stored as typed so it can be "
            "read back to whoever forgets theirs — it guards a timesheet entry "
            "a person reviews, not an account."
        ),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Unticked takes them off the hours form without touching the hours "
            "they already reported."
        ),
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TimeEntry(models.Model):
    """One person's hours for one day, as they reported them.

    Hours are a self-reported decimal rather than a clock-in and a clock-out.
    That is a deliberate trade: the arithmetic nobody wants to do at the end
    of a fair day disappears, and in exchange there is no start time to check
    a claim against. What replaces it is review — the weekly sheet flags the
    rows worth questioning, and a person signs the week off.

    Scope is deliberately narrow: **hours running the booth during festival
    days**. Production help — dyeing, prep, anything back at the shop — is
    not recorded here and must not be added later without deciding what it
    means for payroll first. A field that quietly starts collecting a second
    kind of work turns every total on the timesheet into a number whose
    meaning depends on who typed it.

    One row per employee per day, enforced in the database. A double-tapped
    Submit is the likeliest mistake this form will ever see, and without the
    constraint it books the day twice.
    """
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="time_entries",
    )
    work_date = models.DateField(
        help_text="The day worked — not the day it was reported.",
    )
    hours = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.25")),
            MaxValueValidator(Decimal("16")),
        ],
    )
    notes = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-work_date", "employee__name"]
        verbose_name_plural = "time entries"
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "work_date"],
                name="one_time_entry_per_employee_per_day",
            ),
        ]

    def __str__(self):
        return f"{self.employee.name} — {self.hours}h on {self.work_date:%d %b %Y}"

    @property
    def was_revised(self) -> bool:
        """True once the day has been reported a second time.

        `auto_now` and `auto_now_add` fire microseconds apart on a fresh row,
        so a plain inequality would call everything revised — hence the
        one-second slack.
        """
        return (self.updated_at - self.created_at).total_seconds() > 1

    @property
    def reported_late_by(self) -> int:
        """Days between working the shift and reporting it; 0 for same day.

        A figure typed a fortnight later is a memory, not a record. The
        timesheet says so rather than presenting it like the rest.
        """
        return (timezone.localtime(self.created_at).date() - self.work_date).days



class LabelStock(models.Model):
    """One kind of die-cut label sheet, described by the numbers a PDF needs
    to put ink where the die-cuts are.

    A laser label sheet is plain paper with cuts in it; the printer knows
    nothing about the cuts. So a "template" is not a file — it's the eight
    measurements below, all of which every vendor publishes on the product
    page. That's why this is a table you type into rather than a template
    you upload: recovering die geometry from a vendor's .docx means
    reverse-engineering Word's cell rounding, and being 2mm out is only
    discoverable by wasting a sheet.

    Geometry is in **inches**, because that's the unit the vendor spec sheets
    use and it should be transcribable straight across. The two offsets are in
    **millimetres**, because they exist to correct a printer's feed
    registration and that's what you measure with a ruler. Both units are in
    the field names so they can't be misread.
    """

    name = models.CharField(max_length=150, unique=True)
    purchase_url = models.URLField(
        blank=True,
        help_text="Where to buy more of this stock. Shown on the label page.",
    )

    page_width_in = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("8.5"))
    page_height_in = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("11"))

    label_width_in = models.DecimalField(max_digits=6, decimal_places=4)
    label_height_in = models.DecimalField(max_digits=6, decimal_places=4)

    columns = models.PositiveSmallIntegerField()
    rows = models.PositiveSmallIntegerField()

    margin_left_in = models.DecimalField(
        max_digits=6, decimal_places=4,
        help_text="Sheet edge to the left edge of the first column.",
    )
    margin_top_in = models.DecimalField(
        max_digits=6, decimal_places=4,
        help_text="Sheet edge to the top edge of the first row.",
    )
    pitch_x_in = models.DecimalField(
        max_digits=6, decimal_places=4,
        help_text="Left edge of one column to the left edge of the next "
                  "(label width + the gap, not the gap on its own).",
    )
    pitch_y_in = models.DecimalField(
        max_digits=6, decimal_places=4,
        help_text="Top edge of one row to the top edge of the next.",
    )

    x_offset_mm = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0"),
        help_text="Nudge every label right (+) or left (−) to correct this "
                  "printer's registration. Print the calibration sheet first.",
    )
    y_offset_mm = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0"),
        help_text="Nudge every label up (+) or down (−).",
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def labels_per_sheet(self) -> int:
        return self.columns * self.rows

    @property
    def is_continuous(self) -> bool:
        """True for a thermal roll: one label per 'sheet'.

        A roll is the degenerate case of this table — a 1 × 1 grid whose page
        size *is* the label, with no margins, no pitch and no registration to
        get wrong. Only two of the eight numbers matter, which is why adding a
        label printer needs no new model.

        Two behaviours have to know the difference. There is no part-used
        sheet to resume, so `start_at` is always 1; and a marker sticker would
        print after every single run for no one to read, doubling the label
        count on short runs instead of saving anything.
        """
        return self.labels_per_sheet == 1

    def overflow_in(self):
        """How far the last column/row runs past the sheet, in inches.

        Positive on either axis means the numbers are wrong — a transposed
        digit in a pitch is the likeliest typo when adding a stock, and it
        prints a whole ruined sheet before anyone notices. `LabelStockTests`
        asserts this is clear for every seeded stock, and `clean()` refuses
        to save one that isn't.
        """
        right = (
            self.margin_left_in
            + (self.columns - 1) * self.pitch_x_in
            + self.label_width_in
        )
        bottom = (
            self.margin_top_in
            + (self.rows - 1) * self.pitch_y_in
            + self.label_height_in
        )
        return right - self.page_width_in, bottom - self.page_height_in

    def clean(self):
        super().clean()
        tolerance = Decimal("0.01")
        over_x, over_y = self.overflow_in()
        if over_x > tolerance:
            raise ValidationError(
                f"{self.columns} columns at {self.pitch_x_in}in pitch run "
                f"{over_x:.4f}in off the right edge of a "
                f"{self.page_width_in}in sheet."
            )
        if over_y > tolerance:
            raise ValidationError(
                f"{self.rows} rows at {self.pitch_y_in}in pitch run "
                f"{over_y:.4f}in off the bottom of a "
                f"{self.page_height_in}in sheet."
            )
