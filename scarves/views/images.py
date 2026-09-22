"""Product photos: the phone upload, the photo walk, product search and filing."""
import uuid
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.urls import reverse
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.shortcuts import render

from ..sitemap import page_meta
from ..models import (
    DisplayFixture,
    FinishedProduct,
    FinishedProductImage,
    ProductImageUpload,
    RawProduct,
)
from .. import colorbands, photowalk, skus
from ..s3utils import download_object, presigned_post, upload_object


# ---------------------------------------------------------------------------
# Product image upload: phone -> presigned POST straight to bucket -> server
# decodes the barcode and files the photo against a FinishedProduct. If the
# barcode can't be read, the uploader picks the product inline (HTMX type-ahead).
# ---------------------------------------------------------------------------

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    # iPhones shoot HEIC and Safari sometimes hands it over as-is. It lands as
    # .jpg on purpose, not by accident: _shrink_image transcodes it to JPEG
    # during processing, so the extension describes what ends up in the bucket.
    "image/heic": ".jpg",
    "image/heif": ".jpg",
}


def _attach_image(upload, product):
    """Create a FinishedProductImage pointing at the already-uploaded object."""
    next_order = (
        product.images.aggregate(Max("order"))["order__max"] or 0
    ) + 1
    fpi = FinishedProductImage(finished_product=product, order=next_order)
    # The file already lives in the bucket at upload.key; reference it in place
    # rather than re-uploading (no move, no egress).
    fpi.image.name = upload.key
    fpi.save()
    return fpi


@page_meta(
    title="Upload Product Photos",
    description="Snap or pick product photos from your phone; each is uploaded "
                "straight to the bucket and auto-filed to the matching product "
                "by reading its barcode. Unreadable ones are assigned inline.",
    category="Products",
    note="Uploads to the bucket when configured, otherwise to local storage.",
)
@login_required
def image_upload(request):
    """The upload page, plus the blanks you can say you are shooting.

    **Saying what is in front of the camera is what makes a failed decode
    cheap.** Roughly half the barcodes in a session of forty photos don't
    read — a phone, a small Code128 on a hang tag, whatever the light is
    doing — and each miss then costs a product name typed out in full on a
    phone next to a pile of scarves. But a photo session is a *pile of one
    blank*: forty half-circle veils, then forty sash belts. So the blank is
    known before the first shot and stays true for the whole pile, which is
    exactly the half of `BLANK-DYEBATH` the picker can fill in for you.

    It fills the box in; it does not decide anything. The barcode still wins
    whenever it reads, and the prefill is ordinary editable text — same
    bargain `colorbands` and the crew cookie make.
    """
    # Only blanks that have something photographable under them. A blank
    # whose colorways are all retired would be a line in the menu that
    # narrows to nothing.
    blanks = [
        {"name": raw.name, "prefix": skus.slug(raw.name)}
        for raw in RawProduct.objects.filter(
            is_active=True, finished_products__is_active=True
        )
        .distinct()
        .order_by("name")
    ]
    return render(
        request,
        "scarves/image_upload.html",
        {"use_s3": settings.USE_S3, "blanks": blanks},
    )


@require_POST
@login_required
def presign_upload(request):
    """Create a tracking row and return presigned POST fields for direct upload."""
    if not settings.USE_S3:
        return JsonResponse(
            {"error": "Bucket storage is not configured on this environment."},
            status=400,
        )
    content_type = (request.POST.get("content_type") or "image/jpeg").lower()
    ext = _CONTENT_TYPE_EXT.get(content_type, ".jpg")
    key = f"finished_products/{uuid.uuid4().hex}{ext}"

    upload = ProductImageUpload.objects.create(key=key)
    post = presigned_post(key, content_type=content_type)
    return JsonResponse(
        {
            "upload_id": upload.id,
            "url": post["url"],
            "fields": post["fields"],
        }
    )


@require_POST
@login_required
def local_upload(request):
    """Dev-only transport: take the file straight into default storage.

    The bucket path (presign -> browser POSTs to the bucket) needs S3, so
    without it the upload page was unusable locally. Everything downstream —
    barcode decode, SKU match, manual assign — is storage-agnostic and shared,
    so this only replaces the two transport steps.
    """
    if settings.USE_S3:
        return JsonResponse(
            {"error": "Bucket storage is configured; use the presigned upload."},
            status=400,
        )

    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "No file supplied."}, status=400)

    content_type = (f.content_type or "image/jpeg").lower()
    ext = _CONTENT_TYPE_EXT.get(content_type, ".jpg")
    # Storage may rename on collision, so trust the key it hands back —
    # _attach_image points FinishedProductImage.image at exactly this key.
    key = default_storage.save(f"finished_products/{uuid.uuid4().hex}{ext}", f)

    upload = ProductImageUpload.objects.create(key=key)
    return JsonResponse({"upload_id": upload.id})


