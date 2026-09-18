"""Idempotency resolver and upsert primitive.

Every migrated NetBox object carries ``custom_fields.racktables_id`` set to its
namespaced source key (e.g. ``object:42``).  That is the primary match key.  A
natural key (slug / address / vid) is used as a fallback so objects created by
hand before migration are adopted rather than duplicated.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from ..logging_setup import get_logger
from ..netbox.bootstrap import CF_NAME, MIGRATION_TAG_SLUG
from ..netbox.client import NetBoxClient

log = get_logger(__name__)

CREATED, UPDATED, SKIPPED, FAILED = "created", "updated", "skipped", "failed"


class Resolver:
    """Caches rt_key -> NetBox id per endpoint and resolves cross references."""

    def __init__(self, nb: NetBoxClient):
        self.nb = nb
        self._cache: Dict[Tuple[str, str], Optional[int]] = {}

    def register(self, endpoint: str, rt_key: str, obj_id: int) -> None:
        self._cache[(endpoint, rt_key)] = obj_id

    def resolve(self, endpoint: str, rt_key: Optional[str]) -> Optional[int]:
        if not rt_key:
            return None
        ck = (endpoint, rt_key)
        if ck in self._cache:
            return self._cache[ck]
        obj = find_by_cf(self.nb, endpoint, rt_key)
        obj_id = obj["id"] if obj else None
        self._cache[ck] = obj_id
        return obj_id


def find_by_cf(nb, endpoint: str, rt_key: str) -> Optional[Dict[str, Any]]:
    """Find the object whose racktables_id EXACTLY equals ``rt_key``.

    NetBox filters text custom fields with a partial (icontains) match, so
    ``cf_racktables_id=devicetype:...-r730`` also returns ``...-r730xd``. We
    narrow server-side with the filter, then confirm the exact value in code so
    prefix-related keys never cross-match.
    """
    for obj in nb.get_all(endpoint, {f"cf_{CF_NAME}": rt_key}):
        if _norm((obj.get("custom_fields") or {}).get(CF_NAME)) == _norm(rt_key):
            return obj
    return None


def _cf_value(existing: Dict[str, Any], name: str) -> Any:
    cf = existing.get("custom_fields") or {}
    return cf.get(name)


def _norm(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value


def _cf_norm(value: Any) -> Any:
    """Normalize a custom-field value for comparison.

    NetBox stores text custom fields as strings and reads them back as strings,
    but exported RackTables ``uint``/``float`` attributes ("CPU, MHz", "Flash
    memory, MB", ...) arrive here as numbers. Comparing int ``5`` to str ``"5"``
    as-is would look like a change and PATCH on every run, so scalar values are
    compared by their string form. None and "" both mean "unset".
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value  # keep booleans distinct from "1"/"0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return value


def compute_patch(existing: Dict[str, Any], desired: Dict[str, Any]) -> Dict[str, Any]:
    """Return only the fields in ``desired`` that differ from ``existing``.

    Handles NetBox's nested representations: a related field appears as a brief
    object (with ``id``) on read but is written as a bare id; ``custom_fields``
    and ``tags`` get set-merge semantics so we never clobber unrelated data.
    """
    patch: Dict[str, Any] = {}
    for key, want in desired.items():
        if key == "custom_fields":
            cur = existing.get("custom_fields") or {}
            changed = {k: v for k, v in want.items() if _cf_norm(cur.get(k)) != _cf_norm(v)}
            if changed:
                patch["custom_fields"] = {**{k: cur.get(k) for k in cur}, **changed}
            continue
        if key == "tags":
            cur_slugs = {t.get("slug") for t in (existing.get("tags") or [])}
            want_slugs = {t["slug"] for t in want}
            if not want_slugs.issubset(cur_slugs):
                merged = cur_slugs | want_slugs
                patch["tags"] = [{"slug": s} for s in sorted(merged)]
            continue
        # Many-to-many id list (e.g. tagged_vlans): compare as a set of ids.
        if isinstance(want, list) and all(isinstance(x, int) for x in want):
            cur_ids = {(x.get("id") if isinstance(x, dict) else x) for x in (existing.get(key) or [])}
            if cur_ids != set(want):
                patch[key] = want
            continue
        cur = existing.get(key)
        # NetBox returns related objects as {id,...} and choice fields as
        # {value,label} on read, but both are written as a bare scalar.
        if isinstance(cur, dict):
            if "id" in cur:
                cur = cur.get("id")
            elif "value" in cur:
                cur = cur.get("value")
        if _norm(cur) != _norm(want):
            patch[key] = desired[key]
    return patch


class Upserter:
    def __init__(self, nb: NetBoxClient, resolver: Resolver, dry_run: bool):
        self.nb = nb
        self.resolver = resolver
        self.dry_run = dry_run

    def upsert(
        self,
        endpoint: str,
        rt_key: Optional[str],
        desired: Dict[str, Any],
        natural_key: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[int], str]:
        """Create or update one object.  Returns (netbox_id, action)."""
        # Always stamp identity + migration tag.
        desired = dict(desired)
        cf = dict(desired.get("custom_fields") or {})
        if rt_key:
            cf[CF_NAME] = rt_key
        desired["custom_fields"] = cf
        tags = list(desired.get("tags") or [])
        if not any(t.get("slug") == MIGRATION_TAG_SLUG for t in tags):
            tags.append({"slug": MIGRATION_TAG_SLUG})
        desired["tags"] = tags

        existing = None
        if rt_key:
            existing = find_by_cf(self.nb, endpoint, rt_key)
        if existing is None and natural_key:
            existing = self.nb.get_one(endpoint, natural_key)

        if existing is not None:
            patch = compute_patch(existing, desired)
            if rt_key:
                self.resolver.register(endpoint, rt_key, existing["id"])
            if not patch:
                return existing["id"], SKIPPED
            if self.dry_run:
                log.debug("[dry-run] would PATCH %s/%s: %s", endpoint, existing["id"], list(patch))
                return existing["id"], UPDATED
            self.nb.update(endpoint, existing["id"], patch)
            return existing["id"], UPDATED

        if self.dry_run:
            log.debug("[dry-run] would CREATE %s: %s", endpoint, desired.get("name") or desired)
            return None, CREATED
        created = self.nb.create(endpoint, desired)
        if rt_key:
            self.resolver.register(endpoint, rt_key, created["id"])
        return created["id"], CREATED
