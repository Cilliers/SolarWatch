import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs

from solarwatch.connectors import AuthError, make_connector
from solarwatch.connectors.deye import DeyeConnector, parse_latest
from solarwatch.connectors.goodwe_plus import GoodWePlusConnector, hash_password, parse_flow, signature
from solarwatch.connectors.luxpower import LuxPowerConnector, combine_runtimes
from solarwatch.connectors.sma import SMAConnector, parse_energy_balance

from fakes import FakeResponse, FakeSession


def check_signature(sig, uid, token):
    digest, ts = base64.b64decode(sig).decode().rsplit("@", 1)
    return digest == hashlib.sha256(f"{ts}@{uid}@{token}".encode()).hexdigest()


# ---------------------------------------------------------------- GoodWe SEMS+
class GoodWePlus(unittest.TestCase):
    FLOW = {"pSystem": 2.286, "pConsum": 0.339, "pGrid": -0.003, "pBat": -1.950, "soc": 64,
            "refreshTime": "2026-10-23 10:15:00"}

    def test_password_hash(self):
        self.assertEqual(hash_password("abc"),
                         base64.b64encode(hashlib.md5(b"abc").hexdigest().encode()).decode())

    def test_flow_signs_and_units(self):
        r = parse_flow(self.FLOW, "Africa/Johannesburg")
        self.assertAlmostEqual(r.pv_w, 2286)
        self.assertAlmostEqual(r.load_w, 339)
        self.assertAlmostEqual(r.battery_w, 1950)   # SEMS+ -1.95 kW (charging) -> +1950
        self.assertAlmostEqual(r.grid_w, 3)         # SEMS+ -0.003 (import) -> +3
        self.assertEqual(r.soc, 64)
        self.assertIsNotNone(r.reported_at)

    def test_poll_installer_fallback_and_relogin(self):
        s = FakeSession()
        logins, flows = [], []

        def login(call):
            logins.append(call)
            body = call["json"]
            assert body["pwd"] == hash_password("pw") and body["agreement"] == 1
            assert check_signature(call["headers"]["x-signature"], "", "")
            return {"code": "00000", "data": {"uid": "u9", "token": f"tk{len(logins)}", "timestamp": 1},
                    "api": "https://au-gateway.semsportal.com/web/sems/"}

        def flow(call):
            flows.append(call)
            assert call["host"] == "au-gateway.semsportal.com", "gateway from login reply"
            tok = json.loads(call["headers"]["token"])
            assert tok["client"] == "semsPlusWeb"
            assert check_signature(call["headers"]["x-signature"], "u9", tok["token"])
            if len(flows) == 1:
                return {"code": "C0602", "msg": "token expired"}
            return {"code": "00000", "data": self.FLOW}

        s.on("POST", "/auth/cross-login", login)
        s.on("POST", "/stations/simple-query", {"code": "00000", "data": {"dataList": []}})
        s.on("POST", "/portal/stations/page", {"code": "00000", "data": {"total": 1, "dataList": [
            {"stationId": "st-1", "stationName": "Pillay PV", "capacity": 8, "productionToday": 12.5}]}})
        s.on("GET", "/stations/flow", flow)
        c = GoodWePlusConnector({"id": "g", "platform": "goodwe_plus", "username": "a", "password": "pw"}, session=s)
        (snap,) = c.poll()
        self.assertEqual((snap.site.name, snap.site.capacity_kwp), ("Pillay PV", 8))
        self.assertEqual(snap.reading.pv_kwh_today, 12.5)
        self.assertEqual(len(logins), 2)

    def test_untrusted_api_host_is_ignored(self):
        s = FakeSession()
        s.on("POST", "/auth/cross-login", {"code": "00000", "data": {"uid": "u", "token": "t"},
                                           "api": "https://evil.example.com/"})
        s.on("POST", "/stations/simple-query", {"code": "00000", "data": {"list": [{"id": "x", "name": "X"}]}})
        s.on("GET", "/stations/flow", {"code": "00000", "data": {}})
        c = GoodWePlusConnector({"id": "g", "platform": "goodwe_plus", "username": "a", "password": "pw"}, session=s)
        c.poll()
        self.assertTrue(all(call["host"] != "evil.example.com" for call in s.calls))

    def test_bad_login(self):
        s = FakeSession().on("POST", "/auth/cross-login", {"code": "100004", "msg": "parameter error"})
        c = GoodWePlusConnector({"id": "g", "platform": "goodwe_plus", "username": "a", "password": "x"}, session=s)
        with self.assertRaises(AuthError):
            c.poll()


