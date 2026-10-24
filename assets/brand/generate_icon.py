"""Generate Branchglance Live Stack vectors, app icons, and review sheets.

Build-time dependency: Pillow (already present in the release build environment).
The wordmark is raster-traced from the bundled Geist SemiBold WOFF2, so every
checked-in SVG is self-contained and the application needs no added runtime font.
"""
from __future__ import annotations

import argparse
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
STATIC = REPO / "token_tracker" / "static"
DOCS = REPO / "docs" / "brand"
FONT = STATIC / "fonts" / "geist" / "Geist-latin.woff2"
TEXT = "Branchglance"
INK = "#0B0B0D"
LAVENDER = "#B9ADF5"
PAPER = "#F2F0EA"
MUTED = "#AAA6BC"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPERSAMPLE = 4
VECTOR_FONT_SIZE = 256
VECTOR_OVERSAMPLE = 4
WORDMARK_STRETCH_X = 0.98

# Canonical 512 x 512 layout. The visible mark spans 512 x 432 (1.185:1),
# with 40 px vertical optical margins. The Lavender lower edge and bar 3 are
# parallel; the bottom wedge follows that angle and ends in a soft point.
TOP_RECT = (0.0, 40.0, 399.0, 131.0, 32.0)
PURPLE_PATH = ("M36 161 H476 Q512 161 512 197 V271 "
               "Q512 303 480 297 L126 236 "
               "C112 234 104 244 104 257 V278 "
               "Q104 310 72 310 H36 Q0 310 0 274 V197 Q0 161 36 161 Z")
THIRD = ((125.0, 264.0), (512.0, 331.0), (512.0, 431.0), (125.0, 364.0))
WEDGE_PATH = ("M20 376 L374 437 "
              "C386 439 405 446 400 459 "
              "C397 468 388 472 374 472 H20 "
              "Q0 472 0 452 V396 Q0 376 20 376 Z")

# Compact marks are hand-simplified on a 16 px grid, then snapped to
# half-pixel boundaries at 16/20/24/32 px to preserve the band gaps.
SMALL_TOP = (2.0, 2.5, 12.0, 4.25, 1.0)
SMALL_PURPLE = ((3.0, 5.0), (12.4, 5.0), (14.0, 6.6), (14.0, 7.2),
                (12.6, 8.2), (5.0, 7.0), (4.45, 7.15), (4.0, 7.85),
                (4.0, 8.8), (3.0, 9.8), (2.8, 9.8), (2.0, 9.0), (2.0, 6.0))
SMALL_THIRD = ((5.0, 8.0), (14.0, 9.55), (14.0, 11.05), (5.0, 9.5))
SMALL_THIRD_16 = ((5.0, 8.0), (14.0, 8.0), (14.0, 9.5), (5.0, 9.5))
SMALL_WEDGE = ((2.0, 10.7), (11.9, 12.3), (13.0, 12.55),
               (12.6, 12.8), (2.9, 12.8), (2.0, 11.8))
SMALL_RADII = (0.28, 0.22, 0.20)
SMALL_SNAP = {16: 1.0, 20: 0.5, 24: 0.5, 32: 0.5}


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _rounded_corners(points: tuple[tuple[float, float], ...], radius: float):
    corners = []
    for i, p in enumerate(points):
        prev, nxt = points[i - 1], points[(i + 1) % len(points)]
        d0 = math.hypot(prev[0] - p[0], prev[1] - p[1])
        d1 = math.hypot(nxt[0] - p[0], nxt[1] - p[1])
        if d0 == 0 or d1 == 0 or radius <= 0:
            corners.append((p, p, p))
            continue
        t0 = min(0.45, radius / d0)
        t1 = min(0.45, radius / d1)
        start = (p[0] + (prev[0] - p[0]) * t0, p[1] + (prev[1] - p[1]) * t0)
        end = (p[0] + (nxt[0] - p[0]) * t1, p[1] + (nxt[1] - p[1]) * t1)
        corners.append((start, p, end))
    return corners


