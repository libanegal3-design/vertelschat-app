"""Runtime configuration read from environment variables. See .env.example for every key."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
DEV_SECRET = "dev-only-insecure-secret-change-me"
log = logging.getLogger("vertelschat")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _derive(secret: str, purpose: str) -> bytes:
    return hashlib.sha256(f"{purpose}:{secret}".encode()).digest()


@dataclass(frozen=True)
class Settings:
    env: str
    base_url: str
    qr_base_url: str
    storefront_url: str
    database_url: str
    secret_key: str
    phone_encryption_key: str
    phone_hash_key: str
    var_dir: Path
    storage_backend: str
    storage_local_path: Path
    s3_endpoint: str
    s3_bucket: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    whatsapp_backend: str
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_waba_id: str
    whatsapp_app_secret: str
    whatsapp_verify_token: str
    whatsapp_api_version: str
    whatsapp_display_number: str
    whatsapp_template_lang: str
    transcription_backend: str
    mistral_api_key: str
    transcription_model: str
    story_backend: str
    anthropic_api_key: str
    story_model: str
    mail_backend: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_starttls: bool
    mail_from: str
    support_email: str
    shopify_webhook_secret: str
    shopify_store_domain: str
    shopify_storefront_token: str
    shopify_extra_book_variant_id: str
    shopify_extension_variant_id: str
    print_provider: str
    peecho_api_key: str
    gelato_api_key: str
    plausible_domain: str
    sentry_dsn: str
    dev_tools: bool
    inline_worker: bool
    compose_delay_seconds: int
    session_days: int

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def qr_host(self) -> str:
        return self.qr_base_url.split("://", 1)[-1].split("/", 1)[0].lower()

    @property
    def whatsapp_link_number(self) -> str:
        return "".join(ch for ch in self.whatsapp_display_number if ch.isdigit())


def load_settings() -> Settings:
    env = _env("VT_ENV", "development")
    var_dir = Path(_env("VT_VAR_DIR", str(Path.cwd() / "var"))).resolve()
    secret = _env("VT_SECRET_KEY", DEV_SECRET)
    phone_key = _env("VT_PHONE_ENCRYPTION_KEY") or base64.urlsafe_b64encode(_derive(secret, "phone-enc")).decode()
    phone_hash_key = _env("VT_PHONE_HASH_KEY") or _derive(secret, "phone-hash").hex()
    dev_default = env != "production"
    s = Settings(
        env=env,
        base_url=_env("VT_BASE_URL", "http://localhost:8000").rstrip("/"),
        qr_base_url=_env("VT_QR_BASE_URL", "https://v.vertelschat.nl").rstrip("/"),
        storefront_url=_env("VT_STOREFRONT_URL", "https://vertelschat.nl").rstrip("/"),
        database_url=_env("DATABASE_URL", f"sqlite:///{var_dir / 'vertelschat.db'}"),
        secret_key=secret,
        phone_encryption_key=phone_key,
        phone_hash_key=phone_hash_key,
        var_dir=var_dir,
        storage_backend=_env("VT_STORAGE_BACKEND", "local"),
        storage_local_path=Path(_env("VT_STORAGE_PATH", str(var_dir / "storage"))).resolve(),
        s3_endpoint=_env("S3_ENDPOINT_URL"),
        s3_bucket=_env("S3_BUCKET"),
        s3_region=_env("S3_REGION", "eu-central-1"),
        s3_access_key=_env("S3_ACCESS_KEY_ID"),
        s3_secret_key=_env("S3_SECRET_ACCESS_KEY"),
        whatsapp_backend=_env("WHATSAPP_BACKEND", "mock" if dev_default else "cloud"),
        whatsapp_token=_env("WHATSAPP_ACCESS_TOKEN"),
        whatsapp_phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_waba_id=_env("WHATSAPP_WABA_ID"),
        whatsapp_app_secret=_env("WHATSAPP_APP_SECRET", "dev-app-secret" if dev_default else ""),
        whatsapp_verify_token=_env("WHATSAPP_VERIFY_TOKEN", "dev-verify-token" if dev_default else ""),
        whatsapp_api_version=_env("WHATSAPP_API_VERSION", "v25.0"),
        whatsapp_display_number=_env("WHATSAPP_DISPLAY_NUMBER", "+31 20 000 0000"),
        whatsapp_template_lang=_env("WHATSAPP_TEMPLATE_LANG", "nl"),
        transcription_backend=_env("TRANSCRIPTION_BACKEND", "fixture" if dev_default else "mistral"),
        mistral_api_key=_env("MISTRAL_API_KEY"),
        transcription_model=_env("TRANSCRIPTION_MODEL", "voxtral-mini-latest"),
        story_backend=_env("STORY_BACKEND", "local" if dev_default else "anthropic"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        story_model=_env("STORY_MODEL", "claude-sonnet-5"),
        mail_backend=_env("MAIL_BACKEND", "outbox" if dev_default else "smtp"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_starttls=_bool("SMTP_STARTTLS", True),
        mail_from=_env("MAIL_FROM", "Vertelschat <hallo@vertelschat.nl>"),
        support_email=_env("VT_SUPPORT_EMAIL", "hallo@vertelschat.nl"),
        shopify_webhook_secret=_env("SHOPIFY_WEBHOOK_SECRET", "dev-shopify-secret" if dev_default else ""),
        shopify_store_domain=_env("SHOPIFY_STORE_DOMAIN"),
        shopify_storefront_token=_env("SHOPIFY_STOREFRONT_TOKEN"),
        shopify_extra_book_variant_id=_env("SHOPIFY_EXTRA_BOOK_VARIANT_ID"),
        shopify_extension_variant_id=_env("SHOPIFY_EXTENSION_VARIANT_ID"),
        print_provider=_env("PRINT_PROVIDER", "manual"),
        peecho_api_key=_env("PEECHO_API_KEY"),
        gelato_api_key=_env("GELATO_API_KEY"),
        plausible_domain=_env("PLAUSIBLE_DOMAIN"),
        sentry_dsn=_env("SENTRY_DSN"),
        dev_tools=_bool("VT_DEV_TOOLS", dev_default),
        inline_worker=_bool("VT_INLINE_WORKER", False),
        compose_delay_seconds=_int("VT_COMPOSE_DELAY_SECONDS", 240),
        session_days=_int("VT_SESSION_DAYS", 30),
    )
    if s.is_production:
        problems = []
        if secret == DEV_SECRET or len(secret) < 32:
            problems.append("VT_SECRET_KEY (min. 32 tekens)")
        if not _env("VT_PHONE_ENCRYPTION_KEY"):
            problems.append("VT_PHONE_ENCRYPTION_KEY")
        if not _env("VT_PHONE_HASH_KEY"):
            problems.append("VT_PHONE_HASH_KEY")
        if s.dev_tools:
            problems.append("VT_DEV_TOOLS moet uit staan")
        if s.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL (gebruik PostgreSQL in productie)")
        if s.whatsapp_backend == "cloud" and not (s.whatsapp_app_secret and s.whatsapp_verify_token):
            problems.append("WHATSAPP_APP_SECRET / WHATSAPP_VERIFY_TOKEN")
        if not s.shopify_webhook_secret:
            problems.append("SHOPIFY_WEBHOOK_SECRET")
        if problems:
            raise RuntimeError("Onveilige productieconfiguratie, ontbrekend of onjuist: " + ", ".join(problems))
    return s


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
