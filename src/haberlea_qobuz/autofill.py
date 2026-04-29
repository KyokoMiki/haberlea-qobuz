"""Auto-fill parser for Qobuz account-share text."""

import re

# Line-anchored "Key ➠ value" / "Key: value" / "Key = value". The value is
# captured up to end of line so it may contain spaces (e.g. a display name).
_LINE_PATTERN = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 _\-]*?)\s*(?:➠|:|=)\s*(.+?)\s*$",
    re.MULTILINE,
)

# Inline fallback for compact / decorated lines that the line-anchored pattern
# cannot reach, e.g.:
#   "⚠️ Old token version, use app_id: 12345 & app_secret: abcdef"
# Restricted to a closed list of known keywords so noise text cannot leak
# through, and bounds the value at whitespace / common separators so multiple
# label-value pairs on the same line are extracted independently.
_INLINE_PATTERN = re.compile(
    r"\b(app[\s_]?id|app[\s_]?secret|user[\s_]?id|username|password|"
    r"token|region|country|name|email)\b\s*(?:➠|:|=)\s*([^\s&,;]+)",
    re.IGNORECASE,
)

# Header heuristic for share text titled like "Qobuz - FR 🇫🇷". Captures the
# 2-letter region code directly after the service name.
_REGION_HEADER_PATTERN = re.compile(
    r"\bqobuz\b\s*[-\u2013\u2014:]\s*([A-Za-z]{2})\b",
    re.IGNORECASE,
)

# Known-working public Qobuz app credentials used as a fallback when the
# share text does not carry explicit values. These constants are commonly
# referenced by Qobuz reverse-engineered tooling and target the current
# token format.
_DEFAULT_APP_ID = "312369995"
_DEFAULT_APP_SECRET = "e79f8b9be485692b0e5f9dd895826368"

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

    Recognizes labelled values in three forms:

    * Line-anchored ``Label ➠ value`` / ``Label: value`` / ``Label = value``.
    * Inline ``label: value`` pairs sharing a sentence, e.g.
      ``... use app_id: 12345 & app_secret: abcdef``.
    * A header line such as ``Qobuz - FR``, from which the ``region`` and a
      fallback ``name`` mirroring it are derived.

    The first occurrence of each target key wins, so line-anchored matches
    take precedence over the more permissive inline fallback.

    When both ``username`` and ``password`` were parsed from the share
    text but explicit ``app_id`` / ``app_secret`` values were not, the
    publicly-known defaults are returned so the resulting account can
    authenticate out of the box. Inputs that do not yield a complete
    credential pair leave ``app_id`` / ``app_secret`` untouched, so the
    caller can surface a warning or let the user finish filling fields
    manually instead of silently writing default app credentials.

    Args:
        text: The pasted text to parse.

    Returns:
        Mapping from qobuz account-config key to extracted value.
    """
    result: dict[str, str] = {}

    # Line-anchored matches first: their value capture preserves spaces and is
    # the authoritative source when a label appears on its own line.
    candidates: list[tuple[str, str]] = list(_LINE_PATTERN.findall(text))
    # Inline matches second: only fill keys that the line pass missed.
    candidates.extend(_INLINE_PATTERN.findall(text))

    for raw_key, raw_value in candidates:
        normalized = " ".join(raw_key.strip().lower().split())
        target = _ALIASES.get(normalized)
        if target is None or target in result:
            continue
        value = raw_value.strip()
        if value:
            result[target] = value

    # Region heuristic: derive from a "Qobuz - <CC>" style header when no
    # explicit Region/Country label was given.
    if "region" not in result:
        match = _REGION_HEADER_PATTERN.search(text)
        if match:
            result["region"] = match.group(1)

    # Default the display name to the region when the share text omits it.
    if "name" not in result and "region" in result:
        result["name"] = result["region"]

    # Fall back to the publicly-known Qobuz app credentials only when the
    # share text yielded a complete user credential pair. Without both
    # username and password the account cannot authenticate anyway, so we
    # avoid silently planting default app credentials on a partially filled
    # form.
    if "username" in result and "password" in result:
        result.setdefault("app_id", _DEFAULT_APP_ID)
        result.setdefault("app_secret", _DEFAULT_APP_SECRET)

    return result
