"""Control air purifiers from several brands through one interface.

Purifiers are listed in purifier.json (see purifier.example.json). Each entry has a
"type" naming its driver. Drivers today:

  xiaomi   Xiaomi Smart Air Purifier 4 Compact (xiaomi.airp.cpa4m), local network (miIO)
  qubo     Qubo smart air purifiers (HPH series), Qubo cloud
  philips  Philips air purifiers with Wi-Fi (CoAP), local network
  dyson    Dyson Pure / Purifier fans, local network (needs: pip install libdyson-neon)

Adding a brand = one Driver subclass (see the Driver class) plus a branch in `cmd_add`.

Usage:
  python purifier.py [-p NAME] [command]     NAME: purifier name or number (default: the first)

  status                      (default)
  on | off
  mode MODE                   e.g. auto, sleep, favorite / manual (depends on the purifier)
  level N                     fan speed (range depends on the purifier)
  lock on|off                 child lock
  beep on|off                 Xiaomi only
  screen off|dim|bright       Xiaomi only

  list                        list configured purifiers
  add                         add a purifier (asks which brand)
  remove NAME                 remove a purifier
"""
import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import pathlib
import random
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

CFG_PATH = pathlib.Path(__file__).with_name("purifier.json")


# ── config ─────────────────────────────────────────────────────────────────
def load_cfg():
    try:
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        cfg = {}
    if "purifiers" not in cfg:  # older single-purifier format
        cfg["purifiers"] = []
        if cfg.get("ip"):
            cfg["purifiers"].append({k: cfg.pop(k) for k in ("name", "ip", "token", "model") if k in cfg})
    return cfg


def save_cfg(cfg):
    CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


CFG = load_cfg()


# ── driver interface ───────────────────────────────────────────────────────
class Driver:
    """One brand/model family. Subclasses fill in the class attributes and the methods.

    read() returns a dict with these keys (None when the purifier doesn't report it):
        power   bool
        pm25    number, µg/m³
        mode    one of the MODES keys
        fan     int within FAN_RANGE
        lock    bool, child lock
        filter  int, % filter life left
    """
    BRAND = "?"
    MODES = []            # [(key, label)], in the order the widget lists them
    FAN_RANGE = None      # (lowest, highest) or None if the fan speed can't be set
    FAN_MODE = None       # mode key the fan speed belongs to (the driver switches to it)
    HAS_LOCK = False

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def where(self):
        return self.cfg.get("ip", "cloud")

    def read(self):
        raise NotImplementedError

    def set_power(self, on):
        raise NotImplementedError

    def set_mode(self, key):
        raise NotImplementedError

    def set_fan(self, level):
        raise NotImplementedError

    def set_lock(self, on):
        raise NotImplementedError


# ── Xiaomi (local miIO / MIoT) ─────────────────────────────────────────────
class XiaomiDriver(Driver):
    BRAND = "Xiaomi"
    MODES = [("auto", "Auto"), ("sleep", "Sleep"), ("favorite", "Favorite")]
    FAN_RANGE = (0, 14)
    FAN_MODE = "favorite"
    HAS_LOCK = True
    SUPPORTED_MODEL = "xiaomi.airp.cpa4m"

    # (siid, piid) from the device's MIoT spec
    P = {
        "power": (2, 1), "fault": (2, 2), "mode": (2, 4),
        "pm25": (3, 4),
        "filter_life": (4, 1), "filter_used_h": (4, 3), "filter_left_days": (4, 4),
        "beep": (6, 1), "lock": (8, 1), "fan_level": (9, 11), "screen": (13, 2),
    }
    SCREEN = {0: "off", 1: "bright", 2: "dim"}
    FAULTS = {0: "none", 2: "motor stuck", 3: "sensor lost"}

    def __init__(self, cfg):
        super().__init__(cfg)
        from miio import Device
        self.dev = Device(cfg["ip"], cfg["token"], timeout=5)

    def send(self, method, params, tries=3):
        # the purifier's Wi-Fi dozes; the first handshake sometimes times out
        for i in range(tries):
            try:
                return self.dev.send(method, params)
            except Exception:
                if i == tries - 1:
                    raise

    def raw(self):
        req = [{"did": k, "siid": s, "piid": p} for k, (s, p) in self.P.items()]
        return {r["did"]: r.get("value") for r in self.send("get_properties", req)}

    def set_prop(self, name, value):
        s, p = self.P[name]
        res = self.send("set_properties", [{"did": name, "siid": s, "piid": p, "value": value}])
        if res[0].get("code") != 0:
            raise RuntimeError(f"{self.cfg.get('name')} refused {name}={value}: {res[0]}")

    def read(self):
        v = self.raw()
        keys = [k for k, _ in self.MODES]
        return {"power": bool(v["power"]), "pm25": v["pm25"],
                "mode": keys[v["mode"]] if v["mode"] in range(len(keys)) else None,
                "fan": v["fan_level"], "lock": bool(v["lock"]), "filter": v["filter_life"], "raw": v}

    def set_power(self, on):
        self.set_prop("power", bool(on))

    def set_mode(self, key):
        self.set_prop("mode", [k for k, _ in self.MODES].index(key))

    def set_fan(self, level):
        self.set_prop("fan_level", max(self.FAN_RANGE[0], min(self.FAN_RANGE[1], int(level))))
        self.set_mode(self.FAN_MODE)  # the level only applies in Favorite

    def set_lock(self, on):
        self.set_prop("lock", bool(on))


