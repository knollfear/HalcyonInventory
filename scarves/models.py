import re
import secrets
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


class CatalogGroup(models.Model):
    """Several raw products sold under one Square item.

    Everywhere else the catalog's two axes are blank × colorway: the Square
    ITEM is the blank (`Silk Infinity`) and each VARIATION is a recipe. That
    holds because one blank really does come in many colorways.

    Undyed stock inverts it. A yarn we sell exactly as it arrives has no
    colorway at all, and the thing a customer picks between is the *yarn* —
    so the item is "Undyed Yarn" and the variations are the blanks. Same two
    axes, swapped.

    Rather than teach `RawProduct` to be sometimes-an-item, the grouping is
    named here and pointed at. A raw product with no group is its own item,
    which is every scarf blank and stays the default.
    """

    name = models.CharField(
        max_length=150,
        unique=True,
        help_text="The Square item name, e.g. 'Undyed Yarn'.",
    )
    square_item_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Square CatalogItem ID for the group's item.",
    )
    category = models.ForeignKey(
        RawProductCategory,
        on_delete=models.PROTECT,
        related_name="catalog_groups",
        help_text="Square category for the group's item.",
    )

    class Meta:
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
    catalog_group = models.ForeignKey(
        CatalogGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="raw_products",
        help_text=(
            "Sell this under a shared Square item instead of one of its own. "
            "Blank for everything dyed, which is the normal case — see "
            "CatalogGroup for when it isn't."
        ),
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
        null=True,
        blank=True,
        help_text=(
            "The colorway. **Null means this was never dyed** — an undyed "
            "passthrough, bought and sold as it arrives. Null rather than a "
            "sentinel 'Undyed' recipe on purpose: every dyed-only query in "
            "the app joins through this FK, so a null row drops out of "
            "production planning, the rainbow sheets, the colour pages and "
            "the games by construction. A sentinel would need each of those "
            "to remember to exclude it, and a forgotten exclusion is silent "
            "— an undyed skein filed under a colour it doesn't have."
        ),
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

    def save(self, *args, **kwargs):
        """Fill in a SKU on the way in, if there isn't one.

        Generation used to live only in the `generate_skus` command, so
        anything created through the admin, the bulk matrix or a shell had no
        barcode until somebody remembered to run it. A product with no SKU
        can't be printed on a label or scanned in Square, and nothing said so
        — it simply wasn't there.

        Only ever fills a blank. A SKU that exists has been printed on
        reference sheets and stickers and handed to Square, none of which this
        app can rewrite, so `save()` must never change one.

        Fixtures are unaffected: `loaddata` goes through `save_base(raw=True)`
        and never calls this, so a deliberately blank SKU in a fixture stays
        blank. `FixtureSkuTests` pins that.

        Also settles a passthrough's count, which is not its own to hold —
        see `is_passthrough` and the `mirror_passthrough_stock` signal. The
        signal covers the raw row moving afterwards; this covers the row
        being created, when there was no passthrough for the signal to find.
        """
        # No recipe is a passthrough, not a half-built object — `base_for`
        # gives those the `UNDYED` half. Requiring a recipe here is what left
        # every undyed yarn with a blank SKU and nothing to scan.
        if not self.sku and self.raw_product_id:
            from .skus import unique_sku

            self.sku = unique_sku(self)
            # A caller passing update_fields didn't know a SKU was coming, so
            # add it — otherwise the value is set in memory and silently lost.
            self._also_update(kwargs, "sku")

        if self.is_passthrough and self.raw_product_id:
            stock = self.raw_product.number_on_hand
            if self.number_on_hand != stock:
                self.number_on_hand = stock
                self._also_update(kwargs, "number_on_hand")

        super().save(*args, **kwargs)

    @staticmethod
    def _also_update(kwargs, field):
        """Add `field` to an explicit `update_fields`, if one was given."""
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and field not in update_fields:
            kwargs["update_fields"] = list(update_fields) + [field]

    @property
    def is_passthrough(self) -> bool:
        """Bought and sold as it arrives — no dye step, no colorway.

        The raw product and this row describe the *same physical skein*, which
        is the whole difference. For anything dyed they are two piles and the
        dye bath is the event that moves one to the other; here there is one
        pile with two names for it.
        """
        return self.recipe_id is None

    @property
    def variation_name(self) -> str:
        """What Square should call this variation.

        A colorway when there is one. For a passthrough the item is the group
        ("Undyed Yarn") and the thing being chosen between is the blank, so
        the blank's name is what belongs on the variation.
        """
        return self.recipe.name if self.recipe_id else self.raw_product.name

    def set_on_hand(self, value):
        """Write a counted quantity to whichever row actually holds it.

        For anything dyed that is this row. For a passthrough it is the raw
        product — the two describe one pile, and writing here instead would
        be writing to a mirror: `save()` re-derives it, so the number would
        snap back and the stock take would look like it hadn't taken.
        """
        value = max(int(value), 0)
        if self.is_passthrough:
            raw = self.raw_product
            raw.number_on_hand = value
            raw.save(update_fields=["number_on_hand"])   # signal mirrors down
            self.refresh_from_db(fields=["number_on_hand"])
        else:
            self.number_on_hand = value
            self.save(update_fields=["number_on_hand"])

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
    square_image_id = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Square's ID for this photo once `sync_to_square --images` has "
            "pushed it. Blank means Square has never seen it. This is what "
            "stops a second run stacking the same photo on the variation "
            "again — Square appends to `image_ids` and has no idea it is "
            "looking at a picture it already holds."
        ),
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
    user = models.OneToOneField(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee",
        help_text=(
            "The login this person uses, for the few who have one. Almost "
            "nobody does — the crew are deliberately account-less. It exists "
            "so a signed-in staff member isn't asked to pick their own name "
            "off a list and type a PIN on a page they have already "
            "authenticated for. Blank is the normal case and changes nothing."
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


class BoothPhoto(models.Model):
    """A photo sent in from the booth, and the reason it was sent.

    Two reasons, one form, because there is one moment when a phone comes out
    at a stall and asking someone to pick the right page first is how you get
    no photos at all:

    * **share** — something worth putting on the website or Instagram.
    * **unidentified** — a scarf nobody could name, sold anyway. The photo,
      the time, and the first six characters of the barcode are what let the
      sale be reconstructed later; see `UnmatchedSale`.

    Identified by `Employee` and a PIN rather than a login, for the same
    reason the hours form is: a seasonal crew would need an account each, and
    the page would then be behind exactly the door the people it was built for
    can't open. Attribution still matters here — a sharing permission nobody
    can attribute is not a permission — so the employee is required, not
    optional.
    """

    REASON_SHARE = "share"
    REASON_UNIDENTIFIED = "unidentified"
    REASON_CHOICES = [
        (REASON_SHARE, "Something to share"),
        (REASON_UNIDENTIFIED, "A scarf nobody could identify"),
    ]

    image = models.ImageField(
        upload_to="booth/",
        help_text="The photo as sent, downscaled on the way in.",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="booth_photos",
        help_text="Who sent it. PROTECT because a permission with nobody "
                  "attached to it can't be relied on later.",
    )
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- reason: share -----------------------------------------------------
    # Two destinations, two ticks. Permission for the website is not
    # permission for Instagram: one is a shop page, the other is a feed with
    # an audience and a comment box, and people do say yes to one and no to
    # the other.
    share_website = models.BooleanField(default=False)
    share_instagram = models.BooleanField(default=False)
    people_in_photo = models.BooleanField(
        default=False,
        help_text="Someone recognisable is in the shot.",
    )
    people_agreed = models.BooleanField(
        default=False,
        help_text=(
            "They were asked and said yes. Separate from the ticks above "
            "because those record the *sender's* permission, and the sender "
            "cannot give permission on behalf of the person in the picture."
        ),
    )
    caption = models.TextField(blank=True)
    tag = models.CharField(
        max_length=200,
        blank=True,
        help_text="Anyone to tag, as the sender wrote it.",
    )

    # --- reason: unidentified sale ----------------------------------------
    sold_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the scarf sold, as reported. Defaults to the moment "
                  "the photo was sent, which is usually within a minute of it.",
    )
    sku_prefix = models.CharField(
        max_length=6,
        blank=True,
        help_text=(
            "First six characters of the barcode — the blank, not the "
            "colorway (SKUs are BLANK-DYEBATH). Nobody can read a colorway "
            "off a scarf they couldn't name, but the style is on the tag and "
            "it narrows a few hundred products to a few dozen."
        ),
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_reason_display()} from {self.employee.name}"

    @property
    def when(self):
        """The moment this is about: the sale if one was reported, else the
        moment the photo arrived."""
        return self.sold_at or self.created_at

    @property
    def shareable(self) -> bool:
        """Whether this may actually be posted anywhere.

        A tick for a destination is not enough on its own: if there is a
        recognisable person in the shot, they have to have agreed too. The
        gallery leans on this rather than on the two destination flags, so
        the awkward case can't be posted by reading the wrong checkbox.
        """
        if not (self.share_website or self.share_instagram):
            return False
        return self.people_agreed if self.people_in_photo else True

    def candidate_products(self):
        """Products whose SKU starts with the reported prefix.

        Empty prefix means every active product — the reviewer picks by hand,
        which is the honest answer rather than pretending to have narrowed it.
        """
        products = FinishedProduct.objects.filter(is_active=True)
        if self.sku_prefix:
            products = products.filter(sku__istartswith=self.sku_prefix)
        return products.select_related("raw_product", "recipe").order_by("name")


class UnmatchedSale(models.Model):
    """A Square line item this app could not tie to a FinishedProduct.

    Before this existed the webhook simply skipped those lines, so a scarf
    nobody could name was rung up, walked out of the tent, and left no trace
    anywhere: Square had the money, this app still had the stock, and nothing
    in either said they disagreed. Silence was the whole failure — the count
    was wrong and looked fine.

    A row lands here whenever the line item has no `catalog_object_id`, or has
    one this app doesn't know, whatever the reason (rung up as a generic
    item, sold as a custom amount, or a variation that was never synced).
    Getting it wrong in the cautious direction is cheap: a row that turns out
    not to be a scarf is dismissed in one click.

    Every row ends in exactly one of two states, and both of them are actions
    a person took:

    * **resolved** — matched to a product, so the stock moves and an
      `InventoryLog` SALE row is written, marked as a manual match.
    * **dismissed** — it was never a scarf (a tip, a bag, a hat), and it says
      so. Without this the queue could only grow, and a queue that can't be
      emptied stops being read.
    """

    order_id = models.CharField(max_length=100, db_index=True)
    line_uid = models.CharField(
        max_length=100,
        help_text="Square's per-line identifier within the order.",
    )
    name = models.CharField(max_length=255, blank=True)
    variation_name = models.CharField(max_length=255, blank=True)
    square_variation_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Blank when the line had no catalog object at all — a "
                  "custom amount rather than an item.",
    )
    quantity = models.PositiveIntegerField(default=1)
    amount_cents = models.IntegerField(
        default=0,
        help_text="What it sold for, in cents. Kept because price is often "
                  "the strongest hint about which style it was.",
    )
    sold_at = models.DateTimeField(
        help_text="Square's time for the order, not the time we heard about it.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    resolved_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="manual_matches",
    )
    resolved_photo = models.ForeignKey(
        BoothPhoto,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matched_sales",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-sold_at"]
        constraints = [
            # Square sends order.updated more than once for the same order, so
            # without this the queue fills with copies of one sale and the
            # reviewer resolves the same scarf out of stock repeatedly.
            models.UniqueConstraint(
                fields=["order_id", "line_uid"],
                name="one_unmatched_row_per_order_line",
            ),
        ]

    def __str__(self):
        return f"{self.name or 'unnamed line'} × {self.quantity} on {self.sold_at:%d %b %Y}"

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None and self.dismissed_at is None

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_cents) / 100


