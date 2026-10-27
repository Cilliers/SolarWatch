"""Victron VRM (official, documented API v2).

Auth: a personal access token (VRM -> Preferences -> Integrations -> Access tokens) is
preferred; username/password login is supported for accounts without one.

Per poll:
  GET /users/me                                 (once, cached)
  GET /users/{id}/installations                 site list
  GET /installations/{site}/diagnostics         live values, per site
  GET /installations/{site}/stats?type=kwh      today's energy, per site
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..models import Reading, SiteInfo, Snapshot, to_float
from .base import AuthError, Connector, ConnectorError, TransientError

BASE = "https://vrmapi.victronenergy.com/v2"

# How a value is found in /diagnostics, most reliable first. Each record carries
# dbusServiceType + dbusPath (the GX device's own D-Bus names, the same on every site and in
# every language) as well as a human description, which varies by firmware and system.
#   system   - the GX "system overview" totals (com.victronenergy.system)
#   battery  - a battery monitor / BMS (BMV, SmartShunt, Lynx, managed lithium)
#   vebus    - MultiPlus / Quattro, which estimates SoC when there is no battery monitor
_SYS = "system"
_PV_PATHS = re.compile(r"^/(Dc/Pv/Power|Ac/PvOn(Output|Grid|Genset)/L\d/Power)$")
_LOAD_PATHS = re.compile(r"^/Ac/Consumption/L\d/Power$")
_GRID_PATHS = re.compile(r"^/Ac/Grid/L\d/Power$")
# description fallbacks (older VRM payloads without dbus fields)
_PV = re.compile(r"^PV - (DC-coupled|AC-coupled on (output|input|generator)( L\d)?)$", re.I)
_LOAD = re.compile(r"^AC Consumption L\d$", re.I)
_GRID = re.compile(r"^Grid L\d$", re.I)
_SOC_SYS = re.compile(r"^Battery (State of Charge|SOC) \(System\)$", re.I)
_SOC_ANY = re.compile(r"(state of charge|\bsoc\b)", re.I)
_SOC_NOT = re.compile(r"(minimum|limit|min |max |target|active soc limit)", re.I)
_BATT_P = re.compile(r"^Battery Power \(System\)$", re.I)
_BATT_V = re.compile(r"^Battery Voltage \(System\)$", re.I)
_BATT_I = re.compile(r"^Battery Current \(System\)$", re.I)
SOC_ATTRIBUTE_ID = 51   # battery monitor "State of charge"


class VictronConnector(Connector):
    platform = "victron"

    def __init__(self, account, session=None):
        super().__init__(account, session)
        self.base = account.get("base_url", BASE).rstrip("/")
        self._auth_header: Optional[str] = None
        self._user_id: Optional[int] = account.get("user_id")
        self._site_filter = {str(s) for s in account.get("site_ids") or []}

    # -- auth -------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        if not self._auth_header:
            token = self.account.get("access_token")
            if token:
                self._auth_header = f"Token {token}"
            else:
                self._login()
        return {"X-Authorization": self._auth_header}

    def _login(self) -> None:
        user, pwd = self.account.get("username"), self.account.get("password")
        if not user or not pwd:
            raise AuthError("victron: set access_token, or username and password")
        j = self._request("POST", f"{self.base}/auth/login", json={"username": user, "password": pwd})
        if not j.get("token"):
            raise AuthError("victron: login refused")
        self._auth_header = f"Bearer {j['token']}"
        self._user_id = self._user_id or j.get("idUser")

    def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            j = self._request("GET", f"{self.base}{path}", headers=self._headers(), params=params)
        except AuthError:
            # A bearer token may have expired; log in once more. Access tokens don't expire.
            if self.account.get("access_token"):
                raise
            self._auth_header = None
            j = self._request("GET", f"{self.base}{path}", headers=self._headers(), params=params)
        if isinstance(j, dict) and j.get("success") is False:
            raise TransientError(f"victron: {j.get('errors') or j.get('error_code') or 'request failed'}")
        return j

    # -- poll -------------------------------------------------------------------
    def poll(self) -> list[Snapshot]:
        if self._user_id is None:
            me = self._get("/users/me")
            self._user_id = (me.get("user") or {}).get("id")
            if self._user_id is None:
                raise TransientError("victron: /users/me returned no user id")
        recs = self._get(f"/users/{self._user_id}/installations").get("records", [])
        out = []
        for r in recs:
            site = SiteInfo(
                external_id=str(r["idSite"]),
                name=r.get("name") or f"VRM {r['idSite']}",
                timezone=r.get("timezone"),
                meta={"identifier": r.get("identifier"), "device_icon": r.get("device_icon")},
            )
            if self._site_filter and site.external_id not in self._site_filter:
                continue
            try:
                out.append(Snapshot(site, self._read(site)))
            except AuthError:
                raise
            except ConnectorError as e:
                out.append(Snapshot(site, error=str(e)))
        return out

    def _read(self, site: SiteInfo) -> Reading:
        diag = self._get(f"/installations/{site.external_id}/diagnostics", {"count": 1000})
        reading = parse_diagnostics(diag.get("records", []))
        try:
            start = _local_midnight(site.timezone)
            stats = self._get(f"/installations/{site.external_id}/stats",
                              {"type": "kwh", "interval": "days", "start": start, "end": int(time.time())})
            apply_kwh_totals(reading, stats.get("totals") or {})
        except TransientError:
            pass  # live values are still worth keeping without today's totals
        recs = diag.get("records", [])
        reading.raw = {"diagnostics_count": len(recs),
                       "columns": ["description", "dbusServiceType", "dbusPath", "instance",
                                   "idDataAttribute", "rawValue"],
                       "records": diagnostics_summary(recs)}
        return reading


class _Rec:
    __slots__ = ("desc", "service", "path", "attr", "instance", "value", "ts")

    def __init__(self, r: dict):
        self.desc = (r.get("description") or "").strip()
        self.service = str(r.get("dbusServiceType") or "").lower()
        self.path = str(r.get("dbusPath") or "")
        self.attr = to_float(r.get("idDataAttribute"))
        self.instance = to_float(r.get("instance"))
        self.value = to_float(r.get("rawValue"))
        self.ts = to_float(r.get("timestamp"))


def parse_diagnostics(records: list[dict]) -> Reading:
    recs = [x for x in (_Rec(r) for r in records) if x.value is not None]
    latest_ts = max((x.ts for x in recs if x.ts), default=None)

    def total(path_rx, desc_rx):
        """Sum per-phase/per-source values; dbus paths first, description as fallback."""
        by_path: dict[str, float] = {}
        for x in recs:
            if x.service == _SYS and path_rx.match(x.path):
                by_path.setdefault(x.path, x.value)     # first record wins on duplicates
        if by_path:
            return sum(by_path.values())
        by_desc: dict[str, float] = {}
        for x in recs:
            if desc_rx.match(x.desc):
                by_desc.setdefault(x.desc, x.value)
        return sum(by_desc.values()) if by_desc else None

    def first(pred):
        hits = sorted((x for x in recs if pred(x)), key=lambda x: (x.instance is None, x.instance or 0))
        return hits[0].value if hits else None

    # Battery state of charge, from the most authoritative source available
    soc = first(lambda x: x.service == _SYS and x.path == "/Dc/Battery/Soc")
    if soc is None:
        soc = first(lambda x: _SOC_SYS.match(x.desc))
    if soc is None:
        soc = first(lambda x: (x.service == "battery" and x.path == "/Soc") or x.attr == SOC_ATTRIBUTE_ID)
    if soc is None:
        soc = first(lambda x: x.service == "vebus" and x.path == "/Soc")
    if soc is None:   # last resort: any state-of-charge reading that isn't a limit/setting
        soc = first(lambda x: _SOC_ANY.search(x.desc) and not _SOC_NOT.search(x.desc) and 0 <= x.value <= 100)

    # Battery power (+ charging): system total, else battery monitor, else V x I
    batt = first(lambda x: x.service == _SYS and x.path == "/Dc/Battery/Power")
    if batt is None:
        batt = first(lambda x: _BATT_P.match(x.desc))
    if batt is None:
        batt = first(lambda x: x.service == "battery" and x.path == "/Dc/0/Power")
    if batt is None:
        v = first(lambda x: (x.service == _SYS and x.path == "/Dc/Battery/Voltage") or _BATT_V.match(x.desc))
        i = first(lambda x: (x.service == _SYS and x.path == "/Dc/Battery/Current") or _BATT_I.match(x.desc))
        if v is None or i is None:
            v = first(lambda x: x.service == "battery" and x.path == "/Dc/0/Voltage")
            i = first(lambda x: x.service == "battery" and x.path == "/Dc/0/Current")
        batt = v * i if v is not None and i is not None else None

    pv = total(_PV_PATHS, _PV)
    return Reading(
        pv_w=max(pv, 0.0) if pv is not None else (0.0 if recs else None),
        load_w=total(_LOAD_PATHS, _LOAD),
        battery_w=batt,     # VRM: + charging, - discharging (matches ours)
        grid_w=total(_GRID_PATHS, _GRID),   # VRM: + import, - export (matches ours)
        soc=soc,
        reported_at=latest_ts,
    )


def diagnostics_summary(records: list[dict]) -> list[list]:
    """Compact copy of every attribute, shown under 'Platform data' to debug mappings."""
    return [[r.get("description"), r.get("dbusServiceType"), r.get("dbusPath"), r.get("instance"),
             r.get("idDataAttribute"), r.get("rawValue")] for r in records]


def apply_kwh_totals(reading: Reading, t: dict) -> None:
    """VRM's kwh stats split energy by source->destination:
    P=PV, B=battery, G=grid; c=consumers. e.g. Pb = PV to battery."""
    g = lambda k: to_float(t.get(k)) or 0.0
    if not t:
        return
    reading.pv_kwh_today = g("Pb") + g("Pc") + g("Pg")
    reading.load_kwh_today = g("Pc") + g("Bc") + g("Gc")
    reading.import_kwh_today = g("Gc") + g("Gb")
    reading.export_kwh_today = g("Pg") + g("Bg")


def _local_midnight(tz: Optional[str]) -> int:
    try:
        zone = ZoneInfo(tz) if tz else None
    except Exception:
        zone = None
    now = datetime.now(zone) if zone else datetime.now().astimezone()
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
