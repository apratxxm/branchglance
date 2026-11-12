# Third-party notices

Branchglance's original source is licensed under the MIT License in [`LICENSE`](LICENSE). The components below retain their own licenses; the MIT license does not replace those terms.

## Font files

The webfonts in `token_tracker/static/fonts/` are distributed under the SIL Open Font License, Version 1.1 (OFL-1.1). Full license text and copyright statements are present beside each font family:

| Font family | Copyright notice | License file |
| --- | --- | --- |
| Geist | Copyright 2024 The Geist Project Authors; source: [vercel/geist-font](https://github.com/vercel/geist-font) | [`token_tracker/static/fonts/geist/OFL.txt`](token_tracker/static/fonts/geist/OFL.txt) |
| Inter | Copyright 2020 The Inter Project Authors; source: [rsms/inter](https://github.com/rsms/inter) | [`token_tracker/static/fonts/inter/OFL.txt`](token_tracker/static/fonts/inter/OFL.txt) |

The `assets/brand/wordmark.svg` outlines were traced from the bundled Geist SemiBold font. The font software remains under OFL-1.1; the generated wordmark is an image/document created using that font, not a redistributed font program.

## Python requirements

The direct requirements and their licenses are:

| Component | Version | License | Use / source |
| --- | --- | --- | --- |
| pywebview | 6.2.1 | BSD 3-Clause | Copyright (c) 2014–2017 Roman Sirokov. Desktop webview; [source/license](https://github.com/r0x0r/pywebview/blob/master/LICENSE) |
| pystray | 0.19.5 | GNU LGPL 3.0-or-later | Copyright (C) 2016–2025 Moses Palmér. System tray; [source/license](https://github.com/moses-palmer/pystray/blob/master/COPYING.LGPL) |
| Pillow | 12.3.0 | MIT-CMU | Copyright © 1997–2011 Secret Labs AB; Copyright © 1995–2011 Fredrik Lundh and contributors; Copyright © 2010 Jeffrey “Alex” Clark and contributors. Used by icon generation; [source/license](https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE) |
| zstandard | 0.25.0 | BSD 3-Clause | Copyright (c) 2016 Gregory Szorc. The included Zstandard library source is separately copyright Meta Platforms, Inc. and affiliates under BSD terms, with a GPLv2 alternative; [Python binding license](https://github.com/indygreg/python-zstandard/blob/0.25.0/LICENSE), [Zstandard source license](https://github.com/indygreg/python-zstandard/blob/0.25.0/zstd/LICENSE) |
| pytest | 8.4.2 | MIT | Copyright Holger Krekel and others, 2004. Development/test dependency only; [source/license](https://github.com/pytest-dev/pytest/blob/8.4.2/LICENSE) |
| tzdata | 2026.5 | Apache-2.0 | Copyright Python Software Foundation and contributors. Time-zone data bundled in the app for named time zones; [source/license](https://github.com/python/tzdata/blob/master/LICENSE) |

### Transitive runtime packages in the Windows app

The frozen Windows app also bundles the runtime dependencies that pywebview and pystray pull in. They are pinned in `packaging/requirements-release.txt` and keep their own licenses:

| Component | Version | License | Use / source |
| --- | --- | --- | --- |
| pythonnet | 3.1.0 | MIT | Copyright (c) 2006–2021 the contributors of the Python.NET project. Pulled in by pywebview for the Windows .NET bridge; [homepage](https://pythonnet.github.io/) |
| clr_loader | 0.3.1 | MIT | Copyright (c) 2019–2026 Benedikt Reinartz. Pulled in by pythonnet; [source](https://github.com/pythonnet/clr-loader) |
| cffi | 2.1.1 | MIT-0 (per package metadata) | Copyright Armin Rigo and Maciej Fijalkowski. Pulled in by clr_loader; [documentation](https://cffi.readthedocs.io/) |
| pycparser | 3.11 | BSD 3-Clause | Copyright (c) 2008–2027, Eli Bendersky. Pulled in by cffi; [source/license](https://github.com/eliben/pycparser) |
| bottle | 0.13.4 | MIT | Copyright (c) 2009–2024, Marcel Hellkamp. Pulled in by pywebview; [homepage](http://bottlepy.org/) |
| proxy_tools | 0.1.0 | MIT (per package metadata) | Copyright (c) 2013 Armin Ronacher; adapted by Jonathan Tushman, 2014. Pulled in by pywebview; [source](http://github.com/jtushman/proxy_tools) |
| typing_extensions | 4.16.0 | PSF-2.0 | Copyright Guido van Rossum, Jukka Lehtosalo, Łukasz Langa, Michael Lee and contributors. Pulled in by pywebview; [source](https://github.com/python/typing_extensions) |
| six | 1.17.0 | MIT | Copyright (c) 2010–2024 Benjamin Peterson. Pulled in by pystray; [source](https://github.com/benjaminp/six) |

pytest and its dependencies and the PyInstaller build tool are used only to test and build; they are not part of the shipped app. PyInstaller's bootloader is, under the terms noted below.

These licenses are compatible with using Branchglance's own source under MIT when their component terms are preserved. pystray remains separately licensed; changes to those components, if any, must follow their respective licenses. The application does not modify these dependencies.

`requirements-token-tracker.txt` pins the direct packages above, but does not lock transitive dependencies. Pip resolves those from the packages' declared metadata. Any frozen release should retain a build-specific dependency inventory and include the corresponding notices/licenses. The PyInstaller app build freezes the resolved runtime tree; its LGPL and MPL components need their separate notices in any binary distribution.

This repository has no `pyproject.toml`, `setup.cfg`, `setup.py`, or `package.json` project metadata declaration; there is no existing package `license` field to update. `LICENSE` and the README license badge/section identify the project license.

## Packaging tool

`packaging/build.py` invokes PyInstaller from the build environment, and PyInstaller is pinned in `packaging/requirements-release.txt` for build use only. Copyright (c) 2010–2023 PyInstaller Development Team; Copyright (c) 2005–2009 Giovanni Bajo; based on work copyright (c) 2002 McMillan Enterprises, Inc. PyInstaller is licensed under GPL-2.0-or-later with its Bootloader Exception; its runtime hooks and some runtime modules are Apache-2.0. The exception permits distributing applications built with its bootloader under a different license, while PyInstaller modifications and separately redistributed PyInstaller files remain subject to their own terms. See [PyInstaller's license terms](https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt).

## JavaScript and brand artwork

The static frontend and `.mjs` checks use vanilla JavaScript and Node built-ins; there is no `package.json` or third-party JavaScript package to list. `assets/brand/generate_icon.py` creates the project's SVG and icon outputs from the repository's geometry and bundled Geist font. The source and generated mark files are Branchglance artwork, subject to the repository license except where a third-party asset is identified above.
