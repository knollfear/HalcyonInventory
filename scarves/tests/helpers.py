"""Builders and fakes shared by more than one test module.
"""
import base64
import re
from decimal import Decimal
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
)
from ..models import (
    UNCATEGORIZED_BRAND,
    BoothPhoto,
    CatalogGroup,
    CloseRun,
    CloseRunRow,
    DisplayFixture,
    DisplayPosition,
    Dye,
    DyeBrand,
    DayWeather,
    Employee,
    Faire,
    FaireDay,
    LabelStock,
    FinishedProduct,
    FinishedProductImage,
    InventoryLog,
    ProductImageUpload,
    ProductionRun,
    ProductionRunRow,
    RUN_ADJECTIVES,
    RUN_ANIMALS,
    new_run_token,
    normalize_token,
    RawProduct,
    RawProductCategory,
    Recipe,
    RecipeDye,
    RestockCheck,
    RestockPass,
    Sale,
    SaleLine,
    TimeEntry,
    UnmatchedSale,
    sync_display_slots,
)


def make_recipe(name, hexes=("#3355cc",), active=True):
    brand, _ = DyeBrand.objects.get_or_create(name="TestBrand")
    recipe = Recipe.objects.create(name=name, is_active=active)
    for i, hex_color in enumerate(hexes, start=1):
        dye, _ = Dye.objects.get_or_create(
            name=f"{name}-dye-{i}", brand=brand, defaults={"hex_color": hex_color}
        )
        dye.hex_color = hex_color
        dye.save()
        RecipeDye.objects.create(recipe=recipe, dye=dye, order=i)
    return recipe
def make_product(recipe, name, with_image=True, active=True):
    category, _ = RawProductCategory.objects.get_or_create(name="Silk")
    raw, _ = RawProduct.objects.get_or_create(
        name=f"raw-{name}", category=category, defaults={"price": "5.00"}
    )
    product = FinishedProduct.objects.create(
        name=name, raw_product=raw, recipe=recipe, price="30.00", is_active=active
    )
    if with_image:
        FinishedProductImage.objects.create(
            finished_product=product,
            image_url=f"https://example.test/{name}.jpg",
        )
    return product
def make_jpeg(size=(4032, 3024), exif_orientation=None, color=(180, 90, 60)):
    """Bytes of a JPEG, optionally carrying an EXIF orientation tag."""
    from io import BytesIO
    from PIL import Image

    img = Image.new("RGB", size, color)
    buf = BytesIO()
    if exif_orientation is None:
        img.save(buf, "JPEG", quality=90)
    else:
        exif = img.getexif()
        exif[0x0112] = exif_orientation
        img.save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()
def image_size(data):
    from io import BytesIO
    from PIL import Image

    return Image.open(BytesIO(data)).size
# --- Timekeeping ------------------------------------------------------------


def make_employee(name, pin="1234", active=True):
    return Employee.objects.create(name=name, pin=pin, is_active=active)
def make_stock(**overrides):
    """The stock we actually buy: OL25WX / Avery 5167, 4 × 20."""
    fields = {
        "name": "Test 1.75 x 0.5",
        "page_width_in": Decimal("8.5"),
        "page_height_in": Decimal("11"),
        "label_width_in": Decimal("1.75"),
        "label_height_in": Decimal("0.5"),
        "columns": 4,
        "rows": 20,
        "margin_left_in": Decimal("0.32812"),
        "margin_top_in": Decimal("0.5"),
        "pitch_x_in": Decimal("2.03125"),
        "pitch_y_in": Decimal("0.5"),
    }
    fields.update(overrides)
    return LabelStock.objects.create(**fields)
def _pdf_text(pdf_bytes):
    """Visible text from a reportlab PDF, for asserting on rendered output.

    reportlab writes content streams as ASCII85 + Flate, so this inflates them
    and pulls out the `(...) Tj` operands. Worth the few lines: without it the
    only thing a PDF test can check is that bytes came back, which passes just
    as happily when the page is blank.
    """
    import base64 as _b64, zlib as _zlib

    out = []
    for m in re.finditer(rb"stream\n(.*?)endstream", pdf_bytes, re.S):
        blob = m.group(1).strip()
        try:
            blob = _zlib.decompress(_b64.a85decode(blob, adobe=True))
        except Exception:
            try:
                blob = _zlib.decompress(blob)
            except Exception:
                continue
        out.append(blob.decode("latin-1"))
    content = "\n".join(out)
    return "".join(re.findall(r"\((.*?)\)\s*Tj", content, re.S))
def _pdf_streams(pdf_bytes):
    import base64 as _b64, zlib as _zlib

    out = []
    for m in re.finditer(rb"stream\n(.*?)endstream", pdf_bytes, re.S):
        blob = m.group(1).strip()
        try:
            blob = _zlib.decompress(_b64.a85decode(blob, adobe=True))
        except Exception:
            continue
        out.append(blob.decode("latin-1"))
    return "\n".join(out)
