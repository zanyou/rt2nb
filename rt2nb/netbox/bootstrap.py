"""Ensure the NetBox-side scaffolding the migration relies on exists.

Creates (idempotently):
  * a text custom field ``racktables_id`` attached to every content type we
    write, used as the primary idempotency key; and
  * a tag ``from-racktables`` applied to every migrated object.

Custom-field API shape follows NetBox 4.x (``object_types`` as ``app.model``).
"""

from __future__ import annotations

from typing import List

from ..logging_setup import get_logger
from .client import NetBoxClient

log = get_logger(__name__)

CF_NAME = "racktables_id"
MIGRATION_TAG_SLUG = "from-racktables"

# Content types that carry a racktables_id custom field.
CF_OBJECT_TYPES: List[str] = [
    "dcim.site",
    "dcim.location",
    "dcim.rack",
    "dcim.manufacturer",
    "dcim.devicetype",
    "dcim.device",
    "dcim.interface",
    "dcim.cable",
    "ipam.prefix",
    "ipam.ipaddress",
    "ipam.vlan",
    "ipam.vlangroup",
    "virtualization.cluster",
    "virtualization.virtualmachine",
    "virtualization.vminterface",
]


def ensure_scaffolding(nb: NetBoxClient, dry_run: bool = False) -> None:
    _ensure_custom_field(nb, dry_run)
    _ensure_tag(nb, dry_run)


def _ensure_custom_field(nb: NetBoxClient, dry_run: bool) -> None:
    existing = nb.get_one("extras/custom-fields", {"name": CF_NAME})
    payload = {
        "name": CF_NAME,
        "label": "RackTables ID",
        "type": "text",
        "object_types": CF_OBJECT_TYPES,
        "description": "Namespaced source identity (table:id) from RackTables.",
        "required": False,
    }
    if existing is None:
        if dry_run:
            log.info("[dry-run] would create custom field %s", CF_NAME)
            return
        nb.create("extras/custom-fields", payload)
        log.info("created custom field %s", CF_NAME)
        return
    # Make sure every content type we need is attached.
    have = set(existing.get("object_types") or existing.get("content_types") or [])
    want = set(CF_OBJECT_TYPES)
    if not want.issubset(have):
        merged = sorted(have | want)
        if dry_run:
            log.info("[dry-run] would extend custom field %s object_types", CF_NAME)
            return
        nb.update("extras/custom-fields", existing["id"], {"object_types": merged})
        log.info("extended custom field %s to %d object types", CF_NAME, len(merged))


def _ensure_tag(nb: NetBoxClient, dry_run: bool) -> None:
    existing = nb.get_one("extras/tags", {"slug": MIGRATION_TAG_SLUG})
    if existing is not None:
        return
    if dry_run:
        log.info("[dry-run] would create tag %s", MIGRATION_TAG_SLUG)
        return
    nb.create("extras/tags", {
        "name": "from-racktables",
        "slug": MIGRATION_TAG_SLUG,
        "description": "Imported from RackTables by rt2nb.",
        "color": "9e9e9e",
    })
    log.info("created tag %s", MIGRATION_TAG_SLUG)
