"""The bulk recipe matrix: a paste box that builds the grid's own POST."""
import csv
import io
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.forms import formset_factory
from django import forms
from django.db import transaction
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.shortcuts import render, redirect

from ..sitemap import page_meta
from ..models import FinishedProduct, RawProduct
from ..models import Recipe


def _parse_raw_ids(raw_ids_param: str) -> list[int]:
    raw_ids = []
    for part in (raw_ids_param or "").split(","):
        part = part.strip()
        if part.isdigit():
            raw_ids.append(int(part))
    # de-dupe preserving order
    seen = set()
    out = []
    for i in raw_ids:
        if i not in seen:
            out.append(i)
            seen.add(i)
    return out


def _default_finished_name(raw_product_name: str, recipe_name: str) -> str:
    return f"{raw_product_name} - {recipe_name}".strip()


def _default_price_for_raw(raw_product: RawProduct) -> Decimal:
    if raw_product.suggested_price is not None:
        return raw_product.suggested_price
    # blank_cost, not price: a fancy blank's price is 0 and its real cost is
    # the plain blank plus the line work.
    return (raw_product.blank_cost or Decimal("0")) * Decimal("3.0")


def build_recipe_matrix_form_class(raw_products: list[RawProduct]):
    """
    Dynamically builds a Form class with:
      - recipe_name
      - on_hand_<raw_id> for each raw product
    """
    fields = {
        "recipe_name": forms.CharField(max_length=150, required=True),
    }

    for rp in raw_products:
        fields[f"on_hand_{rp.id}"] = forms.IntegerField(
            required=False,
            min_value=0,
            label=f"{rp.name} (id={rp.id})",
            help_text=f"How many finished items on hand for this raw product.",
        )

    return type("RecipeMatrixRowForm", (forms.Form,), fields)


def _matrix_formset_data_from_paste(text: str, columns, absolute_max: int):
    """Turn a pasted block into the POST data the grid's formset already reads.

    The grid is the right shape for a handful of rows and the wrong shape for
    twenty: a colorway list arrives as twenty names against three blanks, which
    is sixty boxes to tab through, and the job is usually the same number in
    every cell. A paste is the same work in one gesture.

    **It builds `form-N-...` keys and hands them to the same `RowFormSet`**,
    rather than saving anything itself. That is the whole design: the columns,
    the validation, the blank-means-leave-alone rule and the save loop stay in
    one place, so a paste can never write something the grid could not. A
    second parser that reached `FinishedProduct` on its own would be a second
    door onto the same room, and the two would drift.

    Returns `(data, errors, lines)`; `data` is only usable when `errors` is
    empty, and `lines` maps each form's index back to the line it came from —
    blank lines are skipped, so the two numbers drift, and an error naming the
    wrong line sends somebody to a row that is fine.

    Two rules, and both are about the silent half. **A row must carry exactly
    one cell per column**, so `Pink,5` against three blanks is refused rather
    than quietly setting one and leaving two alone — the paste looks applied
    either way, and a column that didn't take is invisible until somebody
    counts the shelf. Writing `Pink,5,,` says the same thing on purpose. And
    **a count with no name is refused**, where the typed grid just skips the
    row: a dropped first field shifts every count one column left, which is
    exactly the kind of wrong that reads as fine.

    Tabs are accepted as the separator when any line holds one, because the
    other way this list arrives is a spreadsheet selection.
    """
    # A spreadsheet paste is tab-separated; a typed one is commas. Sniffing the
    # whole block rather than per line keeps one row from parsing differently
    # from its neighbours.
    delimiter = "\t" if "\t" in text else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    errors = []
    rows = []
    lines = []
    for line_number, cells in enumerate(reader, start=1):
        cells = [cell.strip() for cell in cells]
        if not any(cells):
            continue        # blank line, including the one a paste ends with

        name, counts = cells[0], cells[1:]

        if not name:
            errors.append(
                f"Line {line_number}: a count with no colorway name. "
                f"Every line starts with the recipe."
            )
            continue

        if len(counts) != len(columns):
            errors.append(
                f"Line {line_number} ({name}): {len(counts)} "
                f"count{'' if len(counts) == 1 else 's'} for "
                f"{len(columns)} column{'' if len(columns) == 1 else 's'}. "
                f"Leave a cell empty to skip it — {name},"
                + ",".join(["5"] * len(columns))
                + "."
            )
            continue

        rows.append((name, counts))
        lines.append(line_number)

    if len(rows) > absolute_max:
        errors.append(
            f"{len(rows)} rows pasted, and the grid takes {absolute_max}. "
            f"Split it and paste the rest after."
        )

    if errors:
        return {}, errors, []

    data = {
        "form-TOTAL_FORMS": str(len(rows)),
        "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": str(absolute_max),
    }
    for index, (name, counts) in enumerate(rows):
        data[f"form-{index}-recipe_name"] = name
        for (rp, field_name), value in zip(columns, counts):
            data[f"form-{index}-{field_name}"] = value

    return data, errors, lines


