# Branchglance mark concepts

Drafts for approval. Hand-written SVG; no dependencies, external assets, embedded fonts or app changes. All three marks use a 16 x 16 coordinate grid and can be placed at 16 px. XML validity will be checked; tray raster quality and contrast in the native shell still need visual acceptance.

| File | Idea | Why it fits | Tradeoff |
| --- | --- | --- | --- |
| `mark-fork.svg` | One root branches to two visible children. | The most direct connection to the live chat/subagent tree; rounded Git-like joins and endpoint dots. **Recommended.** | A familiar branch symbol; does not carry the attention concept alone. |
| `mark-bands.svg` | One spine connects three stacked endpoints. | Suggests the widget's three blocky bands and several work lanes held in one view. | Can read as a list/settings symbol. |
| `mark-signal.svg` | Root splits into two children; one endpoint has a larger ring. | Adds an attention focal point to the tree without a play/send/control icon. | More detail at tray size; ring is structural rather than a live status indicator. |

`wordmark.svg` pairs the recommended fork with literal `Branchglance` text. This is a layout variant, not a fourth mark. Font fallback is `Segoe UI, sans-serif`; after naming approval, remeasure the actual name before outlining or exporting. No final typography is implied.

## Visual use

- Transparent marks for a charcoal background (`#101116`); primary violet `#B9AEFF`, supporting blue `#89B6FF`. No blurred glow is required for legibility at 16 px.
- Keep at least 2 px clear space around a 16 px mark. Do not shrink the wordmark to tray size; use a mark alone.
- For a single-color native tray asset, make all strokes/fills one high-contrast color while retaining the geometry. No monochrome export or ICO is included in this draft scope.
- Reserve green for the centered in-widget clock and yellow for real waiting states. Brand marks do not simulate live activity or display status colors.
- In launch layouts, use deep charcoal/black and restrained purple-blue surroundings. Preserve the existing three-band interface; these concepts do not authorize a widget redesign.

Approval requested: choose one mark and final name, then inspect actual 16 px rendering before any tray replacement. These assets are not installed or published.