def _upload_bytes(key):
    """Read an uploaded object back, from the bucket or from local storage."""
    if settings.USE_S3:
        return download_object(key)
    with default_storage.open(key, "rb") as fh:
        return fh.read()


def _replace_upload_bytes(key, data, content_type):
    """Overwrite an uploaded object in place. Returns the key actually written.

    Local storage is configured not to overwrite either, so the old file is
    removed first; the returned key is what the caller must trust, since a
    FinishedProductImage points straight at it.
    """
    if settings.USE_S3:
        upload_object(key, data, content_type=content_type)
        return key
    default_storage.delete(key)
    return default_storage.save(key, ContentFile(data))


# Long edge of a stored product photo. Phone cameras hand us ~4000px / 5MB
# JPEGs, which is 40MB+ of downloads for one round of the matching game and
# slow enough in the reference-sheet PDF to threaten the gunicorn timeout.
# 1200 is comfortably past what either use needs.
IMAGE_MAX_EDGE = 1200
IMAGE_JPEG_QUALITY = 85

# Formats a browser will actually render. Anything else has to be transcoded no
# matter its size — an iPhone HEIC is the case that matters, and Chrome and
# Firefox both refuse to display it.
WEB_SAFE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "GIF"}


def _shrink_image(data, max_edge=IMAGE_MAX_EDGE):
    """Downscale an uploaded photo so its long edge is at most `max_edge`.

    Returns `(bytes, content_type)`, or None when the image is already small
    enough, correctly oriented, and in a format browsers can render — an
    in-bounds upload is never re-encoded, so it can't lose quality just by
    passing through here.

    Aspect ratio is preserved: a 4032x3024 phone photo becomes 1200x900, and a
    portrait one 900x1200. Nothing is cropped or squared off.

    A web-safe format is kept as-is so the object still matches the extension in
    its key and the Content-Type it was uploaded under. HEIC becomes JPEG, which
    is what `_CONTENT_TYPE_EXT` already assumes when it names the key.
    """
    from PIL import Image as PILImage, ImageOps

    im = PILImage.open(BytesIO(data))
    im.load()
    fmt = (im.format or "JPEG").upper()

    # 0x0112 is the EXIF Orientation tag. Phones record rotation there rather
    # than rotating the pixels, and re-encoding drops the tag — so a portrait
    # photo that looked upright would come out sideways in the games and the
    # PDF. Baking the rotation in is what makes the resize safe.
    needs_rotation = im.getexif().get(0x0112, 1) != 1
    # Size is not the only reason to rewrite: a small HEIC left alone would sit
    # in the bucket under a .jpg key that no browser can open.
    needs_transcode = fmt not in WEB_SAFE_FORMATS

    if max(im.size) <= max_edge and not needs_rotation and not needs_transcode:
        return None

    im = ImageOps.exif_transpose(im)
    im.thumbnail((max_edge, max_edge), PILImage.LANCZOS)

    out = BytesIO()
    if fmt == "PNG":
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    if fmt == "WEBP":
        im.save(out, "WEBP", quality=IMAGE_JPEG_QUALITY)
        return out.getvalue(), "image/webp"

    # Everything else lands as JPEG, which is what phone cameras send anyway.
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(out, "JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), "image/jpeg"


@require_POST
@login_required
def process_upload(request, upload_id):
    """Download the object, decode its barcode, and file it if a SKU matches."""
    upload = get_object_or_404(ProductImageUpload, id=upload_id)

    # Idempotency: if already filed, just re-render the filed card.
    if upload.status in (ProductImageUpload.STATUS_MATCHED, ProductImageUpload.STATUS_ASSIGNED):
        return render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True})

    data = None
    codes = []
    try:
        data = _upload_bytes(upload.key)
    except Exception as exc:
        upload.error = str(exc)

    if data is not None:
        try:
            # Imported lazily so the app still runs where libzbar0 isn't installed.
            from pyzbar.pyzbar import decode as zbar_decode
            from PIL import Image

            # Decoded at full resolution, before the downscale below: a Code128
            # label is a small part of the frame, and shrinking first is exactly
            # what would stop it resolving.
            img = Image.open(BytesIO(data))
            codes = [r.data.decode("utf-8", "ignore").strip() for r in zbar_decode(img)]
        except Exception as exc:  # decode failure -> fall back to manual assign
            upload.error = str(exc)

        # Swap the phone-sized original for a display-sized copy. Done after the
        # decode and before the photo is ever served, so nothing downstream —
        # the games, the PDF, the upload card — deals with a 5MB file again.
        try:
            shrunk = _shrink_image(data)
            if shrunk:
                body, content_type = shrunk
                upload.key = _replace_upload_bytes(upload.key, body, content_type)
        except Exception as exc:
            # A photo that won't resize is still a usable photo; keep the
            # original rather than losing the upload over it.
            upload.error = (upload.error + " | " if upload.error else "") + f"resize: {exc}"

    scanned = None
    for code in codes:
        if not code:
            continue
        scanned = FinishedProduct.objects.filter(sku=code).first()
        if scanned:
            upload.detected_sku = code
            break

    # **On a walk, the peg is the claim and it beats a barcode that
    # disagrees.** The blank picker on the batch page is a coarse statement
    # covering forty photos, so there a decoded symbol is the better evidence.
    # A stop is the opposite: made per photo, at the peg, by somebody looking
    # at the scarf — while a symbol that happens to resolve in shot may belong
    # to the colorway hanging two inches to the left. Reported either way,
    # because a silent resolution in either direction is how a photo ends up
    # on the wrong colorway with nothing to say so.
    stop = _walk_stop(request.POST)
    expected = stop["product"] if stop else None
    product = expected or scanned
    mismatch = scanned if expected and scanned and scanned != expected else None

    if product:
        fpi = _attach_image(upload, product)
        upload.finished_product = product
        upload.product_image = fpi
        upload.status = ProductImageUpload.STATUS_MATCHED
        upload.save()
        return render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True, "mismatch": mismatch,
                       "expected": expected})

    # No barcode / no match -> uploader assigns it inline, with the blank
    # they said they were shooting already typed into the box. Run through
    # `slug` rather than trusted as sent: it is a value from the page, it
    # goes straight into a search box, and `slug` is the same function that
    # built the SKU half it is meant to match.
    upload.save()

    # **An empty peg is the fresh-board case, and it is the main one.** Set
    # the display up, walk it once, and come away with the photos *and* the
    # map — which only works if naming the colorway is quick. So the photo
    # that was just taken orders the list: the bands it shows against the
    # bands each colorway claims, exact first, then supersets, then any
    # overlap, then the rest alphabetically.
    #
    # Ordering only. A band set is not an identity — dozens of colorways are
    # blue-and-green — so this moves the answer near the top and the person
    # holding the scarf does the rest. Same rule `colorbands` follows
    # everywhere else: fill the form in, never decide.
    candidates = []
    confirmed = total = 0
    photo_bands = []
    if stop and expected is None:
        if data is not None:
            try:
                photo_bands = colorbands.bands_from_image(BytesIO(data))
            except Exception:
                photo_bands = []
        candidates, _ = photowalk.candidates(stop["fixture"], photo_bands)
        confirmed, total = photowalk.rankable(stop["fixture"])

    return render(request, "scarves/partials/upload_card.html",
                  {"upload": upload, "needs_assign": True,
                   "prefix": skus.slug(request.POST.get("prefix")),
                   # An empty peg: pick the colorway and it lands on the peg
                   # as well as on the photo. Carried through the search so
                   # the buttons it returns can do both — see `assign_upload`.
                   "stop": stop,
                   "candidates": candidates,
                   "photo_bands": photo_bands,
                   "band_names": [
                       colorbands.BAND_LABELS.get(b, b) for b in photo_bands
                   ],
                   # Stated, because a list that fell back to alphabetical for
                   # want of confirmed bands looks exactly like one where the
                   # photo matched nothing — and only one of those has a fix.
                   "confirmed_count": confirmed,
                   "candidate_total": total})


