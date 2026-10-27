"""Web API + dashboard (Starlette)."""
from __future__ import annotations

import base64
import contextlib
import logging
import secrets
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .alerts import site_health
from .config import load_config
from .db import Database
from .poller import Poller

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("solarwatch")


class BasicAuth(BaseHTTPMiddleware):
    def __init__(self, app, username: str, password: str):
        super().__init__(app)
        self.expected = base64.b64encode(f"{username}:{password}".encode()).decode()

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic ") and secrets.compare_digest(header[6:], self.expected):
            return await call_next(request)
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="SolarWatch"'})


def create_app(cfg: dict | None = None, db: Database | None = None, start_poller: bool = True) -> Starlette:
    cfg = cfg or load_config()
    db = db or Database(cfg["database"])
    poller = Poller(cfg, db)

    def account_states() -> dict[str, dict]:
        return {k: v.public() for k, v in poller.states.items()}

    def enriched_sites() -> tuple[list[dict], list[dict]]:
        states = account_states()
        sites, alerts = [], []
        for s in db.sites_with_latest(list(poller.connectors)):   # retired accounts' sites are hidden
            status, site_alerts = site_health(s, states.get(s["account_id"]), cfg)
            s["status"] = status
            s["alerts"] = site_alerts
            sites.append(s)
            alerts.extend(site_alerts)
        order = {"critical": 0, "error": 1, "warning": 2}
        alerts.sort(key=lambda a: (order.get(a["level"], 9), a["site_name"]))
        return sites, alerts

    async def index(_):
        return FileResponse(STATIC / "index.html")

    async def api_overview(_):
        sites, alerts = enriched_sites()
        live = [s for s in sites if s["status"] in ("ok", "warning")]
        socs = [s["soc"] for s in live if s.get("soc") is not None]
        summary = {
            "sites": len(sites),
            "by_status": {st: sum(1 for s in sites if s["status"] == st)
                          for st in ("ok", "warning", "fault", "offline", "error", "pending")},
            "pv_w": sum(s.get("pv_w") or 0 for s in live),
            "load_w": sum(s.get("load_w") or 0 for s in live),
            "pv_kwh_today": sum(s.get("pv_kwh_today") or 0 for s in sites),
            "avg_soc": sum(socs) / len(socs) if socs else None,
            "capacity_kwp": sum(s.get("capacity_kwp") or 0 for s in sites),
        }
        return JSONResponse({"now": time.time(), "summary": summary, "sites": sites, "alerts": alerts,
                             "accounts": list(account_states().values()), "poll_interval": poller.interval,
                             "thresholds": cfg["alerts"]})

    async def api_site(request: Request):
        site_id = int(request.path_params["site_id"])
        s = db.site(site_id)
        if not s or s["account_id"] not in poller.connectors:
            return JSONResponse({"error": "not found"}, 404)
        s["status"], s["alerts"] = site_health(s, account_states().get(s["account_id"]), cfg)
        return JSONResponse(s)

    async def api_history(request: Request):
        site_id = int(request.path_params["site_id"])
        hours = min(max(float(request.query_params.get("hours", 24)), 1), 24 * 31)
        return JSONResponse({"site_id": site_id, "hours": hours,
                             "points": db.history(site_id, time.time() - hours * 3600)})

    async def api_raw(request: Request):
        return JSONResponse(db.raw(int(request.path_params["site_id"])) or {})

    async def api_poll(request: Request):
        acc = request.path_params.get("account_id")
        if acc and acc not in poller.states:
            return JSONResponse({"error": "unknown account"}, 404)
        return JSONResponse({"woken": poller.poll_now(acc)})

    async def healthz(_):
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if start_poller:
            await poller.start()
        yield
        await poller.stop()

    middleware = []
    if cfg.get("auth"):
        middleware.append(Middleware(BasicAuth, username=cfg["auth"]["username"], password=cfg["auth"]["password"]))

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/overview", api_overview),
            Route("/api/sites/{site_id:int}", api_site),
            Route("/api/sites/{site_id:int}/history", api_history),
            Route("/api/sites/{site_id:int}/raw", api_raw),
            Route("/api/poll", api_poll, methods=["POST"]),
            Route("/api/poll/{account_id}", api_poll, methods=["POST"]),
            Route("/healthz", healthz),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )
    app.state.poller, app.state.db, app.state.cfg = poller, db, cfg
    return app
