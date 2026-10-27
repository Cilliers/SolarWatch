"""Derive each site's health and the fleet's alert list from the latest readings."""
from __future__ import annotations

import time
from typing import Any


def site_health(site: dict[str, Any], account_state: dict[str, Any] | None, cfg: dict) -> tuple[str, list[dict]]:
    """Return (status, alerts) for one site.

    status: ok | warning | fault | offline | error | pending
    """
    now = time.time()
    stale_s = float(cfg["alerts"]["stale_minutes"]) * 60
    low_soc = float(cfg["alerts"]["low_soc"])
    alerts: list[dict] = []

    def add(level, code, msg):
        alerts.append({"level": level, "code": code, "message": msg, "site_id": site["id"],
                       "site_name": site["name"], "platform": site["platform"]})

    if site.get("last_error"):
        add("error", "read_error", site["last_error"])
    if site.get("fetched_at") is None:
        return ("error" if alerts else "pending"), alerts

    reported = site.get("reported_at") or site["fetched_at"]
    silent_for = now - reported
    if silent_for > stale_s:
        add("critical", "silent", f"No data from the system for {_ago(silent_for)}")
    if account_state and account_state.get("state") in ("error", "auth_failed"):
        age = now - site["fetched_at"]
        if age > stale_s:
            add("error", "account", f"Account not updating: {account_state.get('last_error')}")
    if site.get("device_status") == "fault":
        add("critical", "fault", "Platform reports an inverter fault")
    elif site.get("device_status") == "offline" and silent_for <= stale_s:
        add("critical", "silent", "Platform reports the system offline")
    soc = site.get("soc")
    if soc is not None and soc < low_soc and silent_for <= stale_s:
        add("warning", "low_soc", f"Battery at {soc:.0f}%")

    codes = {a["code"] for a in alerts}
    if "silent" in codes:
        status = "offline"
    elif "fault" in codes:
        status = "fault"
    elif codes & {"read_error", "account"}:
        status = "error"
    elif alerts:
        status = "warning"
    else:
        status = "ok"
    return status, alerts


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} min"
    h = m // 60
    return f"{h} h {m % 60} min" if h < 48 else f"{h // 24} days"
