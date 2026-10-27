"""SMA Sunny Portal (ennexOS).

Two ways in, chosen with `mode`:

mode: portal (default) - the same login as ennexos.sunnyportal.com. Unofficial: SolarWatch
  signs in the way the web app does (OAuth2 PKCE against login.sma.energy, client "SPpbeOS")
  and reads the web app's own API at uiapi.sunnyportal.com:
      GET  /api/v1/navigation                               plants on the account
      GET  /api/v1/widgets/gauge/power?componentId=&type=PvProduction   live PV, W
      POST /api/v1/measurements/search                      today's PV yield
  This path gives solar power and yield only; battery, grid and load are not exposed in a
  form we have been able to confirm. Accounts with two-step sign-in can't use it.

mode: monitoring_api - SMA's official Monitoring API. Requires an SMA API contract
  (developer.sma.de; billed per system), client credentials, and the plant owner's consent.
  Give SolarWatch the client_id, client_secret and the refresh token you received when the
  owner granted access; SolarWatch keeps the refreshed token in `token_store`.
      GET /v1/plants
      GET /v1/plants/{id}/measurements/sets/EnergyBalance/Recent   PV, load, battery, grid, SoC

Classic Sunny Portal (sunnyportal.com) plants have been migrated to ennexOS by SMA; if a plant
is still classic-only it is reachable through monitoring_api only.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from ..models import Reading, SiteInfo, Snapshot, pick, pick_str
from .base import AuthError, Connector, ConnectorError, TransientError

# --- portal (ennexOS web app) ---------------------------------------------------------
AUTH_BASE = "https://login.sma.energy/auth/realms/SMA/protocol/openid-connect"
CLIENT_ID = "SPpbeOS"
REDIRECT_URI = "https://ennexos.sunnyportal.com/dashboard/initialize"
UIAPI = "https://uiapi.sunnyportal.com/api/v1"
PORTAL = "https://ennexos.sunnyportal.com"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
# --- official Monitoring API ------------------------------------------------------------
OFFICIAL_TOKEN_URL = "https://auth.smaapis.de/oauth2/token"
OFFICIAL_API = "https://smaapis.de/monitoring"

TOTALS_EVERY = 3


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(os.urandom(32))
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


class SMAConnector(Connector):
    platform = "sma"

    def __init__(self, account, session=None):
        super().__init__(account, session)
        self.mode = account.get("mode", "portal")
        if self.mode not in ("portal", "monitoring_api"):
            raise ValueError(f"sma account {self.account_id!r}: mode must be portal or monitoring_api")
        self.session.headers["User-Agent"] = BROWSER_UA
        self._access: Optional[str] = None
        self._refresh: Optional[str] = None
        self._exp = 0.0
        self._poll_no = 0
        self._totals: dict[str, Optional[float]] = {}
        self._filter = {str(s) for s in account.get("plant_ids") or []}
        self.api_base = account.get("api_base", OFFICIAL_API).rstrip("/")
        self.token_url = account.get("token_url", OFFICIAL_TOKEN_URL)
        self.token_store = Path(account.get("token_store", f"data/{self.account_id}.refresh_token"))

    # ===================================================================== auth
    def _portal_login(self) -> None:
        user, pwd = self.account.get("username"), self.account.get("password")
        if not user or not pwd:
            raise AuthError("sma: set username and password (your Sunny Portal login)")
        verifier, challenge = pkce_pair()
        params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                  "scope": "openid profile", "state": _b64url(os.urandom(16)), "nonce": _b64url(os.urandom(16)),
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        page = self._http("GET", f"{AUTH_BASE}/auth", params=params, headers={"Accept": "text/html"})
        m = re.search(r'<form[^>]*\saction=["\']([^"\']+)["\']', page.text, re.I)
        if not m:
            raise TransientError("sma: the SMA sign-in page changed (no login form found)")
        action = urllib.parse.urljoin(f"{AUTH_BASE}/auth", m.group(1).replace("&amp;", "&"))
        r = self._http("POST", action, data={"username": user, "password": pwd, "credentialId": ""},
                       headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "null"},
                       allow_redirects=False)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers.get("Location", "")).query).get("code", [None])[0]
        if not code:
            # Keycloak re-renders the form (HTTP 200) on a bad password or an extra step (OTP, terms)
            if re.search(r"otp|totp|one-time", r.text or "", re.I):
                raise AuthError("sma: this account uses two-step sign-in, which SolarWatch can't complete")
            raise AuthError("sma: Sunny Portal refused the username or password")
        self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                             "code_verifier": verifier, "client_id": CLIENT_ID}, f"{AUTH_BASE}/token",
                            headers={"Origin": PORTAL, "Referer": PORTAL + "/"})

    def _official_refresh(self) -> None:
        a = self.account
        if not a.get("client_id") or not a.get("client_secret"):
            raise AuthError("sma: monitoring_api mode needs client_id and client_secret")
        rt = self._refresh or self._load_refresh() or a.get("refresh_token")
        if not rt:
            raise AuthError("sma: monitoring_api mode needs the owner's refresh_token")
        self._token_request({"grant_type": "refresh_token", "refresh_token": rt, "client_id": a["client_id"],
                             "client_secret": a["client_secret"]}, self.token_url)
        if self._refresh and self._refresh != rt:
            self._save_refresh(self._refresh)

    def _token_request(self, form: dict, url: str, headers: Optional[dict] = None) -> None:
        r = self._http("POST", url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                        **(headers or {})})
        if r.status_code in (400, 401):
            raise AuthError(f"sma: token refused ({_err(r)})")
        try:
            j = r.json()
        except ValueError as e:
            raise TransientError("sma: token endpoint sent a non-JSON reply") from e
        if not j.get("access_token"):
            raise AuthError(f"sma: no access token ({j.get('error_description') or j.get('error')})")
        self._access = j["access_token"]
        self._refresh = j.get("refresh_token") or self._refresh
        self._exp = time.time() + max(float(j.get("expires_in") or 300) - 60, 30)

    def _ensure_token(self) -> None:
        if self._access and time.time() < self._exp:
            return
        if self.mode == "monitoring_api":
            self._official_refresh()
            return
        if self._refresh:
            try:
                self._token_request({"grant_type": "refresh_token", "refresh_token": self._refresh,
                                     "client_id": CLIENT_ID}, f"{AUTH_BASE}/token",
                                    headers={"Origin": PORTAL, "Referer": PORTAL + "/"})
                return
            except AuthError:
                self._refresh = None   # refresh token expired: fall through to a full sign-in
        self._portal_login()

    def _load_refresh(self) -> Optional[str]:
        try:
            return self.token_store.read_text().strip() or None
        except OSError:
            return None

    def _save_refresh(self, token: str) -> None:
        try:
            self.token_store.parent.mkdir(parents=True, exist_ok=True)
            self.token_store.write_text(token)
            os.chmod(self.token_store, 0o600)
        except OSError:
            pass

    # ================================================================ transport
    def _http(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        try:
            r = self.session.request(method, url, **kw)
        except requests.RequestException as e:
            raise TransientError(f"sma: could not reach {urllib.parse.urlparse(url).hostname} "
                                 f"({e.__class__.__name__})") from e
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"sma: {urllib.parse.urlparse(url).hostname} answered HTTP {r.status_code}")
        return r

    def _api(self, method: str, url: str, **kw) -> Any:
        for attempt in (0, 1):
            self._ensure_token()
            hdrs = {"Authorization": f"Bearer {self._access}", "Accept": "application/json"}
            if self.mode == "portal":
                hdrs.update({"Origin": PORTAL, "Referer": PORTAL + "/"})
            r = self._http(method, url, headers=hdrs, **kw)
            if r.status_code == 401 and not attempt:
                self._access = None
                self._exp = 0
                continue
            if r.status_code in (401, 403):
                raise AuthError(f"sma: access denied ({_err(r)})")
            if r.status_code == 404:
                raise TransientError(f"sma: not found ({urllib.parse.urlparse(url).path})")
            if r.status_code >= 400:
                raise TransientError(f"sma: HTTP {r.status_code} ({_err(r)})")
            try:
                return r.json()
            except ValueError as e:
                raise TransientError("sma: non-JSON reply") from e
        raise TransientError("sma: unreachable")  # pragma: no cover

    # ===================================================================== poll
    def poll(self) -> list[Snapshot]:
        self._poll_no += 1
        plants = self._portal_plants() if self.mode == "portal" else self._official_plants()
        out = []
        for p in plants:
            if self._filter and p["id"] not in self._filter:
                continue
            site = SiteInfo(p["id"], p["name"], timezone=p.get("tz") or self.account.get("timezone"),
                            meta={"mode": self.mode})
            try:
                r = self._portal_read(site) if self.mode == "portal" else self._official_read(site)
                out.append(Snapshot(site, r))
            except AuthError:
                raise
            except ConnectorError as e:
                out.append(Snapshot(site, error=str(e)))
        return out

    # -- portal ------------------------------------------------------------------
    def _portal_plants(self) -> list[dict]:
        nav = self._api("GET", f"{UIAPI}/navigation")
        nav = nav if isinstance(nav, list) else [nav]
        found = []

        def walk(items):
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                if it.get("componentType") == "Plant" or ("componentType" not in it and "componentId" in it):
                    found.append({"id": str(it["componentId"]), "name": it.get("name") or str(it["componentId"])})
                walk(it.get("children") or it.get("items"))
        walk(nav)
        if not found:
            raise TransientError("sma: no plants on this Sunny Portal account")
        return found

    def _portal_read(self, site: SiteInfo) -> Reading:
        g = self._api("GET", f"{UIAPI}/widgets/gauge/power", params={"componentId": site.external_id,
                                                                      "type": "PvProduction"})
        pv = pick(g, "value")
        r = Reading(pv_w=abs(pv) if pv is not None else None,
                    reported_at=_iso(pick_str(g, "timestamp", "time")))
        if site.external_id not in self._totals or self._poll_no % TOTALS_EVERY == 1:
            try:
                self._totals[site.external_id] = self._portal_today_kwh(site)
            except TransientError:
                pass
        r.pv_kwh_today = self._totals.get(site.external_id)
        r.raw = {"gauge": g, "pv_kwh_today": r.pv_kwh_today}
        return r

    def _portal_today_kwh(self, site: SiteInfo) -> Optional[float]:
        tzname = site.timezone or "UTC"
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            tz, tzname = ZoneInfo("UTC"), "UTC"
        now = datetime.now(tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        channel = "Measurement.Metering.TotWhOut.Pv"
        data = self._api("POST", f"{UIAPI}/measurements/search", json={
            "queryItems": [{"componentId": site.external_id, "channelId": channel, "resolution": "OneDay",
                            "timezone": tzname, "aggregate": "Dif", "multiAggregate": "Sum"}],
            "dateTimeBegin": utc(start), "dateTimeEnd": utc(start.replace(hour=23, minute=59, second=59))})
        for ch in data if isinstance(data, list) else []:
            if ch.get("channelId") == channel:
                vals = [pick(v, "value") for v in ch.get("values") or []]
                vals = [v for v in vals if v is not None]
                if vals:
                    return vals[-1] / 1000   # Wh -> kWh
        return None

    # -- official Monitoring API ------------------------------------------------------
    def _official_plants(self) -> list[dict]:
        d = self._api("GET", f"{self.api_base}/v1/plants")
        rows = d.get("plants") if isinstance(d, dict) else d
        out = [{"id": str(p.get("plantId")), "name": p.get("name") or str(p.get("plantId")),
                "tz": p.get("timezone")} for p in rows or [] if p.get("plantId") is not None]
        if not out:
            raise TransientError("sma: no plants shared with this client yet (has the owner given consent?)")
        return out

    def _official_read(self, site: SiteInfo) -> Reading:
        d = self._api("GET", f"{self.api_base}/v1/plants/{site.external_id}/measurements/sets/EnergyBalance/Recent")
        r = parse_energy_balance(d)
        r.raw = d
        return r


def parse_energy_balance(d: dict) -> Reading:
    s = d.get("set") if isinstance(d, dict) else None
    if isinstance(s, list):
        s = s[-1] if s else {}
    s = s or {}
    pv, load = pick(s, "pvGeneration"), pick(s, "totalConsumption")
    chg, dis = pick(s, "batteryCharging"), pick(s, "batteryDischarging")
    feed, buy = pick(s, "gridFeedIn"), pick(s, "gridConsumption")
    status = "fault" if (d.get("error") or "").lower() == "devicecommunication" else "normal"
    return Reading(
        pv_w=pv, load_w=load,
        battery_w=None if chg is None and dis is None else (chg or 0) - (dis or 0),
        grid_w=None if feed is None and buy is None else (buy or 0) - (feed or 0),
        soc=pick(s, "batteryStateOfCharge"),
        reported_at=_iso(s.get("time")),
        device_status="offline" if status == "fault" else "normal",
    )


def _iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _err(r: requests.Response) -> str:
    try:
        j = r.json()
        return str(j.get("error_description") or j.get("error") or j.get("message") or r.status_code)
    except ValueError:
        return f"HTTP {r.status_code}"
