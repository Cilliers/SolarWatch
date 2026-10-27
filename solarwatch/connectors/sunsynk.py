"""Sunsynk Connect (unofficial API behind the Sunsynk app and sunsynk.net).

Login flow as of Oct 2026 (it has changed before):
  1. GET  /anonymous/publicKey?source=sunsynk&nonce=<ms>&sign=md5("nonce=<ms>&source=sunsynkPOWER_VIEW")
  2. RSA PKCS#1 v1.5 encrypt the password with that key
  3. POST /oauth/token/new {client_id:"csp-web", grant_type:"password", username, password,
          source:"sunsynk", nonce, sign=md5("nonce=<ms>&source=sunsynk" + key[:10])}

Data per poll:
  GET /api/v1/plants                         all plants + today's PV kWh (one call)
  GET /api/v1/plant/energy/{id}/flow         live power flow, per plant
  GET /api/v1/plant/{id}/inverters           inverter serials (cached)
  GET /api/v1/inverter/{battery,grid,load}/{sn}/realtime   today's totals, per inverter

Deye / Inteless accounts use base_url https://pv.inteless.com.
MFA-enabled accounts are not supported by the API; use an account without MFA.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from datetime import datetime, date
from typing import Any, Optional
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

from ..models import Reading, SiteInfo, Snapshot, to_float
from .base import AuthError, Connector, ConnectorError, TransientError

BASE = "https://api.sunsynk.net"
CLIENT_ID = "csp-web"
TOKEN_MARGIN = 300  # seconds: log in again a little before expiry
TOTALS_EVERY = 3    # fetch daily totals every Nth poll; they move slowly


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def public_key_sign(nonce: int, source: str) -> str:
    return md5(f"nonce={nonce}&source={source}POWER_VIEW")


def token_sign(nonce: int, source: str, key: str) -> str:
    return md5(f"nonce={nonce}&source={source}{key[:10]}")


def rsa_encrypt(key: str, text: str) -> str:
    der = base64.b64decode(re.sub(r"-----[^-]+-----|\s+", "", key))
    pub = load_der_public_key(der)
    return base64.b64encode(pub.encrypt(text.encode(), padding.PKCS1v15())).decode()


class SunsynkConnector(Connector):
    platform = "sunsynk"

    def __init__(self, account, session=None):
        super().__init__(account, session)
        self.base = account.get("base_url", BASE).rstrip("/")
        self.source = account.get("source", "sunsynk")
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._inverters: dict[str, list[str]] = {}
        self._totals: dict[str, dict] = {}
        self._poll_no = 0
        self._plant_filter = {str(s) for s in account.get("plant_ids") or []}

    # -- auth -------------------------------------------------------------------
    def _login(self) -> None:
        user, pwd = self.account.get("username"), self.account.get("password")
        if not user or not pwd:
            raise AuthError("sunsynk: set username and password")
        n1 = int(time.time() * 1000)
        pk = self._request("GET", f"{self.base}/anonymous/publicKey", params={
            "source": self.source, "nonce": n1, "sign": public_key_sign(n1, self.source)})
        data = pk.get("data") if isinstance(pk, dict) else None
        key = data if isinstance(data, str) else (data or {}).get("publicKey") or (data or {}).get("key")
        if pk.get("code") != 0 or not key:
            raise TransientError(f"sunsynk: no login key ({pk.get('msg', 'unknown reason')})")
        key = key.strip()
        n2 = int(time.time() * 1000)
        body = {
            "client_id": CLIENT_ID, "grant_type": "password", "username": user,
            "password": rsa_encrypt(key, pwd), "source": self.source,
            "nonce": n2, "sign": token_sign(n2, self.source, key),
        }
        tok = self._request("POST", f"{self.base}/oauth/token/new", json=body)
        d = (tok or {}).get("data") or {}
        if tok.get("code") != 0 or not d.get("access_token"):
            msg = tok.get("msg") or "login refused"
            if "interface" in msg.lower():   # "Invalid login interface request": signing scheme changed
                raise TransientError(f"sunsynk: {msg} - the login scheme may have changed")
            raise AuthError(f"sunsynk: {msg}")
        self._token = d["access_token"]
        life = to_float(d.get("expires_in")) or 3600
        self._token_exp = time.time() + max(life - TOKEN_MARGIN, 60)

    def _get(self, path: str, params: dict | None = None) -> Any:
        for attempt in (0, 1):
            if not self._token or time.time() >= self._token_exp or attempt:
                self._login()
            try:
                j = self._request("GET", f"{self.base}{path}", params=params,
                                  headers={"Authorization": f"Bearer {self._token}"}, auth_statuses=(401,))
            except AuthError:
                if attempt:
                    raise
                self._token = None
                continue
            if j.get("code") != 0:
                raise TransientError(f"sunsynk: {j.get('msg') or 'request refused'} ({path.split('?')[0]})")
            return j.get("data")
        raise TransientError("sunsynk: unreachable")  # pragma: no cover

    # -- poll -------------------------------------------------------------------
    def poll(self) -> list[Snapshot]:
        self._poll_no += 1
        plants = self._plants()
        out = []
        for p in plants:
            site = SiteInfo(
                external_id=str(p["id"]),
                name=p.get("name") or f"Plant {p['id']}",
                capacity_kwp=to_float(p.get("totalPower")),
                timezone=self.account.get("timezone"),
                meta={"address": p.get("address")},
            )
            if self._plant_filter and site.external_id not in self._plant_filter:
                continue
            try:
                out.append(Snapshot(site, self._read(site, p)))
            except AuthError:
                raise
            except ConnectorError as e:
                out.append(Snapshot(site, error=str(e)))
        return out

    def _plants(self) -> list[dict]:
        out, page = [], 1
        while True:
            d = self._get("/api/v1/plants", {"page": page, "limit": 50, "name": "", "status": ""}) or {}
            infos = d.get("infos") or []
            out.extend(infos)
            if not infos or len(out) >= int(d.get("total") or 0) or page >= 40:
                return out
            page += 1

    def _read(self, site: SiteInfo, plant: dict) -> Reading:
        today = _local_date(site.timezone).isoformat()
        flow = self._get(f"/api/v1/plant/energy/{site.external_id}/flow", {"date": today}) or {}
        r = normalize_flow(flow)
        r.pv_kwh_today = to_float(plant.get("etoday"))
        r.reported_at = parse_time(plant.get("updateAt"))
        r.device_status = _plant_status(plant.get("status"))
        if site.external_id not in self._totals or self._poll_no % TOTALS_EVERY == 1:
            try:
                self._totals[site.external_id] = self._daily_totals(site.external_id)
            except TransientError:
                pass
        t = self._totals.get(site.external_id) or {}
        r.load_kwh_today, r.import_kwh_today, r.export_kwh_today = t.get("load"), t.get("import"), t.get("export")
        r.raw = {"flow": flow, "plant": {k: plant.get(k) for k in ("status", "pac", "etoday", "updateAt")}}
        return r

    def _daily_totals(self, plant_id: str) -> dict:
        if plant_id not in self._inverters:
            d = self._get(f"/api/v1/plant/{plant_id}/inverters",
                          {"page": 1, "limit": 20, "status": -1, "sn": "", "id": plant_id, "type": -2}) or {}
            self._inverters[plant_id] = [str(i["sn"]) for i in d.get("infos", []) if i.get("sn")]
        load = imp = exp = None
        add = lambda a, b: b if a is None else a + (b or 0)
        for sn in self._inverters[plant_id]:
            g = self._get(f"/api/v1/inverter/grid/{sn}/realtime", {"sn": sn}) or {}
            ld = self._get(f"/api/v1/inverter/load/{sn}/realtime", {"sn": sn}) or {}
            imp = add(imp, _pick(g, "etodayFrom", "etodayBuy"))
            exp = add(exp, _pick(g, "etodayTo", "etodaySell"))
            load = add(load, _pick(ld, "dailyUsed", "etodayUsed"))
        return {"load": load, "import": imp, "export": exp}


def normalize_flow(d: dict) -> Reading:
    """Sunsynk gives magnitudes plus direction flags (toBat/batTo, gridTo/toGrid)."""
    pv = abs(_pick(d, "pvPower") or 0)
    load = abs(_pick(d, "loadOrEpsPower", "loadPower", "homeLoadPower") or 0)
    braw = _pick(d, "battPower", "batteryPower") or 0
    if d.get("toBat") is True:
        batt = abs(braw)
    elif d.get("batTo") is True:
        batt = -abs(braw)
    elif d.get("toBat") is False and d.get("batTo") is False:
        batt = 0.0
    else:
        batt = -braw   # no flags: Sunsynk's own sign is + discharging
    graw = _pick(d, "gridOrMeterPower", "gridPower") or 0
    if d.get("gridTo") is True:
        grid = abs(graw)
    elif d.get("toGrid") is True:
        grid = -abs(graw)
    elif d.get("gridTo") is False and d.get("toGrid") is False:
        grid = 0.0
    else:
        grid = graw
    return Reading(pv_w=pv, load_w=load, battery_w=batt, grid_w=grid, soc=_pick(d, "soc"))


def parse_time(s: Any) -> Optional[float]:
    """Sunsynk timestamps carry a zone ("2026-10-06T00:31:27Z"). Without one, refuse to guess."""
    if not isinstance(s, str) or not re.search(r"(Z|[+-]\d{2}:?\d{2})$", s.strip()):
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _plant_status(code: Any) -> str:
    return {1: "normal", 2: "offline", 3: "fault"}.get(int(to_float(code) or -1), "unknown")


def _pick(d: dict, *keys: str) -> Optional[float]:
    for k in keys:
        v = to_float(d.get(k))
        if v is not None:
            return v
    return None


def _local_date(tz: Optional[str]) -> date:
    try:
        return datetime.now(ZoneInfo(tz)).date() if tz else datetime.now().date()
    except Exception:
        return datetime.now().date()
