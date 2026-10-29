# Screenshot renderer

From the repository root, run `python scripts/screenshots/render.py` on Windows with Microsoft Edge installed in its usual location. The script starts an ephemeral loopback server, renders at the widget's native logical sizes with device scale factor 2, and writes the three PNGs to `docs/release/screenshots/`. It uses a temporary Edge profile and stops the server when finished.

All chats, workspaces, usage and limits are synthetic. No real accounts or local tracking data are read.
