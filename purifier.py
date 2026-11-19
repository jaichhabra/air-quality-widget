"""Control a Xiaomi Smart Air Purifier (xiaomi.airp.cpa4m) over the local network.

Reads the device IP and token from purifier.json (see purifier.example.json).

Usage:
  python purifier.py                 status
  python purifier.py on | off
  python purifier.py mode auto|sleep|favorite
  python purifier.py level 0-14      fan level for favorite mode
  python purifier.py lock on|off     child lock
  python purifier.py beep on|off
  python purifier.py screen off|dim|bright
"""
import json
import pathlib
import sys

from miio import Device

CFG = json.loads(pathlib.Path(__file__).with_name("purifier.json").read_text(encoding="utf-8-sig"))

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

dev = Device(CFG["ip"], CFG["token"], timeout=5)


def send(method, params, tries=3):
    # the purifier's Wi-Fi dozes; the first handshake sometimes times out
    for i in range(tries):
        try:
            return dev.send(method, params)
        except Exception:
            if i == tries - 1:
                raise


def get_all():
    req = [{"did": k, "siid": s, "piid": p} for k, (s, p) in P.items()]
    return {r["did"]: r.get("value") for r in send("get_properties", req)}


def set_prop(name, value):
    s, p = P[name]
    res = send("set_properties", [{"did": name, "siid": s, "piid": p, "value": value}])
    if res[0].get("code") != 0:
        sys.exit(f"Device refused {name}={value}: {res[0]}")


def status():
    v = get_all()
    print(f"{CFG['name']} ({CFG['ip']})")
    print(f"  Power:        {'ON' if v['power'] else 'off'}")
    print(f"  Mode:         {MODES.get(v['mode'], v['mode'])}  (favorite level {v['fan_level']})")
    print(f"  PM2.5:        {v['pm25']} µg/m³")
    print(f"  Filter:       {v['filter_life']}% left, ~{v['filter_left_days']} days, used {v['filter_used_h']} h")
    print(f"  Child lock:   {'on' if v['lock'] else 'off'}")
    print(f"  Beep:         {'on' if v['beep'] else 'off'}")
    print(f"  Screen:       {SCREEN.get(v['screen'], v['screen'])}")
    print(f"  Fault:        {FAULTS.get(v['fault'], v['fault'])}")


def onoff(x):
    if x not in ("on", "off"):
        sys.exit("expected on or off")
    return x == "on"


def main(a):
    if not a:
        return status()
    cmd = a[0]
    if cmd in ("on", "off"):
        set_prop("power", cmd == "on")
    elif cmd == "mode":
        set_prop("mode", {v: k for k, v in MODES.items()}[a[1]])
    elif cmd == "level":
        set_prop("fan_level", max(0, min(14, int(a[1]))))
    elif cmd == "lock":
        set_prop("lock", onoff(a[1]))
    elif cmd == "beep":
        set_prop("beep", onoff(a[1]))
    elif cmd == "screen":
        set_prop("screen", {v: k for k, v in SCREEN.items()}[a[1]])
    else:
        sys.exit(__doc__)
    status()


if __name__ == "__main__":
    main(sys.argv[1:])
