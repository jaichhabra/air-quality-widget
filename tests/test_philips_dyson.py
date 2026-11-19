"""Offline tests for the Philips and Dyson drivers.  Run:  python -m unittest discover tests"""
import json
import pathlib
import socket
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import purifier as pur  # noqa: E402


class FakePhilips(threading.Thread):
    """A pretend Philips purifier on 127.0.0.1 speaking encrypted CoAP."""

    def __init__(self, status):
        super().__init__(daemon=True)
        self.status = dict(status)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.crypto = pur.PhilipsCoAP("127.0.0.1")  # only its encrypt/decrypt are used
        self.crypto.key = "1A2B3C4D"
        self.writes = []
        self.stop = False

    def reply(self, addr, token, mid, payload):
        head = bytes([0x50 | len(token), 0x45]) + mid + token  # NON, 2.05 Content
        self.sock.sendto(head + b"\xff" + payload, addr)

    def run(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(8192)
            except (socket.timeout, OSError):
                continue
            _, code, mid, token, payload = pur.PhilipsCoAP.parse(data)
            if code == 0x02 and len(payload) == 8:            # sync
                self.reply(addr, token, mid, self.crypto.key.encode())
            elif code == 0x01:                                 # status
                body = json.dumps({"state": {"reported": self.status}})
                self.reply(addr, token, mid, self.crypto.encrypt(body).encode())
            else:                                              # control
                desired = json.loads(self.crypto.decrypt(payload.decode()))["state"]["desired"]
                for k in ("CommandType", "DeviceId", "EnduserId"):
                    desired.pop(k)
                self.writes.append(desired)
                self.status.update(desired)
                self.reply(addr, token, mid, b'{"status":"success"}')

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop = True
        self.join(1)
        self.sock.close()
        self.crypto.close()


class PhilipsTests(unittest.TestCase):
    def driver(self, fake, profile):
        return pur.Purifier({"type": "philips", "name": "Study", "ip": "127.0.0.1", "port": fake.port,
                             "profile": profile})

    def test_encryption_round_trip_and_checksum(self):
        a = pur.PhilipsCoAP("127.0.0.1")
        a.key = "000000FF"
        msg = a.encrypt('{"pwr":"1"}')
        self.assertEqual(msg[:8], "00000100", "session key increments before each message")
        self.assertEqual(a.decrypt(msg), '{"pwr":"1"}')
        with self.assertRaises(ValueError):
            a.decrypt(msg[:-1] + ("0" if msg[-1] != "0" else "1"))
        a.close()

    def test_packet_round_trip(self):
        pkt = pur.PhilipsCoAP.packet(0x02, "/sys/dev/control", b"hello", token=b"\x01\x02\x03\x04", mid=7)
        typ, code, mid, token, payload = pur.PhilipsCoAP.parse(pkt)
        self.assertEqual((typ, code, mid, token, payload), (1, 0x02, b"\x00\x07", b"\x01\x02\x03\x04", b"hello"))

    def test_classic_model_read_and_control(self):
        with FakePhilips({"pwr": "1", "mode": "M", "om": "2", "pm25": 34, "cl": False,
                          "modelid": "AC2729/10"}) as fake:
            p = self.driver(fake, "classic")
            v = p.get_all()
            self.assertEqual({k: v[k] for k in ("power", "pm25", "mode", "fan", "lock")},
                             {"power": True, "pm25": 34, "mode": "manual", "fan": 2, "lock": False})
            self.assertEqual(p.fan_range, (1, 4))
            p.set_fan(4)
            self.assertEqual(fake.writes[-1], {"pwr": "1", "mode": "M", "om": "t"})
            p.set_mode("auto")
            self.assertEqual(fake.writes[-1], {"pwr": "1", "mode": "P"})
            self.assertEqual(p.get_all()["mode"], "auto")
            p.set_power(False)
            self.assertFalse(p.get_all()["power"])

    def test_newest_generation(self):
        with FakePhilips({"D03102": 1, "D0310C": 18, "D03221": 12, "D03103": 0,
                          "D01S05": "AC0850/11 AWS_Philips_AIR_Combo"}) as fake:
            p = self.driver(fake, "gen3")
            v = p.get_all()
            self.assertEqual((v["mode"], v["fan"], v["pm25"], v["lock"]), ("manual", 2, 12, False))
            p.set_lock(True)
            self.assertEqual(fake.writes[-1], {"D03103": 1})

    def test_profile_detection(self):
        self.assertEqual(pur.philips_profile({"modelid": "AC2729/10"}), ("classic", "AC2729/10"))
        self.assertEqual(pur.philips_profile({"modelid": "AC2939/10"})[0], "classic_ag")
        self.assertEqual(pur.philips_profile({"D01-05": "AC1715/10"}), ("gen2", "AC1715/10"))
        self.assertEqual(pur.philips_profile({"D01S05": "AC0950/10"})[0], "gen3_ac0950")
        self.assertEqual(pur.philips_profile({"D01S05": "AC0850/11"})[0], "gen3")

    def test_no_answer_times_out(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))  # bound but silent
        c = pur.PhilipsCoAP("127.0.0.1", s.getsockname()[1], timeout=0.5)
        with self.assertRaises(TimeoutError):
            c.sync()
        c.close()
        s.close()


