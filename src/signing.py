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


def _ensure_pubkey_imported() -> None:
    pub = _gpg_pubkey_path()
    if not pub.is_file():
        return
    gpg = _find_gpg()
    if gpg is None:
        return
    try:
        subprocess.run([gpg, "--import", str(pub)], capture_output=True, timeout=10)
    except Exception:
        pass


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