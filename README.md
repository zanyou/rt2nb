# rt2nb — RackTables → NetBox migration tool

A two-phase, idempotent, dry-run-capable tool that migrates a **RackTables
0.20.x** MySQL/MariaDB database into **NetBox 4.6** via the REST API. It reads
RackTables strictly **read-only**, writes an intermediate JSON snapshot you can
review, and then upserts that snapshot into NetBox so you can run it as many
times as you like without creating duplicates.

- **`export`** — SELECT-only queries against RackTables produce intermediate
  JSON. RackTables is never written to.
- **`import`** — the intermediate JSON is created/updated/skipped in NetBox.
  Safe to re-run; supports `--dry-run`.
- **`verify`** — reconciles the exported JSON against what is actually in
  NetBox, per category, with a CI-friendly exit code.

## Table of contents

- [Tested with](#tested-with)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start (Docker)](#quick-start-docker)
- [Running natively](#running-natively)
- [Commands and options](#commands-and-options)
- [Mapping overview](#mapping-overview)
- [Design decisions](#design-decisions)
- [Idempotency model](#idempotency-model)
- [Nothing is dropped silently](#nothing-is-dropped-silently)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Development and tests](#development-and-tests)
- [Safety: read-only against RackTables](#safety-read-only-against-racktables)
- [License](#license)

## Tested with

This release was verified end-to-end against the following concrete
combination:

| Component | Version |
|---|---|
| rt2nb | 1.0.0 |
| NetBox | v4.6.8 (deployed via [netbox-docker](https://github.com/netbox-community/netbox-docker), PostgreSQL 18) |
| RackTables | 0.20.x on MySQL / MariaDB |
| Runtime | Docker (image built on `python:3.11-slim`) |
| Python | 3.11 (inside the image) |

Other NetBox 4.x releases and other Docker hosts are expected to work but were
not part of this verified run.

## Features

- **Idempotent upserts.** Every migrated object is stamped with a
  `racktables_id` custom field and a `from-racktables` tag. Re-running `import`
  matches on those first (then on a natural key), so a second run is all skips
  and hand-created objects are adopted rather than duplicated.
- **Read-only source.** The only module that touches RackTables refuses any
  statement that is not a bare `SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN`, and marks
  the session `READ ONLY`.
- **Dry-run imports.** `import --dry-run` reports the create/update/skip counts
  without writing anything to NetBox.
- **Category filtering.** `--only` / `--skip` restrict a run to specific
  object categories (sites, racks, devices, cables, prefixes, …).
- **Nothing dropped silently.** Anything that cannot be represented in NetBox
  is recorded — with source table, source id, and a reason — in an unmigrated
  report, and where possible its detail is preserved in the object's comments.
- **Schema-adaptive export.** The export phase introspects the live RackTables
  schema and adapts to the columns actually present rather than trusting a
  fixed schema dump.

## How it works

```
RackTables (MySQL/MariaDB)          NetBox 4.6 (REST API)
        │                                   ▲
        │  export (SELECT only)             │  import (create/update/skip)
        ▼                                   │
   export_data/*.json  ───────────────────►┘
        ▲
        └──────── verify (reconcile JSON vs NetBox)
```

The `export_data` directory is the hand-off between phases: `export` writes it,
`import` and `verify` read it. Because state lives in JSON on disk, you can
review and even edit the snapshot before importing, and the two network-facing
phases never need to reach both systems at once.

## Requirements

- **Docker** on any host (recommended path), or **Python 3.7+** for a native
  install (see below).
- Network reachability from wherever rt2nb runs to **both** the RackTables
  MySQL/MariaDB port and the NetBox HTTP(S) endpoint.
- A **read-only** MySQL/MariaDB account for RackTables (defence in depth on top
  of the tool's own read-only guard).
- A NetBox **API token** with write permission, plus permission to create
  custom fields, tags, device roles, and cluster types (used once on the first
  import to bootstrap the `racktables_id` custom field and `from-racktables`
  tag).

## Quick start (Docker)

```bash
# 1. Build the image
docker build -t rt2nb:latest .

# 2. Create your config from the sample and edit it
cp config.example.yml config.yml
#   ... fill in racktables.* and netbox.* ...

# 3. Export (read-only against RackTables) into ./export_data
docker run --rm --network host \
  -v "$PWD/config.yml:/app/config.yml:ro" \
  -v "$PWD/export_data:/data/export_data" \
  rt2nb:latest export

# 4. Review ./export_data/*.json and ./export_data/unmigrated_report.json

# 5. Dry-run the import (writes nothing)
docker run --rm --network host \
  -v "$PWD/config.yml:/app/config.yml:ro" \
  -v "$PWD/export_data:/data/export_data" \
  rt2nb:latest import --dry-run

# 6. Real import
docker run --rm --network host \
  -v "$PWD/config.yml:/app/config.yml:ro" \
  -v "$PWD/export_data:/data/export_data" \
  rt2nb:latest import

# 7. Verify
docker run --rm --network host \
  -v "$PWD/config.yml:/app/config.yml:ro" \
  -v "$PWD/export_data:/data/export_data" \
  rt2nb:latest verify
```

The image declares a `VOLUME` at `/data` and `config.example.yml` defaults
`paths.export_dir` to `/data/export_data` and `paths.log_file` to
`/data/rt2nb.log`, so the mounts above land the JSON snapshot and log on your
host.

### Networking

- `--network host` is the simplest option when both services are reachable from
  the host's own network.
- If RackTables and/or NetBox run in Docker Compose networks, attach the
  container to those networks and address services by their Compose **service
  name**. Export and import talk to different systems, so they can use
  different networks — the shared `export_data` volume carries state between
  them:

  ```bash
  # Export against the RackTables compose network
  docker run --rm --network racktables_default \
    -v "$PWD/config.yml:/app/config.yml:ro" \
    -v "$PWD/export_data:/data/export_data" \
    rt2nb:latest export
  # config.yml -> racktables.host: db          (the compose service name)

  # Import against the NetBox compose network
  docker run --rm --network netbox_default \
    -v "$PWD/config.yml:/app/config.yml:ro" \
    -v "$PWD/export_data:/data/export_data" \
    rt2nb:latest import
  # config.yml -> netbox.url: http://netbox:8080
  ```

  (`docker network ls` lists the network names; they are usually
  `<project>_default`.)

### Secrets via environment variables

Any config leaf can be overridden by an environment variable named
`RT2NB_<SECTION>__<KEY>` (dotted path, upper-cased, dots become double
underscores). This keeps credentials out of `config.yml`:

```bash
docker run --rm --network host \
  -e RT2NB_NETBOX__TOKEN="$NETBOX_TOKEN" \
  -e RT2NB_RACKTABLES__PASSWORD="$RT_DB_PASSWORD" \
  -v "$PWD/config.yml:/app/config.yml:ro" \
  -v "$PWD/export_data:/data/export_data" \
  rt2nb:latest import
```

## Running natively

Docker is the primary path, but the tool is plain Python and runs natively too:

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python -m rt2nb -c config.yml export
python -m rt2nb -c config.yml import --dry-run
python -m rt2nb -c config.yml import
python -m rt2nb -c config.yml verify
```

The tool uses `requests` (not pynetbox) and relies on `from __future__ import
annotations`, so it runs on **Python 3.7+**. For a native run, point
`paths.export_dir` and `paths.log_file` at writable local paths (the `/data`
defaults are for the container).

## Commands and options

```
rt2nb [-c config.yml] [-v] [--only CATS] [--skip CATS] <command>

commands:
  export               RackTables (SELECT only) -> export_data/*.json
  import [--dry-run]    export_data/*.json -> NetBox (create/update/skip)
  verify               reconcile exported JSON against NetBox
  schema               print the live RackTables table list and exit
```

Global options:

- `-c`, `--config` — path to `config.yml` (default `config.yml`).
- `-v`, `--verbose` — debug logging to stdout (full detail always goes to the
  log file).
- `--only CATS` — comma-separated list of categories to include.
- `--skip CATS` — comma-separated list of categories to exclude.
- `--version` — print the version and exit.

Categories: `tags, sites, locations, racks, manufacturers, device_types,
clusters, devices, vms, interfaces, cables, prefixes, ip_addresses,
vlan_groups, vlans`.

`import`-only option:

- `--dry-run` — compute and print create/update/skip counts without writing to
  NetBox.

Run `schema` first as a connectivity sanity check: it prints the tables the
live RackTables database actually exposes.

## Mapping overview

Objects are imported in dependency order:

| # | RackTables | NetBox |
|---|---|---|
| 1 | Location / Row objects | Site / Location |
| 2 | Rack objects | Rack (U-height, Row → Location) |
| 3 | HW type dictionary | Manufacturer / DeviceType (placeholder if unknown) |
| 4 | Object (Server/Switch/Router/PDU…) | Device (rack / position / face from RackSpace) |
| 5 | Non-racked Objects / VMs | Device (default site) / VirtualMachine + Cluster |
| 6 | Port | Interface (type mapped from port type, unknown → `other`) |
| 7 | Link | Cable |
| 8 | IPv4Network / IPv6Network | Prefix |
| 9 | IPv4 / IPv6 Address + Allocation | IPAddress bound to the device/VM Interface |
| 10 | VLANDomain / VLANDescription | VLANGroup / VLAN; port VLAN mode → interface mode/tagged/untagged |
| 11 | TagTree / TagStorage | Tag (flattened, path encoded in slug) |
| 12 | AttributeValue | serial / asset_tag / custom fields / comments |
| 13 | Object comment | comments |

**Attributes** are mapped by name (see `rt2nb/constants.py` and
`mapping.attribute_overrides` in the config). `OEM S/N 1` → `serial`, the asset
tag → `asset_tag`, and anything unmapped is preserved as a device custom field
keyed by a slug of the attribute name — nothing is silently discarded.

**Device type fallback**: when a device has no HW-type attribute, its NetBox
device type model is taken from the RackTables **object-type label**
(`CableOrganizer`, `Shelf`, `Server`, …) under the placeholder manufacturer
(`Unknown` by default, configurable), so unmodeled objects are grouped by what
they are instead of collapsing into a single `Unknown Device Type`. Only when
the object type is also empty does it fall back to `Unknown Device Type`. Any
raw HW-type string that could not be parsed is appended to the device comments.

**Device role**: each device/VM gets a NetBox role named after its RackTables
object type (`Server`, `Network switch`, `Router`, `PDU`, …), created on demand
(and marked usable for VMs). Objects with no object-type label fall back to the
`Migrated` role.

**IP-to-machine binding**: RackTables binds a machine's IPs (NIC names such as
`eth0`/`bond0`, and the IPMI address) to the object under a free-text interface
name that frequently has no physical `Port` — servers often have no ports at
all. For every such (object, interface-name) that has no matching Port, the
export creates a synthetic interface so the IP import attaches the address to
the correct device/VM interface instead of leaving it unattached.

## Design decisions

These are encoded in `config.yml` under `mapping:` and can be changed without
touching code.

| Question | Default | Config key |
|---|---|---|
| RackTables hierarchy → NetBox | RackTables **Location → Site**, **Row → NetBox Location** (rack group) | `row_as: location` |
| Tag hierarchy → flat NetBox tags | **Flatten**: tag name is the leaf, and the slug encodes the full parent path (e.g. `owner-teama`) so distinct paths sharing a leaf never collide | `tag_strategy: flatten_path` |
| Unknown device types | Placeholder manufacturer / device type, raw string kept in comments | `placeholder_manufacturer`, `placeholder_device_type` |

Alternatives are supported: `row_as: site` (each Row becomes a Site) or
`row_as: single_site` (one Site, everything nested as Locations); and
`tag_strategy: leaf` or `explode`.

## Idempotency model

Every migrated NetBox object gets:

- a **custom field `racktables_id`** (text) set to a **namespaced** source key,
  e.g. `object:42`, `ipv4net:5`, `mfr:dell`. Namespacing is required because
  RackTables id-spaces overlap across tables. This is the primary match key.
- the **tag `from-racktables`**.

On import each record is looked up first by `racktables_id`, then by a
**natural key** (slug / address / vid / name+site) as a fallback so objects you
created by hand are adopted rather than duplicated:

- found and identical → **skip**
- found and different → **PATCH** only the changed fields
- absent → **create**

The custom field and tag are created automatically (a one-time `bootstrap`)
before the first object is written. Running `import` twice in a row yields all
skips on the second run.

## Nothing is dropped silently

Anything that cannot be represented is written — with source table, source id,
and a reason — to one of two reports under `export_data/`:

- **`unmigrated_report.json`** — export-time issues (e.g. fractional-U or
  multi-rack mounts, ports on non-device objects, unparseable networks,
  addresses allocated to multiple objects).
- **`unmigrated_import_report.json`** — import-time failures (e.g. a NetBox
  validation rejection), including the API error text.

`verify` prints, per category, `expected` (records in JSON) versus `in_netbox`
(objects carrying that `racktables_id`) and lists any missing keys. Its exit
code is `0` when nothing is missing and `2` otherwise, so it is usable in CI.

## Known limitations

- **Rack geometry.** NetBox stores a single mount position and face. Half-U
  mounts, non-contiguous spans, and objects mounted across multiple racks are
  reduced to the bottom-most unit / front face, recorded in the unmigrated
  report, and their original detail is preserved in the device comments.
- **VM interface cabling.** NetBox cannot cable virtual-machine interfaces, so a
  RackTables `Link` touching a VM port is reported, not created.
- **Duplicate keys.** Duplicate `asset_tag`, prefix, or object name values that
  NetBox would reject are handled defensively and reported rather than allowed
  to abort the run; the affected records appear in the unmigrated import report.
- **MAC addresses.** A port's MAC is folded into the NetBox interface
  description rather than modeled as a separate MAC object.
- **Multi-assignment IPs.** An address allocated to several RackTables objects
  is created once and bound to the first resolvable interface; the rest are
  reported.
- **Tag hierarchy** is flattened (see [Design decisions](#design-decisions)).

## Troubleshooting

Real-world gotchas, kept generic:

- **Custom-field text filters are partial-match.** NetBox filters a text custom
  field (like `racktables_id`) by substring, not exact match. The tool accounts
  for this internally when it looks objects up, but be aware of it if you query
  `racktables_id` yourself.
- **Duplicate CIDRs / IPs rejected.** netbox-docker ships
  `ENFORCE_GLOBAL_UNIQUE=true` by default, which rejects duplicate prefixes and
  addresses. If your RackTables data legitimately contains overlapping space,
  set it to `false` in the NetBox environment before importing.
- **Token format.** NetBox 4.x tokens look like `nbt_<key>.<secret>` and are
  sent as a Bearer token. Paste only the token value into `netbox.token` (no
  `Token`/`Bearer` prefix). `auth_scheme: auto` picks Bearer for these and the
  classic `Token` scheme for legacy 40-hex tokens.
- **CSRF 403 on login.** If the NetBox UI returns a CSRF 403 when you log in,
  add your origin to `CSRF_TRUSTED_ORIGINS` in the NetBox configuration.
- **Postgres crashes on old Docker / libseccomp.** On hosts with an old
  Docker/libseccomp, recent PostgreSQL images can crash on unrecognised
  syscalls. Add `security_opt: [seccomp=unconfined]` to the postgres service as
  a workaround.
- **First-boot DB-wait race.** On the very first `docker compose up`, NetBox may
  start before the database is ready and exit. Simply re-run
  `docker compose up -d`.

## Development and tests

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Unit tests cover the pure mapping logic (slugs, MAC, tags, IP conversion, rack
geometry, HW-type parsing, interface-type mapping), the idempotency diff, the
upsert create/update/skip/dry-run behaviour, and the read-only SQL guard.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and PR expectations.

## Safety: read-only against RackTables

`rt2nb/db.py` is the only module that connects to RackTables. Every statement
passes through a read-only guard which:

- allows only statements starting with `SELECT` / `SHOW` / `DESCRIBE` /
  `EXPLAIN`, and
- rejects any statement containing a write verb
  (`INSERT`/`UPDATE`/`DELETE`/`DROP`/…) outside quoted literals, including
  attempts to smuggle in a second statement.

The session is additionally set `TRANSACTION READ ONLY` with autocommit and no
`COMMIT` is ever issued. Using a read-only MySQL/MariaDB grant on top of this is
still recommended as defence in depth.

## License

Released under the [MIT License](LICENSE).
