"""Draw the app icon (icon.ico): a dark glass tile with the AQI colour arc."""
import math
import pathlib

from PIL import Image, ImageDraw

STOPS = [(48, 209, 88), (255, 214, 10), (255, 159, 10), (255, 69, 58), (191, 90, 242)]


def colour(t):
    t = max(0.0, min(1.0, t)) * (len(STOPS) - 1)
    i = min(int(t), len(STOPS) - 2)
    f = t - i
    return tuple(round(a + (b - a) * f) for a, b in zip(STOPS[i], STOPS[i + 1]))


def draw(size=1024):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad, r = size * 0.06, size * 0.22
    d.rounded_rectangle([pad, pad, size - pad, size - pad], r, fill=(28, 28, 32, 255))
    # soft top sheen
    sheen = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).rounded_rectangle([pad, pad, size - pad, size * 0.55], r, fill=(255, 255, 255, 18))
    img = Image.alpha_composite(img, sheen)
    d = ImageDraw.Draw(img)
    # gauge arc, 220° sweep, drawn as many short colour segments
    cx, cy, rad, w = size / 2, size * 0.56, size * 0.30, size * 0.075
    start, sweep, steps = 160, 220, 220
    for k in range(steps):
        a0 = math.radians(start + sweep * k / steps)
        a1 = math.radians(start + sweep * (k + 1.6) / steps)
        pts = []
        for a in (a0, a1):
            pts.append((cx + (rad + w / 2) * math.cos(a), cy + (rad + w / 2) * math.sin(a)))
        for a in (a1, a0):
            pts.append((cx + (rad - w / 2) * math.cos(a), cy + (rad - w / 2) * math.sin(a)))
        d.polygon(pts, fill=colour(k / steps))
    for a, c in ((start, colour(0)), (start + sweep, colour(1))):  # round caps
        x, y = cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a))
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=c)
    # marker
    a = math.radians(start + sweep * 0.3)
    mx, my, mr = cx + rad * math.cos(a), cy + rad * math.sin(a), w * 0.85
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(255, 255, 255, 255))
    # leaf-like droplet in the centre
    dr = size * 0.09
    d.ellipse([cx - dr, cy - dr * 0.6, cx + dr, cy + dr * 1.4], fill=(255, 255, 255, 235))
    d.polygon([(cx, cy - dr * 1.9), (cx - dr * 0.86, cy + dr * 0.2), (cx + dr * 0.86, cy + dr * 0.2)],
              fill=(255, 255, 255, 235))
    return img


if __name__ == "__main__":
    out = pathlib.Path(__file__).with_name("icon.ico")
    draw().resize((256, 256), Image.LANCZOS).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    draw().resize((256, 256), Image.LANCZOS).save(out.with_suffix(".png"))
    print("wrote", out)
