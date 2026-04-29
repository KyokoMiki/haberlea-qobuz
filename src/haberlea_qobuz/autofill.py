"""Auto-fill parser for Qobuz account-share text."""

import re

# Matches lines like "Key ➠ value", "Key: value", or "Key = value".
_LINE_PATTERN = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 _\-]*?)\s*(?:➠|:|=)\s*(.+?)\s*$",
    re.MULTILINE,
)

# Map normalized labels (lowercased, single-spaced) to qobuz account keys.
_ALIASES: dict[str, str] = {
    "token": "password",
    "user id": "username",
    "userid": "username",
    "user": "username",
    "username": "username",
    "password": "password",
    "region": "region",
    "country": "region",
    "app id": "app_id",
    "app_id": "app_id",
    "app secret": "app_secret",
    "app_secret": "app_secret",
    "name": "name",
}


def parse_autofill_text(text: str) -> dict[str, str]:
    """Parses pasted Qobuz account-share text into account field updates.

    Recognizes labelled lines such as ``Token ➠ <value>`` and
    ``User ID: <value>`` and maps them onto qobuz account-config keys.

    Args:
        text: The pasted text to parse.

    Returns:
        Mapping from qobuz account-config key to extracted value.
    """
    result: dict[str, str] = {}
    for raw_key, raw_value in _LINE_PATTERN.findall(text):
        normalized = " ".join(raw_key.strip().lower().split())
        target = _ALIASES.get(normalized)
        if target is None or target in result:
            continue
        value = raw_value.strip()
        if value:
            result[target] = value
    return result
