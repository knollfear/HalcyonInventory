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
    # POST-only, from the type-ahead in any dye picker. No @page_meta: there
    # is no page here, only the "add this dye" the picker offers.
    path("private/dyes/new/", views.dye_create, name="dye_create"),
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

    # --- Production sheets ---
    # Planning is staff at a desk; reporting is whoever was at the sink, and
    # they have no accounts. So the return path is secret/ and its way in is
    # a token printed on the paper — scoped to one sheet rather than a
    # standing URL, and expiring in practice when the run is reported.
    path(
        "private/production-sheet/",
        views.production_sheet_index,
        name="production_sheet_index",
    ),
    path(
        "private/production-sheet/<int:pk>/",
        views.production_run_detail,
        name="production_run_detail",
    ),
    path(
        "private/production-sheet/<int:pk>/pdf/",
        views.production_sheet_pdf,
        name="production_sheet_pdf",
    ),
    path(
        "secret/production/",
        views.production_run_index,
        name="production_run_index",
    ),
    # Before the token route, or `upload` would be read as a sheet code.
    path(
        "secret/production/upload/",
        views.production_upload,
        name="production_upload",
    ),
    path(
        "secret/production/<str:token>/",
        views.production_run,
        name="production_run",
    ),

    # --- Converting plain scarves to fancy ones ---
    # The one part of fancy that is worth systematising: not the production
    # (supply is opportunistic), but the conversion event, by colorway.
    path("private/fancy/", views.fancy_convert, name="fancy_convert"),

    # --- The display map (staff: what hangs where) ---
    # Editing the map is a desk job and a staff decision, so it is private/
    # and separate from the restock pages: saving an assignment must never
    # read as somebody having checked a peg.
    path("private/display/", views.display_map_index, name="display_map_index"),
    path(
        "private/display/<int:fixture_id>/",
        views.display_map,
        name="display_map",
    ),

    # --- Restocking the display ---
    # Restocking generates the kanban cards, so it comes *before* the close —
    # a close run first is checking against a pile that hasn't finished being
    # made. See scarves/restock.py.
    path("secret/restock/", views.restock_index, name="restock_index"),
    path(
        "secret/restock/<int:fixture_id>/",
        views.restock_board,
        name="restock_board",
    ),

    # --- The Sunday close ---
    # Whoever is holding the physical tags runs this, in a car park, on one
    # bar of signal — so it is secret/ and a PIN, the same bargain the hours
    # and booth forms make. The PIN is asked once and the day's token carries
    # the steps after it. The history is a desk page and staff-only.
    path("secret/close/", views.close_index, name="close_index"),
    path("secret/close/<str:token>/", views.close_run, name="close_run"),
    path(
        "secret/close/<str:token>/tag/",
        views.close_add_tag,
        name="close_add_tag",
    ),
    path(
        "secret/close/<str:token>/undo/<int:pk>/",
        views.close_undo,
        name="close_undo",
    ),
    path("private/closes/", views.close_history, name="close_history"),

    # --- Timekeeping ---
    # The two halves sit in different buckets on purpose. Reporting your own
    # hours needs no account — that is the entire point — but it isn't for
    # customers either, so it's secret/: reachable by anyone holding the URL,
    # off the public map, and guarded by a four-digit PIN. The week, which
    # shows what everyone worked, is staff-only.
    path("secret/hours/", views.hours_entry, name="hours_entry"),

    # --- The booth ---
    # Same bucket and the same reasoning as the hours form: the crew has no
    # accounts, and a login here would lock out exactly the people it is for
    # — silently, because the symptom is only that nobody ever reports. The
    # PIN is what guards it, in the page. The two office pages are private/.
    path("secret/booth/", views.booth_photo, name="booth_photo"),

    # --- The crew handbook ---
    # Same bucket and the same reasoning again: no accounts, so no login. The
    # name and PIN are here to pick which pass comes back rather than to guard
    # anything — the page's own text is not a secret, and a faire pass is a
    # barcode and a photograph. What makes secret/ right is that customers
    # have no business tripping over the crew's instructions.
    path("secret/handbook/", views.crew_handbook, name="crew_handbook"),
    path("private/booth-photos/", views.booth_photos, name="booth_photos"),
    path(
        "private/unidentified-sales/",
        views.unmatched_sales,
        name="unmatched_sales",
    ),
    path(
        "private/unidentified-sales/<int:pk>/resolve/",
        views.resolve_unmatched_sale,
        name="resolve_unmatched_sale",
    ),
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
    path("public/color-bands/", views.color_bands_page, name="color_bands_page"),
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
