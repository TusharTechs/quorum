"""Ed25519 signing for audit checkpoints, certificates and judge evaluation protocols.

The private key is generated on first boot into QUORUM_DATA_DIR/keys (mode 0600) and never
stored in the database. The public key is published at /.well-known/quorum-keys.json.
We sign the exact payload bytes (canonical JSON), so verification needs no
canonicalisation step on the verifier's side."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from django.conf import settings


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key_path():
    d = settings.KEY_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "signing-ed25519.key"


def load_or_create_key() -> tuple[str, Ed25519PrivateKey]:
    path = _key_path()
    if path.exists():
        priv = Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    else:
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = hashlib.sha256(pub).hexdigest()[:16]
    from .models import SigningKey

    SigningKey.objects.get_or_create(key_id=key_id, defaults={"public_key_b64": b64u(pub)})
    return key_id, priv


def sign(payload: str) -> tuple[str, str]:
    key_id, priv = load_or_create_key()
    return key_id, b64u(priv.sign(payload.encode("utf-8")))


def verify(payload: str, signature: str, key_id: str) -> bool:
    from .models import SigningKey

    k = SigningKey.objects.filter(key_id=key_id).first()
    if not k:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(b64u_dec(k.public_key_b64)).verify(b64u_dec(signature), payload.encode())
        return True
    except Exception:
        return False


def rotate_key() -> tuple[str | None, str]:
    """Retire the current signing key and start a new one.

    The retired key's public half stays published (marked retired), so every record signed
    before the rotation still verifies. Its private half is deleted: nothing can be signed
    with it again. Returns (old_key_id, new_key_id)."""
    from django.utils import timezone

    from .models import SigningKey

    path = _key_path()
    old = None
    if path.exists():
        old, _ = load_or_create_key()
        SigningKey.objects.filter(key_id=old).update(retired_at=timezone.now())
        path.unlink()
    new, _ = load_or_create_key()
    return old, new


def key_status(key_id: str) -> dict:
    from .models import SigningKey

    k = SigningKey.objects.filter(key_id=key_id).first()
    if not k:
        return {"known": False}
    return {"known": True, "retired_at": k.retired_at.isoformat() if k.retired_at else None}


def public_jwks() -> dict:
    from .models import SigningKey

    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": k.key_id, "x": k.public_key_b64,
                      "use": "sig", "retired": bool(k.retired_at),
                      "retired_at": k.retired_at.isoformat() if k.retired_at else None}
                     for k in SigningKey.objects.order_by("created_at")]}
