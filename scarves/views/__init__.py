"""The views, one module per page group.

Everything a URLconf or a test used to reach as `views.<name>` is re-exported
here, so `scarves/urls.py` is unchanged. The modules are the unit to read:
each one holds the pages of one `docs/claude/` topic.
"""
from .close import (  # noqa: F401
    close_add_tag,
    close_index,
    close_run,
    close_tag_search,
    close_undo,
)
from .crew import (  # noqa: F401
    BOOTH_PIN_ATTEMPT_LIMIT,
    HOURS_PIN_ATTEMPT_LIMIT,
    UNMATCHED_WINDOW,
    booth_photo,
    booth_photos,
    crew_handbook,
    hours_entry,
    resolve_unmatched_sale,
    timesheet,
    unmatched_sales,
)
from .games import (  # noqa: F401
    GAME_DEFAULT_PAIRS,
    GAME_PAIR_SIZES,
    QUIZ_CHOICES,
    QUIZ_DEFAULT_QUESTIONS,
    QUIZ_LENGTHS,
    QUIZ_MIN_POOL,
    QUIZ_POINTS_CORRECT,
    QUIZ_SPEED_BONUS,
    QUIZ_SPEED_WINDOW,
    color_bands_page,
    game_board,
    game_page,
    quiz_board,
    quiz_page,
)
from .images import (  # noqa: F401
    IMAGE_JPEG_QUALITY,
    IMAGE_MAX_EDGE,
    WEB_SAFE_FORMATS,
    assign_upload,
    image_upload,
    local_upload,
    photo_walk,
    photo_walk_index,
    presign_upload,
    process_upload,
    product_search,
    search_products,
)
from .invoices import (  # noqa: F401
    invoice_book,
    invoice_detail,
    invoice_discard,
    invoice_index,
    invoice_receive,
    invoice_receive_lines,
    invoice_write_off,
)
from .labels import (  # noqa: F401
    label_calibration_pdf,
    label_index,
    label_pdf,
)
from .matrix import (  # noqa: F401
    build_recipe_matrix_form_class,
    bulk_recipe_matrix_entry,
)
from .production import (  # noqa: F401
    PRODUCED_SINCE_PRESETS,
    produced_since_retract,
    produced_since_view,
    production_from_close,
    production_needed_view,
    production_run,
    production_run_add_bath,
    production_run_add_row,
    production_run_cancel_remaining,
    production_run_detail,
    production_run_index,
    production_run_strike_row,
    production_sheet_index,
    production_sheet_pdf,
    production_upload,
    record_dye_bath,
    sheet_list,
)
from .recipes import (  # noqa: F401
    CARD_ROWS,
    RECIPE_LOG_LIMIT,
    card_backfill,
    card_backfill_index,
    color_bands_save,
    color_classify,
    color_suggest_from_photo,
    dye_create,
    parse_card_date,
    quick_recipe_entry,
    recipe_detail,
    recipe_dyes_save,
    recipe_history,
    recipe_par_save,
    recipe_restore,
    recipe_retire,
    recipe_row,
    recipe_showcase,
    record_recipe_production,
)
from .reports import (  # noqa: F401
    SALES_COLUMNS,
    close_history,
    dye_statements,
    sales_report,
    season_report,
    slow_sellers,
    stock_value,
)
from .restock import (  # noqa: F401
    display_map,
    display_map_index,
    restock_board,
    restock_index,
)
from .sheets import (  # noqa: F401
    reference_sheet_by_color_pdf,
    reference_sheet_index,
    reference_sheet_pdf,
)
from .site import (  # noqa: F401
    index,
    navigation,
    public_index,
)
from .square import (  # noqa: F401
    square_webhook,
)
from .stock import (  # noqa: F401
    BULK_REASON_CHOICES,
    BULK_REASON_PRESETS,
    blank_edit,
    blank_index,
    blank_new,
    build_bulk_inventory_form_class,
    bulk_inventory_update,
    bulk_reason,
    fancy_convert,
    raw_inventory_index,
    raw_inventory_view,
    raw_par_save,
    raw_supply_save,
    supplier_detail,
    supplier_index,
)