def _pdf_text_items(pdf_bytes):
    """(x, y, text) for each positioned string reportlab wrote.

    Matches the `Tm ... Tj` operators directly rather than carving the stream
    into `BT ... ET` blocks first. The obvious block regex is non-greedy, and
    "SHEET" ends in "ET" — so it truncates mid-string on exactly the banner
    this is used to locate, and reports nothing rather than reporting wrong.
    """
    return [
        (float(x), float(y), text)
        for x, y, text in re.findall(
            r"1 0 0 1 ([-\d.]+) ([-\d.]+) Tm \((.*?)\) Tj",
            _pdf_streams(pdf_bytes),
            re.S,
        )
    ]
class FakeSquareResult:
    """Stands in for the SDK's ApiResponse."""

    def __init__(self, body=None, errors=None):
        self.body = body or {}
        self.errors = errors or []

    def is_error(self):
        return bool(self.errors)

    def is_success(self):
        return not self.errors
class FakeSquareClient:
    """Records what the command would send, and replays canned responses.

    Deliberately dumb: the point is to exercise our payload building and our
    write-back, not to reimplement Square. Anything it can't answer honestly
    it refuses to answer at all.
    """

    def __init__(self, upsert_results=None, retrieve_result=None,
                 inventory_result=None, locations_result=None,
                 image_results=None, retrieve_results=None):
        self.upserts = []
        self.retrieves = []
        self.inventory_changes = []
        self.images = []
        self._image_results = list(image_results or [])
        self._image_seq = 0
        self._upsert_results = list(upsert_results or [])
        self._retrieve_result = retrieve_result or FakeSquareResult({"objects": []})
        # A run can read Square more than once — `--update` reads versions and
        # the ordering pass then reads whole items — so a canned sequence is
        # sometimes needed where one answer used to do.
        self._retrieve_results = list(retrieve_results or [])
        self._inventory_result = inventory_result or FakeSquareResult()
        self._locations_result = locations_result or FakeSquareResult(
            {"locations": [{"id": "LOC123"}]}
        )
        self.catalog = self._Catalog(self)
        self.inventory = self._Inventory(self)
        self.locations = self._Locations(self)

    class _Catalog:
        def __init__(self, outer):
            self.outer = outer

        def batch_upsert_catalog_objects(self, body):
            self.outer.upserts.append(body)
            if self.outer._upsert_results:
                return self.outer._upsert_results.pop(0)
            return FakeSquareResult({"id_mappings": []})

        def batch_retrieve_catalog_objects(self, body):
            self.outer.retrieves.append(body)
            if self.outer._retrieve_results:
                return self.outer._retrieve_results.pop(0)
            return self.outer._retrieve_result

        def create_catalog_image(self, request, image_file):
            # The bytes are read here rather than kept, because the thing
            # worth asserting is that a real file reached the call at all —
            # the bucket read is the step most likely to be silently skipped.
            self.outer.images.append((request, image_file.read()))
            if self.outer._image_results:
                return self.outer._image_results.pop(0)
            self.outer._image_seq += 1
            return FakeSquareResult({
                "image": {"id": f"SQ_IMG_{self.outer._image_seq}"},
            })

    class _Inventory:
        def __init__(self, outer):
            self.outer = outer

        def batch_change_inventory(self, body):
            self.outer.inventory_changes.append(body)
            return self.outer._inventory_result

    class _Locations:
        def __init__(self, outer):
            self.outer = outer

        def list_locations(self):
            return self.outer._locations_result
def make_bathable(recipe, name, on_hand=0, par=8, bath=4):
    """A finished product with the stock numbers a production sheet reads."""
    product = make_product(recipe, name, with_image=False)
    FinishedProduct.objects.filter(pk=product.pk).update(
        number_on_hand=on_hand, par=par
    )
    RawProduct.objects.filter(pk=product.raw_product_id).update(
        number_per_dye_bath=bath, number_on_hand=100
    )
    product.refresh_from_db()
    product.raw_product.refresh_from_db()
    return product
def make_undyed(name, category_name="Yarn", group=None, on_hand=0, par_level=10):
    """A yarn sold exactly as it arrives: raw product, no recipe."""
    category, _ = RawProductCategory.objects.get_or_create(name=category_name)
    raw = RawProduct.objects.create(
        name=name, category=category, price="9.00",
        number_on_hand=on_hand, par_level=par_level,
        catalog_group=group,
    )
    product = FinishedProduct.objects.create(
        name=name, raw_product=raw, recipe=None, price="18.00",
    )
    product.refresh_from_db()
    return product
def make_close_product(name, on_hand=0, slots=2, par=3):
    """A finished product with a known count and display, for the close tests.

    `slots` is display capacity — how many hang on the pegs when it is full.
    The close asks about a product when `on_hand <= slots`, because that is
    the app saying the bag behind the display is empty.
    """
    product = make_product(make_recipe(f"{name} Recipe"), name, with_image=False)
    FinishedProduct.objects.filter(pk=product.pk).update(
        number_on_hand=on_hand, par=par, display_slots=slots
    )
    product.refresh_from_db()
    return product
def hang(fixture, product, row, column):
    """Put a colorway on a peg that already exists."""
    position, _ = DisplayPosition.objects.get_or_create(
        fixture=fixture, row=row, column=column
    )
    position.finished_product = product
    position.save()
    return position