# ── Qubo (cloud) ───────────────────────────────────────────────────────────
class QuboCloud:
    """Qubo has no public API. Endpoints and payloads follow the open-source qubopy library
    (github.com/selvakk2k/qubopy, MIT), which reverse-engineered them from the Android app.
    The session is kept in purifier.json under accounts.qubo (git-ignored)."""

    BASE = "https://srvcapp.platform.quboworld.com"
    SP = "d10e4bfb0153496e8e8bb955f7ebe413"
    APP = "com.hero.iot"
    LABEL = "Air Quality widget"

    def __init__(self):
        acct = load_cfg().get("accounts", {}).get("qubo", {})
        self.install = acct.get("install_id") or str(uuid.uuid4())
        self.tokens = acct.get("tokens")
        self._phone_session = None
        self._lock = threading.Lock()

    def _save(self):
        cfg = load_cfg()
        cfg.setdefault("accounts", {})["qubo"] = {"install_id": self.install, "tokens": self.tokens}
        save_cfg(cfg)

    @property
    def signed_in(self):
        return bool(self.tokens)

    # http
    def _send(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", "App-Id": self.APP, "Source-Device-Id": self.install}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.BASE + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post_unauth(self, path, body, headers):
        status, data = self._send("POST", path, body, headers)
        if status != 200:
            msg = ""
            try:
                d = json.loads(data)
                msg = d.get("message") or d.get("errorMessage") or d.get("error") or ""
            except Exception:
                pass
            raise RuntimeError(f"Qubo: {msg}" if msg else f"Qubo sign-in failed (HTTP {status})")
        return json.loads(data)

    def _auth_headers(self):
        t = self.tokens
        return {"Authorization": f"Bearer {t['access']}", "Token-Type": "USER", "User-UUID": t["user"],
                "Subscriber-Key": t["access"], "Source": "Android"}

    @staticmethod
    def _expiry(jwt):
        try:
            p = jwt.split(".")[1]
            p += "=" * (-len(p) % 4)
            return float(json.loads(base64.urlsafe_b64decode(p))["exp"])
        except Exception:
            return time.time() + 3600

    def _set_tokens(self, d, user=None):
        access = d.get("accessToken") or d.get("token") or d.get("access_token")
        user = user or d.get("uuid") or d.get("userUUID") or d.get("userId")
        if not access or not user:
            raise RuntimeError("Qubo did not return a session.")
        refresh = d.get("refreshToken") or d.get("refresh_token") or (self.tokens or {}).get("refresh")
        self.tokens = {"access": access, "refresh": refresh, "user": str(user), "exp": self._expiry(access)}
        self._save()

    # sign-in
    def login_email(self, email, password):
        d = self._post_unauth(f"/sms/api/v1/sp/{self.SP}/user/login?system=CS",
                              {"username": email, "email": email, "password": password, "deviceAttribute": self.LABEL},
                              {"Source": "USER", "Token-Type": "USER"})
        self._set_tokens(d[0] if isinstance(d, list) else d)

    def request_phone_code(self, phone, isd="+91"):
        isd = isd if isd.startswith("+") else "+" + isd
        d = self._post_unauth(f"/sms/api/v1/sp/{self.SP}/user/phone/login",
                              {"phone": phone, "isdCode": isd, "deviceAttribute": self.LABEL}, {"Source": "USER"})
        self._phone_session = d.get("uuid")

    def verify_phone(self, phone, code, isd="+91"):
        isd = isd if isd.startswith("+") else "+" + isd
        d = self._post_unauth(f"/sms/api/v1/sp/{self.SP}/user/phone/verify",
                              {"phone": phone, "isdCode": isd, "otp": code, "deviceAttribute": self.LABEL,
                               "uuid": self._phone_session}, {"Source": "USER"})
        profile = (d if isinstance(d, list) else [d])[0]
        self._set_tokens(profile)
        if profile.get("email"):  # swap the ~1 h phone session for a ~6 month one
            f = self._post_unauth(f"/sms/api/v1/sp/{self.SP}/user/emailprofile/login",
                                  {"username": profile["email"], "deviceAttribute": self.LABEL},
                                  {"Token-Type": "USER", "Source": "Android", "User-UUID": self.tokens["user"],
                                   "Subscriber-Key": self.tokens["access"]})
            self._set_tokens(f, user=self.tokens["user"])

    def refresh(self):
        t = self.tokens or {}
        if not t.get("refresh"):
            raise RuntimeError("Qubo sign-in expired. Run: python purifier.py add  (and choose Qubo)")
        status, data = self._send("POST", f"/sms/api/v1/sp/{self.SP}/users/{t['user']}/auth/refresh",
                                  {"accessToken": t["access"], "refreshToken": t["refresh"]}, self._auth_headers())
        if status != 200:
            if status in (400, 401, 403):
                self.tokens = None
                self._save()
            raise RuntimeError("Qubo sign-in expired. Run: python purifier.py add  (and choose Qubo)")
        self._set_tokens(json.loads(data), user=t["user"])

    def request(self, method, path, body=None, retried=False):
        with self._lock:
            if not self.tokens:
                raise RuntimeError("Not signed in to Qubo. Run: python purifier.py add")
            if self.tokens["exp"] - time.time() < 300 and self.tokens.get("refresh"):
                self.refresh()
            status, data = self._send(method, path, body, self._auth_headers())
            if status == 401 and not retried and self.tokens.get("refresh"):
                self.refresh()
                status, data = self._send(method, path, body, self._auth_headers())
        if status in (401, 403):
            raise RuntimeError("Qubo rejected the sign-in. Run: python purifier.py add  (and choose Qubo)")
        if status not in (200, 201, 204):
            raise RuntimeError(f"Qubo cloud error (HTTP {status})")
        return json.loads(data) if data else {}

    # devices
    @staticmethod
    def _items(resp, *keys):
        if isinstance(resp, list):
            return resp
        for k in keys:
            if isinstance(resp.get(k), list):
                return resp[k]
        return [resp]

    def purifiers(self):
        out, seen = [], set()
        units = self.request("GET", f"/unit-entity-management/api/v1/sp/{self.SP}/units")
        for u in self._items(units, "data", "units"):
            unit = u.get("unitUUID") or u.get("uuid") or u.get("unitId")
            if not unit:
                continue
            devs = self.request("GET", f"/unit-entity-management/api/v1/sp/{self.SP}/units/{unit}/devices")
            for d in self._items(devs, "data", "devices"):
                did = d.get("deviceUUID") or d.get("deviceId") or d.get("uuid")
                model = str(d.get("deviceModel") or d.get("model") or d.get("aModelNo") or "")
                kind = str(d.get("deviceType") or d.get("type") or "")
                if did and did not in seen and (model.upper().startswith("HPH") or "PURIFIER" in kind.upper()):
                    seen.add(did)
                    out.append({"id": did, "unit": d.get("unitUUID") or unit,
                                "entity": d.get("entityUUID") or d.get("entityId") or "",
                                "model": model, "name": d.get("deviceName") or d.get("name") or f"Qubo {did[:6]}"})
        return out

    def path(self, dev):
        entity = dev.get("entity") or dev["id"]
        return (f"/unit-entity-management/api/v1/sp/{self.SP}/units/{dev['unit']}"
                f"/entities/{entity}/devices/{dev['id']}")


_qubo = None


def qubo():
    global _qubo
    if _qubo is None:
        _qubo = QuboCloud()
    return _qubo


def _flatten_shadow(resp):
    """Qubo shadows come as services[].attributes or as state.reported; flatten both."""
    out = {}
    if not isinstance(resp, dict):
        return out
    for s in resp.get("services") or []:
        for k, v in (s.get("attributes") or {}).items():
            val = v.get("value") if isinstance(v, dict) else v
            if val is not None:
                out[f"{s.get('service')}_{k}"] = val
    reported = (resp.get("state") or {}).get("reported", resp.get("reported")) or {}
    out.update(reported if isinstance(reported, dict) else {})
    return out


def _first(r, *keys):
    return next((r[k] for k in keys if r.get(k) is not None), None)


def _as_bool(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return {1: True, 0: False}.get(v)
    return {"true": True, "1": True, "on": True, "enable": True, "enabled": True, "yes": True,
            "false": False, "0": False, "off": False, "disable": False, "disabled": False,
            "no": False}.get(str(v).strip().lower())


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class QuboDriver(Driver):
    BRAND = "Qubo"
    MODES = [("auto", "Auto"), ("sleep", "Sleep"), ("manual", "Manual")]
    FAN_RANGE = (1, 3)
    FAN_MODE = "manual"
    HAS_LOCK = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.dev = cfg["device"]
        self.cloud = qubo()
        self._nudged = 0

    def read(self):
        if time.time() - self._nudged > 300:  # ask for a fresh PM2.5 reading now and then (best effort)
            self._nudged = time.time()
            body = {"devices": {"deviceUUID": self.dev["id"], "services": {
                "aqiRefresh": {"commands": {"refresh": {"parameters": {}, "instanceId": 0}}}}}}
            try:
                self.cloud.request("PUT", self.cloud.path(self.dev) + "/command", body)
            except Exception:
                pass
        r = _flatten_shadow(self.cloud.request("GET", self.cloud.path(self.dev) + "/shadow"))
        mode = _first(r, "fanControlMode_state", "mode", "preset_mode")
        return {"power": bool(_as_bool(_first(r, "lcSwitchControl_power", "state", "power", "power_state"))),
                "pm25": _as_int(_first(r, "aqiStatus_PM25", "pm2_5", "pm25", "aqi")),
                "mode": str(mode).lower() if mode is not None else None,
                "fan": _as_int(_first(r, "fanSpeedControl_speed", "speed", "fan_speed", "fanSpeed")),
                "lock": _as_bool(_first(r, "childLockControl_state", "child_lock", "childLock")),
                "filter": _as_int(_first(r, "filterReset_percentage", "filter_life", "filter_life_percent")),
                "raw": r}

    def _attr(self, service, attribute, value):
        self.cloud.request("PUT", self.cloud.path(self.dev) + "/shadow/attribute",
                           {"instanceId": 0, "serviceName": service, "attributeName": attribute,
                            "attributeValue": value})

    def set_power(self, on):
        self._attr("lcSwitchControl", "power", "on" if on else "off")

    def set_mode(self, key):
        self._attr("fanControlMode", "state", key)

    def set_fan(self, level):
        self.set_mode(self.FAN_MODE)
        self._attr("fanSpeedControl", "speed", str(max(1, min(3, int(level)))))

    def set_lock(self, on):
        self._attr("childLockControl", "state", "enable" if on else "disable")


# ── Philips (local CoAP) ───────────────────────────────────────────────────
class PhilipsCoAP:
    """Minimal client for Philips purifiers' encrypted CoAP API (UDP 5683).

    Protocol and encryption follow aioairctrl (github.com/betaboon/aioairctrl, MIT): sync a
    session key, then AES-CBC payloads with key/IV from md5("JiangPan" + key) and a SHA-256
    check. Built on the standard library + `cryptography` instead of aiocoap/pycryptodomex.
    """
    SECRET = "JiangPan"
    GET, POST = 0x01, 0x02

    def __init__(self, host, port=5683, timeout=5):
        self.addr = (host, port)
        self.timeout = timeout
        self.key = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def close(self):
        self.sock.close()

    # CoAP framing
    @staticmethod
    def _ext(n):
        if n < 13:
            return n, b""
        if n < 269:
            return 13, bytes([n - 13])
        return 14, (n - 269).to_bytes(2, "big")

    @classmethod
    def packet(cls, code, path, payload=b"", observe=False, token=None, mid=None):
        token = token if token is not None else os.urandom(4)
        mid = mid if mid is not None else random.randrange(65536)
        opts = ([(6, b"")] if observe else []) + [(11, s.encode()) for s in path.strip("/").split("/")]
        body, last = b"", 0
        for num, val in opts:  # already in option-number order: Observe (6) before Uri-Path (11)
            d, dx = cls._ext(num - last)
            ln, lx = cls._ext(len(val))
            body += bytes([(d << 4) | ln]) + dx + lx + val
            last = num
        head = bytes([0x50 | len(token), code]) + mid.to_bytes(2, "big") + token  # v1, NON
        return head + body + ((b"\xff" + payload) if payload else b"")

    @staticmethod
    def parse(data):
        """-> (type, code, mid, token, payload)"""
        tkl = data[0] & 0x0F
        i = 4 + tkl
        while i < len(data) and data[i] != 0xFF:
            d, ln = data[i] >> 4, data[i] & 0x0F
            i += 1
            i += {13: 1, 14: 2}.get(d, 0)
            if ln == 13:
                ln, i = data[i] + 13, i + 1
            elif ln == 14:
                ln, i = int.from_bytes(data[i:i + 2], "big") + 269, i + 2
            i += ln
        payload = data[i + 1:] if i < len(data) else b""
        return (data[0] >> 4) & 3, data[1], data[2:4], data[4:4 + tkl], payload

    def _request(self, code, path, payload=b"", observe=False):
        token = os.urandom(4)
        self.sock.sendto(self.packet(code, path, payload, observe, token), self.addr)
        deadline = time.time() + self.timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"No answer from the Philips purifier at {self.addr[0]}")
            self.sock.settimeout(left)
            try:
                data, _ = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:  # Windows reports ICMP "port unreachable" as an error
                continue
            typ, _, mid, tok, body = self.parse(data)
            if typ == 0:  # confirmable reply: acknowledge it
                self.sock.sendto(bytes([0x60, 0]) + mid, self.addr)
            # These purifiers reuse message ids, so match on the token, never de-duplicate.
            if tok == token:
                return body

    # encryption
    def _cipher(self, key):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        kv = hashlib.md5((self.SECRET + key).encode()).hexdigest().upper()
        return Cipher(algorithms.AES(kv[:16].encode()), modes.CBC(kv[16:].encode()))

    def encrypt(self, text):
        from cryptography.hazmat.primitives import padding
        self.key = ((int(self.key, 16) + 1) & 0xFFFFFFFF).to_bytes(4, "big").hex().upper()
        p = padding.PKCS7(128).padder()
        data = p.update(text.encode()) + p.finalize()
        e = self._cipher(self.key).encryptor()
        ct = (e.update(data) + e.finalize()).hex().upper()
        return self.key + ct + hashlib.sha256((self.key + ct).encode()).hexdigest().upper()

    def decrypt(self, s):
        from cryptography.hazmat.primitives import padding
        key, ct, digest = s[:8], s[8:-64], s[-64:]
        if hashlib.sha256((key + ct).encode()).hexdigest().upper() != digest:
            raise ValueError("Philips reply failed its checksum")
        d = self._cipher(key).decryptor()
        data = d.update(bytes.fromhex(ct)) + d.finalize()
        u = padding.PKCS7(128).unpadder()
        return (u.update(data) + u.finalize()).decode()

    # API
    def sync(self):
        self.key = self._request(self.POST, "/sys/dev/sync", os.urandom(4).hex().upper().encode()).decode()
        return self.key

    def status(self):
        body = self._request(self.GET, "/sys/dev/status", observe=True).decode()
        return json.loads(self.decrypt(body))["state"]["reported"]

    def control(self, data):
        desired = {"state": {"desired": {"CommandType": "app", "DeviceId": "", "EnduserId": "", **data}}}
        body = self._request(self.POST, "/sys/dev/control", self.encrypt(json.dumps(desired)).encode())
        try:
            return json.loads(body).get("status") == "success"
        except ValueError:
            return False


# Modes and fan steps per model family, as the key/value patterns the purifier reports and
# accepts. Taken from the philips-airpurifier-coap Home Assistant integration (kongo09, MIT).
PHILIPS_PROFILES = {
    "classic": {   # AC2729, AC2889, AC3829, AC1214…  keys pwr / mode / om / pm25 / cl
        "power": ("pwr", "1", "0"), "pm25": "pm25", "lock": ("cl", True, False),
        "modes": [("auto", "Auto", {"mode": "P"}), ("sleep", "Sleep", {"mode": "S"}),
                  ("manual", "Manual", {"mode": "M"})],
        "speeds": [{"mode": "M", "om": "1"}, {"mode": "M", "om": "2"}, {"mode": "M", "om": "3"},
                   {"mode": "M", "om": "t"}],
    },
    "classic_ag": {  # AC29xx, AC3021, AC305x, AC3836, AC385x, AC4236, AC4558
        "power": ("pwr", "1", "0"), "pm25": "pm25", "lock": ("cl", True, False),
        "modes": [("auto", "Auto", {"mode": "AG"}), ("sleep", "Sleep", {"mode": "S"}),
                  ("manual", "Manual", {"mode": "GT"})],
        "speeds": [{"mode": "GT"}, {"mode": "T"}],          # Gentle, Turbo
    },
    "gen2": {      # AC1715, AC0850 (AWS_Philips_AIR firmware)
        "power": ("D03-02", "ON", "OFF"), "pm25": "D03-33", "lock": None,
        "modes": [("auto", "Auto", {"D03-12": "Auto General"}), ("sleep", "Sleep", {"D03-12": "Sleep"}),
                  ("manual", "Manual", {"D03-12": "Gentle/Speed 1"})],
        "speeds": [{"D03-12": "Gentle/Speed 1"}, {"D03-12": "Speed 2"}, {"D03-12": "Turbo"}],
    },
    "gen3": {      # newest firmware (…_Combo): numeric keys
        "power": ("D03102", 1, 0), "pm25": "D03221", "lock": ("D03103", 1, 0),
        "modes": [("auto", "Auto", {"D0310C": 0}), ("sleep", "Sleep", {"D0310C": 17}),
                  ("manual", "Manual", {"D0310C": 19})],
        "speeds": [{"D0310C": 19}, {"D0310C": 18}],         # Medium, Turbo
    },
    "gen3_ac0950": {
        "power": ("D03102", 1, 0), "pm25": "D03221", "lock": ("D03103", 1, 0),
        "modes": [("auto", "Auto", {"D0310C": 0, "D0310D": 1}), ("sleep", "Sleep", {"D0310C": 17, "D0310D": 1}),
                  ("manual", "Manual", {"D0310C": 19, "D0310D": 2})],
        "speeds": [{"D0310C": 17, "D0310D": 1}, {"D0310C": 19, "D0310D": 2}, {"D0310C": 18, "D0310D": 18}],
    },
}
PHILIPS_AG_MODELS = ("AC29", "AC3021", "AC305", "AC3836", "AC385", "AC4236", "AC4558")


def philips_profile(status):
    """Pick the profile for a purifier from its own status report -> (profile, model)."""
    if "D01S05" in status:
        model = str(status["D01S05"])
        return ("gen3_ac0950" if model.startswith(("AC0950", "AC0951")) else "gen3"), model
    if "D01-05" in status:
        return "gen2", str(status["D01-05"])
    model = str(status.get("modelid") or status.get("type") or "")
    return ("classic_ag" if model.upper().startswith(PHILIPS_AG_MODELS) else "classic"), model


def _matches(status, pattern):
    return all(status.get(k) == v for k, v in pattern.items())


class PhilipsDriver(Driver):
    BRAND = "Philips"
    FAN_MODE = "manual"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.p = PHILIPS_PROFILES[cfg.get("profile", "classic")]
        self.MODES = [(k, label) for k, label, _ in self.p["modes"]]
        self.FAN_RANGE = (1, len(self.p["speeds"]))
        self.HAS_LOCK = self.p["lock"] is not None

    def _client(self):
        c = PhilipsCoAP(self.cfg["ip"], self.cfg.get("port", 5683))
        try:
            c.sync()
        except Exception:
            c.close()
            raise
        return c

    def status(self):
        c = self._client()
        try:
            return c.status()
        finally:
            c.close()

    def read(self):
        s, p = self.status(), self.p
        fan = next((i + 1 for i, pat in enumerate(p["speeds"]) if _matches(s, pat)), None)
        mode = next((k for k, _, pat in p["modes"] if k != "manual" and _matches(s, pat)), None)
        if mode is None and fan is not None:
            mode = "manual"
        lock = None
        if p["lock"] and p["lock"][0] in s:
            lock = s[p["lock"][0]] == p["lock"][1]
        power_key, on, _ = p["power"]
        return {"power": s.get(power_key) == on, "pm25": _as_int(s.get(p["pm25"])), "mode": mode,
                "fan": fan if mode == "manual" else None, "lock": lock, "filter": None, "raw": s}

    def _write(self, data):
        c = self._client()
        try:
            if not c.control(data):
                c.sync()
                if not c.control(data):
                    raise RuntimeError(f"{self.cfg.get('name', 'Philips purifier')} refused {data}")
        finally:
            c.close()

    def _on(self):
        key, on, _ = self.p["power"]
        return {key: on}

    def set_power(self, on):
        key, v_on, v_off = self.p["power"]
        self._write({key: v_on if on else v_off})

    def set_mode(self, key):
        pattern = next(pat for k, _, pat in self.p["modes"] if k == key)
        self._write({**self._on(), **pattern})

    def set_fan(self, level):
        speeds = self.p["speeds"]
        self._write({**self._on(), **speeds[max(1, min(len(speeds), int(level))) - 1]})

    def set_lock(self, on):
        if not self.p["lock"]:
            raise RuntimeError("This Philips model has no child lock")
        key, v_on, v_off = self.p["lock"]
        self._write({key: v_on if on else v_off})


def scan_philips(seconds=3):
    """Find Philips purifiers: send a CoAP sync to every address on the /24 -> {ip: model?}."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    pkt = PhilipsCoAP.packet(PhilipsCoAP.POST, "/sys/dev/sync", b"0000000A")
    for host in local_subnet().hosts():
        try:
            s.sendto(pkt, (str(host), 5683))
        except OSError:
            pass
    found, end = [], time.time() + seconds
    while time.time() < end:
        if select.select([s], [], [], 0.3)[0]:
            try:
                data, (ip, _) = s.recvfrom(2048)
            except OSError:
                continue
            payload = PhilipsCoAP.parse(data)[4]
            if len(payload) == 8 and ip not in found:
                found.append(ip)
    s.close()
    return found


# ── Dyson (local MQTT via libdyson-neon) ───────────────────────────────────
def _libdyson():
    try:
        import libdyson
        return libdyson
    except ImportError:
        raise RuntimeError("Dyson support needs the libdyson-neon library:  pip install libdyson-neon")


_dyson_devices = {}  # serial -> connected libdyson device (MQTT connection kept open)


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


class DysonDriver(Driver):
    """Dyson Pure / Purifier fans. Uses libdyson-neon (github.com/libdyson-wg/libdyson-neon, MIT).
    The purifier's local MQTT login (serial + credential) comes once from the Dyson account."""
    BRAND = "Dyson"
    MODES = [("auto", "Auto"), ("night", "Night"), ("manual", "Manual")]
    FAN_RANGE = (1, 10)
    FAN_MODE = "manual"
    HAS_LOCK = False

    def __init__(self, cfg):
        super().__init__(cfg)
        self.lib = _libdyson()

    def device(self):
        serial = self.cfg["serial"]
        d = _dyson_devices.get(serial)
        if d is None or not d.is_connected:
            d = self.lib.get_device(serial, self.cfg["credential"], self.cfg["device_type"])
            if d is None:
                raise RuntimeError(f"Unsupported Dyson model ({self.cfg['device_type']})")
            d.connect(self.cfg["ip"])
            _dyson_devices[serial] = d
        return d

    def read(self):
        d = self.device()
        _safe(d.request_environmental_data)  # the answer arrives in the background
        if _safe(lambda: d.night_mode):
            mode = "night"
        elif _safe(lambda: d.auto_mode):
            mode = "auto"
        else:
            mode = "manual"
        return {"power": bool(_safe(lambda: d.is_on)),
                # Pure Cool Link models report particulates on their own scale, not µg/m³: shown as unavailable
                "pm25": _safe(lambda: d.particulate_matter_2_5),
                "mode": mode, "fan": _safe(lambda: d.speed) if mode == "manual" else None,
                "lock": None, "filter": _safe(lambda: d.hepa_filter_life), "raw": {}}

    def set_power(self, on):
        d = self.device()
        d.turn_on() if on else d.turn_off()

    def set_mode(self, key):
        d = self.device()
        if key == "night":
            d.enable_night_mode()
            return
        if _safe(lambda: d.night_mode):
            d.disable_night_mode()
        d.enable_auto_mode() if key == "auto" else d.disable_auto_mode()

    def set_fan(self, level):
        d = self.device()
        if _safe(lambda: d.night_mode):
            d.disable_night_mode()
        if _safe(lambda: d.auto_mode):
            d.disable_auto_mode()
        d.set_speed(max(1, min(10, int(level))))

    def set_lock(self, on):
        raise RuntimeError("Dyson purifiers have no child lock")


def scan_tcp(port, timeout=0.6):
    """Hosts on the /24 with `port` open (Dyson purifiers listen for MQTT on 1883)."""
    def probe(host):
        try:
            with socket.create_connection((str(host), port), timeout=timeout):
                return str(host)
        except OSError:
            return None
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        return [h for h in pool.map(probe, local_subnet().hosts()) if h]


DRIVERS = {"xiaomi": XiaomiDriver, "qubo": QuboDriver, "philips": PhilipsDriver, "dyson": DysonDriver}


# ── purifier (what the widget and CLI use) ─────────────────────────────────
class Purifier:
    def __init__(self, c):
        self.cfg = c
        self.type = c.get("type", "xiaomi")
        self.driver = DRIVERS[self.type](c)
        self.name = c.get("name") or self.driver.where
        self.ip = self.driver.where

    # capabilities
    @property
    def modes(self):
        return self.driver.MODES

    @property
    def fan_range(self):
        return self.driver.FAN_RANGE

    @property
    def fan_mode(self):
        return self.driver.FAN_MODE

    def mode_label(self, key):
        return dict(self.modes).get(key, (key or "—").capitalize())

    def get_all(self):
        v = self.driver.read()
        v["mode_label"] = self.mode_label(v.get("mode"))
        return v

    def set_power(self, on):
        self.driver.set_power(on)

    def set_mode(self, key):
        self.driver.set_mode(key)

    def set_fan(self, level):
        self.driver.set_fan(level)

    def set_lock(self, on):
        self.driver.set_lock(on)


def purifiers(cfg=None):
    out = []
    for c in (cfg or load_cfg())["purifiers"]:
        try:
            out.append(Purifier(c))
        except Exception as e:  # e.g. a driver's library isn't installed
            print(f"Skipping {c.get('name', '?')}: {e}", file=sys.stderr)
    return out


def pick(name=None, cfg=None):
    items = purifiers(cfg)
    if not items:
        sys.exit("No purifiers configured. Run:  python purifier.py add")
    if name is None:
        return items[0]
    if name.isdigit() and 1 <= int(name) <= len(items):
        return items[int(name) - 1]
    for p in items:
        if p.name.lower() == name.lower():
            return p
    sys.exit(f"No purifier called {name!r}. Configured: " + ", ".join(p.name for p in items))


# ── network scan (Xiaomi) ──────────────────────────────────────────────────
def local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def scan(seconds=4):
    """Find Mi devices by sending the miIO hello packet to every address on the /24."""
    hello = bytes.fromhex("21310020" + "ff" * 28)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    for host in local_subnet().hosts():
        try:
            s.sendto(hello, (str(host), 54321))
        except OSError:
            pass
    found, end = {}, time.time() + seconds
    while time.time() < end:
        if select.select([s], [], [], 0.3)[0]:
            try:
                data, (ip, _) = s.recvfrom(1024)
            except OSError:
                continue
            if len(data) >= 32 and data[:2] == b"\x21\x31":
                found[ip] = int.from_bytes(data[8:12], "big")
    return found


# ── commands ───────────────────────────────────────────────────────────────
def status(p):
    v = p.get_all()
    print(f"{p.name}  ({p.driver.BRAND}, {p.ip})")
    print(f"  Power:        {'ON' if v['power'] else 'off'}")
    fan = f"  (fan {v['fan']})" if v.get("fan") is not None else ""
    print(f"  Mode:         {v['mode_label']}{fan}")
    print(f"  PM2.5:        {v['pm25'] if v.get('pm25') is not None else '—'} µg/m³")
    if v.get("filter") is not None:
        print(f"  Filter:       {v['filter']}% left")
    if v.get("lock") is not None:
        print(f"  Child lock:   {'on' if v['lock'] else 'off'}")
    raw = v.get("raw", {})
    if p.type == "xiaomi":
        print(f"  Beep:         {'on' if raw['beep'] else 'off'}")
        print(f"  Screen:       {XiaomiDriver.SCREEN.get(raw['screen'], raw['screen'])}")
        print(f"  Fault:        {XiaomiDriver.FAULTS.get(raw['fault'], raw['fault'])}")


def cmd_list():
    items = load_cfg()["purifiers"]
    if not items:
        print("No purifiers yet. Run:  python purifier.py add")
    for i, c in enumerate(items, 1):
        kind = c.get("type", "xiaomi")
        where = c.get("ip") or "cloud"
        print(f"  {i}. {c.get('name', '?'):<20} {kind:<8} {where:<16} {c.get('model') or c.get('device', {}).get('model', '')}")


def ask(prompt, default=None, secret=False):
    try:
        if secret:
            import getpass
            v = getpass.getpass(f"{prompt}: ")
        else:
            v = input(f"{prompt}{f' [{default}]' if default else ''}: ")
    except EOFError:
        return None
    v = v.strip().lstrip("﻿").strip()
    return v or default


def cmd_add():
    print("\nAdd an air purifier\n───────────────────")
    print("  1. Xiaomi   (on your Wi-Fi)")
    print("  2. Qubo     (through your Qubo account)")
    print("  3. Philips  (on your Wi-Fi)")
    print("  4. Dyson    (on your Wi-Fi; one-time Dyson account sign-in)")
    brand = (ask("Brand") or "").lower()
    adders = {"1": add_xiaomi, "xiaomi": add_xiaomi, "2": add_qubo, "qubo": add_qubo,
              "3": add_philips, "philips": add_philips, "4": add_dyson, "dyson": add_dyson}
    if brand in adders:
        return adders[brand]()
    print("Cancelled." if not brand else f"{brand!r} isn't supported yet.")


def pick_ip(found, known, what):
    """Show discovered addresses, return the chosen IP (or None)."""
    new = [ip for ip in found if ip not in known]
    for i, ip in enumerate(new, 1):
        print(f"  {i}. {ip}")
    if not new:
        print(f"  No new {what} found (already-added ones are hidden).")
    choice = ask("\nNumber from the list, or type the purifier's IP address")
    if not choice:
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(new):
        return new[int(choice) - 1]
    try:
        return str(ipaddress.ip_address(choice))
    except ValueError:
        print(f"{choice!r} is not a number from the list or an IP address.")
        return None


def add_philips():
    cfg = load_cfg()
    known = {c.get("ip") for c in cfg["purifiers"]}
    print("\nLooking for Philips purifiers on your network…")
    ip = pick_ip(scan_philips(), known, "Philips purifiers")
    if not ip:
        return print("Cancelled.")
    print("Connecting…")
    c = PhilipsCoAP(ip)
    try:
        c.sync()
        status = c.status()
    except Exception as e:
        return print(f"Could not talk to a Philips purifier at {ip}: {e}")
    finally:
        c.close()
    profile, model = philips_profile(status)
    print(f"Connected: {model or 'Philips purifier'}  (profile: {profile})")
    default = status.get("name") or status.get("D01-03") or status.get("D01S03") or "Philips"
    name = ask("Name to show on the widget", str(default))
    cfg["purifiers"].append({"type": "philips", "name": name, "ip": ip, "profile": profile, "model": model})
    save_cfg(cfg)
    print(f"\nAdded {name}. The widget picks it up automatically: click the room name to switch.")


def add_dyson():
    try:
        lib = _libdyson()
    except RuntimeError:
        if (ask("Dyson support needs the libdyson-neon library. Install it now? (y/n)", "y") or "").lower() != "y":
            return print("Cancelled.")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "libdyson-neon"])
        import importlib
        importlib.invalidate_caches()
        lib = _libdyson()
    from libdyson.cloud import DysonAccount

    print("\nSign in to your Dyson account once to get each purifier's local login.")
    print("Dyson emails you a code. Your password goes only to Dyson and isn't saved.")
    region = (ask("Country of your Dyson account (two letters, e.g. IN, US, GB)", "IN") or "IN").upper()
    email = ask("Email")
    if not email:
        return print("Cancelled.")
    account = DysonAccount()
    try:
        verify = account.login_email_otp(email, region)
        password = ask("Password (not shown)", secret=True)
        code = ask("Code from Dyson's email")
        verify(code, password)
        infos = account.devices()
    except Exception as e:
        return print(f"Dyson sign-in failed: {e}")

    cfg = load_cfg()
    known = {c.get("serial") for c in cfg["purifiers"] if c.get("type") == "dyson"}
    vacuums = {getattr(lib, n) for n in dir(lib) if n.startswith("DEVICE_TYPE_360")}
    devices = []
    for info in infos:
        dtype = _safe(info.get_device_type) or info.product_type
        if info.serial not in known and dtype not in vacuums:
            devices.append((info, dtype))
    if not devices:
        return print("No new Dyson purifiers in this account (already-added ones are hidden).")
    for i, (info, dtype) in enumerate(devices, 1):
        print(f"  {i}. {info.name:<24} {info.serial}  (type {dtype})")
    choice = ask("Number", "1")
    if not (choice and choice.isdigit() and 1 <= int(choice) <= len(devices)):
        return print("Cancelled.")
    info, dtype = devices[int(choice) - 1]

    print("\nLooking for Dyson devices on your network…")
    ip = pick_ip(scan_tcp(1883), {c.get("ip") for c in cfg["purifiers"]}, "Dyson devices")
    if not ip:
        return print("Cancelled.")
    entry = {"type": "dyson", "name": info.name, "ip": ip, "serial": info.serial,
             "credential": info.credential, "device_type": dtype, "model": info.product_type}
    try:
        DysonDriver(entry).read()
        print("Connected.")
    except Exception as e:
        return print(f"Could not connect to {info.name} at {ip}: {e}")
    entry["name"] = ask("Name to show on the widget", info.name)
    cfg["purifiers"].append(entry)
    save_cfg(cfg)
    print(f"\nAdded {entry['name']}. The widget picks it up automatically: click the room name to switch.")


