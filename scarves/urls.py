from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path(
        "production-needed/",
        views.production_needed_view,
        name="production_needed",
    ),
    path(
        "production-needed/dye-bath/<int:pk>/",
        views.record_dye_bath,
        name="record_dye_bath",
    ),
    path(
        "raw-inventory/<int:category_id>/",
        views.raw_inventory_view,
        name="raw_inventory",
    ),
    path(
        "raw-inventory/adjust/<int:pk>/",
        views.adjust_raw_stock,
        name="adjust_raw_stock",
    ),
]

