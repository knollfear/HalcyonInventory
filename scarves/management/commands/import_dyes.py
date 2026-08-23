"""Import a dye catalog file into the dye list, without disturbing what's there.

The reason this isn't `loaddata`: the fixtures carry primary keys, and a
`RecipeDye` points at a dye *by* primary key. Loading `dharma_dyes.json` over
a database whose pks drifted rewrites the dye that pk 7 refers to, and every
recipe using it silently changes colour — no error, no clue, and the only
symptom is a scarf on the reference sheet under a band it was never dyed in.

So this matches on content instead, and never overwrites a colour anybody
already recorded.
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scarves.colorutils import hex_to_rgb
from scarves.models import UNCATEGORIZED_BRAND, Dye, DyeBrand, dye_match_key


def normal_hex(value):
    """'#FFEC05', 'ffec05', '#fec' -> '#ffec05'. None if it isn't a colour.

    Hex is the identity this works from, so it has to compare the way a
    person means it: case and a missing '#' are typing, not difference.
    """
    rgb = hex_to_rgb(value)
    return "#%02x%02x%02x" % rgb if rgb else None


def load_entries(path):
    """`{name: hex}` from either shape a dye catalog arrives in.

    The catalogs in `fixtures/` are Django fixtures; a fresh scrape off a
    supplier's page is a plain name-to-hex map. Both say the same thing and
    both get pointed at this command, so both are read.
    """
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise CommandError(f"No such file: {path}")
    except json.JSONDecodeError as exc:
        raise CommandError(f"{path} isn't readable JSON: {exc}")

    if isinstance(data, dict):
        return {str(k): v for k, v in data.items()}

    if isinstance(data, list):
        entries = {}
        for record in data:
            if not isinstance(record, dict) or record.get("model") != "scarves.dye":
                continue
            fields = record.get("fields", {})
            entries[fields.get("name", "")] = fields.get("hex_color", "")
        if entries:
            return entries

    raise CommandError(
        f"{path} is neither a name-to-hex map nor a fixture of scarves.dye rows."
    )


class Command(BaseCommand):
    help = (
        "Import dyes from a catalog file, skipping any colour already on "
        "file. Run with --dry-run first: it prints exactly what would change."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", help="A {name: hex} JSON file, or a dye fixture.")
        parser.add_argument(
            "--brand",
            required=True,
            help="Brand to file new dyes under, e.g. 'Dharma Acid Dyes'. "
                 "Created if it doesn't exist.",
        )
        parser.add_argument(
            "--out-of-stock",
            action="store_true",
            help="Import the new dyes as not in stock — for loading a "
                 "supplier's full range when you only own part of it.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = options["path"]
        dry_run = options["dry_run"]
        entries = load_entries(path)

        brand_name = options["brand"]
        on_file = list(Dye.objects.select_related("brand"))
        # First one wins on a collision: these only ever answer "is this
        # already here", so which of two duplicates answers doesn't change
        # the answer.
        by_hex = {}
        by_name = {}
        for dye in on_file:
            key = normal_hex(dye.hex_color)
            if key:
                # Colour is matched across every brand, because the question
                # it settles is "have I already imported this file".
                by_hex.setdefault(key, dye)
            # Name is matched only within the brand being imported and the
            # uncategorized pile. `Peacock Blue` under Jacquard is a
            # different jar from Dharma's 416 and must not be written over;
            # `Peacock Blue` with no brand at all is somebody's half-typed
            # note, and is exactly what this file finishes.
            if dye.brand.name in (brand_name, UNCATEGORIZED_BRAND):
                by_name.setdefault(dye_match_key(dye.name), dye)

        creating, filling, skipped, conflicts, unreadable = [], [], [], [], []

        for name, raw_hex in entries.items():
            name = " ".join(str(name).split())
            if not name:
                continue
            hex_color = normal_hex(raw_hex)
            if not hex_color:
                unreadable.append((name, raw_hex))
                continue

            match = by_hex.get(hex_color)
            if match:
                # The colour is already here, whatever it is called. This is
                # the rule that makes a re-import safe to run: a dye whose
                # name was tidied up by hand must not come back a second time
                # under the name the file has for it.
                skipped.append((name, match))
                continue

            twin = by_name.get(dye_match_key(name))
            if twin is None:
                creating.append((name, hex_color))
                continue
            if twin.hex_color:
                # Same dye, two different colours. The one in the database
                # was put there by a person; the file is a catalog scrape.
                conflicts.append((name, hex_color, twin))
            else:
                # A dye typed in from a recipe picker, which is a name and
                # nothing else. This is the file finishing that off — the
                # exact cleanup `Dye.needs_review` exists to get done.
                # The name is carried along because `_apply` overwrites it,
                # and the report has to read the same in a dry run as in the
                # real one — a dry run nobody can trust is one nobody runs.
                filling.append((name, hex_color, twin, twin.name))

        if not dry_run:
            self._apply(creating, filling, options)
        elif not DyeBrand.objects.filter(name=brand_name).exists():
            # Said in the dry run because this is what a typo'd --brand looks
            # like, and the symptom of one is a second brand holding half the
            # range with nothing to say they belong together.
            known = ", ".join(DyeBrand.objects.values_list("name", flat=True)) or "none"
            self.stdout.write(self.style.WARNING(
                f"Would create a new brand {brand_name!r}. Brands on file: {known}"
            ))

        self._report(creating, filling, skipped, conflicts, unreadable, dry_run)

    @transaction.atomic
    def _apply(self, creating, filling, options):
        brand, made = DyeBrand.objects.get_or_create(name=options["brand"])
        if made:
            self.stdout.write(f"Created brand {brand.name!r}.")

        in_stock = not options["out_of_stock"]
        Dye.objects.bulk_create([
            Dye(name=name, hex_color=hex_color, brand=brand, in_stock=in_stock)
            for name, hex_color in creating
        ])

        for name, hex_color, twin, _was in filling:
            twin.hex_color = hex_color
            fields = ["hex_color"]
            # A dye typed in from a picker has no real brand either, and the
            # file is the answer to both halves at once.
            if twin.brand.name == UNCATEGORIZED_BRAND:
                twin.brand = brand
                fields.append("brand")
            # The catalog number is what's printed on the jar, so a name
            # typed from memory gains it here. Only ever an addition: a name
            # that already carries a number is left as it is, and so is one
            # whose new name is taken — a rename is a convenience, not worth
            # failing the import over.
            taken = Dye.objects.filter(brand=twin.brand, name=name).exclude(pk=twin.pk)
            if twin.name != name and twin.sort_name == twin.name and not taken.exists():
                twin.name = name
                fields.append("name")
            twin.save(update_fields=fields)

    def _report(self, creating, filling, skipped, conflicts, unreadable, dry_run):
        verb = "Would add" if dry_run else "Added"
        for name, hex_color in creating:
            self.stdout.write(f"  + {name}  {hex_color}")
        for name, hex_color, _twin, was in filling:
            rename = f", renamed from {was!r}" if was != name else ""
            self.stdout.write(f"  ~ {name}  {hex_color} — filled in a blank colour{rename}")

        # Named, not just counted. "68 skipped" is a number you have to trust;
        # a list is one you can read down and recognise.
        if conflicts:
            self.stdout.write(self.style.WARNING(
                f"\n{len(conflicts)} dye(s) already on file under a different "
                "colour — left alone, since the one on file was recorded by a "
                "person:"
            ))
            for name, hex_color, twin in conflicts:
                self.stdout.write(
                    f"  ! {name} wants {hex_color}, "
                    f"{twin.name!r} ({twin.brand.name}) has {twin.hex_color}"
                )
        if unreadable:
            self.stdout.write(self.style.WARNING(
                f"\n{len(unreadable)} entry/entries had no readable colour and "
                "were skipped:"
            ))
            for name, raw_hex in unreadable:
                self.stdout.write(f"  ? {name}: {raw_hex!r}")

        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {len(creating)}, filled in {len(filling)}, "
            f"skipped {len(skipped)} already on file"
            + (f", {len(conflicts)} conflict(s)" if conflicts else "")
            + (f", {len(unreadable)} unreadable" if unreadable else "")
            + "."
        ))
        if dry_run:
            self.stdout.write("Dry run — nothing was written.")
