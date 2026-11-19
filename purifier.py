"""Control Xiaomi Smart Air Purifiers (xiaomi.airp.cpa4m) over the local network.

Purifiers are listed in purifier.json (see purifier.example.json).

Usage:
  python purifier.py [-p NAME] [command]     NAME: purifier name or number (default: the first)

  status                      (default)
  on | off
  mode auto|sleep|favorite
  level 0-14                  fan level for favorite mode
  lock on|off                 child lock
  beep on|off
  screen off|dim|bright

  list                        list configured purifiers
  add                         find a purifier on your network and add it
  remove NAME                 remove a purifier
"""
import ipaddress
import json
import pathlib
import select
import socket
import sys
import time

from miio import Device

CFG_PATH = pathlib.Path(__file__).with_name("purifier.json")
SUPPORTED_MODEL = "xiaomi.airp.cpa4m"

# (siid, piid) from the device's MIoT spec
P = {
    "power": (2, 1), "fault": (2, 2), "mode": (2, 4),
    "pm25": (3, 4),
    "filter_life": (4, 1), "filter_used_h": (4, 3), "filter_left_days": (4, 4),
    "beep": (6, 1), "lock": (8, 1), "fan_level": (9, 11), "screen": (13, 2),
}
MODES = {0: "auto", 1: "sleep", 2: "favorite"}
SCREEN = {0: "off", 1: "bright", 2: "dim"}
FAULTS = {0: "none", 2: "motor stuck", 3: "sensor lost"}
FAN_MAX = 14


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


# ── device ─────────────────────────────────────────────────────────────────
class Purifier:
    def __init__(self, c):
        self.cfg = c
        self.name = c.get("name") or c["ip"]
        self.ip = c["ip"]
        self.dev = Device(c["ip"], c["token"], timeout=5)

    def send(self, method, params, tries=3):
        # the purifier's Wi-Fi dozes; the first handshake sometimes times out
        for i in range(tries):
            try:
                return self.dev.send(method, params)
            except Exception:
                if i == tries - 1:
                    raise

    def get_all(self):
        req = [{"did": k, "siid": s, "piid": p} for k, (s, p) in P.items()]
        return {r["did"]: r.get("value") for r in self.send("get_properties", req)}

    def set_prop(self, name, value):
        s, p = P[name]
        res = self.send("set_properties", [{"did": name, "siid": s, "piid": p, "value": value}])
        if res[0].get("code") != 0:
            raise RuntimeError(f"{self.name} refused {name}={value}: {res[0]}")


def purifiers(cfg=None):
    return [Purifier(c) for c in (cfg or load_cfg())["purifiers"]]


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


# ── network scan ───────────────────────────────────────────────────────────
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
    print(f"{p.name} ({p.ip})")
    print(f"  Power:        {'ON' if v['power'] else 'off'}")
    print(f"  Mode:         {MODES.get(v['mode'], v['mode'])}  (favorite level {v['fan_level']})")
    print(f"  PM2.5:        {v['pm25']} µg/m³")
    print(f"  Filter:       {v['filter_life']}% left, ~{v['filter_left_days']} days, used {v['filter_used_h']} h")
    print(f"  Child lock:   {'on' if v['lock'] else 'off'}")
    print(f"  Beep:         {'on' if v['beep'] else 'off'}")
    print(f"  Screen:       {SCREEN.get(v['screen'], v['screen'])}")
    print(f"  Fault:        {FAULTS.get(v['fault'], v['fault'])}")


def cmd_list():
    items = load_cfg()["purifiers"]
    if not items:
        print("No purifiers yet. Run:  python purifier.py add")
    for i, c in enumerate(items, 1):
        print(f"  {i}. {c.get('name', '?'):<20} {c['ip']:<16} {c.get('model', '')}")


def ask(prompt, default=None):
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip().lstrip("﻿").strip()
    except EOFError:
        return None
    return v or default


def cmd_add():
    """Interactive: pick a device found on the network, give its token and a name, test, save."""
    cfg = load_cfg()
    known = {c["ip"] for c in cfg["purifiers"]}
    print("\nAdd an air purifier\n───────────────────")
    print("Looking for Xiaomi devices on your network…")
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
    if model != SUPPORTED_MODEL:
        print(f"Note: controls are mapped for {SUPPORTED_MODEL}; this model may not respond to all of them.")

    name = ask("Name to show on the widget (e.g. Bedroom)", name or f"Purifier {len(cfg['purifiers']) + 1}")
    cfg["purifiers"].append({"name": name, "ip": ip, "token": token, "model": model})
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
        p.set_prop("power", cmd == "on")
    elif cmd == "mode":
        p.set_prop("mode", {v: k for k, v in MODES.items()}[a[1]])
    elif cmd == "level":
        p.set_prop("fan_level", max(0, min(FAN_MAX, int(a[1]))))
    elif cmd == "lock":
        p.set_prop("lock", onoff(a[1]))
    elif cmd == "beep":
        p.set_prop("beep", onoff(a[1]))
    elif cmd == "screen":
        p.set_prop("screen", {v: k for k, v in SCREEN.items()}[a[1]])
    else:
        sys.exit(__doc__)
    status(p)


if __name__ == "__main__":
    main(sys.argv[1:])