def add_xiaomi():
    """Pick a device found on the network, give its token and a name, test, save."""
    from miio import Device
    cfg = load_cfg()
    known = {c.get("ip") for c in cfg["purifiers"]}
    print("\nLooking for Xiaomi devices on your network…")
    found = scan()
    new = [(ip, did) for ip, did in sorted(found.items()) if ip not in known]
    for i, (ip, did) in enumerate(new, 1):
        print(f"  {i}. {ip:<16} device id {did}")
    if not new:
        print("  No new devices found (already-added purifiers are hidden).")
    choice = ask("\nNumber from the list, or type the purifier's IP address")
    if not choice:
        return print("Cancelled.")
    if choice.isdigit() and 1 <= int(choice) <= len(new):
        ip, did = new[int(choice) - 1]
    else:
        try:
            ip, did = str(ipaddress.ip_address(choice)), None
        except ValueError:
            return print(f"{choice!r} is not a number from the list or an IP address.")

    print("\nThe token is a 32-character key for this device. Get it with Xiaomi Cloud Tokens")
    print("Extractor (github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor). You can paste")
    print("the token, or the path of the JSON file the extractor saves with -o.")
    entry = ask("Token or JSON path")
    if not entry:
        return print("Cancelled.")
    token, model, name = entry, None, None
    if pathlib.Path(entry.strip('"')).is_file():
        data = json.loads(pathlib.Path(entry.strip('"')).read_text(encoding="utf-8"))
        devs = [d for s in data for h in s.get("homes", []) for d in h.get("devices", [])]
        match = next((d for d in devs if d.get("localip") == ip or (did and str(d.get("did")) == str(did))), None)
        if not match:
            return print(f"That file has no device at {ip}.")
        token, model, name = match["token"], match.get("model"), match.get("name")
    if len(token) != 32:
        return print("A token is exactly 32 characters (0-9, a-f).")

    print("\nConnecting…")
    try:
        info = Device(ip, token, timeout=5).info()
        model = info.model or model
    except Exception as e:
        return print(f"Could not talk to {ip} with that token: {e}")
    print(f"Connected: {model}")
    if model != XiaomiDriver.SUPPORTED_MODEL:
        print(f"Note: controls are mapped for {XiaomiDriver.SUPPORTED_MODEL}; this model may not respond to all of them.")

    name = ask("Name to show on the widget (e.g. Bedroom)", name or f"Purifier {len(cfg['purifiers']) + 1}")
    cfg["purifiers"].append({"type": "xiaomi", "name": name, "ip": ip, "token": token, "model": model})
    save_cfg(cfg)
    print(f"\nAdded {name}. The widget picks it up automatically: click the room name to switch.")