# ---------------------------------------------------------------- Deye Cloud
class Deye(unittest.TestCase):
    def test_latest_signs(self):
        r = parse_latest({"generationPower": 3838, "consumptionPower": 882, "batteryPower": -1500,
                          "wirePower": -1456, "batterySOC": 32, "lastUpdateTime": 1787624115})
        self.assertEqual((r.pv_w, r.load_w, r.battery_w, r.grid_w, r.soc), (3838, 882, 1500, -1456, 32))
        self.assertEqual(r.reported_at, 1787624115)

    def test_latest_mirror_fields(self):
        r = parse_latest({"generationPower": 0, "dischargePower": 700, "purchasePower": 100})
        self.assertEqual((r.battery_w, r.grid_w), (-700, 100))

    def test_poll(self):
        s = FakeSession()
        tokens = []

        def token(call):
            assert call["params"]["appId"] == "APP"
            b = call["json"]
            assert b["password"] == hashlib.sha256(b"pw").hexdigest() and b["email"] == "a@b.c"
            tokens.append(1)
            return {"success": True, "code": "1000000", "accessToken": "AT", "expiresIn": "5183999"}

        s.on("POST", "/v1.0/account/token", token)
        s.on("POST", "/v1.0/station/list", {"success": True, "total": 1, "stationList": [
            {"id": 4711, "name": "Khumalo Home", "installedCapacity": 10.0}]})
        s.on("POST", "/v1.0/station/latest", {"success": True, "code": "1000000", "generationPower": 4000,
                                               "consumptionPower": 900, "batteryPower": -2600, "wirePower": -500,
                                               "batterySOC": 71, "lastUpdateTime": time.time()})
        s.on("POST", "/v1.0/station/history", {"success": True, "stationDataItems": [
            {"generationValue": 20.0, "purchaseValue": 1.0, "gridValue": 3.0, "chargeValue": 8.0,
             "dischargeValue": 4.0, "consumptionValue": 99}]})
        c = DeyeConnector({"id": "d", "platform": "deye", "app_id": "APP", "app_secret": "S",
                           "email": "a@b.c", "password": "pw"}, session=s)
        (snap,) = c.poll()
        r = snap.reading
        self.assertEqual((r.battery_w, r.grid_w), (2600, -500))
        self.assertEqual((r.pv_kwh_today, r.import_kwh_today, r.export_kwh_today), (20.0, 1.0, 3.0))
        self.assertEqual(r.load_kwh_today, 14.0)   # 20 + 1 - 3 + 4 - 8, not Deye's residual
        self.assertTrue(all(c["headers"].get("Authorization") == "Bearer AT"
                            for c in s.calls if "/station/" in c["path"]))
        c.poll()
        self.assertEqual(len(tokens), 1)

    def test_bad_app(self):
        s = FakeSession().on("POST", "/v1.0/account/token",
                             {"success": False, "code": "2101021", "msg": "auth invalid appId"})
        c = DeyeConnector({"id": "d", "platform": "deye", "app_id": "X", "app_secret": "S",
                           "email": "a", "password": "p"}, session=s)
        with self.assertRaises(AuthError):
            c.poll()


