# Air Quality for Mac (Qubo)

A glass desktop widget for macOS that shows indoor air quality from a **Qubo
smart air purifier** next to outdoor air quality, and controls the purifier.

- **Inside**: PM2.5 from your Qubo purifier as AQI, with power, mode
  (Auto / Sleep / Manual), fan speed (1–3), child lock and filter life.
  Several purifiers? Click the room name to switch, or rename one.
- **Outside**: any location you search for. Uses the nearest official station
  with a free [WAQI token](https://aqicn.org/data-platform/token/), otherwise
  Open-Meteo's modelled estimate.
- **US AQI** (matches aqi.in) or **India NAQI**, from the right-click menu.
- Real macOS glass (behind-window blur). Sits on the desktop below your
  windows, on every Space. Drag it anywhere.

Requires macOS 13 Ventura or later. Apple Silicon and Intel.

## Install

1. Download **AirQuality-mac** from the latest successful run on the repo's
   [Actions → macOS app](../../../actions/workflows/mac.yml) page and unzip it.
2. Move **Air Quality.app** to Applications.
3. The app is not notarized by Apple, so macOS blocks it the first time.
   Right-click it → **Open** → **Open**, or run:

   ```bash
   xattr -dr com.apple.quarantine "/Applications/Air Quality.app"
   ```

4. Sign in with your Qubo account. Email and password keeps you signed in for
   about six months. Phone + SMS code also works; add an email to your Qubo
   profile so it lasts longer than an hour.

Right-click the widget for Settings (outside location, WAQI token), the AQI
scale, **Open at login**, sign out and quit.

## Build it yourself

```bash
cd mac
swift test                 # unit tests
bash scripts/make_app.sh   # builds dist/Air Quality.app and dist/AirQuality-mac.zip
```

Only the Xcode Command Line Tools are needed (`xcode-select --install`).

## How it talks to Qubo

Qubo has no public API. The client in `Sources/AirQualityCore/QuboClient.swift`
uses the cloud endpoints documented by the open-source
[qubopy](https://github.com/selvakk2k/qubopy) library (MIT), which
reverse-engineered them from the Qubo Android app. That means:

- It needs the internet and Qubo's servers. The purifier doesn't have to be on
  the same network as the Mac.
- Purifier support in qubopy is marked partly unverified on hardware, and
  Qubo can change its API at any time. If something stops working, check
  qubopy's changelog.
- Your sign-in session is stored in the macOS Keychain. Your password is only
  sent to Qubo, never stored.

This project is not affiliated with Qubo or Hero Electronix.
