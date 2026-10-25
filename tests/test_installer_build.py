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


def _signpath_finalize_module():
    spec = importlib.util.spec_from_file_location(
        "branchglance_signpath_finalize_for_test", ROOT / "packaging" / "signpath_finalize.py")
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


def _signpath_component_fixture(tmp_path, *, inventory_path=None):
    from scripts.branchglance_installer.core import write_manifest, write_stamps
    create_component_inventory = _signpath_finalize_module().create_component_inventory
    components = tmp_path / "components"
    release = components / "releases" / "1.2.3+abc123"
    release.mkdir(parents=True)
    (components / "Branchglance.exe").write_bytes(b"unsigned launcher")
    (components / "BranchglanceUninstall.exe").write_bytes(b"unsigned uninstaller")
    (release / "BranchglanceApp.exe").write_bytes(b"unsigned app")
    (release / "BranchglanceHook.exe").write_bytes(b"unsigned hook")
    (release / "WebView2Loader.dll").write_bytes(b"third-party dll")
    write_manifest(release, "1.2.3", "abc123")
    write_stamps(release)
    digest = create_component_inventory(components, "1.2.3", "abc123", inventory_path=inventory_path)
    return components, release, digest


def _valid_signature(_path):
    return {"status": "Valid", "signer": "SignPath Foundation",
            "productName": "Branchglance", "productVersion": "1.2.3"}


def test_signpath_finalizer_rehashes_signed_release_binaries(tmp_path):
    from scripts.branchglance_installer.core import verify_manifest
    finalize_signed_components = _signpath_finalize_module().finalize_signed_components
    components, release, inventory_hash = _signpath_component_fixture(tmp_path)
    for name in ("Branchglance.exe", "BranchglanceUninstall.exe"):
        path = components / name
        path.write_bytes(path.read_bytes() + b";valid signature")
    for name in ("BranchglanceApp.exe", "BranchglanceHook.exe"):
        path = release / name
        path.write_bytes(path.read_bytes() + b";valid signature")

    assert finalize_signed_components(components, inventory_hash, expected_version="1.2.3+abc123",
                                      signature_inspector=_valid_signature) == [str(release)]
    manifest = verify_manifest(release, expected_version="1.2.3+abc123")
    assert manifest["files"]["BranchglanceApp.exe"] != __import__("hashlib").sha256(b"unsigned app").hexdigest()
    assert "BranchglanceApp.exe" in manifest["stamps"]
    assert not (components / "signpath-inventory.json").exists()


def test_signpath_finalizer_accepts_inventory_sidecar_outside_signed_archive(tmp_path):
    from scripts.branchglance_installer.core import verify_manifest
    finalize_signed_components = _signpath_finalize_module().finalize_signed_components
    sidecar = tmp_path / "inventory-artifact" / "signpath-inventory.json"
    components, release, inventory_hash = _signpath_component_fixture(tmp_path, inventory_path=sidecar)
    assert not (components / "signpath-inventory.json").exists()
    for name in ("Branchglance.exe", "BranchglanceUninstall.exe"):
        path = components / name
        path.write_bytes(path.read_bytes() + b";signature")
    for name in ("BranchglanceApp.exe", "BranchglanceHook.exe"):
        path = release / name
        path.write_bytes(path.read_bytes() + b";signature")

    finalize_signed_components(components, inventory_hash, expected_version="1.2.3+abc123",
                               inventory_path=sidecar, signature_inspector=_valid_signature)
    assert sidecar.exists()
    assert "BranchglanceApp.exe" in verify_manifest(release)["files"]


def test_signpath_rejects_unsigned_target_executable(tmp_path):
    from scripts.branchglance_installer.core import SafetyError
    finalize_signed_components = _signpath_finalize_module().finalize_signed_components
    components, _release, inventory_hash = _signpath_component_fixture(tmp_path)

    def unsigned_exe(path):
        result = _valid_signature(path)
        if path.name == "Branchglance.exe":
            result = {**result, "status": "NotSigned", "signer": ""}
        return result

    with pytest.raises(SafetyError, match="missing or invalid"):
        finalize_signed_components(components, inventory_hash, expected_version="1.2.3+abc123",
                                  signature_inspector=unsigned_exe)


