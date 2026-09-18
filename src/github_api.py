"""GitHub API helper — token-aware requests with rate-limit fallback.

Handles the ``GITHUB_TOKEN`` .env variable. When the token is present, every
GitHub-related request (``api.github.com``, ``github.com``,
``raw.githubusercontent.com``) is first attempted **with** the token via the
``Authorization: token <TOKEN>`` header without trying an unauthenticated
request first. If that authenticated request is rate limited, the helper
retries **without** the token. If the unauthenticated retry is also rate
limited, a ``GithubRateLimitedError`` is raised so callers can decide:

* version checks → silently skip (no error, Akupara keeps running)
* other functionality that needs GitHub data → log an error but do not crash

Rate-limit detection: ``HTTP 403`` or ``HTTP 429`` whose body/headers
indicate a rate limit (``rate limit`` in the body, ``X-RateLimit-Remaining:
0``, or ``429``). Only in that exact condition is the special handling
applied — other ``403``/``404`` responses are treated as normal errors.
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class GithubRateLimitedError(RuntimeError):
    """Raised when GitHub API requests are rate limited even without a token.

    This is raised **only** when the failure is due to rate limiting (see
    module docstring). Callers that perform version checks should catch this
    and treat it as "no update / offline" without logging an error; callers
    that need GitHub data for other functionality should catch, log an error,
    and return a user-facing error without crashing the process.
    """


def _get_github_token() -> str | None:
    """Return the configured ``GITHUB_TOKEN`` or ``None`` when not set.

    Reads from ``os.environ`` (populated by ``load_dotenv``) and falls back
    to reading ``.env`` directly via ``env_store`` so the helper works even
    before ``_initialize_service_config`` has run.
    """
    token = os.getenv("GITHUB_TOKEN")
    if token is not None and token.strip():
        return token.strip()
    # Fallback: read .env directly (avoids circular import with main)
    try:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            if k.strip() == "GITHUB_TOKEN":
                val = v.strip().strip('"').strip("'")
                if val:
                    return val
                return None
    except OSError:
        pass
    # Also try env_store if available (lazy import to avoid cycle)
    try:
        import env_store as _env_store  # type: ignore

        val = _env_store.read_env_var("GITHUB_TOKEN", "")
        if val and val.strip():
            return val.strip()
    except Exception:
        pass
    return None


def _is_github_host(url: str) -> bool:
    """Return True when *url* targets a GitHub host where a token is useful."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    # api.github.com, github.com, raw.githubusercontent.com, gist.github.com, etc.
    # The leading dot on the suffix checks prevents lookalike hosts such as
    # "evilgithubusercontent.com" from being treated as GitHub-owned.
    return (
        host in {"github.com", "githubusercontent.com"}
        or host.endswith(".github.com")
        or host.endswith(".githubusercontent.com")
    )


def _is_rate_limited_error(exc: BaseException) -> bool:
    """Return True iff *exc* is an HTTP 403/429 rate-limit error."""
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    if exc.code not in (403, 429):
        return False
    # 429 is always a rate limit
    if exc.code == 429:
        return True
    # Check header
    try:
        remaining = exc.headers.get("X-RateLimit-Remaining")  # type: ignore[attr-defined]
        if remaining is not None and str(remaining).strip() == "0":
            return True
        # Some responses use lowercase header
        remaining2 = exc.headers.get("x-ratelimit-remaining")
        if remaining2 is not None and str(remaining2).strip() == "0":
            return True
    except Exception:
        pass
    # Check body for "rate limit" phrase
    try:
        body = exc.read().decode("utf-8", errors="replace").lower()  # type: ignore[attr-defined]
        if "rate limit" in body:
            return True
    except Exception:
        pass
    # Check reason
    try:
        reason = str(getattr(exc, "reason", "") or "").lower()
        if "rate limit" in reason:
            return True
    except Exception:
        pass
    return False


def github_urlopen(request: urllib.request.Request, timeout: int = 8, context: ssl.SSLContext | None = None):
    """Open *request* with GitHub token handling and rate-limit fallback.

    Behaviour:
    * If ``GITHUB_TOKEN`` is set and the request targets a GitHub host, the
      request is first sent **with** ``Authorization: token <TOKEN>``.
    * If that authenticated request is rate limited (403/429 with rate-limit
      indication), the request is retried **without** the token.
    * If the unauthenticated request is also rate limited, ``GithubRateLimitedError``
      is raised (only in this exact double-rate-limited case).
    * Other errors (404, non-rate-limit 403, network failures) are propagated
      unchanged.

    The returned object is the ``http.client.HTTPResponse`` from
    ``urllib.request.urlopen`` and must be used as a context manager by the
    caller (``with github_urlopen(req, ...) as resp:``).
    """
    if context is None:
        context = ssl.create_default_context()
    token = _get_github_token()
    url = getattr(request, "full_url", "") or getattr(request, "get_full_url", lambda: "")()
    use_token = bool(token and _is_github_host(url))

    if use_token:
        # Build a copy with Authorization header without mutating the original
        # (so fallback without token is clean).
        assert token is not None
        # Collect existing headers
        existing: dict[str, str] = {}
        try:
            # urllib.request.Request stores headers in .headers and .unredirected_hdrs
            for k, v in request.header_items():  # type: ignore[attr-defined]
                existing[k] = v
        except Exception:
            try:
                existing.update(dict(getattr(request, "headers", {})))
            except Exception:
                pass
        existing["Authorization"] = f"token {token}"
        # Also ensure User-Agent / Accept are preserved (they are already in existing)
        token_req = urllib.request.Request(url, headers=existing, method=request.get_method())  # type: ignore[attr-defined]
        # Preserve data if any (POST etc.)
        try:
            if getattr(request, "data", None) is not None:
                token_req.data = request.data  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            return urllib.request.urlopen(token_req, timeout=timeout, context=context)
        except urllib.error.HTTPError as exc:
            if _is_rate_limited_error(exc):
                # Token was rate limited → retry without token
                try:
                    return urllib.request.urlopen(request, timeout=timeout, context=context)
                except urllib.error.HTTPError as exc2:
                    if _is_rate_limited_error(exc2):
                        raise GithubRateLimitedError(f"GitHub API rate limit exceeded for {url}") from exc2
                    raise
                except Exception:
                    raise
            raise
        except Exception:
            raise
    else:
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=context)
        except urllib.error.HTTPError as exc:
            if _is_rate_limited_error(exc):
                raise GithubRateLimitedError(f"GitHub API rate limit exceeded for {url}") from exc
            raise

