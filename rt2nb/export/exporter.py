"""RackTables export engine.

Reads everything needed for the migration with SELECT-only queries, adapting to
whatever tables/columns the live 0.20.x schema actually exposes, and writes one
JSON file per category into ``export_dir``.  Logical parent references are
stored as namespaced ``rt_key`` strings (e.g. ``object:42``) which the import
phase resolves to NetBox ids.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from ..constants import (
    CHAPTER_OBJTYPE,
    DEFAULT_ATTRIBUTE_MAP,
    classify_objtype,
)
from ..db import ReadOnlyConnection, Schema
from ..logging_setup import get_logger
from ..mapping import ipaddr
from ..mapping.normalize import cf_name, clean_name, normalize_mac, rt_key, slugify
from ..mapping.transforms import (
    ip_allocation_role,
    rack_position_and_face,
    resolve_device_type,
    strip_dict_markup,
    tag_records_from_path,
)
from ..report import Summary, UnmigratedReport

log = get_logger(__name__)

ALL_CATEGORIES = [
    "sites", "locations", "racks", "manufacturers", "device_types",
    "devices", "clusters", "vms", "interfaces", "cables", "prefixes",
    "ip_addresses", "vlan_groups", "vlans", "tags",
]

# EntityLink realms that all resolve to Object ids and express containment.
# Rack/Row/Location are VIEWs over Object, so their ids equal Object.id.
_CONTAINMENT_REALMS = {"object", "rack", "row", "location"}


class Exporter:
    def __init__(
        self,
        conn: ReadOnlyConnection,
        schema: Schema,
        config,
        categories: Optional[Set[str]] = None,
    ):
        self.conn = conn
        self.schema = schema
        self.cfg = config
        self.mapping = config.mapping
        self.categories = categories or set(ALL_CATEGORIES)
        self.export_dir = config.paths["export_dir"]
        self.unmig = UnmigratedReport()
        self.summary = Summary()

        # Shared caches populated by _load_base().
        self.objtype_label: Dict[int, str] = {}
        self.objects: Dict[int, Dict[str, Any]] = {}
        self.obj_class: Dict[int, str] = {}
        self.child_to_parent: Dict[int, int] = {}
        self.dictionary: Dict[int, str] = {}
        self.attr_meta: Dict[int, Dict[str, Any]] = {}
        self.tag_by_id: Dict[int, Dict[str, Any]] = {}

        # rt_key -> site rt_key resolution for racks/rows.
        self.default_site_key = "default:site"

    # -- public entry -------------------------------------------------------

    def run(self) -> Tuple[Summary, UnmigratedReport]:
        os.makedirs(self.export_dir, exist_ok=True)
        self._load_base()
        self._export_sites_and_locations()
        self._export_racks()
        # Manufacturers and device types are derived while exporting devices.
        self._export_devices_and_vms_and_clusters()
        self._export_interfaces()
        self._export_cables()
        self._export_prefixes()
        self._export_ip_addresses()
        self._export_vlans()
        self._export_tags()
        self._write_meta()
        self.unmig.save(os.path.join(self.export_dir, "unmigrated_report.json"))
        return self.summary, self.unmig

    # -- helpers ------------------------------------------------------------

    def _dump(self, name: str, records: List[Dict[str, Any]]) -> None:
        path = os.path.join(self.export_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, ensure_ascii=False, default=str)
        log.info("export: %-14s -> %4d records (%s)", name, len(records), path)

    def _want(self, category: str) -> bool:
        return category in self.categories

    # -- base data ----------------------------------------------------------

    def _load_base(self) -> None:
        # Object-type labels.
        if self.schema.has_table("Dictionary"):
            for r in self.conn.query(
                "SELECT dict_key, dict_value FROM Dictionary WHERE chapter_id = %s",
                (CHAPTER_OBJTYPE,),
            ):
                self.objtype_label[int(r["dict_key"])] = strip_dict_markup(r["dict_value"])
            # Full dictionary for resolving dict-typed attribute values.
            for r in self.conn.query("SELECT dict_key, dict_value FROM Dictionary"):
                self.dictionary[int(r["dict_key"])] = strip_dict_markup(r["dict_value"])

        # Objects.
        obj_cols = self.schema.present_columns(
            "Object",
            ["id", "name", "label", "objtype_id", "asset_no", "comment", "has_problems"],
        )
        rows = self.conn.query(f"SELECT {', '.join(obj_cols)} FROM Object")
        for r in rows:
            oid = int(r["id"])
            r["_label_type"] = self.objtype_label.get(int(r.get("objtype_id") or 0), "")
            self.objects[oid] = r
            self.obj_class[oid] = classify_objtype(r["_label_type"])

        # Object containment via EntityLink (object -> object).
        # RackTables stores the physical hierarchy (Location -> Row -> Rack) and
        # generic object containment in EntityLink.  Depending on the install it
        # uses either the generic 'object' realm or the typed realms
        # 'location'/'row'/'rack'.  Rack/Row/Location are VIEWs over Object, so
        # all of these entity ids are Object ids and can share one child->parent
        # map.
        if self.schema.has_table("EntityLink"):
            for r in self.conn.query(
                "SELECT parent_entity_type, parent_entity_id, "
                "child_entity_type, child_entity_id FROM EntityLink"
            ):
                if (r["parent_entity_type"] in _CONTAINMENT_REALMS
                        and r["child_entity_type"] in _CONTAINMENT_REALMS):
                    self.child_to_parent[int(r["child_entity_id"])] = int(r["parent_entity_id"])

        # Attribute metadata.
        if self.schema.has_table("Attribute"):
            for r in self.conn.query("SELECT id, type, name FROM Attribute"):
                self.attr_meta[int(r["id"])] = {"type": r["type"], "name": r["name"]}

        # Tags.
        if self.schema.has_table("TagTree"):
            tcols = self.schema.present_columns("TagTree", ["id", "parent_id", "tag"])
            for r in self.conn.query(f"SELECT {', '.join(tcols)} FROM TagTree"):
                self.tag_by_id[int(r["id"])] = {
                    "tag": r["tag"],
                    "parent_id": r.get("parent_id"),
                }

        log.info(
            "loaded %d objects, %d containment links, %d tags, %d dict entries",
            len(self.objects), len(self.child_to_parent),
            len(self.tag_by_id), len(self.dictionary),
        )

    # Resolve the Site rt_key for a given object by walking containment up to a
    # Location (or the top), honouring the row_as mapping.
    def _site_key_for(self, oid: int) -> str:
        row_as = self.mapping.get("row_as", "location")
        if row_as == "single_site":
            return self.default_site_key
        # Walk parents. In 'location' mode the Site is the nearest Location
        # (Rows become NetBox Locations, not Sites). In 'site' mode the Site is
        # the nearest Row, or a Location if the object sits directly under one.
        seen = set()
        cur = self.child_to_parent.get(oid)
        while cur is not None and cur not in seen:
            seen.add(cur)
            cls = self.obj_class.get(cur)
            if cls == "location":
                return rt_key("object", cur)
            if cls == "row" and row_as == "site":
                return rt_key("object", cur)
            cur = self.child_to_parent.get(cur)
        return self.default_site_key

    def _tag_paths_for(self, realm: str, entity_id: int) -> List[List[str]]:
        """Return root-to-leaf name paths for tags on one entity."""
        if not self.schema.has_table("TagStorage"):
            return []
        rows = self.conn.query(
            "SELECT tag_id FROM TagStorage WHERE entity_realm = %s AND entity_id = %s",
            (realm, entity_id),
        )
        paths = []
        for r in rows:
            path = self._tag_path(int(r["tag_id"]))
            if path:
                paths.append(path)
        return paths

    def _tag_path(self, tag_id: int) -> List[str]:
        names: List[str] = []
        seen = set()
        cur: Optional[int] = tag_id
        while cur and cur not in seen:
            seen.add(cur)
            node = self.tag_by_id.get(cur)
            if not node:
                break
            names.append(node["tag"])
            parent = node.get("parent_id")
            cur = int(parent) if parent else None
        return list(reversed(names))

    def _object_tags(self, oid: int) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        seen = set()
        for path in self._tag_paths_for("object", oid):
            for rec in tag_records_from_path(path, self.mapping.get("tag_strategy", "flatten_path")):
                if rec["slug"] not in seen:
                    seen.add(rec["slug"])
                    out.append(rec)
        return out

    # -- attribute values ---------------------------------------------------

    def _attr_values(self, oid: int) -> List[Dict[str, Any]]:
        if not self.schema.has_table("AttributeValue"):
            return []
        cols = self.schema.present_columns(
            "AttributeValue",
            ["attr_id", "uint_value", "float_value", "string_value"],
        )
        rows = self.conn.query(
            f"SELECT {', '.join(cols)} FROM AttributeValue WHERE object_id = %s",
            (oid,),
        )
        out = []
        for r in rows:
            meta = self.attr_meta.get(int(r["attr_id"]))
            if not meta:
                continue
            value = self._resolve_attr_value(meta["type"], r)
            if value in (None, ""):
                continue
            out.append({"name": meta["name"], "type": meta["type"], "value": value})
        return out

    def _resolve_attr_value(self, atype: str, row: Dict[str, Any]) -> Any:
        if atype == "dict":
            key = row.get("uint_value")
            if key is None:
                return None
            return self.dictionary.get(int(key), str(key))
        if atype == "uint":
            return row.get("uint_value")
        if atype == "float":
            return row.get("float_value")
        # string / date / other
        return clean_name(row.get("string_value") or "")

    def _obj_name(self, oid: int) -> str:
        obj = self.objects.get(oid, {})
        return clean_name(obj.get("name") or obj.get("label") or f"object-{oid}")

    # -- sites / locations --------------------------------------------------

    def _export_sites_and_locations(self) -> None:
        row_as = self.mapping.get("row_as", "location")
        default_site = {
            "rt_key": self.default_site_key,
            "source_table": "config",
            "source_id": None,
            "name": clean_name(self.mapping.get("default_site_name", "Migrated")),
            "slug": slugify(self.mapping.get("default_site_slug", "migrated")),
            "is_default": True,
        }
        sites: List[Dict[str, Any]] = [default_site]
        locations: List[Dict[str, Any]] = []

        for oid, cls in self.obj_class.items():
            if cls not in ("location", "row"):
                continue
            name = self._obj_name(oid)
            key = rt_key("object", oid)
            make_site = (
                (cls == "location" and row_as in ("location", "site"))
                or (cls == "row" and row_as == "site")
            )
            if make_site:
                sites.append({
                    "rt_key": key, "source_table": "Object", "source_id": oid,
                    "name": name, "slug": slugify(name),
                })
            else:
                site_ref = (
                    self.default_site_key if row_as == "single_site"
                    else self._site_key_for(oid)
                )
                locations.append({
                    "rt_key": key, "source_table": "Object", "source_id": oid,
                    "name": name, "slug": slugify(name),
                    "site_ref": site_ref, "kind": cls,
                })

        if self._want("sites"):
            self.summary.stat("sites").source = len(sites)
            self._dump("sites", sites)
        if self._want("locations"):
            self.summary.stat("locations").source = len(locations)
            self._dump("locations", locations)

    # -- racks --------------------------------------------------------------

    def _rack_height(self, oid: int, rackspace: Dict[int, List[Tuple[int, int, str]]]) -> int:
        # 1) explicit Height attribute on the rack object.
        for av in self._attr_values(oid):
            if "height" in av["name"].lower():
                try:
                    return max(1, int(float(av["value"])))
                except (TypeError, ValueError):
                    pass
        # 2) tallest occupied unit in RackSpace.
        cells = rackspace.get(oid, [])
        if cells:
            return max(1, max(u for (_r, u, _a) in cells if u is not None))
        return 42

    def _export_racks(self) -> None:
        row_as = self.mapping.get("row_as", "location")
        # RackSpace keyed by rack_id for height inference.
        rack_cells: Dict[int, List[Tuple[int, int, str]]] = defaultdict(list)
        if self.schema.has_table("RackSpace"):
            for r in self.conn.query(
                "SELECT rack_id, unit_no, atom, object_id FROM RackSpace"
            ):
                rack_cells[int(r["rack_id"])].append(
                    (int(r["rack_id"]), r["unit_no"], r["atom"])
                )

        racks: List[Dict[str, Any]] = []
        for oid, cls in self.obj_class.items():
            if cls != "rack":
                continue
            name = self._obj_name(oid)
            parent = self.child_to_parent.get(oid)
            location_ref = None
            if row_as == "location" and parent is not None and self.obj_class.get(parent) == "row":
                location_ref = rt_key("object", parent)
            racks.append({
                "rt_key": rt_key("object", oid), "source_table": "Object",
                "source_id": oid, "name": name,
                "u_height": self._rack_height(oid, rack_cells),
                "site_ref": self._site_key_for(oid),
                "location_ref": location_ref,
                "comments": clean_name(self.objects[oid].get("comment") or ""),
                "tags": self._object_tags(oid),
            })
        if self._want("racks"):
            self.summary.stat("racks").source = len(racks)
            self._dump("racks", racks)

    # -- devices / vms / clusters -------------------------------------------

    def _rackspace_by_object(self) -> Dict[int, List[Tuple[int, int, str]]]:
        out: Dict[int, List[Tuple[int, int, str]]] = defaultdict(list)
        if not self.schema.has_table("RackSpace"):
            return out
        for r in self.conn.query(
            "SELECT rack_id, unit_no, atom, object_id FROM RackSpace WHERE object_id IS NOT NULL"
        ):
            if r.get("object_id") is None:
                continue
            out[int(r["object_id"])].append(
                (int(r["rack_id"]), r["unit_no"], r["atom"])
            )
        return out

    def _export_devices_and_vms_and_clusters(self) -> None:
        rackspace = self._rackspace_by_object()
        attr_overrides = {**DEFAULT_ATTRIBUTE_MAP, **(self.mapping.get("attribute_overrides") or {})}
        placeholder_mfr = self.mapping.get("placeholder_manufacturer", "Unknown")
        placeholder_dt = self.mapping.get("placeholder_device_type", "Unknown Device Type")
        placeholder_u = int(self.mapping.get("placeholder_u_height", 1))

        manufacturers: Dict[str, Dict[str, Any]] = {}
        device_types: Dict[str, Dict[str, Any]] = {}
        devices: List[Dict[str, Any]] = []
        vms: List[Dict[str, Any]] = []
        clusters: List[Dict[str, Any]] = []

        def ensure_mfr(name: str) -> str:
            slug = slugify(name)
            manufacturers.setdefault(slug, {
                "rt_key": f"mfr:{slug}", "source_table": "Dictionary",
                "source_id": slug, "name": name, "slug": slug,
            })
            return f"mfr:{slug}"

        model_to_slug: Dict[Tuple[str, str], str] = {}

        def ensure_dt(mfr_name: str, model: str, u_height: int) -> str:
            mk = (mfr_name, model)
            if mk in model_to_slug:
                slug = model_to_slug[mk]
                device_types[slug]["u_height"] = max(device_types[slug]["u_height"], u_height)
                return f"devicetype:{slug}"
            # Ensure a unique slug even when two distinct models slugify alike.
            base = slugify(f"{mfr_name}-{model}")
            slug, n = base, 2
            while slug in device_types:
                slug, n = f"{base}-{n}", n + 1
            model_to_slug[mk] = slug
            device_types[slug] = {
                "rt_key": f"devicetype:{slug}", "source_table": "Dictionary",
                "source_id": slug, "manufacturer_ref": ensure_mfr(mfr_name),
                "model": model, "slug": slug, "u_height": u_height,
            }
            return f"devicetype:{slug}"

        for oid, cls in self.obj_class.items():
            if cls not in ("device", "vm", "cluster"):
                continue
            obj = self.objects[oid]
            name = self._obj_name(oid)
            key = rt_key("object", oid)
            comments = [clean_name(obj.get("comment") or "")]
            tags = self._object_tags(oid)

            if cls == "cluster":
                clusters.append({
                    "rt_key": key, "source_table": "Object", "source_id": oid,
                    "name": name, "comments": comments[0], "tags": tags,
                })
                continue

            # Attribute processing (shared by device & vm).
            serial = None
            asset_tag = clean_name(obj.get("asset_no") or "") or None
            custom_fields: Dict[str, Any] = {}
            hw_label = None
            for av in self._attr_values(oid):
                aname = av["name"]
                if aname.lower() in ("hw type", "hardware type"):
                    hw_label = av["value"]
                rule = attr_overrides.get(aname)
                if not rule:
                    # Unmapped attribute: keep it as a custom field, don't drop.
                    custom_fields[cf_name(aname)] = av["value"]
                    continue
                target = rule.get("target")
                if target == "serial":
                    serial = str(av["value"])
                elif target == "asset_tag":
                    asset_tag = str(av["value"])
                elif target == "comments":
                    comments.append(f"{aname}: {av['value']}")
                else:  # custom_field
                    cf = cf_name(rule.get("name") or aname)
                    custom_fields[cf] = av["value"]

            # Occupancy -> device type u_height.  Only a *contiguous* span is a
            # meaningful height; a non-contiguous mount (e.g. units [4,25,33])
            # must NOT set a 30U type, or every device sharing that type would
            # collide.  Non-contiguous / unracked -> placeholder height.
            cells = [(u, a) for (_r, u, a) in rackspace.get(oid, []) if u is not None]
            if cells:
                units = sorted({u for u, _ in cells})
                contiguous = (units[-1] - units[0] + 1 == len(units))
                occ_u = len(units) if contiguous else placeholder_u
            else:
                occ_u = placeholder_u

            mfr, model, hw_typed = resolve_device_type(
                hw_label, self.objects[oid].get("_label_type"),
                placeholder_mfr, placeholder_dt,
            )
            if not hw_typed and hw_label:
                comments.append(f"RackTables HW type: {strip_dict_markup(hw_label)}")
            # Only HW-typed models derive their U-height from rack occupancy;
            # fallback (object-type) models stay at the placeholder height since
            # they are shared by many unrelated objects of unknown size.
            dt_height = max(occ_u, placeholder_u) if hw_typed else placeholder_u
            dt_ref = ensure_dt(mfr, model, dt_height) if cls == "device" else None

            base = {
                "rt_key": key, "source_table": "Object", "source_id": oid,
                "name": name, "serial": serial, "asset_tag": asset_tag,
                # NetBox device/VM role from the RackTables object-type label
                # (Server, Network switch, Router, PDU, ...); empty falls back
                # to the default role at import time.
                "role": clean_name(self.objects[oid].get("_label_type") or ""),
                "custom_fields": {k: v for k, v in custom_fields.items() if v not in (None, "")},
                "comments": "\n".join(c for c in comments if c),
                "tags": tags,
            }

            if cls == "vm":
                parent = self.child_to_parent.get(oid)
                cluster_ref = rt_key("object", parent) if parent and self.obj_class.get(parent) == "cluster" else None
                base["cluster_ref"] = cluster_ref
                vms.append(base)
            else:
                base["device_type_ref"] = dt_ref
                base["site_ref"] = None
                base["rack_ref"] = None
                base["position"] = None
                base["face"] = "front"
                if cells:
                    rack_ids = {r for (r, _u, _a) in rackspace.get(oid, [])}
                    rack_id = sorted(rack_ids)[0]
                    if len(rack_ids) > 1:
                        self.unmig.add("RackSpace", oid,
                                       "object mounted in multiple racks; used lowest rack_id",
                                       rack_ids=sorted(rack_ids))
                    pos, face, problems = rack_position_and_face(cells)
                    base["rack_ref"] = rt_key("object", rack_id)
                    base["position"] = pos
                    base["face"] = face
                    base["site_ref"] = self._site_key_for(rack_id)
                    if problems:
                        base["comments"] = (base["comments"] + "\n" +
                                            "; ".join(problems)).strip()
                        self.unmig.add("RackSpace", oid,
                                       "rack placement not fully representable in NetBox",
                                       problems=problems, position=pos, face=face)
                # A device whose parent is another device (chassis/blade): note it.
                parent = self.child_to_parent.get(oid)
                if parent and self.obj_class.get(parent) == "device":
                    base["comments"] = (base["comments"] +
                                        f"\nContained in RackTables object #{parent} "
                                        f"({self._obj_name(parent)})").strip()
                devices.append(base)

        # Disambiguate duplicate device names within the same site. NetBox keys
        # device uniqueness by (site, name); without this, same-named devices
        # collapse via the natural-key fallback (breaking faithfulness and
        # idempotency). The original name is preserved in comments.
        name_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for d in devices:
            site = d.get("site_ref") or self.default_site_key
            name_groups[(site, d["name"])].append(d)
        for members in name_groups.values():
            if len(members) > 1:
                for d in members:
                    orig = d["name"]
                    d["name"] = f"{orig} [{d['source_id']}]"[:64]
                    d["comments"] = (d.get("comments", "") +
                                     f"\nRackTables name: {orig} (disambiguated)").strip()

        # NetBox requires a globally-unique asset_tag. RackTables asset numbers
        # are not unique, so keep the tag on the lowest source_id and move the
        # duplicates into comments (field cleared) to avoid a hard conflict and
        # keep re-runs idempotent.
        seen_asset: Dict[str, Any] = {}
        for d in sorted(devices, key=lambda x: x["source_id"]):
            at = d.get("asset_tag")
            if not at:
                continue
            if at in seen_asset:
                d["comments"] = (d.get("comments", "") +
                                 f"\nRackTables asset tag: {at} "
                                 f"(duplicate; assigned to object {seen_asset[at]})").strip()
                d["asset_tag"] = None
            else:
                seen_asset[at] = d["source_id"]

        # VMs: NetBox keys uniqueness by (cluster, name); disambiguate likewise.
        vm_groups: Dict[Tuple[Any, str], List[Dict[str, Any]]] = defaultdict(list)
        for v in vms:
            vm_groups[(v.get("cluster_ref"), v["name"])].append(v)
        for members in vm_groups.values():
            if len(members) > 1:
                for v in members:
                    orig = v["name"]
                    v["name"] = f"{orig} [{v['source_id']}]"[:64]
                    v["comments"] = (v.get("comments", "") +
                                     f"\nRackTables name: {orig} (disambiguated)").strip()

        if self._want("manufacturers"):
            recs = list(manufacturers.values())
            self.summary.stat("manufacturers").source = len(recs)
            self._dump("manufacturers", recs)
        if self._want("device_types"):
            recs = list(device_types.values())
            self.summary.stat("device_types").source = len(recs)
            self._dump("device_types", recs)
        if self._want("clusters"):
            self.summary.stat("clusters").source = len(clusters)
            self._dump("clusters", clusters)
        if self._want("vms"):
            self.summary.stat("vms").source = len(vms)
            self._dump("vms", vms)
        if self._want("devices"):
            self.summary.stat("devices").source = len(devices)
            self._dump("devices", devices)

    # -- interfaces ---------------------------------------------------------

    def _export_interfaces(self) -> None:
        from ..mapping.interface_types import map_interface_type

        overrides = self.mapping.get("interface_type_overrides") or {}
        interfaces: List[Dict[str, Any]] = []
        # Interface names already present per object (exact clean_name), so an IP
        # allocation that matches a physical Port reuses it instead of adding a
        # duplicate synthetic interface.
        have: Dict[int, set] = defaultdict(set)

        # 1) Physical ports (L1/L2) from the Port table.
        if self.schema.has_table("Port"):
            oif = {}
            if self.schema.has_table("PortOuterInterface"):
                for r in self.conn.query("SELECT id, oif_name FROM PortOuterInterface"):
                    oif[int(r["id"])] = r["oif_name"]
            iif = {}
            if self.schema.has_table("PortInnerInterface"):
                for r in self.conn.query("SELECT id, iif_name FROM PortInnerInterface"):
                    iif[int(r["id"])] = r["iif_name"]
            cols = self.schema.present_columns(
                "Port", ["id", "object_id", "name", "iif_id", "oif_id",
                         "l2address", "label", "reservation_comment"],
            )
            for r in self.conn.query(f"SELECT {', '.join(cols)} FROM Port"):
                oid = int(r["object_id"])
                cls = self.obj_class.get(oid)
                if cls not in ("device", "vm"):
                    # Port on a rack/row/location/cluster object: not an interface.
                    self.unmig.add("Port", r["id"],
                                   "port on non-device object; skipped", object_id=oid)
                    continue
                outer = oif.get(int(r["oif_id"])) if r.get("oif_id") else None
                inner = iif.get(int(r["iif_id"])) if r.get("iif_id") else None
                nb_type, fallback = map_interface_type(outer, inner, overrides)
                if fallback and (outer or inner):
                    log.warning("interface type fallback -> other for port %s (%s / %s)",
                                r["id"], outer, inner)
                name = clean_name(r.get("name") or f"port-{r['id']}")
                have[oid].add(name)
                interfaces.append({
                    "rt_key": rt_key("port", r["id"]), "source_table": "Port",
                    "source_id": int(r["id"]),
                    "parent_kind": cls, "parent_ref": rt_key("object", oid),
                    "name": name,
                    "type": nb_type,
                    "mac_address": self._l2_to_mac(r.get("l2address")),
                    "description": clean_name(r.get("label") or r.get("reservation_comment") or ""),
                })

        # 2) L3 interfaces implied by IP allocations. In RackTables a machine's
        # IPs (NIC names like eth0/bond0 and the IPMI address) are bound to the
        # object under a free-text interface name that usually has no Port row —
        # servers often have no ports at all. Without this, those IPs would land
        # in NetBox unattached to the device. Create one interface per distinct
        # (object, allocation name) not already covered by a Port so the IP
        # import can bind the address to it.
        for table in ("IPv4Allocation", "IPv6Allocation"):
            if not self.schema.has_table(table):
                continue
            acols = self.schema.present_columns(table, ["object_id", "name"])
            if "object_id" not in acols or "name" not in acols:
                continue
            for r in self.conn.query(f"SELECT DISTINCT object_id, name FROM {table}"):
                oid = int(r["object_id"])
                cls = self.obj_class.get(oid)
                if cls not in ("device", "vm"):
                    continue
                name = clean_name(r.get("name") or "")
                if not name or name in have[oid]:
                    continue
                have[oid].add(name)
                interfaces.append({
                    "rt_key": f"ipiface:{oid}:{slugify(name)}",
                    "source_table": table, "source_id": f"{oid}:{name}",
                    "parent_kind": cls, "parent_ref": rt_key("object", oid),
                    "name": name,
                    "type": "other",
                    "mac_address": None,
                    "description": "L3 interface (from RackTables IP allocation)",
                })

        if self._want("interfaces"):
            self.summary.stat("interfaces").source = len(interfaces)
            self._dump("interfaces", interfaces)

    @staticmethod
    def _l2_to_mac(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = bytes(value).hex()
        return normalize_mac(str(value))

    # -- cables -------------------------------------------------------------

    def _export_cables(self) -> None:
        if not self.schema.has_table("Link"):
            self._dump("cables", [])
            return
        cols = self.schema.present_columns("Link", ["porta", "portb", "cable"])
        cables: List[Dict[str, Any]] = []
        seen = set()
        for r in self.conn.query(f"SELECT {', '.join(cols)} FROM Link"):
            a, b = int(r["porta"]), int(r["portb"])
            lo, hi = sorted((a, b))
            key = f"{lo}-{hi}"
            if key in seen:
                continue
            seen.add(key)
            cables.append({
                "rt_key": rt_key("link", key), "source_table": "Link",
                "source_id": key,
                "a_ref": rt_key("port", a), "b_ref": rt_key("port", b),
                "label": clean_name(r.get("cable") or ""),
            })
        if self._want("cables"):
            self.summary.stat("cables").source = len(cables)
            self._dump("cables", cables)

    # -- prefixes -----------------------------------------------------------

    def _export_prefixes(self) -> None:
        prefixes: List[Dict[str, Any]] = []
        if self.schema.has_table("IPv4Network"):
            cols = self.schema.present_columns("IPv4Network", ["id", "ip", "mask", "name", "comment"])
            for r in self.conn.query(f"SELECT {', '.join(cols)} FROM IPv4Network"):
                cidr = ipaddr.ipv4_cidr(r["ip"], r["mask"])
                if not cidr:
                    self.unmig.add("IPv4Network", r["id"], "unparseable network")
                    continue
                prefixes.append(self._prefix_rec("ipv4net", r, cidr))
        if self.schema.has_table("IPv6Network"):
            cols = self.schema.present_columns("IPv6Network", ["id", "ip", "mask", "name", "comment"])
            for r in self.conn.query(f"SELECT {', '.join(cols)} FROM IPv6Network"):
                cidr = ipaddr.ipv6_cidr(r["ip"], r["mask"])
                if not cidr:
                    self.unmig.add("IPv6Network", r["id"], "unparseable network")
                    continue
                prefixes.append(self._prefix_rec("ipv6net", r, cidr))
        if self._want("prefixes"):
            self.summary.stat("prefixes").source = len(prefixes)
            self._dump("prefixes", prefixes)

    @staticmethod
    def _prefix_rec(source: str, r: Dict[str, Any], cidr: str) -> Dict[str, Any]:
        desc = clean_name(r.get("name") or "")
        return {
            "rt_key": rt_key(source, r["id"]), "source_table": source,
            "source_id": int(r["id"]), "prefix": cidr,
            "description": desc[:200],
            "comments": clean_name(r.get("comment") or ""),
        }

    # -- ip addresses -------------------------------------------------------

    @staticmethod
    def _ip_records(
        family: int,
        id_part: Any,
        addr: str,
        base_desc: str,
        dns: str,
        comments: str,
        allocs: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build the export record(s) for one address (IPv4 or IPv6).

        With allocations, each device binding becomes its own IPAddress (A-plan:
        shared/virtual IPs are fully reproduced). Without any, a single unbound
        address is emitted. v4/v6 differ only in the rt_key namespace, source
        table name and family, so the record shape is shared here.
        """
        fam = f"IPv{family}"
        alloc_ns = f"ipv{family}alloc"
        addr_ns = f"ipv{family}addr"
        common = {
            "address": addr, "family": family, "dns_name": dns,
            "description": base_desc, "comments": comments,
        }
        if not allocs:
            return [{
                "rt_key": rt_key(addr_ns, id_part),
                "source_table": f"{fam}Address", "source_id": id_part,
                **common, "role": None, "assignments": [],
            }]
        shared = len(allocs) > 1
        out: List[Dict[str, Any]] = []
        for a in allocs:
            obj_id = int(a["object_id"])
            iface = clean_name(a.get("name") or "")
            role, note = ip_allocation_role(a.get("type"))
            out.append({
                "rt_key": rt_key(alloc_ns, f"{id_part}:{obj_id}:{slugify(iface) or 'x'}"),
                "source_table": f"{fam}Allocation", "source_id": id_part,
                **common,
                "role": role or ("anycast" if shared else None),
                "assignments": [{
                    "object_ref": rt_key("object", obj_id),
                    "interface_name": iface, "rt_type": a.get("type"), "note": note,
                }],
            })
        return out

    def _export_ip_addresses(self) -> None:
        # Build network lists for prefix-length lookup.
        v4nets: List[Tuple[int, int]] = []  # (network_int, mask)
        if self.schema.has_table("IPv4Network"):
            for r in self.conn.query("SELECT ip, mask FROM IPv4Network"):
                v4nets.append((int(r["ip"]) & 0xFFFFFFFF, int(r["mask"])))
        v4nets.sort(key=lambda t: t[1], reverse=True)  # longest mask first

        def v4_prefixlen(ip_int: int) -> int:
            for net_int, mask in v4nets:
                if mask == 0:
                    return 32
                shift = 32 - mask
                if (ip_int >> shift) == (net_int >> shift):
                    return mask
            return 32

        records: List[Dict[str, Any]] = []

        # Address-level metadata (name/comment) keyed by ip.
        v4_meta: Dict[int, Dict[str, Any]] = {}
        if self.schema.has_table("IPv4Address"):
            cols = self.schema.present_columns("IPv4Address", ["ip", "name", "comment", "reserved"])
            for r in self.conn.query(
                f"SELECT {', '.join(cols)} FROM IPv4Address "
                "WHERE name <> '' OR comment <> '' OR reserved = 'yes'"
            ):
                v4_meta[int(r["ip"]) & 0xFFFFFFFF] = r

        # Allocations grouped by ip so each device binding becomes its own
        # NetBox IPAddress (A-plan: shared/virtual IPs are fully reproduced;
        # NetBox permits duplicate addresses in the global table).
        v4_allocs: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        if self.schema.has_table("IPv4Allocation"):
            cols = self.schema.present_columns("IPv4Allocation", ["object_id", "ip", "name", "type"])
            for r in self.conn.query(f"SELECT {', '.join(cols)} FROM IPv4Allocation"):
                v4_allocs[int(r["ip"]) & 0xFFFFFFFF].append(r)

        v4_ips = set(v4_meta) | set(v4_allocs)
        for ip_int in sorted(v4_ips):
            addr = ipaddr.with_prefixlen(ipaddr.ipv4_int_to_str(ip_int), v4_prefixlen(ip_int))
            meta = v4_meta.get(ip_int, {})
            base_desc = clean_name(meta.get("name") or "")
            dns = _safe_dns(meta.get("name"))
            comments = clean_name(meta.get("comment") or "")
            allocs = v4_allocs.get(ip_int, [])
            records.extend(self._ip_records(4, ip_int, addr, base_desc, dns, comments, allocs))

        # IPv6 (prefix length defaults to /128 for bare addresses).
        v6_default = 128
        v6_meta: Dict[str, Dict[str, Any]] = {}
        if self.schema.has_table("IPv6Address"):
            cols = self.schema.present_columns("IPv6Address", ["ip", "name", "comment", "reserved"])
            for r in self.conn.query(
                f"SELECT {', '.join(cols)} FROM IPv6Address "
                "WHERE name <> '' OR comment <> '' OR reserved = 'yes'"
            ):
                a = ipaddr.ipv6_bytes_to_str(r["ip"])
                if a:
                    v6_meta[a] = r
        v6_allocs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if self.schema.has_table("IPv6Allocation"):
            cols = self.schema.present_columns("IPv6Allocation", ["object_id", "ip", "name", "type"])
            for r in self.conn.query(f"SELECT {', '.join(cols)} FROM IPv6Allocation"):
                a = ipaddr.ipv6_bytes_to_str(r["ip"])
                if a:
                    v6_allocs[a].append(r)
        for a in sorted(set(v6_meta) | set(v6_allocs)):
            addr = ipaddr.with_prefixlen(a, v6_default)
            meta = v6_meta.get(a, {})
            base_desc = clean_name(meta.get("name") or "")
            dns = _safe_dns(meta.get("name"))
            comments = clean_name(meta.get("comment") or "")
            allocs = v6_allocs.get(a, [])
            records.extend(self._ip_records(6, a, addr, base_desc, dns, comments, allocs))

        if self._want("ip_addresses"):
            self.summary.stat("ip_addresses").source = len(records)
            self._dump("ip_addresses", records)

    # -- vlans --------------------------------------------------------------

    def _export_vlans(self) -> None:
        groups: List[Dict[str, Any]] = []
        vlans: List[Dict[str, Any]] = []
        if self.schema.has_table("VLANDomain"):
            dcols = self.schema.present_columns("VLANDomain", ["id", "description"])
            for r in self.conn.query(f"SELECT {', '.join(dcols)} FROM VLANDomain"):
                name = clean_name(r.get("description") or f"domain-{r['id']}")
                groups.append({
                    "rt_key": rt_key("vlandomain", r["id"]), "source_table": "VLANDomain",
                    "source_id": int(r["id"]), "name": name, "slug": slugify(name),
                })
        if self.schema.has_table("VLANDescription"):
            vcols = self.schema.present_columns(
                "VLANDescription", ["domain_id", "vlan_id", "vlan_type", "vlan_descr"]
            )
            for r in self.conn.query(f"SELECT {', '.join(vcols)} FROM VLANDescription"):
                vid = int(r["vlan_id"])
                if vid < 1 or vid > 4094:
                    continue
                name = clean_name(r.get("vlan_descr") or f"VLAN{vid}")
                vlans.append({
                    "rt_key": rt_key("vlan", f"{r['domain_id']}-{vid}"),
                    "source_table": "VLANDescription",
                    "source_id": f"{r['domain_id']}-{vid}",
                    "vid": vid, "name": name[:64] or f"VLAN{vid}",
                    "group_ref": rt_key("vlandomain", r["domain_id"]),
                })
        if self._want("vlan_groups"):
            self.summary.stat("vlan_groups").source = len(groups)
            self._dump("vlan_groups", groups)
        if self._want("vlans"):
            self.summary.stat("vlans").source = len(vlans)
            self._dump("vlans", vlans)

        self._export_interface_vlans()

    def _export_interface_vlans(self) -> None:
        # Domain binding per switch object.
        switch_domain: Dict[int, int] = {}
        if self.schema.has_table("VLANSwitch"):
            for r in self.conn.query("SELECT object_id, domain_id FROM VLANSwitch"):
                switch_domain[int(r["object_id"])] = int(r["domain_id"])

        config: Dict[Tuple[int, str], Dict[str, Any]] = {}

        def rec_for(oid: int, port: str) -> Dict[str, Any]:
            k = (oid, port)
            if k not in config:
                config[k] = {
                    "object_ref": rt_key("object", oid),
                    "interface_name": clean_name(port),
                    "mode": None, "untagged_ref": None, "tagged_refs": [],
                }
            return config[k]

        if self.schema.has_table("PortVLANMode"):
            for r in self.conn.query("SELECT object_id, port_name, vlan_mode FROM PortVLANMode"):
                rec = rec_for(int(r["object_id"]), r["port_name"])
                rec["mode"] = "access" if r["vlan_mode"] == "access" else "tagged"
        if self.schema.has_table("PortNativeVLAN"):
            for r in self.conn.query("SELECT object_id, port_name, vlan_id FROM PortNativeVLAN"):
                dom = switch_domain.get(int(r["object_id"]))
                if dom is None:
                    continue
                rec = rec_for(int(r["object_id"]), r["port_name"])
                rec["untagged_ref"] = rt_key("vlan", f"{dom}-{int(r['vlan_id'])}")
        if self.schema.has_table("PortAllowedVLAN"):
            for r in self.conn.query("SELECT object_id, port_name, vlan_id FROM PortAllowedVLAN"):
                dom = switch_domain.get(int(r["object_id"]))
                if dom is None:
                    continue
                rec = rec_for(int(r["object_id"]), r["port_name"])
                rec["tagged_refs"].append(rt_key("vlan", f"{dom}-{int(r['vlan_id'])}"))

        recs = list(config.values())
        if self._want("vlans"):
            self._dump("interface_vlans", recs)

    # -- tags ---------------------------------------------------------------

    def _export_tags(self) -> None:
        # Emit every tag record implied by the whole tag tree so import can
        # create them up front (assignments reference them by slug).
        strategy = self.mapping.get("tag_strategy", "flatten_path")
        seen: Dict[str, Dict[str, str]] = {}
        for tid in self.tag_by_id:
            path = self._tag_path(tid)
            for rec in tag_records_from_path(path, strategy):
                seen.setdefault(rec["slug"], {
                    "rt_key": f"tag:{rec['slug']}", "source_table": "TagTree",
                    "source_id": rec["slug"], "name": rec["name"][:100],
                    "slug": rec["slug"],
                })
        recs = list(seen.values())
        if self._want("tags"):
            self.summary.stat("tags").source = len(recs)
            self._dump("tags", recs)

    # -- meta ---------------------------------------------------------------

    def _write_meta(self) -> None:
        meta = {
            "tool_version": __import__("rt2nb").__version__,
            "categories": sorted(self.categories),
            "row_as": self.mapping.get("row_as"),
            "tag_strategy": self.mapping.get("tag_strategy"),
            "rt_tables_present": sorted(self.schema.tables),
            "object_count": len(self.objects),
        }
        with open(os.path.join(self.export_dir, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, ensure_ascii=False, default=str)


def _safe_dns(name: Optional[str]) -> str:
    """RackTables 'name' is free text; only keep it as dns_name if hostname-ish."""
    import re as _re
    if not name:
        return ""
    n = name.strip()
    if _re.fullmatch(r"[A-Za-z0-9._-]{1,255}", n) and "." in n:
        return n
    return ""
