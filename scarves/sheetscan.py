"""Reading the tick boxes off a photo of a production sheet.

The sheet already comes back by QR code — scan it, tap the baths you did,
submit. This is the shortcut: photograph the marked paper instead, and the
same list arrives already ticked.

**The photo says which sheet it is.** Uploading happens at one page for all
runs, not at a run's own page, so nothing has identified the sheet before the
picture arrives — the QR in the header does it, and the token it carries is
what admits the marks to that run. That is the same bargain `secret/` makes
everywhere here, arriving by camera instead of by address bar.

When the QR can't be read the person types the code printed beside it. That
is almost always what has happened: the code is on every page, so a
whole-page shot includes it, and a failure to read one means the photo came
out soft rather than that anybody is holding the wrong paper. Typing it is a
way through, not an interrogation.

**The likely failure is the photograph, not the sheet.** Soft focus, a hurried
frame, a third-generation photocopy — and it fails *partially*, taking out
some rows and leaving others. So a caller is told how many rows it read as
well as how many were filled: a count of what was found reads as a complete
answer unless something says what was missed.

**It never applies anything.** What it produces is a pre-filled form, which
the person then looks at and submits. That is the whole safety argument, and
it is the same rule `colorbands` follows: it fills the form in, a person
decides. It also means the photo path can never be *worse* than tapping —
at worst it saves zero taps and they tap them anyway.

How a box is found
------------------

The barcode does the hard part. Every row prints one a fixed distance from
its box, so a decoded symbol gives its row's identity *and* the position,
scale and orientation of everything beside it. Locating a tick box is then
arithmetic rather than the general checkbox-recognition problem, which is the
part that would otherwise be hard and unreliable.

Geometry comes from `production.box_geometry()` — the same constants the PDF
draws with — because a scanner carrying its own copy of the layout would
eventually drift from it, and the symptom of that is the worst one available:
the sample window lands on blank paper and every box reads empty, which is
indistinguishable from a careful person who ticked nothing.

How a box is read
-----------------

Ink, not colour. Each barcode is a known pattern of full-black bars on
full-white paper a couple of centimetres from its own box, so it doubles as a
calibration swatch: the dark end and the light end of *this row*, under this
light, at this exposure. The box is then scored on where it falls between
them, which is a ratio and so survives a phone's white balance, a tungsten
bulb in a dye room, and one corner of the page catching a glare.

Because the score is luminance-based, the pen colour mostly doesn't matter —
red, blue, green and pencil all sit far nearer black than paper. Yellow is
the exception and always will be; a highlighter is about as bright as the
page it's on. The sheet says "any pen but yellow" for that reason.

Anything landing between the two thresholds is reported `unsure` rather than
guessed, and shows up on the confirmation page asking to be looked at.
"""

from dataclasses import dataclass, field

from . import production

#: Fraction of the box to ignore around the edge before measuring. The
#: printed border is ink too, and including it would put a floor under every
#: reading — an empty box would score as partly filled and the thresholds
#: would have to be raised to compensate, which costs real sensitivity.
BORDER_INSET = 0.28

#: Score above which a box counts as filled, and below which as empty. The
#: gap between them is deliberately wide: a mark that lands in it is worth a
#: person's glance, and there is somewhere for it to go.
FILLED_AT = 0.35
EMPTY_BELOW = 0.12

#: Percentiles taken from the barcode to stand for "ink" and "paper". Not min
#: and max, which would be one hot pixel and one dark speck.
INK_PERCENTILE = 0.10
PAPER_PERCENTILE = 0.90

FILLED = "filled"
EMPTY = "empty"
UNSURE = "unsure"


@dataclass
class Mark:
    """One decoded row: which row it is, and how full its box looked."""

    code: str         # `SKU#line`, as printed — see production.line_code
    state: str
    score: float
    top: int          # for ordering marks down the page

    @property
    def is_filled(self):
        return self.state == FILLED