def _svg_rounded_polygon(points: tuple[tuple[float, float], ...], radius: float) -> str:
    corners = _rounded_corners(points, radius)
    out = [f"M{_fmt(corners[0][0][0])} {_fmt(corners[0][0][1])}"]
    for i, (start, control, end) in enumerate(corners):
        if i:
            out.append(f"L{_fmt(start[0])} {_fmt(start[1])}")
        out.append(f"Q{_fmt(control[0])} {_fmt(control[1])} {_fmt(end[0])} {_fmt(end[1])}")
    out.append("Z")
    return " ".join(out)


def _raster_rounded_polygon(points, radius: float, scale: float):
    corners = _rounded_corners(tuple(points), radius)
    result = [corners[0][0]]
    steps = 8
    for i, (start, control, end) in enumerate(corners):
        if i:
            result.append(start)
        for step in range(1, steps + 1):
            t = step / steps
            u = 1 - t
            result.append((u * u * start[0] + 2 * u * t * control[0] + t * t * end[0],
                           u * u * start[1] + 2 * u * t * control[1] + t * t * end[1]))
    return [(x * scale, y * scale) for x, y in result]


def _canonical_svg_shapes(fill_paper: str, fill_lavender: str) -> str:
    third = _svg_rounded_polygon(THIRD, 32.0)
    x0, y0, x1, y1, radius = TOP_RECT
    return "\n".join((
        f'<rect x="{_fmt(x0)}" y="{_fmt(y0)}" width="{_fmt(x1-x0)}" height="{_fmt(y1-y0)}" rx="{_fmt(radius)}" fill="{fill_paper}"/>',
        f'<path d="{PURPLE_PATH}" fill="{fill_lavender}"/>',
        f'<path d="{third}" fill="{fill_paper}"/>',
        f'<path d="{WEDGE_PATH}" fill="{fill_paper}"/>',
    ))


def _wordmark_outline(font_path: Path = FONT):
    if not font_path.is_file():
        raise FileNotFoundError(f"bundled Geist font not found: {font_path}")
    mask = _render_wordmark_mask(VECTOR_FONT_SIZE, VECTOR_OVERSAMPLE, font_path)
    loops = _trace_mask(mask)
    paths = []
    pad = 2.0
    for loop in loops:
        simplified = _simplify_closed(loop, 2.0)
        if len(simplified) < 3:
            continue
        points = [((x / VECTOR_OVERSAMPLE + pad) * WORDMARK_STRETCH_X,
                   y / VECTOR_OVERSAMPLE + pad) for x, y in simplified]
        path = "M" + " L".join(f"{_fmt(x)} {_fmt(y)}" for x, y in points) + " Z"
        paths.append(path)
    out_width = (mask.width / VECTOR_OVERSAMPLE + pad * 2) * WORDMARK_STRETCH_X
    out_height = mask.height / VECTOR_OVERSAMPLE + pad * 2
    return " ".join(paths), out_width, out_height


def _render_wordmark_mask(font_size: int, supersample: int, font_path: Path = FONT) -> Image.Image:
    """Render Geist SemiBold with -2% em tracking for outline and preview output."""
    font = ImageFont.truetype(str(font_path), font_size * supersample)
    try:
        font.set_variation_by_name("SemiBold")
    except (AttributeError, OSError):
        pass
    advances = [font.getlength(char) for char in TEXT]
    kerning = [font.getlength(TEXT[i:i + 2]) - advances[i] - advances[i + 1]
               for i in range(len(TEXT) - 1)]
    tracking = -font_size * supersample * 0.02
    total = sum(advances) + sum(kerning) + tracking * (len(TEXT) - 1)
    ascent, descent = font.getmetrics()
    margin = 12 * supersample
    mask = Image.new("L", (math.ceil(total) + margin * 2, ascent + descent + margin * 2), 0)
    draw = ImageDraw.Draw(mask)
    cursor = float(margin)
    baseline = margin + ascent
    for i, char in enumerate(TEXT):
        draw.text((cursor, baseline), char, font=font, anchor="ls", fill=255)
        cursor += advances[i]
        if i < len(kerning):
            cursor += kerning[i] + tracking
    mask = mask.point(lambda value: 255 if value >= 128 else 0)
    bounds = mask.getbbox()
    if bounds is None:
        raise RuntimeError("wordmark rasterization produced an empty mask")
    return mask.crop(bounds)


