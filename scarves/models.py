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

from . import seasons

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


#: Brand given to a dye typed in from a recipe page. The dye is real and the
#: recipe needs it now; which brand's jar it was is a question for later, and
#: making it a required answer up front is how the dye ends up not recorded at
#: all. `Dye.needs_review` is what makes "later" findable.
UNCATEGORIZED_BRAND = "Uncategorized"

#: A leading catalog number, with or without punctuation after it: the `416 `
#: of `416 Peacock Blue`, the `#25 - ` of `#25 - Sapphire`.
_NUMBER_PREFIX = re.compile(r"^\s*#?\d+\s*[-–—.:)]?\s*")


#: A trailing parenthetical: the `(Primary)` of `402 Fire Engine Red
#: (Primary)`. Dharma marks its mixing primaries this way.
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def dye_match_key(name):
    """What makes two dye names the same dye, for "is this already on file?".

    Neither the catalog number nor the `(Primary)` tag is part of what the
    dye *is* — both are catalog furniture, and both are exactly what somebody
    typing from memory leaves off. Matching on the full string means "Fire
    Engine Red" doesn't find `402 Fire Engine Red (Primary)`, and the second
    Fire Engine Red gets created beside the first. From then on the two
    split a history that reads as complete on either row.

    This is a comparison key only. Nothing is ever displayed or stored from
    here: the name on the jar keeps its number, because that is how the jar
    is found on the shelf.
    """
    key = dye_sort_name(name)
    while True:
        shorter = _TRAILING_PAREN.sub("", key)
        if shorter == key or not shorter:
            break
        key = shorter
    key = " ".join(key.split()).casefold()
    # Trailing marks go the same way, for the same reason: Dharma stars a
    # few names (`409 Dark Navy*`) and nobody types the star.
    return re.sub(r"[^0-9a-z]+$", "", key) or key


def dye_sort_name(name):
    """A dye name with any leading catalog number taken off.

    Dharma and Jacquard both ship their dyes numbered — `416 Peacock Blue`,
    `402 Fire Engine Red` — and the number is worth keeping, because it is
    what is printed on the jar. But it is also the first thing a sort or a
    search sees, so an alphabetical list of dyes comes out in catalog order
    and a person hunting for peacock reads all 84 entries. Filed under P
    instead, with the number still showing.

    A name that is *only* a number keeps it, since the alternative is a dye
    with no name at all.
    """
    return _NUMBER_PREFIX.sub("", name).strip() or name