#: Words for a sheet's code. Chosen to be short, unambiguously spelled, and
#: readable off a photocopy — no homophones, no doubled letters that blur, and
#: nothing anyone has to think about how to spell. Someone types these in a
#: dye room while holding the paper.
RUN_ADJECTIVES = (
    "amber azure brisk bronze calm clever coastal copper coral crimson curious "
    "dapper deep dusty eager early easy emerald fabled fleet gentle gilded "
    "golden grand happy hardy hazel hidden humble idle indigo ivory jade jolly "
    "keen kindly lively lucky lunar marble mellow merry misty modest noble "
    "nimble northern olive opal patient pearl plucky polar prairie proud quiet "
    "rapid ready restless rosy royal ruby rugged rustic sable sandy scarlet "
    "shady sharp silent silver simple sleepy slender snowy solar sombre "
    "southern sparkling spry stately steady stormy sunny swift tawny tidy "
    "timber tranquil trusty umber upland velvet violet wandering warm western "
    "whispering wild windy winter wise woven zesty"
).split()

RUN_ANIMALS = (
    "adder badger bison bittern bobcat caribou chamois cheetah civet condor "
    "corgi cougar coyote crane cricket curlew dingo dormouse dunlin eagle "
    "egret falcon fennec ferret finch fisher gannet gecko gerbil gibbon "
    "godwit gopher goshawk grebe heron hoopoe ibex ibis impala jackal jaguar "
    "kestrel kingfisher kite koala kudu lapwing lemur leopard linnet lynx "
    "magpie manatee marmot marten meerkat merlin mongoose moorhen muntjac "
    "narwhal newt nuthatch ocelot opossum osprey otter panther pelican petrel "
    "pika plover polecat puffin quokka raccoon redshank reindeer roebuck "
    "sable serval shrew siskin skylark sparrow stoat swallow tapir teal tern "
    "thrush toucan vicuna vole wallaby walrus warbler weasel wombat wren"
).split()


