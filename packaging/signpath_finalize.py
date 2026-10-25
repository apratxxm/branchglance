"""Inventory, authenticate, and finalize PE files returned by SignPath."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.branchglance_installer.core import (  # noqa: E402
    SafetyError, atomic_json, is_reparse, read_json, sha256_file, validate_version, verify_manifest,
    write_manifest, write_stamps,
)

INVENTORY_NAME = "signpath-inventory.json"
INVENTORY_SCHEMA = 1
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
EXPECTED_SIGNER = "SignPath Foundation"
PRODUCT_NAME = "Branchglance"

SignatureInspector = Callable[[Path], Mapping[str, str]]


def _component_targets(versioned: str) -> list[str]:
    return [
        "Branchglance.exe",
        "BranchglanceUninstall.exe",
        f"releases/{versioned}/BranchglanceApp.exe",
        f"releases/{versioned}/BranchglanceHook.exe",
    ]


def _valid_relative_posix_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (bool(value) and not path.is_absolute() and ".." not in path.parts
            and "\\" not in value and path.as_posix() == value)


def _plain_file_inventory(root: Path, *, exclude_inventory: bool = False) -> dict[str, str]:
    root = Path(root).absolute()
    if is_reparse(root) or not root.is_dir():
        raise SafetyError("SignPath artifact root is missing or redirected")
    result: dict[str, str] = {}
    for directory, dirs, names in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            path = base / name
            if is_reparse(path) or not path.is_dir():
                raise SafetyError(f"SignPath artifact contains a redirected directory: {path.relative_to(root)}")
        for name in names:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if exclude_inventory and relative == INVENTORY_NAME:
                continue
            if is_reparse(path) or not path.is_file():
                raise SafetyError(f"SignPath artifact contains a redirected or non-file entry: {relative}")
            result[relative] = sha256_file(path)
    return dict(sorted(result.items()))


def create_signing_inventory(root: Path, version: str, build_id: str, targets: list[str], *,
                             inventory_path: Path | None = None) -> str:
    """Persist the pre-sign hashes; the workflow separately retains this file's hash."""
    versioned = validate_version(f"{version}+{build_id}")
    root = Path(root).absolute()
    if (not isinstance(targets, list) or not targets
            or any(not isinstance(item, str) or not _valid_relative_posix_path(item) for item in targets)
            or len(set(targets)) != len(targets)):
        raise SafetyError("Signing target list is invalid")
    inventory_path = Path(inventory_path).absolute() if inventory_path is not None else root / INVENTORY_NAME
    try:
        relative_inventory = inventory_path.relative_to(root)
    except ValueError:
        relative_inventory = None
    if relative_inventory is not None and relative_inventory.as_posix() != INVENTORY_NAME:
        raise SafetyError("An inventory inside the artifact root must use its reserved root filename")
    if os.path.lexists(inventory_path):
        raise SafetyError("A signing inventory already exists")
    files = _plain_file_inventory(root)
    if not set(targets).issubset(files):
        missing = sorted(set(targets) - files.keys())
        raise SafetyError("Required signing targets are missing: " + ", ".join(missing))
    executables = {name for name in files if name.casefold().endswith(".exe")}
    executable_targets = {name for name in targets if name.casefold().endswith(".exe")}
    if executables != executable_targets:
        raise SafetyError("The executable inventory does not exactly match the required signing targets")
    payload = {"schemaVersion": INVENTORY_SCHEMA, "product": PRODUCT_NAME,
               "version": versioned, "targets": sorted(targets), "files": files}
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(inventory_path, payload)
    return sha256_file(inventory_path)


def create_component_inventory(components: Path, version: str, build_id: str, *,
                               inventory_path: Path | None = None) -> str:
    versioned = validate_version(f"{version}+{build_id}")
    return create_signing_inventory(components, version, build_id, _component_targets(versioned),
                                    inventory_path=inventory_path)


def create_setup_inventory(setup_directory: Path, version: str, build_id: str, *,
                           inventory_path: Path | None = None) -> str:
    return create_signing_inventory(setup_directory, version, build_id, ["BranchglanceSetup.exe"],
                                    inventory_path=inventory_path)