def _trace_mask(mask: Image.Image):
    """Trace the binary glyph mask as directed pixel-edge loops."""
    width, height = mask.size
    raw = mask.tobytes()
    edges = set()
    for y in range(height):
        row = y * width
        above = row - width
        below = row + width
        for x in range(width):
            idx = row + x
            if not raw[idx]:
                continue
            if y == 0 or not raw[above + x]:
                edges.add((x, y, x + 1, y))
            if x + 1 == width or not raw[idx + 1]:
                edges.add((x + 1, y, x + 1, y + 1))
            if y + 1 == height or not raw[below + x]:
                edges.add((x + 1, y + 1, x, y + 1))
            if x == 0 or not raw[idx - 1]:
                edges.add((x, y + 1, x, y))
    outgoing = defaultdict(list)
    for edge in edges:
        outgoing[(edge[0], edge[1])].append(edge)
    for value in outgoing.values():
        value.sort()
    unused = set(edges)
    loops = []
    while unused:
        first = min(unused)
        start = (first[0], first[1])
        edge = first
        points = [start]
        for _ in range(len(edges) + 1):
            unused.remove(edge)
            end = (edge[2], edge[3])
            if end == start:
                break
            points.append(end)
            direction = _direction(edge)
            choices = [candidate for candidate in outgoing[end] if candidate in unused]
            if not choices:
                raise RuntimeError("could not close a wordmark outline")
            priority = ((direction + 1) % 4, direction, (direction - 1) % 4, (direction + 2) % 4)
            edge = min(choices, key=lambda candidate: (priority.index(_direction(candidate)), candidate))
        else:
            raise RuntimeError("wordmark outline exceeded its pixel-edge limit")
        if len(points) >= 3:
            loops.append(points)
    return loops


def _direction(edge):
    dx, dy = edge[2] - edge[0], edge[3] - edge[1]
    return 0 if dx > 0 else 1 if dy > 0 else 2 if dx < 0 else 3


def _simplify_open(points, epsilon):
    if len(points) <= 2:
        return points
    x1, y1 = points[0]
    x2, y2 = points[-1]
    dx, dy = x2 - x1, y2 - y1
    denom = math.hypot(dx, dy) or 1.0
    best, index = -1.0, -1
    for i, (x, y) in enumerate(points[1:-1], 1):
        distance = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / denom
        if distance > best:
            best, index = distance, i
    if best <= epsilon:
        return [points[0], points[-1]]
    left = _simplify_open(points[:index + 1], epsilon)
    right = _simplify_open(points[index:], epsilon)
    return left[:-1] + right


def _simplify_closed(points, epsilon):
    if len(points) < 4:
        return points
    corners = []
    for i, point in enumerate(points):
        prev = points[i - 1]
        nxt = points[(i + 1) % len(points)]
        if ((point[0] - prev[0], point[1] - prev[1]) !=
                (nxt[0] - point[0], nxt[1] - point[1])):
            corners.append(point)
    if len(corners) < 4:
        return corners
    p0 = corners[0]
    split = max(range(1, len(corners)), key=lambda i: (corners[i][0] - p0[0]) ** 2 + (corners[i][1] - p0[1]) ** 2)
    a = _simplify_open(corners[:split + 1], epsilon)
    b = _simplify_open(corners[split:] + [p0], epsilon)
    return a[:-1] + b[:-1]


def _svg_document(title: str, desc: str, body: str, width: int, height: int,
                  viewbox: str | None = None) -> str:
    viewbox = viewbox or f"0 0 {width} {height}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="{viewbox}" role="img" aria-labelledby="title desc">\n'
            f'  <title id="title">{escape(title)}</title>\n  <desc id="desc">{escape(desc)}</desc>\n{body}\n</svg>\n')