def new_run_token():
    """The code identifying one production sheet: `42-brisk-wombat`.

    Two words and two digits because a person types this off paper when the
    QR won't read, and typing it *from the sheet* is what ties a photo to the
    run it claims to be of. A random string does that job too, right up until
    somebody has to transcribe `VnHePvvkqH__toMw` from a photocopy — at which
    point the fallback is one in name only.

    The digits are there for entropy, not decoration. Words alone give about
    fourteen bits, and with a handful of sheets open at once that is inside
    reach of a script; the pair of digits buys back most of what plain words
    give away. Worth being clear what is at stake either way: the URL is
    unlisted rather than secret, and the worst a guess wins is production
    recorded against a sheet, which is visible on the run's own page and
    correctable — see CLAUDE.md on what `secret/` does and doesn't promise.
    """
    return "-".join((
        f"{secrets.randbelow(100):02d}",
        secrets.choice(RUN_ADJECTIVES),
        secrets.choice(RUN_ANIMALS),
    ))


def normalize_token(text):
    """Compare codes the way a person types them.

    `42 Brisk Wombat`, `42-brisk-wombat` and `42BRISKWOMBAT` are the same
    answer. Punctuation and case are how a phone keyboard differs from a
    printed page, not how one sheet differs from another.

    Which is also why the code carries no punctuation beyond its separators:
    stripped here, a symbol would add nothing to the guesswork while costing
    a keystroke on the worst keyboard anyone will use for this. A third digit
    would buy real bits for the same effort, if it is ever wanted.
    """
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


