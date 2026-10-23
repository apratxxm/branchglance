"""Render Branchglance's fork mark to a crisp, multi-size Windows ICO using Pillow."""
from __future__ import annotations

import argparse
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
ROOT = Path(__file__).resolve().parent


def _geometry(svg: Path):
    tree = ET.parse(svg)
    ns = "{http://www.w3.org/2000/svg}"
    path = tree.find(f"{ns}path")
    circles = tree.findall(f"{ns}circle")
    if path is None or len(circles) != 3:
        raise ValueError("fork SVG must contain one path and three endpoint circles")
    numbers = [float(value) for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+)", path.attrib["d"])]
    if len(numbers) != 21:
        raise ValueError("fork SVG path does not match the supported move/line/arc geometry")
    root = (numbers[0], numbers[1])
    lower_stem = (root[0], root[1] + numbers[2])
    lower_elbow = (lower_stem[0] + numbers[8], lower_stem[1] + numbers[9])
    lower_end = (lower_elbow[0] + numbers[10], lower_elbow[1])
    upper_start = (numbers[11], numbers[12])
    upper_turn = (upper_start[0] + numbers[13], upper_start[1])
    upper_end = (upper_turn[0] + numbers[19], upper_turn[1] + numbers[20])
    geometry = (root, lower_stem, lower_elbow, lower_end, upper_start, upper_turn, upper_end,
                numbers[3], numbers[14])
    colors = [circle.attrib["fill"] for circle in circles]
    circles = [(float(circle.attrib["cx"]), float(circle.attrib["cy"]), float(circle.attrib["r"]))
               for circle in circles]
    return colors, geometry, circles


def _render(size: int, colors: list[str], geometry, circles) -> Image.Image:
    scale = 8
    n = size * scale
    image = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = max(0.5, size * 0.035) * scale
    radius = size * 0.23 * scale
    draw.rounded_rectangle((pad, pad, n - pad, n - pad), radius=radius, fill="#090B12")

    # Soft, restrained blue-violet light behind the fork.
    glow = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    center = n / 2
    gd.ellipse((center - size * .30 * scale, center - size * .30 * scale,
                center + size * .30 * scale, center + size * .30 * scale), fill=(104, 112, 255, 72))
    glow = glow.filter(ImageFilter.GaussianBlur(size * .15 * scale))
    tile = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    tile_draw = ImageDraw.Draw(tile)
    tile_draw.rounded_rectangle((pad, pad, n - pad, n - pad), radius=radius, fill="#090B12")
    tile.alpha_composite(glow)

    # The SVG's 16x16 path, sampled at high resolution. Rounded bends and endpoints
    # are preserved while the stroke scales enough to stay visible at 16 px.
    factor = n / 16
    stroke = max(1.05, min(1.5, size / 12)) * scale
    def p(x, y):
        return (x * factor, y * factor)
    root, lower_stem, lower_elbow, lower_end, upper_start, upper_turn, upper_end, lower_radius, upper_radius = geometry
    root = p(*root)
    fork_x = p(*lower_stem)
    elbow = p(*lower_elbow)
    lower_end = p(*lower_end)
    upper_start = p(*upper_start)
    upper_turn = p(*upper_turn)
    upper_end = p(*upper_end)
    draw = ImageDraw.Draw(tile)
    draw.line([root, fork_x], fill=colors[0], width=int(stroke))
    # Quarter-circle curve from the vertical stem to the lower horizontal branch.
    draw.line([p(lower_stem[0] + lower_radius * (1 - math.cos(t * math.pi / 2)),
                 lower_stem[1] + lower_radius * math.sin(t * math.pi / 2))
               for t in (i / 24 for i in range(25))], fill=colors[0], width=int(stroke))
    draw.line([elbow, lower_end], fill=colors[0], width=int(stroke))
    draw.line([upper_start, upper_turn], fill=colors[0], width=int(stroke))
    draw.line([p(upper_turn[0] + upper_radius * math.sin(t * math.pi / 2),
                 upper_turn[1] - upper_radius * (1 - math.cos(t * math.pi / 2)))
               for t in (i / 24 for i in range(25))], fill=colors[0], width=int(stroke))
    for (cx, cy, radius), color in zip(circles, colors):
        cx, cy, r = cx * factor, cy * factor, radius * factor
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def generate(svg: Path = ROOT / "mark-fork.svg", output: Path = ROOT / "branchglance.ico") -> Path:
    colors, geometry, circles = _geometry(svg)
    images = [_render(size, colors, geometry, circles) for size in SIZES]
    output.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(output, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "branchglance.ico")
    args = parser.parse_args()
    print(generate(output=args.output))