def test_signpath_rejects_changed_untargeted_dll(tmp_path):
    from scripts.branchglance_installer.core import SafetyError
    finalize_signed_components = _signpath_finalize_module().finalize_signed_components
    components, release, inventory_hash = _signpath_component_fixture(tmp_path)
    dll = release / "WebView2Loader.dll"
    dll.write_bytes(b"changed after signing request")
    original_manifest = (release / "release-manifest.json").read_bytes()

    with pytest.raises(SafetyError, match="untargeted file changed.*WebView2Loader.dll"):
        finalize_signed_components(components, inventory_hash, expected_version="1.2.3+abc123",
                                  signature_inspector=_valid_signature)
    assert (release / "release-manifest.json").read_bytes() == original_manifest


def test_signpath_rejects_wrong_signer(tmp_path):
    from scripts.branchglance_installer.core import SafetyError
    finalize_signed_components = _signpath_finalize_module().finalize_signed_components
    components, _release, inventory_hash = _signpath_component_fixture(tmp_path)

    def wrong_signer(path):
        return {**_valid_signature(path), "signer": "Another Publisher"}

    with pytest.raises(SafetyError, match="Unexpected Authenticode signer"):
        finalize_signed_components(components, inventory_hash, expected_version="1.2.3+abc123",
                                  signature_inspector=wrong_signer)


def test_signpath_rejects_missing_required_executable(tmp_path):
    from scripts.branchglance_installer.core import SafetyError
    finalize_signed_components = _signpath_finalize_module().finalize_signed_components
    components, release, inventory_hash = _signpath_component_fixture(tmp_path)
    (release / "BranchglanceHook.exe").unlink()

    with pytest.raises(SafetyError, match="inventory changed.*missing=.*BranchglanceHook.exe"):
        finalize_signed_components(components, inventory_hash, expected_version="1.2.3+abc123",
                                  signature_inspector=_valid_signature)


def test_signpath_rejects_modified_pre_sign_inventory(tmp_path):
    from scripts.branchglance_installer.core import SafetyError
    finalize_signed_components = _signpath_finalize_module().finalize_signed_components
    components, _release, inventory_hash = _signpath_component_fixture(tmp_path)
    inventory_path = components / "signpath-inventory.json"
    inventory_path.write_text("{}", encoding="utf-8")

    with pytest.raises(SafetyError, match="original pre-sign inventory changed"):
        finalize_signed_components(components, inventory_hash, expected_version="1.2.3+abc123",
                                  signature_inspector=_valid_signature)


def test_signpath_validates_final_setup_metadata_before_removing_inventory(tmp_path):
    finalizer = _signpath_finalize_module()
    create_setup_inventory, finalize_setup = finalizer.create_setup_inventory, finalizer.finalize_setup
    setup_dir = tmp_path / "setup"
    setup_dir.mkdir()
    (setup_dir / "BranchglanceSetup.exe").write_bytes(b"unsigned setup")
    digest = create_setup_inventory(setup_dir, "1.2.3", "abc123")
    exe = setup_dir / "BranchglanceSetup.exe"
    exe.write_bytes(b"signed setup")

    assert finalize_setup(setup_dir, digest, expected_version="1.2.3+abc123",
                          signature_inspector=_valid_signature) == exe
    assert exe.exists() and not (setup_dir / "signpath-inventory.json").exists()


def test_signpath_configuration_only_signs_named_branchglance_executables():
    import xml.etree.ElementTree as ET
    config = ET.parse(ROOT / ".signpath" / "artifact-configuration.xml").getroot()
    namespace = "{http://signpath.io/artifact-configuration/v1}"
    executable_rules = config.findall(f".//{namespace}pe-file")
    paths = {rule.attrib["path"] for rule in executable_rules}
    assert paths == {"Branchglance.exe", "BranchglanceUninstall.exe",
                     "releases/*/BranchglanceApp.exe", "releases/*/BranchglanceHook.exe"}
    assert all(rule.attrib["product-name"] == "Branchglance" for rule in executable_rules)
    assert all(rule.attrib["product-version"] == "${version}" for rule in executable_rules)
    assert all(rule.find(f"{namespace}authenticode-sign") is not None for rule in executable_rules)
    assert all("min-matches" not in rule.attrib for rule in executable_rules)
    setup = ET.parse(ROOT / ".signpath" / "setup-artifact-configuration.xml").getroot()
    setup_rules = setup.findall(f".//{namespace}pe-file")
    assert len(setup_rules) == 1 and setup_rules[0].attrib["path"] == "BranchglanceSetup.exe"
    assert "min-matches" not in setup_rules[0].attrib


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
