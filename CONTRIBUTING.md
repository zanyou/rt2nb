# Contributing to rt2nb

Thanks for your interest in improving rt2nb. This is a focused tool for
migrating RackTables 0.20.x into NetBox 4.6; contributions that keep it correct,
idempotent, and read-only against RackTables are very welcome.

## Development setup

Use a virtual environment and install the development requirements (which
include the runtime requirements):

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements-dev.txt
```

Python 3.7+ is supported; the project image is built on Python 3.11.

## Running the tests

```bash
pytest
```

The suite is fast and requires no database or NetBox instance — it covers the
pure mapping logic, the idempotency diff, the upsert create/update/skip/dry-run
behaviour, and the read-only SQL guard. Please add or update tests alongside any
behaviour change, and keep the suite green before opening a PR.

If your change touches the mapping or import logic, a quick manual sanity check
against a disposable NetBox (e.g. netbox-docker) with `import --dry-run` is
appreciated.

## Code style

- Standard library `logging` for output; no `print` for diagnostics.
- Type hints on public functions; the codebase uses
  `from __future__ import annotations`.
- Keep functions small and pure where possible — the mapping layer is
  deliberately side-effect-free so it stays unit-testable.
- Match the existing formatting and naming conventions in the file you are
  editing.

## Guardrails to preserve

- **RackTables access stays read-only.** All database access goes through the
  guarded connection in `rt2nb/db.py`. Do not add write statements or bypass the
  guard.
- **Idempotency is a feature.** Any new object type must carry the
  `racktables_id` custom field and `from-racktables` tag and be looked up before
  it is created, so re-running `import` stays safe.
- **Nothing is dropped silently.** If data cannot be represented in NetBox,
  record it in the appropriate unmigrated report rather than discarding it.

## Pull requests

- Keep PRs focused on a single change and describe what and why.
- Include tests for new behaviour and note any config or mapping changes.
- Make sure `pytest` passes.
- Update `README.md` and `CHANGELOG.md` when behaviour or capabilities change.

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
