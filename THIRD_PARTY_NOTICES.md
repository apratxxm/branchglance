# Third-party notices

Branchglance's original source is licensed under the MIT License in [`LICENSE`](LICENSE). The components below retain their own licenses; the MIT license does not replace those terms.

## Font files

The webfonts in `token_tracker/static/fonts/` are distributed under the SIL Open Font License, Version 1.1 (OFL-1.1). Full license text and copyright statements are present beside each font family:

| Font family | Copyright notice | License file |
| --- | --- | --- |
| Geist | Copyright 2024 The Geist Project Authors; source: [vercel/geist-font](https://github.com/vercel/geist-font) | [`token_tracker/static/fonts/geist/OFL.txt`](token_tracker/static/fonts/geist/OFL.txt) |
| Inter | Copyright 2020 The Inter Project Authors; source: [rsms/inter](https://github.com/rsms/inter) | [`token_tracker/static/fonts/inter/OFL.txt`](token_tracker/static/fonts/inter/OFL.txt) |
| Instrument Sans | Copyright 2022 The Instrument Sans Project Authors; source: [Instrument/instrument-sans](https://github.com/Instrument/instrument-sans) | [`token_tracker/static/fonts/instrument-sans/OFL.txt`](token_tracker/static/fonts/instrument-sans/OFL.txt) |
| Red Hat Mono | Copyright 2024 The Red Hat Project Authors; source: [RedHatOfficial/RedHatFont](https://github.com/RedHatOfficial/RedHatFont) | [`token_tracker/static/fonts/red-hat-mono/OFL.txt`](token_tracker/static/fonts/red-hat-mono/OFL.txt) |

The `assets/brand/wordmark.svg` outlines were traced from the bundled Geist SemiBold font. The font software remains under OFL-1.1; the generated wordmark is an image/document created using that font, not a redistributed font program.

## Python requirements

The direct requirements and their licenses are:

| Component | Version | License | Use / source |
| --- | --- | --- | --- |
| pywebview | 6.2.1 | BSD 3-Clause | Copyright (c) 2014–2017 Roman Sirokov. Desktop webview; [source/license](https://github.com/r0x0r/pywebview/blob/master/LICENSE) |
| pystray | 0.19.5 | GNU LGPL 3.0-or-later | Copyright (C) 2016–2025 Moses Palmér. System tray; [source/license](https://github.com/moses-palmer/pystray/blob/master/COPYING.LGPL) |
| Pillow | 12.3.0 | MIT-CMU | Copyright © 1997–2011 Secret Labs AB; Copyright © 1995–2011 Fredrik Lundh and contributors; Copyright © 2010 Jeffrey “Alex” Clark and contributors. Used by icon generation; [source/license](https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE) |
| zstandard | 0.25.0 | BSD 3-Clause | Copyright (c) 2016 Gregory Szorc. The included Zstandard library source is separately copyright Meta Platforms, Inc. and affiliates under BSD terms, with a GPLv2 alternative; [Python binding license](https://github.com/indygreg/python-zstandard/blob/0.25.0/LICENSE), [Zstandard source license](https://github.com/indygreg/python-zstandard/blob/0.25.0/zstd/LICENSE) |
| pywebpush | 2.5.0 | Mozilla Public License 2.0 (MPL-2.0) | Optional phone Web Push support; package author JR Conlin; [license/source](https://github.com/web-push-libs/pywebpush) |
| pytest | 8.4.2 | MIT | Copyright Holger Krekel and others, 2004. Development/test dependency only; [source/license](https://github.com/pytest-dev/pytest/blob/8.4.2/LICENSE) |

These licenses are compatible with using Branchglance's own source under MIT when their component terms are preserved. pystray and pywebpush remain separately licensed; changes to those components, if any, must follow their respective licenses. The application does not modify these dependencies.

`requirements-token-tracker.txt` pins the direct packages above, but does not lock transitive dependencies. Pip resolves those from the packages' declared metadata. Any frozen release should retain a build-specific dependency inventory and include the corresponding notices/licenses. The PyInstaller app build freezes the resolved runtime tree; its LGPL and MPL components need their separate notices in any binary distribution.

This repository has no `pyproject.toml`, `setup.cfg`, `setup.py`, or `package.json` project metadata declaration; there is no existing package `license` field to update. `LICENSE` and the README license badge/section identify the project license.

## Packaging tool

`packaging/build.py` invokes PyInstaller from the build environment, but PyInstaller is not pinned in this repository's requirements file. Copyright (c) 2010–2023 PyInstaller Development Team; Copyright (c) 2005–2009 Giovanni Bajo; based on work copyright (c) 2002 McMillan Enterprises, Inc. PyInstaller is licensed under GPL-2.0-or-later with its Bootloader Exception; its runtime hooks and some runtime modules are Apache-2.0. The exception permits distributing applications built with its bootloader under a different license, while PyInstaller modifications and separately redistributed PyInstaller files remain subject to their own terms. See [PyInstaller's license terms](https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt).

## JavaScript and brand artwork

The static frontend and `.mjs` checks use vanilla JavaScript and Node built-ins; there is no `package.json` or third-party JavaScript package to list. `assets/brand/generate_icon.py` creates the project's SVG and icon outputs from the repository's geometry and bundled Geist font. The source and generated mark files are Branchglance artwork, subject to the repository license except where a third-party asset is identified above.
