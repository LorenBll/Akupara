"""Signing helpers for the project and plugin-library hash files.

The ``hash`` files (repo root ``hash`` and ``resources/plugins-lib/hash``) now
carry an armored PGP detached signature over the hash value (``<hex>\\n``),
produced with the Akupara developer's private key (the same key used for
plugin trust marks) and verified with the distributed public key
``resources/plugins-lib/lorenbll-akupara-pub``.

File format::

    <hex-hash>
    -----BEGIN PGP SIGNATURE-----
    ...
    -----END PGP SIGNATURE-----
"""

from __future__ import annotations

import codecs
import subprocess
import tempfile
from pathlib import Path

_GPG_CANDIDATES = [
    "gpg",
    r"C:\Program Files\GnuPG\bin\gpg.exe",
    r"C:\Program Files (x86)\GnuPG\bin\gpg.exe",
]


def _gpg_pubkey_path() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "plugins-lib" / "lorenbll-akupara-pub"


def _find_gpg() -> str | None:
    for g in _GPG_CANDIDATES:
        try:
            proc = subprocess.run([g, "--version"], capture_output=True, timeout=5)
            if proc.returncode == 0:
                return g
        except Exception:
            continue
    return None


def _public_key_bytes(path: Path) -> bytes:
    """Return the shipped armored public key with any UTF-8 BOM stripped.

    gpg rejects an ASCII-armored block whose first line starts with a BOM
    (``no valid OpenPGP data found``), which silently turns the import into a
    no-op and later makes every signature check fail with a misleading
    "invalid signature". The key itself is unaffected by the BOM.
    """
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8):]
    return data.lstrip(b"\r\n \t")


def import_public_key(pub_path: Path, gpg: str) -> bool:
    """Import *pub_path* with *gpg* into the keyring; ``True`` when accepted.

    The key is imported from a sanitised temporary copy so that a BOM cannot
    make gpg reject the whole file.
    """
    try:
        data = _public_key_bytes(pub_path)
    except OSError:
        return False
    if not data:
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "pub.asc"
            tmp.write_bytes(data)
            proc = subprocess.run([gpg, "--import", str(tmp)], capture_output=True, timeout=10)
            return proc.returncode == 0
    except Exception:
        return False


def public_key_status() -> str:
    """Return ``""`` when the shipped public key is usable, else the reason.

    A failed hash signature is otherwise indistinguishable from a tampered hash
    file, which makes a broken public key very hard to diagnose.
    """
    pub = _gpg_pubkey_path()
    if not pub.is_file():
        return f"public key missing: {pub}"
    gpg = _find_gpg()
    if gpg is None:
        return "gpg not found"
    try:
        data = _public_key_bytes(pub)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "pub.asc"
            tmp.write_bytes(data)
            proc = subprocess.run(
                [gpg, "--show-keys", "--with-colons", str(tmp)],
                capture_output=True,
                timeout=10,
            )
        if proc.returncode != 0 or b"pub:" not in proc.stdout:
            reason = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
            detail = f": {reason[0]}" if reason else ""
            return f"shipped public key is not a valid OpenPGP key{detail}"
        return ""
    except Exception as exc:  # noqa: BLE001 - returned as a diagnostic string
        return f"public key check failed: {exc}"


def _ensure_pubkey_imported() -> None:
    pub = _gpg_pubkey_path()
    if not pub.is_file():
        return
    gpg = _find_gpg()
    if gpg is None:
        return
    import_public_key(pub, gpg)


def sign_hash(hex_value: str) -> str:
    """Return the armored detached signature over ``<hex>\\n``.

    Uses the developer's default signing GPG key (same key used for plugin
    trust marks). Raises RuntimeError when gpg is unavailable or signing fails.
    """
    gpg = _find_gpg()
    if gpg is None:
        raise RuntimeError("gpg not found; cannot sign hash.")
    payload = (str(hex_value).strip() + "\n").encode("utf-8")
    proc = subprocess.run(
        [gpg, "--armor", "--detach-sign"],
        input=payload,
        capture_output=True,
        timeout=15,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"gpg sign failed: {proc.stderr.decode('utf-8', errors='replace')[:300]}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


def verify_signed_hash(hex_value: str, armored_sig: str) -> bool:
    """Verify *armored_sig* over ``<hex>\\n`` with the Akupara public key."""
    gpg = _find_gpg()
    if gpg is None:
        return False
    _ensure_pubkey_imported()
    payload = (str(hex_value).strip() + "\n").encode("utf-8")
    try:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            data_path = td_p / "hash.txt"
            sig_path = td_p / "hash.sig"
            data_path.write_bytes(payload)
            sig_path.write_text(armored_sig, encoding="utf-8")
            proc = subprocess.run([gpg, "--verify", str(sig_path), str(data_path)], capture_output=True, timeout=15)
            return proc.returncode == 0
    except Exception:
        return False


def read_signed_hash_file(path: Path) -> tuple[str | None, bool]:
    """Read a signed hash file → ``(hex, signature_valid)``.

    ``hex`` is the first whitespace token; the armored signature block is the
    text between the PGP BEGIN/END markers. A missing/invalid signature yields
    ``valid=False`` (which the runtime treats as a hard failure).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, False
    tokens = text.split()
    if not tokens:
        return None, False
    hex_value = tokens[0].strip()
    start = text.find("-----BEGIN PGP SIGNATURE-----")
    end = text.find("-----END PGP SIGNATURE-----")
    if start == -1 or end == -1:
        return hex_value, False
    sig = text[start:end + len("-----END PGP SIGNATURE-----")].strip()
    if not sig:
        return hex_value, False
    return hex_value, verify_signed_hash(hex_value, sig)


def write_signed_hash_file(path: Path, hex_value: str, armored_sig: str) -> None:
    """Write ``<hex>\\n\\n<armored signature>`` to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = hex_value.strip() + "\n\n" + armored_sig.strip() + "\n"
    path.write_text(content, encoding="utf-8")