"""Higher-level pure mapping transforms (tags, IP roles, rack geometry)."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .normalize import clean_name, slugify

# ---------------------------------------------------------------------------
# HW type (model) parsing
# ---------------------------------------------------------------------------

# RackTables dictionary values embed UI optgroup markup like "%GSKIP%" /
# "%GPASS%" and generic "%G...%" tokens.  Strip them before display.
_DICT_MARKUP = re.compile(r"%G[A-Z0-9]*%?")

# A small set of known vendors so we can split "Dell PowerEdge R620" into
# manufacturer + model even when the string is a single flat label.
_KNOWN_VENDORS = [
    "Dell", "HP", "HPE", "Hewlett-Packard", "Cisco", "Juniper", "Arista",
    "Supermicro", "IBM", "Lenovo", "Fujitsu", "NEC", "Huawei", "Brocade",
    "Netgear", "Mikrotik", "Ubiquiti", "APC", "Eaton", "VMware", "Nokia",
    "Extreme", "Force10", "F5", "Palo Alto", "Fortinet", "Checkpoint",
    "Oracle", "Sun", "QNAP", "Synology", "NetApp", "EMC", "Nutanix",
]


def strip_dict_markup(label: Optional[str]) -> str:
    if not label:
        return ""
    text = _DICT_MARKUP.sub(" ", str(label))
    # RackTables wiki-link syntax embeds an href: "[[EX 2200-48T-4G | http://...]]"
    # Keep the human label, drop the brackets and the URL after the pipe.
    text = text.replace("[[", " ").replace("]]", " ")
    if "|" in text:
        text = text.split("|", 1)[0]
    return clean_name(text)


def parse_hw_type(label: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Split a RackTables HW-type label into (manufacturer, model).

    Returns ``(None, None)`` when there is nothing usable so the caller can fall
    back to the configured placeholder.  The raw label should still be retained
    in comments by the caller.
    """
    text = strip_dict_markup(label)
    if not text:
        return (None, None)
    low = text.lower()
    for vendor in _KNOWN_VENDORS:
        if low.startswith(vendor.lower() + " "):
            model = text[len(vendor):].strip(" -/")
            return (vendor, model or text)
        if low == vendor.lower():
            return (vendor, text)
    # No known vendor prefix: treat first token as vendor if there is a rest.
    parts = text.split(None, 1)
    if len(parts) == 2 and len(parts[0]) > 1:
        return (parts[0], parts[1])
    return (None, text)


def resolve_device_type(
    hw_label: Optional[str],
    objtype_label: Optional[str],
    placeholder_mfr: str,
    placeholder_dt: str,
) -> Tuple[str, str, bool]:
    """Decide the NetBox (manufacturer, model, hw_typed) for a device.

    Prefer the RackTables HW-type attribute.  When it is absent, fall back to
    the object-type label (CableOrganizer, Shelf, Server, ...) as the model so
    unmodeled objects are grouped by what they *are* rather than collapsing into
    a single "Unknown Device Type".  ``hw_typed`` is True only when the model
    came from a real HW type (callers use it to decide whether the device type's
    U-height may be derived from rack occupancy).
    """
    mfr, model = parse_hw_type(hw_label)
    hw_typed = bool(model)
    if not mfr:
        mfr = placeholder_mfr
    if not model:
        model = clean_name(objtype_label or "") or placeholder_dt
    return mfr, model, hw_typed

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def tag_records_from_path(
    path_names: Sequence[str], strategy: str
) -> List[Dict[str, str]]:
    """Return the NetBox tag records implied by one RackTables tag path.

    ``path_names`` is root-to-leaf, e.g. ["Owner", "TeamA"].

    * ``flatten_path``: a single tag; name = leaf, slug encodes the full path
      so distinct paths with the same leaf never collide.
    * ``leaf``: a single tag named after the leaf; slug = slug(leaf).
    * ``explode``: one tag per level (each an assignable tag).
    """
    path_names = [p for p in path_names if p]
    if not path_names:
        return []
    if strategy == "explode":
        out = []
        for i in range(len(path_names)):
            out.append(
                {
                    "name": path_names[i],
                    "slug": slugify("-".join(path_names[: i + 1])),
                }
            )
        return out
    if strategy == "leaf":
        leaf = path_names[-1]
        return [{"name": leaf, "slug": slugify(leaf)}]
    # default: flatten_path
    leaf = path_names[-1]
    return [{"name": leaf, "slug": slugify("-".join(path_names))}]


# ---------------------------------------------------------------------------
# IP address allocation type  (RackTables IPv4Allocation.type)
# ---------------------------------------------------------------------------

# RackTables allocation type -> (NetBox IPAddress.role or None, human note)
_ALLOC_ROLE = {
    "regular": (None, "regular"),
    "shared": ("anycast", "shared (RackTables)"),
    "virtual": ("vip", "virtual (RackTables)"),
    "router": (None, "router/gateway (RackTables)"),
}


def ip_allocation_role(rt_type: Optional[str]) -> Tuple[Optional[str], str]:
    """Map a RackTables allocation type to (netbox_role, note)."""
    if not rt_type:
        return (None, "")
    return _ALLOC_ROLE.get(rt_type.strip().lower(), (None, rt_type))


# ---------------------------------------------------------------------------
# Rack geometry (RackSpace atoms -> NetBox position/face)
# ---------------------------------------------------------------------------

# RackTables mounts an object into a set of (unit_no, atom) cells where atom is
# one of 'front' / 'interior' / 'rear'.  NetBox stores a single mount position
# (bottom-most U) plus a face ('front' or 'rear').
_FRONT_ATOMS = {"front", "interior"}
_REAR_ATOMS = {"rear", "interior"}


def rack_position_and_face(
    cells: Sequence[Tuple[int, str]],
) -> Tuple[Optional[int], str, List[str]]:
    """Resolve NetBox (position, face, problems) from RackSpace cells.

    ``cells`` is a sequence of (unit_no, atom).  Returns:
      * position: the bottom-most integer unit occupied (NetBox counts up from
        the bottom, and so does RackTables), or None if unresolved.
      * face: 'front' if the object touches the front plane, else 'rear'.
      * problems: notes for anything NetBox cannot represent (fractional U,
        multiple non-contiguous spans) — recorded, never silently dropped.
    """
    problems: List[str] = []
    if not cells:
        return (None, "front", ["no rack cells"])

    units = sorted({int(u) for u, _ in cells})
    atoms = {a.strip().lower() for _, a in cells if a}

    # Non-contiguous occupancy (e.g. shared/patch-panel style) — NetBox wants a
    # single contiguous block anchored at ``position``.
    if units and units[-1] - units[0] + 1 != len(units):
        problems.append(
            f"non-contiguous units {units}; using bottom-most as position"
        )

    touches_front = bool(atoms & _FRONT_ATOMS)
    touches_rear = bool(atoms & _REAR_ATOMS)
    if touches_front and touches_rear:
        face = "front"  # full-depth; NetBox represents as front-mounted
    elif touches_rear:
        face = "rear"
    else:
        face = "front"

    return (units[0], face, problems)
