from django.apps import AppConfig


class ScarvesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'scarves'

    def ready(self):
        # Registers the post_delete receivers that clean up stored files.
        from . import signals  # noqa: F401
