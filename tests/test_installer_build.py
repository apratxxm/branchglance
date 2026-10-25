from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _build_module():
    spec = importlib.util.spec_from_file_location("branchglance_build_for_test", ROOT / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_build_has_one_final_version_default():
    module = _build_module()
    assert module.RELEASE_VERSION == "1.0.0"
    assert 'parser.add_argument("--version", default=RELEASE_VERSION)' in (ROOT / "packaging" / "build.py").read_text()


def test_all_executable_builds_get_branchglance_version_resources():
    module = _build_module()
    python_resource = module._pyinstaller_version_info("1.2.3")
    launcher_resource = module._launcher_assembly_info("1.2.3")
    for resource in (python_resource, launcher_resource):
        assert "Branchglance" in resource
        assert "1.2.3" in resource
    assert "StringStruct('ProductName', 'Branchglance')" in python_resource
    assert "StringStruct('ProductVersion', \"1.2.3\")" in python_resource
    assert "StringStruct('FileVersion', \"1.2.3.0\")" in python_resource
    assert '[assembly: AssemblyProduct("Branchglance")]' in launcher_resource
    assert '[assembly: AssemblyFileVersion("1.2.3.0")]' in launcher_resource
    assert '[assembly: AssemblyInformationalVersion("1.2.3")]' in launcher_resource
    assert "FixedFileInfo(filevers=(1, 2, 3, 0)" in python_resource


def test_signpath_finalizer_rehashes_signed_release_binaries(tmp_path):
    from scripts.branchglance_installer.core import verify_manifest, write_manifest, write_stamps
    spec = importlib.util.spec_from_file_location("branchglance_signpath_finalize_test",
                                                   ROOT / "packaging" / "signpath_finalize.py")
    finalizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(finalizer)
    components = tmp_path / "components"
    release = components / "releases" / "1.2.3+abc123"
    release.mkdir(parents=True)
    app_exe = release / "BranchglanceApp.exe"
    app_exe.write_bytes(b"unsigned")
    (release / "BranchglanceHook.exe").write_bytes(b"hook unsigned")
    write_manifest(release, "1.2.3", "abc123")
    write_stamps(release)
    app_exe.write_bytes(b"signed by SignPath")

    assert finalizer.finalize_signed_components(components) == [str(release)]
    manifest = verify_manifest(release, expected_version="1.2.3+abc123")
    assert manifest["files"]["BranchglanceApp.exe"] != __import__("hashlib").sha256(b"unsigned").hexdigest()
    assert "BranchglanceApp.exe" in manifest["stamps"]


def test_signpath_configuration_only_signs_named_branchglance_executables():
    import xml.etree.ElementTree as ET
    config = ET.parse(ROOT / ".signpath" / "artifact-configuration.xml").getroot()
    namespace = "{http://signpath.io/artifact-configuration/v1}"
    executable_rules = config.findall(f".//{namespace}pe-file")
    paths = {rule.attrib["path"] for rule in executable_rules}
    assert paths == {
        "Branchglance.exe", "BranchglanceUninstall.exe", "BranchglanceSetup.exe",
        "releases/*/BranchglanceApp.exe", "releases/*/BranchglanceHook.exe",
    }
    assert all(rule.attrib["product-name"] == "Branchglance" for rule in executable_rules)
    assert all(rule.attrib["product-version"] == "${version}" for rule in executable_rules)
    assert all(rule.find(f"{namespace}authenticode-sign") is not None for rule in executable_rules)


def test_build_entry_dispatches_claude_hook_by_module_name_to_stub(tmp_path):
    module = _build_module()
    source = tmp_path / "source"
    package = source / "token_tracker"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "widget.py").write_text("def main(): raise AssertionError('widget path selected')\n")
    # Lead contract: the entry calls token_tracker.claude_hook.run() (never main()).
    (package / "claude_hook.py").write_text(
        "import json, os, sys\n"
        "def main(*a): raise AssertionError('main must not be called')\n"
        "def run():\n"
        " open(os.environ['HOOK_RESULT'], 'w').write(json.dumps([sys.argv[2:], sys.stdin.read()]))\n")
    entry = source / "branchglance_entry.py"
    entry.write_text(module._entry_source(), encoding="utf-8")
    marker = tmp_path / "hook-result.json"
    env = os.environ.copy()
    env["HOOK_RESULT"] = str(marker)
    result = subprocess.run([sys.executable, str(entry), "--claude-hook", "event.json", "--debug"],
                            cwd=source, env=env, input='{"e":1}', capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(marker.read_text()) == [["event.json", "--debug"], '{"e":1}']


def test_build_entry_hook_failure_or_missing_module_still_exits_zero(tmp_path):
    module = _build_module()
    source = tmp_path / "source"
    (source / "token_tracker").mkdir(parents=True)
    (source / "token_tracker" / "__init__.py").write_text("")
    (source / "token_tracker" / "widget.py").write_text("raise AssertionError('widget imported')\n")
    entry = source / "branchglance_entry.py"
    entry.write_text(module._entry_source(), encoding="utf-8")
    missing = subprocess.run([sys.executable, str(entry), "--claude-hook"], cwd=source, input="",
                             capture_output=True, text=True, timeout=10)
    assert missing.returncode == 0 and "hook unavailable" in missing.stderr
    (source / "token_tracker" / "claude_hook.py").write_text("def run(): raise RuntimeError('boom')\n")
    failing = subprocess.run([sys.executable, str(entry), "--claude-hook"], cwd=source, input="",
                             capture_output=True, text=True, timeout=10)
    assert failing.returncode == 0 and "boom" in failing.stderr


def test_build_entry_self_test_writes_report_without_importing_widget(tmp_path):
    module = _build_module()
    source = tmp_path / "source"
    (source / "token_tracker").mkdir(parents=True)
    (source / "token_tracker" / "__init__.py").write_text("")
    (source / "token_tracker" / "widget.py").write_text("raise AssertionError('widget imported')\n")
    (source / "pythonnet.py").write_text("def load(*a): raise OSError('no runtime here')\n")  # shadows the real one
    entry = source / "branchglance_entry.py"
    entry.write_text(module._entry_source(), encoding="utf-8")
    report = tmp_path / "self-test.json"
    result = subprocess.run([sys.executable, str(entry), "--self-test", str(report)], cwd=source,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is False and "no runtime here" in payload["error"]


def test_release_build_refuses_a_source_checkout_output_path(tmp_path):
    module = _build_module()
    with pytest.raises(RuntimeError, match="outside the source checkout"):
        module.build(ROOT, "1.0.0", "probe")


def test_release_build_rejects_nonversioned_release_names(tmp_path):
    from branchglance_installer.core import SafetyError, validate_version
    for name in ("../escape", "1.0", "1.0.0+bad/path", "1.0.0+.."):
        with pytest.raises(SafetyError):
            validate_version(name)
