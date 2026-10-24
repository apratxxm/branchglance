# Live Stack brand

The selected mark is a four-band stack: a short cap, a lavender step, a slanted bar and a tapered lower wedge. The lavender L-shaped notch creates the stepped join; the descending white bands suggest parallel work continuing through the stack. Use the full-colour mark on dark charcoal, the off-white mono mark on dark surfaces, and the near-black mono mark knocked out of an off-white rounded tile on light surfaces.

`mark-full.svg`, `mark-mono-dark.svg` and `mark-mono-light.svg` are the three mark variants. `wordmark.svg` contains outlined Branchglance lettering traced from the bundled Geist SemiBold variable font with -2% tracking. Geist was selected over Inter for its closer geometric forms and single-storey `g`. `lockup.svg` combines the full-colour mark and outlined wordmark on charcoal. The small header in the visual reference is presentation text and is not part of the logo.

`generate_icon.py` uses Pillow, already available in the build environment, to reproduce the vectors as the multi-size `.ico`, tray/app icon, favicon, Apple touch icon and PWA icons. The generator also creates the social preview and icon review sheet. The app has no added runtime font or image dependency.

## Small-size use

The generator hand-tunes the small raster silhouette: the 16 px mark uses a horizontal third bar and integer-pixel edges; 20, 24 and 32 px restore the descending slant with half-pixel edge snapping. It uses the canonical geometry for 40, 48, 64, 128 and 256 px. The `.ico` contains every size in that set. The tray/app tile uses a charcoal rounded square with off-white and lavender bands so it remains distinct on light and dark taskbars.

The 1280 × 640 social preview is a dark presentation card; it is not a replacement icon. `docs/brand/compare.png` compares the reference mark and wordmark with the vector redraw at matched dimensions. `docs/brand/icon-sheet.png` shows the raster sizes against both dark and light surfaces. Review those at native scale before replacing any installed assets.

The Codex blue and Claude orange interface accents remain provider colours. The brand mark does not change the widget palette or imitate a live status.
