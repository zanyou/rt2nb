from rt2nb.importer.state import compute_patch, Upserter, Resolver, CREATED, UPDATED, SKIPPED
from rt2nb.mapping.interface_types import map_interface_type


class TestComputePatch:
    def test_no_change_is_empty(self):
        existing = {"name": "sw1", "slug": "sw1", "custom_fields": {"racktables_id": "object:1"}}
        desired = {"name": "sw1", "slug": "sw1"}
        assert compute_patch(existing, desired) == {}

    def test_scalar_change(self):
        existing = {"name": "old"}
        desired = {"name": "new"}
        assert compute_patch(existing, desired) == {"name": "new"}

    def test_related_field_compared_by_id(self):
        existing = {"site": {"id": 7, "name": "S"}}
        assert compute_patch(existing, {"site": 7}) == {}
        assert compute_patch(existing, {"site": 8}) == {"site": 8}

    def test_custom_fields_merge_only_changed(self):
        existing = {"custom_fields": {"racktables_id": "object:1", "hw_type": "old"}}
        patch = compute_patch(existing, {"custom_fields": {"hw_type": "new"}})
        assert patch["custom_fields"]["hw_type"] == "new"
        assert patch["custom_fields"]["racktables_id"] == "object:1"

    def test_custom_fields_same_is_skipped(self):
        existing = {"custom_fields": {"racktables_id": "object:1"}}
        assert compute_patch(existing, {"custom_fields": {"racktables_id": "object:1"}}) == {}

    def test_tags_only_added_when_missing(self):
        existing = {"tags": [{"slug": "from-racktables"}]}
        assert compute_patch(existing, {"tags": [{"slug": "from-racktables"}]}) == {}
        patch = compute_patch(existing, {"tags": [{"slug": "owner-team"}]})
        assert {t["slug"] for t in patch["tags"]} == {"from-racktables", "owner-team"}

    def test_none_and_empty_string_equivalent(self):
        existing = {"serial": None}
        assert compute_patch(existing, {"serial": ""}) == {}

    def test_choice_field_compared_by_value(self):
        # NetBox returns status/face as {value,label} on read.
        existing = {"status": {"value": "active", "label": "Active"}}
        assert compute_patch(existing, {"status": "active"}) == {}
        assert compute_patch(existing, {"status": "offline"}) == {"status": "offline"}

    def test_m2m_id_list_compared_as_set(self):
        existing = {"tagged_vlans": [{"id": 3}, {"id": 1}]}
        assert compute_patch(existing, {"tagged_vlans": [1, 3]}) == {}
        assert compute_patch(existing, {"tagged_vlans": [1, 2]}) == {"tagged_vlans": [1, 2]}

    def test_numeric_custom_field_returned_as_string_is_stable(self):
        # NetBox stores text custom fields as strings, but RackTables uint/float
        # attributes (e.g. "CPU, MHz") export as numbers. int 5 vs str "5" must
        # NOT churn a patch on every re-run.
        existing = {"custom_fields": {"racktables_id": "object:1", "cpu_mhz": "2400"}}
        assert compute_patch(existing, {"custom_fields": {"cpu_mhz": 2400}}) == {}
        # a float that reads back as its string form is likewise stable
        existing = {"custom_fields": {"flash_mb": "5.0"}}
        assert compute_patch(existing, {"custom_fields": {"flash_mb": 5.0}}) == {}

    def test_numeric_custom_field_real_change_still_patches(self):
        existing = {"custom_fields": {"racktables_id": "object:1", "cpu_mhz": "2400"}}
        patch = compute_patch(existing, {"custom_fields": {"cpu_mhz": 3200}})
        assert patch["custom_fields"]["cpu_mhz"] == 3200
        assert patch["custom_fields"]["racktables_id"] == "object:1"

    def test_unset_custom_field_none_vs_empty_is_stable(self):
        existing = {"custom_fields": {"racktables_id": "object:1", "sw_type": None}}
        assert compute_patch(existing, {"custom_fields": {"sw_type": ""}}) == {}


class FakeNB:
    """Minimal NetBox stand-in recording calls and serving canned lookups."""

    def __init__(self, existing_by_cf=None):
        self.existing_by_cf = existing_by_cf or {}
        self.created = []
        self.updated = []
        self._next_id = 1000

    def get_one(self, endpoint, params):
        cf = params.get("cf_racktables_id")
        if cf and cf in self.existing_by_cf:
            return self.existing_by_cf[cf]
        return None

    def get_all(self, endpoint, params=None):
        params = params or {}
        cf = params.get("cf_racktables_id")
        if cf and cf in self.existing_by_cf:
            return [self.existing_by_cf[cf]]
        return []

    def create(self, endpoint, data):
        self._next_id += 1
        obj = dict(data)
        obj["id"] = self._next_id
        self.created.append((endpoint, data))
        # register for later lookups by cf
        cf = (data.get("custom_fields") or {}).get("racktables_id")
        if cf:
            self.existing_by_cf[cf] = obj
        return obj

    def update(self, endpoint, obj_id, data):
        self.updated.append((endpoint, obj_id, data))
        return {"id": obj_id, **data}


