"""Unit tests for hash-file signing helpers (GPG with a pinned fingerprint)."""

import codecs
import shutil
from pathlib import Path

import pytest

import signing

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

requires_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="requires gpg")

_PINNED = signing._TRUST_KEY_FINGERPRINT


@pytest.fixture(autouse=True)
def _reset_trust_cache():
    signing._trusted_key_cache = None
    yield
    signing._trusted_key_cache = None


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
        "abc123\n-----BEGIN PGP SIGNATURE-----\nxyz\n-----END PGP SIGNATURE-----",
        encoding="utf-8",
    )
    monkeypatch.setattr(signing, "verify_signed_hash", lambda hex_value, sig: True)
    hex_value, valid = signing.read_signed_hash_file(path)
    assert hex_value == "abc123"
    assert valid is True


def test_public_key_bytes_strips_a_bom(tmp_path):
    """A BOM must never reach gpg: it makes the whole armor block unreadable."""
    pub = tmp_path / "pub.asc"
    pub.write_bytes(codecs.BOM_UTF8 + b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
    assert signing._public_key_bytes(pub) == b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"


@requires_gpg
def test_shipped_key_matches_the_pinned_fingerprint():
    gpg = signing._find_gpg()
    assert gpg is not None
    data = signing._public_key_bytes(signing._gpg_pubkey_path())
    assert signing._key_fingerprint(gpg, data) == _PINNED


@requires_gpg
def test_shipped_hashes_verify_against_the_pinned_key():
    """The shipped ``hash`` files must verify against the pinned key.

    This is the migration gate: a stale signature or a key that no longer matches
    the pin fails here, and the service would refuse to start.
    """
    for rel in ("hash", "resources/plugins-lib/hash"):
        hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / rel)
        assert hex_value, f"{rel} has no hash value"
        assert valid is True, (
            f"{rel} does not verify against the pinned key ({signing.public_key_status()})"
        )


@requires_gpg
def test_bommed_public_key_is_still_accepted(tmp_path, monkeypatch):
    """A BOM in the distributed public key must not break verification."""
    bommed = tmp_path / "lorenbll-akupara-pub"
    bommed.write_bytes(
        codecs.BOM_UTF8
        + (PROJECT_ROOT / "resources" / "lorenbll-akupara-pub").read_bytes()
    )
    monkeypatch.setattr(signing, "_gpg_pubkey_path", lambda: bommed)
    signing._trusted_key_cache = None
    hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / "resources" / "plugins-lib" / "hash")
    assert hex_value
    assert valid is True, f"a BOM in the public key broke verification ({signing.public_key_status()})"


@requires_gpg
def test_signature_rejected_when_pin_does_not_match(monkeypatch):
    """A signature by any other key must be refused.

    Flipping the pin makes the real key no longer match, so verification must
    fail even though the shipped signature is cryptographically valid — the pin,
    not the presence of a valid signature, is the trust root.
    """
    monkeypatch.setattr(signing, "_TRUST_KEY_FINGERPRINT", "0" * 40)
    monkeypatch.setattr(signing, "_fetch_github_key", lambda gpg: None)
    signing._trusted_key_cache = None
    hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / "hash")
    assert hex_value
    assert valid is False


def test_garbage_signatures_are_rejected():
    assert signing.verify_signature(b"payload", "") is False
    assert signing.verify_signature(b"payload", "not a signature") is False
    # The former gpg-less SHA-256 hex fallback is not a signature.
    assert signing.verify_signature(b"payload", "a" * 64) is False


def test_public_key_status_is_clean_when_gpg_is_present():
    if signing._find_gpg() is None:
        pytest.skip("requires gpg")
    assert signing.public_key_status() == ""
