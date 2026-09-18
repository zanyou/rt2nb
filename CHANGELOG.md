# Changelog

All notable changes to rt2nb are documented in this file. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/).

## 1.0.0

First public release. Migrates RackTables 0.20.x into NetBox 4.6 in three
phases with idempotent, re-runnable imports.

Verified against NetBox v4.6.8 (deployed via netbox-docker, PostgreSQL 18),
RackTables 0.20.x on MySQL/MariaDB, Docker (image built on `python:3.11-slim`),
and Python 3.11 in the image.

Capabilities:

- **`export`** — read-only (SELECT-only) extraction of RackTables into
  intermediate JSON under `export_data/`, adapting to the live schema.
- **`import`** — upserts the JSON into NetBox via the REST API with
  create/update/skip semantics; supports `--dry-run`.
- **`verify`** — reconciles the exported JSON against NetBox per category, with a
  CI-friendly exit code (`0` when complete, `2` when records are missing).
- **`schema`** — prints the live RackTables table list as a connectivity check.
- **Idempotency** via a `racktables_id` custom field and a `from-racktables`
  tag, with natural-key fallback so hand-created objects are adopted rather than
  duplicated.
- **Category filtering** with `--only` / `--skip`.
- **Configurable mapping**: RackTables Location → Site / Row → Location
  (`row_as`), tag path flattening (`tag_strategy`), placeholder device types,
  interface-type overrides, and attribute overrides.
- **Meaningful device types for unmodeled objects**: a device with no HW-type
  attribute takes its device-type model from the RackTables object-type label
  (CableOrganizer, Shelf, Server, …) instead of a single "Unknown Device Type".
- **Device/VM roles from object type**: each device and VM gets a NetBox role
  named after its RackTables object type (Server, Network switch, Router, PDU,
  …), created on demand, instead of a single generic role.
- **IP ↔ machine binding preserved**: each address is attached to the device/VM
  interface it belongs to. NIC and IPMI interfaces implied by RackTables IP
  allocations that have no physical `Port` are created automatically so the
  machine↔IP link is not lost.
- **Nothing dropped silently**: unrepresentable data is written to
  `export_data/unmigrated_report.json` (export-time) and
  `export_data/unmigrated_import_report.json` (import-time).
- **Config via YAML or environment variables** (`RT2NB_<SECTION>__<KEY>`),
  keeping secrets out of `config.yml`.
- Ships as a Docker image; also runs natively on Python 3.7+.
