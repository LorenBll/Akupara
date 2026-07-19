"""Generic POST request model."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PostRequest:
    """Describe a generic POST request."""

    url: str
    body: bytes = b""
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)
