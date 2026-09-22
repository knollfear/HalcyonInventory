"""Read an invoice and print what came back, without touching anything.

The page is the product; this is the bench. Pointing it at a stack of real
invoices is how you find out whether the reading is worth confirming before
anybody builds a habit around it — and it writes no row, so a bad reading
costs nothing but the call.

    docker compose exec web python manage.py read_invoice /home/app/samples/w2d.pdf
"""

import mimetypes
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from scarves import invoiceread


class Command(BaseCommand):
    help = "Read a supplier invoice (PDF, photo or pasted text) and print the lines."

    #: Dated on purpose. A hardcoded price table is the kind of thing that
    #: goes stale silently, so the rate is printed beside every figure it
    #: produces — a cost you cannot check is worth less than the tokens,
    #: which are a fact either way.
    RATES_AS_OF = "2026-06"
    RATES = {                       # $ per million tokens, in / out
        "claude-opus-5": (5.00, 25.00),
        "claude-sonnet-5": (2.00, 10.00),
        "claude-haiku-4-5": (1.00, 5.00),
    }

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", help="PDFs, photos or .txt to read.")
        parser.add_argument(
            "--model",
            default=None,
            help=(
                "Read with this model instead of the configured one "
                "(settings.CLAUDE_MODEL). The point of the flag: run the same "
                "invoice through two and compare what they made of it."
            ),
        )

    def handle(self, *args, **options):
        from scarves.models import RawProduct

        products = list(
            RawProduct.objects.active()
            .select_related("category").order_by("category__name", "name")
        )
        names = {p.pk: p.name for p in products}
        self.stdout.write(
            f"{len(products)} active blanks to match against. "
            f"Reading with {options['model'] or invoiceread.model_name()}.\n"
        )

        for raw in options["paths"]:
            path = Path(raw)
            if not path.exists():
                raise CommandError(f"No such file: {path}")

            content_type = mimetypes.guess_type(path.name)[0] or ""
            if content_type.startswith("text/") or path.suffix.lower() in (".txt", ".eml"):
                # A .txt is the pasted-email path, which is the one most of
                # these arrive by. Same reader either way.
                reading = invoiceread.read_text(
                    path.read_text(errors="replace"), products=products,
                    model=options["model"],
                )
            else:
                reading = invoiceread.read(
                    path.read_bytes(),
                    content_type=content_type,
                    filename=path.name,
                    products=products,
                    model=options["model"],
                )

            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{path.name}"))
            if reading.error:
                self.stdout.write(self.style.ERROR(f"  {reading.error}"))
                continue

            self.stdout.write(
                f"  order {reading.order_number or '-'} · "
                f"{reading.supplier_name or 'supplier not stated'} · "
                f"{reading.ordered_on or 'no date'}"
            )
            for line in reading.lines:
                match = names.get(line.raw_product_id, "— not matched —")
                self.stdout.write(
                    f"    {str(line.quantity or '?'):>4} × {match:<30} "
                    f"@ {line.unit_cost if line.unit_cost is not None else '?':>8}"
                    f"   total {line.line_total if line.line_total is not None else '?':>9}"
                    f"   « {line.description}"
                )
            self.stdout.write(f"  {reading.note}")
            self.stdout.write("  " + self._cost(reading))

    def _cost(self, reading):
        """Tokens always; dollars only when the rate is one we know.

        The tokens are the fact. The dollars are a conversion at a published
        rate that can move without this file noticing, so the rate and the
        month it came from are printed with them — an unlabelled price is the
        kind of number somebody quotes back a year later.
        """
        tokens = f"{reading.input_tokens} in / {reading.output_tokens} out"
        # The API answers with the dated snapshot it actually ran
        # (`claude-haiku-4-5-20251001`), so match on the prefix — an exact
        # lookup silently drops the price for every model that carries a date.
        rate = next(
            (r for name, r in self.RATES.items() if reading.model.startswith(name)),
            None,
        )
        if rate is None:
            return f"  {tokens} tokens"
        cents = (
            reading.input_tokens * rate[0] + reading.output_tokens * rate[1]
        ) / 1_000_000
        return (
            f"  {tokens} tokens  ≈ ${cents:.4f} "
            f"at ${rate[0]:g}/${rate[1]:g} per Mtok, {self.RATES_AS_OF}"
        )
