"""Air Quality — a frosted-glass desktop widget.

Inside: Xiaomi purifier.  Outside: your location from purifier.json (nearest WAQI station if
"waqi_token" is in purifier.json, otherwise Open-Meteo's modelled PM2.5).

Lives on the desktop behind your windows. Drag to move · double-click Outside to
open aqi.in · right-click for the menu · ✕ to close.

The glass is real: the wallpaper behind the widget is blurred, tinted and
composited with a soft shadow and a light edge, all rendered anti-aliased at 3×.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import pathlib
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
import winreg

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

try:  # crisp on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import purifier as pur

INSIDE_MS = 30_000
OUTDOOR_MS = 10 * 60_000
LOC = pur.CFG.get("location", {})          # kept in purifier.json, never committed
HOME = (LOC.get("lat", 28.6139), LOC.get("lon", 77.2090))  # default: New Delhi
PLACE = LOC.get("label", "Outside")
REFERENCE_URL = LOC.get("reference_url", "https://www.aqi.in/")
REFERENCE_NAME = LOC.get("reference_name", "aqi.in")
POS_FILE = pathlib.Path(__file__).with_name("aqi_widget_pos.json")

# ── Air quality maths ──────────────────────────────────────────────────────
BANDS = [  # India NAQI, PM2.5: (conc_lo, conc_hi, aqi_lo, aqi_hi, label)
    (0, 30, 0, 50, "Good"),
    (30, 60, 51, 100, "Satisfactory"),
    (60, 90, 101, 200, "Moderate"),
    (90, 120, 201, 300, "Poor"),
    (120, 250, 301, 400, "Very Poor"),
    (250, 600, 401, 500, "Severe"),
]
US_BP = [(0, 50, 0.0, 9.0), (51, 100, 9.1, 35.4), (101, 150, 35.5, 55.4),
         (151, 200, 55.5, 125.4), (201, 300, 125.5, 225.4), (301, 500, 225.5, 325.4)]


US_LABELS = ["Good", "Moderate", "Unhealthy for Some", "Unhealthy", "Very Unhealthy", "Hazardous"]
SCALES = {"us": "US AQI (like aqi.in)", "in": "India NAQI (CPCB)"}


def aqi_from_pm25(pm, scale="us"):
    if scale == "us":  # US EPA 2024 PM2.5 breakpoints
        for (ilo, ihi, clo, chi), label in zip(US_BP, US_LABELS):
            if pm <= chi + 0.05:
                pm = max(pm, clo)
                return round(ilo + (pm - clo) * (ihi - ilo) / (chi - clo)), label
        return 500, "Hazardous"
    for clo, chi, alo, ahi, label in BANDS:
        if pm <= chi:
            return round(alo + (pm - clo) * (ahi - alo) / (chi - clo)), label
    return 500, "Severe"


def us_index_to_pm25(idx):
    for ilo, ihi, clo, chi in US_BP:
        if idx <= ihi:
            return clo + (idx - ilo) * (chi - clo) / (ihi - ilo)
    return 325.4


def fetch_outdoor():
    """(pm25 µg/m³, caption). Nearest station if a WAQI token is set, else Open-Meteo model."""
    lat, lon = HOME
    token = pur.CFG.get("waqi_token")
    note = "estimate"
    if token:
        try:
            d = json.load(urllib.request.urlopen(
                f"https://api.waqi.info/feed/geo:{lat};{lon}/?token={urllib.parse.quote(token)}", timeout=15))
        except Exception:
            d = {}
        if d.get("status") == "ok" and "pm25" in d["data"].get("iaqi", {}):
            return us_index_to_pm25(d["data"]["iaqi"]["pm25"]["v"]), d["data"]["city"]["name"].split(",")[0]
        note = "estimate (token not accepted)" if d.get("status") == "error" else "estimate (station offline)"
    d = json.load(urllib.request.urlopen(
        f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=pm2_5",
        timeout=15))
    return d["current"]["pm2_5"], f"{PLACE} · {note}"


# ── Design tokens (logical px) ─────────────────────────────────────────────
SS = 3                      # supersampling for anti-aliasing
CARD_W, CARD_H, RADIUS = 348, 224, 26
MARGIN = 0                  # window = card; corners are truly transparent
WIN_W, WIN_H = CARD_W + 2 * MARGIN, CARD_H + 2 * MARGIN
POP_W, POP_H, POP_R = 236, 214, 18   # mode / fan-speed dropdown
ROOM_W = 220                          # purifier (room) switcher
BUTTONS = ("power", "mode", "room", "refresh", "close")
def pop_h(n_modes, has_fan):
    """Height of the mode/fan dropdown for a purifier with n_modes modes."""
    return 8 + n_modes * 36 + (98 if has_fan else 8)
PAD = 22
GREEN = (48, 209, 88)
SCALE = [(0, (48, 209, 88)), (100, (255, 214, 10)), (200, (255, 159, 10)),
         (300, (255, 69, 58)), (400, (191, 90, 242)), (500, (162, 56, 79))]
FONT_FILE = r"C:\Windows\Fonts\SegUIVar.ttf"
ICON_FILE = r"C:\Windows\Fonts\SegoeIcons.ttf"
G_HOME, G_PIN, G_POWER, G_CHEVRON, G_REFRESH, G_CLOSE = "\uE80F", "\uE81D", "\uE7E8", "\uE70D", "\uE72C", "\uE8BB"


def white(a):
    return (255, 255, 255, round(255 * a))


def scale_rgb(v):
    v = max(0, min(500, v))
    for (v0, c0), (v1, c1) in zip(SCALE, SCALE[1:]):
        if v <= v1:
            t = (v - v0) / (v1 - v0)
            return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
    return SCALE[-1][1]


# ── Wallpaper (for the glass) ──────────────────────────────────────────────
class _MI(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitor_rect(x, y):
    u = ctypes.windll.user32
    u.MonitorFromPoint.restype = wt.HANDLE
    h = u.MonitorFromPoint(wt.POINT(x, y), 2)
    mi = _MI(cbSize=ctypes.sizeof(_MI))
    u.GetMonitorInfoW(h, ctypes.byref(mi))
    r = mi.rcMonitor
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def wallpaper_source():
    path = pathlib.Path(os.environ["APPDATA"]) / "Microsoft/Windows/Themes/TranscodedWallpaper"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as k:
            style = int(winreg.QueryValueEx(k, "WallpaperStyle")[0] or 10)
            tile = int(winreg.QueryValueEx(k, "TileWallpaper")[0] or 0)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Colors") as k:
            bg = tuple(int(c) for c in winreg.QueryValueEx(k, "Background")[0].split())
    except OSError:
        style, tile, bg = 10, 0, (0, 0, 0)
    return path, style, tile, bg


def render_wallpaper(size):
    """The wallpaper as Windows draws it on a monitor of `size`."""
    path, style, tile, bg = wallpaper_source()
    canvas = Image.new("RGB", size, bg)
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return canvas
    if tile:
        for x in range(0, size[0], img.width):
            for y in range(0, size[1], img.height):
                canvas.paste(img, (x, y))
        return canvas
    if style == 2:      # stretch
        return img.resize(size, Image.LANCZOS)
    if style == 6:      # fit
        return ImageOps.pad(img, size, Image.LANCZOS, color=bg)
    if style == 0:      # centre
        canvas.paste(img, ((size[0] - img.width) // 2, (size[1] - img.height) // 2))
        return canvas
    # fill (default), span. Windows anchors the crop a third of the way down, not at the
    # centre (measured against a real desktop screenshot: <1 level mean error).
    return ImageOps.fit(img, size, Image.LANCZOS, centering=(0.5, 1 / 3))


# ── Renderer ───────────────────────────────────────────────────────────────
class Renderer:
    def __init__(self, s):
        self.s, self.k = s, s * SS
        self.size = (round(WIN_W * s), round(WIN_H * s))
        self._fonts = {}
        self._wp_key = None
        self._build_static()

    def q(self, v):
        return round(v * self.k)

    def font(self, px, wght=400, icon=False):
        key = (px, wght, icon)
        if key not in self._fonts:
            if icon:
                f = ImageFont.truetype(ICON_FILE, self.q(px))
            else:
                f = ImageFont.truetype(FONT_FILE, self.q(px))
                f.set_variation_by_axes([wght, max(5, min(36, px * 0.75))])
            self._fonts[key] = f
        return self._fonts[key]

    def _build_static(self):
        self._layers = {}

    def _glass_layers(self, w, h, radius):
        """Mask, light edge, sheen and tint for a glass panel of logical size w×h (cached)."""
        key = (w, h, radius)
        if key not in self._layers:
            size = (round(w * self.s), round(h * self.s))
            big = (self.q(w), self.q(h))
            box = [0, 0, big[0] - 1, big[1] - 1]
            m = Image.new("L", big, 0)
            ImageDraw.Draw(m).rounded_rectangle(box, self.q(radius), fill=255)
            mask = m.resize(size, Image.LANCZOS)
            # light edge: 1px stroke, brighter at the top
            e = Image.new("L", big, 0)
            ImageDraw.Draw(e).rounded_rectangle(box, self.q(radius), outline=255, width=max(1, round(self.k)))
            e = e.resize(size, Image.LANCZOS)
            grad = Image.linear_gradient("L").resize(size).point(lambda v: round(80 - v * 62 / 255))
            ones = Image.new("L", size, 255)
            edge = Image.merge("RGBA", (ones, ones, ones, Image.composite(grad, Image.new("L", size, 0), e)))
            sh = Image.linear_gradient("L").resize(size).point(lambda v: max(0, round(30 - v * 60 / 255)))
            sheen = Image.merge("RGBA", (ones, ones, ones, sh))
            tint = Image.new("RGBA", size, (18, 18, 22, 118))
            self._layers[key] = (size, mask, edge, sheen, tint)
        return self._layers[key]

    def _wallpaper(self, mon):
        path = wallpaper_source()[0]
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        key = (mon[2], mon[3], mtime)
        if key != self._wp_key:
            raw = render_wallpaper((mon[2], mon[3]))
            f = 8
            small = raw.resize((max(1, raw.width // f), max(1, raw.height // f)), Image.BILINEAR)
            small = small.filter(ImageFilter.GaussianBlur(34 * self.s / f))
            small = ImageEnhance.Color(small).enhance(1.45)
            self.raw, self.blur = raw, small.resize(raw.size, Image.BICUBIC)
            self._wp_key = key

    def backdrop(self, x, y, w=CARD_W, h=CARD_H, radius=RADIUS, tint=None):
        """Frosted-glass panel (RGBA, transparent outside the rounded corners) for a window at (x, y)."""
        (W, H), mask, edge, sheen, tint_layer = self._glass_layers(w, h, radius)
        mon = monitor_rect(x + W // 2, y + H // 2)
        self._wallpaper(mon)
        bx, by = x - mon[0], y - mon[1]
        glass = self.blur.crop((bx, by, bx + W, by + H)).convert("RGBA")
        if tint:
            tint_layer = Image.new("RGBA", (W, H), tint)
        glass = Image.alpha_composite(Image.alpha_composite(glass, tint_layer), sheen)
        glass = Image.alpha_composite(glass, edge)
        glass.putalpha(mask)
        return glass

    # content
    def content(self, st):
        """Returns (RGBA layer at window size, hit regions in logical px)."""
        q = self.q
        img = Image.new("RGBA", (q(WIN_W), q(WIN_H)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        hits = []
        ox, oy = MARGIN, MARGIN

        def text(x, y, s, px, wght=400, fill=white(1), anchor="ls", track=0.0, icon=False):
            f = self.font(px, wght, icon)
            if not track:
                d.text((q(ox + x), q(oy + y)), s, font=f, fill=fill, anchor=anchor)
                return f.getlength(s) / self.k
            cx = q(ox + x)
            for ch in s:
                d.text((cx, q(oy + y)), ch, font=f, fill=fill, anchor=anchor)
                cx += f.getlength(ch) + q(track)
            return (cx - q(ox + x)) / self.k

        def rrect(x0, y0, x1, y1, r, **kw):
            d.rounded_rectangle([q(ox + x0), q(oy + y0), q(ox + x1), q(oy + y1)], q(r), **kw)

        colw = (CARD_W - 2 * PAD - 36) / 2
        for i, (title, glyph, data, tag) in enumerate(
                [(st.get("room", "Inside"), G_HOME, st["inside"], "inside"),
                 ("Outside", G_PIN, st["outside"], "outside")]):
            x = PAD + i * (colw + 36)
            title = title.upper()
            if i == 0:  # room name: click to switch purifier or add one
                f = self.font(10.5, 600)
                while len(title) > 3 and (f.getlength(title) / self.k + 0.9 * len(title)) > colw - 34:
                    title = title[:-2] + "…"
                hot = st.get("hover") == "room"
                if hot:
                    tw = f.getlength(title) / self.k + 0.9 * len(title)
                    rrect(x - 7, 17, x + 17 + tw + 20, 37, 10, fill=white(0.12))
                tw = text(x + 17, 32, title, 10.5, 600, white(0.85 if hot else 0.55), track=0.9)
                text(x, 32, glyph, 10.5, icon=True, fill=white(0.85 if hot else 0.55))
                text(x + 17 + tw + 3, 31, G_CHEVRON, 7.5, icon=True, fill=white(0.85 if hot else 0.5))
                hits.append((ox + x - 7, oy + 17, ox + x + 17 + tw + 20, oy + 37, "room"))
            else:
                text(x, 32, glyph, 10.5, icon=True, fill=white(0.55))
                text(x + 17, 32, title, 10.5, 600, white(0.55), track=0.9)
            unit = ""
            if data is None:
                num, cat, cap1, cap2, aqi = "—", "Loading…", "", "", None
            elif data == "offline":
                num, cat, cap1, cap2, aqi = "—", "Unavailable", "Retrying shortly", "", None
            else:
                pm, cap2 = data
                scale = st.get("scale", "us")
                aqi, cat = aqi_from_pm25(pm, scale)
                index = "US AQI" if scale == "us" else "NAQI"
                if st.get("metric") == "pm25":  # same number the purifier's own app shows
                    num, unit, cap1 = f"{pm:.0f}", "µg/m³", f"{index} {aqi}"
                else:
                    num, unit, cap1 = str(aqi), index, f"PM2.5 {pm:.0f} µg/m³"
            w = text(x - 2, 86, num, 46, 600)
            if unit:
                text(x - 2 + w + 5, 86, unit, 10.5, 600, white(0.5))
            text(x, 109, cat, 15, 600)
            self._bar(img, ox + x, oy + 121, colw, aqi)
            text(x, 145, cap1, 11.5, 400, white(0.62))
            text(x, 160, cap2, 11.5, 400, white(0.42))
            hits.append((ox + x - 6, oy + 14, ox + x + colw + 6, oy + 166, tag))

        # hairlines
        d.line([q(ox + CARD_W / 2), q(oy + 22), q(ox + CARD_W / 2), q(oy + 160)], fill=white(0.12), width=max(1, q(0.5)))
        d.line([q(ox + PAD), q(oy + 176), q(ox + CARD_W - PAD), q(oy + 176)], fill=white(0.12), width=max(1, q(0.5)))

        # footer
        fy, ph = 200, 28
        hov = st.get("hover")
        v = st.get("vals") or {}
        on = v.get("power")
        x = PAD

        if v:
            # power: round toggle, green when on
            r = ph / 2
            cx = x + r
            if on:
                fill = tuple(min(255, c + 22) for c in GREEN) + (255,) if hov == "power" else GREEN + (240,)
            else:
                fill = white(0.20 if hov == "power" else 0.11)
            d.ellipse([q(ox + cx - r), q(oy + fy - r), q(ox + cx + r), q(oy + fy + r)], fill=fill,
                      outline=None if on else white(0.10), width=max(1, q(0.5)))
            text(cx, fy, G_POWER, 12, icon=True, fill=white(1 if on else 0.75), anchor="mm")
            hits.append((ox + x, oy + fy - r, ox + x + ph, oy + fy + r, "power"))
            x += ph + 8

            # mode: pill with chevron
            mode = v.get("mode_label") or "—"
            w = 14 + self.font(13, 600).getlength(mode) / self.k + 8 + 9 + 12
            rrect(x, fy - ph / 2, x + w, fy + ph / 2, ph / 2,
                  fill=white(0.20 if hov == "mode" else 0.11), outline=white(0.10), width=max(1, q(0.5)))
            text(x + 14, fy + 4.5, mode, 13, 600)
            text(x + w - 12 - 9, fy + 3.5, G_CHEVRON, 9, icon=True, fill=white(0.6))
            hits.append((ox + x, oy + fy - ph / 2, ox + x + w, oy + fy + ph / 2, "mode"))
        else:
            text(PAD, fy + 4.5, "Connecting to purifier…", 12, 400, white(0.5))

        for cx, glyph, tag in [(CARD_W - PAD - 46, G_REFRESH, "refresh"), (CARD_W - PAD - 14, G_CLOSE, "close")]:
            if hov == tag:
                d.ellipse([q(ox + cx - 14), q(oy + fy - 14), q(ox + cx + 14), q(oy + fy + 14)], fill=white(0.16))
            text(cx, fy, glyph, 12, icon=True, fill=white(1 if hov == tag else 0.62), anchor="mm")
            hits.append((ox + cx - 14, oy + fy - 14, ox + cx + 14, oy + fy + 14, tag))

        if st.get("updated"):
            text(CARD_W - PAD - 66, fy + 4, st["updated"], 11, 400, white(0.42), anchor="rs")

        return img.resize(self.size, Image.LANCZOS), hits

    # dropdown: purifiers + add
    def room_content(self, st):
        q = self.q
        n = len(st["items"])
        W, H = ROOM_W, room_popup_h(n)
        img = Image.new("RGBA", (q(W), q(H)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        hits, hov = [], st.get("hover")

        def text(x, y, s, px, wght=400, fill=white(1), anchor="ls", icon=False):
            d.text((q(x), q(y)), s, font=self.font(px, wght, icon), fill=fill, anchor=anchor)

        for i, name in enumerate(st["items"]):
            y0, tag, sel = 8 + i * 36, f"room{i}", i == st["sel"]
            if hov == tag:
                d.rounded_rectangle([q(6), q(y0), q(W - 6), q(y0 + 34)], q(10), fill=white(0.13))
            text(18, y0 + 17, G_HOME, 11, icon=True, fill=white(0.9 if sel else 0.5), anchor="lm")
            text(38, y0 + 22, name, 13.5, 600 if sel else 400, white(1 if sel else 0.85))
            if sel:
                text(W - 20, y0 + 17, "", 12, icon=True, fill=white(0.95), anchor="mm")
            hits.append((6, y0, W - 6, y0 + 34, tag))

        ys = 8 + n * 36 + 4
        d.line([q(16), q(ys), q(W - 16), q(ys)], fill=white(0.12), width=max(1, q(0.5)))
        y0 = ys + 5
        if hov == "add":
            d.rounded_rectangle([q(6), q(y0), q(W - 6), q(y0 + 34)], q(10), fill=white(0.13))
        text(18, y0 + 17, "", 11, icon=True, fill=white(0.85), anchor="lm")
        text(38, y0 + 22, "Add purifier…", 13.5, 400, white(0.85))
        hits.append((6, y0, W - 6, y0 + 34, "add"))
        return img.resize((round(W * self.s), round(H * self.s)), Image.LANCZOS), hits

    # dropdown: the purifier's own modes + fan speed (whatever its driver declares)
    def popup_content(self, st):
        q = self.q
        modes, has_fan = st["modes"], st["fan_range"] is not None
        W, H = POP_W, pop_h(len(modes), has_fan)
        img = Image.new("RGBA", (q(W), q(H)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        hits, hov = [], st.get("hover")
        mode = st["mode"]

        def text(x, y, s, px, wght=400, fill=white(1), anchor="ls", icon=False):
            d.text((q(x), q(y)), s, font=self.font(px, wght, icon), fill=fill, anchor=anchor)

        for i, (key, label) in enumerate(modes):
            y0, tag, cur = 8 + i * 36, f"mode{i}", key == mode
            if hov == tag:
                d.rounded_rectangle([q(6), q(y0), q(W - 6), q(y0 + 34)], q(10), fill=white(0.13))
            text(18, y0 + 22, label, 13.5, 600 if cur else 400, white(1 if cur else 0.85))
            if cur:
                text(W - 20, y0 + 17, "", 12, icon=True, fill=white(0.95), anchor="mm")
            hits.append((6, y0, W - 6, y0 + 34, tag))
        if not has_fan:
            return img.resize((round(W * self.s), round(H * self.s)), Image.LANCZOS), hits

        lo, hi = st["fan_range"]
        level = max(lo, min(hi, st["level"] if st["level"] is not None else lo))
        in_fan_mode = mode == st["fan_mode"]
        yb = 8 + len(modes) * 36
        d.line([q(16), q(yb + 8), q(W - 16), q(yb + 8)], fill=white(0.12), width=max(1, q(0.5)))
        text(18, yb + 34, "Fan speed", 12, 600, white(0.9))
        text(W - 18, yb + 34, f"{level} / {hi}", 12, 400, white(0.55), anchor="rs")
        if not in_fan_mode:
            text(18, yb + 50, f"Adjusting switches to {st['fan_label']}", 10.5, 400, white(0.42))

        cy = yb + 74
        for cx, glyph, tag in [(26, "", "minus"), (W - 26, "", "plus")]:
            d.ellipse([q(cx - 13), q(cy - 13), q(cx + 13), q(cy + 13)],
                      fill=white(0.22 if hov == tag else 0.11), outline=white(0.10), width=max(1, q(0.5)))
            text(cx, cy, glyph, 10, icon=True, fill=white(0.9), anchor="mm")
            hits.append((cx - 14, cy - 14, cx + 14, cy + 14, tag))

        tx0, tx1, th = 50, W - 50, 6
        d.rounded_rectangle([q(tx0), q(cy - th / 2), q(tx1), q(cy + th / 2)], q(th / 2), fill=white(0.16))
        kx = tx0 + (tx1 - tx0) * (level - lo) / max(1, hi - lo)
        if level > lo:
            d.rounded_rectangle([q(tx0), q(cy - th / 2), q(kx), q(cy + th / 2)], q(th / 2),
                                fill=white(0.9 if in_fan_mode else 0.55))
        if hi - lo <= 6:  # few speeds: show a tick for each
            for s in range(lo, hi + 1):
                tx = tx0 + (tx1 - tx0) * (s - lo) / max(1, hi - lo)
                d.ellipse([q(tx - 2), q(cy - 2), q(tx + 2), q(cy + 2)], fill=white(0.5))
        d.ellipse([q(kx - 9.5), q(cy - 8.5), q(kx + 9.5), q(cy + 10.5)], fill=(0, 0, 0, 55))
        d.ellipse([q(kx - 9), q(cy - 9), q(kx + 9), q(cy + 9)], fill=(255, 255, 255, 255))
        hits.append((tx0 - 10, cy - 14, tx1 + 10, cy + 14, "track"))
        return img.resize((round(W * self.s), round(H * self.s)), Image.LANCZOS), hits

    def _bar(self, img, x, y, w, aqi):
        q, h = self.q, 5
        W, H = q(w), q(h)
        grad = Image.new("RGB", (W, 1))
        grad.putdata([scale_rgb(500 * i / max(1, W - 1)) for i in range(W)])
        grad = grad.resize((W, H), Image.NEAREST)
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, W - 1, H - 1], H // 2, fill=round(255 * (0.95 if aqi is not None else 0.35)))
        img.paste(grad, (q(x), q(y)), m)
        if aqi is not None:
            d = ImageDraw.Draw(img)
            cx = q(x) + H / 2 + (W - H) * min(aqi, 500) / 500
            cy = q(y) + H / 2
            r = q(7)
            d.ellipse([cx - r - q(1.5), cy - r - q(0.5), cx + r + q(1.5), cy + r + q(2.5)], fill=(0, 0, 0, 60))
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
            ri = q(3.2)
            d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=scale_rgb(aqi) + (255,))


# ── Layered window (per-pixel alpha) ───────────────────────────────────────
class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class _BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


_u32, _gdi = ctypes.windll.user32, ctypes.windll.gdi32
_u32.GetDC.restype = _gdi.CreateCompatibleDC.restype = _gdi.CreateDIBSection.restype = ctypes.c_void_p
_gdi.SelectObject.restype = ctypes.c_void_p
_u32.GetDC.argtypes = [ctypes.c_void_p]
_u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                  ctypes.c_void_p, wt.DWORD]
_gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi.DeleteDC.argtypes = [ctypes.c_void_p]
_u32.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD]


def make_layered(hwnd):
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TOOLWINDOW = -20, 0x80000, 0x80
    style = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    _u32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOOLWINDOW)


def update_layered(hwnd, img):
    """Show an RGBA image as the window's pixels, with real per-pixel transparency."""
    w, h = img.size
    r, g, b, a = img.split()
    pm = [ImageChops.multiply(c, a) for c in (r, g, b)]
    data = Image.merge("RGBA", (pm[2], pm[1], pm[0], a)).tobytes()  # premultiplied BGRA
    screen = _u32.GetDC(None)
    mem = _gdi.CreateCompatibleDC(screen)
    bih = _BIH(ctypes.sizeof(_BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bmp = _gdi.CreateDIBSection(mem, ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    old = _gdi.SelectObject(mem, bmp)
    size, src = wt.SIZE(w, h), wt.POINT(0, 0)
    blend = _BLEND(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
    _u32.UpdateLayeredWindow(hwnd, screen, None, ctypes.byref(size), mem, ctypes.byref(src), 0,
                             ctypes.byref(blend), 2)  # ULW_ALPHA
    _gdi.SelectObject(mem, old)
    _gdi.DeleteObject(bmp)
    _gdi.DeleteDC(mem)
    _u32.ReleaseDC(None, screen)


# ── Dropdown ───────────────────────────────────────────────────────────────
def room_popup_h(n):
    return 8 + n * 36 + 9 + 36 + 8


class Popup(tk.Toplevel):
    """Glass dropdown. kind="mode": modes + fan speed. kind="room": switch or add purifiers."""

    def __init__(self, app, x, y, kind="mode"):
        super().__init__(app)
        self.app, self.s, self.kind = app, app.s, kind
        if kind == "room":
            names = [p.name for p in app.purifiers]
            self.lw, self.lh = ROOM_W, room_popup_h(len(names))
            self.st = {"items": names, "sel": app.sel, "hover": None}
        else:
            v, p = app.st["vals"], app.cur
            self.lw, self.lh = POP_W, pop_h(len(p.modes), p.fan_range is not None)
            self.st = {"modes": p.modes, "mode": v.get("mode"), "level": v.get("fan"), "hover": None,
                       "fan_range": p.fan_range, "fan_mode": p.fan_mode,
                       "fan_label": p.mode_label(p.fan_mode)}
        W, H = round(self.lw * self.s), round(self.lh * self.s)
        self.overrideredirect(True)
        self.geometry(f"{W}x{H}+{x}+{y}")
        self.cv = tk.Canvas(self, width=W, height=H, highlightthickness=0, bd=0, bg="#000000")
        self.cv.pack()
        self.hits, self._dragging = [], False
        self.update_idletasks()
        make_layered(self._hwnd())
        self.attributes("-topmost", True)
        self._bd = app.r.backdrop(self.winfo_x(), self.winfo_y(), self.lw, self.lh, POP_R, tint=(16, 16, 20, 150))
        self.redraw()

        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", lambda e: self._hover(None))
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda e: self.close())
        self.focus_force()
        self._watch()

    def _hwnd(self):
        return ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()

    def redraw(self):
        render = self.app.r.room_content if self.kind == "room" else self.app.r.popup_content
        content, self.hits = render(self.st)
        update_layered(self._hwnd(), Image.alpha_composite(self._bd, content))

    def _hit(self, e):
        x, y = e.x / self.s, e.y / self.s
        return next((t for x0, y0, x1, y1, t in self.hits if x0 <= x <= x1 and y0 <= y <= y1), None)

    def _hover(self, tag):
        if tag != self.st["hover"]:
            self.st["hover"] = tag
            self.cv.config(cursor="hand2" if tag else "")
            self.redraw()

    def _clamp(self, level):
        lo, hi = self.st["fan_range"]
        return max(lo, min(hi, level))

    def _level_at(self, e):
        lo, hi = self.st["fan_range"]
        tx0, tx1 = 50, POP_W - 50
        return self._clamp(lo + round((e.x / self.s - tx0) / (tx1 - tx0) * (hi - lo)))

    def _motion(self, e):
        if self._dragging:
            lvl = self._level_at(e)
            if lvl != self.st["level"]:
                self.st["level"] = lvl
                self.redraw()
        else:
            self._hover(self._hit(e))

    def _press(self, e):
        tag = self._hit(e)
        if self.kind == "room":
            if tag == "add":
                self.close()
                self.app.add_purifier()
            elif tag and tag.startswith("room"):
                self.close()
                self.app.select_purifier(int(tag[4:]))
            return
        if tag == "track":
            self._dragging = True
            self.st["level"] = self._level_at(e)
            self.redraw()
        elif tag in ("minus", "plus"):
            cur = self.st["level"] if self.st["level"] is not None else self.st["fan_range"][0]
            self.st["level"] = self._clamp(cur + (1 if tag == "plus" else -1))
            self._commit_level()
        elif tag and tag.startswith("mode"):
            self.app.set_mode(self.st["modes"][int(tag[4:])][0])
            self.close()

    def _release(self, e):
        if self._dragging:
            self._dragging = False
            self._commit_level()

    def _commit_level(self):
        self.st["mode"] = self.st["fan_mode"]
        self.redraw()
        self.app.set_fan_level(self.st["level"])

    def _watch(self):
        """Close when the user clicks anywhere outside the dropdown."""
        u = ctypes.windll.user32
        if u.GetAsyncKeyState(0x01) & 0x8000 or u.GetAsyncKeyState(0x02) & 0x8000:
            pt = wt.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            x0, y0 = self.winfo_rootx(), self.winfo_rooty()
            if not (x0 <= pt.x < x0 + self.winfo_width() and y0 <= pt.y < y0 + self.winfo_height()):
                self.close()
                return
        self._job = self.after(60, self._watch)

    def close(self):
        if self.app.popup is self:
            self.app.popup = None
            self.app.popup_closed_at = time.time()
        self.destroy()


# ── Window ─────────────────────────────────────────────────────────────────
class Widget(tk.Tk):
    def __init__(self):
        super().__init__()
        self.s = self.winfo_fpixels("1i") / 96
        self.r = Renderer(self.s)
        W, H = self.r.size
        self.overrideredirect(True)
        self.geometry(f"{W}x{H}{self._load_pos()}")
        self.configure(bg="#000000")
        self.cv = tk.Canvas(self, width=W, height=H, highlightthickness=0, bd=0, bg="#000000")
        self.cv.pack()
        self.prefs = self._load_prefs()
        self.st = {"inside": None, "outside": None, "vals": {}, "hover": None, "updated": "",
                   "scale": self.prefs.get("scale", "us"), "metric": self.prefs.get("metric", "aqi")}
        self._cfg_mtime = 0
        self.purifiers, self.sel = [], 0
        self._load_purifiers()
        self.hits, self.jobs, self._drag = [], {}, None
        self.popup, self.popup_closed_at = None, 0.0

        self.scale_var = tk.StringVar(value=self.st["scale"])
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Refresh", command=self.refresh_all)
        self.menu.add_command(label=f"Open {REFERENCE_NAME}", command=lambda: webbrowser.open(REFERENCE_URL))
        self.menu.add_separator()
        for key, label in SCALES.items():
            self.menu.add_radiobutton(label=label, value=key, variable=self.scale_var, command=self._set_scale)
        self.metric_var = tk.StringVar(value=self.st["metric"])
        self.menu.add_separator()
        self.menu.add_radiobutton(label="Big number: AQI", value="aqi", variable=self.metric_var,
                                  command=self._set_metric)
        self.menu.add_radiobutton(label="Big number: PM2.5 (like the purifier's app)", value="pm25",
                                  variable=self.metric_var, command=self._set_metric)
        self.menu.add_separator()
        self.menu.add_command(label="Use real station data outside…", command=self._ask_waqi_token)
        self.menu.add_separator()
        self.menu.add_command(label="Close widget", command=self.close)

        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Leave>", lambda e: self._set_hover(None))
        self.cv.bind("<ButtonPress-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self.cv.bind("<Double-Button-1>", self._on_double)
        self.cv.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.bind("<FocusIn>", lambda e: self.after(50, self._send_to_bottom))

        self.update_idletasks()
        make_layered(self._hwnd())
        self._backdrop = self.r.backdrop(self.winfo_x(), self.winfo_y())
        self.redraw()
        self._keep_on_desktop()
        self.refresh_all()

    # rendering
    def redraw(self, content=True):
        if content:
            self._content, self.hits = self.r.content(self.st)
        update_layered(self._hwnd(), Image.alpha_composite(self._backdrop, self._content))

    def _rebackdrop(self):
        self._backdrop = self.r.backdrop(self.winfo_x(), self.winfo_y())
        self.redraw(content=False)

    # input
    def _hit(self, e):
        x, y = e.x / self.s, e.y / self.s
        for x0, y0, x1, y1, tag in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return tag
        return None

    def _set_hover(self, tag):
        tag = tag if tag in BUTTONS else None
        if tag != self.st["hover"]:
            self.st["hover"] = tag
            self.cv.config(cursor="hand2" if tag else "")
            self.redraw()

    def _on_motion(self, e):
        if not self._drag:
            self._set_hover(self._hit(e))

    def _on_press(self, e):
        tag = self._hit(e)
        if tag in BUTTONS:
            self._drag = None
            self._pressed = tag
        else:
            self._pressed = None
            self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _on_drag(self, e):
        if self._drag:
            self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")
            self._rebackdrop()

    def _on_release(self, e):
        if self._drag:
            self._drag = None
            self._save_pos()
            self._rebackdrop()
        elif self._pressed and self._pressed == self._hit(e):
            {"power": self.toggle_power, "mode": lambda: self.toggle_popup("mode"),
             "room": lambda: self.toggle_popup("room"),
             "refresh": self.refresh_all, "close": self.close}[self._pressed]()

    def toggle_popup(self, kind):
        if self.popup:
            same = self.popup.kind == kind
            self.popup.close()
            if same:
                return
        elif time.time() - self.popup_closed_at < 0.4:
            return  # this click is the one that just closed it
        if kind == "mode" and not self.st["vals"]:
            return
        if kind == "room" and not self.purifiers:
            return self.add_purifier()
        if kind == "mode":
            h = pop_h(len(self.cur.modes), self.cur.fan_range is not None)
        else:
            h = room_popup_h(len(self.purifiers))
        x0, y0, x1, y1 = next(h_[:4] for h_ in self.hits if h_[4] == kind)
        px = self.winfo_x() + round(x0 * self.s)
        py = self.winfo_y() + round((y1 + 8) * self.s)
        mon = monitor_rect(px, py)
        if py + round(h * self.s) > mon[1] + mon[3]:
            py = self.winfo_y() + round((y0 - 8 - h) * self.s)
        self.popup = Popup(self, px, py, kind)

    # purifiers
    @property
    def cur(self):
        return self.purifiers[self.sel] if self.purifiers else None

    def _load_purifiers(self):
        """(Re)read purifier.json; called at start and whenever the file changes."""
        try:
            mtime = pur.CFG_PATH.stat().st_mtime
        except OSError:
            mtime = 0
        if mtime == self._cfg_mtime:
            return False
        self._cfg_mtime = mtime
        self.purifiers = pur.purifiers(pur.load_cfg())
        names = [p.name for p in self.purifiers]
        want = self.prefs.get("purifier")
        self.sel = names.index(want) if want in names else 0
        self.st["room"] = self.cur.name if self.cur else "Add purifier"
        return True

    def select_purifier(self, i):
        if i == self.sel or i >= len(self.purifiers):
            return
        self.sel = i
        self.prefs["purifier"] = self.cur.name
        self._save_pos()
        self.st.update(room=self.cur.name, inside=None, vals={})
        self.redraw()
        self.refresh_inside()

    def add_purifier(self):
        """Open the add-purifier wizard in its own console window."""
        import subprocess
        exe = pathlib.Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":
            exe = exe.with_name("python.exe")
        here = pathlib.Path(__file__).resolve().parent
        subprocess.Popen([str(exe), str(here / "purifier.py"), "add"], cwd=str(here),
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        self._watch_cfg()

    def _watch_cfg(self, tries=600):
        """After launching the wizard, pick up the new purifier as soon as it is saved."""
        if self._load_purifiers():
            if len(self.purifiers) > 0:
                self.select_purifier(len(self.purifiers) - 1)
                self.st["room"] = self.cur.name
                self.redraw()
            return
        if tries:
            self.after(1000, self._watch_cfg, tries - 1)

    def _set_scale(self):
        self.st["scale"] = self.prefs["scale"] = self.scale_var.get()
        self._save_pos()
        self.redraw()

    def _set_metric(self):
        self.st["metric"] = self.prefs["metric"] = self.metric_var.get()
        self._save_pos()
        self.redraw()

    def _ask_waqi_token(self):
        """Outside defaults to a model estimate; a free WAQI token switches it to the nearest station."""
        from tkinter import simpledialog
        cfg = pur.load_cfg()
        token = simpledialog.askstring(
            "Real station data",
            "Paste your free WAQI token (get one at aqicn.org/data-platform/token).\n"
            "Outside will then show the nearest official monitoring station.\n"
            "Leave empty to go back to the model estimate.",
            initialvalue=cfg.get("waqi_token", ""), parent=self)
        if token is None:
            return
        cfg["waqi_token"] = pur.CFG["waqi_token"] = token.strip()
        pur.save_cfg(cfg)
        self._cfg_mtime = 0
        self.refresh_outside()

    def _on_double(self, e):
        if self._hit(e) == "outside":
            webbrowser.open(REFERENCE_URL)

    # data
    def _bg(self, fn, done):
        def run():
            try:
                res, err = fn(), None
            except BaseException as e:  # device asleep / offline / no network
                res, err = None, e
            self.after(0, done, res, err)
        threading.Thread(target=run, daemon=True).start()

    def _schedule(self, key, ms, fn):
        if self.jobs.get(key):
            self.after_cancel(self.jobs[key])
        self.jobs[key] = self.after(ms, fn)

    def _stamp(self):
        self.st["updated"] = "Updated " + time.strftime("%I:%M %p").lstrip("0")

    def refresh_all(self):
        self.refresh_inside()
        self.refresh_outside()

    def refresh_inside(self):
        self._load_purifiers()
        p = self.cur
        if not p:
            self.st["inside"] = "offline"
            self.redraw()
            return self._schedule("in", INSIDE_MS, self.refresh_inside)
        self._bg(p.get_all, lambda v, err, p=p: self._got_for(p, v, err))

    def _got_for(self, p, v, err):
        if self.cur and (p.name, p.ip) == (self.cur.name, self.cur.ip):
            self._got_inside(v, err)
        else:  # reading belongs to a purifier we've since switched away from
            self._schedule("in", 500, self.refresh_inside)

    def _got_inside(self, v, err):
        if err:
            self.st["inside"] = "offline"
        else:
            self.st["vals"] = v
            p = self.cur
            if not v["power"]:
                cap = "Purifier is off"
            elif p and v.get("mode") == p.fan_mode and v.get("fan") is not None:
                cap = f"Purifier · Fan {v['fan']}"
            else:
                cap = "Purifier · " + v.get("mode_label", "")
            self.st["inside"] = (v["pm25"], cap) if v.get("pm25") is not None else "offline"
            if self.popup and self.popup.kind == "mode" and not self.popup._dragging:
                self.popup.st["mode"], self.popup.st["level"] = v.get("mode"), v.get("fan")
                self.popup.redraw()
            self._stamp()
        self._rebackdrop_if_wallpaper_changed()
        self.redraw()
        self._schedule("in", INSIDE_MS, self.refresh_inside)

    def refresh_outside(self):
        self._bg(fetch_outdoor, self._got_outside)

    def _got_outside(self, res, err):
        self.st["outside"] = "offline" if err else res
        self.redraw()
        self._schedule("out", OUTDOOR_MS, self.refresh_outside)

    def _rebackdrop_if_wallpaper_changed(self):
        key = self.r._wp_key
        self._backdrop = self.r.backdrop(self.winfo_x(), self.winfo_y())
        return key != self.r._wp_key

    def toggle_power(self):
        v = self.st["vals"]
        if v:
            new = not v["power"]
            p = self.cur
            self._bg(lambda: p.set_power(new), lambda *_: self.refresh_inside())

    def set_mode(self, key):
        v, p = self.st["vals"], self.cur
        if v and p:
            v["mode"], v["mode_label"] = key, p.mode_label(key)
            self.redraw()
            self._bg(lambda: p.set_mode(key), lambda *_: self.refresh_inside())

    def set_fan_level(self, level):
        """The driver also switches to the mode the fan speed belongs to (Favorite, Manual…)."""
        v, p = self.st["vals"], self.cur
        if not (v and p and p.fan_range):
            return
        v["mode"], v["mode_label"], v["fan"] = p.fan_mode, p.mode_label(p.fan_mode), level
        self.redraw()
        self._bg(lambda: p.set_fan(level), lambda *_: self.refresh_inside())

    # desktop placement
    def _hwnd(self):
        return ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()

    def _send_to_bottom(self):
        try:  # bottom of the window stack: on the desktop, behind every app
            ctypes.windll.user32.SetWindowPos(self._hwnd(), 1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
        except Exception:
            pass

    def _keep_on_desktop(self):
        self._send_to_bottom()
        self.after(2000, self._keep_on_desktop)

    @staticmethod
    def _load_prefs():
        try:
            return json.loads(POS_FILE.read_text())
        except Exception:
            return {}

    def _load_pos(self):
        p = self._load_prefs()
        if "x" in p:
            return f"+{p['x']}+{p['y']}"
        return f"+{self.winfo_screenwidth() - round((WIN_W + 16) * self.s)}+{round(32 * self.s)}"

    def _save_pos(self):
        try:
            self.prefs.update(x=self.winfo_x(), y=self.winfo_y())
            POS_FILE.write_text(json.dumps(self.prefs))
        except Exception:
            pass

    def close(self):
        self._save_pos()
        self.destroy()


if __name__ == "__main__":
    Widget().mainloop()
