"""Read a supplier's invoice and propose what it was an order of.

**The model is deliberately the dumbest part of this.** It gets the document
and the list of blanks we buy, and it answers one question per line: which of
these is this, and how many at what each. It does not decide anything. Every
number it produces lands in a box on a page somebody then corrects, and the
line as the invoice worded it is printed beside the box — because a
suggestion whose evidence is hidden is not something anybody can agree or
disagree with, which is the argument *The app advises, a person decides* in
`CLAUDE.md` makes about par.

That is also why there is no confidence score, no "did you mean", and no
second pass. A reading that is 80% right and openly editable is worth more
than one that is 95% right and arrives looking settled — the first gets read,
the second gets rubber-stamped. Adding cleverness here does not make the page
safer; it makes the page less likely to be read.

**What it is asked for, and what is derived.** Quantity and line total come
off the document, because they are printed there. The per-unit cost is
*derived* — total over quantity — whenever both are readable, and only falls
back to a printed unit price when it isn't. An invoice line is the pack (see
*What is deliberately not here yet* in `docs/claude/stock.md`), and the
number this shop has always wanted is what one costs, which is the division
somebody used to do by hand on the way to the basket.

**A failure here is never fatal to the page.** No key configured, an API
having a bad afternoon, a photo of somebody's thumb: all of them return a
reading with an `error` and no lines, and the review page still offers an
empty form with an *Add a row* button. Typing a nine-line bill out by hand is
the job as it exists today, so the worst case of this feature is the status
quo.
"""

from __future__ import annotations

import json
import logging
import re

from .models import RawProduct
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.conf import settings

logger = logging.getLogger(__name__)

#: The default, and why: this is a one-page document a couple of times a
#: month, so a year of it is a rounding error against one mis-booked
#: delivery, and reading a table off a photograph and matching it to a
#: catalogue is exactly where a better model earns its keep. Nothing here is
#: high volume.
#:
#: `CLAUDE_MODEL` overrides it, and `read_invoice --model` overrides that for
#: one document — which is how to find out whether a cheaper one is as good
#: on real paperwork, rather than by arguing about it.
DEFAULT_MODEL = "claude-opus-5"


def model_name():
    return getattr(settings, "CLAUDE_MODEL", "") or DEFAULT_MODEL


#: Models that have rejected `effort`, learned at runtime rather than listed.
#:
#: Haiku 4.5 returns a 400 for it where the 5-family takes it happily, and a
#: hardcoded list of which models accept which parameter is a list that is
#: wrong the week after it is written. So the first call for a model that
#: refuses costs one retry, and every call after it skips the parameter —
#: the alternative being that picking a cheaper model silently turns the
#: whole feature off, which is what it did before this.
_NO_EFFORT = set()

#: The long edge a photographed invoice is reduced to. The API downsamples
#: past roughly this anyway, and a 5MB phone JPEG of a piece of A4 is mostly
#: white paper.
MAX_EDGE = 1800

#: Image types that can be sent as they are. Anything else PIL can open —
#: HEIC in particular, which is what an iPhone actually shoots — is
#: transcoded to JPEG first.
NATIVE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

SYSTEM = """\
You are reading a supplier's invoice or packing slip for a small business \
that buys undyed silk scarves, undyed yarn and market-stall notions, dyes \
them and sells them.

Report what the document says. Do not correct it, do not infer quantities it \
does not state, and do not invent an order number that is not printed on it.

Rules:
- List one entry per line of goods. Skip shipping, handling, tax, discounts \
and totals: they buy no stock.
- Quantity is the number of units that line delivered. If the line is priced \
per pack, quantity is the number of units in the pack, not the number of \
packs.
- Give the line total exactly as printed. Give the unit price only if the \
document prints one.
- Match each line to one of the catalogue blanks listed below when you are \
reasonably sure, using its id. Use null when you are not sure, when the line \
is something this catalogue does not carry, or when two blanks fit equally \
well. A wrong match costs somebody a correction; a null costs them a \
dropdown. Prefer the null.
- Copy the description across verbatim from the document, so a person can \
check your match against what they are holding.
"""


