# Live Stack brand

The approved mark is the Photoshop `Logo - matched corners Shape Path`, exported through **File → Export → Paths to Illustrator**. Its exact native path is preserved in `source/photoshop-native-path.ai` and `mark-native.svg`; the approved `mark-master.svg` converts 17 designated straight spans to lines and retains 18 original rounded-corner cubic segments. It has four closed contours in native order: top, middle, diagonal, wedge.

Full colour assigns off-white to the top, diagonal, and wedge, with lavender on the stepped middle. Use it on charcoal. `mark-mono-dark.svg` is off-white for dark surfaces. `mark-mono-light.svg` is near-black on a light rounded tile; `mark-mono-light-transparent.svg` is the same mono mark on transparent canvas. Geometry remains the same at every size; no compact hand-drawn substitute is used.

`wordmark.svg` retains the existing outlined Branchglance lettering and its evenodd counter rule. `lockup-dark.svg` and `lockup-light.svg` combine those unchanged outlines with the canonical mark. Legacy `lockup.svg` is an exact alias of the dark lockup; `social-preview.png` is the 1280×640 dark lockup raster.

`generate_icon.py` verifies the canonical master SHA-256 before producing all brand SVGs and raster icons. It renders SVGs with the existing build-only `sharp`/libvips runtime, then packages the SVG-rendered frames into ICO files. `render_svgs.js` is the shared renderer. Supply the existing runtime using `--node` and `--node-modules`, or `BRAND_NODE` and `BRAND_NODE_MODULES`. No application dependency is added.

The generator updates `branchglance.ico` and these static assets: `favicon.svg`, `favicon.ico`, `phone-icon.svg`, `phone-icon-192.png`, `phone-icon-512.png`, and `apple-touch-icon.png`. Static icons use a charcoal tile for light/dark surface contrast. No phone-feature logic or release state is changed by asset generation.

The source and its refinement history do not establish trademark clearance, exclusivity, or legal originality.
