"""Device import must never be lost to a NetBox rack-position rejection."""

from rt2nb.importer.importer import Importer
from rt2nb.importer.state import Resolver, Upserter
from rt2nb.netbox.client import NetBoxError
from rt2nb.report import Summary, UnmigratedReport


class PosFakeNB:
    """Rejects any create that includes a rack position (simulating overlap)."""

    def __init__(self):
        self.created = []
        self._id = 0

    def get_one(self, endpoint, params):
        return None

    def get_all(self, endpoint, params=None):
        return []

    def create(self, endpoint, data):
        if "position" in data:
            raise NetBoxError('400 POST .../dcim/devices/: '
                              '{"position":["U29.0 is already occupied ..."]}')
        self._id += 1
        self.created.append(data)
        return {**data, "id": self._id}

    def update(self, endpoint, obj_id, data):
        return {"id": obj_id, **data}


def _importer(nb):
    imp = Importer.__new__(Importer)
    imp.nb = nb
    imp.resolver = Resolver(nb)
    imp.up = Upserter(nb, imp.resolver, dry_run=False)
    imp.summary = Summary()
    imp.unmig = UnmigratedReport()
    imp.dry_run = False
    return imp


def _desired():
    return {
        "name": "sw1", "device_type": 5, "role": 1, "site": 2, "status": "active",
        "rack": 9, "position": 29, "face": "front", "custom_fields": {}, "tags": [],
    }


class TestDeviceFallback:
    def test_position_conflict_retries_unplaced(self):
        nb = PosFakeNB()
        imp = _importer(nb)
        stat = imp.summary.stat("devices")
        imp._upsert_device(stat, {"rt_key": "object:1", "source_id": 1, "name": "sw1"},
                           _desired(), None)
        # device was still created, just without a position
        assert stat.created == 1 and stat.failed == 0
        assert "position" not in nb.created[0] and "face" not in nb.created[0]
        assert nb.created[0]["rack"] == 9          # rack assignment kept
        assert len(imp.unmig) == 1                 # placement recorded, not dropped

    def test_asset_tag_conflict_dropped(self):
        class AssetNB(PosFakeNB):
            def create(self, endpoint, data):
                if data.get("asset_tag"):
                    raise NetBoxError('400 POST .../dcim/devices/: '
                                      '{"asset_tag":["device with this asset tag already exists."]}')
                self._id += 1
                self.created.append(data)
                return {**data, "id": self._id}
        nb = AssetNB()
        imp = _importer(nb)
        stat = imp.summary.stat("devices")
        desired = _desired()
        desired.pop("position"); desired.pop("face")   # isolate the asset_tag conflict
        desired["asset_tag"] = "AT1"
        imp._upsert_device(stat, {"rt_key": "object:3", "source_id": 3, "name": "sw3"},
                           desired, None)
        assert stat.created == 1 and stat.failed == 0
        assert not nb.created[0].get("asset_tag")       # tag dropped
        assert len(imp.unmig) == 1                      # recorded

    def test_placed_device_succeeds_without_fallback(self):
        class OkNB(PosFakeNB):
            def create(self, endpoint, data):
                self._id += 1
                self.created.append(data)
                return {**data, "id": self._id}
        nb = OkNB()
        imp = _importer(nb)
        stat = imp.summary.stat("devices")
        imp._upsert_device(stat, {"rt_key": "object:2", "source_id": 2, "name": "sw2"},
                           _desired(), None)
        assert stat.created == 1 and len(imp.unmig) == 0
        assert nb.created[0]["position"] == 29     # placement kept when accepted
