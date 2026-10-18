"""Tray icon: a printer glyph with a status badge."""

from __future__ import annotations

from PIL import Image, ImageDraw

BADGE = {
    "ok": (46, 160, 67),
    "warning": (230, 162, 0),
    "error": (214, 48, 49),
    "offline": (128, 128, 128),
}


def render(severity: str, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((18, 4, 46, 22), fill=(250, 250, 250), outline=(60, 60, 60), width=2)  # paper in
    d.rounded_rectangle((4, 18, 60, 44), radius=6, fill=(70, 74, 82))  # body
    d.rectangle((16, 36, 48, 60), fill=(250, 250, 250), outline=(60, 60, 60), width=2)  # paper out
    d.line((22, 46, 42, 46), fill=(150, 150, 150), width=2)
    d.line((22, 52, 36, 52), fill=(150, 150, 150), width=2)
    d.ellipse((36, 36, 63, 63), fill=BADGE.get(severity, BADGE["offline"]), outline=(255, 255, 255), width=3)
    return img if size == 64 else img.resize((size, size), Image.Resampling.LANCZOS)
