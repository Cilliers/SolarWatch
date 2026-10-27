"""LuxPower Tek (LXP / SNA inverters) via the Luxpower web monitor (unofficial "WManage" API).

Same login as the LuxPowerTek web portal / app. Also works for EG4 accounts
(base_url https://monitor.eg4electronics.com).

  POST {base}/WManage/api/login                      form {account, password, language}  (session cookie)
       -> plants[] with inverters[] (serialNum)
  POST {base}/WManage/web/config/plant/list/viewer   form, fallback when login lists no plants
  POST {base}/WManage/api/inverterOverview/list      form {plantId, ...} -> serials for that plant
  POST {base}/WManage/api/inverter/getInverterRuntime     form {serialNum} -> live W
  POST {base}/WManage/api/inverter/getInverterEnergyInfo  form {serialNum} -> today, 0.1 kWh units

Regional servers: af.luxpowertek.com (Africa / Middle East), eu., na., sea., server. (China).
An expired session is answered with the HTML login page; the connector logs in again once.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from ..models import Reading, SiteInfo, Snapshot, pick, pick_str
from .base import AuthError, Connector, ConnectorError, TransientError

BASE = "https://af.luxpowertek.com"
TOTALS_EVERY = 3


class LuxPowerConnector(Connector):
    platform = "luxpower"

    def __init__(self, account, session=None):
        super().__init__(account, session)
        self.base = account.get("base_url", BASE).rstrip("/")
        self._logged_in_at = 0.0
        self._plants: Optional[list[dict]] = None
        self._poll_no = 0
        self._totals: dict[str, dict] = {}
        self._filter = {str(s) for s in account.get("plant_ids") or []}

    # -- transport --------------------------------------------------------------
    def _raw(self, path: str, data: dict) -> Optional[dict]:
        """POST a form; returns parsed JSON, or None when the server sent its login page."""
        url = self.base + path
        try:
            resp = self.session.request("POST", url, data=data, timeout=self.timeout,
                                        headers={"Accept": "application/json, text/javascript, */*"})
        except requests.RequestException as e:
            raise TransientError(f"luxpower: could not reach {self.base} ({e.__class__.__name__})") from e
        if resp.status_code in (401, 403):
            return None
        if resp.status_code == 429 or resp.status_code >= 400:
            raise TransientError(f"luxpower: HTTP {resp.status_code} from {path.rsplit('/', 1)[-1]}")
        try:
            return resp.json()
        except ValueError:
            return None    # HTML login page: session gone

    def _login(self) -> dict:
        user, pwd = self.account.get("username"), self.account.get("password")
        if not user or not pwd:
            raise AuthError("luxpower: set username and password")
        j = self._raw("/WManage/api/login", {"account": user, "password": pwd, "language": "ENGLISH"})
        if j is None:
            raise TransientError("luxpower: login page answered with HTML (wrong base_url?)")
        if not j.get("success"):
            raise AuthError(f"luxpower: login refused ({j.get('msg') or j.get('message') or 'no reason given'})")
        self._logged_in_at = time.time()
        return j

    def _post(self, path: str, data: dict) -> dict:
        for attempt in (0, 1):
            if not self._logged_in_at or time.time() - self._logged_in_at > 90 * 60 or attempt:
                login = self._login()
                if self._plants is None:
                    self._plants = _plants_from_login(login)
            j = self._raw(path, data)
            if j is None:
                self._logged_in_at = 0
                continue
            if j.get("success") is False:
                raise TransientError(f"luxpower: {j.get('msg') or j.get('message') or 'request refused'} "
                                     f"({path.rsplit('/', 1)[-1]})")
            return j
        raise TransientError("luxpower: session keeps expiring")

    # -- poll -------------------------------------------------------------------
    def poll(self) -> list[Snapshot]:
        self._poll_no += 1
        out = []
        for p in self._plant_list():
            pid = p["id"]
            if self._filter and pid not in self._filter:
                continue
            site = SiteInfo(external_id=pid, name=p["name"], timezone=self.account.get("timezone"),
                            capacity_kwp=p.get("kwp"), meta={"inverters": p["serials"]})
            try:
                out.append(Snapshot(site, self._read(p)))
            except AuthError:
                raise
            except ConnectorError as e:
                out.append(Snapshot(site, error=str(e)))
        return out

    def _plant_list(self) -> list[dict]:
        if self._plants is None:
            self._plants = _plants_from_login(self._login())
        if not self._plants:   # installer/viewer accounts may get no plants in the login reply
            d = self._post("/WManage/web/config/plant/list/viewer",
                           {"sort": "createDate", "order": "desc", "searchText": "", "page": 1, "rows": 100})
            self._plants = [{"id": str(r["plantId"]), "name": r.get("name") or str(r["plantId"]),
                             "kwp": (pick(r, "nominalPower") or 0) / 1000 or None,
                             "tz_minutes": None, "serials": []}
                            for r in d.get("rows") or [] if r.get("plantId") is not None]
        for p in self._plants:
            if not p["serials"]:
                d = self._post("/WManage/api/inverterOverview/list",
                               {"page": 1, "rows": 30, "plantId": p["id"], "searchText": "", "statusText": "all"})
                p["serials"] = [str(r["serialNum"]) for r in d.get("rows") or [] if r.get("serialNum")]
        if not self._plants:
            raise TransientError("luxpower: no plants found for this account")
        return self._plants

    def _read(self, p: dict) -> Reading:
        if not p["serials"]:
            raise TransientError("luxpower: plant has no inverters")
        runtimes = [self._post("/WManage/api/inverter/getInverterRuntime", {"serialNum": sn}) for sn in p["serials"]]
        r = combine_runtimes(runtimes, p.get("tz_minutes"))
        if p["id"] not in self._totals or self._poll_no % TOTALS_EVERY == 1:
            try:
                infos = [self._post("/WManage/api/inverter/getInverterEnergyInfo", {"serialNum": sn})
                         for sn in p["serials"]]
                self._totals[p["id"]] = combine_energy(infos)
            except TransientError:
                pass
        t = self._totals.get(p["id"]) or {}
        r.pv_kwh_today, r.load_kwh_today = t.get("pv"), t.get("load")
        r.import_kwh_today, r.export_kwh_today = t.get("import"), t.get("export")
        r.raw = {"runtime": runtimes, "energy_today": t}
        return r


def _plants_from_login(j: dict) -> list[dict]:
    out = []
    for p in j.get("plants") or []:
        if p.get("plantId") is None:
            continue
        tz = None
        if p.get("timezoneHourOffset") is not None:
            h, m = int(p.get("timezoneHourOffset") or 0), int(p.get("timezoneMinuteOffset") or 0)
            tz = h * 60 + (m if h >= 0 else -m)
        out.append({"id": str(p["plantId"]), "name": p.get("name") or str(p["plantId"]), "kwp": None,
                    "tz_minutes": tz,
                    "serials": [str(i["serialNum"]) for i in p.get("inverters") or [] if i.get("serialNum")]})
    return out


def combine_runtimes(rts: list[dict], tz_minutes: Optional[int]) -> Reading:
    """Sum a plant's inverters. Luxpower gives magnitudes: pCharge/pDisCharge, pToUser (grid
    import) / pToGrid (export). Its consumptionPower is unreliable on several models, so load
    comes from the energy balance."""
    pv = chg = dis = imp = exp = 0.0
    socs, seen, lost, fault = [], False, 0, False
    reported = None
    for rt in rts:
        if rt.get("lost"):
            lost += 1
            reported = _device_time(rt.get("deviceTime"), tz_minutes) if reported is None else reported
            continue
        seen = True
        ppv = pick(rt, "ppv")
        if ppv is None:
            ppv = sum(pick(rt, f"ppv{i}") or 0 for i in range(1, 7))
        pv += ppv
        chg += pick(rt, "pCharge") or 0
        dis += pick(rt, "pDisCharge") or 0
        imp += pick(rt, "pToUser") or 0
        exp += pick(rt, "pToGrid") or 0
        if pick(rt, "soc") is not None:
            socs.append(pick(rt, "soc"))
        if "fault" in (pick_str(rt, "statusText") or "").lower():
            fault = True
    if not seen:
        return Reading(device_status="offline", reported_at=reported, pv_w=0, load_w=0, battery_w=0, grid_w=0)
    return Reading(
        pv_w=pv, battery_w=chg - dis, grid_w=imp - exp,
        load_w=max(0.0, pv + dis - chg + imp - exp),
        soc=sum(socs) / len(socs) if socs else None,
        reported_at=time.time(),   # Luxpower flags a silent inverter with `lost` instead of a stale time
        device_status="fault" if fault else "normal",
    )


def combine_energy(infos: list[dict]) -> dict:
    def total(key):
        vals = [pick(i, key) for i in infos]
        vals = [v for v in vals if v is not None]
        return sum(vals) / 10 if vals else None    # API unit is 0.1 kWh
    return {"pv": total("todayYielding"), "load": total("todayUsage"),
            "import": total("todayImport"), "export": total("todayExport")}


def _device_time(s: Any, tz_minutes: Optional[int]) -> Optional[float]:
    if not isinstance(s, str) or tz_minutes is None:
        return None
    try:
        dt = datetime.strptime(s.strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone(timedelta(minutes=tz_minutes))).timestamp()
