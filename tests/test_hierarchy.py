"""Hierarchy resolution: Location -> Row -> Rack -> Device via EntityLink.

Regression guard for the case where RackTables expresses the physical
hierarchy with typed EntityLink realms (location/row/rack) rather than the
generic 'object' realm, so the child->parent map must span all of them.
"""

from rt2nb.export.exporter import Exporter, _CONTAINMENT_REALMS


def _exporter(row_as="location"):
    ex = Exporter.__new__(Exporter)
    ex.mapping = {"row_as": row_as}
    ex.default_site_key = "default:site"
    # 1=Location, 2=Row, 3=Rack, 4=Device mounted in the rack.
    ex.obj_class = {1: "location", 2: "row", 3: "rack", 4: "device"}
    # location(1) -> row(2) -> rack(3) -> device(4)
    ex.child_to_parent = {2: 1, 3: 2, 4: 3}
    return ex


class TestContainmentRealms:
    def test_typed_realms_included(self):
        # The realms this install actually uses must all be recognised.
        assert {"rack", "row", "location", "object"} <= _CONTAINMENT_REALMS


class TestSiteResolution:
    def test_rack_resolves_to_location_as_site(self):
        ex = _exporter("location")
        assert ex._site_key_for(3) == "object:1"

    def test_device_resolves_up_through_rack_and_row(self):
        ex = _exporter("location")
        assert ex._site_key_for(4) == "object:1"

    def test_row_as_site_uses_row(self):
        ex = _exporter("site")
        # row_as=site: the nearest Row becomes the Site.
        assert ex._site_key_for(3) == "object:2"

    def test_orphan_falls_back_to_default(self):
        ex = _exporter("location")
        ex.child_to_parent = {}  # no links
        assert ex._site_key_for(3) == "default:site"