def _write_vectors():
    full = _canonical_svg_shapes(PAPER, LAVENDER)
    mono_dark = _canonical_svg_shapes(PAPER, PAPER)
    mono_light = (f'<rect width="512" height="512" rx="96" fill="{PAPER}"/>\n'
                  f'<g transform="translate(40 41) scale(0.84)">{_canonical_svg_shapes(INK, INK)}</g>')
    (ROOT / "mark-full.svg").write_text(_svg_document("Live Stack full-colour mark", "Four stacked rounded bars: off-white, lavender, a slanted bar, and a tapered wedge.", full, 512, 512), encoding="utf-8")
    (ROOT / "mark-mono-dark.svg").write_text(_svg_document("Live Stack mono dark mark", "The four-bar Live Stack mark in off-white for dark surfaces.", mono_dark, 512, 512), encoding="utf-8")
    (ROOT / "mark-mono-light.svg").write_text(_svg_document("Live Stack mono light mark", "The four-bar Live Stack mark knocked out in near-black on an off-white rounded tile.", mono_light, 512, 512), encoding="utf-8")
    wordmark_d, wordmark_w, wordmark_h = _wordmark_outline()
    wordmark_width = math.ceil(wordmark_w)
    wordmark_height = math.ceil(wordmark_h)
    wordmark_body = f'<path d="{wordmark_d}" fill="{PAPER}" fill-rule="evenodd"/>'
    (ROOT / "wordmark.svg").write_text(_svg_document("Branchglance wordmark", "Outlined Branchglance wordmark based on bundled Geist SemiBold.", wordmark_body, wordmark_width, wordmark_height), encoding="utf-8")
    mark_scale = 0.94
    text_scale = 0.50
    lockup_width = math.ceil(48 + 512 * mark_scale + 48 + wordmark_width * text_scale + 40)
    lockup_height = 512
    text_x = 48 + 512 * mark_scale + 48
    text_y = (lockup_height - wordmark_height * text_scale) / 2
    lockup_body = (f'<rect width="100%" height="100%" fill="{INK}"/>\n'
                   f'<g transform="translate(48 0) scale({mark_scale})">{full}</g>\n'
                   f'<g transform="translate({_fmt(text_x)} {_fmt(text_y)}) scale({text_scale})">{wordmark_body}</g>')
    (ROOT / "lockup.svg").write_text(_svg_document("Branchglance Live Stack lockup", "Full-colour Live Stack mark beside the outlined Branchglance wordmark on dark charcoal.", lockup_body, lockup_width, lockup_height), encoding="utf-8")
    # Phone/PWA SVGs are self-contained rounded tiles; static pages already
    # point at phone-icon.svg and browsers can discover favicon.svg/ICO.
    tile_body = (f'<rect width="512" height="512" rx="110" fill="{INK}"/>\n'
                 f'<g transform="translate(40 41) scale(0.84)">{full}</g>')
    icon_svg = _svg_document("Branchglance Live Stack icon", "Live Stack app icon on a dark rounded tile.", tile_body, 512, 512)
    (STATIC / "phone-icon.svg").write_text(icon_svg, encoding="utf-8")
    (STATIC / "favicon.svg").write_text(icon_svg, encoding="utf-8")
    return wordmark_w, wordmark_h


def _snap(value, step):
    return round(value / step) * step


def _small_shapes(size):
    scale = size / 16.0
    step = SMALL_SNAP[size]
    def point_list(points):
        return tuple((_snap(x * scale, step), _snap(y * scale, step)) for x, y in points)
    x0, y0, x1, y1, radius = SMALL_TOP
    third = SMALL_THIRD_16 if size == 16 else SMALL_THIRD
    return (("rect", (_snap(x0 * scale, step), _snap(y0 * scale, step),
                       _snap(x1 * scale, step), _snap(y1 * scale, step),
                       _snap(radius * scale, step))),
            ("poly", point_list(SMALL_PURPLE), SMALL_RADII[0] * scale),
            ("poly", point_list(third), SMALL_RADII[1] * scale),
            ("poly", point_list(SMALL_WEDGE), SMALL_RADII[2] * scale))


