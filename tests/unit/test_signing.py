"""Unit tests for hash-file signing helpers (Ed25519)."""

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import signing

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key, raw


def test_read_signed_hash_file_missing(tmp_path):
    hex_value, valid = signing.read_signed_hash_file(tmp_path / "nope")
    assert hex_value is None
    assert valid is False


def test_read_signed_hash_file_no_signature(tmp_path):
    path = tmp_path / "hash"
    path.write_text("abc123\n", encoding="utf-8")
    hex_value, valid = signing.read_signed_hash_file(path)
    assert hex_value == "abc123"
    assert valid is False


def test_read_signed_hash_file_with_signature(tmp_path, monkeypatch):
    path = tmp_path / "hash"
    path.write_text(
        "abc123\n-----BEGIN AKUPARA SIGNATURE-----\nxyz\n-----END AKUPARA SIGNATURE-----",
        encoding="utf-8",
    )
    monkeypatch.setattr(signing, "verify_signed_hash", lambda hex_value, sig: True)
    hex_value, valid = signing.read_signed_hash_file(path)
    assert hex_value == "abc123"
    assert valid is True


def test_sign_and_verify_roundtrip(monkeypatch):
    key, raw = _keypair()
    monkeypatch.setattr(signing, "_public_key", lambda: key.public_key())
    sig = signing.sign_bytes(b"payload", raw)
    assert signing.verify_signature(b"payload", sig) is True
    assert signing.verify_signature(b"other", sig) is False
    assert signing.verify_signature(b"payload", base64.b64encode(b"bad").decode("ascii")) is False


def test_signature_by_a_different_key_is_rejected():
    # A signature made by any key other than the pinned one must be refused.
    _, other = _keypair()
    sig = signing.sign_bytes(b"payload", other)
    assert signing.verify_signature(b"payload", sig) is False


def test_verify_signature_rejects_garbage():
    assert signing.verify_signature(b"payload", "") is False
    assert signing.verify_signature(b"payload", "not-base64!!") is False
    assert signing.verify_signature(b"payload", "a" * 64) is False  # hex-looking but not a signature


def test_shipped_hashes_verify_against_the_pinned_key():
    """The shipped ``hash`` files must verify against the key pinned in source.

    This is the migration gate: if the hashes are not re-signed with the private
    key whose public half is pinned, this test fails and the service would refuse
    to start.
    """
    for rel in ("hash", "resources/plugins-lib/hash"):
        hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / rel)
        assert hex_value, f"{rel} has no hash value"
        assert valid is True, (
            f"{rel} does not verify against the pinned public key "
            f"({signing.public_key_status()})"
        )


def test_public_key_status_is_clean():
    assert signing.public_key_status() == ""