def add_qubo():
    cloud = qubo()
    try:
        if cloud.signed_in:
            cloud.purifiers()
    except Exception:
        cloud.tokens = None
    if not cloud.signed_in:
        print("\nSign in with the account you use in the Qubo app.")
        how = ask("Sign in with  1. email + password   2. phone + SMS code", "1")
        try:
            if how == "2":
                isd = ask("Country code", "+91")
                phone = "".join(ch for ch in (ask("Phone number") or "") if ch.isdigit())
                cloud.request_phone_code(phone, isd)
                code = "".join(ch for ch in (ask("Code from the SMS") or "") if ch.isdigit())
                cloud.verify_phone(phone, code, isd)
                if not cloud.tokens.get("refresh"):
                    print("Signed in, but only for about an hour. Add an email to your Qubo profile to stay signed in.")
            else:
                email = ask("Email")
                password = ask("Password (not shown)", secret=True)
                cloud.login_email(email, password)
        except Exception as e:
            return print(f"Sign-in failed: {e}")
        print("Signed in.")

    try:
        devices = cloud.purifiers()
    except Exception as e:
        return print(f"Could not list your Qubo devices: {e}")
    cfg = load_cfg()
    known = {c.get("device", {}).get("id") for c in cfg["purifiers"] if c.get("type") == "qubo"}
    new = [d for d in devices if d["id"] not in known]
    if not new:
        return print("No new Qubo purifiers in this account (already-added ones are hidden).")
    for i, d in enumerate(new, 1):
        print(f"  {i}. {d['name']:<24} {d['model']}")
    choice = ask("Number", "1")
    if not (choice and choice.isdigit() and 1 <= int(choice) <= len(new)):
        return print("Cancelled.")
    d = new[int(choice) - 1]
    try:
        QuboDriver({"device": d}).read()
        print("Connected.")
    except Exception as e:
        print(f"Note: couldn't read it yet ({e}). Saving anyway.")
    name = ask("Name to show on the widget", d["name"])
    cfg["purifiers"].append({"type": "qubo", "name": name, "device": d})
    save_cfg(cfg)
    print(f"\nAdded {name}. The widget picks it up automatically: click the room name to switch.")


