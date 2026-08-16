"""URL map for the scarves app, mounted at /scarves/.

The first path segment says who a route is for, so the answer to "is this
thing exposed?" is readable straight off the URL:

    private/    staff only — every view behind @login_required
    public/     no login: the matching game and the quiz, plus the board
                endpoints they and any embed fetch
    secret/     no login either, but not advertised — the URL is the way in.
                Listed on the staff site map so you can find the link to hand
                out, and filtered off the public one so a customer never
                stumbles into it.
    webhooks/   machine-to-machine, unauthenticated but not browsable

The split mirrors the template layers (base_internal.html / base_public.html),
so a page's URL and its shell can't disagree about which side of the fence
it's on. URLTests enforces both halves.

Route *names* are the stable interface — everything reverses by name, so
moving a path is a one-line edit here.
"""

from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # /scarves/ is the address people actually type; the map itself is a
    # staff page, so it lives under private/ and the bare path points there.
    path("", RedirectView.as_view(pattern_name="index", permanent=False)),
    path("private/", views.index, name="index"),

    # --- Production ---
    path(
        "private/production-needed/",
        views.production_needed_view,
        name="production_needed",
    ),
    path(
        "private/production-needed/dye-bath/<int:pk>/",
        views.record_dye_bath,
        name="record_dye_bath",
    ),

    # --- Inventory ---
    path(
        "private/raw-inventory/",
        views.raw_inventory_index,
        name="raw_inventory_index",
    ),
    path(
        "private/raw-inventory/<int:category_id>/",
        views.raw_inventory_view,
        name="raw_inventory",
    ),
    path(
        "private/raw-inventory/adjust/<int:pk>/",
        views.adjust_raw_stock,
        name="adjust_raw_stock",
    ),
    path(
        "private/bulk-inventory/",
        views.bulk_inventory_update,
        name="bulk_inventory_update",
    ),

    # --- Recipes ---
    path(
        "private/quick-recipes/",
        views.quick_recipe_entry,
        name="quick_recipe_entry",
    ),
    path("private/recipes/", views.recipe_showcase, name="recipe_showcase"),
    path("private/recipes/<int:pk>/", views.recipe_detail, name="recipe_detail"),
    path(
        "private/recipes/<int:pk>/production/",
        views.record_recipe_production,
        name="record_recipe_production",
    ),
    path("private/recipes/<int:pk>/row/", views.recipe_row, name="recipe_row"),
    path(
        "private/recipes/<int:pk>/dyes/",
        views.recipe_dyes_save,
        name="recipe_dyes_save",
    ),
    path(
        "private/bulk-matrix/",
        views.bulk_recipe_matrix_entry,
        name="bulk_recipe_matrix_entry",
    ),

    # --- Colour classification ---
    # Which rainbow sections each colorway claims. The two endpoints beneath
    # the page are POST-only row actions, so they carry no @page_meta and the
    # picker rule doesn't apply to them.
    path("private/colors/", views.color_classify, name="color_classify"),
    path(
        "private/colors/<int:pk>/bands/",
        views.color_bands_save,
        name="color_bands_save",
    ),
    path(
        "private/colors/<int:pk>/from-photo/",
        views.color_suggest_from_photo,
        name="color_suggest_from_photo",
    ),

    # --- Kanban card backfill ---
    path("private/cards/", views.card_backfill_index, name="card_backfill_index"),
    path("private/cards/<int:pk>/", views.card_backfill, name="card_backfill"),

    # --- Photo upload ---
    path("private/images/upload/", views.image_upload, name="image_upload"),
    path(
        "private/images/upload/presign/",
        views.presign_upload,
        name="presign_upload",
    ),
    path("private/images/upload/local/", views.local_upload, name="local_upload"),
    path(
        "private/images/upload/<int:upload_id>/process/",
        views.process_upload,
        name="process_upload",
    ),
    path(
        "private/images/upload/<int:upload_id>/assign/",
        views.assign_upload,
        name="assign_upload",
    ),
    path(
        "private/images/products/search/",
        views.product_search,
        name="product_search",
    ),

    # --- Timekeeping ---
    # The two halves sit in different buckets on purpose. Reporting your own
    # hours needs no account — that is the entire point — but it isn't for
    # customers either, so it's secret/: reachable by anyone holding the URL,
    # off the public map, and guarded by a four-digit PIN. The week, which
    # shows what everyone worked, is staff-only.
    path("secret/hours/", views.hours_entry, name="hours_entry"),
    path("private/timesheet/", views.timesheet, name="timesheet"),

    # --- Public: the directory ---
    # The public half of the site map, safe to hand to a stranger. Filtered in
    # the view, not the template.
    path("public/", views.public_index, name="public_index"),

    # --- Public: the games ---
    # Grouped under games/ so the two of them read as one section rather than
    # two unrelated pages. Each board endpoint sits beneath its own page: the
    # boards send CORS headers and are fetched directly by any page embedding
    # a game, so they're as public as the pages themselves.
    path("public/games/match/", views.game_page, name="game_page"),
    path("public/games/match/board/", views.game_board, name="game_board"),
    path("public/games/quiz/", views.quiz_page, name="quiz_page"),
    path("public/games/quiz/board/", views.quiz_board, name="quiz_board"),

    # --- Public: reference sheets ---
    # Photos, names and barcodes — the same things printed and laid on the
    # stall table, so there's nothing here to gate. The picker sits alongside
    # rather than under private/ so the two halves stay in one bucket.
    path(
        "public/reference-sheet/",
        views.reference_sheet_index,
        name="reference_sheet_index",
    ),
    path(
        "public/reference-sheet/<int:category_id>/",
        views.reference_sheet_pdf,
        name="reference_sheet_pdf",
    ),
    # The same category, ordered by the rainbow instead of by colorway. The
    # category stays ahead of the ordering in the path so both sheets hang off
    # the one picker, which is also what keeps them one card on the site map.
    path(
        "public/reference-sheet/<int:category_id>/by-color/",
        views.reference_sheet_by_color_pdf,
        name="reference_sheet_by_color_pdf",
    ),

    # --- Barcode labels ---
    # Staff: the page carries production quantities and drives a printer.
    # The two PDF routes take their arguments from the query string rather
    # than the path, so a run stays a re-openable URL and neither needs a
    # picker of its own beyond the page they're both reached from.
    path("private/labels/", views.label_index, name="label_index"),
    path("private/labels/pdf/", views.label_pdf, name="label_pdf"),
    path(
        "private/labels/calibrate/",
        views.label_calibration_pdf,
        name="label_calibration_pdf",
    ),

    # --- Webhooks ---
    # Deliberately left outside private/ and public/: the URL is registered in
    # the Square dashboard, so moving it here without changing it there would
    # drop sale events silently. Both spellings, because Square has sent each.
    path("webhooks/square", views.square_webhook, name="square_webhook"),
    path("webhooks/square/", views.square_webhook),
]
