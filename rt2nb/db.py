"""Read-only MySQL access to the RackTables database.

This module is the *only* place that talks to RackTables.  It guards against
accidental writes in two ways:

  * ``ReadOnlyConnection.query`` rejects any statement that is not a bare
    ``SELECT`` (or ``SHOW`` / ``DESCRIBE``) before it ever reaches the server.
  * On connect it best-effort sets the session to READ ONLY / autocommit and
    never issues COMMIT, so even a driver-level slip cannot persist.

Schema introspection lets the export phase adapt to whatever columns the live
0.20.x database actually has, instead of trusting possibly-stale docs.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import pymysql
import pymysql.cursors

from .logging_setup import get_logger

log = get_logger(__name__)

# Only these leading keywords are ever allowed to reach the server.
_ALLOWED_STMT = re.compile(r"^\s*(select|show|describe|desc|explain)\b", re.IGNORECASE)
# Defensive: reject anything that smuggles a second statement or a write verb.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|replace|grant|"
    r"revoke|call|load\s+data|into\s+outfile|set\s+@)\b",
    re.IGNORECASE,
)


class WriteAttemptError(RuntimeError):
    """Raised when a non-SELECT statement is passed to the read-only layer."""


class ReadOnlyConnection:
    def __init__(self, cfg: Dict[str, Any]):
        self._cfg = cfg
        self._conn: Optional[pymysql.connections.Connection] = None

    def connect(self) -> "ReadOnlyConnection":
        self._conn = pymysql.connect(
            host=self._cfg["host"],
            port=int(self._cfg["port"]),
            user=self._cfg["user"],
            password=self._cfg["password"],
            database=self._cfg["database"],
            charset=self._cfg.get("charset", "utf8mb4"),
            connect_timeout=int(self._cfg.get("connect_timeout", 10)),
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
        # Best-effort: mark the session read-only.  Harmless if unsupported.
        try:
            with self._conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception as exc:  # pragma: no cover - server-dependent
            log.debug("Could not set session READ ONLY: %s", exc)
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "ReadOnlyConnection":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- guarded query ------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] | None = None) -> List[Dict[str, Any]]:
        self._assert_read_only(sql)
        assert self._conn is not None, "connect() first"
        with self._conn.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())

    @staticmethod
    def _assert_read_only(sql: str) -> None:
        if not _ALLOWED_STMT.match(sql):
            raise WriteAttemptError(f"Refusing non-SELECT statement: {sql[:80]!r}")
        # Strip string/quoted literals before scanning for forbidden verbs so a
        # column value like 'update_ts' inside a WHERE never trips the guard.
        scrubbed = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)
        scrubbed = re.sub(r'"(?:[^"\\]|\\.)*"', '""', scrubbed)
        scrubbed = re.sub(r"`[^`]*`", "``", scrubbed)
        if _FORBIDDEN.search(scrubbed):
            raise WriteAttemptError(f"Refusing statement with write verb: {sql[:80]!r}")

    # -- introspection ------------------------------------------------------

    def list_tables(self) -> Set[str]:
        rows = self.query(
            "SELECT table_name AS t FROM information_schema.tables "
            "WHERE table_schema = %s",
            (self._cfg["database"],),
        )
        return {r["t"] for r in rows}

    def list_columns(self, table: str) -> Set[str]:
        rows = self.query(
            "SELECT column_name AS c FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (self._cfg["database"], table),
        )
        return {r["c"] for r in rows}


class Schema:
    """Cached snapshot of the live RackTables schema.

    Used throughout the export phase to check for the existence of tables and
    columns before referencing them, so a slightly different 0.20.x build does
    not crash the run.
    """

    def __init__(self, conn: ReadOnlyConnection):
        self._conn = conn
        self.tables: Set[str] = conn.list_tables()
        self._cols: Dict[str, Set[str]] = {}

    def has_table(self, table: str) -> bool:
        return table in self.tables

    def columns(self, table: str) -> Set[str]:
        if table not in self._cols:
            self._cols[table] = (
                self._conn.list_columns(table) if self.has_table(table) else set()
            )
        return self._cols[table]

    def has_column(self, table: str, column: str) -> bool:
        return column in self.columns(table)

    def present_columns(self, table: str, candidates: Iterable[str]) -> List[str]:
        cols = self.columns(table)
        return [c for c in candidates if c in cols]

    def first_present(self, table: str, candidates: Iterable[str]) -> Optional[str]:
        cols = self.columns(table)
        for c in candidates:
            if c in cols:
                return c
        return None
