"""Quorum settings. Everything that differs between a laptop demo and production comes
from environment variables; see .env.example and OPERATIONS.md."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent.parent


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


QUORUM_ENV = env("QUORUM_ENV", "demo")  # demo | production | test
QUORUM_DEMO = env_bool("QUORUM_DEMO", QUORUM_ENV != "production")
DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY", "quorum-demo-secret-key-change-me-in-production-0000000000")
DEFAULT_SECRET = SECRET_KEY.startswith("quorum-demo-secret-key")
PUBLIC_ORIGIN = env("QUORUM_PUBLIC_ORIGIN", "http://localhost:8080").rstrip("/")
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
TRUSTED_PROXIES = [ip.strip() for ip in env("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", PUBLIC_ORIGIN).split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "ninja",  # serves Swagger/Redoc from bundled static files: no CDN, works offline
    "quorum.core",
    "quorum.accounts",
    "quorum.events",
    "quorum.judging",
    "quorum.results",
    "quorum.voting",
    "quorum.audit",
    "quorum.integrations",
    "quorum.intelligence",
    "quorum.web",
]

MIDDLEWARE = [
    "quorum.core.observe.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "quorum.core.middleware.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "quorum.core.middleware.BearerTokenMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "quorum.core.middleware.PolicyErrorMiddleware",
]

ROOT_URLCONF = "quorum.urls"
WSGI_APPLICATION = "quorum.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "quorum.core.context.quorum_context",
            ],
            "builtins": ["django.templatetags.static", "quorum.web.templatetags.qtags"],
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "quorum"),
        "USER": env("POSTGRES_USER", "quorum"),
        "PASSWORD": env("POSTGRES_PASSWORD", "quorum"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/login"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = PUBLIC_ORIGIN.startswith("https://")
SESSION_COOKIE_AGE = 7 * 24 * 3600
SESSION_SAVE_EVERY_REQUEST = False
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if SESSION_COOKIE_SECURE:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(env("QUORUM_STATIC_ROOT", str(REPO_DIR / "var" / "static")))
# Hashed, compressed static files when `collectstatic` has produced a manifest (the Docker
# image does this at build time); plain storage in development and tests.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # the image build (QUORUM_ENV=build) writes the manifest; runtime then serves content-hashed
    # names with far-future, immutable caching, so an upgrade can never leave stale CSS or JS
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
                    if QUORUM_ENV == "build" or ((STATIC_ROOT / "staticfiles.json").exists() and QUORUM_ENV != "test")
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
DATA_DIR = Path(env("QUORUM_DATA_DIR", str(REPO_DIR / "var")))
# Quorum Intelligence: local embeddings (no network). Off switch for very small hosts.
QUORUM_INTELLIGENCE = env_bool("QUORUM_INTELLIGENCE", True)
# Observability: /metrics is served only to these networks (the direct peer address); slow-request log threshold
METRICS_ALLOWED_NETS = [n.strip() for n in env("METRICS_ALLOWED_NETS", "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,"
                                                  "192.168.0.0/16").split(",") if n.strip()]
SLOW_REQUEST_SECONDS = float(env("SLOW_REQUEST_SECONDS", "1.0"))
QUORUM_MODEL_DIR = env("QUORUM_MODEL_DIR", "")
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"
KEY_DIR = DATA_DIR / "keys"
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = env("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("SMTP_HOST", "localhost")
EMAIL_PORT = int(env("SMTP_PORT", "1025"))
EMAIL_HOST_USER = env("SMTP_USER", "")
EMAIL_HOST_PASSWORD = env("SMTP_PASSWORD", "")
EMAIL_USE_TLS = env_bool("SMTP_TLS", False)
DEFAULT_FROM_EMAIL = env("QUORUM_FROM_EMAIL", "Quorum <noreply@quorum.local>")
EMAIL_TIMEOUT = 10

NINJA_PAGINATION_PER_PAGE = 100

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "quorum.core.logging.JsonFormatter"}},
    "filters": {"request_id": {"()": "quorum.core.observe.RequestIdFilter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json", "filters": ["request_id"]}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