@page_meta(
    title="Photograph a Display",
    description="Go round a board peg by peg and photograph what hangs there. The "
                "peg says what the picture is of, so nothing has to be typed "
                "or scanned.",
    category="Products",
)
@login_required
def photo_walk_index(request):
    """The boards, as places to photograph rather than places to restock."""
    fixtures = []
    for fixture in DisplayFixture.objects.active().select_related(
        "raw_product"
    ):
        walk = photowalk.stops(fixture)
        products = [stop["product"] for stop in walk if stop["product"]]
        # Counted here because it is the only number that decides which board
        # to walk: how much of it the catalogue still has no picture of.
        missing = [p for p in products if not p.images.exists()]
        fixtures.append({
            "fixture": fixture,
            "stops": len(walk),
            "assigned": len(products),
            "missing": len(missing),
        })
    # Most to photograph first, which is the only ordering worth having here.
    fixtures.sort(key=lambda entry: (-entry["missing"], entry["fixture"].name))
    return render(request, "scarves/photo_walk_index.html", {"fixtures": fixtures})


@page_meta(
    title="Photograph a Display (one board)",
    description="One peg at a time: what to shoot, whether it already has a "
                "photo, and where you were when you stopped.",
    category="Products",
    show_in_index=False,
)
@login_required
def photo_walk(request, fixture_id):
    """One stop on the walk. **Where you are is the URL and nothing else.**

    Fifteen photos in, get distracted, come back to `?row=3&column=5` — a
    stored cursor would be a second place the answer lived, and the one it
    disagreed with would be the one somebody was looking at. It also means a
    peg can be handed to somebody else as a link.

    An address that isn't a stop advances to the next one that is, so a
    bookmark taken before the board was rearranged resumes rather than
    failing — see `photowalk.stop_at`.
    """
    fixture = get_object_or_404(DisplayFixture, pk=fixture_id, is_active=True)

    def _int(name):
        try:
            return int(request.GET[name])
        except (KeyError, TypeError, ValueError):
            return None

    stop, walk = photowalk.stop_at(fixture, _int("row"), _int("column"))
    product = stop["product"] if stop else None

    return render(request, "scarves/photo_walk.html", {
        "fixture": fixture,
        "stop": stop,
        "product": product,
        "label": photowalk.label_for(product),
        # Stated so a photo that already exists is a choice rather than a
        # surprise: retake it, or move on. Nothing here decides which.
        "existing": list(product.images.all()[:3]) if product else [],
        # Plain navigation. "Peg 17 of 42" is how somebody knows roughly how
        # much board is left, not a score — nothing counts walks, and there is
        # no completeness anywhere.
        "index": (
            next(
                (
                    i + 1
                    for i, candidate in enumerate(walk)
                    if (candidate["row"], candidate["column"])
                    == (stop["row"], stop["column"])
                ),
                None,
            )
            if stop
            else None
        ),
        "total": len(walk),
        "next_url": _walk_url(fixture, photowalk.next_after(walk, stop)) if stop else "",
        "done": stop is None,
        "use_s3": settings.USE_S3,
    })


