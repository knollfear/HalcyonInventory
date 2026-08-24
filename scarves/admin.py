import difflib
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, Max, Q
from django.template.response import TemplateResponse

logger = logging.getLogger(__name__)

from .models import (
    UNCATEGORIZED_BRAND,
    BoothPhoto,
    CatalogGroup,
    DyeBrand,
    Dye,
    Employee,
    LabelStock,
    UnmatchedSale,
    RawProductCategory,
    RawProduct,
    Recipe,
    RecipeDye,
    FinishedProduct,
    FinishedProductImage,
    CloseRun,
    CloseRunRow,
    InventoryLog,
    ProductImageUpload,
    ProductionRun,
    ProductionRunRow,
    TimeEntry,
)


def _fetch_square_catalog():
    from square.client import Client
    client = Client(
        access_token=settings.SQUARE_ACCESS_TOKEN,
        environment=settings.SQUARE_ENVIRONMENT,
    )
    items = []
    cursor = None
    while True:
        result = client.catalog.list_catalog(cursor=cursor, types="ITEM")
        if result.is_error():
            break
        items.extend(result.body.get("objects", []))
        cursor = result.body.get("cursor")
        if not cursor:
            break
    return items


def _match_score(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _rank_square_items(rp_name, square_items, taken_ids):
    scored = []
    for item in square_items:
        sq_name = item["item_data"]["name"]
        score = _match_score(rp_name, sq_name)
        scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    return scored


def preview_square_match(modeladmin, request, queryset):
    if "apply" in request.POST:
        saved = 0
        for rp in queryset:
            item_id = request.POST.get(f"item_{rp.pk}", "").strip()
            if item_id:
                rp.square_item_id = item_id
                rp.save(update_fields=["square_item_id"])
                saved += 1

        messages.success(
            request,
            f"Saved Square item IDs for {saved} raw product(s). "
            "Run sync_to_square to create color variations in Square."
        )
        return None

    square_items = _fetch_square_catalog()
    taken_item_ids = set(
        RawProduct.objects.exclude(square_item_id="").values_list("square_item_id", flat=True)
    )
    taken_var_ids = set(
        FinishedProduct.objects.exclude(square_variation_id="").values_list("square_variation_id", flat=True)
    )

    matches = []
    for rp in queryset.prefetch_related("finished_products__recipe"):
        ranked = _rank_square_items(rp.name, square_items, taken_item_ids)
        best_item = ranked[0][1] if ranked else None

        # Pre-compute a display price for each alternative from its variations
        alternatives = []
        for _, item in ranked[:10]:
            variations = item["item_data"].get("variations", [])
            prices = [
                v["item_variation_data"].get("price_money", {}).get("amount", 0)
                for v in variations
            ]
            unique_prices = sorted(set(prices))
            if len(unique_prices) == 1:
                item["price_display"] = f"${unique_prices[0] / 100:.2f}"
            elif len(unique_prices) > 1:
                item["price_display"] = f"${unique_prices[0] / 100:.2f}–${unique_prices[-1] / 100:.2f}"
            else:
                item["price_display"] = "no price"
            alternatives.append(item)

        variation_matches = []
        if best_item:
            sq_variations = best_item["item_data"].get("variations", [])
            # Pre-format prices as dollars for display
            for v in sq_variations:
                cents = v["item_variation_data"].get("price_money", {}).get("amount", 0)
                v["item_variation_data"]["price_dollars"] = f"{cents / 100:.2f}"

            for fp in rp.finished_products.filter(is_active=True):
                var_ranked = sorted(
                    sq_variations,
                    key=lambda v: -_match_score(
                        fp.recipe.name,
                        v["item_variation_data"].get("name", "")
                    )
                )
                best_var = var_ranked[0] if var_ranked else None
                variation_matches.append({
                    "fp": fp,
                    "best_var": best_var,
                    "all_vars": sq_variations,
                })

        matches.append({
            "rp": rp,
            "best_item": best_item,
            "alternatives": alternatives,
            "variation_matches": variation_matches,
        })

    return TemplateResponse(request, "admin/scarves/match_square_preview.html", {
        "matches": matches,
        "queryset": queryset,
        "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        "opts": modeladmin.model._meta,
    })


preview_square_match.short_description = "Match to Square catalog (preview)"


def bulk_update_finished_price(modeladmin, request, queryset):
    if "apply" in request.POST:
        raw_price = request.POST.get("new_price", "").strip()
        try:
            new_price = Decimal(raw_price)
            if new_price <= 0:
                raise InvalidOperation
        except InvalidOperation:
            messages.error(request, f"Invalid price: '{raw_price}'")
            return None

        updated = FinishedProduct.objects.filter(
            raw_product__in=queryset, is_active=True
        ).update(price=new_price)

        messages.success(
            request,
            f"Updated {updated} finished product(s) to ${new_price:.2f} across "
            f"{queryset.count()} raw product(s).",
        )
        return None

    return TemplateResponse(request, "admin/scarves/bulk_price.html", {
        "queryset": queryset,
        "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        "opts": modeladmin.model._meta,
    })


bulk_update_finished_price.short_description = "Bulk update finished product prices"


def bulk_update_finished_par(modeladmin, request, queryset):
    """
    Set `par` on every active finished product made from the selected raw
    product(s). Par is the trigger for production, so raising it here is how
    you ask for more of everything in a blank — see the recipe production page.
    """
    if "apply" in request.POST:
        raw_par = request.POST.get("new_par", "").strip()
        try:
            new_par = int(raw_par)
            if new_par < 0:
                raise ValueError
        except ValueError:
            messages.error(request, f"Invalid par: '{raw_par}'")
            return None

        updated = FinishedProduct.objects.filter(
            raw_product__in=queryset, is_active=True
        ).update(par=new_par)

        messages.success(
            request,
            f"Set par to {new_par} on {updated} finished product(s) across "
            f"{queryset.count()} raw product(s).",
        )
        return None

    rows = []
    for rp in queryset.annotate(
        active_count=Count(
            "finished_products",
            filter=Q(finished_products__is_active=True),
        ),
    ):
        current = sorted(
            set(
                rp.finished_products.filter(is_active=True)
                .values_list("par", flat=True)
            )
        )
        rows.append({
            "rp": rp,
            "active_count": rp.active_count,
            "current_par": ", ".join(str(p) for p in current) or "—",
        })

    return TemplateResponse(request, "admin/scarves/bulk_par.html", {
        "rows": rows,
        "queryset": queryset,
        "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        "opts": modeladmin.model._meta,
    })


bulk_update_finished_par.short_description = "Bulk update finished product par"


@admin.register(DyeBrand)
class DyeBrandAdmin(admin.ModelAdmin):
    list_display = ("name", "website")
    search_fields = ("name",)
    ordering = ("name",)


class NeedsReviewFilter(admin.SimpleListFilter):
    """Dyes typed in from a recipe picker and not finished off.

    A dye added mid-entry is a name and nothing else — that is the trade that
    keeps entry moving (see `NewDyeForm`). It costs nothing until somebody
    asks a colour question of it, at which point it silently contributes
    nothing: no band on the rainbow sheet, no chip on the dye-collection
    page, no palette point in the games. This is the list that gets that
    finished, and it is why the deferral is safe.
    """

    title = "needs review"
    parameter_name = "review"

    def lookups(self, request, model_admin):
        return (("yes", "No colour or no brand yet"), ("no", "Filled in"))

    def queryset(self, request, queryset):
        unfinished = Q(hex_color="") | Q(brand__name=UNCATEGORIZED_BRAND)
        if self.value() == "yes":
            return queryset.filter(unfinished)
        if self.value() == "no":
            return queryset.exclude(unfinished)
        return queryset


@admin.register(Dye)
class DyeAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "hex_color", "in_stock", "sku")
    # Editable in the list, because the cleanup this is for is a colour and a
    # brand on each of a dozen rows — a job that is one screen here and a
    # dozen round trips through the change form.
    list_editable = ("brand", "hex_color", "in_stock")
    list_filter = (NeedsReviewFilter, "brand", "in_stock")
    search_fields = ("name", "brand__name", "sku")
    ordering = ("brand__name", "name")


