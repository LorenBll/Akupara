"""Unit tests for hash-file signing helpers.

Most tests parse only; the tests that need a real keyring run ``gpg`` and skip
where it is unavailable.
"""

import codecs
import shutil
from pathlib import Path

import pytest

import signing

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

requires_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="requires gpg")


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
def test_shipped_hashes_verify_with_a_clean_keyring(tmp_path, monkeypatch):
    """The shipped ``hash`` files must verify against the shipped public key.

    The keyring is emptied first on purpose: the signing key is already present
    on the developer's machine, so a missing or unreadable public key file stays
    invisible there and only surfaces on a clean machine (CI, or any other
    installation). This is what catches a public key shipped with a UTF-8 BOM,
    which gpg refuses to import.
    """
    # gpg requires the GNUPGHOME directory to exist; it does not create it.
    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir()
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home))
    for rel in ("hash", "resources/plugins-lib/hash"):
        hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / rel)
        assert hex_value, f"{rel} has no hash value"
        assert valid is True, f"{rel} does not verify against the shipped public key"


@requires_gpg
def test_bommed_public_key_is_still_importable(tmp_path, monkeypatch):
    """A BOM in the distributed public key must not break verification."""
    # gpg requires the GNUPGHOME directory to exist; it does not create it.
    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir()
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home))
    bommed = tmp_path / "lorenbll-akupara-pub"
    bommed.write_bytes(
        codecs.BOM_UTF8
        + (PROJECT_ROOT / "resources" / "plugins-lib" / "lorenbll-akupara-pub").read_bytes()
    )
    monkeypatch.setattr(signing, "_gpg_pubkey_path", lambda: bommed)

    hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / "resources" / "plugins-lib" / "hash")
    assert hex_value
    assert valid is True