def token_in(text):
    """The run token out of a sheet's return URL, or None.

    **It has to be the run route and nothing else.** This used to take the
    last non-empty segment of any URL at all, which is safe exactly as long as
    the only code in frame is the one in the sheet's own header — and wrong
    the moment anything else decodes. `/secret/production/upload/` came back
    as the token `upload`; a URL off a shipping label, a second sheet at the
    edge of the shot or a phone screen on the bench came back as whatever it
    happened to end in. And a stray code doesn't merely fail: `_read` keeps
    the *first* QR that yields anything, so it wins, and names a run that
    does not exist.

    The URLconf already knew about the collision — `upload/` is registered
    ahead of the token route for precisely this reason. Resolving against it
    rather than re-reading the path by hand is what stops the two drifting,
    and rejects every foreign URL by construction.

    Note that `resolve` does not raise for an unknown path here: `mysite`
    ends in a catch-all, so an unmatched URL matches `lost_and_found`
    instead. The check is therefore on the route's *name*, never on whether
    resolution succeeded.
    """
    if not text or "/" not in text:
        return None
    from urllib.parse import urlsplit

    from django.urls import resolve

    try:
        match = resolve(urlsplit(str(text)).path)
    except Exception:
        # An unparseable or relative path. Nothing decodable off a sheet
        # looks like this, and a reading that can't be trusted is one the
        # upload page asks for the printed code instead.
        return None
    if match.url_name != "production_run":
        return None
    return match.kwargs.get("token") or None


#: Where a logged photograph goes, and how long it lives there.
#:
#: **Nothing in the database points at these.** There is no row, no model and
#: no admin — the bucket is browsable and a pointer would only be a second
#: place for the answer to live, which is the place that goes stale the moment
#: the bucket's own lifecycle rule deletes the object underneath it. What a
#: row would have carried rides in the key instead, so a listing of the prefix
#: is the report: sorted by name it comes out in time order, and a read that
#: went badly says so in its own filename.
#:
#: The prefix is the retention group. S3 has no per-object TTL — expiry is a
#: bucket rule matched on a prefix — so everything under here is the thing
#: being timed, and nothing else is.
PHOTO_PREFIX = "sheet_photos/"

#: A week. Long enough to tune the scanner against a real photograph, short
#: enough that a shop's paperwork is not quietly accumulating. A constant
#: rather than a setting, because a retention promise with a dial on it is not
#: a promise — `KEEP_SHEET_PHOTOS` turns the whole thing off, and that is the
#: only lever there is.
PHOTO_KEEP_DAYS = 7


def photo_key(scan, when, suffix=".jpg"):
    """The object key for one logged photograph — and the whole of its record.

    Everything a database row would have held is in the name: when, which
    sheet it claimed to be, how big it was, and what the scanner made of it.
    That is not a compression of the row, it is the reason there is no row.
    A listing of the prefix sorts into time order on its own, and the photos
    worth looking at — `r0` read nothing, `u3` was unsure three times — are
    findable by eye without opening any of them.
    """
    stamp = when.strftime("%Y%m%dT%H%M%S")
    named = scan.qr_token or "unnamed"
    safe = "".join(c for c in named if c.isalnum() or c in "-_")[:40] or "unnamed"
    return (
        f"{PHOTO_PREFIX}{stamp}-{safe}"
        f"-w{scan.width}-r{len(scan.marks)}-f{len(scan.filled)}"
        f"-u{len(scan.unsure)}{suffix}"
    )


#: Below this the Code128 on a row cannot be decoded, whatever is done to the
#: pixels — a 7.7 mil module needs about two pixels to survive, and a sheet is
#: 8.5in across. Measured on a 1308px-wide picture of run 5: the modules landed
#: on 1.18px and **not one of thirteen decode attempts read a single row
#: barcode**, including 4x upscaling with unsharp masking. Interpolation cannot
#: invent a sample that was never taken.
ROWS_NEED_WIDTH = 2200

#: Enough room for a doubled 12MP frame and not for a doubled 48MP one. Past
#: here a photo that failed did not fail for want of pixels — it was soft, or
#: moving, or badly lit — so the second pass would cost hundreds of megabytes
#: to learn nothing.
UPSCALE_MAX_PIXELS = 60_000_000


def _passes(grey):
    """The image, then the same image enlarged, then enlarged and sharpened.

    **Every pass runs and their findings are pooled**, because they are good
    at different things and no single one of them was best. Measured on a
    3024px iPhone photo of run 5:

    ==========================  ====  ==
    pass                        rows  QR
    ==========================  ====  ==
    as-is                         11   0
    2x lanczos                    12   1
    2x lanczos + unsharp           1   1
    ==========================  ====  ==

    Three things fall out of that table and each one is a decision here:

    * **The plain enlargement is the workhorse.** It was the only pass that
      read the whole sheet, and it recovered a QR that the native-resolution
      pass missed entirely — which on the upload page is the difference
      between a photo that names its run and one that asks you to type a code
      off the paper.
    * **Sharpening wrecks row barcodes.** Twelve down to one. It survives as
      a last pass only because it is what rescued the QR out of a 1308px
      screenshot where every row was hopeless anyway, and pooling means it can
      contribute that without taking anything away.
    * **Stopping at the first pass that finds a row would have been wrong.**
      That is pass one, which found eleven rows and no QR — so an early exit
      throws away both the twelfth row and the only thing that names the
      sheet.

    Each pass comes with the scale it is drawn at, because pooling them makes
    every pixel coordinate ambiguous otherwise: a row found at 2x reports a
    `top` twice the size of the same row found at 1x, and sorting the pooled
    marks on that puts row one in the middle of the page.
    """
    from PIL import Image, ImageFilter

    yield grey, 1

    if grey.width * grey.height * 4 > UPSCALE_MAX_PIXELS:
        return
    bigger = grey.resize((grey.width * 2, grey.height * 2), Image.LANCZOS)
    yield bigger, 2
    yield bigger.filter(ImageFilter.UnsharpMask(radius=2, percent=150)), 2


