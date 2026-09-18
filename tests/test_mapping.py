from rt2nb.mapping.normalize import slugify, rt_key, normalize_mac, clean_name, cf_name
from rt2nb.mapping.transforms import (
    tag_records_from_path,
    ip_allocation_role,
    rack_position_and_face,
    parse_hw_type,
    resolve_device_type,
    strip_dict_markup,
)
from rt2nb.mapping.ipaddr import (
    ipv4_int_to_str,
    ipv6_bytes_to_str,
    ipv4_cidr,
    ipv6_cidr,
    with_prefixlen,
)
from rt2nb.constants import classify_objtype


class TestSlug:
    def test_basic(self):
        assert slugify("Rack A-01") == "rack-a-01"

    def test_unicode_transliterates_or_drops(self):
        assert slugify("東京 DC") == "dc"

    def test_empty_falls_back(self):
        assert slugify("") == "x"
        assert slugify("!!!") == "x"

    def test_rt_key_namespaced(self):
        assert rt_key("object", 42) == "object:42"
        assert rt_key("ipv4net", 5) == "ipv4net:5"


class TestCfName:
    def test_valid_chars_only(self):
        assert cf_name("OEM S/N 2") == "oem_s_n_2"

    def test_leading_digit_prefixed(self):
        assert cf_name("3D model").startswith("attr_")

    def test_empty_falls_back(self):
        assert cf_name("") == "attr"
        assert cf_name("!!!") == "attr"


class TestMac:
    def test_plain_hex(self):
        assert normalize_mac("001122334455") == "00:11:22:33:44:55"

    def test_already_separated(self):
        assert normalize_mac("00:11:22:33:44:55") == "00:11:22:33:44:55"

    def test_invalid(self):
        assert normalize_mac("zzzz") is None
        assert normalize_mac("") is None


class TestTags:
    def test_flatten_path_encodes_full_path(self):
        recs = tag_records_from_path(["Owner", "TeamA"], "flatten_path")
        assert recs == [{"name": "TeamA", "slug": "owner-teama"}]

    def test_leaf_only(self):
        recs = tag_records_from_path(["Owner", "TeamA"], "leaf")
        assert recs == [{"name": "TeamA", "slug": "teama"}]

    def test_explode_one_per_level(self):
        recs = tag_records_from_path(["Owner", "TeamA"], "explode")
        assert [r["slug"] for r in recs] == ["owner", "owner-teama"]

    def test_distinct_paths_same_leaf_do_not_collide(self):
        a = tag_records_from_path(["X", "Web"], "flatten_path")[0]["slug"]
        b = tag_records_from_path(["Y", "Web"], "flatten_path")[0]["slug"]
        assert a != b


class TestIpAllocationRole:
    def test_known(self):
        assert ip_allocation_role("virtual")[0] == "vip"
        assert ip_allocation_role("shared")[0] == "anycast"
        assert ip_allocation_role("regular")[0] is None

    def test_unknown_kept_in_note(self):
        role, note = ip_allocation_role("weird")
        assert role is None and note == "weird"


class TestRackGeometry:
    def test_bottom_unit_is_position(self):
        pos, face, problems = rack_position_and_face([(10, "front"), (11, "front"), (12, "front")])
        assert pos == 10 and face == "front" and problems == []

    def test_rear_face(self):
        pos, face, _ = rack_position_and_face([(5, "rear")])
        assert face == "rear" and pos == 5

    def test_full_depth_reports_front(self):
        _, face, _ = rack_position_and_face([(5, "front"), (5, "rear")])
        assert face == "front"

    def test_noncontiguous_flagged(self):
        pos, _, problems = rack_position_and_face([(1, "front"), (3, "front")])
        assert pos == 1 and problems


class TestHwType:
    def test_known_vendor(self):
        assert parse_hw_type("Dell PowerEdge R620") == ("Dell", "PowerEdge R620")

    def test_markup_stripped(self):
        assert strip_dict_markup("%GSKIP%Cisco 2960") == "Cisco 2960"

    def test_wiki_link_label_kept_url_dropped(self):
        assert strip_dict_markup("[[EX 2200-48T-4G | http://juniper.net/x]]") == "EX 2200-48T-4G"

    def test_unknown_returns_model_only(self):
        mfr, model = parse_hw_type("MysteryBox")
        assert mfr is None and model == "MysteryBox"

    def test_empty(self):
        assert parse_hw_type("") == (None, None)


class TestResolveDeviceType:
    def test_hw_type_wins(self):
        mfr, model, hw_typed = resolve_device_type(
            "Dell PowerEdge R620", "Server", "Unknown", "Unknown Device Type")
        assert (mfr, model, hw_typed) == ("Dell", "PowerEdge R620", True)

    def test_falls_back_to_objtype_label(self):
        # No HW type -> group by what the object IS, not a generic placeholder.
        mfr, model, hw_typed = resolve_device_type(
            None, "CableOrganizer", "Unknown", "Unknown Device Type")
        assert (mfr, model, hw_typed) == ("Unknown", "CableOrganizer", False)

    def test_falls_back_to_placeholder_when_no_objtype(self):
        mfr, model, hw_typed = resolve_device_type(
            "", "", "Unknown", "Unknown Device Type")
        assert (mfr, model, hw_typed) == ("Unknown", "Unknown Device Type", False)

    def test_multipurpose_bag_and_shelf_get_distinct_models(self):
        a = resolve_device_type(None, "Multi-purpose Bag", "Unknown", "Unknown Device Type")[1]
        b = resolve_device_type(None, "Shelf", "Unknown", "Unknown Device Type")[1]
        assert a == "Multi-purpose Bag" and b == "Shelf" and a != b


class TestIpConv:
    def test_v4_roundtrip(self):
        assert ipv4_int_to_str(0xC0A80001) == "192.168.0.1"

    def test_v4_signed_masked(self):
        # a value that would be negative as signed int32
        assert ipv4_int_to_str(-1) == "255.255.255.255"

    def test_v4_cidr(self):
        assert ipv4_cidr(0xC0A80005, 24) == "192.168.0.0/24"

    def test_v6(self):
        packed = bytes.fromhex("20010db8000000000000000000000001")
        assert ipv6_bytes_to_str(packed) == "2001:db8::1"

    def test_v6_none_and_bad_length(self):
        assert ipv6_bytes_to_str(None) is None
        assert ipv6_bytes_to_str(b"\x00\x01") is None   # not 16 bytes

    def test_v4_cidr_edge_masks(self):
        assert ipv4_cidr(0x0A000001, 32) == "10.0.0.1/32"
        assert ipv4_cidr(0x0A000001, 0) == "0.0.0.0/0"    # host bits cleared

    def test_v6_cidr(self):
        packed = bytes.fromhex("20010db8000000000000000000000001")
        assert ipv6_cidr(packed, 64) == "2001:db8::/64"

    def test_with_prefixlen(self):
        assert with_prefixlen("10.0.0.1", 24) == "10.0.0.1/24"
        assert with_prefixlen("2001:db8::1", 128) == "2001:db8::1/128"


class TestClassify:
    def test_rack_row_location(self):
        assert classify_objtype("Rack") == "rack"
        assert classify_objtype("Row") == "row"
        assert classify_objtype("Location") == "location"

    def test_vm_and_cluster(self):
        assert classify_objtype("VM") == "vm"
        assert classify_objtype("VM Cluster") == "cluster"

    def test_default_device(self):
        assert classify_objtype("Network switch") == "device"
        assert classify_objtype("Server") == "device"
