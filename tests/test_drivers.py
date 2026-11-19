"""Offline tests for the purifier drivers.  Run:  python -m unittest discover tests"""
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import purifier as pur  # noqa: E402


class FakeQuboCloud:
    """Stands in for Qubo's servers: records requests, answers from a routing function."""

    def __init__(self, route):
        self.route, self.calls = route, []

    def __call__(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers or {}))
        status, payload = self.route(method, path, body)
        return status, json.dumps(payload).encode() if payload is not None else b""


class QuboTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(pur, "CFG_PATH", pathlib.Path(self.tmp.name) / "purifier.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        pur._qubo = None

    def cloud(self, route, tokens=True):
        c = pur.qubo()
        if tokens:
            c.tokens = {"access": "A", "refresh": "R", "user": "U", "exp": time.time() + 86400}
        fake = FakeQuboCloud(route)
        c._send = fake
        return c, fake

    def test_email_login_stores_session(self):
        c, fake = self.cloud(lambda m, p, b: (200, {"accessToken": "A1", "refreshToken": "R1", "uuid": "U1"}),
                             tokens=False)
        c.login_email("me@example.com", "pw")
        method, path, body, headers = fake.calls[0]
        self.assertEqual((method, path), ("POST", f"/sms/api/v1/sp/{pur.QuboCloud.SP}/user/login?system=CS"))
        self.assertEqual(body["email"], "me@example.com")
        self.assertEqual(c.tokens["access"], "A1")
        saved = json.loads(pur.CFG_PATH.read_text())["accounts"]["qubo"]["tokens"]
        self.assertEqual(saved["refresh"], "R1")

    def test_lists_only_purifiers(self):
        def route(m, p, b):
            if p.endswith("/units"):
                return 200, [{"unitUUID": "home"}]
            return 200, {"data": [
                {"deviceUUID": "p1", "deviceName": "Bedroom", "deviceModel": "HPH03", "entityUUID": "e1"},
                {"deviceUUID": "plug", "deviceModel": "HSP01"},
            ]}
        c, _ = self.cloud(route)
        self.assertEqual(c.purifiers(), [{"id": "p1", "unit": "home", "entity": "e1", "model": "HPH03",
                                          "name": "Bedroom"}])

    def test_driver_reads_services_shadow(self):
        shadow = {"services": [
            {"service": "lcSwitchControl", "attributes": {"power": {"value": "on"}}},
            {"service": "fanControlMode", "attributes": {"state": {"value": "Manual"}}},
            {"service": "fanSpeedControl", "attributes": {"speed": {"value": "2"}}},
            {"service": "aqiStatus", "attributes": {"PM25": {"value": 87}}},
            {"service": "childLockControl", "attributes": {"state": {"value": "disable"}}},
        ]}
        self.cloud(lambda m, p, b: (200, shadow if p.endswith("/shadow") else {}))
        p = pur.Purifier({"type": "qubo", "name": "Bedroom",
                          "device": {"id": "p1", "unit": "home", "entity": "e1", "model": "HPH03"}})
        v = p.get_all()
        self.assertEqual({k: v[k] for k in ("power", "pm25", "mode", "fan", "lock")},
                         {"power": True, "pm25": 87, "mode": "manual", "fan": 2, "lock": False})
        self.assertEqual(v["mode_label"], "Manual")
        self.assertEqual((p.fan_range, p.fan_mode), ((1, 3), "manual"))

    def test_fan_speed_switches_to_manual_and_clamps(self):
        _, fake = self.cloud(lambda m, p, b: (200, {}))
        p = pur.Purifier({"type": "qubo", "device": {"id": "p1", "unit": "home", "entity": "", "model": "HPH03"}})
        p.set_fan(9)
        bodies = [(c[1].split("/")[-2:], c[2]) for c in fake.calls]
        self.assertEqual(bodies[0][1]["serviceName"], "fanControlMode")
        self.assertEqual(bodies[0][1]["attributeValue"], "manual")
        self.assertEqual(bodies[1][1]["serviceName"], "fanSpeedControl")
        self.assertEqual(bodies[1][1]["attributeValue"], "3")
        self.assertIn("/entities/p1/devices/p1/shadow/attribute", fake.calls[1][1],
                      "falls back to the device id when there is no entity")

    def test_refreshes_once_after_401(self):
        state = {"n": 0}

        def route(m, p, b):
            if p.endswith("/auth/refresh"):
                return 200, {"accessToken": "NEW"}
            state["n"] += 1
            return (401, None) if state["n"] == 1 else (200, {"services": []})
        c, fake = self.cloud(route)
        c.request("GET", "/x")
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[2][3]["Authorization"], "Bearer NEW")
        self.assertEqual(c.tokens["refresh"], "R", "keeps the refresh token when none is returned")


class XiaomiCapabilityTests(unittest.TestCase):
    def test_xiaomi_declares_its_controls(self):
        d = pur.XiaomiDriver
        self.assertEqual([k for k, _ in d.MODES], ["auto", "sleep", "favorite"])
        self.assertEqual((d.FAN_RANGE, d.FAN_MODE), ((0, 14), "favorite"))

    def test_old_config_without_type_is_xiaomi(self):
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(pur, "CFG_PATH", pathlib.Path(t) / "purifier.json"):
            pur.CFG_PATH.write_text(json.dumps({"ip": "10.0.0.2", "token": "0" * 32, "name": "Old"}))
            cfg = pur.load_cfg()
            self.assertEqual(cfg["purifiers"][0]["name"], "Old")
            self.assertEqual(cfg["purifiers"][0].get("type", "xiaomi"), "xiaomi")


if __name__ == "__main__":
    unittest.main()