@dataclass
class ScanResult:
    marks: list = field(default_factory=list)
    #: Barcodes that decoded but aren't on this sheet — a photo of the wrong
    #: run, or a stray label in frame. Reported rather than ignored, because
    #: "I photographed it and nothing happened" needs an explanation.
    unknown_codes: list = field(default_factory=list)
    error: str = ""
    #: Token read off the QR in the header, if one was legible. This is how a
    #: photo names the run it belongs to; without it the person types the same
    #: code off the sheet.
    qr_token: str = ""
    #: How wide the photo was, so a caller can say *why* nothing came back.
    #: "Couldn't read that photo" and "that photo is too small to hold a row
    #: barcode" send somebody to do completely different things.
    width: int = 0

    @property
    def too_small_for_rows(self):
        """The photo cannot contain a readable row barcode.

        A separate question from whether any were found: a big photo that read
        nothing was soft or badly lit and is worth retaking, while a small one
        will read nothing however carefully it is shot again.
        """
        return bool(self.width) and self.width < ROWS_NEED_WIDTH

    @property
    def named_but_unread(self):
        """It says which sheet this is, and nothing about which baths.

        Worth its own name because it is not a failure — the QR is what the
        upload page exists to get, and the boxes are the bonus. The page it
        hands off to is the run's own, where the boxes are tapped anyway.
        """
        return bool(self.qr_token) and not self.marks

    @property
    def filled(self):
        return [m for m in self.marks if m.state == FILLED]

    @property
    def unsure(self):
        return [m for m in self.marks if m.state == UNSURE]

    @property
    def found_any(self):
        return bool(self.marks) or bool(self.unknown_codes)

    @property
    def filled_codes(self):
        return {mark.code for mark in self.filled}


def _percentile(image, fraction):
    """Luminance at `fraction` of the way up this crop's histogram.

    Straight off PIL's histogram — no numpy in this project, and none needed
    for one 256-bin cumulative sum.
    """
    histogram = image.histogram()
    total = sum(histogram)
    if not total:
        return 0
    target = total * fraction
    running = 0
    for value, count in enumerate(histogram):
        running += count
        if running >= target:
            return value
    return 255


def _score_box(grey, box, ink, paper):
    """How inked `box` is, on a 0 (paper) to 1 (solid) scale for this row."""
    from PIL import ImageStat

    left, top, right, bottom = box
    if right - left < 3 or bottom - top < 3:
        return None
    crop = grey.crop((left, top, right, bottom))
    mean = ImageStat.Stat(crop).mean[0]

    spread = paper - ink
    if spread < 12:
        # Bars and paper came out the same shade: the photo is too dark, too
        # blown out, or too blurred to be measuring anything. Better to say
        # so than to divide by it.
        return None
    return (paper - mean) / spread