def _render_mark(size: int, *, mode="full", tile=True) -> Image.Image:
    if size in SMALL_SNAP:
        svg = _small_svg(size, mode=mode, tile=tile)
    elif mode == "mono-light":
        svg = (ROOT / "mark-mono-light.svg").read_text(encoding="utf-8")
    elif mode == "mono-dark":
        svg = (ROOT / "mark-mono-dark.svg").read_text(encoding="utf-8")
    elif tile:
        svg = (STATIC / "phone-icon.svg").read_text(encoding="utf-8")
    else:
        svg = (ROOT / "mark-full.svg").read_text(encoding="utf-8")
    return _rasterize_svg(svg, size)


def _small_svg(size: int, *, mode: str, tile: bool) -> str:
    """Create the pixel-aligned small-size SVG source used by the rasterizer."""
    if mode == "mono-light":
        tile_color, paper_color, lavender_color = PAPER, INK, INK
    elif mode == "mono-dark":
        tile_color, paper_color, lavender_color = None, PAPER, PAPER
    elif tile:
        tile_color, paper_color, lavender_color = INK, PAPER, LAVENDER
    else:
        tile_color, paper_color, lavender_color = None, PAPER, LAVENDER
    shapes = _small_shapes(size)
    body = []
    if tile_color:
        inset = 0.55 if size <= 20 else 0.25
        radius = max(2.4, size * 0.20)
        body.append(f'<rect x="{_fmt(inset)}" y="{_fmt(inset)}" width="{_fmt(size-2*inset)}" height="{_fmt(size-2*inset)}" rx="{_fmt(radius)}" fill="{tile_color}"/>')
    for index, shape in enumerate(shapes):
        fill = lavender_color if index == 1 else paper_color
        if shape[0] == "rect":
            _, (x0, y0, x1, y1, radius) = shape
            body.append(f'<rect x="{_fmt(x0)}" y="{_fmt(y0)}" width="{_fmt(x1-x0)}" height="{_fmt(y1-y0)}" rx="{_fmt(radius)}" fill="{fill}"/>')
        else:
            _, points, radius = shape
            body.append(f'<path d="{_svg_rounded_polygon(points, radius)}" fill="{fill}"/>')
    return _svg_document("Live Stack small icon", "Pixel-aligned four-bar Live Stack mark.", "\n".join(body), size, size)


def _rasterize_svg(svg: str, size: int) -> Image.Image:
    """Rasterize the generator's rect and M/L/H/V/Q/C/Z SVG subset with Pillow."""
    ss = 8 if size <= 32 else 4
    root = ET.fromstring(svg)
    scale = size / float(root.attrib.get("viewBox", f"0 0 {size} {size}").split()[2])
    n = size * ss
    image = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ns = "{http://www.w3.org/2000/svg}"
    def visit(element, transform_scale=1.0, translate_x=0.0, translate_y=0.0):
        tag = element.tag.removeprefix(ns)
        if tag in {"title", "desc"}:
            return
        if tag == "g":
            local_scale, local_x, local_y = 1.0, 0.0, 0.0
            transform = element.attrib.get("transform", "")
            for kind, raw in re.findall(r"(translate|scale)\(([^)]*)\)", transform):
                values = [float(value) for value in re.split(r"[ ,]+", raw.strip()) if value]
                if kind == "translate":
                    x = values[0] if values else 0.0
                    y = values[1] if len(values) > 1 else 0.0
                    local_x += local_scale * x
                    local_y += local_scale * y
                else:
                    local_scale *= values[0]
            combined_scale = transform_scale * local_scale
            combined_x = translate_x + transform_scale * local_x
            combined_y = translate_y + transform_scale * local_y
            for child in element:
                visit(child, combined_scale, combined_x, combined_y)
            return
        fill = element.attrib.get("fill", "#000000")
        if tag == "rect":
            px_scale = scale * ss
            x = (float(element.attrib.get("x", 0)) * transform_scale + translate_x) * px_scale
            y = (float(element.attrib.get("y", 0)) * transform_scale + translate_y) * px_scale
            width = float(element.attrib["width"]) * transform_scale * px_scale
            height = float(element.attrib["height"]) * transform_scale * px_scale
            radius = float(element.attrib.get("rx", 0)) * transform_scale * px_scale
            draw.rounded_rectangle((x, y, x + width, y + height), radius=radius, fill=fill)
        elif tag == "path":
            px_scale = scale * ss * transform_scale
            offset = (translate_x * scale * ss, translate_y * scale * ss)
            polygons = _flatten_path(element.attrib["d"], px_scale, offset)
            for polygon in polygons:
                if len(polygon) >= 3:
                    draw.polygon(polygon, fill=fill)
    for element in root:
        visit(element)
    # Filter premultiplied colour and alpha together. Resizing straight RGBA
    # lets transparent black leak into pale edges and creates a dark fringe.
    return image.convert("RGBa").resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")