def _paste_errors_from_formset(formset, columns, lines):
    """Say which pasted line Django objected to, and in which column.

    The formset's own error rendering is per widget, and there are no widgets
    on screen in the paste branch — so without this the page would come back
    holding the text and nothing saying what was wrong with it. Field labels
    carry the raw product's name already, which is what turns "form-6-on_hand_32"
    into something findable in the box.
    """
    labels = {field_name: rp.name for rp, field_name in columns}
    out = []

    for index, form in enumerate(formset.forms):
        line_number = lines[index] if index < len(lines) else index + 1
        name = (form.data.get(f"form-{index}-recipe_name") or "").strip()
        where = f"Line {line_number}" + (f" ({name})" if name else "")

        for field_name, messages_ in form.errors.items():
            column = labels.get(field_name, "colorway")
            for message in messages_:
                out.append(f"{where}, {column}: {message}")

    # A formset-level error (too many forms, say) has no field to hang on.
    out.extend(str(error) for error in formset.non_form_errors())
    return out


def _matrix_grid_context(formset, raw_ids_param, raw_products, columns, **extra):
    """Everything the grid template needs, built in one place.

    There are four renders of this page and the columns were already missing
    from one of them once, which dropped every cell on the page that existed
    to show the errors. A placeholder derived from the real columns is the
    same hazard a second time, so both come from here.
    """
    context = {
        "formset": formset,
        "raw_ids_param": raw_ids_param,
        "raw_products": raw_products,
        "columns": columns,
        # Built from the columns actually on screen rather than written out,
        # because the whole question the box raises is which count goes where.
        "paste_placeholder": "\n".join(
            f"{name}," + ",".join(["5"] * len(columns))
            for name in ("Peacock", "Navy", "Delphinium")
        ),
    }
    context.update(extra)
    return context


def _matrix_picker_products():
    """
    Every active raw product, for the bulk-matrix picker.

    Deliberately unfiltered, unlike `_bulk_inventory_picker_products`: this is
    the page where a raw product's finished products get *created*, so the ones
    with none yet are the point rather than a dead end.
    """
    return (
        RawProduct.objects.active()
        .select_related("category")
        .order_by("category__name", "name")
    )


def _matrix_picker_response(request):
    return render(
        request,
        "scarves/bulk_recipe_matrix_entry.html",
        {"show_picker": True, "picker_products": _matrix_picker_products()},
    )


