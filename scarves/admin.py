import difflib
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.template.response import TemplateResponse

logger = logging.getLogger(__name__)

from .models import (
    DyeBrand,
    Dye,
    RawProductCategory,
    RawProduct,
    Recipe,
    RecipeDye,
    FinishedProduct,
    FinishedProductImage,
    InventoryLog,
    ProductImageUpload,
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


@admin.register(Dye)
class DyeAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "hex_color", "in_stock", "sku")
    list_filter = ("brand", "in_stock")
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


@admin.register(RawProduct)
class RawProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "price",
        "number_on_hand",
        "is_active",
        "square_item_id",
    )
    list_filter = ("category", "is_active")
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
    list_filter = ("is_active", "raw_product__category")
    search_fields = (
        "name",
        "raw_product__name",
        "recipe__name",
        "sku",
    )
    autocomplete_fields = ("raw_product", "recipe")
    inlines = [FinishedProductImageInline]
    ordering = ("name",)


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ("finished_product", "log_type", "quantity", "raw_product", "sale_reference", "created_at")
    list_filter = ("log_type", "raw_product__category", "created_at")
    search_fields = ("finished_product__name", "raw_product__name", "sale_reference")
    ordering = ("-created_at",)
