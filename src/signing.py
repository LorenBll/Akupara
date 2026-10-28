"""Signing helpers for the project and plugin-library hash files.

The ``hash`` files (repo root ``hash`` and ``resources/plugins-lib/hash``) carry
an armored PGP detached signature over the hash value (``<hex>\\n``), produced
with the Akupara developer's private key (the same key used for plugin trust
marks).

The verifying key is identified by its **primary fingerprint**, pinned below.
Verification imports the key into a dedicated temporary keyring (never the
user's) and requires the signature to have been made by that exact key. The key
itself is obtained first from a local copy, gated by the fingerprint, and falls
back to the developer's GitHub account (``https://github.com/LorenBll.gpg``),
again gated by the fingerprint. A key whose fingerprint does not match the pin is
never used, so a modified library cannot ship its own key.

File format::

    <hex-hash>
    -----BEGIN PGP SIGNATURE-----
    ...
    -----END PGP SIGNATURE-----
"""

from __future__ import annotations

import codecs
import shutil
import ssl
import subprocess
import tempfile
import urllib.request
from pathlib import Path

_GPG_CANDIDATES = [
    "gpg",
    r"C:\Program Files\GnuPG\bin\gpg.exe",
    r"C:\Program Files (x86)\GnuPG\bin\gpg.exe",
]

# Primary fingerprint of the Akupara signing key ``lorenbll-akupara``, published
# on the developer's GitHub account. This is the trust root: verification only
# accepts a signature made by this key, and only a key matching this fingerprint
# is ever used to verify.
_TRUST_KEY_FINGERPRINT = "4F4633B426B4BE46A44814D5B729FD1B62A59BCF"

_GITHUB_KEY_URL = "https://github.com/LorenBll.gpg"

_trusted_key_cache: bytes | None = None


def _gpg_pubkey_path() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "lorenbll-akupara-pub"


def _trust_cache_path() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "trust" / "akupara-signing.pub"


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
    """Return an armored public key with any UTF-8 BOM stripped.

    gpg rejects an ASCII-armored block whose first line starts with a BOM
    (``no valid OpenPGP data found``), which silently turns the import into a
    no-op and later makes every signature check fail with a misleading
    "invalid signature". The key itself is unaffected by the BOM.
    """
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8):]
    return data.lstrip(b"\r\n \t")


