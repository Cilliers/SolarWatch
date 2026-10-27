import base64
import hashlib
import json
import time
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from solarwatch.connectors import AuthError, TransientError
from solarwatch.connectors.sunsynk import SunsynkConnector, normalize_flow, parse_time
from solarwatch.connectors.victron import VictronConnector, parse_diagnostics
from solarwatch.models import to_float

from fakes import FakeSession


class ToFloat(unittest.TestCase):
    def test_lenient(self):
        self.assertEqual(to_float("1234(W)"), 1234)
        self.assertEqual(to_float("86%"), 86)
        self.assertEqual(to_float("1,5"), 1.5)
        self.assertEqual(to_float("-250.5"), -250.5)
        self.assertIsNone(to_float(""))
        self.assertIsNone(to_float(None))
        self.assertIsNone(to_float("n/a"))
        self.assertIsNone(to_float(True))


# ---------------------------------------------------------------- Victron
DIAG = [
    {"description": "Battery State of Charge (System)", "rawValue": 76.5, "timestamp": 1792670000},
    {"description": "Battery Voltage (System)", "rawValue": 52.0, "timestamp": 1792670000},
    {"description": "Battery Current (System)", "rawValue": -10.0, "timestamp": 1792670000},
    {"description": "PV - DC-coupled", "rawValue": 2100, "timestamp": 1792670010},
    {"description": "PV - AC-coupled on output L1", "rawValue": 400, "timestamp": 1792670000},
    {"description": "AC Consumption L1", "rawValue": 900, "timestamp": 1792670000},
    {"description": "AC Consumption L2", "rawValue": 300, "timestamp": 1792670000},
    {"description": "Grid L1", "rawValue": -650, "timestamp": 1792670000},
    {"description": "Grid L1", "rawValue": 999, "timestamp": 1792670000},   # duplicate: ignored
    {"description": "Input voltage phase 1", "rawValue": 231, "timestamp": 1792670000},
]