def search_products(q, limit=10):
    """Active products matching a typed name or SKU.

    One definition, three callers — the upload page's picker, the label
    page's hand-picked list, and the close page's "I'm holding a tag for
    this". They differ in what a result *does*, never in what counts as a
    match, and a second copy of the query is how one of them quietly starts
    finding a different set of products.
    """
    q = (q or "").strip()
    if not q:
        return FinishedProduct.objects.none()
    return FinishedProduct.objects.filter(
        Q(name__icontains=q) | Q(sku__icontains=q),
        is_active=True,
    ).order_by("name")[:limit]


@login_required
def product_search(request):
    """HTMX type-ahead: products matching the typed name or SKU."""
    q = (request.GET.get("q") or "").strip()
    upload_id = request.GET.get("upload_id")
    mode = request.GET.get("mode")
    for_labels = mode == "labels"
    # The sheet editor needs to know which sheet it is adding to, so a result
    # can be a form that posts on its own. Nothing else about the search
    # changes — a sheet narrows nothing, because what belongs on it is
    # exactly the judgement the person typing is making.
    for_sheet = mode == "sheet"
    # And a fourth: picking baths for a sheet that does not exist yet. It
    # cannot share `mode=sheet`, whose results post straight onto a run —
    # there is no run to post to — and it cannot share `mode=labels`, which
    # greys out anything with no SKU. A SKU is needed to print a sticker, not
    # to dye a bath.
    for_plan = mode == "plan"
    # Which kind of session is being planned, so the results can say why a
    # colorway can't join *this* one. Not a filter: an oven colorway hidden
    # from the dye-room search is one somebody searches for, doesn't find,
    # and concludes is missing from the app — the same call the label picker
    # makes about a product with no SKU.
    oven = request.GET.get("oven") == "1"
    run_pk = request.GET.get("run")
    # Passed straight through to the assign call's URL. The search itself is
    # unchanged by it — a walk narrows nothing, because the whole reason this
    # peg is being typed into is that the map doesn't know what hangs there.
    stop_query = _stop_query(_walk_stop(request.GET))

    products = search_products(q)

    # Same search, two click behaviours: the upload page assigns the product
    # to an upload, the label page adds it to a list. Only the template
    # differs, so it's picked here rather than duplicating the query.
    if for_plan:
        template = "scarves/partials/bath_pick_results.html"
    elif for_sheet:
        template = "scarves/partials/sheet_item_results.html"
    elif for_labels:
        template = "scarves/partials/label_item_results.html"
    else:
        template = "scarves/partials/product_search_results.html"
    return render(request, template, {
        "products": products, "upload_id": upload_id, "stop_query": stop_query,
        "run_pk": run_pk,
        "oven": oven,
    })


