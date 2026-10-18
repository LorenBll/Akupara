"""Unit tests for hash-file signing helpers.

Most tests parse only; the tests that need a real keyring run ``gpg`` and skip
where it is unavailable.
"""

import codecs
import shutil
import subprocess
from pathlib import Path

import pytest

import signing

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

requires_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="requires gpg")


def _point_gpg_at_an_empty_keyring(tmp_path, monkeypatch) -> None:
    """Redirect gpg to an empty keyring, in the path form it understands.

    Isolation is essential: the signing key is present on the developer's
    machine, so without an empty keyring a broken or unreadable public key stays
    invisible and only fails on a clean machine.

    The path form has to match the gpg flavour. The Git-for-Windows build is
    MSYS and resolves a Windows path against its own install root
    (``GNUPGHOME=C:\\...`` becomes ``/p/C:\\...`` and gpg reports "directory does
    not exist"), so it needs the POSIX form instead; native GnuPG needs the
    Windows form. ``gpg --version`` reports which one is in use via its
    ``Home:`` line.
    """
    home = tmp_path / "gnupg"
    home.mkdir()

    version = subprocess.run(["gpg", "--version"], capture_output=True, text=True).stdout
    value = str(home)
    if any(line.startswith("Home: /") for line in version.splitlines()):
        cygpath = shutil.which("cygpath")
        converted = ""
        if cygpath:
            proc = subprocess.run([cygpath, "-u", str(home)], capture_output=True, text=True)
            converted = proc.stdout.strip() if proc.returncode == 0 else ""
        value = converted or "/" + str(home).replace("\\", "/").replace(":", "")

    monkeypatch.setenv("GNUPGHOME", value)


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

    This is what catches a public key shipped in a form gpg cannot read: with
    the developer's keyring the defect is invisible, and it only ever surfaces
    on a clean machine (CI, or any other installation).
    """
    _point_gpg_at_an_empty_keyring(tmp_path, monkeypatch)
    for rel in ("hash", "resources/plugins-lib/hash"):
        hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / rel)
        assert hex_value, f"{rel} has no hash value"
        assert valid is True, (
            f"{rel} does not verify against the shipped public key "
            f"({signing.public_key_status()})"
        )


@requires_gpg
def test_bommed_public_key_is_still_importable(tmp_path, monkeypatch):
    """A BOM in the distributed public key must not break verification."""
    _point_gpg_at_an_empty_keyring(tmp_path, monkeypatch)
    bommed = tmp_path / "lorenbll-akupara-pub"
    bommed.write_bytes(
        codecs.BOM_UTF8
        + (PROJECT_ROOT / "resources" / "plugins-lib" / "lorenbll-akupara-pub").read_bytes()
    )
    monkeypatch.setattr(signing, "_gpg_pubkey_path", lambda: bommed)

    hex_value, valid = signing.read_signed_hash_file(PROJECT_ROOT / "resources" / "plugins-lib" / "hash")
    assert hex_value
    assert valid is True, f"a BOM in the public key broke verification ({signing.public_key_status()})"
