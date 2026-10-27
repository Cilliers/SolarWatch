"""GoodWe SEMS+ (semsplus.goodwe.com) - the newer GoodWe portal and its "gateway" API.

Use this for stations that live on SEMS+ (the legacy `goodwe` connector returns empty
data for them). Unofficial: reverse-engineered from the SEMS+ web app by the community.

  POST {login_host}/web/sems/sems-user/api/v1/auth/cross-login
       body {account, pwd: base64(md5_hex(password)), agreement:1, isChinese:false, isLocal:false}
  every call carries:
       token:       JSON {uid, timestamp, token, client:"semsPlusWeb", ...}
       x-signature: base64( sha256_hex(f"{ts}@{uid}@{token}") + "@" + ts )
  POST {gateway}/web/sems/sems-plant/api/stations/simple-query      station list (owners)
  POST {gateway}/web/sems/sems-plant/api/portal/stations/page       station list (installers)
  GET  {gateway}/web/sems/sems-plant/api/stations/flow?stationId=   live flow, in kW

Flow signs as reported by SEMS+: pGrid > 0 exporting, pBat > 0 discharging.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from ..models import Reading, SiteInfo, Snapshot, pick, pick_str, to_float
from .base import AuthError, Connector, ConnectorError, TransientError

LOGIN_HOST = "https://semsplus.goodwe.com"
LOGIN_PATH = "/web/sems/sems-user/api/v1/auth/cross-login"
DEFAULT_GATEWAY = "https://eu-gateway.semsportal.com"
CLIENT = "semsPlusWeb"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
OK_CODES = {0, "0", "00000", 200, "200"}
EXPIRED_CODES = {100002, "100002", "C0602", 100001, "100001"}
PLANT = "/web/sems/sems-plant/api"


def hash_password(pwd: str) -> str:
    return base64.b64encode(hashlib.md5(pwd.encode()).hexdigest().encode()).decode()


def signature(uid: str, token: str, ts_ms: Optional[int] = None) -> str:
    ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
    digest = hashlib.sha256(f"{ts}@{uid}@{token}".encode()).hexdigest()
    return base64.b64encode(f"{digest}@{ts}".encode()).decode()


class GoodWePlusConnector(Connector):
    platform = "goodwe_plus"

    def __init__(self, account, session=None):
        super().__init__(account, session)
        self.session.headers["User-Agent"] = BROWSER_UA
        self.login_host = account.get("login_host", LOGIN_HOST).rstrip("/")
        self._gateway_cfg = account.get("gateway")
        self._gateway = (self._gateway_cfg or DEFAULT_GATEWAY).rstrip("/")
        self._sess: Optional[dict] = None
        self._station_endpoint: Optional[str] = None
        self._filter = {str(s) for s in account.get("station_ids") or []}

    # -- auth -------------------------------------------------------------------
    def _login(self) -> None:
        user, pwd = self.account.get("username"), self.account.get("password")
        if not user or not pwd:
            raise AuthError("goodwe_plus: set username and password")
        placeholder = {"uid": "", "timestamp": 0, "token": "", "client": CLIENT, "version": "", "language": "en"}
        j = self._request("POST", self.login_host + LOGIN_PATH, json={
            "account": user, "pwd": hash_password(pwd), "agreement": 1, "isChinese": False, "isLocal": False,
        }, headers={
            "Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
            "Origin": self.login_host, "Referer": self.login_host + "/",
            "token": json.dumps(placeholder, separators=(",", ":")), "x-signature": signature("", ""),
        })
        data = (j or {}).get("data") or {}
        if j.get("code") not in OK_CODES or not data.get("token") or not data.get("uid"):
            raise AuthError(f"goodwe_plus: login refused ({j.get('msg') or j.get('description') or j.get('code')})")
        self._sess = {"uid": str(data["uid"]), "token": str(data["token"]),
                      "timestamp": data.get("timestamp") or int(time.time() * 1000)}
        api = j.get("api") or data.get("api")
        if not self._gateway_cfg and isinstance(api, str):
            host = urlparse(api).hostname or ""
            # only ever send the token to GoodWe's own hosts
            if urlparse(api).scheme == "https" and (host.endswith(".semsportal.com") or host.endswith(".goodwe.com")):
                self._gateway = f"https://{host}"

    def _headers(self) -> dict[str, str]:
        s = self._sess or {}
        tok = {"uid": s["uid"], "timestamp": str(s["timestamp"]), "token": s["token"], "client": CLIENT,
               "version": "", "language": "en", "api": self._gateway + "/web/sems"}
        return {"Accept": "application/json, text/plain, */*", "neutral": "0", "currentLang": "en",
                "token": json.dumps(tok, separators=(",", ":")), "x-signature": signature(s["uid"], s["token"])}

    def _call(self, method: str, path: str, *, params=None, body=None) -> Any:
        for attempt in (0, 1):
            if self._sess is None or attempt:
                self._login()
            j = self._request(method, self._gateway + path, params=params, json=body, headers=self._headers())
            code = (j or {}).get("code")
            if code in OK_CODES:
                return j.get("data")
            if code in EXPIRED_CODES and not attempt:
                self._sess = None
                continue
            raise TransientError(f"goodwe_plus: {j.get('msg') or j.get('description') or 'request failed'} "
                                 f"(code {code}, {path.rsplit('/', 1)[-1]})")
        raise AuthError("goodwe_plus: session keeps expiring")

    # -- poll -------------------------------------------------------------------
    def poll(self) -> list[Snapshot]:
        out = []
        for st in self._station_list():
            site = SiteInfo(external_id=st["id"], name=st["name"], capacity_kwp=st.get("kwp"),
                            timezone=self.account.get("timezone"), meta={"address": st.get("address")})
            if self._filter and site.external_id not in self._filter:
                continue
            try:
                flow = self._call("GET", f"{PLANT}/stations/flow", params={"stationId": site.external_id})
                r = parse_flow(flow or {}, site.timezone)
                r.pv_kwh_today = st.get("today_kwh")
                r.raw = {"flow": flow, "station": st.get("raw")}
                out.append(Snapshot(site, r))
            except AuthError:
                raise
            except ConnectorError as e:
                out.append(Snapshot(site, error=str(e)))
        return out

    def _station_list(self) -> list[dict]:
        """Fetched every poll: it is also where today's yield comes from."""
        rows: list[dict] = []
        if self._station_endpoint in (None, "simple"):
            d = self._call("POST", f"{PLANT}/stations/simple-query", body={"pageIndex": 1, "pageSize": 100})
            rows = _rows(d)
            if rows:
                self._station_endpoint = "simple"
        if not rows:   # installer accounts get nothing from simple-query
            self._station_endpoint = "page"
            page = 1
            while page <= 20:
                d = self._call("POST", f"{PLANT}/portal/stations/page", body={"current": page, "size": 50})
                batch = _rows(d)
                rows.extend(batch)
                total = int(to_float((d or {}).get("total")) or 0) if isinstance(d, dict) else 0
                if not batch or len(rows) >= total:
                    break
                page += 1
        stations = []
        for s in rows:
            sid = pick_str(s, "stationId", "powerStationId", "id")
            if not sid:
                continue
            stations.append({
                "id": sid, "name": pick_str(s, "stationName", "name", "stationname") or sid,
                "kwp": pick(s, "capacity", "installedCapacity"),
                "today_kwh": pick(s, "productionToday", "eDay", "eday", "todayPower"),
                "address": pick_str(s, "address", "location"), "raw": s,
            })
        if not stations:
            raise TransientError("goodwe_plus: no stations found for this account (set station_ids?)")
        return stations