def match_square_categories(modeladmin, request, queryset):
    if "apply" in request.POST:
        saved = 0
        for cat in queryset:
            cat_id = request.POST.get(f"cat_{cat.pk}", "").strip()
            if cat_id:
                cat.square_category_id = cat_id
                cat.save(update_fields=["square_category_id"])
                saved += 1
        messages.success(request, f"Saved Square category IDs for {saved} category(s).")
        return None

    from square.client import Client
    client = Client(
        access_token=settings.SQUARE_ACCESS_TOKEN,
        environment=settings.SQUARE_ENVIRONMENT,
    )
    result = client.catalog.list_catalog(types="CATEGORY")
    square_cats = []
    if result.is_success():
        square_cats = sorted(
            result.body.get("objects", []),
            key=lambda c: c["category_data"]["name"],
        )

    return TemplateResponse(request, "admin/scarves/match_square_categories.html", {
        "queryset": queryset,
        "square_cats": square_cats,
        "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        "opts": modeladmin.model._meta,
    })

match_square_categories.short_description = "Match to Square categories"


@admin.register(RawProductCategory)
class RawProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "square_category_id")
    search_fields = ("name",)
    ordering = ("name",)
    actions = [match_square_categories]


@admin.register(CatalogGroup)
class CatalogGroupAdmin(admin.ModelAdmin):
    """Several raw products sold under one Square item.

    Exists for undyed stock, where the item is "Undyed Yarn" and the
    variations are the blanks — the usual blank × colorway axes swapped. See
    the model for why. Everything dyed leaves `catalog_group` blank and is its
    own item, which stays the default.
    """
    list_display = ("name", "category", "member_count", "square_item_id")
    search_fields = ("name",)
    ordering = ("name",)

    @admin.display(description="Raw products")
    def member_count(self, obj):
        return obj.raw_products.count()