class FakeDyson:
    def __init__(self):
        self.is_connected, self.is_on, self.auto_mode, self.night_mode = False, True, True, False
        self.speed, self.hepa_filter_life, self.calls = None, 81, []

    @property
    def particulate_matter_2_5(self):
        return 23

    def connect(self, host):
        self.is_connected = True
        self.calls.append(("connect", host))

    def request_environmental_data(self):
        self.calls.append("env")

    def enable_auto_mode(self):
        self.auto_mode = True
        self.calls.append("auto_on")

    def disable_auto_mode(self):
        self.auto_mode = False
        self.calls.append("auto_off")

    def enable_night_mode(self):
        self.night_mode = True
        self.calls.append("night_on")

    def disable_night_mode(self):
        self.night_mode = False
        self.calls.append("night_off")

    def set_speed(self, s):
        self.speed = s
        self.calls.append(("speed", s))

    def turn_on(self):
        self.is_on = True

    def turn_off(self):
        self.is_on = False


class DysonTests(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDyson()
        fake_lib = types.ModuleType("libdyson")
        fake_lib.get_device = lambda serial, cred, dtype: self.dev
        patcher = mock.patch.dict(sys.modules, {"libdyson": fake_lib})
        patcher.start()
        self.addCleanup(patcher.stop)
        pur._dyson_devices.clear()
        self.p = pur.Purifier({"type": "dyson", "name": "Living", "ip": "10.0.0.9", "serial": "AB-CD-1234",
                               "credential": "secret", "device_type": "438"})

    def test_reads_auto_mode(self):
        v = self.p.get_all()
        self.assertEqual({k: v[k] for k in ("power", "pm25", "mode", "fan", "filter", "lock")},
                         {"power": True, "pm25": 23, "mode": "auto", "fan": None, "filter": 81, "lock": None})
        self.assertIn(("connect", "10.0.0.9"), self.dev.calls)
        self.assertEqual((self.p.fan_range, self.p.fan_mode), ((1, 10), "manual"))

    def test_fan_speed_leaves_auto_and_night(self):
        self.dev.night_mode = True
        self.p.set_fan(14)
        self.assertEqual(self.dev.calls[-3:], ["night_off", "auto_off", ("speed", 10)])
        v = self.p.get_all()
        self.assertEqual((v["mode"], v["fan"]), ("manual", 10))

    def test_modes(self):
        self.p.set_mode("night")
        self.assertEqual(self.p.get_all()["mode"], "night")
        self.p.set_mode("auto")
        self.assertEqual(self.dev.calls[-2:], ["night_off", "auto_on"])
        self.p.set_mode("manual")
        self.assertEqual(self.p.get_all()["mode"], "manual")

    def test_connection_is_reused(self):
        self.p.get_all()
        self.p.get_all()
        self.assertEqual(sum(1 for c in self.dev.calls if c[0] == "connect"), 1)


if __name__ == "__main__":
    unittest.main()
