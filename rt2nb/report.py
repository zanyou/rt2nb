"""Run reporting: unmigrated records and per-phase summary tables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class UnmigratedReport:
    """Collects records that could not be migrated, with a reason each."""

    entries: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, source_table: str, source_id: Any, reason: str, **extra: Any) -> None:
        entry = {
            "source_table": source_table,
            "source_id": source_id,
            "reason": reason,
        }
        if extra:
            entry["detail"] = extra
        self.entries.append(entry)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.entries, fh, indent=2, ensure_ascii=False, default=str)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class CategoryStat:
    category: str
    source: int = 0        # count on the RackTables side
    created: int = 0
    updated: int = 0
    skipped: int = 0       # existed, no diff
    failed: int = 0
    unmigrated: int = 0    # deliberately not migrated (see unmigrated report)


class Summary:
    """Accumulates per-category stats and renders an aligned table."""

    def __init__(self) -> None:
        self._stats: Dict[str, CategoryStat] = {}

    def stat(self, category: str) -> CategoryStat:
        return self._stats.setdefault(category, CategoryStat(category))

    def render(self) -> str:
        cols = [
            ("category", "category", 18),
            ("source", "rt", 7),
            ("created", "created", 8),
            ("updated", "updated", 8),
            ("skipped", "skipped", 8),
            ("failed", "failed", 7),
            ("unmigrated", "unmig", 7),
        ]
        header = "  ".join(title.ljust(w) for _, title, w in cols)
        lines = [header, "  ".join("-" * w for _, _, w in cols)]
        totals = CategoryStat("TOTAL")
        for cat in sorted(self._stats):
            s = self._stats[cat]
            for f in ("source", "created", "updated", "skipped", "failed", "unmigrated"):
                setattr(totals, f, getattr(totals, f) + getattr(s, f))
            lines.append(_row(s, cols))
        lines.append("  ".join("-" * w for _, _, w in cols))
        lines.append(_row(totals, cols))
        return "\n".join(lines)


def _row(s: CategoryStat, cols) -> str:
    values = {
        "category": s.category,
        "source": s.source,
        "created": s.created,
        "updated": s.updated,
        "skipped": s.skipped,
        "failed": s.failed,
        "unmigrated": s.unmigrated,
    }
    return "  ".join(str(values[key]).ljust(w) for key, _, w in cols)