def _flatten_path(data: str, scale: float, offset=(0.0, 0.0)):
    tokens = re.findall(r"[MLHQVCZ]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", data)
    polygons = []
    points = []
    current = (0.0, 0.0)
    command = None
    i = 0
    while i < len(tokens):
        if tokens[i] in {"M", "L", "H", "V", "Q", "C", "Z"}:
            command = tokens[i]
            i += 1
            if command == "Z":
                if points:
                    polygons.append(points)
                    points = []
                continue
        if command in {"M", "L"}:
            x, y = float(tokens[i]), float(tokens[i + 1])
            i += 2
            current = (x, y)
            mapped = (x * scale + offset[0], y * scale + offset[1])
            if command == "M" and points:
                polygons.append(points)
                points = []
            points.append(mapped)
            command = "L" if command == "M" else command
        elif command == "H":
            x = float(tokens[i]); i += 1
            current = (x, current[1])
            points.append((current[0] * scale + offset[0], current[1] * scale + offset[1]))
        elif command == "V":
            y = float(tokens[i]); i += 1
            current = (current[0], y)
            points.append((current[0] * scale + offset[0], current[1] * scale + offset[1]))
        elif command == "Q":
            cx, cy, x, y = map(float, tokens[i:i + 4])
            i += 4
            start = current
            for step in range(1, 9):
                t = step / 8
                u = 1 - t
                px = u*u*start[0] + 2*u*t*cx + t*t*x
                py = u*u*start[1] + 2*u*t*cy + t*t*y
                points.append((px * scale + offset[0], py * scale + offset[1]))
            current = (x, y)
        elif command == "C":
            c1x, c1y, c2x, c2y, x, y = map(float, tokens[i:i + 6])
            i += 6
            start = current
            for step in range(1, 13):
                t = step / 12
                u = 1 - t
                px = u**3*start[0] + 3*u*u*t*c1x + 3*u*t*t*c2x + t**3*x
                py = u**3*start[1] + 3*u*u*t*c1y + 3*u*t*t*c2y + t**3*y
                points.append((px * scale + offset[0], py * scale + offset[1]))
            current = (x, y)
        else:
            raise ValueError(f"unsupported SVG path command: {command}")
    if points:
        polygons.append(points)
    return polygons