def read_sheet(data):
    """Read a photo of a marked sheet. Returns a `ScanResult`.

    `data` is the uploaded bytes, decoded at full resolution — a page holds
    twenty small barcodes rather than one big one, and shrinking first is
    exactly what stops them resolving.

    One decode pass finds both kinds of symbol on the page: the Code128 on
    each row, and the QR in the header. Which run this is comes back as
    `qr_token`; matching marks to rows is the caller's job, once it knows
    which run to match against.
    """
    result = ScanResult()

    try:
        from io import BytesIO

        from PIL import Image, ImageOps
        from pyzbar.pyzbar import decode as zbar_decode
    except Exception as exc:                       # pragma: no cover
        result.error = f"Barcode reading isn't available here ({exc})."
        return result

    try:
        image = Image.open(BytesIO(data))
        image.load()
        # Phones record rotation in EXIF rather than rotating the pixels, so
        # without this a portrait photo is read sideways and nothing decodes.
        image = ImageOps.exif_transpose(image)
        grey = image.convert("L")
        result.width = grey.width

        # Every pass runs and their findings are pooled — see `_passes` for
        # the measurements, and for why stopping at the first pass to find a
        # row would have thrown away both the last row and the QR.
        #
        # **A row is read on the image it was found in.** Each pass carries
        # its own scale, and the tick box is located by arithmetic off the
        # barcode's own rectangle, so a code found at 2x must have its window
        # measured at 2x. Mixing them would put the sample on blank paper,
        # which reads as "nobody ticked anything" — the failure this whole
        # module takes its geometry from `production` to avoid.
        rows = []
        for candidate, at_scale in _passes(grey):
            for code in zbar_decode(candidate):
                if code.type == "QRCODE":
                    if not result.qr_token:
                        seen = token_in(
                            code.data.decode("utf-8", "ignore").strip()
                        )
                        if seen:
                            result.qr_token = seen
                    continue
                rows.append((candidate, at_scale, code))
    except Exception as exc:
        result.error = f"Couldn't read that photo ({exc})."
        return result

    # First pass to *score* a given row wins it — scoring, not merely
    # decoding, and the difference is the whole point. A row can decode on one
    # pass and still have its box fall outside the frame or refuse to score,
    # and claiming it at the moment it decoded would retire the code before it
    # produced anything, so the enlargement that would have read it properly
    # never gets its turn. That cost two of twelve rows on the measured photo.
    done = set()
    for grey, at_scale, code in rows:
        value = code.data.decode("utf-8", "ignore").strip()
        if not value or value in done:
            continue

        rect = code.rect
        try:
            width_pt = production.bars_width(value)
            right_gap, below, size = production.box_geometry(value)
        except Exception:
            continue
        if not width_pt or rect.width <= 0:
            continue

        scale = rect.width / width_pt          # pixels per point, this row

        bars_left = rect.left
        bars_bottom = rect.top + rect.height
        box_right = bars_left - right_gap * scale
        box_left = box_right - size * scale
        box_bottom = bars_bottom + below * scale
        box_top = box_bottom - size * scale

        inset = size * scale * BORDER_INSET
        window = (
            int(box_left + inset), int(box_top + inset),
            int(box_right - inset), int(box_bottom - inset),
        )
        if window[0] < 0 or window[1] < 0:
            continue                            # box fell outside the frame

        bars = grey.crop((rect.left, rect.top,
                          rect.left + rect.width, rect.top + rect.height))
        ink = _percentile(bars, INK_PERCENTILE)
        paper = _percentile(bars, PAPER_PERCENTILE)

        score = _score_box(grey, window, ink, paper)
        if score is None:
            continue

        if score >= FILLED_AT:
            state = FILLED
        elif score < EMPTY_BELOW:
            state = EMPTY
        else:
            state = UNSURE
        done.add(value)
        result.marks.append(
            Mark(
                code=value, state=state, score=round(score, 3),
                # Back to the original image's coordinates, so marks pooled
                # from passes at different scales still sort down the page.
                top=rect.top // at_scale,
            )
        )

    result.marks.sort(key=lambda m: m.top)
    return result


def rows_to_tick(run, scan):
    """Which of `run`'s lines the scan says were done, as their tick keys.

    One mark, one **line** — the reporting sheet now prints a colorway once
    however many baths of it there are, so a mark is an answer about a pile
    of fifteen rather than about one pot. The key handed back is the line's
    own (`production.Line.key`, its first row's pk), which is what the
    checkbox posts and what `?done=` has always carried, so nothing about the
    URL shape or the form changed.

    The barcode still carries the position as well as the SKU. Grouping made
    that redundant for uniqueness — a SKU appears on one line now — and it
    stays as a second check on being pointed at the right sheet.

    Lines already fully recorded are skipped, which is what makes re-reading
    the same photo harmless: the second pass ticks nothing new. A line with
    one bath still open comes back, because there is genuinely something left
    to accept on it.
    """
    filled = scan.filled_codes
    return [
        line.key
        for line in production.lines_for_run(run)
        if not line.is_accepted and production.line_code(line) in filled
    ]


def strays(run, scan):
    """Codes in the photo that aren't lines of `run`.

    Expected to be empty forever. If it isn't, the photo is of some other
    sheet, and saying so costs a line — the marks that *did* match would
    otherwise be applied to this run without comment.
    """
    codes = {production.line_code(line) for line in production.lines_for_run(run)}
    return sorted({mark.code for mark in scan.marks} - codes)
