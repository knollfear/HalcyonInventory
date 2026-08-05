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
    path("raw-inventory/", views.raw_inventory_index, name="raw_inventory_index"),
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
    path("internal/quick-recipes/", views.quick_recipe_entry, name="quick_recipe_entry"),
    path("internal/recipes/", views.recipe_showcase, name="recipe_showcase"),
    path("internal/recipes/<int:pk>/row/", views.recipe_row, name="recipe_row"),
    path("internal/recipes/<int:pk>/dyes/", views.recipe_dyes_save, name="recipe_dyes_save"),
    path("bulk-matrix/", views.bulk_recipe_matrix_entry, name="bulk_recipe_matrix_entry"),
    path("bulk-inventory/", views.bulk_inventory_update, name="bulk_inventory_update"),
    path("webhooks/square", views.square_webhook, name="square_webhook"),
    path("webhooks/square/", views.square_webhook),
    path("reference-sheet/", views.reference_sheet_index, name="reference_sheet_index"),
    path("reference-sheet/<int:category_id>/", views.reference_sheet_pdf, name="reference_sheet_pdf"),
    path("images/upload/", views.image_upload, name="image_upload"),
    path("images/upload/presign/", views.presign_upload, name="presign_upload"),
    path("images/upload/local/", views.local_upload, name="local_upload"),
    path("images/upload/<int:upload_id>/process/", views.process_upload, name="process_upload"),
    path("images/upload/<int:upload_id>/assign/", views.assign_upload, name="assign_upload"),
    path("images/products/search/", views.product_search, name="product_search"),
    path("game/", views.game_page, name="game_page"),
    path("game/board/", views.game_board, name="game_board"),
    path("quiz/", views.quiz_page, name="quiz_page"),
    path("quiz/board/", views.quiz_board, name="quiz_board"),
]

