"""Shared plumbing for platform connectors."""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from ..models import Snapshot

log = logging.getLogger(__name__)

USER_AGENT = "SolarWatch/1.0 (+self-hosted fleet monitor)"


class ConnectorError(Exception):
    """Base class. `kind` tells the poller how to react."""
    kind = "transient"


class AuthError(ConnectorError):
    """Credentials refused. Retrying on a timer can lock the account, so the poller stops."""
    kind = "auth"


class TransientError(ConnectorError):
    """Network trouble, 5xx, rate limit, odd payload: retry later with backoff."""
    kind = "transient"


class Connector:
    """One platform account. Subclasses implement `poll()`.

    Connectors are synchronous; the poller runs them in a worker thread. A connector
    owns its session and token cache, so one instance lives for the account's lifetime.
    """
    platform = "base"

    def __init__(self, account: dict[str, Any], session: Optional[requests.Session] = None):
        self.account = account
        self.account_id = account["id"]
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.timeout = float(account.get("timeout", 30))

    def poll(self) -> list[Snapshot]:
        """Return every site on the account with its latest reading (or a per-site error)."""
        raise NotImplementedError

    # -- HTTP helper -------------------------------------------------------------
    def _request(self, method: str, url: str, *, auth_statuses=(401, 403), **kw) -> Any:
        kw.setdefault("timeout", self.timeout)
        try:
            resp = self.session.request(method, url, **kw)
        except requests.RequestException as e:
            raise TransientError(f"{self.platform}: could not reach {_host(url)} ({e.__class__.__name__})") from e
        if resp.status_code in auth_statuses:
            raise AuthError(f"{self.platform}: {_host(url)} answered HTTP {resp.status_code}")
        if resp.status_code == 429:
            raise TransientError(f"{self.platform}: rate limited by {_host(url)}")
        if resp.status_code >= 400:
            raise TransientError(f"{self.platform}: {_host(url)} answered HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise TransientError(f"{self.platform}: {_host(url)} sent a non-JSON reply") from e


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else url
