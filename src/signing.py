"""Ed25519 signing for the project and plugin-library hash files.

The ``hash`` files (repo root ``hash`` and ``resources/plugins-lib/hash``) now
carry a base64 Ed25519 signature over the hash value (``<hex>\\n``), produced
with the Akupara developer's private key and verified with the public key pinned
in this module.

The public key is 32 bytes, so it is pinned directly in source rather than
distributed inside ``plugins-lib``. The plugin library is itself verified by
this key, so shipping the key alongside it would make the library self-certifying
(bootstrap/self-referential trust). Pinning the key in source instead anchors
verification to the released source tree; the same key is published on the
developer's GitHub account for transparency and rotation, but verification does
not need the network.

The private key lives outside version control, in
``resources/trust/akupara-signing.key``, and is only read by the developer's
``scripts/administration.py``.

File format::

    <hex-hash>

    -----BEGIN AKUPARA SIGNATURE-----
    <base64 signature>
    -----END AKUPARA SIGNATURE-----
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# The public key that verifies the signed hash files and every plugin trust mark.
# Pinned in source so the plugin library (which this key authenticates) can never
# supply its own key.
_PUBLIC_KEY_B64 = "BI9cLMPPLFLgZKOWT4hYzQUFtmgPeD/XAcXHLrdGyWQ="

_BEGIN_MARKER = "-----BEGIN AKUPARA SIGNATURE-----"
_END_MARKER = "-----END AKUPARA SIGNATURE-----"


def _public_key() -> Ed25519PublicKey:
    raw = base64.b64decode(_PUBLIC_KEY_B64, validate=True)
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_signature(payload: bytes, signature_b64: str) -> bool:
    """Return whether *signature_b64* is a valid Ed25519 signature over *payload*."""
    if not isinstance(signature_b64, str) or not signature_b64.strip():
        return False
    try:
        sig = base64.b64decode(signature_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        _public_key().verify(sig, payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def sign_bytes(payload: bytes, private_key_bytes: bytes) -> str:
    """Return the base64 Ed25519 signature over *payload*."""
    key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return base64.b64encode(key.sign(payload)).decode("ascii")


def load_private_key(path: Path) -> bytes:
    """Return the developer's Ed25519 private key (PEM, PKCS8) as raw bytes."""
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def sign_hash(hex_value: str, private_key_bytes: bytes) -> str:
    """Return the base64 Ed25519 signature over ``<hex>\\n``."""
    payload = (str(hex_value).strip() + "\n").encode("utf-8")
    return sign_bytes(payload, private_key_bytes)


def verify_signed_hash(hex_value: str, signature_b64: str) -> bool:
    """Verify *signature_b64* over ``<hex>\\n`` with the pinned public key."""
    payload = (str(hex_value).strip() + "\n").encode("utf-8")
    return verify_signature(payload, signature_b64)


def parse_signed_hash_text(text: str) -> tuple[str | None, bool]:
    """Parse signed hash *text* → ``(hex, signature_valid)``."""
    tokens = text.split()
    if not tokens:
        return None, False
    hex_value = tokens[0].strip()
    start = text.find(_BEGIN_MARKER)
    end = text.find(_END_MARKER)
    if start == -1 or end == -1:
        return hex_value, False
    sig = text[start + len(_BEGIN_MARKER):end].strip()
    if not sig:
        return hex_value, False
    return hex_value, verify_signed_hash(hex_value, sig)


def read_signed_hash_file(path: Path) -> tuple[str | None, bool]:
    """Read a signed hash file → ``(hex, signature_valid)``.

    ``hex`` is the first whitespace token; the signature is the base64 between
    the AKUPARA markers. A missing/invalid signature yields ``valid=False``
    (which the runtime treats as a hard failure).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, False
    return parse_signed_hash_text(text)


def write_signed_hash_file(path: Path, hex_value: str, signature_b64: str) -> None:
    """Write ``<hex>\\n\\n<markers + base64 signature>`` to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        hex_value.strip()
        + "\n\n"
        + _BEGIN_MARKER
        + "\n"
        + signature_b64.strip()
        + "\n"
        + _END_MARKER
        + "\n"
    )
    path.write_text(content, encoding="utf-8")


def public_key_status() -> str:
    """Return ``""`` when the pinned public key is usable, else the reason.

    A failed hash signature is otherwise indistinguishable from a tampered hash
    file, which makes a broken public key very hard to diagnose.
    """
    try:
        _public_key()
        return ""
    except Exception as exc:  # noqa: BLE001 - returned as a diagnostic string
        return f"pinned public key is invalid: {exc}"