# ---------------------------------------------------------------- LuxPower
RT_CHARGING = {"success": True, "lost": False, "statusText": "normal", "ppv": 5200, "pCharge": 3000,
               "pDisCharge": 0, "pToUser": 0, "pToGrid": 800, "soc": 66, "consumptionPower": 0}
RT_NIGHT = {"success": True, "lost": False, "statusText": "normal", "ppv1": 0, "ppv2": 0, "pCharge": 1045,
            "pDisCharge": 0, "pToUser": 1030, "pToGrid": 0, "soc": 71, "consumptionPower": 0}


class LuxPower(unittest.TestCase):
    def test_combine(self):
        r = combine_runtimes([RT_CHARGING], 120)
        self.assertEqual((r.pv_w, r.battery_w, r.grid_w, r.load_w, r.soc), (5200, 3000, -800, 1400, 66))
        r = combine_runtimes([RT_NIGHT], 120)   # sample payload where consumptionPower reads 0
        self.assertEqual((r.battery_w, r.grid_w, r.load_w), (1045, 1030, 0))

    def test_all_lost_is_offline(self):
        r = combine_runtimes([{"lost": True, "statusText": "offline", "deviceTime": "2026-10-23 06:00:00"}], 120)
        self.assertEqual(r.device_status, "offline")
        self.assertIsNotNone(r.reported_at)

    def test_poll_with_session_expiry(self):
        s = FakeSession()
        logins, rt_calls = [], []

        def login(call):
            logins.append(call)
            assert call["data"]["account"] == "u"
            return {"success": True, "plants": [{"plantId": 77, "name": "Smit Farm", "timezoneHourOffset": 2,
                                                 "timezoneMinuteOffset": 0,
                                                 "inverters": [{"serialNum": "A1"}, {"serialNum": "A2"}]}]}

        def runtime(call):
            rt_calls.append(call)
            if len(rt_calls) == 1:
                return FakeResponse(200, "<html>login</html>")   # session expired
            return RT_CHARGING

        s.on("POST", "/WManage/api/login", login)
        s.on("POST", "/getInverterRuntime", runtime)
        s.on("POST", "/getInverterEnergyInfo", {"success": True, "todayYielding": 184, "todayUsage": 96,
                                                 "todayImport": 5, "todayExport": 40})
        c = LuxPowerConnector({"id": "l", "platform": "luxpower", "username": "u", "password": "p"}, session=s)
        (snap,) = c.poll()
        self.assertEqual(snap.site.name, "Smit Farm")
        self.assertEqual(snap.reading.pv_w, 10400)                # two inverters summed
        self.assertAlmostEqual(snap.reading.pv_kwh_today, 36.8)   # 2 x 18.4 kWh
        self.assertEqual(len(logins), 2)

    def test_bad_login(self):
        s = FakeSession().on("POST", "/WManage/api/login", {"success": False, "msg": "loginFailed"})
        c = LuxPowerConnector({"id": "l", "platform": "luxpower", "username": "u", "password": "x"}, session=s)
        with self.assertRaises(AuthError):
            c.poll()


# ---------------------------------------------------------------- SMA
LOGIN_PAGE = ('<html><form id="kc-form-login" action="https://login.sma.energy/auth/realms/SMA/login-actions/'
              'authenticate?session_code=abc&amp;execution=e1&amp;client_id=SPpbeOS" method="post"></form></html>')


