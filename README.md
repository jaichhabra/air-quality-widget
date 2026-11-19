# Air Quality desktop widget

> **On a Mac with a Qubo purifier?** See [mac/](mac/README.md) for the native
> macOS version.

A frosted-glass Windows 11 desktop widget that shows indoor air quality from a
**Xiaomi Smart Air Purifier** next to outdoor air quality for your location, and
lets you control the purifier. It talks to the purifier directly over your Wi-Fi,
not through Xiaomi's cloud.

- **Inside**: live PM2.5 from the purifier (every 30 s), shown as AQI with a
  colour scale, plus power, mode and fan speed controls.
- **Several purifiers**: click the room name to switch between them, or choose
  *Add purifier…* to find a new one on your network.
- **Outside**: PM2.5 for your coordinates (every 10 min), from the nearest
  official monitoring station via [WAQI](https://aqicn.org/data-platform/token/)
  if you add a free token, otherwise Open-Meteo's modelled estimate.
- **AQI scale**: US EPA (matches aqi.in) or India NAQI (CPCB), switchable from
  the right-click menu.
- **Real glass**: the wallpaper behind the widget is blurred and tinted, and the
  window has per-pixel transparency, so the rounded card is exactly the window.
  It sits on the desktop, behind your apps.

## Requirements

- Windows 11 (uses Segoe UI Variable and Segoe Fluent Icons)
- Python 3.10+
- A Xiaomi purifier on the same network. Built for `xiaomi.airp.cpa4m`
  (Smart Air Purifier 4 Compact); other MIoT purifiers may need different
  property ids in `purifier.py`.

```bash
pip install --no-deps python-miio==0.5.12
pip install pillow netifaces-plus android_backup appdirs attrs click "construct<3,>=2.10.56" croniter cryptography defusedxml micloud pytz PyYAML tqdm zeroconf
```

`python-miio` is installed with `--no-deps` because its `netifaces` dependency
does not build on Windows; `netifaces-plus` replaces it.

## Setup

1. Get each purifier's token with
   [Xiaomi Cloud Tokens Extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)
   (QR-code login works for accounts created with Sign in with Apple). Save its
   output with `-o devices.json` to skip copying tokens by hand.
2. Add your purifiers. The wizard scans your network, asks for the token (or
   the extractor's JSON file), tests the connection and saves it:

   ```bash
   python purifier.py add
   ```

3. Copy `location` and `waqi_token` from `purifier.example.json` into
   `purifier.json` and set them:
   - `location`: your coordinates, a short label, and the AQI page to open
     when you double-click Outside.
   - `waqi_token` (optional): for real station readings outdoors.

   `purifier.json` is git-ignored. Keep it private: a token lets anyone on your
   network control that purifier.
4. Start the widget by double-clicking `aqi_widget.pyw`.

## Using it

| Action | How |
| --- | --- |
| Move | Drag anywhere on the card |
| Switch or add a purifier | Click the room name (top left) |
| Power | Round button, bottom left (green when on) |
| Mode and fan speed | Mode pill, opens a dropdown |
| AQI scale, refresh, close | Right-click |
| Open your AQI reference page | Double-click Outside |

## Command line

```bash
python purifier.py list             # configured purifiers
python purifier.py add              # find and add a purifier
python purifier.py remove NAME
python purifier.py                  # status of the first purifier
python purifier.py -p Bedroom on    # pick a purifier by name or number
python purifier.py on | off
python purifier.py mode auto|sleep|favorite
python purifier.py level 0-14       # fan level (Favorite mode)
python purifier.py lock on|off
python purifier.py beep on|off
python purifier.py screen off|dim|bright
```

## Notes

- AQI here is computed from PM2.5 only, using live readings. Official indices
  also use other pollutants and 24-hour averages, so they can differ.
- If the purifier's IP changes, update `purifier.json`, or reserve the address
  in your router.