def _walk_stop(data):
    """The peg a photo was taken at, from whatever the page sent.

    `None` on the batch page, which is not standing anywhere. Anything
    unparseable is also `None` rather than an error: the walk is navigation,
    and the worst a bad address can do is fall back to filing the photo the
    way the batch page would.
    """
    try:
        fixture_id = int(data.get("fixture"))
        row = int(data.get("row"))
        column = int(data.get("column"))
    except (TypeError, ValueError):
        return None

    fixture = DisplayFixture.objects.active().filter(pk=fixture_id).first()
    if fixture is None:
        return None
    position = fixture.positions.filter(row=row, column=column).first()
    if position is not None and not position.is_home:
        return None
    return {
        "fixture": fixture,
        "row": row,
        "column": column,
        "position": position,
        "product": position.finished_product if position else None,
    }


def _stop_query(stop):
    """`?fixture=…&row=…&column=…`, or empty off a walk."""
    if not stop:
        return ""
    return (
        f"?fixture={stop['fixture'].pk}&row={stop['row']}&column={stop['column']}"
    )


@require_POST
@login_required
def assign_upload(request, upload_id):
    """File a manually-picked product for an upload the barcode couldn't match.

    On a walk it does a second thing: **an empty peg gets the colorway you
    just picked.** You are standing in front of the hook, you have just said
    what is hanging on it, and the map not knowing is the reason the walk had
    nothing to tell you here. Making that a separate trip to the map editor
    would mean the fact is known at the wall and recorded nowhere.

    **An occupied peg is never overwritten**, the same refusal
    `copy_board_layout` makes: a peg that already names a colorway is
    somebody's decision, and disagreeing with it is a map question rather than
    a photo one.
    """
    upload = get_object_or_404(ProductImageUpload, id=upload_id)
    product = get_object_or_404(FinishedProduct, id=request.POST.get("product_id"))

    if upload.status not in (ProductImageUpload.STATUS_MATCHED, ProductImageUpload.STATUS_ASSIGNED):
        fpi = _attach_image(upload, product)
        upload.finished_product = product
        upload.product_image = fpi
        upload.status = ProductImageUpload.STATUS_ASSIGNED
        upload.save()

    stop = _walk_stop(request.GET)
    response = render(request, "scarves/partials/upload_card.html",
                      {"upload": upload, "matched": True})
    if stop is None:
        return response

    position = stop["position"]
    if position is None:
        position = photowalk.position_for(stop["fixture"], stop["row"], stop["column"])
    if position.finished_product_id is None:
        position.finished_product = product
        # The signal on DisplayPosition writes `display_slots` from here, so
        # this peg starts counting towards the close and the restock walk
        # immediately — which is what putting a colorway on the map means.
        position.save(update_fields=["finished_product"])
        messages.success(
            request,
            f"Filed the photo and put {product.name} on "
            f"{stop['fixture'].name} r{stop['row']}c{stop['column']}.",
        )
    else:
        messages.info(
            request,
            f"Filed the photo. Left the peg as it was — it already says "
            f"{position.finished_product.name}.",
        )

    # The walk moves on by navigating, so the redirect rides on the response
    # to the click rather than being a second request the page has to make.
    walk = photowalk.stops(stop["fixture"])
    nxt = photowalk.next_after(walk, stop)
    response["HX-Redirect"] = _walk_url(stop["fixture"], nxt)
    return response


def _walk_url(fixture, stop):
    """Where the walk goes next, or back to the board when it is done."""
    url = reverse("photo_walk", args=[fixture.pk])
    if stop is None:
        return url + "?done=1"
    return f"{url}?row={stop['row']}&column={stop['column']}"