@admin.register(RawProduct)
class RawProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "price",
        "number_on_hand",
        "par_level",
        "finished_par_default",
        "is_active",
        "catalog_group",
        "square_item_id",
    )
    list_editable = ("par_level", "finished_par_default")
    list_filter = ("category", "is_active", "catalog_group")
    search_fields = ("name", "category__name", "sku")
    ordering = ("category__name", "name")
    actions = [
        preview_square_match,
        bulk_update_finished_price,
        bulk_update_finished_par,
    ]


class RecipeDyeInline(admin.TabularInline):
    model = RecipeDye
    extra = 1
    autocomplete_fields = ("dye",)
    fields = ("dye", "order", "ratio")
    ordering = ("order",)


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ("name", "dye_count", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    inlines = [RecipeDyeInline]
    ordering = ("name",)


class FinishedProductImageInline(admin.TabularInline):
    model = FinishedProductImage
    extra = 1
    fields = ("image", "image_url", "alt_text", "order")
    ordering = ("order",)


@admin.register(ProductImageUpload)
class ProductImageUploadAdmin(admin.ModelAdmin):
    list_display = ("key", "status", "detected_sku", "finished_product", "created_at")
    list_filter = ("status",)
    search_fields = ("key", "detected_sku", "finished_product__name")
    readonly_fields = ("key", "created_at", "updated_at")


@admin.register(FinishedProduct)
class FinishedProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "raw_product",
        "recipe",
        "price",
        "number_on_hand",
        "par",
        "is_active",
        "created_at",
    )
    search_fields = (
        "name",
        "raw_product__name",
        "recipe__name",
        "sku",
    )
    autocomplete_fields = ("raw_product", "recipe")
    inlines = [FinishedProductImageInline]
    # Blank recipe means undyed passthrough, and that is a real choice made
    # here rather than an oversight — so it gets a filter of its own.
    list_filter = ("is_active", "raw_product__category", ("recipe", admin.EmptyFieldListFilter))
    ordering = ("name",)


class ProductionRunRowInline(admin.TabularInline):
    """The sheet's rows, read-only.

    Here so a run can be *seen* before it is deleted, not edited. A row's
    quantity is what the paper said and its `applied_log` is what it moved;
    neither is something to retype.
    """
    model = ProductionRunRow
    extra = 0
    can_delete = False
    fields = ("order", "finished_product", "quantity", "done_at", "applied_log")
    readonly_fields = fields
    ordering = ("order",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ProductionRun)
