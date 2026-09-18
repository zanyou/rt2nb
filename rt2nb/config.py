"""Configuration loading.

Reads ``config.yml`` (YAML), applies built-in defaults, and lets any leaf be
overridden by an environment variable named ``RT2NB_<DOTTED_PATH>`` with dots
replaced by double underscores, e.g. ``RT2NB_NETBOX__TOKEN``.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Dict

import yaml

_DEFAULTS: Dict[str, Any] = {
    "racktables": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "racktables",
        "password": "",
        "database": "racktables",
        "charset": "utf8mb4",
        "connect_timeout": 10,
    },
    "netbox": {
        "url": "http://127.0.0.1:8000",
        "token": "",
        "auth_scheme": "auto",
        "ssl_verify": True,
        "timeout": 30,
        "retries": 5,
        "retry_backoff": 0.5,
        "page_size": 200,
    },
    "paths": {
        "export_dir": "./export_data",
        "log_file": "./rt2nb.log",
    },
    "mapping": {
        "row_as": "location",
        "default_site_name": "Migrated",
        "default_site_slug": "migrated",
        "tag_strategy": "flatten_path",
        "placeholder_manufacturer": "Unknown",
        "placeholder_device_type": "Unknown Device Type",
        "placeholder_u_height": 1,
        "interface_type_overrides": {},
        "attribute_overrides": {},
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env_overrides(cfg: Dict[str, Any], prefix: str = "RT2NB") -> None:
    """Mutate ``cfg`` in place from ``RT2NB_SECTION__KEY`` env vars."""
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix + "_"):
            continue
        path = env_key[len(prefix) + 1:].lower().split("__")
        node = cfg
        for part in path[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        leaf = path[-1]
        # Preserve type of an existing default where possible.
        existing = node.get(leaf)
        node[leaf] = _coerce(env_val, existing)


def _coerce(value: str, like: Any) -> Any:
    if isinstance(like, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(like, float):
        try:
            return float(value)
        except ValueError:
            return value
    return value


@dataclass
class Config:
    racktables: Dict[str, Any] = field(default_factory=dict)
    netbox: Dict[str, Any] = field(default_factory=dict)
    paths: Dict[str, Any] = field(default_factory=dict)
    mapping: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None) -> "Config":
        file_data: Dict[str, Any] = {}
        if path:
            with open(path, "r", encoding="utf-8") as fh:
                file_data = yaml.safe_load(fh) or {}
        merged = _deep_merge(_DEFAULTS, file_data)
        _apply_env_overrides(merged)
        return cls(
            racktables=merged["racktables"],
            netbox=merged["netbox"],
            paths=merged["paths"],
            mapping=merged["mapping"],
            raw=merged,
        )

    def require_netbox_token(self) -> None:
        if not self.netbox.get("token"):
            raise ValueError(
                "netbox.token is empty. Set it in config.yml or via "
                "RT2NB_NETBOX__TOKEN."
            )
