"""Generic GET response model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class GetResponse:
    """Describe a normalized GET response."""

    status_code: int
    reason: str
    body: str
    body_size: int
    headers: dict[str, str]
    json_body: dict | list | str | int | float | bool | None = None
