"""A duplicate source CIDR must be recorded, not hard-fail the run."""

from rt2nb.importer.importer import Importer
from rt2nb.importer.state import Resolver, Upserter
from rt2nb.netbox.client import NetBoxError
from rt2nb.report import Summary, UnmigratedReport


class DupPrefixNB:
    def get_one(self, endpoint, params):
        return None

    def get_all(self, endpoint, params=None):
        return []

    def create(self, endpoint, data):
        raise NetBoxError('400 POST .../ipam/prefixes/: '
                          '{"prefix":["Duplicate prefix found in global table: 10.2.1.0/24"]}')

    def update(self, endpoint, obj_id, data):
        return {"id": obj_id, **data}


def _importer(nb, records):
    imp = Importer.__new__(Importer)
    imp.nb = nb
    imp.resolver = Resolver(nb)
    imp.up = Upserter(nb, imp.resolver, dry_run=False)
    imp.summary = Summary()
    imp.unmig = UnmigratedReport()
    imp.dry_run = False
    imp._load = lambda name: records
    return imp


def test_duplicate_prefix_recorded_not_failed():
    rec = {"rt_key": "ipv4net:3", "source_table": "ipv4net", "source_id": 3,
           "prefix": "10.2.1.0/24", "description": "", "comments": ""}
    imp = _importer(DupPrefixNB(), [rec])
    imp._import_prefixes()
    stat = imp.summary.stat("prefixes")
    assert stat.skipped == 1
    assert stat.failed == 0
    assert len(imp.unmig) == 1


def test_non_duplicate_prefix_error_still_fails():
    class BadNB(DupPrefixNB):
        def create(self, endpoint, data):
            raise NetBoxError('400 POST .../ipam/prefixes/: {"prefix":["Invalid IP"]}')
    rec = {"rt_key": "ipv4net:9", "source_table": "ipv4net", "source_id": 9,
           "prefix": "999.0.0.0/24", "description": "", "comments": ""}
    imp = _importer(BadNB(), [rec])
    imp._import_prefixes()
    stat = imp.summary.stat("prefixes")
    assert stat.failed == 1 and stat.skipped == 0
