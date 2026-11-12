"""Release version wiring and changelog consistency."""
import importlib.util
import re
from pathlib import Path

from token_tracker._version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_release_version_wiring():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", __version__)
    spec = importlib.util.spec_from_file_location("release_build_version_test", ROOT / "packaging/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert build.RELEASE_VERSION == __version__
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "Get-Content token_tracker/_version.py" in workflow
    assert "__version__\\s*=" in workflow
    assert "$Matches.version -ne $version" in workflow
    mac_build = (ROOT / "macos/build.sh").read_text(encoding="utf-8")
    assert "../token_tracker/_version.py" in mac_build
    assert "RELEASE_VERSION" not in mac_build


def _semver_key(text):
    core, _, pre = text.partition("-")
    # A prerelease sorts before its release (0.2.0-rc.1 < 0.2.0).
    return tuple(int(part) for part in core.split(".")), pre == "", pre


def test_changelog_matches_version_between_and_at_releases():
    # The release workflow requires an exact match when tagging. Between releases the
    # version may lead the newest released entry, but only with an Unreleased section.
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    pattern = re.compile(r"^##\s+\[?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\]?(?:\s|$)")
    released = next((match.group(1) for line in lines if (match := pattern.match(line))), None)
    assert released is not None
    if released != __version__:
        assert _semver_key(__version__) > _semver_key(released)
        first = next(line for line in lines if line.startswith("## "))
        assert re.match(r"^##\s+\[?Unreleased\]?(?:\s|$)", first)
