# Branchglance site

Static landing page. No build step: `index.html`, `styles.css`, `main.js`, plus local fonts and brand assets.

Preview locally:

```sh
python3 -m http.server -d site 8000
```

Colours, type and components are taken from the widget's own styles in `token_tracker/static/runway*.css`, so the page stays visually tied to the app. The hero tree is live HTML driven by a short script in `main.js`, not a screenshot.

Fonts: Inter and Red Hat Mono, SIL OFL 1.1 (licences in `fonts/`).
