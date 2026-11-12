"""Run the repository's Python, UI and lint checks."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
ENV = os.environ.copy()
for name in ("TOKEN_TRACKER_LEDGER", "TOKEN_TRACKER_CLAUDE_SEND"):
    ENV.pop(name, None)


def run(label, command):
    print(f"\n== {label} ==", flush=True)
    try:
        result = subprocess.run(command, cwd=ROOT, env=ENV, check=False)
        return result.returncode == 0
    except OSError as exc:
        print(f"Could not run {label}: {exc}", flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("python", "node", "lint"))
    args = parser.parse_args()
    results = []
    if args.only in (None, "python"):
        results.append(("Python", run("Python", [sys.executable, "-m", "pytest", "-q"])))
    if args.only in (None, "node"):
        files = sorted((ROOT / "tests").glob("*.mjs"))
        if not files:
            results.append(("Node tests (none found)", False))
        for path in files:
            label = f"Node: {path.name}"
            results.append((label, run(label, ["node", str(path)])))
    if args.only in (None, "lint"):
        results.append(("Ruff", run("Ruff", [sys.executable, "-m", "ruff", "check", "."])))
        npx = "npx.cmd" if os.name == "nt" else "npx"
        results.append(("ESLint", run("ESLint", [npx, "--yes", "eslint@9.39.1", "token_tracker/static/*.js"])))
    print("\nCheck summary:")
    for label, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'} {label}")
    return 0 if results and all(passed for _, passed in results) else 1


if __name__ == "__main__":
    sys.exit(main())
