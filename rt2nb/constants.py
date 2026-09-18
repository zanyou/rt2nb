"""RackTables domain constants and name-based classification.

RackTables object-type ids (``Object.objtype_id``) are configurable per
install, so we never hardcode numeric ids.  Instead the export phase reads the
``Dictionary`` chapter that holds object-type labels and classifies each object
by its *label*, using the substrings below.  This survives custom dictionaries
and localized labels far better than a fixed id table.
"""

from __future__ import annotations

# RackTables Dictionary chapter that stores object-type labels.
CHAPTER_OBJTYPE = 1

# Label (case-insensitive substring) -> logical class used by the migration.
# Order matters: the first matching rule wins.
OBJTYPE_CLASSIFICATION = [
    ("rack", "rack"),
    ("row", "row"),
    ("location", "location"),
    ("vm cluster", "cluster"),
    ("vm resource pool", "cluster"),
    ("hypervisor", "device"),   # a hypervisor is still a physical device
    ("vm ", "vm"),
    ("virtual machine", "vm"),
]
# Exact-label fast paths (case-insensitive) for the common defaults.
OBJTYPE_EXACT = {
    "rack": "rack",
    "row": "row",
    "location": "location",
    "vm": "vm",
    "vm cluster": "cluster",
    "vm resource pool": "cluster",
}


def classify_objtype(label: str) -> str:
    """Return one of: rack, row, location, cluster, vm, device."""
    if not label:
        return "device"
    low = label.strip().lower()
    if low in OBJTYPE_EXACT:
        return OBJTYPE_EXACT[low]
    for needle, cls in OBJTYPE_CLASSIFICATION:
        if needle in low:
            return cls
    return "device"


# EntityLink entity-type strings used by RackTables 0.20 for object-to-object
# containment (Location contains Row contains Rack; cluster contains VM).
ENTITY_OBJECT = "object"

# Common RackTables Attribute names and how they map onto NetBox device fields.
# target: serial | asset_tag | custom_field | comments
DEFAULT_ATTRIBUTE_MAP = {
    "OEM S/N 1": {"target": "serial"},
    "OEM S/N 2": {"target": "custom_field", "name": "oem_sn_2"},
    "Serial Number": {"target": "serial"},
    "Asset Tag": {"target": "asset_tag"},
    "Asset tag": {"target": "asset_tag"},
    "HW type": {"target": "custom_field", "name": "hw_type"},
    "SW type": {"target": "custom_field", "name": "sw_type"},
    "SW version": {"target": "custom_field", "name": "sw_version"},
    "OS": {"target": "custom_field", "name": "operating_system"},
    "Contact person": {"target": "custom_field", "name": "contact_person"},
    "Flash memory, MB": {"target": "custom_field", "name": "flash_mb"},
    "DRAM, MB": {"target": "custom_field", "name": "dram_mb"},
    "CPU, MHz": {"target": "custom_field", "name": "cpu_mhz"},
    "UUID": {"target": "custom_field", "name": "uuid"},
}
