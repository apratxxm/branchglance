"""Generate production Branchglance logo, lockup, and icon assets from mark-master.svg.

Build-time rasterization uses the existing sharp/libvips runtime; it does not add
an application dependency. The frozen master hash and path commands are checked
before any output is written. No independent mark geometry lives in this file.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
STATIC = REPO / "token_tracker" / "static"
MASTER = ROOT / "mark-master.svg"
WORDMARK = ROOT / "wordmark.svg"
EXPECTED_MASTER_SHA256 = "96e1c4f66cd9e094b181889ea92ebd983f56bfc6fad172e51a3bb9d162cefae6"
INK, LAVENDER, PAPER = "#0B0B0D", "#B9ADF5", "#F2F0EA"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def _tag(name: str) -> str:
    return f"{{{SVG_NS}}}{name}"


def _master() -> tuple[ET.Element, ET.Element, list[ET.Element]]:
    source_bytes = MASTER.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != EXPECTED_MASTER_SHA256:
        raise RuntimeError(f"canonical mark-master.svg hash mismatch: {digest}")
    root = ET.fromstring(source_bytes)
    group = root.find(_tag("g"))
    if group is None:
        raise RuntimeError("canonical master group missing")
    paths = group.findall(_tag("path"))
    if len(paths) != 4:
        raise RuntimeError("canonical master must contain exactly four path contours")
    command_counts = {cmd: 0 for cmd in ("C", "L", "H", "V", "Z")}
    for path in paths:
        d = path.get("d", "")
        if not d.startswith("M") or not d.rstrip().endswith("Z"):
            raise RuntimeError("canonical contour is not a closed absolute path")
        for cmd in command_counts:
            command_counts[cmd] += len(re.findall(rf"(?<![A-Za-z]){cmd}(?![A-Za-z])", d))
    if (command_counts["C"], command_counts["L"] + command_counts["H"] + command_counts["V"], command_counts["Z"]) != (18, 17, 4):
        raise RuntimeError(f"canonical master geometry changed: {command_counts}")
    return root, group, paths


def _wordmark_path() -> ET.Element:
    root = ET.parse(WORDMARK).getroot()
    path = root.find(_tag("path"))
    if path is None or path.get("fill-rule") != "evenodd":
        raise RuntimeError("existing outlined wordmark with evenodd fill rule is required")
    return path


def _serialize(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode") + "\n"


def _set_title(root: ET.Element, title: str, description: str) -> None:
    title_el = root.find(_tag("title"))
    if title_el is None:
        title_el = ET.Element(_tag("title"))
        root.insert(0, title_el)
    title_el.text = title
    desc_el = root.find(_tag("desc"))
    if desc_el is not None:
        desc_el.text = description


def _recolor(root: ET.Element, fills: tuple[str, str, str, str], title: str, description: str) -> ET.Element:
    out = copy.deepcopy(root)
    _set_title(out, title, description)
    group = out.find(_tag("g"))
    if group is None:
        raise RuntimeError("master group missing in variant")
    for path, fill in zip(group.findall(_tag("path")), fills):
        path.set("fill", fill)
    return out


def _square_wrapper(root: ET.Element, size: int, background: str, radius: int | None = None) -> ET.Element:
    """Letterbox canonical root uniformly; retain its exact group transform once."""
    vb = [float(v) for v in root.get("viewBox", "").split()]
    if len(vb) != 4:
        raise RuntimeError("master viewBox missing")
    _, _, width, height = vb
    side = max(width, height)
    y = (height - side) / 2
    outer = ET.Element(_tag("svg"), {"viewBox": f"0 {y:.4f} {side:.4f} {side:.4f}", "width": str(size), "height": str(size), "role": "img"})
    ET.SubElement(outer, _tag("title")).text = "Branchglance Live Stack app icon"
    bg_attrs = {"x": "0", "y": f"{y:.4f}", "width": f"{side:.4f}", "height": f"{side:.4f}", "fill": background}
    if radius is not None:
        # Center the rounded background on the square viewport; the original mark group is untouched.
        outer.set("viewBox", f"0 0 {side:.4f} {side:.4f}")
        bg_attrs = {"x": "0", "y": "0", "width": f"{side:.4f}", "height": f"{side:.4f}", "rx": f"{side*radius/512:.4f}", "fill": background}
        fit_width = side * 0.84
        fit_height = fit_width * height / width
        nested_x = (side - fit_width) / 2
        nested_y = (side - fit_height) / 2
        nested = ET.Element(_tag("svg"), {"x": f"{nested_x:.4f}", "y": f"{nested_y:.4f}", "width": f"{fit_width:.4f}", "height": f"{fit_height:.4f}", "viewBox": root.get("viewBox")})
        ET.SubElement(outer, _tag("rect"), bg_attrs)
        group = root.find(_tag("g"))
        if group is None: raise RuntimeError("master group missing")
        nested.append(copy.deepcopy(group)); outer.append(nested)
        return outer
    ET.SubElement(outer, _tag("rect"), bg_attrs)
    group = root.find(_tag("g"))
    if group is None: raise RuntimeError("master group missing")
    nested = ET.Element(_tag("svg"), {"x": "0", "y": "0", "width": f"{side:.4f}", "height": f"{side:.4f}", "viewBox": outer.get("viewBox")})
    # Square viewBox already letterboxes; preserve source group transform without a second Y flip.
    nested.set("viewBox", f"0 {y:.4f} {side:.4f} {side:.4f}")
    nested.append(copy.deepcopy(group)); outer.append(nested)
    return outer


def _write_vectors() -> dict[str, str]:
    source, group, paths = _master()
    full = _recolor(source, (PAPER, LAVENDER, PAPER, PAPER), "Live Stack full-colour mark", "Approved Photoshop-derived mark: paper top, lavender middle, paper diagonal and wedge.")
    mono_dark = _recolor(source, (PAPER,) * 4, "Live Stack mono dark mark", "Exact canonical mark in off-white for dark surfaces.")
    mono_light = _recolor(source, (INK,) * 4, "Live Stack mono light mark", "Exact canonical mark knocked out in near-black on a light rounded tile.")
    mono_light_transparent = _recolor(source, (INK,) * 4, "Live Stack mono light transparent mark", "Canonical mark in near-black on a transparent canvas.")
    width, height = float(source.get("width")), float(source.get("height"))
    natural_h = f"{512 * height / width:.4f}"
    for name, root in (("mark-full.svg", full), ("mark-mono-dark.svg", mono_dark)):
        root.set("width", "512"); root.set("height", natural_h)
        (ROOT / name).write_text(_serialize(root), encoding="utf-8")
    mono_tile = _square_wrapper(mono_light, 512, PAPER, radius=96)
    (ROOT / "mark-mono-light.svg").write_text(_serialize(mono_tile), encoding="utf-8")
    (ROOT / "mark-mono-light-transparent.svg").write_text(_serialize(mono_light_transparent), encoding="utf-8")

    # Wordmark remains its existing self-contained outline; lockups embed the same path with evenodd.
    word_path = _wordmark_path()
    lockups: dict[str, str] = {}
    for theme, word_fill, background in (("dark", PAPER, INK), ("light", INK, PAPER)):
        outer = ET.Element(_tag("svg"), {"viewBox": "0 0 1280 640", "width": "1280", "height": "640", "role": "img"})
        ET.SubElement(outer, _tag("title")).text = f"Branchglance Live Stack {theme} lockup"
        ET.SubElement(outer, _tag("rect"), {"x": "0", "y": "0", "width": "1280", "height": "640", "fill": background})
        nested_h = 330 * height / width
        nested = ET.Element(_tag("svg"), {"x": "45", "y": "140", "width": "330", "height": f"{nested_h:.4f}", "viewBox": source.get("viewBox")})
        mark_group = _recolor(source, (PAPER, LAVENDER, PAPER, PAPER), "", "").find(_tag("g"))
        if mark_group is None: raise RuntimeError("master group missing for lockup")
        nested.append(mark_group); outer.append(nested)
        text_path = copy.deepcopy(word_path)
        text_path.set("fill", word_fill); text_path.set("transform", "translate(445 262) scale(0.497)")
        outer.append(text_path)
        data = _serialize(outer)
        lockups[theme] = data
        (ROOT / f"lockup-{theme}.svg").write_text(data, encoding="utf-8")
    # Legacy path remains a documented exact alias of the charcoal GitHub-ready lockup.
    (ROOT / "lockup.svg").write_text(lockups["dark"], encoding="utf-8")
    return {"full": _serialize(full), "mono-dark": _serialize(mono_dark), "mono-light": _serialize(mono_tile), "mono-light-transparent": _serialize(mono_light_transparent), "lockup-dark": lockups["dark"], "lockup-light": lockups["light"]}


def _render_jobs(vectors: dict[str, str], jobs: list[dict], scratch: Path) -> None:
    for name, svg in vectors.items():
        (scratch / f"{name}.svg").write_text(svg, encoding="utf-8")
    source, _, _ = _master()
    full = _recolor(source, (PAPER, LAVENDER, PAPER, PAPER), "tile", "")
    icon = _square_wrapper(full, 512, INK, radius=96)
    icon_svg = _serialize(icon)
    (ROOT / "icon-tile.svg").write_text(icon_svg, encoding="utf-8")
    (STATIC / "phone-icon.svg").write_text(icon_svg, encoding="utf-8")
    (STATIC / "favicon.svg").write_text(icon_svg, encoding="utf-8")
    (scratch / "icon-tile.svg").write_text(icon_svg, encoding="utf-8")
    for size in SIZES:
        jobs.append({"source": str(scratch / "icon-tile.svg"), "output": str(scratch / f"icon-{size}.png"), "width": size, "height": size})
    for size, path in ((180, STATIC / "apple-touch-icon.png"), (192, STATIC / "phone-icon-192.png"), (512, STATIC / "phone-icon-512.png")):
        jobs.append({"source": str(scratch / "icon-tile.svg"), "output": str(path), "width": size, "height": size})
    width, height = float(source.get("width")), float(source.get("height"))
    for name, svg_name in (("full", "full"), ("mono-dark", "mono-dark"), ("mono-light", "mono-light-transparent")):
        for size in (256, 512, 1024):
            h = round(size * height / width)
            jobs.append({"source": str(scratch / f"{svg_name}.svg"), "output": str(ROOT / f"mark-{name}-{size}.png"), "width": size, "height": h})
    jobs.append({"source": str(scratch / "lockup-dark.svg"), "output": str(ROOT / "lockup-dark.png"), "width": 1280, "height": 640})
    jobs.append({"source": str(scratch / "lockup-light.svg"), "output": str(ROOT / "lockup-light.png"), "width": 1280, "height": 640})
    jobs.append({"source": str(scratch / "lockup-dark.svg"), "output": str(ROOT / "social-preview.png"), "width": 1280, "height": 640})


def _render_png_jobs(jobs: list[dict], node: str, node_modules: str) -> None:
    env = os.environ.copy(); env["NODE_PATH"] = node_modules
    subprocess.run([node, str(ROOT / "render_svgs.js")], input=json.dumps(jobs), text=True, check=True, env=env)


def _write_ico_png_frames(frames: dict[int, Path], output: Path) -> Path:
    """Package exact SVG-rendered PNG frames into a standard multi-image ICO container."""
    sizes = tuple(sorted(frames))
    if not sizes or any(n < 1 or n > 256 for n in sizes):
        raise ValueError("ICO PNG frame sizes must be in 1..256")
    payloads = [frames[n].read_bytes() for n in sizes]
    if any(not png.startswith(b"\x89PNG\r\n\x1a\n") for png in payloads):
        raise ValueError("every ICO frame must be a PNG rendered from its SVG source")
    header_size = 6 + 16 * len(sizes)
    entries = []
    offset = header_size
    for n, png in zip(sizes, payloads):
        dim = 0 if n == 256 else n
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset))
        offset += len(png)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(sizes)))
        f.writelines(entries)
        f.writelines(payloads)
    return output


def _make_ico(sizes=SIZES, output: Path | None = None, *, node=None, node_modules=None):
    """Backward-compatible ICO helper; every frame is rendered from icon-tile.svg."""
    output = Path(output) if output else ROOT / "branchglance.ico"
    node = node or os.environ.get("BRAND_NODE") or shutil.which("node")
    node_modules = node_modules or os.environ.get("BRAND_NODE_MODULES") or os.environ.get("NODE_PATH")
    if not node or not node_modules: raise RuntimeError("ICO generation requires existing Node/sharp paths")
    source, _, _ = _master()
    full = _recolor(source, (PAPER, LAVENDER, PAPER, PAPER), "icon", "")
    icon = _serialize(_square_wrapper(full, 512, INK, radius=96))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="branchglance-ico-") as temp:
        temp=Path(temp); src=temp/"icon.svg"; src.write_text(icon,encoding="utf-8")
        jobs=[]
        for n in sizes: jobs.append({"source":str(src),"output":str(temp/f"{n}.png"),"width":n,"height":n})
        _render_png_jobs(jobs,str(node),str(node_modules))
        _write_ico_png_frames({n:temp/f"{n}.png" for n in sizes}, output)
    return output


def generate(output: Path | None = None, *, node=None, node_modules=None) -> Path:
    """Generate canonical brand SVGs, static icons, lockups and the app/browser ICOs."""
    if output is not None:
        return _make_ico(SIZES, output, node=node, node_modules=node_modules)
    node = node or os.environ.get("BRAND_NODE") or shutil.which("node")
    node_modules = node_modules or os.environ.get("BRAND_NODE_MODULES") or os.environ.get("NODE_PATH")
    if not node or not node_modules: raise RuntimeError("Pass --node and --node-modules for the existing build-only sharp renderer")
    STATIC.mkdir(parents=True, exist_ok=True)
    vectors = _write_vectors()
    with tempfile.TemporaryDirectory(prefix="branchglance-assets-") as temp:
        scratch=Path(temp); jobs=[]
        _render_jobs(vectors,jobs,scratch)
        _render_png_jobs(jobs,str(node),str(node_modules))
        # Render small ICO source frames directly from the same tile SVG at each requested size.
        for ico_path in (ROOT/"branchglance.ico",STATIC/"favicon.ico"):
            _write_ico_png_frames({n:scratch/f"icon-{n}.png" for n in SIZES},ico_path)
    # Publish a source/layout alias only within the brand directory; no static HTML or app logic changes.
    return ROOT/"branchglance.ico"


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,help="legacy mode: write only the multi-size ICO")
    parser.add_argument("--node",help="existing Node executable containing sharp")
    parser.add_argument("--node-modules",help="existing node_modules folder containing sharp")
    args=parser.parse_args()
    print(generate(args.output,node=args.node,node_modules=args.node_modules))
