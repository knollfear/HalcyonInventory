"""Reading the tick boxes off a photo of a production sheet.

The sheet already comes back by QR code — scan it, tap the baths you did,
submit. This is the shortcut: photograph the marked paper instead, and the
same list arrives already ticked.

**What the QR does, and what it doesn't.** Reaching this page at all means
holding the run's token — that is the whole of the authorisation, the bargain
`secret/` makes everywhere here, and it is already spent by the time a photo
is uploaded. So the QR in the photo adds no permission. What it adds is
**binding**: evidence that the paper in the picture is the run the URL says.

That makes the two failures unequal, and they are treated differently:

- **The QR reads and doesn't match.** Positive evidence of the wrong sheet.
  Refused outright, nothing read; there is nothing to weigh.
- **No QR readable at all.** No evidence either way, and the person is
  already authorised. The marks are read, and the page says the sheet
  couldn't be confirmed.

The second is deliberately not a refusal. Glare, a torn corner, a third-
generation photocopy and a hurried frame are all ordinary, and turning any of
them into "start again" spends a real person's patience to buy nothing —
they hold the token either way.

It is said out loud rather than passed over, though, because the evidence is
genuinely weak *here* in a way it wouldn't be elsewhere. Row codes are not
unique across runs, and consecutive sheets tend to be near-identical: print
one, don't report it, print another tomorrow and it lists much the same work.
So "the row codes matched" is not much of a check, and the confirmation step
is doing the real work.

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

    code: str         # `SKU#order`, as printed — see production.row_code
    state: str
    score: float
    top: int          # for ordering marks down the page

    @property
    def is_filled(self):
        return self.state == FILLED


def token_in(text):
    """The run token out of a sheet's return URL, or None.

    The QR holds an absolute URL ending `/secret/production/<token>/`, so the
    token is its last non-empty segment.
    """
    if not text or "/" not in text:
        return None
    parts = [part for part in str(text).split("/") if part]
    return parts[-1] if parts else None


@dataclass
class ScanResult:
    marks: list = field(default_factory=list)
    #: Barcodes that decoded but aren't on this sheet — a photo of the wrong
    #: run, or a stray label in frame. Reported rather than ignored, because
    #: "I photographed it and nothing happened" needs an explanation.
    unknown_codes: list = field(default_factory=list)
    error: str = ""
    #: Token read off the QR in the photo, when it isn't this run's. The sheet
    #: carries its own identity, so photographing the wrong one is a question
    #: that can be answered rather than assumed — and it has to be, because
    #: two sheets printed days apart share most of their SKUs, so the marks
    #: would otherwise land on plausible-looking rows of the wrong run.
    wrong_sheet: str = ""
    #: Whether a QR in the photo positively tied it to this run. False means
    #: no readable code was in frame — the marks are still read, because the
    #: person held the token to get here, but nothing has confirmed the paper
    #: in the picture is this sheet and the page has to say so.
    sheet_confirmed: bool = False

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


def read_sheet(data, known_codes=(), expect_token=None):
    """Read a photo of a marked sheet. Returns a `ScanResult`.

    `data` is the uploaded bytes, decoded at full resolution — a page holds
    twenty small barcodes rather than one big one, and shrinking first is
    exactly what stops them resolving.

    One decode pass finds both kinds of symbol on the page: the Code128 on
    each row, and the QR in the header. With `expect_token` given, a QR that
    *disagrees* stops the read; a QR that can't be found doesn't, and leaves
    `sheet_confirmed` False for the page to report. See the module docstring
    for why those two aren't the same thing.
    """
    result = ScanResult()
    known = set(known_codes)

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
        codes = zbar_decode(grey)
    except Exception as exc:
        result.error = f"Couldn't read that photo ({exc})."
        return result

    if expect_token:
        for code in codes:
            if code.type != "QRCODE":
                continue
            seen = token_in(code.data.decode("utf-8", "ignore").strip())
            if not seen:
                continue
            if seen != expect_token:
                result.wrong_sheet = seen
                return result
            result.sheet_confirmed = True

        # Not finding one is not a refusal — see the module docstring. The
        # caller already held the token to get here, and `sheet_confirmed`
        # stays False so the page can say the sheet wasn't confirmed.

    for code in codes:
        if code.type == "QRCODE":
            continue
        value = code.data.decode("utf-8", "ignore").strip()
        if not value:
            continue
        if value not in known:
            if value not in result.unknown_codes:
                result.unknown_codes.append(value)
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
        result.marks.append(
            Mark(code=value, state=state, score=round(score, 3), top=rect.top)
        )

    result.marks.sort(key=lambda m: m.top)
    return result


def rows_to_tick(run, scan):
    """Which of `run`'s rows the scan says were done.

    One mark, one row: the barcode carries the row's position as well as its
    SKU, so there is nothing to match up by counting. That matters most for
    the case a sheet is *expected* to contain — several baths of the same
    colorway, printed together on purpose.

    Rows already recorded are skipped, which is what makes re-reading the
    same photo harmless: the second pass ticks nothing new rather than
    finding another row with the same SKU to put the mark on.
    """
    filled = scan.filled_codes
    return [
        row.pk
        for row in run.rows.all()
        if not row.is_applied and production.row_code(row) in filled
    ]