@page_meta(
    title="Bulk Recipe Matrix",
    description="Spreadsheet-style grid: rows are recipes, columns are raw "
                "products. Bulk-creates/updates FinishedProducts with auto-named "
                '"<Raw> - <Recipe>" entries and default pricing. Paste a list '
                "instead of typing the cells when there are more than a few.",
    category="Recipes",
    note="Add ?raw_ids=1,2,3 — or open with none to pick from a list.",
)
@require_http_methods(["GET", "POST"])
@login_required
def bulk_recipe_matrix_entry(request):
    """
    Usage:
      /scarves/private/bulk-matrix/?raw_ids=1,2,3   (or open bare and pick from a list)

    Each row = one recipe name and counts for each raw product.
    Creates/updates FinishedProduct for every (recipe, raw_product) cell provided.
    Finished product names are auto-generated as "<Raw> - <Recipe>".
    """
    # Accept either ?raw_ids=1,2,3 or repeated ?raw_ids=1&raw_ids=2 (picker form).
    raw_ids_param = ",".join(v.strip() for v in request.GET.getlist("raw_ids") if v.strip())
    raw_ids = _parse_raw_ids(raw_ids_param)

    # No selection yet: show the picker. The hidden `picked` marker is what
    # tells a submitted-but-empty picker apart from a bare first visit — an
    # unticked checkbox form submits no parameters at all.
    if not raw_ids:
        if request.GET.get("picked"):
            messages.error(request, "Pick at least one raw product to build the grid.")
        return _matrix_picker_response(request)

    raw_products_qs = (
        RawProduct.objects.active().filter(id__in=raw_ids)
        .select_related("category")
    )
    raw_products_by_id = {rp.id: rp for rp in raw_products_qs}

    # Preserve the user-specified order
    raw_products = [raw_products_by_id[i] for i in raw_ids if i in raw_products_by_id]

    columns = [(rp, f"on_hand_{rp.id}") for rp in raw_products]

    missing = [i for i in raw_ids if i not in raw_products_by_id]
    if missing:
        messages.error(request, f"Some raw_ids were not found/active: {missing}")

    if not raw_products:
        messages.error(request, "No valid raw products found for the provided raw_ids.")
        return _matrix_picker_response(request)

    RowForm = build_recipe_matrix_form_class(raw_products)
    RowFormSet = formset_factory(RowForm, extra=10)

    if request.method == "POST":
        # Which of the two forms posted. The paste box is its own form with its
        # own button, rather than a disclosure under the grid's: one button that
        # means two things is the mistake the recipe page's back-date made, and
        # the person reading top to bottom would reach Save before learning the
        # other half existed.
        pasted = request.POST.get("csv_rows")

        if pasted is not None:
            data, paste_errors, paste_lines = _matrix_formset_data_from_paste(
                pasted, columns, RowFormSet.absolute_max
            )
            if not paste_errors and data["form-TOTAL_FORMS"] == "0":
                paste_errors = [
                    "Nothing pasted — one line per colorway, "
                    "name first then a count per column."
                ]
            if paste_errors:
                return render(
                    request,
                    "scarves/bulk_recipe_matrix_entry.html",
                    _matrix_grid_context(
                        RowFormSet(), raw_ids_param, raw_products, columns,
                        # Handing back the text matters more here than
                        # anywhere else on the page: the paste is the work,
                        # and losing twenty lines to one bad one is a reason
                        # not to use the box again.
                        csv_rows=pasted,
                        csv_errors=paste_errors,
                    ),
                )
            formset = RowFormSet(data)
        else:
            formset = RowFormSet(request.POST)

        if not formset.is_valid():
            # A paste that parsed but won't validate (a count that isn't a
            # number) comes back as the text, not as a grid — the rows are
            # already in the box, and re-rendering them as fifty boxes hides
            # which line was wrong.
            if pasted is not None:
                return render(
                    request,
                    "scarves/bulk_recipe_matrix_entry.html",
                    _matrix_grid_context(
                        RowFormSet(), raw_ids_param, raw_products, columns,
                        csv_rows=pasted,
                        csv_errors=_paste_errors_from_formset(
                            formset, columns, paste_lines
                        ),
                    ),
                )
            # Without the columns the grid re-renders with no cells at all,
            # hiding the very errors this branch exists to show.
            return render(
                request,
                "scarves/bulk_recipe_matrix_entry.html",
                _matrix_grid_context(
                    formset, raw_ids_param, raw_products, columns
                ),
            )

        created_recipes = 0
        created_fp = 0
        updated_fp = 0
        touched_cells = 0

        with transaction.atomic():
            for form in formset:
                cd = form.cleaned_data
                if not cd:
                    continue

                recipe_name = (cd.get("recipe_name") or "").strip()
                if not recipe_name:
                    continue

                recipe, recipe_created = Recipe.objects.get_or_create(
                    name=recipe_name,
                    defaults={"description": "", "is_active": True},
                )
                if recipe_created:
                    created_recipes += 1

                # For each raw product column, if user provided a number, set that inventory
                for rp in raw_products:
                    field_name = f"on_hand_{rp.id}"
                    value = cd.get(field_name, None)

                    # Blank cell => skip (don’t change existing)
                    if value is None:
                        continue

                    finished_name = _default_finished_name(rp.name, recipe.name)
                    price = _default_price_for_raw(rp)

                    fp, created = FinishedProduct.objects.update_or_create(
                        raw_product=rp,
                        recipe=recipe,
                        name=finished_name,
                        defaults={
                            "price": price,
                            "number_on_hand": int(value),
                            "is_active": True,
                        },
                    )

                    touched_cells += 1
                    if created:
                        # Par comes from the blank, not the field default, and
                        # only on creation — an existing product's par is
                        # someone's decision and this form never asked about it.
                        fp.par = rp.finished_par_default
                        fp.save(update_fields=["par"])
                        created_fp += 1
                    else:
                        updated_fp += 1

        messages.success(
            request,
            (
                f"Saved {touched_cells} inventory cells. "
                f"Recipes: {created_recipes} created. "
                f"Finished products: {created_fp} created, {updated_fp} updated."
            ),
        )
        return redirect(f"{request.path}?raw_ids={raw_ids_param}")

    # GET
    return render(
        request,
        "scarves/bulk_recipe_matrix_entry.html",
        _matrix_grid_context(
            RowFormSet(), raw_ids_param, raw_products, columns
        ),
    )
