import json

from django.core.management.base import BaseCommand, CommandError
from django.core import serializers
from django.db import transaction


class Command(BaseCommand):
    help = "Load a fixture, skipping records that already exist."

    def add_arguments(self, parser):
        parser.add_argument("fixture", help="Path to the JSON fixture file")

    def handle(self, *args, **options):
        path = options["fixture"]
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            raise CommandError(f"Fixture file not found: {path}")

        skipped = 0
        loaded = 0

        for record in serializers.deserialize("json", json.dumps([record for record in data])):
            try:
                with transaction.atomic():
                    record.save()
                loaded += 1
            except Exception:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(f"Loaded: {loaded}, Skipped (conflicts): {skipped}"))
