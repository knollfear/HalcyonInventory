"""Put the sheet-photo retention rule on the bucket itself.

**This is the only thing deleting them, and that is on purpose.** The app
keeps no pointer to a kept photo and runs no sweep of its own: a retention
promise the app has to remember is one that lapses the first time the app
stops running, gets redeployed wrong, or has `KEEP_SHEET_PHOTOS` turned off
while a week of pictures is already sitting in the bucket. The clock belongs
where the objects are.

It lives here rather than in a shell history because a retention rule nobody
can find is a retention rule nobody can check. Run it again any time; it is
idempotent, and `--dry-run` prints what it would send.

**S3 has no per-object TTL.** Expiration is a bucket rule matched on a prefix
(or a tag), which is why sheet photos have a prefix of their own — everything
under `sheet_photos/` is the thing being timed, so the prefix *is* the group.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scarves.sheetscan import PHOTO_KEEP_DAYS, PHOTO_PREFIX

PREFIX = PHOTO_PREFIX
RULE_ID = "expire-sheet-photos"


class Command(BaseCommand):
    help = (
        f"Set the bucket to expire {PREFIX} objects after "
        f"{PHOTO_KEEP_DAYS} days."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--show",
            action="store_true",
            help="Print the rules already on the bucket and change nothing.",
        )

    def handle(self, *args, **options):
        if not settings.USE_S3:
            raise CommandError(
                "No bucket configured — this run would have done nothing and "
                "said it succeeded."
            )

        import boto3
        from botocore.exceptions import ClientError

        client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            region_name=getattr(settings, "AWS_S3_REGION_NAME", "auto"),
        )
        bucket = settings.S3_BUCKET_NAME

        existing = _current_rules(client, bucket)
        if options["show"]:
            if not existing:
                self.stdout.write("No lifecycle rules on this bucket.")
            for rule in existing:
                self.stdout.write(f"  {rule}")
            return

        rule = {
            "ID": RULE_ID,
            "Status": "Enabled",
            "Filter": {"Prefix": PREFIX},
            "Expiration": {"Days": PHOTO_KEEP_DAYS},
        }
        # Everything else on the bucket is kept. A lifecycle PUT replaces the
        # whole configuration, so sending only our rule would silently drop
        # anybody else's — the same trap `_make_items_till_ready` records about
        # an ITEM upsert replacing its variation list.
        keep = [r for r in existing if r.get("ID") != RULE_ID]
        rules = keep + [rule]

        if options["dry_run"]:
            self.stdout.write(f"Would send to {bucket}:")
            for r in rules:
                self.stdout.write(f"  {r}")
            return

        try:
            client.put_bucket_lifecycle_configuration(
                Bucket=bucket, LifecycleConfiguration={"Rules": rules}
            )
        except ClientError as exc:
            # Not every S3-compatible bucket implements lifecycle. Saying so
            # matters: the app's own sweep still holds the week, but the
            # backstop that survives the app is not there, and a run that
            # printed nothing would have left that believed.
            raise CommandError(
                f"The bucket refused the lifecycle rule ({exc}). **Nothing "
                f"else is deleting these** — the app keeps no pointer to them "
                f"and runs no sweep, deliberately, because the bucket is "
                f"where the clock belongs. Until a rule is on it, sheet "
                f"photos accumulate. Set KEEP_SHEET_PHOTOS=0 if it cannot be."
            ) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"{bucket} will expire {PREFIX} objects after "
                f"{PHOTO_KEEP_DAYS} days ({len(keep)} other rule(s) kept)."
            )
        )


def _current_rules(client, bucket):
    """The bucket's rules, or an empty list if it has none.

    A bucket with no configuration raises rather than returning an empty one,
    and that is not an error — but every *other* failure is, and must not be
    read as "no rules", because that reading is what would drop somebody
    else's configuration on the next PUT.
    """
    from botocore.exceptions import ClientError

    try:
        answer = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in (
            "NoSuchLifecycleConfiguration",
            "NoSuchLifecycleConfigurationError",
        ):
            return []
        raise
    return answer.get("Rules", [])
