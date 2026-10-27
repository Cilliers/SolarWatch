"""Background polling: one asyncio task per account, each running its connector in a
worker thread so a slow platform never holds up the others."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .connectors import RETIRED, Connector, make_connector
from .connectors.base import ConnectorError
from .db import Database

log = logging.getLogger("solarwatch.poller")
MAX_BACKOFF = 30 * 60


@dataclass
class AccountState:
    id: str
    platform: str
    enabled: bool = True
    state: str = "pending"          # pending | ok | partial | error | auth_failed | disabled
    last_attempt: Optional[float] = None
    last_success: Optional[float] = None
    next_poll: Optional[float] = None
    last_error: Optional[str] = None
    failures: int = 0
    site_count: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "wake"}


class Poller:
    def __init__(self, cfg: dict, db: Database):
        self.cfg, self.db = cfg, db
        self.interval = max(60, int(cfg["poll_interval"]))
        self.connectors: dict[str, Connector] = {}
        self.states: dict[str, AccountState] = {}
        self._tasks: list[asyncio.Task] = []
        for acc in cfg["accounts"]:
            if acc.get("platform") in RETIRED:
                # Keep the dashboard up; show the account as paused with what to change.
                st = AccountState(acc["id"], acc["platform"], enabled=False, state="disabled")
                st.last_error = RETIRED[acc["platform"]]
                self.states[acc["id"]] = st
                log.error("account %s: %s", acc["id"], st.last_error)
                continue
            conn = make_connector(acc)
            self.connectors[acc["id"]] = conn
            self.states[acc["id"]] = AccountState(acc["id"], conn.platform, enabled=acc.get("enabled", True),
                                                  state="pending" if acc.get("enabled", True) else "disabled")
        db.delete_account_sites_except(list(self.connectors))

    async def start(self) -> None:
        for i, acc_id in enumerate(self.connectors):
            if self.states[acc_id].enabled:
                self._tasks.append(asyncio.create_task(self._run(acc_id, initial_delay=i * 2)))
        self._tasks.append(asyncio.create_task(self._housekeeping()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def poll_now(self, account_id: Optional[str] = None) -> list[str]:
        """Wake one or all accounts. Also the only way out of auth_failed."""
        woken = []
        for st in self.states.values():
            if st.enabled and (account_id is None or st.id == account_id):
                st.wake.set()
                woken.append(st.id)
        return woken

    async def _run(self, acc_id: str, initial_delay: float = 0) -> None:
        st = self.states[acc_id]
        await asyncio.sleep(initial_delay)
        while True:
            await self.poll_account(acc_id)
            if st.state == "auth_failed":
                delay = None    # repeated bad logins can lock the account: wait for a human
            elif st.failures:
                delay = min(self.interval * 2 ** (st.failures - 1), MAX_BACKOFF)
            else:
                delay = self.interval
            if delay is not None:
                delay += random.uniform(0, min(20, delay * 0.05))   # spread load across accounts
            st.next_poll = time.time() + delay if delay is not None else None
            st.wake.clear()
            try:
                await asyncio.wait_for(st.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def poll_account(self, acc_id: str) -> None:
        st, conn = self.states[acc_id], self.connectors[acc_id]
        st.last_attempt = time.time()
        if hasattr(conn, "backfill") and not self.db.account_has_readings(acc_id):
            for site, points in await asyncio.to_thread(conn.backfill):
                sid = self.db.upsert_site(acc_id, conn.platform, site)
                for t, r in points:
                    self.db.add_reading(sid, r, t)
        try:
            snapshots = await asyncio.to_thread(conn.poll)
        except ConnectorError as e:
            st.failures += 1
            st.last_error = str(e)
            st.state = "auth_failed" if e.kind == "auth" else "error"
            log.warning("account %s: %s", acc_id, e)
            return
        except Exception as e:   # a bug or a payload we didn't expect: keep the app alive
            st.failures += 1
            st.last_error = f"unexpected error: {e!r}"
            st.state = "error"
            log.exception("%s: unexpected error while polling", acc_id)
            return
        fetched = time.time()
        errors = 0
        for snap in snapshots:
            site_id = self.db.upsert_site(acc_id, conn.platform, snap.site, snap.error)
            if snap.reading is not None:
                self.db.add_reading(site_id, snap.reading, fetched)
            else:
                errors += 1
        st.site_count = len(snapshots)
        st.failures = 0
        st.last_success = fetched
        st.last_error = f"{errors} site(s) failed to read" if errors else None
        st.state = "partial" if errors else "ok"
        log.info("%s: %d site(s) polled, %d error(s)", acc_id, len(snapshots), errors)

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(3600)
            n = await asyncio.to_thread(self.db.prune, float(self.cfg["retention_days"]))
            if n:
                log.info("pruned %d old readings", n)
