"""Import orchestration: intermediate JSON -> NetBox, in dependency order."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from ..mapping.normalize import slugify
from ..netbox.bootstrap import CF_NAME, MIGRATION_TAG_SLUG, ensure_scaffolding
from ..netbox.client import NetBoxClient, NetBoxError
from ..report import Summary, UnmigratedReport
from .state import (
    CREATED, FAILED, SKIPPED, UPDATED,
    Resolver, Upserter, compute_patch, find_by_cf,
)

log = get_logger(__name__)

# Import order (also the set of category names for --only / --skip).
IMPORT_ORDER = [
    "tags", "sites", "locations", "racks", "manufacturers", "device_types",
    "clusters", "devices", "vms", "interfaces", "cables", "prefixes",
    "ip_addresses", "vlan_groups", "vlans",
]

EP_SITE = "dcim/sites"
EP_LOCATION = "dcim/locations"
EP_RACK = "dcim/racks"
EP_MFR = "dcim/manufacturers"
EP_DT = "dcim/device-types"
EP_DEVICE = "dcim/devices"
EP_IFACE = "dcim/interfaces"
EP_CABLE = "dcim/cables"
EP_CLUSTER = "virtualization/clusters"
EP_VM = "virtualization/virtual-machines"
EP_VMIFACE = "virtualization/interfaces"
EP_PREFIX = "ipam/prefixes"
EP_IP = "ipam/ip-addresses"
EP_VLANGROUP = "ipam/vlan-groups"
EP_VLAN = "ipam/vlans"


class Importer:
    def __init__(self, nb: NetBoxClient, config, categories, dry_run: bool):
        self.nb = nb
        self.cfg = config
        self.export_dir = config.paths["export_dir"]
        self.categories = set(categories) if categories else set(IMPORT_ORDER)
        self.dry_run = dry_run
        self.resolver = Resolver(nb)
        self.up = Upserter(nb, self.resolver, dry_run)
        self.summary = Summary()
        self.unmig = UnmigratedReport()

        self.default_site_key = "default:site"
        self.default_cluster_key = "default:cluster"
        self._default_role_id: Optional[int] = None
        self._default_cluster_type_id: Optional[int] = None
        # role name (RackTables object type) -> NetBox device-role id
        self._role_ids: Dict[str, Optional[int]] = {}

        # port rt_key -> {id, kind, parent_id, name}
        self.iface_index: Dict[str, Dict[str, Any]] = {}
        # (object_ref, iface_name) -> {id, kind}
        self.iface_by_obj_name: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # -- io -----------------------------------------------------------------

    def _load(self, name: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.export_dir, f"{name}.json")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _want(self, category: str) -> bool:
        return category in self.categories

    def _resolve_site(self, rec: Dict[str, Any]) -> Optional[int]:
        """Site id from the record's site_ref, falling back to the default site."""
        return (self.resolver.resolve(EP_SITE, rec.get("site_ref"))
                or self.resolver.resolve(EP_SITE, self.default_site_key))

    def _progress(self, name: str, records):
        """Yield records, logging progress every 200 so large phases (which do
        one NetBox lookup per record) don't look frozen."""
        records = list(records)
        total = len(records)
        if total:
            log.info("import: %s -> %d record(s)%s", name, total,
                     "  [DRY RUN]" if self.dry_run else "")
        for i, rec in enumerate(records, 1):
            if i % 200 == 0 or i == total:
                log.info("  %s: %d/%d", name, i, total)
            yield rec

    def _record(self, stat, action: str) -> None:
        if action == CREATED:
            stat.created += 1
        elif action == UPDATED:
            stat.updated += 1
        elif action == SKIPPED:
            stat.skipped += 1
        elif action == FAILED:
            stat.failed += 1

    def _safe(self, stat, source_table, source_id, fn, *a, **kw):
        try:
            _id, action = fn(*a, **kw)
            self._record(stat, action)
            return _id
        except Exception as exc:  # keep the phase going; record the failure
            stat.failed += 1
            self.unmig.add(source_table, source_id, f"import failed: {exc}")
            log.error("import failed for %s/%s: %s", source_table, source_id, exc)
            return None

    # -- run ----------------------------------------------------------------

    def run(self) -> Tuple[Summary, UnmigratedReport]:
        ensure_scaffolding(self.nb, self.dry_run)
        self._ensure_defaults()
        if self._want("tags"):
            self._import_tags()
        if self._want("sites"):
            self._import_sites()
        if self._want("locations"):
            self._import_locations()
        if self._want("racks"):
            self._import_racks()
        if self._want("manufacturers"):
            self._import_manufacturers()
        if self._want("device_types"):
            self._import_device_types()
        if self._want("clusters"):
            self._import_clusters()
        if self._want("devices") or self._want("vms"):
            self._ensure_attribute_custom_fields()
        if self._want("devices"):
            self._import_devices()
        if self._want("vms"):
            self._import_vms()
        if self._want("interfaces"):
            self._import_interfaces()
        if self._want("cables"):
            self._import_cables()
        if self._want("prefixes"):
            self._import_prefixes()
        if self._want("ip_addresses"):
            self._import_ip_addresses()
        if self._want("vlan_groups"):
            self._import_vlan_groups()
        if self._want("vlans"):
            self._import_vlans()
            self._apply_interface_vlans()
        self.unmig.save(os.path.join(self.export_dir, "unmigrated_import_report.json"))
        return self.summary, self.unmig

    # -- scaffolding defaults ----------------------------------------------

    def _ensure_simple(self, endpoint: str, slug: str, payload: Dict[str, Any]) -> Optional[int]:
        existing = self.nb.get_one(endpoint, {"slug": slug})
        if existing:
            return existing["id"]
        if self.dry_run:
            log.info("[dry-run] would create %s %s", endpoint, slug)
            return None
        return self.nb.create(endpoint, payload)["id"]

    def _ensure_role(self, name: Optional[str]) -> Optional[int]:
        """Device-role id for a RackTables object-type label, created on demand.

        Roles are marked ``vm_role`` so the same role works for devices and VMs.
        An empty name falls back to the default ``Migrated`` role.
        """
        name = (name or "").strip()
        if not name:
            return self._default_role_id
        if name not in self._role_ids:
            slug = slugify(name)
            self._role_ids[name] = self._ensure_simple(
                "dcim/device-roles", slug,
                {"name": name[:100], "slug": slug, "color": "9e9e9e", "vm_role": True},
            )
        return self._role_ids[name] or self._default_role_id

    def _ensure_defaults(self) -> None:
        self._default_role_id = self._ensure_simple(
            "dcim/device-roles", "migrated",
            {"name": "Migrated", "slug": "migrated", "color": "9e9e9e", "vm_role": True},
        )
        self._default_cluster_type_id = self._ensure_simple(
            "virtualization/cluster-types", "racktables",
            {"name": "RackTables", "slug": "racktables"},
        )

    def _ensure_attribute_custom_fields(self) -> None:
        """Create a text custom field for every attribute key found on devices
        and VMs, so NetBox does not reject the custom_fields payload.

        RackTables attributes have open-ended names, so we discover the set from
        the exported JSON rather than hardcoding it.
        """
        names: set = set()
        for cat in ("devices", "vms"):
            for rec in self._load(cat):
                names.update((rec.get("custom_fields") or {}).keys())
        if not names:
            return
        existing = {cf["name"] for cf in self.nb.get_all("extras/custom-fields")}
        object_types = ["dcim.device", "virtualization.virtualmachine"]
        for name in sorted(names):
            if name in existing:
                continue
            if self.dry_run:
                log.info("[dry-run] would create attribute custom field %s", name)
                continue
            try:
                self.nb.create("extras/custom-fields", {
                    "name": name, "label": name.replace("_", " ").title(),
                    "type": "text", "object_types": object_types, "required": False,
                    "description": "Migrated RackTables attribute.",
                })
                log.info("created attribute custom field %s", name)
            except Exception as exc:
                self.unmig.add("Attribute", name, f"custom field create failed: {exc}")

    # -- tags ---------------------------------------------------------------

    def _import_tags(self) -> None:
        stat = self.summary.stat("tags")
        for rec in self._progress("tags", self._load("tags")):
            stat.source += 1
            existing = self.nb.get_one("extras/tags", {"slug": rec["slug"]})
            if existing:
                stat.skipped += 1
                continue
            if self.dry_run:
                stat.created += 1
                continue
            try:
                self.nb.create("extras/tags", {"name": rec["name"], "slug": rec["slug"]})
                stat.created += 1
            except Exception as exc:
                stat.failed += 1
                self.unmig.add("TagTree", rec["slug"], f"tag create failed: {exc}")

    # -- sites / locations / racks -----------------------------------------

    def _import_sites(self) -> None:
        stat = self.summary.stat("sites")
        for rec in self._progress("sites", self._load("sites")):
            stat.source += 1
            desired = {"name": rec["name"][:100], "slug": rec["slug"], "status": "active"}
            self._safe(stat, "Object", rec.get("source_id"),
                       self.up.upsert, EP_SITE, rec["rt_key"], desired,
                       {"slug": rec["slug"]})

    def _import_locations(self) -> None:
        stat = self.summary.stat("locations")
        for rec in self._progress("locations", self._load("locations")):
            stat.source += 1
            site_id = self._resolve_site(rec)
            if not site_id and not self.dry_run:
                stat.failed += 1
                self.unmig.add("Object", rec.get("source_id"), "location: site unresolved")
                continue
            desired = {"name": rec["name"][:100], "slug": rec["slug"],
                       "site": site_id, "status": "active"}
            self._safe(stat, "Object", rec.get("source_id"),
                       self.up.upsert, EP_LOCATION, rec["rt_key"], desired,
                       {"slug": rec["slug"], "site_id": site_id} if site_id else None)

    def _import_racks(self) -> None:
        stat = self.summary.stat("racks")
        for rec in self._progress("racks", self._load("racks")):
            stat.source += 1
            site_id = self._resolve_site(rec)
            loc_id = self.resolver.resolve(EP_LOCATION, rec.get("location_ref"))
            desired = {
                "name": rec["name"][:100], "site": site_id,
                "u_height": int(rec.get("u_height") or 42),
                "status": "active",
                "comments": rec.get("comments") or "",
                "tags": rec.get("tags") or [],
            }
            if loc_id:
                desired["location"] = loc_id
            self._safe(stat, "Object", rec.get("source_id"),
                       self.up.upsert, EP_RACK, rec["rt_key"], desired,
                       {"name": rec["name"][:100], "site_id": site_id} if site_id else None)

    # -- manufacturers / device types --------------------------------------

    def _import_manufacturers(self) -> None:
        stat = self.summary.stat("manufacturers")
        for rec in self._progress("manufacturers", self._load("manufacturers")):
            stat.source += 1
            desired = {"name": rec["name"][:100], "slug": rec["slug"]}
            self._safe(stat, "Dictionary", rec.get("source_id"),
                       self.up.upsert, EP_MFR, rec["rt_key"], desired,
                       {"slug": rec["slug"]})

    def _import_device_types(self) -> None:
        stat = self.summary.stat("device_types")
        for rec in self._progress("device_types", self._load("device_types")):
            stat.source += 1
            mfr_id = self.resolver.resolve(EP_MFR, rec.get("manufacturer_ref"))
            desired = {
                "manufacturer": mfr_id, "model": rec["model"][:100],
                "slug": rec["slug"], "u_height": int(rec.get("u_height") or 1),
            }
            # cf-only match: the {slug} natural key matches non-exactly on
            # NetBox and cross-hit prefix-related slugs (e.g. r730 vs r730xd),
            # merging distinct device types. racktables_id is an exact key.
            self._safe(stat, "Dictionary", rec.get("source_id"),
                       self.up.upsert, EP_DT, rec["rt_key"], desired, None)

    # -- clusters / devices / vms ------------------------------------------

    def _import_clusters(self) -> None:
        stat = self.summary.stat("clusters")
        for rec in self._progress("clusters", self._load("clusters")):
            stat.source += 1
            desired = {
                "name": rec["name"][:100], "type": self._default_cluster_type_id,
                "comments": rec.get("comments") or "", "tags": rec.get("tags") or [],
            }
            self._safe(stat, "Object", rec.get("source_id"),
                       self.up.upsert, EP_CLUSTER, rec["rt_key"], desired,
                       {"name": rec["name"][:100]})

    def _default_cluster_id(self) -> Optional[int]:
        cid = self.resolver.resolve(EP_CLUSTER, self.default_cluster_key)
        if cid:
            return cid
        _id, _action = self.up.upsert(
            EP_CLUSTER, self.default_cluster_key,
            {"name": "Unclustered", "type": self._default_cluster_type_id},
            {"name": "Unclustered"},
        )
        return _id

    def _import_devices(self) -> None:
        stat = self.summary.stat("devices")
        for rec in self._progress("devices", self._load("devices")):
            stat.source += 1
            site_id = self._resolve_site(rec)
            dt_id = self.resolver.resolve(EP_DT, rec.get("device_type_ref"))
            rack_id = self.resolver.resolve(EP_RACK, rec.get("rack_ref"))
            desired: Dict[str, Any] = {
                "name": rec["name"][:64] or None,
                "device_type": dt_id,
                "role": self._ensure_role(rec.get("role")),
                "site": site_id,
                "status": "active",
                "serial": (rec.get("serial") or "")[:50],
                "asset_tag": (rec.get("asset_tag") or None),
                "comments": rec.get("comments") or "",
                "custom_fields": rec.get("custom_fields") or {},
                "tags": rec.get("tags") or [],
            }
            if rack_id:
                desired["rack"] = rack_id
                if rec.get("position"):
                    desired["position"] = rec["position"]
                    desired["face"] = rec.get("face") or "front"
            nat = {"name": rec["name"][:64], "site_id": site_id} if (rec.get("name") and site_id) else None
            self._upsert_device(stat, rec, desired, nat)

    def _upsert_device(self, stat, rec, desired, nat) -> None:
        """Create/update a device, dropping fields NetBox rejects for uniqueness
        or geometry (rack position, asset_tag) so the device is never lost. Any
        dropped value is preserved in comments and recorded in the report."""
        attempt = dict(desired)
        dropped = []
        for _ in range(3):
            try:
                _id, action = self.up.upsert(EP_DEVICE, rec["rt_key"], attempt, nat)
                self._record(stat, action)
                if dropped:
                    self.unmig.add("Object", rec.get("source_id"),
                                   "device migrated with conflicting fields dropped",
                                   dropped=dropped)
                return
            except NetBoxError as exc:
                msg = str(exc).lower()
                changed = False
                if attempt.get("position") is not None and (
                        "position" in msg or "occupied" in msg or "space" in msg):
                    pos = attempt.pop("position", None)
                    face = attempt.pop("face", None)
                    attempt["comments"] = (attempt.get("comments", "") +
                                           f"\nRackTables position U{pos} ({face}) not placed "
                                           "in NetBox (space/overlap conflict).").strip()
                    dropped.append(f"position U{pos} ({face})")
                    changed = True
                if attempt.get("asset_tag") and "asset" in msg:
                    at = attempt.pop("asset_tag", None)
                    attempt["comments"] = (attempt.get("comments", "") +
                                           f"\nRackTables asset tag {at} not set "
                                           "(duplicate in NetBox).").strip()
                    dropped.append(f"asset_tag {at}")
                    changed = True
                if not changed:
                    stat.failed += 1
                    self.unmig.add("Object", rec.get("source_id"), f"import failed: {exc}")
                    log.error("import failed for Object/%s: %s", rec.get("source_id"), exc)
                    return
        stat.failed += 1
        self.unmig.add("Object", rec.get("source_id"),
                       "import failed after dropping conflicting fields")

    def _import_vms(self) -> None:
        stat = self.summary.stat("vms")
        for rec in self._progress("vms", self._load("vms")):
            stat.source += 1
            cluster_id = self.resolver.resolve(EP_CLUSTER, rec.get("cluster_ref")) \
                or self._default_cluster_id()
            desired = {
                "name": rec["name"][:64],
                "cluster": cluster_id,
                "role": self._ensure_role(rec.get("role")),
                "status": "active",
                "comments": rec.get("comments") or "",
                "custom_fields": rec.get("custom_fields") or {},
                "tags": rec.get("tags") or [],
            }
            self._safe(stat, "Object", rec.get("source_id"),
                       self.up.upsert, EP_VM, rec["rt_key"], desired,
                       {"name": rec["name"][:64]})

    # -- interfaces / cables -----------------------------------------------

    def _import_interfaces(self) -> None:
        stat = self.summary.stat("interfaces")
        for rec in self._progress("interfaces", self._load("interfaces")):
            stat.source += 1
            kind = rec.get("parent_kind")
            # MAC is folded into the description: NetBox 4.2+ moved MAC to a
            # separate MACAddress model and the writable interface.mac_address
            # shim is unreliable across 4.x. This keeps the value, losslessly.
            desc = rec.get("description") or ""
            if rec.get("mac_address"):
                desc = (desc + f" MAC {rec['mac_address']}").strip()
            desc = desc[:200]
            if kind == "vm":
                parent_id = self.resolver.resolve(EP_VM, rec.get("parent_ref"))
                endpoint = EP_VMIFACE
                desired = {
                    "virtual_machine": parent_id, "name": rec["name"][:64],
                    "description": desc,
                }
            else:
                parent_id = self.resolver.resolve(EP_DEVICE, rec.get("parent_ref"))
                endpoint = EP_IFACE
                desired = {
                    "device": parent_id, "name": rec["name"][:64],
                    "type": rec.get("type") or "other",
                    "description": desc,
                }
            if not parent_id and not self.dry_run:
                stat.failed += 1
                self.unmig.add("Port", rec.get("source_id"), "interface: parent unresolved")
                continue
            nat = {"name": rec["name"][:64]}
            nat["virtual_machine_id" if kind == "vm" else "device_id"] = parent_id
            iid = self._safe(stat, "Port", rec.get("source_id"),
                             self.up.upsert, endpoint, rec["rt_key"], desired,
                             nat if parent_id else None)
            if iid:
                info = {"id": iid, "kind": kind, "parent_id": parent_id, "name": rec["name"][:64]}
                self.iface_index[rec["rt_key"]] = info
                self.iface_by_obj_name[(rec.get("parent_ref"), rec["name"])] = info

    def _import_cables(self) -> None:
        stat = self.summary.stat("cables")
        for rec in self._progress("cables", self._load("cables")):
            stat.source += 1
            a = self.iface_index.get(rec["a_ref"])
            b = self.iface_index.get(rec["b_ref"])
            if not a or not b:
                # resolve from NetBox if not in this run's index
                a = a or self._iface_lookup(rec["a_ref"])
                b = b or self._iface_lookup(rec["b_ref"])
            if not a or not b:
                stat.failed += 1
                self.unmig.add("Link", rec.get("source_id"), "cable endpoint interface not found")
                continue
            if a["kind"] == "vm" or b["kind"] == "vm":
                stat.failed += 1
                self.unmig.add("Link", rec.get("source_id"),
                               "cable endpoint is a VM interface; NetBox cannot cable VM interfaces")
                continue
            # Cables are create-only: NetBox returns terminations in a shape that
            # cannot be reliably diffed, and terminations are not PATCH-able, so
            # we match by racktables_id and skip if it already exists.
            self._safe(stat, "Link", rec.get("source_id"),
                       self._upsert_cable, rec["rt_key"], a["id"], b["id"], rec.get("label") or "")

    def _upsert_cable(self, rt_key, a_id, b_id, label):
        existing = find_by_cf(self.nb, EP_CABLE, rt_key)
        if existing is not None:
            return existing["id"], SKIPPED
        if self.dry_run:
            return None, CREATED
        created = self.nb.create(EP_CABLE, {
            "a_terminations": [{"object_type": "dcim.interface", "object_id": a_id}],
            "b_terminations": [{"object_type": "dcim.interface", "object_id": b_id}],
            "status": "connected",
            "label": label,
            "custom_fields": {CF_NAME: rt_key},
            "tags": [{"slug": MIGRATION_TAG_SLUG}],
        })
        return created["id"], CREATED

    def _iface_lookup(self, rt_key: str) -> Optional[Dict[str, Any]]:
        obj = find_by_cf(self.nb, EP_IFACE, rt_key)
        if obj:
            return {"id": obj["id"], "kind": "device",
                    "parent_id": (obj.get("device") or {}).get("id"), "name": obj.get("name")}
        return None

    # -- prefixes / ip -----------------------------------------------------

    def _import_prefixes(self) -> None:
        stat = self.summary.stat("prefixes")
        for rec in self._progress("prefixes", self._load("prefixes")):
            stat.source += 1
            desired = {
                "prefix": rec["prefix"], "status": "active",
                "description": rec.get("description") or "",
                "comments": rec.get("comments") or "",
            }
            # No {prefix} natural-key fallback: RackTables can hold duplicate
            # CIDRs. If NetBox permits duplicates (ENFORCE_GLOBAL_UNIQUE off)
            # each creates independently; if not, record the duplicate instead
            # of hard-failing so the run stays clean and nothing is dropped.
            try:
                _id, action = self.up.upsert(EP_PREFIX, rec["rt_key"], desired, None)
                self._record(stat, action)
            except NetBoxError as exc:
                if "duplicate prefix" in str(exc).lower():
                    stat.skipped += 1
                    self.unmig.add(rec.get("source_table"), rec.get("source_id"),
                                   "duplicate CIDR already present in NetBox "
                                   "(global uniqueness); not re-created",
                                   prefix=rec["prefix"])
                else:
                    stat.failed += 1
                    self.unmig.add(rec.get("source_table"), rec.get("source_id"),
                                   f"import failed: {exc}")
                    log.error("import failed for %s/%s: %s",
                              rec.get("source_table"), rec.get("source_id"), exc)

    def _import_ip_addresses(self) -> None:
        stat = self.summary.stat("ip_addresses")
        for rec in self._progress("ip_addresses", self._load("ip_addresses")):
            stat.source += 1
            desired: Dict[str, Any] = {
                "address": rec["address"], "status": "active",
                "dns_name": rec.get("dns_name") or "",
                "description": rec.get("description") or "",
                "comments": rec.get("comments") or "",
            }
            if rec.get("role"):
                desired["role"] = rec["role"]
            # Assign to the first resolvable interface.
            for assign in rec.get("assignments") or []:
                info = self.iface_by_obj_name.get((assign.get("object_ref"), assign.get("interface_name")))
                if info:
                    otype = "virtualization.vminterface" if info["kind"] == "vm" else "dcim.interface"
                    desired["assigned_object_type"] = otype
                    desired["assigned_object_id"] = info["id"]
                    break
            # No natural-key fallback on address: shared/virtual IPs are
            # intentionally duplicated (A-plan), so matching must be by
            # racktables_id alone or they would collapse into one object.
            self._safe(stat, rec.get("source_table"), rec.get("source_id"),
                       self.up.upsert, EP_IP, rec["rt_key"], desired, None)

    # -- vlans --------------------------------------------------------------

    def _import_vlan_groups(self) -> None:
        stat = self.summary.stat("vlan_groups")
        for rec in self._progress("vlan_groups", self._load("vlan_groups")):
            stat.source += 1
            desired = {"name": rec["name"][:100], "slug": rec["slug"]}
            self._safe(stat, "VLANDomain", rec.get("source_id"),
                       self.up.upsert, EP_VLANGROUP, rec["rt_key"], desired,
                       {"slug": rec["slug"]})

    def _import_vlans(self) -> None:
        stat = self.summary.stat("vlans")
        for rec in self._progress("vlans", self._load("vlans")):
            stat.source += 1
            group_id = self.resolver.resolve(EP_VLANGROUP, rec.get("group_ref"))
            desired = {"vid": int(rec["vid"]), "name": rec["name"][:64], "status": "active"}
            if group_id:
                desired["group"] = group_id
            nat = {"vid": int(rec["vid"]), "group_id": group_id} if group_id else {"vid": int(rec["vid"])}
            self._safe(stat, "VLANDescription", rec.get("source_id"),
                       self.up.upsert, EP_VLAN, rec["rt_key"], desired, nat)

    def _apply_interface_vlans(self) -> None:
        recs = self._load("interface_vlans")
        if not recs:
            return
        stat = self.summary.stat("interface_vlans")
        for rec in recs:
            stat.source += 1
            info = self.iface_by_obj_name.get((rec.get("object_ref"), rec.get("interface_name")))
            if not info or info["kind"] == "vm":
                stat.skipped += 1
                continue
            untagged = self.resolver.resolve(EP_VLAN, rec.get("untagged_ref"))
            tagged = [self.resolver.resolve(EP_VLAN, t) for t in rec.get("tagged_refs") or []]
            tagged = [t for t in tagged if t]
            mode = rec.get("mode") or ("tagged" if tagged else "access")
            desired: Dict[str, Any] = {"mode": mode}
            if untagged:
                desired["untagged_vlan"] = untagged
            if tagged:
                desired["tagged_vlans"] = tagged
            try:
                existing = self.nb.get_one(EP_IFACE, {"id": info["id"]})
                patch = compute_patch(existing or {}, desired) if existing else desired
                if not patch:
                    stat.skipped += 1
                elif self.dry_run:
                    stat.updated += 1
                else:
                    self.nb.update(EP_IFACE, info["id"], desired)
                    stat.updated += 1
            except Exception as exc:
                stat.failed += 1
                self.unmig.add("PortVLAN", rec.get("object_ref"), f"vlan apply failed: {exc}")
