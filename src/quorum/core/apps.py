from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "quorum.core"
    label = "core"
    verbose_name = "Core infrastructure"

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
