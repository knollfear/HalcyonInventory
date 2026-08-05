from django.apps import AppConfig


class ScarvesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'scarves'

    def ready(self):
        # Registers the post_delete receivers that clean up stored files.
        from . import signals  # noqa: F401

        # Teach PIL to open HEIC/HEIF, the format iPhones shoot in. Without it
        # a HEIC upload can't be decoded at all: no barcode, no downscale, and
        # a file most browsers refuse to render. Guarded so the app still boots
        # if the wheel is unavailable, matching how pyzbar is treated.
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:  # pragma: no cover - depends on the built image
            pass
