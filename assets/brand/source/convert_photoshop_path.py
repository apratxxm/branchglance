"""Convert the frozen Photoshop Illustrator path export to two exact SVGs.

The original LiveStack04 concept was AI-generated in this Codex conversation,
then refined as the Photoshop vector layer `Logo - matched corners`.
`mark-native.svg` retains every native cubic. `mark-master.svg` applies the
lead-approved straight-span cleanup to the named 0-based native segments;
all unselected rounded corners retain their original cubic controls. Only
native path records are parsed. Illustrator/PostScript operators are never
executed. Native coordinate values are emitted as stored; one group transform
translates the ROI and flips Y from Photoshop's 1000x1000 Y-up canvas.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "photoshop-native-path.ai"
MASTER = HERE.parent / "mark-master.svg"
NATIVE = HERE.parent / "mark-native.svg"
MASTER_PREVIEW = HERE / "mark-master-on-red.png"
NATIVE_PREVIEW = HERE / "mark-native-on-red.png"
EXPECTED_SOURCE_SHA256 = "3c2f0ac620bbef6b54db774d37c0a6e428dc87106a86bc2f4ea83b1edac192c3"
# ROI is the native control-point bounds plus exactly 1 px on each side.
XMIN, XMAX, YMIN, YMAX = 402.8915, 597.3609, 416.8, 584.0452
TX, TY = XMIN - 1, YMAX + 1
WIDTH, HEIGHT = XMAX - XMIN + 2, YMAX - YMIN + 2
NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\Z")
SVG_NS = "http://www.w3.org/2000/svg"
# User-approved native cubic segment indices to emit as true straight lines.
# H/V is used when the unchanged native endpoint shares a coordinate; otherwise L.
STRAIGHT_SEGMENTS = {0: {0, 2, 4, 6}, 1: {0, 2, 4, 6, 8, 10}, 2: {0, 2, 4, 6}, 3: {0, 3, 5}}


def parse_native() -> tuple[list[list[tuple[str, list[str]]]], dict[str, int]]:
    raw = SOURCE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SOURCE_SHA256:
        raise SystemExit(f"STOP: frozen native AI source hash mismatch: {digest}")
    text = raw.decode("ascii")
    paths: list[list[tuple[str, list[str]]]] = []
    current: list[tuple[str, list[str]]] | None = None
    counts = {"m": 0, "c": 0, "l": 0, "h": 0}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "n":
            if current is None:
                continue
            paths.append(current)
            current = None
            continue
        fields = stripped.split()
        if not fields:
            continue
        op = fields[-1]
        if op in counts and all(NUMBER.fullmatch(x) for x in fields[:-1]):
            arity = {"m": 2, "c": 6, "l": 2, "h": 0}[op]
            if len(fields[:-1]) != arity:
                raise ValueError(f"Unexpected {op} coordinate count: {line}")
            if op == "m":
                if current is not None:
                    raise ValueError("Move before previous contour terminator")
                current = []
            elif current is None:
                raise ValueError(f"Path segment without move: {line}")
            current.append((op, fields[:-1]))
            counts[op] += 1
        elif current is not None and re.match(r"^[+-]?(?:\d|\.)", stripped):
            raise ValueError(f"Unsupported native path record: {line}")
    if current is not None:
        raise ValueError("Last native path has no terminator")
    if len(paths) != 4 or counts["m"] != 4:
        raise ValueError(f"Expected four native contours, got {len(paths)}")
    for path in paths:
        if not path or path[0][0] != "m":
            raise ValueError("Contour does not start with native move")
        first = path[0][1]
        last_op, last_args = path[-1]
        endpoint = last_args[-2:] if last_op in ("c", "l") else first
        if endpoint != first:
            raise ValueError(f"Contour endpoint differs from start: {endpoint} != {first}")
    return paths, counts


def svg_path(path: list[tuple[str, list[str]]], contour: int, clean: bool) -> str:
    start=path[0][1]; commands=["M"+" ".join(start)]; current=tuple(start); segment=0
    for op,args in path[1:]:
        if op=="c":
            end=tuple(args[-2:])
            if clean and segment in STRAIGHT_SEGMENTS[contour]:
                x0,y0=map(float,current); x1,y1=map(float,end)
                if y0==y1: commands.append("H"+end[0])
                elif x0==x1: commands.append("V"+end[1])
                else: commands.append("L"+" ".join(end))
            else:
                commands.append("C"+" ".join(args))
            current=end; segment+=1
        elif op=="l":
            commands.append("L"+" ".join(args)); current=tuple(args); segment+=1
        elif op=="h": commands.append("Z")
    if commands[-1] != "Z": commands.append("Z")
    return " ".join(commands)


def write_svg(paths: list[list[tuple[str, list[str]]]], clean: bool, label: str) -> str:
    path_elements = "\n".join(f'<path d="{svg_path(path,i,clean)}" fill="#000000"/>' for i,path in enumerate(paths))
    description = "Photoshop native paths with approved straight-span cleanup; all other native corner cubics retained." if clean else "Literal Photoshop AI path conversion; all native cubic control points retained."
    return (
        f'<svg xmlns="{SVG_NS}" width="{WIDTH:.4f}" height="{HEIGHT:.4f}" '
        f'viewBox="0 0 {WIDTH:.4f} {HEIGHT:.4f}">\n'
        f'<title>{label}</title><desc>{description}</desc>\n'
        f'<g transform="translate({-TX:.4f} {TY:.4f}) scale(1 -1)" fill="#000000" stroke="none">\n'
        f'{path_elements}\n</g>\n</svg>\n'
    )


def render_preview(svg: str, output: Path, node: str, node_modules: str) -> None:
    # Red is only a contrast background for comparison with Photoshop.
    red_svg = svg.replace("<g ", '<rect x="0" y="0" width="100%" height="100%" fill="#ff0000"/>\n<g ', 1)
    script = (
        "const fs=require('node:fs'); const sharp=require('sharp'); "
        "const s=fs.readFileSync(0,'utf8'); "
        "sharp(Buffer.from(s),{density:384}).png().toFile(process.argv[1])"
        ".catch(e=>{console.error(e);process.exit(1)})"
    )
    env = os.environ.copy()
    env["NODE_PATH"] = node_modules
    subprocess.run([node, "-e", script, str(output)], input=red_svg, text=True, check=True, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert native Photoshop AI3 path records to exact SVG")
    parser.add_argument("--node", default=os.environ.get("BRAND_NODE"), help="Optional existing Node runtime containing sharp for preview PNGs")
    parser.add_argument("--node-modules", default=os.environ.get("BRAND_NODE_MODULES"), help="Optional existing node_modules path containing sharp")
    args = parser.parse_args()
    if bool(args.node) != bool(args.node_modules):
        raise SystemExit("For PNG previews, pass both --node and --node-modules or set both BRAND_NODE and BRAND_NODE_MODULES")
    paths, counts = parse_native()
    native_svg = write_svg(paths, clean=False, label="Literal Photoshop native path")
    master_svg = write_svg(paths, clean=True, label="Photoshop path master")
    NATIVE.write_text(native_svg, encoding="utf-8", newline="\n")
    MASTER.write_text(master_svg, encoding="utf-8", newline="\n")
    if args.node:
        render_preview(native_svg, NATIVE_PREVIEW, args.node, args.node_modules)
        render_preview(master_svg, MASTER_PREVIEW, args.node, args.node_modules)
    nlines=sum(len(STRAIGHT_SEGMENTS[i]) for i in STRAIGHT_SEGMENTS)
    preview_note = f", {NATIVE_PREVIEW}, {MASTER_PREVIEW}" if args.node else " (SVG-only; previews skipped)"
    print(f"Wrote {NATIVE}, {MASTER}{preview_note}; native commands {counts}; approved straight spans {nlines}; retained cubic segments {counts['c']-nlines}")


if __name__ == "__main__":
    main()
