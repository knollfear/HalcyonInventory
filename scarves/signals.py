"""
Keep stored files in step with the rows that point at them.

Django stopped deleting files on model delete back in 1.3 (a rollback would
leave the row pointing at a file that no longer existed). The result is that
removing a photo in the admin dropped the row and silently left the object in
the bucket, paying for storage forever with nothing left that knew its key.

`post_delete` rather than an overridden `delete()`: the admin's bulk action and
any cascade from FinishedProduct go through the queryset, which never calls
`Model.delete()`. Registering a receiver here also stops Django taking its
fast-delete path, so the signal really does fire for every row in a bulk delete.
"""
import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import FinishedProductImage, ProductImageUpload

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=FinishedProductImage)
def delete_product_image_file(sender, instance, **kwargs):
    """Drop the stored file when its FinishedProductImage goes."""
    if not instance.image:
        return
    try:
        instance.image.delete(save=False)
    except Exception:
        # The row is already gone; failing here would only turn a tidy-up
        # problem into a 500 in the admin.
        logger.exception("Could not delete stored image %s", instance.image.name)


@receiver(post_delete, sender=ProductImageUpload)
def delete_upload_object(sender, instance, **kwargs):
    """Drop the uploaded object when its tracking row goes.

    An upload that never got filed has no FinishedProductImage at all, so this
    is the only thing that would ever clean it up. When it *was* filed both
    rows name the same key and whichever is deleted second finds it already
    gone, which is not an error.
    """
    if not instance.key:
        return
    try:
        from django.conf import settings

        if settings.USE_S3:
            from .s3utils import delete_object
            delete_object(instance.key)
        else:
            from django.core.files.storage import default_storage
            if default_storage.exists(instance.key):
                default_storage.delete(instance.key)
    except Exception:
        logger.exception("Could not delete uploaded object %s", instance.key)
