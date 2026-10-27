import asyncio
import time
import unittest

from starlette.testclient import TestClient

from solarwatch.config import DEFAULTS
from solarwatch.connectors.base import AuthError, Connector, TransientError
from solarwatch.db import Database
from solarwatch.models import Reading, SiteInfo, Snapshot
from solarwatch.poller import Poller
from solarwatch.server import create_app


def cfg(accounts, **kw):
    c = {**DEFAULTS, "database": ":memory:", "accounts": accounts, **kw}
    c["alerts"] = dict(DEFAULTS["alerts"])
    return c


class Scripted(Connector):
    platform = "scripted"

    def __init__(self, results):
        super().__init__({"id": "x"})
        self.results = list(results)

    def poll(self):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.p = Poller(cfg([{"id": "d", "platform": "demo", "sites": 1, "backfill_hours": 0}]), self.db)

    def run_poll(self, conn):
        self.p.connectors["d"] = conn
        asyncio.run(self.p.poll_account("d"))
        return self.p.states["d"]

    def test_auth_error_stops_retries(self):
        st = self.run_poll(Scripted([AuthError("sunsynk: Incorrect password")]))
        self.assertEqual(st.state, "auth_failed")

    def test_transient_then_recovers(self):
        site = SiteInfo("s1", "Home")
        conn = Scripted([TransientError("down"), [Snapshot(site, Reading(pv_w=100, soc=50))]])
        st = self.run_poll(conn)
        self.assertEqual((st.state, st.failures), ("error", 1))
        st = self.run_poll(conn)
        self.assertEqual((st.state, st.failures), ("ok", 0))

    def test_partial_failure(self):
        conn = Scripted([[Snapshot(SiteInfo("a", "A"), Reading(pv_w=1)), Snapshot(SiteInfo("b", "B"), error="boom")]])
        st = self.run_poll(conn)
        self.assertEqual(st.state, "partial")
        sites = {s["name"]: s for s in self.db.sites_with_latest(["d"])}
        self.assertEqual(sites["B"]["last_error"], "boom")
        self.assertEqual(sites["A"]["pv_w"], 1)

    def test_unexpected_exception_does_not_crash(self):
        st = self.run_poll(Scripted([KeyError("surprise")]))
        self.assertEqual(st.state, "error")


class ApiTests(unittest.TestCase):
    def test_overview_alerts_and_history(self):
        c = cfg([{"id": "demo-a", "platform": "demo", "as_platform": "sunsynk", "sites": 3,
                  "backfill_hours": 2, "quirks": {1: "offline", 2: "lowsoc"}}])
        app = create_app(c, Database(":memory:"), start_poller=False)
        asyncio.run(app.state.poller.poll_account("demo-a"))
        with TestClient(app) as client:
            d = client.get("/api/overview").json()
            self.assertEqual(d["summary"]["sites"], 3)
            statuses = sorted(s["status"] for s in d["sites"])
            self.assertEqual(statuses, ["offline", "ok", "warning"])
            codes = {a["code"] for a in d["alerts"]}
            self.assertEqual(codes, {"silent", "low_soc"})
            sid = d["sites"][0]["id"]
            h = client.get(f"/api/sites/{sid}/history?hours=3").json()
            self.assertGreater(len(h["points"]), 15)
            self.assertEqual(client.get("/api/sites/9999").status_code, 404)
            self.assertEqual(client.post("/api/poll").json()["woken"], ["demo-a"])

    def test_retired_platform_does_not_stop_the_app(self):
        c = cfg([{"id": "old-sems", "platform": "goodwe", "username": "u", "password": "p"},
                 {"id": "demo-a", "platform": "demo", "sites": 1, "backfill_hours": 0}])
        app = create_app(c, Database(":memory:"), start_poller=False)
        asyncio.run(app.state.poller.poll_account("demo-a"))
        with TestClient(app) as client:
            d = client.get("/api/overview").json()
            old = next(a for a in d["accounts"] if a["id"] == "old-sems")
            self.assertEqual(old["state"], "disabled")
            self.assertIn("goodwe_plus", old["last_error"])
            self.assertEqual(d["summary"]["sites"], 1)

    def test_basic_auth(self):
        c = cfg([], auth={"username": "admin", "password": "pw"})
        app = create_app(c, Database(":memory:"), start_poller=False)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/overview").status_code, 401)
            self.assertEqual(client.get("/api/overview", auth=("admin", "pw")).status_code, 200)
            self.assertEqual(client.get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
