from django.contrib import admin

# Register your models here.
from django.contrib import admin

from .models import (
    DyeBrand,
    Dye,
    RawProductCategory,
    RawProduct,
    Recipe,
    RecipeDye,
    FinishedProduct,
    FinishedProductImage,
    ProductionLog
)


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


@admin.register(RawProductCategory)
class RawProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(RawProduct)
class RawProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "price",
        "number_on_hand",
        "is_active",
        "sku",
    )
    list_filter = ("category", "is_active")
    search_fields = ("name", "category__name", "sku")
    ordering = ("category__name", "name")


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
    fields = ("image_url", "alt_text", "order")
    ordering = ("order",)


@admin.register(FinishedProduct)
class FinishedProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "raw_product",
        "recipe",
        "price",
        "number_on_hand",
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


@admin.register(ProductionLog)
class ProductionLogAdmin(admin.ModelAdmin):
    list_display = ("finished_product", "raw_product", "quantity", "created_at")
    list_filter = ("raw_product__category", "created_at")
    search_fields = ("finished_product__name", "raw_product__name")
    ordering = ("-created_at",)