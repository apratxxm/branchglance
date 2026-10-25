"""Refresh release hashes after SignPath signs the binaries in a release tree."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.branchglance_installer.core import (  # noqa: E402
    SafetyError, read_json, validate_version, verify_manifest, write_manifest, write_stamps,
)


def finalize_signed_components(components: Path) -> list[str]:
    """Rebuild manifest hashes/stamps for signed release PE files, then verify."""
    components = Path(components).resolve()
    releases = components / "releases"
    if not releases.is_dir():
        raise SafetyError("Signed components are missing the releases folder")
    release_dirs = sorted(path for path in releases.iterdir() if path.is_dir())
    if len(release_dirs) != 1:
        raise SafetyError("Expected exactly one signed release directory")
    release = release_dirs[0]
    manifest = read_json(release / "release-manifest.json")
    full_version = validate_version(manifest.get("version"))
    version, build_id = full_version.split("+", 1)
    write_manifest(release, version, build_id)
    write_stamps(release)
    verify_manifest(release, expected_version=full_version)
    return [str(release)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps({"refreshed": finalize_signed_components(args.components)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