class Victron(unittest.TestCase):
    def test_parse_diagnostics(self):
        r = parse_diagnostics(DIAG)
        self.assertEqual(r.pv_w, 2500)
        self.assertEqual(r.load_w, 1200)
        self.assertEqual(r.grid_w, -650)
        self.assertEqual(r.soc, 76.5)
        self.assertEqual(r.battery_w, -520)       # from V * I when no power attribute
        self.assertEqual(r.reported_at, 1792670010)

    def test_dbus_paths_without_system_descriptions(self):
        """GX systems where the descriptions differ: values found by dbus path."""
        recs = [
            {"description": "ESS Minimum SOC", "dbusServiceType": "settings", "dbusPath": "/Settings/CGwacs/BatteryLife/MinimumSocLimit", "rawValue": 20},
            {"description": "Battery SOC", "dbusServiceType": "system", "dbusPath": "/Dc/Battery/Soc", "rawValue": "83.5", "timestamp": 1792670000},
            {"description": "Battery Power", "dbusServiceType": "system", "dbusPath": "/Dc/Battery/Power", "rawValue": 1200},
            {"description": "PV - DC-coupled", "dbusServiceType": "system", "dbusPath": "/Dc/Pv/Power", "rawValue": 1800},
            {"description": "AC Loads L1", "dbusServiceType": "system", "dbusPath": "/Ac/Consumption/L1/Power", "rawValue": 450},
            {"description": "Grid L1", "dbusServiceType": "system", "dbusPath": "/Ac/Grid/L1/Power", "rawValue": -150},
        ]
        r = parse_diagnostics(recs)
        self.assertEqual((r.soc, r.battery_w, r.pv_w, r.load_w, r.grid_w), (83.5, 1200, 1800, 450, -150))

    def test_soc_from_battery_monitor(self):
        recs = [
            {"description": "ESS Minimum SOC", "rawValue": 20},
            {"description": "State of charge", "idDataAttribute": 51, "dbusServiceType": "battery", "dbusPath": "/Soc", "instance": 512, "rawValue": 64},
            {"description": "Voltage", "dbusServiceType": "battery", "dbusPath": "/Dc/0/Voltage", "instance": 512, "rawValue": 51.2},
            {"description": "Current", "dbusServiceType": "battery", "dbusPath": "/Dc/0/Current", "instance": 512, "rawValue": 10},
        ]
        r = parse_diagnostics(recs)
        self.assertEqual(r.soc, 64)
        self.assertAlmostEqual(r.battery_w, 512)

    def test_soc_from_multiplus_only(self):
        recs = [{"description": "VE.Bus state of charge", "dbusServiceType": "vebus", "dbusPath": "/Soc", "rawValue": 57},
                {"description": "ESS Minimum SOC", "rawValue": 20}]
        self.assertEqual(parse_diagnostics(recs).soc, 57)

    def test_poll_with_access_token(self):
        s = FakeSession()
        s.on("GET", "/users/me", {"success": True, "user": {"id": 42}})
        s.on("GET", "/users/42/installations", {"success": True, "records": [
            {"idSite": 1001, "name": "Beach House", "timezone": "Africa/Johannesburg"},
            {"idSite": 1002, "name": "Farm"}]})
        s.on("GET", "/installations/1001/diagnostics", {"success": True, "records": DIAG})
        s.on("GET", "/installations/1002/diagnostics", (500, {}))
        s.on("GET", "/installations/1001/stats", {"success": True, "totals":
             {"Pb": 3.0, "Pc": 5.0, "Pg": 1.0, "Bc": 2.0, "Bg": 0.5, "Gc": 4.0, "Gb": 0.25}})
        c = VictronConnector({"id": "v", "platform": "victron", "access_token": "abc"}, session=s)
        snaps = c.poll()
        self.assertEqual(s.calls[0]["headers"]["X-Authorization"], "Token abc")
        ok, bad = snaps
        self.assertEqual(ok.site.name, "Beach House")
        self.assertEqual(ok.reading.pv_kwh_today, 9.0)
        self.assertEqual(ok.reading.load_kwh_today, 11.0)
        self.assertEqual(ok.reading.import_kwh_today, 4.25)
        self.assertEqual(ok.reading.export_kwh_today, 1.5)
        self.assertIsNone(bad.reading)
        self.assertIn("HTTP 500", bad.error)

    def test_bad_token_is_auth_error(self):
        s = FakeSession().on("GET", "/users/me", (401, {"success": False}))
        c = VictronConnector({"id": "v", "platform": "victron", "access_token": "bad"}, session=s)
        with self.assertRaises(AuthError):
            c.poll()


