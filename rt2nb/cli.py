"""Command-line interface: export / import / verify subcommands."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Set

from . import __version__
from .config import Config
from .logging_setup import get_logger, setup_logging


def _parse_csv(value: Optional[str]) -> Optional[Set[str]]:
    if not value:
        return None
    return {v.strip() for v in value.split(",") if v.strip()}


def _resolve_categories(only: Optional[str], skip: Optional[str], universe) -> Set[str]:
    base = _parse_csv(only) or set(universe)
    drop = _parse_csv(skip) or set()
    return {c for c in universe if c in base and c not in drop}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rt2nb",
        description="Migrate RackTables 0.20.x to NetBox 4.6 (export/import/verify).",
    )
    p.add_argument("--version", action="version", version=f"rt2nb {__version__}")
    p.add_argument("-c", "--config", default="config.yml", help="path to config.yml")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging to stdout")
    p.add_argument("--only", help="comma-separated categories to include")
    p.add_argument("--skip", help="comma-separated categories to exclude")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("export", help="read RackTables (SELECT only) -> intermediate JSON")
    imp = sub.add_parser("import", help="intermediate JSON -> NetBox")
    imp.add_argument("--dry-run", action="store_true",
                     help="report create/update/skip counts without writing")
    sub.add_parser("verify", help="reconcile exported JSON against NetBox")
    sub.add_parser("schema", help="print the live RackTables schema summary and exit")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    log = setup_logging(cfg.paths.get("log_file"), verbose=args.verbose)

    if args.command == "export":
        return _cmd_export(cfg, args, log)
    if args.command == "import":
        return _cmd_import(cfg, args, log)
    if args.command == "verify":
        return _cmd_verify(cfg, args, log)
    if args.command == "schema":
        return _cmd_schema(cfg, args, log)
    return 1


def _cmd_schema(cfg, args, log) -> int:
    from .db import ReadOnlyConnection, Schema
    with ReadOnlyConnection(cfg.racktables) as conn:
        schema = Schema(conn)
        print(f"RackTables database: {cfg.racktables['database']}")
        print(f"Tables present ({len(schema.tables)}):")
        for t in sorted(schema.tables):
            print(f"  {t}")
    return 0


def _cmd_export(cfg, args, log) -> int:
    from .db import ReadOnlyConnection, Schema
    from .export import Exporter
    from .export.exporter import ALL_CATEGORIES

    cats = _resolve_categories(args.only, args.skip, ALL_CATEGORIES)
    log.info("export: categories = %s", ", ".join(sorted(cats)))
    with ReadOnlyConnection(cfg.racktables) as conn:
        schema = Schema(conn)
        exporter = Exporter(conn, schema, cfg, cats)
        summary, unmig = exporter.run()
    print("\n" + summary.render())
    print(f"\nUnmigrated (export): {len(unmig)} "
          f"(see {cfg.paths['export_dir']}/unmigrated_report.json)")
    return 0


def _cmd_import(cfg, args, log) -> int:
    from .importer import Importer
    from .importer.importer import IMPORT_ORDER
    from .netbox.client import NetBoxClient

    cfg.require_netbox_token()
    cats = _resolve_categories(args.only, args.skip, IMPORT_ORDER)
    nb = NetBoxClient(cfg.netbox)
    ver = nb.version()
    log.info("NetBox version %s at %s", ver, cfg.netbox["url"])
    if not ver.startswith("4."):
        log.warning("This tool targets NetBox 4.x; detected %s. Proceeding.", ver)
    log.info("import: categories = %s%s", ", ".join(sorted(cats)),
             "  [DRY RUN]" if args.dry_run else "")
    importer = Importer(nb, cfg, cats, dry_run=args.dry_run)
    summary, unmig = importer.run()
    print("\n" + summary.render())
    print(f"\nImport failures/unmigrated: {len(unmig)} "
          f"(see {cfg.paths['export_dir']}/unmigrated_import_report.json)")
    if args.dry_run:
        print("\n[DRY RUN] No changes were written to NetBox.")
    return 0


def _cmd_verify(cfg, args, log) -> int:
    from .importer.importer import IMPORT_ORDER
    from .netbox.client import NetBoxClient
    from .verify import run_verify

    cfg.require_netbox_token()
    cats = _resolve_categories(args.only, args.skip, IMPORT_ORDER)
    nb = NetBoxClient(cfg.netbox)
    return run_verify(nb, cfg, cats)