def _homedir_arg(gpg: str, path: Path) -> str:
    """Return the ``--homedir`` value in the path form *gpg* understands.

    Native GnuPG wants the Windows form; the Git-for-Windows MSYS build wants the
    POSIX form (otherwise it resolves the path against its own install root).
    The form is chosen from the ``Home:`` line of ``gpg --version``.
    """
    try:
        version = subprocess.run([gpg, "--version"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        version = ""
    value = str(path)
    if any(line.startswith("Home: /") for line in version.splitlines()):
        cygpath = shutil.which("cygpath")
        converted = ""
        if cygpath:
            try:
                proc = subprocess.run([cygpath, "-u", str(path)], capture_output=True, text=True, timeout=5)
                converted = proc.stdout.strip() if proc.returncode == 0 else ""
            except Exception:
                converted = ""
        value = converted or "/" + str(path).replace("\\", "/").replace(":", "")
    return value


def _key_fingerprint(gpg: str, key_bytes: bytes) -> str | None:
    """Return the primary fingerprint of the first key in *key_bytes*, or ``None``."""
    if not key_bytes:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "pub.asc"
            tmp.write_bytes(key_bytes)
            proc = subprocess.run(
                [gpg, "--show-keys", "--with-colons", str(tmp)],
                capture_output=True,
                timeout=15,
            )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("fpr:"):
                parts = line.split(":")
                if len(parts) > 9 and parts[9]:
                    return parts[9].upper()
    except Exception:
        return None
    return None


def _fetch_github_key(gpg: str) -> bytes | None:
    """Fetch the signing key from GitHub and return the block matching the pin.

    The developer's account may carry several keys, so the fetched bundle is
    imported into a temporary keyring and the single key whose primary
    fingerprint equals the pin is exported.
    """
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(_GITHUB_KEY_URL, headers={"User-Agent": "Akupara/1.0"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            bundle = resp.read()
    except Exception:
        return None
    if not bundle:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "gnupg"
            home.mkdir()
            h = _homedir_arg(gpg, home)
            proc = subprocess.run([gpg, "--homedir", h, "--import"], input=bundle, capture_output=True, timeout=30)
            if proc.returncode != 0:
                return None
            proc = subprocess.run(
                [gpg, "--homedir", h, "--with-colons", "--fingerprint", "--list-keys"],
                capture_output=True,
                timeout=15,
            )
            found = False
            for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                if line.startswith("fpr:"):
                    parts = line.split(":")
                    if len(parts) > 9 and parts[9].upper() == _TRUST_KEY_FINGERPRINT:
                        found = True
                        break
            if not found:
                return None
            proc = subprocess.run(
                [gpg, "--homedir", h, "--armor", "--export", _TRUST_KEY_FINGERPRINT],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
    except Exception:
        return None
    return None


def _trusted_key_bytes() -> bytes | None:
    """Return the trusted public key, gated by the pinned fingerprint.

    Resolution order: the on-disk cache (``resources/trust/``), the shipped copy
    (``resources/plugins-lib/``), then GitHub. A candidate is accepted only when
    its primary fingerprint equals the pin, so a replaced or substituted key can
    never be used. A successful GitHub fetch is cached for offline starts.
    """
    global _trusted_key_cache
    if _trusted_key_cache is not None:
        return _trusted_key_cache
    gpg = _find_gpg()
    if gpg is None:
        return None
    for path in (_trust_cache_path(), _gpg_pubkey_path()):
        try:
            data = _public_key_bytes(path)
        except OSError:
            continue
        if not data:
            continue
        if _key_fingerprint(gpg, data) == _TRUST_KEY_FINGERPRINT:
            _trusted_key_cache = data
            return data
    fetched = _fetch_github_key(gpg)
    if fetched:
        try:
            _trust_cache_path().parent.mkdir(parents=True, exist_ok=True)
            _trust_cache_path().write_bytes(fetched)
        except OSError:
            pass
        _trusted_key_cache = fetched
        return fetched
    return None


def _verify_signature(payload: bytes, armored_sig: str) -> bool:
    """Return whether *armored_sig* is a valid signature by the pinned key over *payload*."""
    gpg = _find_gpg()
    if gpg is None:
        return False
    key = _trusted_key_bytes()
    if not key:
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            home = td_p / "gnupg"
            home.mkdir()
            h = _homedir_arg(gpg, home)
            subprocess.run([gpg, "--homedir", h, "--import"], input=key, capture_output=True, timeout=15)
            data_path = td_p / "data.bin"
            sig_path = td_p / "sig.asc"
            data_path.write_bytes(payload)
            sig_path.write_text(armored_sig, encoding="utf-8")
            proc = subprocess.run(
                [gpg, "--homedir", h, "--status-fd", "1", "--verify", str(sig_path), str(data_path)],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode != 0:
                return False
            for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                if line.startswith("[GNUPG:] VALIDSIG"):
                    parts = line.split()
                    if len(parts) >= 3 and parts[2].upper() == _TRUST_KEY_FINGERPRINT:
                        return True
            return False
    except Exception:
        return False


def verify_signature(payload: bytes, armored_sig: str) -> bool:
    """Return whether *armored_sig* is a valid signature by the pinned key over *payload*.

    Used by plugin trust-mark verification, which signs the canonical JSON bytes.
    """
    return _verify_signature(payload, armored_sig)


def public_key_status() -> str:
    """Return ``""`` when the trusted public key is usable, else the reason.

    A failed hash signature is otherwise indistinguishable from a tampered hash
    file, which makes a broken public key very hard to diagnose.
    """
    gpg = _find_gpg()
    if gpg is None:
        return "gpg not found"
    if _trusted_key_bytes() is None:
        return f"trusted public key unavailable (expected fingerprint {_TRUST_KEY_FINGERPRINT[-16:]})"
    return ""


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
    """Verify *armored_sig* over ``<hex>\\n`` with the pinned Akupara public key."""
    payload = (str(hex_value).strip() + "\n").encode("utf-8")
    return _verify_signature(payload, armored_sig)


def parse_signed_hash_text(text: str) -> tuple[str | None, bool]:
    """Parse signed hash *text* → ``(hex, signature_valid)``."""
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
    return parse_signed_hash_text(text)


def write_signed_hash_file(path: Path, hex_value: str, armored_sig: str) -> None:
    """Write ``<hex>\\n\\n<armored signature>`` to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = hex_value.strip() + "\n\n" + armored_sig.strip() + "\n"
    path.write_text(content, encoding="utf-8")
