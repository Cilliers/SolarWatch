"""Simulated sites, so the app can be tried (and developed) without any credentials.

Set `as_platform` to make the sites appear under any platform in the UI (victron, sunsynk, goodwe_plus, ...).
On first start it backfills `backfill_hours` of history so the charts aren't empty.
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..models import Reading, SiteInfo, Snapshot
from .base import Connector

_NAMES = ["Botha Residence", "Naidoo Family", "Van der Merwe", "Khumalo Home", "Pillay Cottage",
          "Smit Farmhouse", "Dlamini House", "Jacobs Flat", "Mokoena Villa", "Fourie Estate",
          "Ndlovu Home", "Coetzee Plot"]


class DemoConnector(Connector):
    platform = "demo"

    def __init__(self, account, session=None):
        super().__init__(account, session)
        self.platform = account.get("as_platform", "demo")
        n = int(account.get("sites", 4))
        offset = int(account.get("name_offset", 0))
        self.tz = ZoneInfo(account.get("timezone", "Africa/Johannesburg"))
        self.backfill_hours = float(account.get("backfill_hours", 24))
        self.sites = []
        for i in range(n):
            rnd = random.Random(f"{self.account_id}-{i}")
            self.sites.append({
                "id": f"{self.account_id}-{i + 1}",
                "name": _NAMES[(i + offset) % len(_NAMES)],
                "kwp": rnd.choice([3.3, 5.0, 5.5, 8.2, 10.0, 12.4]),
                "batt_kwh": rnd.choice([5.1, 10.2, 14.3, 20.4]),
                "inv_w": rnd.choice([5000, 8000, 10000]),
                "base_load": rnd.uniform(250, 700),
                "soc": rnd.uniform(35, 90),
                "seed": rnd.random(),
                "quirk": (account.get("quirks") or {}).get(i + 1),   # offline | fault | lowsoc
                "totals": {"day": None},
                "t": None,
            })

    def _info(self, s) -> SiteInfo:
        return SiteInfo(s["id"], s["name"], capacity_kwp=s["kwp"], timezone=str(self.tz), meta={"simulated": True})

    def backfill(self) -> list[tuple[SiteInfo, list[tuple[float, Reading]]]]:
        """History for first start: one reading every 5 minutes for backfill_hours."""
        end = time.time()
        start = end - self.backfill_hours * 3600
        out = []
        for s in self.sites:
            pts, t = [], start
            while t < end - 300:
                pts.append((t, self._simulate(s, t)))
                t += 300
            out.append((self._info(s), pts))
        return out

    def poll(self) -> list[Snapshot]:
        now = time.time()
        for s in self.sites:
            if s["t"] is None:   # restarted without backfill: replay today so totals add up
                t = datetime.fromtimestamp(now, self.tz).replace(hour=0, minute=0, second=0).timestamp()
                while t < now - 300:
                    self._simulate(s, t)
                    t += 300
        return [Snapshot(self._info(s), self._simulate(s, now)) for s in self.sites]

    def _simulate(self, s, t: float) -> Reading:
        dt_h = min(max((t - s["t"]) / 3600.0, 1 / 120), 1.0) if s["t"] else 1 / 12
        s["t"] = t
        local = datetime.fromtimestamp(t, self.tz)
        hour = local.hour + local.minute / 60
        day = local.date().isoformat()
        if s["totals"]["day"] != day:
            s["totals"] = {"day": day, "pv": 0.0, "load": 0.0, "imp": 0.0, "exp": 0.0}
        sun = max(0.0, math.sin(math.pi * (hour - 6) / 12.5)) if 6 <= hour <= 18.5 else 0.0
        clouds = 0.72 + 0.28 * math.sin(t / 1300 + s["seed"] * 10) * math.sin(t / 4100 + s["seed"])
        pv = s["kwp"] * 1000 * 0.78 * sun * clouds
        evening = 1500 * math.exp(-((hour - 19) ** 2) / 3) + 800 * math.exp(-((hour - 7) ** 2) / 1.5)
        load = max(150.0, s["base_load"] + evening + 150 * math.sin(t / 300 + s["seed"] * 7))
        surplus = pv - load
        if surplus > 0:
            batt = min(surplus, s["inv_w"] * 0.6) if s["soc"] < 100 else 0.0
        else:
            batt = max(surplus, -s["inv_w"] * 0.7) if s["soc"] > 20 else 0.0
        s["soc"] = max(5.0, min(100.0, s["soc"] + batt * dt_h / (s["batt_kwh"] * 10)))
        if s["quirk"] == "lowsoc" and t > time.time() - 1800:
            s["soc"] = min(s["soc"], 14.0)
        grid = load - pv + batt
        tt = s["totals"]
        tt["pv"] += pv * dt_h / 1000
        tt["load"] += load * dt_h / 1000
        tt["imp"] += max(grid, 0) * dt_h / 1000
        tt["exp"] += max(-grid, 0) * dt_h / 1000
        reported = t - 60
        if s["quirk"] == "offline" and t > time.time() - 5400:
            reported = time.time() - 5400
            pv = load = batt = grid = 0.0   # what a cloud shows once the logger drops off
        return Reading(
            pv_w=round(pv), load_w=round(load), battery_w=round(batt), grid_w=round(grid),
            soc=round(s["soc"], 1), pv_kwh_today=round(tt["pv"], 2), load_kwh_today=round(tt["load"], 2),
            import_kwh_today=round(tt["imp"], 2), export_kwh_today=round(tt["exp"], 2),
            reported_at=reported, device_status="fault" if s["quirk"] == "fault" else "normal",
        )
