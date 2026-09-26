"""Tokens, signatures, phone-number protection and a small rate limiter."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

JOIN_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"  # no 0/O, 1/I, 2/Z, 5/S, 8/B look-alikes
QR_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # lowercase, no 0/o, 1/l/i


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_join_code() -> str:
    return "VT-" + "".join(secrets.choice(JOIN_ALPHABET) for _ in range(6))


def generate_qr_token(length: int = 12) -> str:
    """~59 bits of entropy; always contains a digit and a letter so it never collides with a word route."""
    while True:
        token = "".join(secrets.choice(QR_ALPHABET) for _ in range(length))
        if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
            return token


# ------------------------------------------------------------------ phone numbers
def normalize_phone(raw: str | None) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def _fernet() -> Fernet:
    return Fernet(get_settings().phone_encryption_key.encode())


def encrypt_phone(phone: str) -> str:
    return _fernet().encrypt(normalize_phone(phone).encode()).decode()


def decrypt_phone(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def phone_hash(phone: str) -> str:
    key = get_settings().phone_hash_key.encode()
    return hmac.new(key, normalize_phone(phone).encode(), hashlib.sha256).hexdigest()


def mask_phone(phone: str) -> str:
    d = normalize_phone(phone)
    if len(d) < 6:
        return "onbekend nummer"
    country = "31" if d.startswith("31") else "32" if d.startswith("32") else d[:2]
    return f"+{country} \u2022\u2022\u2022 \u2022\u2022 {d[-4:-2]} {d[-2:]}"


# ------------------------------------------------------------------ webhook signatures
def sign_meta_body(raw: bytes, secret: str | None = None) -> str:
    secret = secret if secret is not None else get_settings().whatsapp_app_secret
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify_meta_signature(raw: bytes, header: str | None, secret: str | None = None) -> bool:
    secret = secret if secret is not None else get_settings().whatsapp_app_secret
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


def sign_shopify_body(raw: bytes, secret: str | None = None) -> str:
    secret = secret if secret is not None else get_settings().shopify_webhook_secret
    return base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()


def verify_shopify_hmac(raw: bytes, header: str | None, secret: str | None = None) -> bool:
    secret = secret if secret is not None else get_settings().shopify_webhook_secret
    if not secret or not header:
        return False
    return hmac.compare_digest(sign_shopify_body(raw, secret), header.strip())


# ------------------------------------------------------------------ signed values & PINs
def signer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=salt)


def unsign(salt: str, value: str, max_age: int):
    try:
        return signer(salt).loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def hmac_hex(message: str, purpose: str = "generic") -> str:
    key = hashlib.sha256(f"{purpose}:{get_settings().secret_key}".encode()).digest()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(candidate, digest)


# ------------------------------------------------------------------ rate limiting
class RateLimiter:
    """In-process sliding window limiter. Good for a single instance; use the reverse proxy or a shared
    store (Redis) when running several app instances (documented in deployment.md)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter()
