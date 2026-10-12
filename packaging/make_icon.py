"""Draw packaging/canopener.ico (the same logo as canopener/ui/icon.svg). Run once; the .ico is committed."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
k = S / 64
d.rounded_rectangle([2 * k, 2 * k, 62 * k, 62 * k], radius=14 * k, fill=(31, 111, 235, 255))
pts = [(10, 40), (18, 40), (18, 24), (26, 24), (26, 40), (34, 40), (34, 24), (42, 24), (42, 40), (54, 40)]
d.line([(x * k, y * k) for x, y in pts], fill="white", width=int(5 * k), joint="curve")
d.ellipse([44 * k, 16 * k, 56 * k, 28 * k], fill=(255, 211, 61, 255))
out = Path(__file__).with_name("canopener.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
