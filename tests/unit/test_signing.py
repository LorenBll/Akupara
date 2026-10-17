"""Unit tests for hash-file signing helpers (parsing only; no GPG)."""

import signing


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
