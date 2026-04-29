"""Qobuz result types replacing tuple return values."""

from typing import Any

import msgspec


class ArtistExtraction(msgspec.Struct, frozen=True):
    """Artist extraction result, replaces tuple[list[str], dict]."""

    artists: list[str]
    artist_data: dict[str, Any]


class ApiSignature(msgspec.Struct, frozen=True):
    """API signature result, replaces tuple[str, str]."""

    signature: str
    timestamp: str
