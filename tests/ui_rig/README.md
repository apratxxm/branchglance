# Real-browser widget rig

Install `requirements-token-tracker.txt` and `requirements-dev.txt`. On Windows the rig uses installed Microsoft Edge; on macOS and Linux run `python -m playwright install chromium` first. Run:

```powershell
$env:TOKEN_TRACKER_UI_RIG = '1'
python -B -m pytest tests/ui_rig -q -p no:cacheprovider
```

Without the opt-in, tests are collected but skipped before browser startup. The server binds an ephemeral loopback port and uses fictional fixtures; external requests are blocked. Update UI selectors in `ui_selectors.py` when the widget markup changes. Native host calls are simulated, so these tests do not establish native WebView behavior.
