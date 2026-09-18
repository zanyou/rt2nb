"""Device/VM role is derived from the RackTables object-type label."""

from rt2nb.importer.importer import Importer


class RoleNB:
    def __init__(self):
        self.created = []
        self._id = 0

    def get_one(self, endpoint, params):
        return None

    def create(self, endpoint, data):
        self._id += 1
        self.created.append((endpoint, data))
        return {"id": self._id, **data}


def _importer(nb):
    imp = Importer.__new__(Importer)
    imp.nb = nb
    imp.dry_run = False
    imp._role_ids = {}
    imp._default_role_id = 1
    return imp


class TestEnsureRole:
    def test_creates_role_from_objtype_and_caches(self):
        nb = RoleNB()
        imp = _importer(nb)
        r1 = imp._ensure_role("Network switch")
        r2 = imp._ensure_role("Network switch")   # cached -> no second create
        assert r1 == r2
        assert len(nb.created) == 1
        ep, data = nb.created[0]
        assert ep == "dcim/device-roles"
        assert data["name"] == "Network switch"
        assert data["slug"] == "network-switch"
        assert data["vm_role"] is True            # usable for VMs too

    def test_distinct_objtypes_get_distinct_roles(self):
        nb = RoleNB()
        imp = _importer(nb)
        imp._ensure_role("Server")
        imp._ensure_role("PDU")
        slugs = {d["slug"] for _ep, d in nb.created}
        assert slugs == {"server", "pdu"}

    def test_empty_falls_back_to_default(self):
        nb = RoleNB()
        imp = _importer(nb)
        assert imp._ensure_role("") == 1
        assert imp._ensure_role(None) == 1
        assert nb.created == []                    # no role created for empty
