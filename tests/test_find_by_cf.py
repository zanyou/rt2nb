"""find_by_cf must return the EXACT racktables_id match, not a partial one.

NetBox filters text custom fields with icontains, so a query for
``devicetype:dell-poweredge-r730`` also returns ``...-r730xd``. This regression
guard reproduces that behaviour and asserts we pick the exact object.
"""

from rt2nb.importer.state import find_by_cf


class IcontainsNB:
    """Mimics NetBox: cf filter returns every object whose cf CONTAINS query."""

    OBJS = [
        {"id": 1, "custom_fields": {"racktables_id": "devicetype:dell-poweredge-r730"}},
        {"id": 2, "custom_fields": {"racktables_id": "devicetype:dell-poweredge-r730xd"}},
        {"id": 3, "custom_fields": {"racktables_id": "object:17"}},
        {"id": 4, "custom_fields": {"racktables_id": "object:175"}},
    ]

    def get_all(self, endpoint, params=None):
        q = (params or {}).get("cf_racktables_id", "")
        return [o for o in self.OBJS if q in o["custom_fields"]["racktables_id"]]


def test_exact_match_amid_prefix_siblings():
    nb = IcontainsNB()
    assert find_by_cf(nb, "dcim/device-types", "devicetype:dell-poweredge-r730")["id"] == 1
    assert find_by_cf(nb, "dcim/device-types", "devicetype:dell-poweredge-r730xd")["id"] == 2


def test_exact_match_numeric_prefix():
    nb = IcontainsNB()
    assert find_by_cf(nb, "dcim/devices", "object:17")["id"] == 3
    assert find_by_cf(nb, "dcim/devices", "object:175")["id"] == 4


def test_no_match_returns_none():
    nb = IcontainsNB()
    assert find_by_cf(nb, "dcim/devices", "object:999") is None
