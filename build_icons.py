"""Render the auto-launch toggle icon (on/off tint variants) to PNG."""
from pathlib import Path

from PIL import Image
from reportlab.graphics import renderPM
from svglib.svglib import svg2rlg

HERE = Path(__file__).parent
SVG_PATH = HERE.parent / "menu-icons-mod" / "build" / "svg2" / "zap.svg"
OUT_DIR = HERE / "source" / "icons"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SIZE = 22
VARIANTS = {
    "zap_on": (120, 240, 190),   # bright teal-green: auto-launch enabled
    "zap_off": (110, 128, 150),  # dim steel gray: auto-launch disabled
}

for name, color in VARIANTS.items():
    raw = SVG_PATH.read_text(encoding="utf-8")
    raw = raw.replace("currentColor", "#{:02x}{:02x}{:02x}".format(*color))
    tmp_path = OUT_DIR / f"_tmp_{name}.svg"
    tmp_path.write_text(raw, encoding="utf-8")

    drawing = svg2rlg(str(tmp_path))
    scale = SIZE / max(drawing.width, drawing.height)
    drawing.width *= scale
    drawing.height *= scale
    drawing.scale(scale, scale)

    raw_png = OUT_DIR / f"_raw_{name}.png"
    renderPM.drawToFile(drawing, str(raw_png), fmt="PNG", bg=0x000000)
    tmp_path.unlink()

    img = Image.open(raw_png).convert("RGB")
    alpha = img.convert("L")
    out = Image.new("RGBA", img.size, color + (0,))
    out.putalpha(alpha)
    out.save(OUT_DIR / f"{name}.png")
    raw_png.unlink()
    print("wrote", f"{name}.png")