class SMA(unittest.TestCase):
    def portal_session(self, password_ok=True):
        s = FakeSession()
        self.token_calls = []
        s.on("GET", "/openid-connect/auth", lambda c: FakeResponse(200, LOGIN_PAGE))

        def authenticate(call):
            assert call["params"]["execution"] == "e1", "&amp; must be unescaped"
            if call["data"]["password"] != "pw":
                return FakeResponse(200, "<html>Invalid username or password.</html>")
            return FakeResponse(302, "", {"Location": "https://ennexos.sunnyportal.com/dashboard/initialize"
                                                      "?state=x&code=CODE123"})

        def token(call):
            self.token_calls.append(call)
            f = call["data"]
            if f["grant_type"] == "authorization_code":
                assert f["code"] == "CODE123" and f["client_id"] == "SPpbeOS" and f["code_verifier"]
            return {"access_token": f"AT{len(self.token_calls)}", "refresh_token": "RT", "expires_in": 300}

        s.on("POST", "/login-actions/authenticate", authenticate)
        s.on("POST", "/openid-connect/token", token)
        s.on("GET", "/api/v1/navigation", [{"componentId": 9001, "componentType": "Plant", "name": "Fourie Estate"}])
        s.on("GET", "/widgets/gauge/power", {"value": 3125.5, "timestamp": "2026-10-23T08:10:00Z"})
        s.on("POST", "/measurements/search", [{"channelId": "Measurement.Metering.TotWhOut.Pv",
                                               "values": [{"time": "2026-10-23", "value": 14250}]}])
        return s

    def test_portal_poll(self):
        s = self.portal_session()
        c = SMAConnector({"id": "s", "platform": "sma", "username": "a@b.c", "password": "pw",
                          "timezone": "Africa/Johannesburg"}, session=s)
        (snap,) = c.poll()
        self.assertEqual(snap.site.name, "Fourie Estate")
        self.assertEqual(snap.reading.pv_w, 3125.5)
        self.assertEqual(snap.reading.pv_kwh_today, 14.25)
        self.assertIsNone(snap.reading.soc)   # not exposed in portal mode
        api = [c for c in s.calls if c["host"] == "uiapi.sunnyportal.com"]
        self.assertTrue(all(c["headers"]["Authorization"] == "Bearer AT1" for c in api))

    def test_portal_wrong_password(self):
        c = SMAConnector({"id": "s", "platform": "sma", "username": "a", "password": "nope"},
                         session=self.portal_session())
        with self.assertRaises(AuthError):
            c.poll()

    def test_official_api_refresh_and_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sma.rt"
            s = FakeSession()

            def token(call):
                f = call["data"]
                assert f["grant_type"] == "refresh_token" and f["client_secret"] == "SEC"
                return {"access_token": "OA", "refresh_token": "RT-new", "expires_in": 3600}

            s.on("POST", "/oauth2/token", token)
            s.on("GET", "/monitoring/v1/plants", {"plants": [{"plantId": "7a1e", "name": "Botha",
                                                              "timezone": "Africa/Johannesburg"}]})
            s.on("GET", "/measurements/sets/EnergyBalance/Recent", {"setType": "EnergyBalance", "set": [
                {"time": "2026-10-23T08:10:00Z", "pvGeneration": 4100, "totalConsumption": 900,
                 "batteryCharging": 2000, "batteryDischarging": 0, "gridFeedIn": 1200, "gridConsumption": 0,
                 "batteryStateOfCharge": 58}]})
            c = SMAConnector({"id": "s", "platform": "sma", "mode": "monitoring_api", "client_id": "CID",
                              "client_secret": "SEC", "refresh_token": "RT-old", "token_store": str(store)},
                             session=s)
            (snap,) = c.poll()
            r = snap.reading
            self.assertEqual((r.pv_w, r.load_w, r.battery_w, r.grid_w, r.soc), (4100, 900, 2000, -1200, 58))
            self.assertEqual(store.read_text(), "RT-new")


class Registry(unittest.TestCase):
    def test_retired_goodwe_gives_migration_hint(self):
        with self.assertRaisesRegex(ValueError, "goodwe_plus"):
            make_connector({"id": "old", "platform": "goodwe"})

    def test_all_platforms_construct(self):
        for p in ("goodwe_plus", "sma", "deye", "luxpower"):
            self.assertEqual(make_connector({"id": p, "platform": p}).platform, p)


if __name__ == "__main__":
    unittest.main()
