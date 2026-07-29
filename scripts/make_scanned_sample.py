"""Render a text sample as a degraded scan, so the image path has something real to read.

The bundled samples are clean ASCII, which never exercises the visual path and
never gives the model a reason to lower its confidence. This renders one of
them the way it would actually arrive at a hospital: printed, photocopied,
faxed, and saved as a mediocre JPEG.

The degradation is deliberately reproducible (fixed seed) so the sample can be
regenerated and the extraction compared run to run.

    python scripts/make_scanned_sample.py
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "samples" / "ed_triage_note_deteriorating.txt"
DEST = ROOT / "data" / "samples" / "ed_triage_note_scanned.jpg"

SEED = 20260728

# A4 at ~200 dpi. Big enough that degraded text stays legible.
PAGE = (1654, 2339)
MARGIN = 110
FONT_SIZE = 25
LINE_HEIGHT = 33

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\cour.ttf",
    r"C:\Windows\Fonts\consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _render_page(text: str, rng: random.Random) -> Image.Image:
    """Lay the note out on off-white paper with per-character print jitter."""
    page = Image.new("L", PAGE, color=247)
    draw = ImageDraw.Draw(page)
    font = _font(FONT_SIZE)

    y = MARGIN
    for line in text.splitlines():
        if y > PAGE[1] - MARGIN:
            break
        x = MARGIN
        for ch in line:
            if ch != " ":
                # Toner is not applied evenly, and the platen is never flat.
                jitter_x = rng.uniform(-0.4, 0.4)
                jitter_y = rng.uniform(-0.7, 0.7)
                ink = rng.randint(28, 78)
                draw.text((x + jitter_x, y + jitter_y), ch, font=font, fill=ink)
            x += font.getlength("M")
        y += LINE_HEIGHT
    return page


def _uneven_lighting(page: Image.Image, rng: random.Random) -> Image.Image:
    """A diagonal brightness gradient — the shadow a lid or a phone casts."""
    w, h = page.size
    gradient = Image.new("L", (w, h))
    gd = ImageDraw.Draw(gradient)
    for i in range(h):
        gd.line([(0, i), (w, i)], fill=int(238 + 17 * (i / h)))
    gradient = gradient.rotate(rng.uniform(-12, 12), fillcolor=246)
    return Image.blend(page, Image.composite(page, gradient, page.point(lambda p: 255 if p > 150 else 0)), 0.45)


def _speckle(page: Image.Image, rng: random.Random) -> Image.Image:
    """Dust, toner spatter, and the odd fax dropout."""
    draw = ImageDraw.Draw(page)
    w, h = page.size
    for _ in range(1400):  # dust
        x, y = rng.randrange(w), rng.randrange(h)
        draw.point((x, y), fill=rng.randint(90, 190))
    for _ in range(45):  # toner blobs
        x, y = rng.randrange(w), rng.randrange(h)
        r = rng.randint(1, 3)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randint(70, 150))
    for _ in range(7):  # horizontal fax scan lines
        y = rng.randrange(h)
        draw.line([(0, y), (w, y)], fill=rng.randint(200, 225))
    return page


def main() -> None:
    rng = random.Random(SEED)
    text = SOURCE.read_text(encoding="utf-8")

    page = _render_page(text, rng)
    page = _uneven_lighting(page, rng)
    page = _speckle(page, rng)

    # Paper is never square to the glass.
    page = page.rotate(rng.uniform(-0.9, 0.9), resample=Image.BICUBIC, fillcolor=243, expand=False)

    # Optics: slight defocus, then a contrast crush from the copier.
    page = page.filter(ImageFilter.GaussianBlur(radius=0.65))
    page = ImageEnhance.Contrast(page).enhance(1.22)
    page = ImageEnhance.Brightness(page).enhance(0.97)

    # Sensor noise.
    noise = Image.effect_noise(page.size, 11).point(lambda p: (p - 128) // 3 + 128)
    page = Image.blend(page, noise, 0.10)

    # Downscale and back up: the resolution loss of a real fax leg.
    small = page.resize((int(PAGE[0] * 0.62), int(PAGE[1] * 0.62)), Image.BILINEAR)
    page = small.resize(PAGE, Image.BILINEAR)

    DEST.parent.mkdir(parents=True, exist_ok=True)
    page.convert("RGB").save(DEST, "JPEG", quality=58, optimize=True)

    kb = DEST.stat().st_size / 1024
    print(f"wrote {DEST.relative_to(ROOT)}  ({PAGE[0]}x{PAGE[1]}, {kb:.0f} KB, JPEG q58)")
    print(f"source: {SOURCE.relative_to(ROOT)}  seed: {SEED}")


if __name__ == "__main__":
    main()
