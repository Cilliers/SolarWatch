"""Platform-neutral data model.

Every connector translates its platform's answers into these shapes, so the rest of
the app never needs to know whether a number came from Victron, Sunsynk or GoodWe.

Sign conventions (from the house's point of view):
    battery_w  > 0 charging,   < 0 discharging
    grid_w     > 0 importing,  < 0 exporting
    pv_w, load_w are always >= 0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SiteInfo:
    external_id: str                 # the platform's own id for the site
    name: str
    capacity_kwp: Optional[float] = None
    timezone: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)   # e.g. inverter serials


@dataclass
class Reading:
    pv_w: Optional[float] = None
    load_w: Optional[float] = None
    battery_w: Optional[float] = None
    grid_w: Optional[float] = None
    soc: Optional[float] = None
    pv_kwh_today: Optional[float] = None
    load_kwh_today: Optional[float] = None
    import_kwh_today: Optional[float] = None
    export_kwh_today: Optional[float] = None
    reported_at: Optional[float] = None   # epoch seconds the device last uploaded
    device_status: str = "normal"         # normal | fault | offline | unknown
    raw: Optional[dict[str, Any]] = None  # platform payload, kept for diagnostics


@dataclass
class Snapshot:
    """One site's result from a poll: a reading, or the error that prevented it."""
    site: SiteInfo
    reading: Optional[Reading] = None
    error: Optional[str] = None


def pick(d: Optional[dict], *keys: str) -> Optional[float]:
    """First key in `d` that holds a usable number."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = to_float(d.get(k))
        if v is not None:
            return v
    return None


def pick_str(d: Optional[dict], *keys: str) -> Optional[str]:
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def to_float(v: Any) -> Optional[float]:
    """Lenient number parsing: '1,5', '1234(W)', '86%', None -> float or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    num = ""
    for ch in s.replace(",", "."):
        if ch.isdigit() or ch in ".-+eE" and (num or ch in "-+"):
            num += ch
        elif num:
            break
    try:
        return float(num)
    except ValueError:
        return None