# ---------------------------------------------------------------- Sunsynk
class SunsynkFake:
    """Plays the Sunsynk server, including checking the login signatures and RSA."""

    def __init__(self):
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        der = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
        self.pub_b64 = base64.b64encode(der).decode()
        self.logins = 0
        self.expire_next_data_call = False
        self.s = FakeSession()
        self.s.on("GET", "/anonymous/publicKey", self.public_key)
        self.s.on("POST", "/oauth/token/new", self.token)
        self.s.on("GET", "/api/v1/plants", self.authed({"total": 1, "infos": [
            {"id": 555, "name": "Smit Home", "totalPower": 8.2, "etoday": 21.4, "status": 1,
             "updateAt": "2026-10-22T11:58:00Z"}]}))
        self.s.on("GET", "/api/v1/plant/energy/555/flow", self.authed(
            {"pvPower": 4200, "loadOrEpsPower": 1500, "battPower": 1900, "gridOrMeterPower": 800,
             "soc": 64, "toBat": True, "batTo": False, "gridTo": False, "toGrid": True}))
        self.s.on("GET", "/api/v1/plant/555/inverters", self.authed({"infos": [{"sn": "2207001"}]}))
        self.s.on("GET", "/api/v1/inverter/grid/2207001/realtime", self.authed({"etodayFrom": "1.2", "etodayTo": "3.4"}))
        self.s.on("GET", "/api/v1/inverter/load/2207001/realtime", self.authed({"dailyUsed": "9.8"}))

    def public_key(self, call):
        n = call["params"]["nonce"]
        exp = hashlib.md5(f"nonce={n}&source=sunsynkPOWER_VIEW".encode()).hexdigest()
        assert call["params"]["sign"] == exp, "public key sign mismatch"
        return {"code": 0, "data": self.pub_b64}

    def token(self, call):
        b = call["json"]
        assert b["sign"] == hashlib.md5(f"nonce={b['nonce']}&source=sunsynk{self.pub_b64[:10]}".encode()).hexdigest()
        pwd = self.priv.decrypt(base64.b64decode(b["password"]), padding.PKCS1v15()).decode()
        self.logins += 1
        if pwd != "s3cret":
            return {"code": 102, "msg": "Incorrect username or password"}
        return {"code": 0, "data": {"access_token": f"tok{self.logins}", "expires_in": 3600}}

    def authed(self, data):
        def h(call):
            if self.expire_next_data_call:
                self.expire_next_data_call = False
                return (401, {})
            assert call["headers"]["Authorization"].startswith("Bearer tok")
            return {"code": 0, "data": data}
        return h


class Sunsynk(unittest.TestCase):
    def test_full_poll(self):
        f = SunsynkFake()
        c = SunsynkConnector({"id": "s", "platform": "sunsynk", "username": "a@b.c", "password": "s3cret",
                              "timezone": "Africa/Johannesburg"}, session=f.s)
        (snap,) = c.poll()
        r = snap.reading
        self.assertEqual((r.pv_w, r.load_w, r.battery_w, r.grid_w, r.soc), (4200, 1500, 1900, -800, 64))
        self.assertEqual((r.pv_kwh_today, r.import_kwh_today, r.export_kwh_today, r.load_kwh_today),
                         (21.4, 1.2, 3.4, 9.8))
        self.assertEqual(snap.site.capacity_kwp, 8.2)
        self.assertAlmostEqual(r.reported_at, parse_time("2026-10-22T11:58:00Z"))
        self.assertEqual(f.logins, 1)
        c.poll()
        self.assertEqual(f.logins, 1, "token should be reused")

    def test_relogin_once_on_401(self):
        f = SunsynkFake()
        c = SunsynkConnector({"id": "s", "platform": "sunsynk", "username": "a", "password": "s3cret"}, session=f.s)
        c.poll()
        f.expire_next_data_call = True
        c.poll()
        self.assertEqual(f.logins, 2)

    def test_wrong_password_is_auth_error(self):
        f = SunsynkFake()
        c = SunsynkConnector({"id": "s", "platform": "sunsynk", "username": "a", "password": "nope"}, session=f.s)
        with self.assertRaises(AuthError):
            c.poll()

    def test_flow_signs(self):
        r = normalize_flow({"pvPower": 0, "battPower": 700, "gridOrMeterPower": 300,
                            "batTo": True, "toBat": False, "gridTo": True, "toGrid": False})
        self.assertEqual((r.battery_w, r.grid_w), (-700, 300))
        r = normalize_flow({"battPower": 500, "gridOrMeterPower": -200})   # no flags
        self.assertEqual((r.battery_w, r.grid_w), (-500, -200))

    def test_time_without_zone_is_refused(self):
        self.assertIsNone(parse_time("2026-10-22 11:58:00"))


class NetworkDown(unittest.TestCase):
    def test_unreachable_is_transient(self):
        c = VictronConnector({"id": "v", "platform": "victron", "access_token": "x"}, session=FakeSession())
        with self.assertRaises(TransientError):
            c.poll()


if __name__ == "__main__":
    unittest.main()
