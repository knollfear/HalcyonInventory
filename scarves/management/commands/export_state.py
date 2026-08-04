from datetime import datetime
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand


MODELS = [
    "scarves.recipe",
    "scarves.recipedye",
    "scarves.finishedproduct",
]


class Command(BaseCommand):
    help = "Export current database state to a timestamped fixture file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            help="Output file path (default: scarves/fixtures/export_<timestamp>.json)",
        )

    def handle(self, *args, **options):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = options["output"] or f"scarves/fixtures/export_{timestamp}.json"

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        buf = StringIO()
        call_command("dumpdata", *MODELS, indent=2, stdout=buf)
        data = buf.getvalue()

        with open(output_path, "w") as f:
            f.write(data)

        # Count records per model for the summary
        import json
        records = json.loads(data)
        counts = {}
        for record in records:
            counts[record["model"]] = counts.get(record["model"], 0) + 1

        self.stdout.write(self.style.SUCCESS(f"Exported {len(records)} records to {output_path}"))
        for model, count in sorted(counts.items()):
            self.stdout.write(f"  {model}: {count}")
        self.stdout.write(f"\nTo restore: python manage.py loaddata {output_path}")