def _make_ico(sizes=SIZES, output: Path | None = None):
    output = output or ROOT / "branchglance.ico"
    images = [_render_mark(size, mode="full", tile=True) for size in sizes]
    output.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(output, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
    return output


def _make_rasters():
    STATIC.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        _render_mark(size, mode="full", tile=True).save(STATIC / f"phone-icon-{size}.png", optimize=False)
    _render_mark(180, mode="full", tile=True).save(STATIC / "apple-touch-icon.png", optimize=False)
    _make_ico(SIZES, ROOT / "branchglance.ico")
    # Conventional browser fallback and the native tray/app icon use the same
    # deterministic multi-size source artwork.
    _make_ico(SIZES, STATIC / "favicon.ico")


def _font(size: int):
    font = ImageFont.truetype(str(FONT), size)
    try:
        font.set_variation_by_name("SemiBold")
    except (AttributeError, OSError):
        pass
    return font


def _draw_glow(image: Image.Image, bounds, color=(185, 173, 245, 35), blur=60):
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(bounds, fill=color)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    image.alpha_composite(layer)


def _make_social_preview():
    width, height = 1280, 640
    image = Image.new("RGBA", (width, height), INK)
    _draw_glow(image, (40, 40, 570, 590), blur=78)
    icon = _render_mark(400, mode="full", tile=False)
    image.alpha_composite(icon, (68, 120))
    draw = ImageDraw.Draw(image)
    word_mask = _render_wordmark_mask(104, 4)
    word_mask = word_mask.resize((round(word_mask.width / 4 * WORDMARK_STRETCH_X),
                                  round(word_mask.height / 4)), Image.Resampling.LANCZOS)
    wordmark = Image.new("RGBA", word_mask.size, PAPER)
    wordmark.putalpha(word_mask)
    image.alpha_composite(wordmark, (510, 258))
    desc_font = _font(28)
    draw.text((510, 370), "LIVE AGENT VISIBILITY FOR WINDOWS", font=desc_font, fill=MUTED)
    image.convert("RGB").save(DOCS / "social-preview.png", optimize=False)


def _reference_cutout(box):
    reference = Image.open(ROOT / "reference" / "live-stack-reference.webp").convert("RGB").crop(box)
    alpha = Image.eval(reference.convert("L"), lambda value: max(0, min(255, round((value - 18) * 255 / 110))))
    reference.putalpha(alpha)
    return reference


def _make_compare():
    width, height = 1800, 850
    image = Image.new("RGBA", (width, height), INK)
    draw = ImageDraw.Draw(image)
    label_font = _font(26)
    small_font = _font(20)
    split = width // 2
    draw.text((30, 24), "MARK · REFERENCE", font=label_font, fill=MUTED)
    draw.text((split + 30, 24), "MARK · LIVE STACK VECTOR", font=label_font, fill=MUTED)
    draw.line((split, 20, split, 525), fill="#302E37", width=1)

    ref_mark = _reference_cutout((168, 228, 527, 531)).resize((512, 432), Image.Resampling.LANCZOS)
    own_square = _render_mark(512, mode="full", tile=False)
    own_mark = own_square.crop((0, 40, 512, 472))
    image.alpha_composite(ref_mark, ((split - 512) // 2, 72))
    image.alpha_composite(own_mark, (split + (split - 512) // 2, 72))

    draw.line((30, 540, width - 30, 540), fill="#302E37", width=1)
    draw.text((30, 556), "WORDMARK · REFERENCE", font=label_font, fill=MUTED)
    draw.text((split + 30, 556), "WORDMARK · GEIST SEMIBOLD / -2% TRACKING", font=label_font, fill=MUTED)
    ref_word = _reference_cutout((850, 350, 1450, 440))
    ref_bounds = ref_word.getchannel("A").getbbox()
    if ref_bounds is None:
        raise RuntimeError("reference wordmark crop contains no visible lettering")
    ref_word = ref_word.crop(ref_bounds)
    own_mask = _render_wordmark_mask(VECTOR_FONT_SIZE, VECTOR_OVERSAMPLE)
    own_mask = own_mask.resize((round(own_mask.width / VECTOR_OVERSAMPLE * WORDMARK_STRETCH_X),
                                round(own_mask.height / VECTOR_OVERSAMPLE)), Image.Resampling.LANCZOS)
    compare_height = 81
    ref_word = ref_word.resize((round(ref_word.width * compare_height / ref_word.height), compare_height), Image.Resampling.LANCZOS)
    own_mask = own_mask.resize((round(own_mask.width * compare_height / own_mask.height), compare_height), Image.Resampling.LANCZOS)
    own_word = Image.new("RGBA", own_mask.size, PAPER)
    own_word.putalpha(own_mask)
    image.alpha_composite(ref_word, (max(24, (split - ref_word.width) // 2), 642))
    image.alpha_composite(own_word, (split + (split - own_word.width) // 2, 642))
    draw.text((30, 760), f"Reference crop: {ref_word.width} × {ref_word.height}px", font=small_font, fill="#777483")
    draw.text((split + 30, 760), f"Outlined Geist path: {own_word.width} × {own_word.height}px", font=small_font, fill="#777483")
    image.convert("RGB").save(DOCS / "compare.png", optimize=False)


def _make_icon_sheet():
    cell_w, cell_h = 200, 268
    top_h, header_h = 250, 96
    width = cell_w * len(SIZES)
    height = header_h + top_h + 2 * cell_h
    image = Image.new("RGB", (width, height), INK)
    draw = ImageDraw.Draw(image)
    title_font = _font(36)
    label_font = _font(18)
    draw.text((28, 22), "LIVE STACK · MARK VARIANTS AND ICON SIZES", font=title_font, fill=PAPER)
    variants = (("FULL COLOUR", "full", True), ("MONO / DARK", "mono-dark", False),
                ("MONO / LIGHT", "mono-light", False))
    variant_w = width // 3
    for i, (label, mode, tile) in enumerate(variants):
        x = i * variant_w
        bg = INK if mode != "mono-light" else "#24232A"
        draw.rectangle((x, header_h, x + variant_w - 1, header_h + top_h - 1), fill=bg)
        mark = _render_mark(150, mode=mode, tile=tile)
        image.paste(mark, (x + (variant_w - 150) // 2, header_h + 18), mark)
        box = draw.textbbox((0, 0), label, font=label_font)
        draw.text((x + (variant_w - (box[2] - box[0])) // 2, header_h + 190), label, font=label_font, fill=MUTED)
        if i:
            draw.line((x, header_h, x, header_h + top_h), fill="#38353F", width=1)
    rows = (("DARK BACKGROUND", INK), ("LIGHT BACKGROUND", PAPER))
    for row, (row_label, bg) in enumerate(rows):
        y0 = header_h + top_h + row * cell_h
        label_color = MUTED if bg == INK else "#514F57"
        for col, size in enumerate(SIZES):
            x0 = col * cell_w
            draw.rectangle((x0, y0, x0 + cell_w - 1, y0 + cell_h - 1), fill=bg)
            if col == 0:
                draw.text((x0 + 10, y0 + 6), row_label, font=label_font, fill=label_color)
            icon = _render_mark(size, mode="full", tile=True)
            display_size = 128
            icon = icon.resize((display_size, display_size), Image.Resampling.NEAREST if size <= 32 else Image.Resampling.LANCZOS)
            dx = x0 + (cell_w - display_size) // 2
            dy = y0 + 34 + (132 - display_size) // 2
            image.paste(icon, (dx, dy), icon)
            caption = f"{size} px"
            box = draw.textbbox((0, 0), caption, font=label_font)
            fill = PAPER if bg == INK else INK
            draw.text((x0 + (cell_w - (box[2] - box[0])) // 2, y0 + 194), caption, font=label_font, fill=fill)
            if col:
                draw.line((x0, y0, x0, y0 + cell_h), fill="#38353F" if bg == INK else "#D0CEC9", width=1)
    image.save(DOCS / "icon-sheet.png", optimize=False)


def generate(output: Path | None = None) -> Path:
    """Generate every checked-in logo and icon artifact; --output is legacy ICO-only mode."""
    if output is not None:
        return _make_ico(SIZES, output)
    DOCS.mkdir(parents=True, exist_ok=True)
    _write_vectors()
    _make_rasters()
    _make_social_preview()
    _make_compare()
    _make_icon_sheet()
    return ROOT / "branchglance.ico"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="legacy mode: write only the multi-size ICO here")
    args = parser.parse_args()
    print(generate(args.output))
