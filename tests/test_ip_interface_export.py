"""IP allocations must yield interfaces so machine<->IP binding is preserved.

RackTables binds a machine's IPs (NIC names, IPMI) to the object under a
free-text interface name that often has no Port row. The exporter must emit a
synthetic interface for each such (object, name) not already covered by a Port,
so the IP import can attach the address to the device.
"""

from rt2nb.export.exporter import Exporter
from rt2nb.report import Summary, UnmigratedReport


class FakeSchema:
    def has_table(self, t):
        return t in {"Port", "PortOuterInterface", "PortInnerInterface",
                     "IPv4Allocation", "IPv6Allocation"}

    def present_columns(self, table, candidates):
        return list(candidates)


class FakeConn:
    def query(self, sql, params=None):
        if "PortOuterInterface" in sql or "PortInnerInterface" in sql:
            return []
        if "IPv4Allocation" in sql:
            return [
                {"object_id": 10, "name": "eth0"},    # no Port -> synthetic
                {"object_id": 10, "name": "Gi0/0"},   # matches Port -> reuse
                {"object_id": 10, "name": "ipmi"},    # IPMI -> synthetic
                {"object_id": 99, "name": "eth0"},    # object 99 is a rack -> skip
            ]
        if "IPv6Allocation" in sql:
            return []
        if "FROM Port" in sql:
            return [{"id": 1, "object_id": 10, "name": "Gi0/0", "iif_id": 0,
                     "oif_id": 0, "l2address": None, "label": "",
                     "reservation_comment": ""}]
        return []


def _exporter():
    ex = Exporter.__new__(Exporter)
    ex.conn = FakeConn()
    ex.schema = FakeSchema()
    ex.mapping = {"interface_type_overrides": {}}
    ex.obj_class = {10: "device", 99: "rack"}
    ex.unmig = UnmigratedReport()
    ex.summary = Summary()
    ex.categories = {"interfaces"}
    ex._captured = None
    ex._dump = lambda name, recs: setattr(ex, "_captured", recs)
    return ex


def test_synthetic_interfaces_for_ip_allocations():
    ex = _exporter()
    ex._export_interfaces()
    ifaces = ex._captured
    by_name = {i["name"]: i for i in ifaces}

    # Physical port kept as-is.
    assert by_name["Gi0/0"]["source_table"] == "Port"
    # Allocation names without a Port become synthetic L3 interfaces.
    assert by_name["eth0"]["source_table"] == "IPv4Allocation"
    assert by_name["eth0"]["rt_key"] == "ipiface:10:eth0"
    assert by_name["eth0"]["parent_ref"] == "object:10"
    assert by_name["ipmi"]["rt_key"] == "ipiface:10:ipmi"
    # The allocation that matches a Port name did NOT create a duplicate.
    assert sum(1 for i in ifaces if i["name"] == "Gi0/0") == 1
    # Allocation on a non-device object (a rack) is ignored.
    assert all(i["parent_ref"] != "object:99" for i in ifaces)


def test_ip_allocation_interface_matches_ip_assignment_name():
    # The synthetic interface name must equal what the IP export references as
    # the assignment interface_name (both clean_name of the allocation name),
    # so the importer's (object_ref, interface_name) lookup binds them.
    ex = _exporter()
    ex._export_interfaces()
    names = {i["name"] for i in ex._captured if i["parent_ref"] == "object:10"}
    assert {"Gi0/0", "eth0", "ipmi"} <= names