@dataclass
class ReadLine:
    """One proposed line. Every field is a suggestion, including the match."""

    description: str = ""
    quantity: int | None = None
    line_total: Decimal | None = None
    unit_cost: Decimal | None = None
    raw_product_id: int | None = None
    #: True when the match came from a wording somebody confirmed before,
    #: rather than from the reading. The page says which, because a matched
    #: row whose basis is invisible is one nobody checks.
    remembered: bool = False


@dataclass
class ReadInvoice:
    order_number: str = ""
    supplier_name: str = ""
    ordered_on: date | None = None
    lines: list[ReadLine] = field(default_factory=list)
    error: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def note(self):
        """One line for `SupplierInvoice.read_note` — diagnostic, not state."""
        if self.error:
            return f"read failed: {self.error}"
        return (
            f"read {len(self.lines)} line{'' if len(self.lines) == 1 else 's'}; "
            f"matched {sum(1 for l in self.lines if l.raw_product_id)} "
            f"({sum(1 for l in self.lines if l.remembered)} remembered); "
            f"order {self.order_number or '-'}; supplier {self.supplier_name or '-'}"
            + (f"; {self.model} {self.input_tokens}in/{self.output_tokens}out"
               if self.model else "")
        )


SCHEMA = {
    "type": "object",
    "properties": {
        "order_number": {
            "type": "string",
            "description": "The order, invoice or PO number printed on the document. Empty string if there is none.",
        },
        "supplier": {
            "type": "string",
            "description": "Who sent it. Empty string if it does not say.",
        },
        "ordered_on": {
            "type": "string",
            "description": "The document's date as YYYY-MM-DD. Empty string if unreadable.",
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["integer", "null"]},
                    "line_total": {"type": ["string", "null"]},
                    "unit_price": {"type": ["string", "null"]},
                    "blank_id": {"type": ["integer", "null"]},
                },
                "required": [
                    "description",
                    "quantity",
                    "line_total",
                    "unit_price",
                    "blank_id",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["order_number", "supplier", "ordered_on", "lines"],
    "additionalProperties": False,
}


def normalise_description(text):
    """One spelling of a supplier's line, for comparing two of them.

    Case, run-together spaces and the two kinds of quote mark are the ways
    the same line arrives looking different — a PDF gives `21" x 76"`, the
    email gives `21\u201d x 76\u201d`, and a person retyping it gives neither.
    Nothing else is touched: the wording *is* the key, and a matcher that
    starts dropping words is one that starts matching the wrong blank.
    """
    if not text:
        return ""
    cleaned = str(text)
    for fancy, plain in (("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"),
                         ("\u2019", "'"), ("\u2013", "-"), ("\u2014", "-")):
        cleaned = cleaned.replace(fancy, plain)
    return " ".join(cleaned.split()).casefold()


#: A pack clause on the end of a supplier's line: a separator, then a count,
#: then `x`, then whatever unit they use. `Angel DK - 10 x 100g SKEINS`.
#:
#: The separator is required, which is what keeps it off the one case that
#: must survive: `Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle` has an
#: `x` and two numbers and is a *size*, not a pack — strip that and the blank
#: loses the thing that distinguishes it from every other habotai.
_PACK_CLAUSE = re.compile(r"\s*[-–—,]\s*\d+\s*[xX×]\s*\S+.*$")


def product_name(description):
    """A supplier's line, as a name for the thing rather than for the order.

    `Angel DK - 10 x 100g SKEINS` is how many arrived in a box, and it ends
    up on reference sheets, barcode labels and the Square till — where "10 x
    100g SKEINS" is noise on every one of them, and wrong the day they change
    the pack size.

    Only the *name* is trimmed. `invoice_description` keeps the line exactly
    as written, because that is a key: it has to match the next invoice
    character for character, and a tidied copy would quietly stop matching.

    A suggestion, and only ever a suggestion — it arrives in a text box
    somebody is looking at before they press the button. So it errs toward
    trimming: a wrong trim costs one correction on a form already open, and a
    missed one costs a pack size printed on a hundred labels.
    """
    cleaned = " ".join((description or "").split())
    trimmed = _PACK_CLAUSE.sub("", cleaned).strip(" -–—,")
    # Never trim it away to nothing: a line that is *only* a pack clause has
    # no name in it, and an empty box is worse than a wordy one.
    return trimmed or cleaned


def known_match(description, products):
    """The blank whose remembered wording is exactly this line, or None.

    **This beats the model rather than asking it.** A wording somebody has
    already confirmed is a certainty; a reading is a suggestion, and where
    the two disagree the certainty wins. `Machine Hemmed 8mm Habotai Scarves
    21" x 76" Circle` is an Infinity, and no amount of cleverness gets there
    from the words — but it only ever has to be said once.

    Exact match on the normalised string, deliberately. No fuzzy matching, no
    scoring, no nearest neighbour: a wrong match here arrives looking
    unremarkable and gets confirmed, which is worse than the dropdown it
    saved.
    """
    wanted = normalise_description(description)
    if not wanted:
        return None
    for product in products:
        if product.invoice_description and normalise_description(
            product.invoice_description
        ) == wanted:
            return product.pk
    return None


def catalogue_text(products):
    """The blanks, as the only thing the model is allowed to match against.

    Ids rather than names come back, so a hallucinated blank cannot land: an
    id that isn't in this list is dropped on the way in.
    """
    lines = []
    for p in products:
        bits = [f"id={p.pk}", p.name, f"({p.category.name})"]
        if p.sku:
            bits.append(f"sku {p.sku}")
        if p.invoice_description:
            # The supplier's own wording, when somebody has confirmed one.
            # The exact match is applied after the reading regardless — this
            # is here because it also helps the *near* misses, where the
            # wording changed by a word and the alias no longer fires.
            bits.append(f'— invoiced as "{p.invoice_description}"')
        elif p.notes:
            bits.append(f"— {' '.join(p.notes.split())[:80]}")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def read(data, content_type="", filename="", products=None, model=None):
    """Read one invoice document. Never raises: a failure comes back as `error`."""
    try:
        block = _document_block(data, content_type, filename)
    except Exception as exc:                      # unreadable file, not a crash
        logger.warning("invoice: could not prepare %s (%s)", filename, exc)
        return ReadInvoice(
            error=f"That file could not be opened as a PDF or a photo ({exc})."
        )
    return _ask([block, {"type": "text", "text": "Read this invoice."}], products, model)


def read_text(text, products=None, model=None):
    """Read an invoice that arrived as text — pasted out of an email.

    **The same reader, because the job is the same job.** A supplier's order
    confirmation is usually an email before it is ever a PDF, and getting a
    screenshot of one off a phone and into a file picker is more work than
    selecting the message and pressing copy. Text is also the *easiest* thing
    to read: no photograph, no scan, no column that wrapped.

    It does not go in as an order number and a set of typed figures, which
    would be the same data entry with extra steps. It goes in as whatever was
    copied — headers, footers, the bit of signature that came with it — and
    comes back as rows to confirm, exactly like a PDF does.
    """
    text = (text or "").strip()
    if not text:
        return ReadInvoice(error="Nothing was pasted in.")
    return _ask(
        [{"type": "text",
          "text": "Read this invoice. It was pasted out of an email, so it "
                  "may carry headers, signatures or wrapped columns that are "
                  "not part of the order.\n\n" + text[:120_000]}],
        products,
        model,
    )


def _ask(content, products=None, model=None):
    """One call, whatever the document arrived as."""

    if products is None:
        products = list(
            RawProduct.objects.active()
            .select_related("category")
            .order_by("category__name", "name")
        )

    key = getattr(settings, "CLAUDE_API_KEY", "")
    if not key:
        return ReadInvoice(
            error="No CLAUDE_API_KEY is set, so nothing was read. The form "
                  "below still works — add the rows by hand."
        )

    chosen = model or model_name()
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        system = SYSTEM + "\nCatalogue blanks:\n" + catalogue_text(products)

        def ask(with_effort):
            output_config = {"format": {"type": "json_schema", "schema": SCHEMA}}
            if with_effort:
                # An invoice is a table. The judgement is all in the matching,
                # and the page is where that gets settled anyway.
                output_config["effort"] = "low"
            return client.messages.create(
                model=chosen,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config=output_config,
            )

        try:
            response = ask(chosen not in _NO_EFFORT)
        except Exception as exc:
            if "effort" not in str(exc):
                raise
            logger.info("invoice: %s does not take effort; retrying without", chosen)
            _NO_EFFORT.add(chosen)
            response = ask(False)
    except Exception as exc:
        logger.warning("invoice: read failed (%s)", exc)
        return ReadInvoice(error=str(exc)[:300])

    try:
        raw = next(b.text for b in response.content if b.type == "text")
        payload = json.loads(raw)
    except Exception as exc:
        logger.warning("invoice: unparseable response (%s)", exc)
        return ReadInvoice(error="The reading came back in a shape this page could not use.")

    reading = _to_reading(payload, products)
    # **What it cost, as tokens rather than a claim.** Printed by the bench
    # command and kept on the draft's note, so "is a cheaper model good
    # enough" is a question somebody can answer off two real documents
    # instead of a guess.
    reading.model = response.model
    usage = getattr(response, "usage", None)
    if usage is not None:
        reading.input_tokens = getattr(usage, "input_tokens", 0) or 0
        reading.output_tokens = getattr(usage, "output_tokens", 0) or 0
    return reading


def _to_reading(payload, products):
    known_ids = {p.pk for p in products}
    reading = ReadInvoice(
        order_number=(payload.get("order_number") or "").strip(),
        supplier_name=(payload.get("supplier") or "").strip(),
        ordered_on=_date(payload.get("ordered_on")),
    )
    for item in payload.get("lines") or []:
        quantity = item.get("quantity")
        quantity = int(quantity) if isinstance(quantity, int) and quantity > 0 else None
        total = _money(item.get("line_total"))
        stated = _money(item.get("unit_price"))
        blank_id = item.get("blank_id")
        description = " ".join((item.get("description") or "").split())[:300]
        # A blank the catalogue doesn't have is a blank nobody can book to, so
        # it comes through as "not matched" rather than as an id the page
        # would then have to defend itself against.
        proposed = blank_id if blank_id in known_ids else None
        remembered = known_match(description, products)
        reading.lines.append(ReadLine(
            description=description,
            quantity=quantity,
            line_total=total,
            unit_cost=unit_cost(total, quantity, stated),
            # The remembered wording wins. It is a person's own past answer
            # to this exact line; the model's is a guess at it.
            raw_product_id=remembered or proposed,
            remembered=remembered is not None,
        ))
    return reading


def unit_cost(total, quantity, stated=None):
    """What one costs: the division, with the printed price as the fallback.

    The pack is the thing the document actually states — *$87.10 for 10* —
    and the per-unit figure is the one this shop needs. Deriving it is why
    invoice ingestion was the right home for pack size in the first place.
    Rounded to the cent, which is what money has.
    """
    if total is not None and quantity:
        return (total / Decimal(quantity)).quantize(Decimal("0.01"))
    return stated


def _money(value):
    if value in (None, ""):
        return None
    try:
        cleaned = str(value).replace("$", "").replace(",", "").strip()
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _document_block(data, content_type="", filename=""):
    """The one content block the document travels in.

    PDFs go as documents, photographs as images, and anything a phone shoots
    that the API does not take — HEIC — is transcoded on the way. A scanned
    invoice arrives as whichever of the two the scanner felt like, so both
    have to work without asking anybody to convert anything.
    """
    import base64

    content_type = (content_type or "").lower().split(";")[0].strip()
    name = (filename or "").lower()

    if content_type == "application/pdf" or name.endswith(".pdf"):
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode("ascii"),
            },
        }

    if content_type in NATIVE_IMAGE_TYPES and len(data) <= 4 * 1024 * 1024:
        media_type, payload = content_type, data
    else:
        media_type, payload = _as_jpeg(data)

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(payload).decode("ascii"),
        },
    }


def _as_jpeg(data, max_edge=MAX_EDGE):
    """Whatever PIL can open, as a JPEG no wider than `max_edge`.

    HEIC works because `apps.py` registers the opener at startup. Nothing is
    upscaled — a small scan of a printed invoice is already at the resolution
    it was made at, and inventing pixels does not put text back.
    """
    from PIL import Image, ImageOps

    im = Image.open(BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    out = BytesIO()
    im.save(out, format="JPEG", quality=85)
    return "image/jpeg", out.getvalue()
