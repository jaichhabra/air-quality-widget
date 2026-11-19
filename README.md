# Air Quality desktop widget

A frosted-glass Windows 11 desktop widget that shows indoor air quality from a
**Xiaomi Smart Air Purifier** next to outdoor air quality for your location, and
lets you control the purifier. It talks to the purifier directly over your Wi-Fi,
not through Xiaomi's cloud.

- **Inside**: live PM2.5 from the purifier (every 30 s), shown as AQI with a
  colour scale, plus power, mode and fan speed controls.
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

1. Copy `purifier.example.json` to `purifier.json` and fill it in. That file is
   git-ignored, keep it private: the token lets anyone on your network control
   the purifier.
   - `ip`, `token`, `model`: get them with
     [Xiaomi Cloud Tokens Extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)
     (QR-code login works for accounts created with Sign in with Apple).
   - `location`: your coordinates, a short label, and the AQI page to open
     when you double-click Outside.
   - `waqi_token` (optional): for real station readings outdoors.
2. Check the connection:

   ```bash
   python purifier.py
   ```

3. Start the widget by double-clicking `aqi_widget.pyw`.

## Using it

| Action | How |
| --- | --- |
| Move | Drag anywhere on the card |
| Power | Round button, bottom left (green when on) |
| Mode and fan speed | Mode pill, opens a dropdown |
| AQI scale, refresh, close | Right-click |
| Open your AQI reference page | Double-click Outside |

## Command line

```bash
python purifier.py                  # status
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
