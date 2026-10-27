"""Deye Cloud (official Deye Cloud developer API).

You need a free developer app: sign in at https://developer.deyecloud.com with your Deye
Cloud account, create an application, and note its App ID and App Secret.

  POST {base}/v1.0/account/token?appId=..  {appSecret, email|username, password: sha256_hex(pw)[, companyId]}
  POST {base}/v1.0/station/list            {page, size}            -> stationList
  POST {base}/v1.0/station/latest          {stationId}             -> live values (W)
  POST {base}/v1.0/station/history         {stationId, granularity:2, startAt, endAt}  -> today's kWh

Live-verified signs (community): batteryPower > 0 discharging, wirePower > 0 importing.
Regions: eu1-developer.deyecloud.com (default), us1-developer.deyecloud.com.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..models import Reading, SiteInfo, Snapshot, pick, pick_str
from .base import AuthError, Connector, ConnectorError, TransientError

BASE = "https://eu1-developer.deyecloud.com"
SUCCESS = "1000000"
TOTALS_EVERY = 3


class DeyeConnector(Connector):
    platform = "deye"

    def __init__(self, account, session=None):
        super().__init__(account, session)
        self.base = account.get("base_url", BASE).rstrip("/")
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._poll_no = 0
        self._totals: dict[str, dict] = {}
        self._filter = {str(s) for s in account.get("station_ids") or []}

    # -- auth -------------------------------------------------------------------
    def _login(self) -> None:
        a = self.account
        if not (a.get("app_id") and a.get("app_secret") and a.get("password")
                and (a.get("email") or a.get("username"))):
            raise AuthError("deye: set app_id, app_secret, email (or username) and password")
        body: dict[str, Any] = {"appSecret": a["app_secret"],
                                "password": hashlib.sha256(str(a["password"]).encode()).hexdigest()}
        if a.get("email"):
            body["email"] = a["email"]
        else:
            body["username"] = a["username"]
        if a.get("company_id"):
            body["companyId"] = a["company_id"]
        j = self._request("POST", f"{self.base}/v1.0/account/token", params={"appId": a["app_id"]}, json=body)
        if not _ok(j) or not j.get("accessToken"):
            # e.g. {"code":"2101021","msg":"auth invalid appId"}: a refused login pauses polling
            raise AuthError(f"deye: login refused ({j.get('msg') or j.get('code')})")
        self._token = j["accessToken"]
        life = float(j.get("expiresIn") or 3600)
        self._token_exp = time.time() + max(life - 300, 60)

    def _post(self, path: str, body: dict) -> dict:
        for attempt in (0, 1):
            if not self._token or time.time() >= self._token_exp or attempt:
                self._login()
            try:
                j = self._request("POST", f"{self.base}/v1.0/{path}", json=body,
                                  headers={"Authorization": f"Bearer {self._token}"}, auth_statuses=(401,))
            except AuthError:
                if attempt:
                    raise
                self._token = None
                continue
            if _ok(j):
                return j
            if "token" in str(j.get("msg", "")).lower() and not attempt:
                self._token = None
                continue
            raise TransientError(f"deye: {j.get('msg') or 'request failed'} (code {j.get('code')}, {path})")
        raise TransientError("deye: unreachable")  # pragma: no cover

    # -- poll -------------------------------------------------------------------
    def poll(self) -> list[Snapshot]:
        self._poll_no += 1
        out = []
        for st in self._stations():
            sid = pick_str(st, "id", "stationId")
            if not sid or (self._filter and sid not in self._filter):
                continue
            site = SiteInfo(
                external_id=sid, name=pick_str(st, "name", "stationName") or f"Station {sid}",
                capacity_kwp=pick(st, "installedCapacity"),
                timezone=self.account.get("timezone") or pick_str(st, "regionTimezone", "timezone"),
                meta={"address": pick_str(st, "locationAddress", "address")},
            )
            try:
                latest = self._post("station/latest", {"stationId": int(sid) if sid.isdigit() else sid})
                r = parse_latest(latest)
                if sid not in self._totals or self._poll_no % TOTALS_EVERY == 1:
                    try:
                        self._totals[sid] = self._today(sid, site.timezone)
                    except TransientError:
                        pass
                t = self._totals.get(sid) or {}
                r.pv_kwh_today, r.load_kwh_today = t.get("pv"), t.get("load")
                r.import_kwh_today, r.export_kwh_today = t.get("import"), t.get("export")
                r.raw = {"latest": {k: v for k, v in latest.items() if k not in ("requestId",)}, "today": t}
                out.append(Snapshot(site, r))
            except AuthError:
                raise
            except ConnectorError as e:
                out.append(Snapshot(site, error=str(e)))
        return out

    def _stations(self) -> list[dict]:
        out, page = [], 1
        while page <= 20:
            j = self._post("station/list", {"page": page, "size": 100})
            batch = j.get("stationList") or []
            out.extend(batch)
            if len(batch) < 100 or len(out) >= int(pick(j, "total") or 0):
                break
            page += 1
        return out

    def _today(self, sid: str, tz: Optional[str]) -> dict:
        day = _local_today(tz)
        j = self._post("station/history", {"stationId": int(sid) if sid.isdigit() else sid,
                                           "granularity": 2, "startAt": day, "endAt": day})
        items = j.get("stationDataItems") or []
        it = items[-1] if items else {}
        pv, imp, exp = pick(it, "generationValue"), pick(it, "purchaseValue"), pick(it, "gridValue")
        chg, dis = pick(it, "chargeValue"), pick(it, "dischargeValue")
        # Deye's consumptionValue is a residual of its own; the energy balance is more reliable
        if None not in (pv, imp, exp):
            load = max(0.0, pv + imp - exp + (dis or 0) - (chg or 0))
        else:
            load = pick(it, "consumptionValue")
        return {"pv": pv, "load": load, "import": imp, "export": exp}


def parse_latest(d: dict) -> Reading:
    batt = pick(d, "batteryPower")
    if batt is None:   # mirrors, populated only when non-zero
        c, dc = pick(d, "chargePower"), pick(d, "dischargePower")
        batt = None if c is None and dc is None else (dc or 0) - (c or 0)
    grid = pick(d, "wirePower")
    if grid is None:
        imp, exp = pick(d, "purchasePower"), pick(d, "gridPower")
        grid = None if imp is None and exp is None else (imp or 0) - (exp or 0)
    ts = pick(d, "lastUpdateTime")
    pv = pick(d, "generationPower")
    load = pick(d, "consumptionPower")
    return Reading(
        pv_w=abs(pv) if pv is not None else None,
        load_w=abs(load) if load is not None else None,
        battery_w=-batt if batt is not None else None,   # Deye: + discharge -> ours: + charging
        grid_w=grid,                                      # Deye: + import (same as ours)
        soc=pick(d, "batterySOC", "batterySoc"),
        reported_at=(ts / 1000 if ts and ts > 1e12 else ts),
    )


def _ok(j: Any) -> bool:
    return isinstance(j, dict) and (j.get("success") is True or str(j.get("code")) == SUCCESS)


def _local_today(tz: Optional[str]) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).date().isoformat() if tz else datetime.now().date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()