class ProductionRunAdmin(admin.ModelAdmin):
    """Printed sheets, mostly so the useless ones can be deleted.

    A run is scaffolding rather than a record — the inventory log is what
    closes the loop — so throwing one away is cheap and normal. Two things
    are worth knowing before you do:

    **Deleting a run does not un-move stock.** Its rows cascade away, but the
    `InventoryLog` rows they created are separate objects and stay, which is
    right: those baths really were dyed. What goes is the trail from the
    sheet to the movement. `Reported` is in the list for exactly that reason
    — a run showing 0 reported has moved nothing and is free to delete.

    **The token is write-once.** It is printed on paper and encoded in that
    sheet's QR code, and this app can rewrite neither, so editing it here
    would silently orphan every copy of the sheet — the same reasoning that
    makes a SKU write-once.
    """
    list_display = (
        "pk", "token", "created_at", "bath_count", "reported", "submitted_at",
        "submitted_by",
    )
    list_filter = (("submitted_at", admin.EmptyFieldListFilter), "category")
    search_fields = ("token", "note")
    ordering = ("-created_at",)
    readonly_fields = ("token", "created_at")
    inlines = [ProductionRunRowInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("rows")

    @admin.display(description="Baths")
    def bath_count(self, obj):
        return obj.rows.count()

    @admin.display(description="Reported")
    def reported(self, obj):
        applied = sum(1 for row in obj.rows.all() if row.applied_log_id)
        return f"{applied} of {obj.rows.count()}"


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    """Every stock movement, filterable by which part of the app made it.

    `source` is in the filters because that is the question worth asking of
    this table: whether the corrections coming out of the Sunday close are
    going up or down, and how they compare with the ways stock is supposed
    to move. The notes still say it in English for whoever is reading one
    row; the field is what makes counting them possible.
    """
    list_display = (
        "finished_product", "log_type", "source", "quantity", "raw_product",
        "sale_reference", "created_at",
    )
    list_filter = ("log_type", "source", "raw_product__category", "created_at")
    search_fields = ("finished_product__name", "raw_product__name", "sale_reference")
    ordering = ("-created_at",)


class CloseRunRowInline(admin.TabularInline):
    """A close's answers, read-only — same reasoning as the production rows.

    `on_hand_before` is what the disagreement was measured against and
    `applied_log` is what it moved. Neither is something to retype, and the
    close is over.
    """
    model = CloseRunRow
    extra = 0
    can_delete = False
    fields = ("finished_product", "outcome", "on_hand_before", "counted",
              "applied_log", "decided_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CloseRun)
class CloseRunAdmin(admin.ModelAdmin):
    """One row per day's close.

    Unlike a production run this *is* a record — the count of things it found
    wrong is the output of the whole exercise, not scaffolding for it — so
    there is nothing here worth deleting and the fields are read-only. The
    day is unique and the token is in a URL somebody may still have open.
    """
    list_display = ("day", "employee", "checked", "put_right", "created_at")
    list_filter = ("employee",)
    search_fields = ("token",)
    ordering = ("-day",)
    readonly_fields = ("day", "token", "created_at")
    inlines = [CloseRunRowInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("rows")

    @admin.display(description="Checked")
    def checked(self, obj):
        return sum(1 for row in obj.rows.all() if row.outcome != CloseRunRow.PENDING)

    @admin.display(description="Put right")
    def put_right(self, obj):
        return sum(
            1 for row in obj.rows.all()
            if row.outcome in (CloseRunRow.MISSING, CloseRunRow.EXTRA)
        )


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    """The roster, and the PIN recovery mechanism.

    PINs are shown in the list and editable there. That's the point: when
    somebody forgets theirs you read it off this screen and tell them, and
    when you want to change one you type over it. It works because the PIN
    guards a timesheet figure that a person reviews, not an account — see
    Employee's docstring for where that line is drawn.
    """
    # `user` is here so linking one is discoverable — it is the only way a
    # signed-in person stops being asked to pick their own name off the booth
    # form. Blank for almost everybody, which is the normal case.
    list_display = (
        "name", "pin", "is_active", "user", "has_pass",
        "entry_count", "last_reported",
    )
    list_editable = ("pin", "is_active")
    list_filter = ("is_active",)
    autocomplete_fields = ("user",)
    search_fields = ("name",)
    ordering = ("name",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _entries=Count("time_entries"),
            _last=Max("time_entries__work_date"),
        )

    @admin.display(description="Pass", boolean=True)
    def has_pass(self, obj):
        """Whether `secret/handbook/` has anything to hand this person.

        A column rather than something you check per-row, because the useful
        question is "who is going to reach the bottom of the handbook and be
        told to contact me" — and that is only answerable by looking down the
        whole roster at once.
        """
        return bool(obj.pass_pdf)

    @admin.display(description="Days reported", ordering="_entries")
    def entry_count(self, obj):
        return obj._entries

    @admin.display(description="Last reported", ordering="_last")
    def last_reported(self, obj):
        return obj._last or "—"


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    """Where a reported figure gets corrected.

    The hours form only reaches back three weeks and only lets somebody edit
    their own day. Anything older, or anyone else's, is fixed here — which is
    also the only way an entry gets deleted.
    """
    list_display = ("employee", "work_date", "hours", "created_at", "was_revised")
    list_filter = ("employee", "work_date")
    search_fields = ("employee__name", "notes")
    date_hierarchy = "work_date"
    ordering = ("-work_date", "employee__name")

    @admin.display(description="Revised", boolean=True)
    def was_revised(self, obj):
        return obj.was_revised


@admin.register(LabelStock)
class LabelStockAdmin(admin.ModelAdmin):
    """Adding a stock is transcribing eight numbers off a vendor page.

    Grouped in the order the vendor lists them so it reads as copying rather
    than as filling in a form. `LabelStock.clean()` refuses geometry that runs
    off the sheet, which is what a transposed digit in a pitch looks like.
    """

    list_display = ("name", "label_size", "grid", "labels_per_sheet", "is_active")
    list_filter = ("is_active",)

    fieldsets = (
        (None, {"fields": ("name", "purchase_url", "is_active")}),
        ("Sheet", {"fields": (("page_width_in", "page_height_in"),)}),
        ("Label", {
            "fields": (("label_width_in", "label_height_in"), ("columns", "rows")),
        }),
        ("Where the first label sits", {
            "fields": (("margin_left_in", "margin_top_in"), ("pitch_x_in", "pitch_y_in")),
        }),
        ("Printer registration", {
            "description": "Print the calibration sheet from the Barcode Labels "
                           "page first, then nudge if the outlines don't line up.",
            "fields": (("x_offset_mm", "y_offset_mm"),),
        }),
    )

    @admin.display(description="Label")
    def label_size(self, obj):
        return f"{obj.label_width_in}in × {obj.label_height_in}in"

    @admin.display(description="Grid")
    def grid(self, obj):
        return f"{obj.columns} × {obj.rows}"


@admin.register(BoothPhoto)
class BoothPhotoAdmin(admin.ModelAdmin):
    """Read-mostly. The permissions on a row were given by a person at a
    moment; editing them here would rewrite what someone agreed to, so the
    sharing fields are visible but the sender and the time are not editable."""
    list_display = ("employee", "reason", "created_at", "shareable", "sku_prefix")
    list_filter = ("reason", "share_website", "share_instagram", "people_in_photo")
    search_fields = ("employee__name", "caption", "tag", "sku_prefix", "note")
    readonly_fields = ("created_at", "shareable")

    @admin.display(boolean=True, description="OK to post")
    def shareable(self, obj):
        return obj.shareable


@admin.register(UnmatchedSale)
class UnmatchedSaleAdmin(admin.ModelAdmin):
    """The queue, for looking at rather than working — resolving belongs on
    the review page, which moves stock and writes the log row too. Resolving
    one here would mark it done and leave the count untouched."""
    list_display = ("name", "quantity", "sold_at", "resolved_at", "dismissed_at")
    list_filter = ("resolved_at", "dismissed_at")
    search_fields = ("name", "variation_name", "order_id")
    readonly_fields = ("order_id", "line_uid", "sold_at", "created_at")
