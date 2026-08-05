"""
Delete every stored product photo, so the set can be rebuilt from re-uploads.

Written for the switch to downscaling on upload: the existing photos are
full-resolution phone originals (~5MB each) and there is no derivative to
regenerate them from, so they get re-shot rather than migrated.

Dry run by default. Nothing is deleted without --yes.
"""
from django.core.management.base import BaseCommand

from scarves.models import FinishedProductImage, ProductImageUpload


class Command(BaseCommand):
    help = "Delete all product photos (DB rows and stored files). Dry run unless --yes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually delete. Without this the command only reports.",
        )
        parser.add_argument(
            "--keep-external", action="store_true",
            help="Keep FinishedProductImage rows that only have an external "
                 "image_url — those have no stored file to be oversized.",
        )

    def handle(self, *args, **options):
        commit = options["yes"]
        images = FinishedProductImage.objects.all()
        if options["keep_external"]:
            images = images.exclude(image="")
        uploads = ProductImageUpload.objects.all()

        self.stdout.write(
            f"{images.count()} product image(s), {uploads.count()} upload record(s)."
        )
        if not commit:
            self.stdout.write(self.style.WARNING("Dry run. Re-run with --yes to delete."))
            return

        image_count = images.count()
        upload_count = uploads.count()

        # The stored files go with the rows: scarves.signals has post_delete
        # receivers on both models, which is also what makes deleting a photo
        # in the admin clean up after itself. Failures there are logged, not
        # raised, so a missing object can't stop the purge.
        images.delete()
        uploads.delete()

        self.stdout.write(self.style.SUCCESS(
            f"Deleted {image_count} image row(s) and {upload_count} upload "
            f"record(s), with their stored files."
        ))
