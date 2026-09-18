"""verify subcommand: reconcile intermediate JSON against NetBox.

The intermediate JSON is the faithful projection of RackTables (anything that
could not be projected is in ``unmigrated_report.json``).  So the authoritative
check is: for each category, is every exported record present in NetBox?

We identify NetBox-side objects by their ``racktables_id`` custom field (set by
the importer), or by slug for tags.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

from .logging_setup import get_logger
from .netbox.bootstrap import CF_NAME
from .netbox.client import NetBoxClient

log = get_logger(__name__)

# category -> (json file, [endpoints], match_mode)
CATEGORIES: List[Tuple[str, List[str], str]] = [
    ("tags", ["extras/tags"], "slug"),
    ("sites", ["dcim/sites"], "cf"),
    ("locations", ["dcim/locations"], "cf"),
    ("racks", ["dcim/racks"], "cf"),
    ("manufacturers", ["dcim/manufacturers"], "cf"),
    ("device_types", ["dcim/device-types"], "cf"),
    ("clusters", ["virtualization/clusters"], "cf"),
    ("devices", ["dcim/devices"], "cf"),
    ("vms", ["virtualization/virtual-machines"], "cf"),
    ("interfaces", ["dcim/interfaces", "virtualization/interfaces"], "cf"),
    ("cables", ["dcim/cables"], "cf"),
    ("prefixes", ["ipam/prefixes"], "cf"),
    ("ip_addresses", ["ipam/ip-addresses"], "cf"),
    ("vlan_groups", ["ipam/vlan-groups"], "cf"),
    ("vlans", ["ipam/vlans"], "cf"),
]


def _load(export_dir: str, name: str) -> List[Dict[str, Any]]:
    path = os.path.join(export_dir, f"{name}.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _netbox_keys(nb: NetBoxClient, endpoints: List[str], mode: str) -> set:
    keys: set = set()
    for ep in endpoints:
        for obj in nb.get_all(ep, {"tag": "from-racktables"} if mode == "cf" else {}):
            if mode == "cf":
                val = (obj.get("custom_fields") or {}).get(CF_NAME)
                if val:
                    keys.add(val)
            else:
                keys.add(obj.get("slug"))
    return keys


def run_verify(nb: NetBoxClient, config, categories) -> int:
    export_dir = config.paths["export_dir"]
    wanted = set(categories) if categories else {c for c, _, _ in CATEGORIES}

    rows = []
    total_missing = 0
    for cat, endpoints, mode in CATEGORIES:
        if cat not in wanted:
            continue
        records = _load(export_dir, cat)
        if mode == "slug":
            expected = {r["slug"] for r in records}
        else:
            expected = {r["rt_key"] for r in records}
        present = _netbox_keys(nb, endpoints, mode)
        missing = expected - present
        total_missing += len(missing)
        rows.append((cat, len(expected), len(present & expected), len(missing), sorted(missing)[:10]))

    # Render.
    print("\nVerification (exported JSON vs NetBox):\n")
    header = f"{'category':<16}{'expected':>10}{'in_netbox':>11}{'missing':>9}"
    print(header)
    print("-" * len(header))
    for cat, exp, got, miss, sample in rows:
        print(f"{cat:<16}{exp:>10}{got:>11}{miss:>9}")
        if sample:
            print(f"    missing e.g.: {', '.join(sample)}")

    unmig_path = os.path.join(export_dir, "unmigrated_report.json")
    if os.path.exists(unmig_path):
        with open(unmig_path, encoding="utf-8") as fh:
            n = len(json.load(fh))
        print(f"\nExport-time unmigrated records (with reasons): {n} "
              f"(see {unmig_path})")

    print(f"\nTotal missing across categories: {total_missing}")
    return 0 if total_missing == 0 else 2
