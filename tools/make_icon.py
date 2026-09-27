"""Generates the app icon: assets/icon.png and assets/icon.ico (purple tile, white drop)."""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SIZE = 256


def draw_icon(size: int = SIZE) -> Image.Image:
    s = size * 4  # draw large and scale down for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=(145, 70, 255, 255))
    # drop: a circle at the bottom, a pointed triangle on top
    cx, cy, r = s // 2, int(s * 0.60), int(s * 0.24)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")
    d.polygon([(cx, int(s * 0.14)), (cx - int(r * 0.93), cy - int(r * 0.37)),
               (cx + int(r * 0.93), cy - int(r * 0.37))], fill="white")
    # small highlight inside the drop
    hr = r // 4
    hx, hy = cx - r // 3, cy + r // 6
    d.ellipse((hx - hr, hy - hr, hx + hr, hy + hr), fill=(145, 70, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    img = draw_icon()
    img.save(out / "icon.png")
    img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Icon written to", out)


if __name__ == "__main__":
    main()