def cmd_remove(name):
    cfg = load_cfg()
    keep = [c for c in cfg["purifiers"] if c.get("name", "").lower() != name.lower()]
    if len(keep) == len(cfg["purifiers"]):
        sys.exit(f"No purifier called {name!r}.")
    cfg["purifiers"] = keep
    save_cfg(cfg)
    print(f"Removed {name}.")


def onoff(x):
    if x not in ("on", "off"):
        sys.exit("expected on or off")
    return x == "on"


def main(a):
    name = None
    if a[:1] in (["-p"], ["--purifier"]):
        name, a = a[1], a[2:]
    cmd = a[0] if a else "status"
    if cmd == "list":
        return cmd_list()
    if cmd == "add":
        cmd_add()
        if sys.stdin.isatty():
            input("\nPress Enter to close.")
        return
    if cmd == "remove":
        return cmd_remove(a[1])
    p = pick(name)
    if cmd == "status":
        return status(p)
    if cmd in ("on", "off"):
        p.set_power(cmd == "on")
    elif cmd == "mode":
        keys = [k for k, _ in p.modes]
        if a[1] not in keys:
            sys.exit(f"{p.name} modes: {', '.join(keys)}")
        p.set_mode(a[1])
    elif cmd == "level":
        if not p.fan_range:
            sys.exit(f"{p.name} has no adjustable fan speed.")
        p.set_fan(int(a[1]))
    elif cmd == "lock":
        p.set_lock(onoff(a[1]))
    elif cmd in ("beep", "screen") and p.type == "xiaomi":
        if cmd == "beep":
            p.driver.set_prop("beep", onoff(a[1]))
        else:
            p.driver.set_prop("screen", {v: k for k, v in XiaomiDriver.SCREEN.items()}[a[1]])
    else:
        sys.exit(__doc__)
    status(p)


if __name__ == "__main__":
    main(sys.argv[1:])