def _rows(d: Any) -> list[dict]:
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("dataList", "list", "records", "rows"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def parse_flow(d: dict, tz: Optional[str] = None) -> Reading:
    kw = lambda *k: (lambda v: None if v is None else v * 1000)(pick(d, *k))
    pv = kw("pSystem", "pPv")
    load = kw("pConsum", "pLoad")
    grid = kw("pGrid")
    batt = kw("pBat")
    if pv is None and load is None:
        # SEMS+ returns only pAc (or nothing) while a station is idle/offline
        pv = kw("pAc")
    r = Reading(
        pv_w=abs(pv) if pv is not None else None,
        load_w=abs(load) if load is not None else None,
        grid_w=-grid if grid is not None else None,      # SEMS+: + export  -> ours: + import
        battery_w=-batt if batt is not None else None,   # SEMS+: + discharge -> ours: + charging
        soc=pick(d, "soc"),
        reported_at=_parse_time(d.get("refreshTime"), tz),
    )
    status = pick_str(d, "status")
    if status in ("-1", "offline", "Offline"):
        r.device_status = "offline"
    return r


def _parse_time(v: Any, tz: Optional[str]) -> Optional[float]:
    n = to_float(v)
    if n and n > 1e12:
        return n / 1000
    if n and n > 1e9 and not isinstance(v, str):
        return n
    if isinstance(v, str) and v.strip():
        s = v.strip().replace("T", " ").replace("Z", "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(s[:19], fmt)
            except ValueError:
                continue
            try:
                zone = ZoneInfo(tz) if tz else datetime.now().astimezone().tzinfo
            except Exception:
                zone = datetime.now().astimezone().tzinfo
            return dt.replace(tzinfo=zone).timestamp()
    return None
