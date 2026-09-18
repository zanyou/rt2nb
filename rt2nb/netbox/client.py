"""Thin NetBox REST client built on ``requests``.

Chosen over pynetbox to keep retry/pagination behaviour explicit and to avoid a
heavier dependency.  Targets the NetBox 4.x API surface.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from ..logging_setup import get_logger

log = get_logger(__name__)


class NetBoxError(RuntimeError):
    pass


class NetBoxClient:
    def __init__(self, cfg: Dict[str, Any]):
        self.base = cfg["url"].rstrip("/") + "/api"
        self.timeout = int(cfg.get("timeout", 30))
        self.retries = int(cfg.get("retries", 5))
        self.backoff = float(cfg.get("retry_backoff", 0.5))
        self.page_size = int(cfg.get("page_size", 200))
        self._status: Optional[Dict[str, Any]] = None

        self.session = requests.Session()
        self.session.verify = bool(cfg.get("ssl_verify", True))
        self.session.headers.update({
            "Authorization": self._auth_header(cfg),
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    @staticmethod
    def _auth_header(cfg: Dict[str, Any]) -> str:
        """Build the Authorization header.

        NetBox 4.x accepts two forms: classic ``Token <40-hex>`` and the newer
        key.secret form ``Bearer <key>.<token>`` (tokens shown as ``nbt_...``).
        We strip any scheme the user accidentally pasted into the token and pick
        the scheme automatically (override with ``netbox.auth_scheme``).
        """
        token = str(cfg.get("token", "")).strip()
        low = token.lower()
        if low.startswith("bearer "):
            token = token[len("bearer "):].strip()
        elif low.startswith("token "):
            token = token[len("token "):].strip()

        scheme = str(cfg.get("auth_scheme", "auto")).strip().lower()
        if scheme == "bearer":
            prefix = "Bearer"
        elif scheme == "token":
            prefix = "Token"
        else:  # auto: new key.secret tokens contain a dot and use Bearer
            prefix = "Bearer" if "." in token else "Token"
        return f"{prefix} {token}"

    # -- low level ----------------------------------------------------------

    def _url(self, endpoint: str) -> str:
        return f"{self.base}/{endpoint.strip('/')}/"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:  # network error
                last_exc = exc
                self._sleep(attempt)
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                retry_after = resp.headers.get("Retry-After")
                self._sleep(attempt, retry_after)
                last_exc = NetBoxError(f"{resp.status_code} on {method} {url}")
                continue
            return resp
        raise NetBoxError(f"Request failed after retries: {method} {url}: {last_exc}")

    def _sleep(self, attempt: int, retry_after: Optional[str] = None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 60))
                return
            except ValueError:
                pass
        time.sleep(min(self.backoff * (2 ** attempt), 30))

    @staticmethod
    def _json_or_raise(resp: requests.Response) -> Any:
        if resp.status_code >= 400:
            raise NetBoxError(f"{resp.status_code} {resp.request.method} "
                              f"{resp.url}: {resp.text[:500]}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- status -------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        if self._status is None:
            resp = self._request("GET", f"{self.base}/status/")
            self._status = self._json_or_raise(resp) or {}
        return self._status

    def version(self) -> str:
        return str(self.status().get("netbox-version", "unknown"))

    # -- CRUD ---------------------------------------------------------------

    def get_all(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("limit", self.page_size)
        url: Optional[str] = self._url(endpoint)
        results: List[Dict[str, Any]] = []
        first = True
        while url:
            resp = self._request("GET", url, params=params if first else None)
            data = self._json_or_raise(resp)
            results.extend(data.get("results", []))
            url = data.get("next")
            first = False
        return results

    def get_one(self, endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        p = dict(params)
        p["limit"] = 2
        resp = self._request("GET", self._url(endpoint), params=p)
        data = self._json_or_raise(resp)
        results = data.get("results", [])
        if not results:
            return None
        return results[0]

    def create(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._request("POST", self._url(endpoint), json=data)
        return self._json_or_raise(resp)

    def bulk_create(self, endpoint: str, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not data:
            return []
        resp = self._request("POST", self._url(endpoint), json=data)
        return self._json_or_raise(resp)

    def update(self, endpoint: str, obj_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._url(endpoint)}{obj_id}/"
        resp = self._request("PATCH", url, json=data)
        return self._json_or_raise(resp)