class TestUpserter:
    def test_create_when_absent(self):
        nb = FakeNB()
        up = Upserter(nb, Resolver(nb), dry_run=False)
        _id, action = up.upsert("dcim/sites", "object:1", {"name": "S", "slug": "s"}, {"slug": "s"})
        assert action == CREATED
        assert nb.created and not nb.updated
        # racktables_id + migration tag were stamped
        _, data = nb.created[0]
        assert data["custom_fields"]["racktables_id"] == "object:1"
        assert any(t["slug"] == "from-racktables" for t in data["tags"])

    def test_second_run_skips(self):
        nb = FakeNB()
        up = Upserter(nb, Resolver(nb), dry_run=False)
        up.upsert("dcim/sites", "object:1", {"name": "S", "slug": "s"}, {"slug": "s"})
        # second identical run: object now exists, no diff -> SKIPPED
        _id, action = up.upsert("dcim/sites", "object:1", {"name": "S", "slug": "s"}, {"slug": "s"})
        assert action == SKIPPED
        assert len(nb.updated) == 0

    def test_update_on_diff(self):
        existing = {"id": 5, "name": "old", "slug": "s",
                    "custom_fields": {"racktables_id": "object:1"},
                    "tags": [{"slug": "from-racktables"}]}
        nb = FakeNB({"object:1": existing})
        up = Upserter(nb, Resolver(nb), dry_run=False)
        _id, action = up.upsert("dcim/sites", "object:1", {"name": "new", "slug": "s"})
        assert action == UPDATED and nb.updated[0][2]["name"] == "new"

    def test_dry_run_writes_nothing(self):
        nb = FakeNB()
        up = Upserter(nb, Resolver(nb), dry_run=True)
        _id, action = up.upsert("dcim/sites", "object:1", {"name": "S", "slug": "s"})
        assert action == CREATED and not nb.created and not nb.updated

    def test_shared_ip_creates_one_object_per_binding(self):
        # A-plan: same address, two racktables_id keys, no natural key ->
        # two distinct IPAddress objects, and a re-run skips both.
        nb = FakeNB()
        up = Upserter(nb, Resolver(nb), dry_run=False)
        for key in ("ipv4alloc:167772161:103:eth0", "ipv4alloc:167772161:104:eth0"):
            _id, action = up.upsert("ipam/ip-addresses", key,
                                    {"address": "10.0.0.1/24", "role": "anycast"}, None)
            assert action == CREATED
        assert len(nb.created) == 2
        # second run: both already exist by their distinct racktables_id
        for key in ("ipv4alloc:167772161:103:eth0", "ipv4alloc:167772161:104:eth0"):
            _id, action = up.upsert("ipam/ip-addresses", key,
                                    {"address": "10.0.0.1/24", "role": "anycast"}, None)
            assert action == SKIPPED
        assert len(nb.created) == 2 and len(nb.updated) == 0


class TestInterfaceTypes:
    def test_copper(self):
        assert map_interface_type("1000Base-T", None)[0] == "1000base-t"
        assert map_interface_type("10GBase-T", None)[0] == "10gbase-t"

    def test_optics(self):
        assert map_interface_type("10GBase-SR", None)[0] == "10gbase-x-sfpp"
        assert map_interface_type("SFP+", None)[0] == "10gbase-x-sfpp"

    def test_unknown_falls_back_to_other(self):
        t, fb = map_interface_type("wibble", None)
        assert t == "other" and fb is True

    def test_override_wins(self):
        t, fb = map_interface_type("1000Base-T", None, {"1000Base-T": "other"})
        assert t == "other" and fb is False

    def test_inner_fallback(self):
        t, fb = map_interface_type(None, "SFP+")
        assert t == "10gbase-x-sfpp" and fb is False

    def test_recognised_other_outer_uses_inner(self):
        # "hardwired" outer maps to other (recognised, no warn) but the inner
        # SFP+ should win and resolve a real media type.
        t, fb = map_interface_type("hardwired", "SFP+")
        assert t == "10gbase-x-sfpp" and fb is False

    def test_recognised_other_with_no_inner_is_fallback(self):
        # "hardwired" alone: recognised -> other, still flagged as fallback.
        t, fb = map_interface_type("hardwired", None)
        assert t == "other" and fb is True

    def test_high_speed_media(self):
        assert map_interface_type("100GBase-CR4", None)[0] == "100gbase-x-qsfp28"
        assert map_interface_type("40GBase-SR4", None)[0] == "40gbase-x-qsfpp"

    def test_override_matches_lowercased_label(self):
        # overrides are matched case-insensitively too.
        t, fb = map_interface_type("Weird-Media", None, {"weird-media": "other"})
        assert t == "other" and fb is False
