from colorfield.fields import ColorField
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
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

