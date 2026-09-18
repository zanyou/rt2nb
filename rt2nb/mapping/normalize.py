"""Name normalization and slug generation."""

from __future__ import annotations

import re
import unicodedata

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")


def slugify(value: str, max_len: int = 100) -> str:
    """NetBox-compatible slug: lowercase, ascii, hyphen-separated.

    NetBox slugs allow ``[-a-zA-Z0-9_]``; we normalise to lowercase and use
    hyphens.  Non-ascii is transliterated where possible, then dropped.
    """
    if value is None:
        value = ""
    value = unicodedata.normalize("NFKD", str(value))
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = _SLUG_STRIP.sub("-", value)
    value = _SLUG_TRIM.sub("", value)
    if not value:
        value = "x"
    return value[:max_len].rstrip("-") or "x"


def rt_key(source: str, source_id) -> str:
    """Namespaced RackTables identity stored in the ``racktables_id`` field.

    Table id-spaces overlap (an IPv4Network id 5 and an Object id 5 are
    unrelated), so we namespace by source table to keep the value unique within
    each NetBox endpoint.
    """
    return f"{source}:{source_id}"


_CF_STRIP = re.compile(r"[^a-z0-9_]+")


def cf_name(name: str, max_len: int = 50) -> str:
    """A valid NetBox custom-field name: ``[a-z0-9_]+`` starting with a letter.

    Used to route arbitrary RackTables attribute names into custom fields
    deterministically (so export and import agree, and re-runs are stable).
    """
    if not name:
        return "attr"
    value = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    value = _CF_STRIP.sub("_", value.lower()).strip("_")
    if not value:
        return "attr"
    if not value[0].isalpha():
        value = "attr_" + value
    return value[:max_len].strip("_") or "attr"


def clean_name(value: str) -> str:
    """Collapse whitespace; NetBox names must be non-empty and <= 64/100 chars
    depending on model.  Callers truncate as needed."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_mac(value: str) -> str | None:
    """Return a colon-separated lowercase MAC, or None if not parseable.

    RackTables stores the L2 address as 12 hex chars with no separators.
    """
    if not value:
        return None
    hexs = re.sub(r"[^0-9a-fA-F]", "", str(value))
    if len(hexs) != 12:
        return None
    hexs = hexs.lower()
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2))
