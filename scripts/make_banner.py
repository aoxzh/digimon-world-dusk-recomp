"""Compose the public repo banner from the two captured DS screenshots.

Run with the bundled Python that has Pillow:
  python make_banner.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "media"

# Nearest-neighbour keeps the DS pixel grid crisp at 2x.
SCALE = 2
PAD = 40
GAP = 80
BG_TOP = (10, 22, 34)
BG_BOTTOM = (24, 48, 66)
FRAME = (96, 148, 176)


def frame(img: Image.Image) -> Image.Image:
    """Upscale and add a thin rounded-looking border via an outline box."""
    up = img.resize((img.width * SCALE, img.height * SCALE), Image.NEAREST)
    canvas = Image.new("RGB", (up.width + 4, up.height + 4), FRAME)
    canvas.paste(up, (2, 2))
    return canvas


def main() -> None:
    title = Image.open(MEDIA / "title.png").convert("RGB")
    field = Image.open(MEDIA / "field.png").convert("RGB")

    left = frame(title)
    right = frame(field)

    width = PAD * 2 + left.width + GAP + right.width
    height = PAD * 2 + left.height

    banner = Image.new("RGB", (width, height), BG_TOP)
    draw = ImageDraw.Draw(banner)

    # Vertical gradient, cheap enough at this size.
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color)

    # Faint scanline texture so the background reads as a screen, not a flat box.
    for y in range(0, height, 4):
        draw.line([(0, y), (width, y)], fill=(12, 28, 42))

    banner.paste(left, (PAD, PAD))
    banner.paste(right, (PAD + left.width + GAP, PAD))

    MEDIA.mkdir(parents=True, exist_ok=True)
    banner.save(MEDIA / "banner.png", optimize=True)
    print(f"wrote {MEDIA / 'banner.png'} ({banner.width}x{banner.height})")


if __name__ == "__main__":
    main()
