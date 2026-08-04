import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


# Fields that reference a PK being offset
FK_RULES = {
    "scarves.recipedye": ["recipe"],
    "scarves.finishedproduct": ["recipe"],
}


class Command(BaseCommand):
    help = "Add an offset to PKs in a fixture to avoid collisions on import."

    def add_arguments(self, parser):
        parser.add_argument("fixture", help="Input fixture path")
        parser.add_argument("--offset", type=int, default=1000, help="Value to add to PKs (default 1000)")
        parser.add_argument("--output", help="Output path (default: <fixture>_offset.json)")

    def handle(self, *args, **options):
        path = Path(options["fixture"])
        offset = options["offset"]
        output = options["output"] or path.with_stem(path.stem + "_offset")

        if not path.exists():
            raise CommandError(f"File not found: {path}")

        with open(path) as f:
            records = json.load(f)

        for record in records:
            record["pk"] += offset
            for field in FK_RULES.get(record["model"], []):
                if record["fields"].get(field) is not None:
                    record["fields"][field] += offset

        with open(output, "w") as f:
            json.dump(records, f, indent=2)

        self.stdout.write(self.style.SUCCESS(f"Written to {output} with offset +{offset}"))
        self.stdout.write(f"To import: python manage.py loaddata {output}")