def inspect_authenticode(path: Path) -> Mapping[str, str]:
    """Use Windows' Authenticode and PE resource readers; no third-party verifier."""
    if os.name != "nt":
        raise SafetyError("Authenticode verification requires Windows")
    script = r'''
$path = $env:BRANCHGLANCE_VERIFY_FILE
$signature = Get-AuthenticodeSignature -LiteralPath $path
$signer = ""
if ($signature.SignerCertificate) {
  $signer = $signature.SignerCertificate.GetNameInfo(
    [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
}
$version = [Diagnostics.FileVersionInfo]::GetVersionInfo($path)
[pscustomobject]@{
  status = [string]$signature.Status
  signer = [string]$signer
  productName = [string]$version.ProductName
  productVersion = [string]$version.ProductVersion
} | ConvertTo-Json -Compress
'''
    environment = os.environ.copy()
    environment["BRANCHGLANCE_VERIFY_FILE"] = str(Path(path).resolve())
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            env=environment, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SafetyError(f"Could not inspect Authenticode metadata ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise SafetyError("Authenticode inspection failed")
    try:
        value = json.loads(result.stdout.strip())
    except ValueError:
        raise SafetyError("Authenticode inspection returned invalid metadata") from None
    if not isinstance(value, dict):
        raise SafetyError("Authenticode inspection returned invalid metadata")
    return value


def _verify_signed_target(path: Path, version: str, inspector: SignatureInspector) -> None:
    try:
        metadata = inspector(path)
    except SafetyError:
        raise
    except Exception as exc:
        raise SafetyError(f"Authenticode inspection failed for {path.name} ({type(exc).__name__})") from None
    if not isinstance(metadata, Mapping):
        raise SafetyError(f"Authenticode metadata is invalid for {path.name}")
    if metadata.get("status") != "Valid":
        raise SafetyError(f"Authenticode signature is missing or invalid for {path.name}")
    if str(metadata.get("signer", "")).strip().casefold() != EXPECTED_SIGNER.casefold():
        raise SafetyError(f"Unexpected Authenticode signer for {path.name}")
    if metadata.get("productName") != PRODUCT_NAME:
        raise SafetyError(f"Unexpected ProductName metadata for {path.name}")
    if metadata.get("productVersion") != version:
        raise SafetyError(f"Unexpected ProductVersion metadata for {path.name}")


def _load_and_validate_inventory(root: Path, inventory_path: Path | None, expected_inventory_sha256: str,
                                 expected_targets: list[str] | None = None,
                                 expected_version: str | None = None) -> tuple[dict, dict[str, str]]:
    inventory_path = Path(inventory_path).absolute() if inventory_path is not None else Path(root) / INVENTORY_NAME
    if not re.fullmatch(r"[0-9a-f]{64}", expected_inventory_sha256 or ""):
        raise SafetyError("The expected pre-sign inventory hash is invalid")
    if is_reparse(inventory_path) or not inventory_path.is_file():
        raise SafetyError("The pre-sign inventory is missing or redirected")
    if inventory_path.stat().st_size > MAX_INVENTORY_BYTES:
        raise SafetyError("The pre-sign inventory is too large")
    if sha256_file(inventory_path) != expected_inventory_sha256:
        raise SafetyError("The original pre-sign inventory changed")
    inventory = read_json(inventory_path)
    if inventory.get("schemaVersion") != INVENTORY_SCHEMA or inventory.get("product") != PRODUCT_NAME:
        raise SafetyError("The pre-sign inventory schema or product is invalid")
    versioned = validate_version(inventory.get("version"))
    if expected_version is not None and versioned != validate_version(expected_version):
        raise SafetyError("The pre-sign inventory version differs from the selected release")
    targets = inventory.get("targets")
    files = inventory.get("files")
    if (not isinstance(targets, list) or not targets
            or any(not isinstance(item, str) or not _valid_relative_posix_path(item) for item in targets)
            or targets != sorted(set(targets))
            or not isinstance(files, dict) or not files):
        raise SafetyError("The pre-sign inventory is malformed")
    if expected_targets is not None and targets != sorted(expected_targets):
        raise SafetyError("The recorded signing targets do not match the required executable inventory")
    for name, digest in files.items():
        if (not isinstance(name, str) or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise SafetyError("The pre-sign file hash inventory is malformed")
    if not set(targets).issubset(files):
        raise SafetyError("The pre-sign inventory omits a required signing target")
    executables = {name for name in files if name.casefold().endswith(".exe")}
    executable_targets = {name for name in targets if name.casefold().endswith(".exe")}
    if executables != executable_targets:
        raise SafetyError("The recorded executable inventory differs from the required signing targets")
    return inventory, files


def validate_signed_inventory(root: Path, expected_inventory_sha256: str, *,
                               inventory_path: Path | None = None,
                               expected_targets: list[str] | None = None,
                               expected_version: str | None = None,
                               signature_inspector: SignatureInspector = inspect_authenticode) -> dict:
    """Reject missing, added, changed, unsigned, or misidentified signed files."""
    root = Path(root).absolute()
    inventory, original_files = _load_and_validate_inventory(
        root, inventory_path, expected_inventory_sha256, expected_targets, expected_version)
    inventory_inside_root = Path(inventory_path or (root / INVENTORY_NAME)).absolute() == root / INVENTORY_NAME
    actual_files = _plain_file_inventory(root, exclude_inventory=inventory_inside_root)
    if set(actual_files) != set(original_files):
        added = sorted(set(actual_files) - set(original_files))
        missing = sorted(set(original_files) - set(actual_files))
        raise SafetyError(f"Signed artifact inventory changed (added={added}, missing={missing})")
    version = inventory["version"].split("+", 1)[0]
    targets = set(inventory["targets"])
    for relative, original_hash in original_files.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        if relative in targets:
            _verify_signed_target(path, version, signature_inspector)
        elif actual_files[relative] != original_hash:
            raise SafetyError(f"An untargeted file changed after signing: {relative}")
    return inventory


def finalize_signed_components(components: Path, expected_inventory_sha256: str, *,
                               expected_version: str,
                               inventory_path: Path | None = None,
                               signature_inspector: SignatureInspector = inspect_authenticode) -> list[str]:
    components = Path(components).absolute()
    full_version = validate_version(expected_version)
    inventory = validate_signed_inventory(components, expected_inventory_sha256,
                                          inventory_path=inventory_path,
                                          expected_targets=_component_targets(full_version),
                                          expected_version=full_version,
                                          signature_inspector=signature_inspector)
    version, build_id = full_version.split("+", 1)
    release = components / "releases" / full_version
    manifest = read_json(release / "release-manifest.json")
    if manifest.get("version") != full_version or manifest.get("product") != PRODUCT_NAME:
        raise SafetyError("The signed release manifest does not match the pre-sign inventory")
    inventory_file = Path(inventory_path).absolute() if inventory_path is not None else components / INVENTORY_NAME
    if inventory_file == components / INVENTORY_NAME:
        inventory_file.unlink()
    write_manifest(release, version, build_id)
    write_stamps(release)
    verify_manifest(release, expected_version=full_version)
    return [str(release)]


def finalize_setup(setup_directory: Path, expected_inventory_sha256: str, *,
                   expected_version: str,
                   inventory_path: Path | None = None,
                   signature_inspector: SignatureInspector = inspect_authenticode) -> Path:
    setup_directory = Path(setup_directory).absolute()
    expected = ["BranchglanceSetup.exe"]
    validate_signed_inventory(setup_directory, expected_inventory_sha256,
                              inventory_path=inventory_path,
                              expected_targets=expected,
                              expected_version=validate_version(expected_version),
                              signature_inspector=signature_inspector)
    inventory_file = Path(inventory_path).absolute() if inventory_path is not None else setup_directory / INVENTORY_NAME
    if inventory_file == setup_directory / INVENTORY_NAME:
        inventory_file.unlink()
    return setup_directory / "BranchglanceSetup.exe"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    component_inventory = commands.add_parser("create-component-inventory")
    component_inventory.add_argument("--components", type=Path, required=True)
    component_inventory.add_argument("--version", required=True)
    component_inventory.add_argument("--build-id", required=True)
    component_inventory.add_argument("--inventory-path", type=Path)
    setup_inventory = commands.add_parser("create-setup-inventory")
    setup_inventory.add_argument("--setup-directory", type=Path, required=True)
    setup_inventory.add_argument("--version", required=True)
    setup_inventory.add_argument("--build-id", required=True)
    setup_inventory.add_argument("--inventory-path", type=Path)
    finalize_components = commands.add_parser("finalize-components")
    finalize_components.add_argument("--components", type=Path, required=True)
    finalize_components.add_argument("--inventory-sha256", required=True)
    finalize_components.add_argument("--versioned", required=True)
    finalize_components.add_argument("--inventory-path", type=Path)
    finalize_setup_command = commands.add_parser("finalize-setup")
    finalize_setup_command.add_argument("--setup-directory", type=Path, required=True)
    finalize_setup_command.add_argument("--inventory-sha256", required=True)
    finalize_setup_command.add_argument("--versioned", required=True)
    finalize_setup_command.add_argument("--inventory-path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "create-component-inventory":
        digest = create_component_inventory(args.components, args.version, args.build_id,
                                            inventory_path=args.inventory_path)
        print(json.dumps({"inventorySha256": digest}))
    elif args.command == "create-setup-inventory":
        digest = create_setup_inventory(args.setup_directory, args.version, args.build_id,
                                        inventory_path=args.inventory_path)
        print(json.dumps({"inventorySha256": digest}))
    elif args.command == "finalize-components":
        refreshed = finalize_signed_components(args.components, args.inventory_sha256,
                                               expected_version=args.versioned,
                                               inventory_path=args.inventory_path)
        print(json.dumps({"refreshed": refreshed}, indent=2))
    else:
        print(json.dumps({"setup": str(finalize_setup(args.setup_directory, args.inventory_sha256,
                                                       expected_version=args.versioned,
                                                       inventory_path=args.inventory_path))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
