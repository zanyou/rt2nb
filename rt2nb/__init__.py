"""RackTables 0.20.x -> NetBox 4.6 migration tool.

Two-phase design:
  * ``export``  reads the RackTables MySQL database (SELECT only) and writes
    intermediate JSON to ``paths.export_dir``.
  * ``import``  reads that JSON and creates/updates objects in NetBox via the
    REST API, idempotently.
  * ``verify``  counts both sides per category and reports the delta.

Nothing in this package ever issues a write statement against RackTables.
"""

__version__ = "1.0.0"
