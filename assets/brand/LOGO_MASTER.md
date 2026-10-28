# Canonical Live Stack production logo

`mark-master.svg` is the approved production geometry source. Its shape comes from Photoshop's **Logo - matched corners Shape Path**, exported by **File → Export → Paths to Illustrator**.

- Frozen master SHA-256: `96e1c4f66cd9e094b181889ea92ebd983f56bfc6fad172e51a3bb9d162cefae6`.
- Native AI source: `source/photoshop-native-path.ai`; SHA-256 `3c2f0ac620bbef6b54db774d37c0a6e428dc87106a86bc2f4ea83b1edac192c3`.
- `mark-native.svg` is the literal conversion with all 35 native cubic segments.
- `mark-master.svg` contains 17 requested straight spans and 18 retained native corner cubics across four closed contours.
- The master applies one ROI translation and Y flip. It does not trace pixels or rescale X and Y separately.

`source/convert_photoshop_path.py` reproduces the literal native SVG and approved master. `generate_icon.py` reads and SHA-checks the frozen master before deriving production colour/mono SVGs, lockups, tile/app/web icons, and ICOs. It renders through the shared `render_svgs.js` helper and existing build-only `sharp`; this is not an app runtime dependency. `lockup.svg` aliases `lockup-dark.svg`; use the dark lockup with its embedded charcoal background for GitHub and social previews.

Palette/order: top `#F2F0EA`, middle `#B9ADF5`, diagonal `#F2F0EA`, wedge `#F2F0EA`. The outlined wordmark remains in `wordmark.svg` and retains its evenodd fill rule.

The original AI-assisted concept and Photoshop refinement do not establish trademark clearance or legal exclusivity.
