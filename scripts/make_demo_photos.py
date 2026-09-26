"""Illustrative demo 'photos' (drawn, then aged like an old print). Clearly demo material, used by the seed script
and the WhatsApp simulator. Run: python scripts/make_demo_photos.py"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

OUT = Path(__file__).resolve().parents[1] / "vertelschat" / "data" / "demo_photos"
W, H = 1600, 1200


def age(img: Image.Image, seed: int) -> Image.Image:
    rnd = random.Random(seed)
    img = img.filter(ImageFilter.GaussianBlur(1.6))
    gray = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
    sepia = ImageOps.colorize(gray, black="#2a2118", white="#f3e6cc", mid="#9c8566")
    noise = Image.effect_noise((W, H), 28).convert("L")
    sepia = Image.blend(sepia, ImageOps.colorize(noise, black="#3b2f22", white="#f5ead3"), 0.12)
    vignette = ImageOps.invert(Image.radial_gradient("L")).resize((W, H))  # bright centre, darker edges
    vignette = vignette.point(lambda v: int(150 + v * 105 / 255)).filter(ImageFilter.GaussianBlur(30))
    dark = Image.new("RGB", (W, H), "#1d1712")
    sepia = Image.composite(sepia, dark, vignette)
    framed = Image.new("RGB", (W + 90, H + 90), "#f7f1e3")
    framed.paste(sepia, (45, 45))
    return framed.rotate(rnd.uniform(-0.6, 0.6), expand=False, fillcolor="#f7f1e3")


def house() -> Image.Image:
    img = Image.new("RGB", (W, H), "#cfd8dc")
    d = ImageDraw.Draw(img)
    for y in range(0, 520):
        c = 210 - y // 8
        d.line([(0, y), (W, y)], fill=(c, c + 6, c + 12))
    for i, x in enumerate(range(-40, W, 420)):
        base = 250 + (i % 2) * 30
        d.polygon([(x, 520), (x + 210, base), (x + 420, 520)], fill=(92, 70, 60))
        d.rectangle([x, 520, x + 420, 1060], fill=(150 - i * 6, 92, 74))
        for bx in range(x, x + 420, 42):
            for by in range(520, 1060, 21):
                d.line([(bx, by), (bx + 42, by)], fill=(120, 72, 58), width=2)
        d.rectangle([x + 40, 600, x + 190, 800], fill=(236, 232, 220))
        d.rectangle([x + 52, 612, x + 178, 788], fill=(58, 66, 74))
        d.line([(x + 115, 612), (x + 115, 788)], fill=(236, 232, 220), width=8)
        d.rectangle([x + 250, 640, x + 360, 1060], fill=(46, 58, 50))
        d.rectangle([x + 262, 652, x + 348, 760], fill=(80, 92, 84))
        d.ellipse([x + 330, 850, x + 344, 864], fill=(210, 180, 90))
        d.rectangle([x + 60, 360, x + 170, 470], fill=(236, 232, 220))
        d.rectangle([x + 72, 372, x + 158, 458], fill=(58, 66, 74))
    d.rectangle([0, 1060, W, H], fill=(128, 128, 122))
    for x in range(0, W, 90):
        d.line([(x, 1060), (x - 40, H)], fill=(110, 110, 104), width=3)
    # a bicycle against the wall
    d.ellipse([560, 930, 700, 1070], outline=(30, 30, 30), width=7)
    d.ellipse([760, 930, 900, 1070], outline=(30, 30, 30), width=7)
    d.line([(630, 1000), (710, 940), (830, 1000), (740, 1000), (630, 1000)], fill=(30, 30, 30), width=7)
    d.line([(710, 940), (700, 905), (740, 905)], fill=(30, 30, 30), width=7)
    d.line([(830, 1000), (800, 920), (840, 910)], fill=(30, 30, 30), width=7)
    return age(img, 1)


def tent() -> Image.Image:
    img = Image.new("RGB", (W, H), "#dfe7ea")
    d = ImageDraw.Draw(img)
    for y in range(0, 560):
        c = 225 - y // 10
        d.line([(0, y), (W, y)], fill=(c, c + 4, c + 8))
    for i in range(14):
        x = i * 130 - 30
        d.ellipse([x, 360 + (i % 3) * 20, x + 220, 600], fill=(70 + (i % 4) * 8, 92, 70))
    d.rectangle([0, 560, W, 760], fill=(120, 140, 150))
    for y in range(570, 760, 14):
        d.line([(0, y), (W, y + 6)], fill=(150, 168, 176), width=2)
    d.rectangle([0, 760, W, H], fill=(104, 128, 84))
    for i in range(400):
        x, y = (i * 37) % W, 770 + (i * 53) % 420
        d.line([(x, y), (x + 4, y - 16)], fill=(84, 110, 66), width=3)
    d.polygon([(420, 1010), (720, 690), (1020, 1010)], fill=(214, 130, 60))
    d.polygon([(560, 1010), (720, 800), (880, 1010)], fill=(92, 54, 34))
    d.line([(720, 690), (720, 640)], fill=(60, 50, 40), width=8)
    d.line([(420, 1010), (330, 1060)], fill=(80, 80, 80), width=3)
    d.line([(1020, 1010), (1110, 1060)], fill=(80, 80, 80), width=3)
    d.rectangle([1180, 930, 1300, 1000], fill=(90, 90, 96))
    d.rectangle([1195, 900, 1285, 930], fill=(60, 60, 66))
    return age(img, 2)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    house().save(OUT / "huis.jpg", quality=86)
    tent().save(OUT / "tent.jpg", quality=86)
    print("ok", list(OUT.iterdir()))


if __name__ == "__main__":
    main()