class Dye(models.Model):
    """
    Represents a dye color you keep in inventory.
    """
    name = models.CharField(max_length=100)
    #: Blank means "nobody has recorded this dye's colour yet", and every
    #: consumer already treats an unparseable hex as no answer: `colorutils`
    #: leaves it out of the palette, `colorbands` claims no band for it, and
    #: the production sheet draws an empty chip. That is the whole reason a
    #: dye can be added mid-entry with no colour — the alternative is the old
    #: default, a confident red that reaches the rainbow sheet, the games and
    #: the dye-collection page as a fact nobody typed.
    hex_color = ColorField(blank=True, default="")
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

    @property
    def sort_name(self):
        """This dye's name with any leading catalog number taken off."""
        return dye_sort_name(self.name)

    @property
    def needs_review(self):
        """Added mid-entry and never cleaned up: no colour, or no real brand.

        Not a validity check — a dye like this works everywhere, it just
        contributes nothing to any colour question until somebody fills it
        in. The admin's dye list, filtered to "needs review", is where
        that gets finished.
        """
        return not self.hex_color or self.brand.name == UNCATEGORIZED_BRAND


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
    made_in_a_dye_bath = models.BooleanField(
        default=True,
        help_text=(
            "Uncheck for anything a dye bath cannot produce, and it drops off "
            "every production list.\n\n"
            "The case this exists for is the fancy veils. A fancy veil is an "
            "already-dyed scarf with extra line work added — so it *has* a "
            "colorway and passes every dyed-only query in the app, but you "
            "can never answer a shortage of one by dyeing. Sending somebody "
            "to the dye room for it is asking for a thing that isn't made "
            "there.\n\n"
            "Undyed passthroughs are excluded by their null recipe instead "
            "and don't need this — see FinishedProduct.recipe. Two markers "
            "because they are two different claims: a passthrough was never "
            "dyed at all, a fancy veil was dyed and then worked on."
        ),
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
    display_slots_default = models.PositiveIntegerField(
        default=2,
        help_text=(
            "How many units of a new colorway of this blank go on display — "
            "a peg that holds two skeins, a spot on the scarf pole. Only "
            "applies at creation, like finished_par_default; use the "
            "'Bulk update display slots' action to change existing ones. "
            "This is display *capacity*, and it is not a production target: "
            "see FinishedProduct.display_slots."
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
    display_slots = models.PositiveIntegerField(
        default=2,
        help_text=(
            "How many of this go on display when the display is full — pegs "
            "times what a peg holds, or spots on the pole. **Capacity, not a "
            "target.** Nothing schedules production to fill it: a bigger "
            "display is somewhere to put stock, never a reason to make more, "
            "and par does not move when the shop gets a new rack.\n\n"
            "What it is for is telling the Sunday close where the backstock "
            "ends. `number_on_hand` counts display and backstock together, so "
            "`number_on_hand <= display_slots` is the app saying the bag is "
            "empty — which is exactly when the crew should be holding this "
            "product's kanban tag. Zero means this never goes on display, so "
            "no tag will ever come up for it and the close leaves it alone."
        ),
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
    def backstock(self) -> int:
        """What the app believes is still in the bag, never stored.

        `number_on_hand` is the *total* — what is hanging on the display and
        what is in the bag behind it, together — and that is deliberate.
        Storing a backstock number instead would let the shop's own furniture
        order dye baths: fill a new rack from the bags and a backstock tracker
        reads empty and calls for production, when nothing sold and the stock
        simply moved across the stall. Deriving it means moving a skein from
        bag to peg changes nothing anywhere.

        So this is a reading, not a record. It is what the Sunday close's
        expected list is built on, and nothing else should treat it as a
        trigger.
        """
        return max(self.number_on_hand - self.display_slots, 0)

    @property
    def display_hole(self) -> int:
        """How many homes on the display this can't fill. Nothing reads it yet.

        `number_on_hand < display_slots` means there is a bare peg or an empty
        spot on the pole, and once the display is mapped that is answerable
        from a desk instead of by walking the stall — which is the whole
        reason the capacity is recorded rather than remembered.

        **It is a merchandising reading, never a production trigger.** A hook
        that holds four exists precisely so that three is allowed to be
        enough; wiring this to a dye bath would put display size back on the
        path to production, which is the coupling the close was rebuilt to
        remove. Par decides what gets made, and par is about demand.
        """
        return max(self.display_slots - self.number_on_hand, 0)

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

    #: Which part of the app wrote this row. Provenance was already being
    #: recorded — in `notes`, as a readable English sentence — and that is
    #: worth keeping, because a person reading one row wants the sentence.
    #: But counting them is a different question, and answering it off prose
    #: means a `LIKE` over wording that was never promised to stay still: one
    #: reworded message drops rows out of a total with nothing to show it
    #: happened. The rates are the point of the Sunday close (see CloseRun),
    #: so the axis they are counted on has to be a field.
    SOURCE_PRODUCTION_SHEET = "production_sheet"
    SOURCE_PRODUCTION_NEEDED = "production_needed"
    SOURCE_RECIPE_PAGE = "recipe_page"
    SOURCE_CARD_BACKFILL = "card_backfill"
    SOURCE_BULK_UPDATE = "bulk_update"
    SOURCE_SUNDAY_CLOSE = "sunday_close"
    SOURCE_RESTOCK = "restock"
    SOURCE_FANCY_CONVERSION = "fancy_conversion"
    SOURCE_SQUARE_WEBHOOK = "square_webhook"
    SOURCE_SQUARE_IMPORT = "square_import"
    SOURCE_UNMATCHED_SALE = "unmatched_sale"
    SOURCE_TEST = "test"
    SOURCE_CHOICES = [
        (SOURCE_PRODUCTION_SHEET, "Production sheet"),
        (SOURCE_PRODUCTION_NEEDED, "Production-needed page"),
        (SOURCE_RECIPE_PAGE, "Recipe page"),
        (SOURCE_CARD_BACKFILL, "Kanban card backfill"),
        (SOURCE_BULK_UPDATE, "Bulk inventory update"),
        (SOURCE_SUNDAY_CLOSE, "Sunday close"),
        (SOURCE_RESTOCK, "Restocking the display"),
        (SOURCE_FANCY_CONVERSION, "Converted to fancy"),
        (SOURCE_SQUARE_WEBHOOK, "Square webhook"),
        (SOURCE_SQUARE_IMPORT, "Square sales import"),
        (SOURCE_UNMATCHED_SALE, "Unidentified sale, resolved"),
        (SOURCE_TEST, "Simulated (fake_sale)"),
    ]
    source = models.CharField(
        max_length=30,
        choices=SOURCE_CHOICES,
        blank=True,
        db_index=True,
        help_text=(
            "Which flow wrote this row. Blank means it predates the field — "
            "not that nobody knows, since the notes usually say. Left blank "
            "rather than back-filled by pattern-matching those notes, because "
            "a guessed provenance counts identically to a recorded one and "
            "there is nothing on the row to say which it was."
        ),
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
    pass_pdf = models.FileField(
        upload_to="crew_passes/",
        blank=True,
        help_text=(
            "This person's faire pass, as the PDF that gets handed out. It is "
            "served by `secret/handbook/` once they have read the page, which "
            "is the only reason the handbook has a name and a PIN on it at "
            "all — not to guard the pass, but to know whose to hand over. "
            "Blank tells them to contact Michael rather than showing a dead "
            "button."
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
        (REASON_UNIDENTIFIED, "A colorway nobody could identify"),
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


class DisplayFixture(models.Model):
    """One piece of furniture the stock hangs on: a pegboard, a scarf pole.

    A grid, plus how many units one position holds. That is genuinely all the
    shape there is — the yarn board is a rectangle of pegs and every peg takes
    the same number of skeins, so a row/column pair addresses a position and
    `capacity_per_position` says what fits on it.

    **The capacity is a ceiling, not a target.** Swapping two-skein hooks for
    four-skein ones doubles what the board can absorb and must change nothing
    about how much gets dyed — see `FinishedProduct.display_slots` and the
    northstar in CLAUDE.md. It is one number on one row precisely so that the
    change is cheap and obviously has no other consequences.

    Orientation is data. Whether the board reads 6×7 or 7×6 is a thing to
    check against the wall, not a thing to decide in code.
    """

    name = models.CharField(max_length=120, unique=True)
    rows = models.PositiveIntegerField(default=7)
    columns = models.PositiveIntegerField(default=6)
    capacity_per_position = models.PositiveIntegerField(
        default=2,
        help_text=(
            "How many units one peg or spot holds. Two-skein hooks today. "
            "Capacity, never a production target."
        ),
    )
    raw_product = models.ForeignKey(
        "RawProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="display_fixtures",
        help_text=(
            "The blank this board is for. In the shop a board tends to carry "
            "one product in all its colorways, and naming it here is what "
            "lets the board say which of that blank's colorways aren't up "
            "anywhere — the only gap worth fussing about, and one a global "
            "list can't express.\n\n"
            "**A lens, not a rule.** It never restricts what can be hung: a "
            "board that ends up carrying a stray from another blank is a "
            "thing that happens on a stall, and an app that refused it would "
            "be arguing with the shelf. Blank means a mixed board, which "
            "simply has no such report."
        ),
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def ensure_positions(self):
        """Every cell of the grid has a row in the table. Adds only.

        A fixture without positions is a board that cannot be used: the grid
        renders as dashes, the editor offers no dropdowns, and there is
        nothing to hang a colorway on. That was survivable while boards were
        only ever built by `seed_display_board`, and it stopped being so the
        moment one got made in the admin — which is the obvious thing to do
        and produced a board that silently did nothing.

        So the pegs follow the fixture, wherever it is created from. Called
        by a `post_save` signal rather than from each site, for the same
        reason `mirror_passthrough_stock` is: a route that forgot would
        produce a board that looks fine in a list and is dead when opened.

        Adds only. Positions outside a shrunken grid are left alone rather
        than deleted — one of them may have a colorway on it, and quietly
        dropping that is worse than carrying a row `grid()` never reads.
        """
        existing = set(self.positions.values_list("row", "column"))
        missing = [
            DisplayPosition(fixture=self, row=row, column=column)
            for row in range(1, self.rows + 1)
            for column in range(1, self.columns + 1)
            if (row, column) not in existing
        ]
        if missing:
            DisplayPosition.objects.bulk_create(missing, ignore_conflicts=True)
        return len(missing)

    def grid(self):
        """Rows of positions, in reading order, with the gaps filled in.

        A fixture's positions are rows in the database and a board is a
        rectangle, so the two have to be reconciled somewhere. Doing it here
        means the template never has to reason about a missing peg — an
        unassigned position and one nobody has created yet look the same to
        it, which is right, because on the wall they are the same empty hook.
        """
        by_cell = {(p.row, p.column): p for p in self.positions.all()}
        return [
            [by_cell.get((r, c)) for c in range(1, self.columns + 1)]
            for r in range(1, self.rows + 1)
        ]


class DisplayPosition(models.Model):
    """One peg. What lives there, or what the space is used for instead.

    Three states, and telling the last two apart is the whole reason
    `reserved_label` exists rather than a bare null product:

    - **assigned** — a colorway lives here and the restock walk asks about it
    - **empty** — a real home with nothing assigned to it yet, which is a gap
      in the map and worth seeing
    - **reserved** — not a home at all. The price tag sits in the middle of the
      top row, and a space taken up by signage must never read as a colorway
      nobody has got round to assigning.

    `on_delete=SET_NULL` because a position is configuration, not history.
    Retiring a colorway empties its peg; it does not protect it. That is the
    one place the "retire, don't delete" rule doesn't apply, because there is
    nothing here anybody would want to read back later — the wall moved on.
    """

    fixture = models.ForeignKey(
        DisplayFixture,
        on_delete=models.CASCADE,
        related_name="positions",
    )
    row = models.PositiveIntegerField()
    column = models.PositiveIntegerField()
    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="display_positions",
    )
    reserved_label = models.CharField(
        max_length=60,
        blank=True,
        help_text=(
            "Set when this space isn't a home — 'Price tag', signage. Blank "
            "means a real peg. A reserved position is never restocked, never "
            "counted, and never contributes display capacity."
        ),
    )

    class Meta:
        ordering = ["fixture", "row", "column"]
        constraints = [
            models.UniqueConstraint(
                fields=["fixture", "row", "column"],
                name="one_thing_per_display_position",
            ),
        ]

    def __str__(self):
        where = f"{self.fixture.name} r{self.row}c{self.column}"
        if self.reserved_label:
            return f"{where} ({self.reserved_label})"
        return f"{where}: {self.finished_product or 'empty'}"

    @property
    def is_home(self) -> bool:
        """A real peg a colorway can live on."""
        return not self.reserved_label


def sync_display_slots(product):
    """Write a product's display capacity from the map it appears on.

    **The map is the source and `display_slots` is what everything reads.**
    One writer, called whenever an assignment changes, rather than two numbers
    that agree until somebody edits one of them. The same bargain `save()`
    makes with SKUs: derived once, stored, and read everywhere without the
    reader needing to know where it came from.

    A product on no fixture is left exactly as it is rather than zeroed. Zero
    means "never goes on display", which would quietly drop it off the Sunday
    close — and "nobody has mapped this yet" is not the same claim as "this
    never goes out". The map page lists those instead, loudly, because a
    silently unmapped colorway is one the close stops asking about.
    """
    if product is None:
        return
    slots = sum(
        position.fixture.capacity_per_position
        for position in product.display_positions.select_related("fixture")
        if position.is_home and position.fixture.is_active
    )
    if slots and product.display_slots != slots:
        FinishedProduct.objects.filter(pk=product.pk).update(display_slots=slots)
        product.display_slots = slots


class RestockPass(models.Model):
    """One walk down a fixture, and somebody's name on the result.

    **This is a repeatable promise that a task was complete**, not an audit.
    It happens at open — where the week's production physically enters the
    display — and again at close, and at minimum at the end of every shift.
    The job is that the display is full; the reconciliation the Sunday close
    does is a different question with a different lifespan.

    Which is why this is not day-scoped the way `CloseRun` is. A close is one
    per evening because two would split one night's findings; a restock is as
    many as there were shifts, and each is its own completed promise.

    Unlike a `ProductionRun` this is worth keeping. A run is scaffolding and
    the `InventoryLog` is what survives it — true here too for the *stock* —
    but "was the board full when we opened?" is a real question about how the
    day went, and only the passes can answer it.
    """

    fixture = models.ForeignKey(
        DisplayFixture,
        on_delete=models.CASCADE,
        related_name="restock_passes",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="restock_passes",
        help_text="Whose PIN signed the promise.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_full = models.BooleanField(
        default=False,
        help_text=(
            "Every peg on the board was answered in this pass. Expected at "
            "open and at close, and worth naming because of what it buys: "
            "afterwards *every* position has a fresh baseline, so everything "
            "the board predicts is trustworthy. After a partial pass some of "
            "it isn't.\n\n"
            "Frozen rather than derived, because the board gets colorways "
            "hung on it and a pass that covered everything at the time must "
            "keep saying so.\n\n"
            "This recognises completeness; nothing anywhere penalises the "
            "lack of it. A pass covering nine pegs is a completed piece of "
            "work, not a failed full check."
        ),
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        kind = "fully restocked" if self.is_full else "restocked"
        return f"{self.fixture.name} {kind} {self.created_at:%d %b %Y %H:%M}"


class RestockCheck(models.Model):
    """One peg, on one pass: what was expected there and what was really there.

    Most of these say "as predicted" and move nothing, which is the point —
    the ordinary answer has to be one tap or the walk stops happening. The
    two exceptions are where stock moves, and they are mirror images:

    - **short** — the peg couldn't be filled and the bag behind it is empty,
      so the app was over. The worked case: app says 3, one on the peg, none
      in the bag, and −2 puts it right.
    - **over** — the peg filled when the app said it couldn't, so the app was
      under. Worth the quick look precisely because the app predicted a gap
      and predicting gaps wrongly is how a colorway stops being offered.

    Row and column are frozen alongside the position, because the map gets
    rebuilt and a history that re-reads it would relocate things that already
    happened. Same reasoning as the production sheet freezing its bath size.
    """

    AS_PREDICTED = "as_predicted"
    SHORT = "short"
    OVER = "over"
    RESULT_CHOICES = [
        (AS_PREDICTED, "As predicted — filled as far as the app said it could"),
        (SHORT, "Couldn't fill it — the app was over"),
        (OVER, "Filled it anyway — the app was under"),
    ]

    restock_pass = models.ForeignKey(
        RestockPass,
        on_delete=models.CASCADE,
        related_name="checks",
    )
    position = models.ForeignKey(
        DisplayPosition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checks",
    )
    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.PROTECT,
        related_name="restock_checks",
    )
    row = models.PositiveIntegerField()
    column = models.PositiveIntegerField()
    result = models.CharField(
        max_length=20,
        choices=RESULT_CHOICES,
        default=AS_PREDICTED,
    )
    expected = models.PositiveIntegerField(
        help_text=(
            "What the app said could go on this peg — `min(stock, capacity)`, "
            "frozen. A peg the app knew was empty expects zero, and "
            "confirming that is a completed job rather than a failure."
        ),
    )
    counted = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="The product's true total. Only an exception asks for one.",
    )
    applied_log = models.ForeignKey(
        InventoryLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "The adjustment this check caused, if it caused one. Set once — "
            "the page gets reopened and the button gets double-tapped."
        ),
    )

    class Meta:
        ordering = ["restock_pass", "row", "column"]
        constraints = [
            models.UniqueConstraint(
                fields=["restock_pass", "row", "column"],
                name="one_restock_check_per_peg",
            ),
        ]

    def __str__(self):
        return f"r{self.row}c{self.column} {self.finished_product}: {self.result}"


class CloseRun(models.Model):
    """One Sunday-night close: the app's empty bags, checked against the tags.

    The crew keep a product's kanban tag when the last of it comes out of the
    bag and goes onto the display. That is a statement about the **bag**, not
    about the shelf — there are still one to three units hanging on the pegs —
    and reading it as "we have none" is the mistake this model was rebuilt to
    stop making. `number_on_hand` counts display and backstock together, so
    the app's own version of the same statement is
    `number_on_hand <= display_slots`. Two systems, one physical pile, and the
    close is where they are read against each other while the van is still
    being loaded.

    Every answered row is a **count**, and the count is the total: fill the
    display, then count what is left in the bag. A tag in hand just means the
    second half is nothing, so the answer is a number between zero and the
    display's capacity and takes about as long as a tick did.

    **The failures are the deliverable, and that is what makes this a record.**
    A `ProductionRun` is deliberately scaffolding: it is how paper and phone
    found each other, and what survives it is the `InventoryLog`. Here the
    count of things found wrong *is* the output — "we fixed ten this weekend"
    is the number that means something, and it means it on its own. There are
    no points for the rows that agreed.

    Which is why nothing here computes a rate. The agreements are stored
    because the flow needs them — a tick is how a row stops coming back at
    somebody working down a pile they may not finish in one go — and not
    because they are half of a ratio. Reporting `4 / 50` would put the
    reassuring number next to the one worth acting on, and the reassuring one
    is not being graded.

    **One run per calendar day, and yesterday's is finished.** The day is the
    boundary rather than a button somebody presses, because the button is
    exactly what doesn't get pressed: the van is loaded, the phone goes in a
    pocket, and a run left open forever is indistinguishable from one that
    found nothing. Reopening the page on the same day lands back in the same
    run and picks up where it stopped, which is what the close actually needs
    — it happens in a car park in the dark and gets interrupted. Come back
    tomorrow and that day is a record: nothing on it can be ticked, counted or
    adjusted any more, and a correction to it goes through the ordinary
    inventory adjustment route with a reason attached, where it belongs.
    """

    day = models.DateField(
        unique=True,
        help_text=(
            "The day this close covers. Unique, because a second run for the "
            "same evening is the same evening — two rows would split one "
            "session's findings across both and make either one readable as "
            "the whole night's work."
        ),
    )
    token = models.CharField(
        max_length=32,
        unique=True,
        default=new_run_token,
        help_text=(
            "This close's code, in the URL. The PIN is checked once, when the "
            "day's run is opened; the token is what carries the steps after "
            "it, so packing up doesn't mean typing four digits four times."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="close_runs",
        help_text="Whoever's PIN opened the day's close.",
    )

    class Meta:
        ordering = ["-day"]

    def __str__(self):
        return f"Close {self.day:%d %b %Y} ({self.token})"

    @property
    def is_open(self) -> bool:
        """Today's run takes answers. Every other day is a record."""
        return self.day == timezone.localdate()


class CloseRunRow(models.Model):
    """One product on one close: what the app believed, and what was counted.

    **Every answered row carries a number, and the number is the total** —
    what is hanging on the display plus what is left in the bag once the
    display has been filled. The kanban tag is not the answer any more; it is
    what puts the product in front of somebody. Holding it means the bag is
    empty, so the count is bounded by `display_slots` and takes seconds.

    That is the pivot this model exists after. The close used to read a tag
    in hand as "set it to zero", which quietly deleted the one to three units
    still hanging on the pegs — and once display stock is invisible, every
    rack the shop adds reads as a sale and calls for a dye bath. Counting the
    display instead means stock moving from bag to peg changes nothing.

    The outcome is then just the sign of `counted - on_hand_before`, and the
    two disagreements point at opposite ends of the pipeline. **Missing** is
    the app undercounting — stock that arrived without being recorded.
    **Extra** is the app overcounting, which is stock that left without
    registering: a swapped sale, a hand-keyed line, or a webhook that has
    quietly stopped delivering. That last one is worth the page on its own,
    because a dropped sale physically becomes a tag in somebody's hand about
    a week later, and this finds it without cross-checking Square at all.

    Whether the row was *predicted* is a separate axis — see `added_by_tag`.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    MISSING = "missing"
    EXTRA = "extra"
    OUTCOME_CHOICES = [
        (PENDING, "Not counted yet"),
        (CONFIRMED, "Counted — the app agreed"),
        (MISSING, "Counted more than the app had — app undercounts"),
        (EXTRA, "Counted less than the app had — app overcounts"),
    ]

    run = models.ForeignKey(
        CloseRun,
        on_delete=models.CASCADE,
        related_name="rows",
    )
    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.PROTECT,
        related_name="close_rows",
    )
    outcome = models.CharField(
        max_length=20,
        choices=OUTCOME_CHOICES,
        default=PENDING,
    )
    on_hand_before = models.PositiveIntegerField(
        help_text=(
            "What the app believed when this row was made. Frozen for the "
            "same reason a production row freezes its bath size: it is what "
            "the disagreement was measured against, and re-reading it later "
            "reads back the number this close already corrected."
        ),
    )
    counted = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "The true total found in hand — everything on display plus "
            "whatever is left in the bag after the display was filled. Every "
            "answered row has one; the outcome is the sign of "
            "`counted - on_hand_before`."
        ),
    )
    display_slots = models.PositiveIntegerField(
        default=0,
        help_text=(
            "What the display held when this row was made. Frozen for the "
            "same reason the production sheet freezes its bath size: the "
            "person answered '0, 1 or 2?' because that is what the paper and "
            "the pegs said that night, and re-reading it later reads back a "
            "display that has since been rebuilt."
        ),
    )
    added_by_tag = models.BooleanField(
        default=False,
        help_text=(
            "This row was not predicted — somebody was holding a tag the "
            "close didn't ask about. Kept apart from the *outcome*, which "
            "records which way the app was wrong: an unpredicted tag usually "
            "means an overcount but doesn't have to, and a predicted row can "
            "come out over too. Conflating the two put the wrong thing in the "
            "one number this page produces."
        ),
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_log = models.ForeignKey(
        InventoryLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "The adjustment this row caused, if it caused one. Set once and "
            "never again — the page is reopened, the button is double-tapped, "
            "and somebody who remembers one more tag comes back to it. Same "
            "failure as a redelivered Square order, same fix."
        ),
    )

    class Meta:
        ordering = ["run", "finished_product__sku", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "finished_product"],
                name="one_close_row_per_product",
            ),
        ]

    def __str__(self):
        return f"{self.finished_product.sku or self.finished_product.name}: {self.outcome}"

    @property
    def is_applied(self) -> bool:
        return self.applied_log_id is not None

    @property
    def is_answered(self) -> bool:
        return self.outcome != self.PENDING


class Faire(models.Model):
    """One year's run of the faire.

    Deliberately thin: a faire is an event, a year, and a way of knowing its
    days. `rule` says which — the Labor Day run generates them from
    `scarves/seasons.py`, and a faire whose dates are announced rather than
    derived is `manual` and has them entered.

    **`slug` is the event across years and `year` is the instance**, which is
    what makes "this faire against the same faire last year" a query. A second
    event — a spring faire, somewhere else entirely — is a new slug and
    changes nothing about this one.

    **Comparison never crosses slugs, and no report should offer it.** Week 1
    of one faire against week 1 of another is not a question anybody here
    asks, and it is not a question the number could answer: a weekend index
    counts position within *that* run, so two faires of different lengths,
    seasons and audiences share nothing but the integer. Scope every
    comparison to one slug and let the years vary.

    A year with no faire — 2020 — is simply a row that does not exist. That is
    better than a row marked cancelled, because every query that walks faires
    then excludes it by construction instead of by remembering to.
    """

    slug = models.SlugField(
        max_length=50,
        help_text="The event, stable across years — what a season-over-season comparison groups on.",
    )
    year = models.PositiveIntegerField(
        help_text="The calendar year this instance of it falls in.",
    )
    name = models.CharField(
        max_length=120,
        blank=True,
        help_text="Optional label. Blank reads as the slug and the year.",
    )
    rule = models.CharField(
        max_length=20,
        choices=seasons.RULE_CHOICES,
        default=seasons.LABOR_DAY_RULE,
        help_text=(
            "How this faire's days are known. A generated rule can be re-run "
            "for any year; a manual one has its dates entered, and "
            "`generate_faire` refuses it rather than inventing a pattern."
        ),
    )
    latitude = models.DecimalField(
        max_digits=8, decimal_places=5, null=True, blank=True,
        help_text="Where the faire is, for the weather fetch. Roughly is fine — the archive grid is about nine kilometres.",
    )
    longitude = models.DecimalField(
        max_digits=8, decimal_places=5, null=True, blank=True,
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-year", "slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["slug", "year"],
                name="one_faire_per_event_per_year",
            ),
        ]

    def __str__(self):
        return self.name or f"{self.slug} {self.year}"

    @property
    def trading_days(self) -> int:
        """Days that actually traded — the denominator for any per-day figure."""
        return self.days.filter(traded=True).count()


class FaireDay(models.Model):
    """One trading day, carrying the weekend it belongs to.

    The weekend number is the axis every season-over-season comparison runs
    on, because Labor Day moves and so the calendar dates do not line up
    between years.

    `date` is unique across all faires, not just within one. That is a real
    constraint rather than a convenience: the booth is in one place at a time,
    so a date cannot belong to two faires, and making it a database fact means
    a sale is placed by date alone without first deciding which event to ask.
    If a second faire ever genuinely overlaps this one, the insert should fail
    loudly — being in two places is the thing that needs a decision, not a
    default.
    """

    faire = models.ForeignKey(
        Faire,
        on_delete=models.CASCADE,
        related_name="days",
    )
    date = models.DateField(unique=True)
    weekend = models.PositiveSmallIntegerField(
        help_text="Which weekend of the run, 1-based.",
    )
    is_labor_day = models.BooleanField(
        default=False,
        help_text=(
            "The Monday. It always lands in weekend 2, which is why that "
            "weekend has three trading days and every per-weekend total for "
            "it reads about a third high."
        ),
    )
    traded = models.BooleanField(
        default=True,
        help_text=(
            "Untick for a day the faire did not open. Three days in "
            "twenty-two years — a washed-out weekend in 2023 and one "
            "hurricane — so there is deliberately no workflow around this, "
            "just this checkbox. It matters because it is the denominator: "
            "counting 2023's washout as two traded days moves that season's "
            "per-day figure by nearly 12%."
        ),
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(
                fields=["faire", "date"],
                name="one_faire_day_per_date",
            ),
        ]

    def __str__(self):
        return f"{self.date:%a %d %b %Y} (weekend {self.weekend})"


class Sale(models.Model):
    """One order rung up at the till.

    **This is not `InventoryLog` and must never become it.** The two answer
    different questions and only one of them can answer this one:

    - `InventoryLog.created_at` is when the row was *written*, not when the
      sale happened — a CSV loaded on Monday piles Saturday onto Monday. Here
      `sold_at` is Square's own timestamp, which is what makes hour-of-day and
      weekend-over-weekend comparison possible at all.
    - A line `InventoryLog` cannot identify never reaches it; it goes to
      `UnmatchedSale`. A revenue total built on it is silently short.
    - There is no money on an `InventoryLog` row.
    - Its product FK is `PROTECT`, so a line item from a season before this
      app existed could never live there.

    So this ledger sits beside that one and **never moves stock**. Nothing here
    writes `number_on_hand`, and nothing here writes an `InventoryLog` row.
    """

    SOURCE_SQUARE_CSV = "square_csv"
    SOURCE_SQUARE_API = "square_api"
    SOURCE_APP = "app"
    SOURCE_CHOICES = [
        (SOURCE_SQUARE_CSV, "Square itemised CSV export"),
        (SOURCE_SQUARE_API, "Square Orders API"),
        (SOURCE_APP, "This app's own records"),
    ]

    order_id = models.CharField(
        max_length=100,
        unique=True,
        help_text="Square's Transaction ID, which is the order id.",
    )
    sold_at = models.DateTimeField(
        db_index=True,
        help_text="When Square says it happened, never when we imported it.",
    )
    location = models.CharField(max_length=120, blank=True)
    device = models.CharField(max_length=120, blank=True)
    customer_name = models.CharField(max_length=200, blank=True)
    card_brand = models.CharField(max_length=40, blank=True)
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        db_index=True,
        help_text=(
            "Which pipeline supplied this row. Square is today's writer, not "
            "the schema — when this app's own records take over, that is one "
            "new writer and no new reports, because nothing downstream "
            "branches on this. It is printed as a breakdown, the way "
            "`InventoryLog.source` already is, never used as a filter that "
            "changes what a total means."
        ),
    )
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sold_at"]

    def __str__(self):
        return f"{self.order_id} on {timezone.localtime(self.sold_at):%d %b %Y, %H:%M}"


class SaleLine(models.Model):
    """One line of an order: an item, a price point, a quantity and its money.

    **Identity is `item` plus `price point`, not the SKU.** Twenty of the
    thirty-six lines in a 2026 itemised export carry no SKU at all, and the
    seasons before that are worse — so a SKU-keyed importer would drop most of
    the history without saying so. Item is the style and price point is the
    colorway, and both are present on every row of every season.

    **It is also not Square's `Token` column**, which looks like a line
    identifier and is not: the same token repeats across orders for the same
    product, so keying on it collapses three separate sales of a triangle
    fringe into one. That mistake is silent and costs revenue off the total.

    The product links are `SET_NULL` rather than `PROTECT`, which is the
    opposite of what `InventoryLog` does, and deliberately. There the log is
    *about* a product, so losing the product would strand the row. Here the
    row carries `item_name` and `price_point` as text and means something
    without any link at all — the link is enrichment, so it is allowed to go.
    """

    PAYMENT = "payment"
    REFUND = "refund"
    EVENT_CHOICES = [
        (PAYMENT, "Payment"),
        (REFUND, "Refund"),
    ]

    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    line_key = models.CharField(
        max_length=300,
        help_text=(
            "Item, price point and an occurrence counter — what makes this "
            "line unique inside its order, and what re-importing the same "
            "export matches on."
        ),
    )
    sold_at = models.DateTimeField(
        db_index=True,
        help_text="Copied from the order so hour-of-day queries need no join.",
    )
    event_type = models.CharField(
        max_length=20,
        choices=EVENT_CHOICES,
        default=PAYMENT,
        db_index=True,
    )

    category = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        help_text=(
            "Square's category. Load-bearing for season-over-season totals: "
            "the wax hands were on this till through 2024 and are gone now, "
            "so a total that does not name its categories reads a "
            "discontinued line as a decline."
        ),
    )
    item_name = models.CharField(max_length=255, db_index=True)
    price_point = models.CharField(
        max_length=255,
        blank=True,
        help_text="The colorway, where one was rung up. 'Regular Price' means none was.",
    )
    sku = models.CharField(max_length=64, blank=True, db_index=True)
    square_variation_id = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text=(
            "Square's own id for the variation sold, where the source carried "
            "one — the Orders API does, the CSV export does not. Kept because "
            "it is the only durable handle on a line: item names get edited "
            "and catalogue objects get deleted, and this is what lets a "
            "category be resolved again later without re-fetching every order."
        ),
    )

    quantity = models.DecimalField(max_digits=9, decimal_places=2)
    gross_cents = models.IntegerField(default=0)
    discount_cents = models.IntegerField(default=0)
    net_cents = models.IntegerField(default=0)
    tax_cents = models.IntegerField(default=0)

    finished_product = models.ForeignKey(
        FinishedProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sale_lines",
    )
    raw_product = models.ForeignKey(
        RawProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sale_lines",
        help_text="The blank, matched off the item name. Coarser than the SKU and available for every season.",
    )

    source = models.CharField(
        max_length=20,
        choices=Sale.SOURCE_CHOICES,
        db_index=True,
    )
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-sold_at", "item_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["sale", "line_key"],
                name="one_sale_line_per_key",
            ),
        ]

    def __str__(self):
        return f"{self.quantity:g} × {self.item_name} ({self.price_point or 'no colorway'})"

    @property
    def net(self):
        """Net sales as a Decimal of dollars."""
        return Decimal(self.net_cents) / 100


class DayWeather(models.Model):
    """What the sky did on one trading day.

    Your sister already reads this row — the site this replaces carried it by
    hand — and the reason it is imported rather than pasted is written in that
    site's own data: its 2022 and 2023 weather are byte-identical copies of
    2021's, and 2024 has none at all. A row typed nine times a season stops
    being typed.

    **Fetched once, after the fact, and stored.** Nothing reads the network
    when a page renders: a report that makes an HTTP call is a report that
    sometimes does not render, and the weather on a weekend three years ago is
    not going to change.

    Cascades from its day for the same reason a product image cascades from
    its product — it is a description of that day, and with the day gone there
    is nothing left to describe.
    """

    day = models.OneToOneField(
        FaireDay,
        on_delete=models.CASCADE,
        related_name="weather",
    )
    high_f = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    low_f = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    mean_f = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    precipitation_in = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
    )
    cloud_pct = models.DecimalField(
        max_digits=5, decimal_places=1, null=True, blank=True,
        help_text=(
            "Mean cloud cover over opening hours, not over the whole day. "
            "Fog at four in the morning is not weather anybody stood in."
        ),
    )
    humidity_pct = models.DecimalField(
        max_digits=5, decimal_places=1, null=True, blank=True,
        help_text="Mean relative humidity over opening hours.",
    )
    source = models.CharField(
        max_length=60,
        default="open-meteo",
        help_text="Where the reading came from, so a re-sourced season is visible.",
    )
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["day__date"]
        verbose_name_plural = "day weather"

    def __str__(self):
        return f"{self.day.date:%d %b %Y}: {self.mean_f}°F"

    @property
    def was_wet(self):
        """Enough rain to be worth noticing on a chart.

        A hundredth of an inch is a passing shower nobody remembers; a tenth
        is the day the stall covers went up.
        """
        return self.precipitation_in is not None and self.precipitation_in >= Decimal("0.1")