class ProductionRun(models.Model):
    """One printed production sheet: a list of dye baths to go and do.

    The sheet is the work order and the paper is what goes to the sink —
    gloves, dye and water make a phone a bad thing to be holding. So the
    session is marked in pencil as it happens, and the phone is only picked up
    afterwards to say which of the baths actually got done.

    **The row is a bath, not a scarf.** A dye bath is one blank plus one
    recipe and yields `number_per_dye_bath` units of a single SKU, so a
    session is a handful of yes/no answers rather than a column of counts.
    "We got through 10 of the 20" is ten ticked rows, and nothing has to be
    added up by the person holding the pencil.

    Getting the answer back is one scan of one QR code for the whole sheet,
    not one per row: twenty codes would be twenty interactions to record what
    is genuinely one session's work. The token in that URL is what authorises
    the return — the same bargain as the other `secret/` pages, except scoped
    to a single sheet rather than standing open forever.
    """

    token = models.CharField(
        max_length=32,
        unique=True,
        default=new_run_token,
        help_text=(
            "The sheet's code. Rides in the crew's return URL, prints as a QR "
            "code, and prints in plain text for someone to type when the QR "
            "won't read."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the crew first reported back. Null means this sheet is "
            "still out — which has to be visible, because a session nobody "
            "reported looks exactly like a session that never happened."
        ),
    )
    submitted_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_runs",
        help_text=(
            "Whoever reported back, if their phone remembered them. A record, "
            "not a check: the token on the paper is what lets the report "
            "through, so asking for a PIN here would only add friction to a "
            "page reached by scanning a sheet you are already holding."
        ),
    )

    # Kept so the sheet can say what it asked for, and so a reprint can be
    # made to match. Not re-derived on submit: by then the shortages have
    # moved, and the question "what was on the paper?" has exactly one honest
    # answer, which is the rows.
    category = models.ForeignKey(
        RawProductCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_runs",
    )
    included_overshoot = models.BooleanField(
        default=False,
        help_text=(
            "Whether the sheet included products a whole bath would take past "
            "par. See FinishedProduct.behind_a_bath for the distinction."
        ),
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Production run #{self.pk} ({self.rows.count()} baths)"

    @property
    def is_open(self) -> bool:
        return self.submitted_at is None

    @property
    def done_count(self) -> int:
        return sum(1 for row in self.rows.all() if row.done_at is not None)


class ProductionRunRow(models.Model):
    """One dye bath on one sheet: one line, one tick box, one barcode.

    `quantity` is frozen when the sheet prints rather than read back off the
    raw product. The paper says "x4" and the paper is what the person worked
    from; if somebody edits the bath size next week, this row still has to
    mean what it said when it was in their hand.
    """

    run = models.ForeignKey(
        ProductionRun,
        on_delete=models.CASCADE,
        related_name="rows",
    )
    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.PROTECT,
        related_name="production_rows",
    )
    order = models.PositiveSmallIntegerField(
        default=1,
        help_text="Position on the printed sheet, so the phone lists them in the same order.",
    )
    quantity = models.PositiveSmallIntegerField(
        help_text="Units this bath yields, as printed on the sheet.",
    )
    done_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this bath was reported done. Null means it wasn't.",
    )
    applied_log = models.ForeignKey(
        InventoryLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "The stock movement this row caused. Set once and then never "
            "again — it is what stops a second submission, a double-tapped "
            "button or a re-scanned QR from dyeing the same bath twice on "
            "paper. Same failure the Square webhook had with redelivered "
            "orders, and the same fix."
        ),
    )

    class Meta:
        ordering = ["run", "order", "pk"]

    def __str__(self):
        return f"{self.finished_product.sku or self.finished_product.name} x{self.quantity}"

    @property
    def is_applied(self) -> bool:
        return self.applied_log_id is not None
