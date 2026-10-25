"""Inventory, authenticate, and finalize PE files returned by SignPath."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
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

_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_SPC_INDIRECT_DATA = "1.3.6.1.4.1.311.2.1.4"
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise SafetyError("PE file is truncated")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise SafetyError("PE file is truncated")
    return struct.unpack_from("<I", data, offset)[0]


def _pe_exclusion_ranges(data: bytes) -> tuple[list[tuple[int, int]], tuple[int, int]]:
    """Return the PE checksum, security-directory, and certificate exclusions."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise SafetyError("Signing target is not a valid PE image")
    pe_offset = _u32(data, 0x3C)
    if pe_offset + 24 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise SafetyError("PE signature is missing or truncated")
    coff = pe_offset + 4
    section_count = _u16(data, coff + 2)
    optional_size = _u16(data, coff + 16)
    optional = coff + 20
    optional_end = optional + optional_size
    if section_count == 0 or optional_end > len(data) or optional_size < 68:
        raise SafetyError("PE headers are invalid or truncated")
    magic = _u16(data, optional)
    if magic == 0x10B:
        directory_count_offset, directories_offset = 92, 96
    elif magic == 0x20B:
        directory_count_offset, directories_offset = 108, 112
    else:
        raise SafetyError("PE optional-header format is unsupported")
    if optional_size < directories_offset + (5 * 8) or _u32(data, optional + directory_count_offset) <= 4:
        raise SafetyError("PE security directory entry is missing")
    size_of_headers = _u32(data, optional + 60)
    checksum_range = (optional + 64, optional + 68)
    security_entry = optional + directories_offset + (4 * 8)
    cert_offset, cert_size = struct.unpack_from("<II", data, security_entry)
    section_table = optional_end
    section_table_end = section_table + (section_count * 40)
    if section_table_end > len(data) or not section_table_end <= size_of_headers <= len(data):
        raise SafetyError("PE section table or SizeOfHeaders is invalid")
    max_section_end = size_of_headers
    for index in range(section_count):
        section = section_table + (index * 40)
        raw_size = _u32(data, section + 16)
        raw_offset = _u32(data, section + 20)
        if raw_size:
            if raw_offset < size_of_headers or raw_offset + raw_size > len(data):
                raise SafetyError("PE section data is outside the file")
            max_section_end = max(max_section_end, raw_offset + raw_size)
    exclusions = [checksum_range, (security_entry, security_entry + 8)]
    if (cert_offset == 0) != (cert_size == 0):
        raise SafetyError("PE certificate table directory is malformed")
    if cert_size:
        cert_end = cert_offset + cert_size
        if cert_offset % 8 or cert_offset < max_section_end or cert_end > len(data):
            raise SafetyError("PE certificate table points outside the file")
        exclusions.append((cert_offset, cert_end))
    exclusions.sort()
    previous_end = 0
    for start, end in exclusions:
        if start < previous_end or end < start or end > len(data):
            raise SafetyError("PE Authenticode exclusion ranges overlap or exceed the file")
        previous_end = end
    return exclusions, (cert_offset, cert_size)


def _hash_ranges(data: bytes, ranges: list[tuple[int, int]],
                 exclusions: list[tuple[int, int]]) -> str:
    digest = hashlib.sha256()
    for range_start, range_end in ranges:
        offset = range_start
        for start, end in exclusions:
            if end <= range_start or start >= range_end:
                continue
            start, end = max(start, range_start), min(end, range_end)
            if start > offset:
                digest.update(data[offset:start])
            offset = max(offset, end)
        if offset < range_end:
            digest.update(data[offset:range_end])
    return digest.hexdigest()


def authenticode_pe_digest_bytes(data: bytes) -> str:
    """Compute the standard PE image digest used in the embedded SPC data.

    PE headers and section contents are hashed (sections in ascending RVA
    order); data past the final section, including PyInstaller overlay data,
    is not part of the Authenticode image digest.
    """
    exclusions, _certificate = _pe_exclusion_ranges(data)
    pe_offset = _u32(data, 0x3C)
    coff = pe_offset + 4
    section_count = _u16(data, coff + 2)
    optional = coff + 20
    optional_end = optional + _u16(data, coff + 16)
    size_of_headers = _u32(data, optional + 60)
    sections = []
    for index in range(section_count):
        section = optional_end + index * 40
        virtual_address = _u32(data, section + 12)
        raw_size = _u32(data, section + 16)
        raw_offset = _u32(data, section + 20)
        if raw_size:
            sections.append((virtual_address, raw_offset, raw_offset + raw_size))
    sections.sort()
    ranges = [(0, size_of_headers), *((start, end) for _va, start, end in sections)]
    return _hash_ranges(data, ranges, exclusions)


def overlay_inclusive_pe_digest_bytes(data: bytes) -> str:
    """Hash the complete file, excluding only fields changed by signing."""
    exclusions, _certificate = _pe_exclusion_ranges(data)
    return _hash_ranges(data, [(0, len(data))], exclusions)


def authenticode_pe_digest(path: Path) -> str:
    try:
        return authenticode_pe_digest_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise SafetyError(f"Could not read PE image ({type(exc).__name__})") from None


def _der_tlv(data: bytes, offset: int, limit: int) -> tuple[int, int, int, int]:
    if offset < 0 or offset + 2 > limit:
        raise SafetyError("Authenticode certificate contains truncated ASN.1 data")
    tag = data[offset]
    if tag & 0x1F == 0x1F:
        raise SafetyError("Authenticode certificate uses unsupported ASN.1 tag encoding")
    first_length = data[offset + 1]
    cursor = offset + 2
    if first_length < 0x80:
        length = first_length
    else:
        length_bytes = first_length & 0x7F
        if length_bytes == 0 or length_bytes > 4 or cursor + length_bytes > limit:
            raise SafetyError("Authenticode certificate has invalid ASN.1 length")
        if data[cursor] == 0:
            raise SafetyError("Authenticode certificate has non-canonical ASN.1 length")
        length = int.from_bytes(data[cursor:cursor + length_bytes], "big")
        if length < 0x80:
            raise SafetyError("Authenticode certificate has non-canonical ASN.1 length")
        cursor += length_bytes
    end = cursor + length
    if end > limit:
        raise SafetyError("Authenticode certificate ASN.1 value exceeds its container")
    return tag, cursor, end, end


def _der_children(data: bytes, start: int, end: int) -> list[tuple[int, int, int, int]]:
    children = []
    cursor = start
    while cursor < end:
        child = _der_tlv(data, cursor, end)
        children.append(child)
        cursor = child[3]
    if cursor != end:
        raise SafetyError("Authenticode certificate ASN.1 container is malformed")
    return children


def _der_oid(data: bytes, start: int, end: int) -> str:
    if start >= end:
        raise SafetyError("Authenticode certificate has an empty object identifier")
    subidentifiers = []
    value = 0
    for byte in data[start:end]:
        value = (value << 7) | (byte & 0x7F)
        if byte & 0x80:
            continue
        subidentifiers.append(value)
        value = 0
    if data[end - 1] & 0x80:
        raise SafetyError("Authenticode certificate object identifier is truncated")
    first = subidentifiers.pop(0)
    first_arc = min(first // 40, 2)
    arcs = [first_arc, first - (first_arc * 40), *subidentifiers]
    return ".".join(str(arc) for arc in arcs)


def _parse_spc_image_digest(cms: bytes) -> tuple[str, bytes] | None:
    root = _der_tlv(cms, 0, len(cms))
    if root[0] != 0x30 or any(cms[root[3]:]):
        raise SafetyError("Authenticode CMS ContentInfo is malformed")
    content_info = _der_children(cms, root[1], root[2])
    if len(content_info) < 2 or content_info[0][0] != 0x06:
        return None
    if _der_oid(cms, content_info[0][1], content_info[0][2]) != _OID_SIGNED_DATA:
        return None
    if content_info[1][0] != 0xA0:
        raise SafetyError("Authenticode CMS SignedData wrapper is missing")
    signed_data_wrapper = _der_children(cms, content_info[1][1], content_info[1][2])
    if not signed_data_wrapper or signed_data_wrapper[0][0] != 0x30:
        raise SafetyError("Authenticode CMS SignedData is malformed")
    signed_fields = _der_children(cms, signed_data_wrapper[0][1], signed_data_wrapper[0][2])
    encap = None
    for field in signed_fields:
        if field[0] != 0x30:
            continue
        children = _der_children(cms, field[1], field[2])
        if children and children[0][0] == 0x06:
            oid = _der_oid(cms, children[0][1], children[0][2])
            if oid == _OID_SPC_INDIRECT_DATA:
                encap = children
                break
    if encap is None:
        return None
    if len(encap) < 2 or encap[1][0] != 0xA0:
        raise SafetyError("Authenticode SPC indirect content is missing")
    explicit_content = _der_children(cms, encap[1][1], encap[1][2])
    if len(explicit_content) != 1 or explicit_content[0][0] != 0x04:
        raise SafetyError("Authenticode SPC indirect content is not an OCTET STRING")
    spc_data = cms[explicit_content[0][1]:explicit_content[0][2]]
    spc_root = _der_tlv(spc_data, 0, len(spc_data))
    if spc_root[0] != 0x30 or any(spc_data[spc_root[3]:]):
        raise SafetyError("Authenticode SPC indirect data is malformed")
    spc_fields = _der_children(spc_data, spc_root[1], spc_root[2])
    if len(spc_fields) < 2 or spc_fields[1][0] != 0x30:
        raise SafetyError("Authenticode SPC DigestInfo is missing")
    digest_info = _der_children(spc_data, spc_fields[1][1], spc_fields[1][2])
    if (len(digest_info) != 2 or digest_info[0][0] != 0x30 or digest_info[1][0] != 0x04):
        raise SafetyError("Authenticode SPC DigestInfo is malformed")
    algorithm = _der_children(spc_data, digest_info[0][1], digest_info[0][2])
    if not algorithm or algorithm[0][0] != 0x06:
        raise SafetyError("Authenticode SPC digest algorithm is missing")
    oid = _der_oid(spc_data, algorithm[0][1], algorithm[0][2])
    digest_value = spc_data[digest_info[1][1]:digest_info[1][2]]
    return oid, digest_value


def _embedded_sha256_image_digests(data: bytes, certificate: tuple[int, int]) -> list[bytes]:
    cert_offset, cert_size = certificate
    if not cert_size:
        raise SafetyError("Signed PE image has no embedded certificate table")
    end = cert_offset + cert_size
    cursor = cert_offset
    digests = []
    while cursor < end:
        if cursor + 8 > end:
            raise SafetyError("PE certificate table has a truncated WIN_CERTIFICATE")
        length = _u32(data, cursor)
        revision = _u16(data, cursor + 4)
        cert_type = _u16(data, cursor + 6)
        if length < 8 or cursor + length > end:
            raise SafetyError("PE certificate table contains an invalid WIN_CERTIFICATE")
        aligned_length = (length + 7) & ~7
        if cursor + aligned_length > end:
            raise SafetyError("PE certificate table alignment exceeds its declared size")
        if cert_type == 0x0002 and revision in (0x0100, 0x0200):
            content = data[cursor + 8:cursor + length]
            parsed = _parse_spc_image_digest(content)
            if parsed is not None and parsed[0] == _OID_SHA256:
                if len(parsed[1]) != 32:
                    raise SafetyError("Embedded Authenticode SHA-256 digest has an invalid length")
                digests.append(parsed[1])
        cursor += aligned_length
    if cursor != end or not digests:
        raise SafetyError("PE certificate table has no SHA-256 Authenticode image digest")
    return digests


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
    authenticode_digests = {
        target: authenticode_pe_digest(root.joinpath(*PurePosixPath(target).parts))
        for target in sorted(targets)
    }
    overlay_digests = {
        target: overlay_inclusive_pe_digest_bytes(
            root.joinpath(*PurePosixPath(target).parts).read_bytes())
        for target in sorted(targets)
    }
    payload = {"schemaVersion": INVENTORY_SCHEMA, "product": PRODUCT_NAME,
               "version": versioned, "targets": sorted(targets), "files": files,
               "authenticodeDigests": authenticode_digests,
               "overlayDigests": overlay_digests}
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
    authenticode_digests = inventory.get("authenticodeDigests")
    overlay_digests = inventory.get("overlayDigests")
    if (not isinstance(targets, list) or not targets
            or any(not isinstance(item, str) or not _valid_relative_posix_path(item) for item in targets)
            or targets != sorted(set(targets))
            or not isinstance(files, dict) or not files
            or not isinstance(authenticode_digests, dict)
            or not isinstance(overlay_digests, dict)):
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
    if (set(authenticode_digests) != set(targets)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in authenticode_digests.values())):
        raise SafetyError("The pre-sign Authenticode digest inventory is malformed")
    if (set(overlay_digests) != set(targets)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in overlay_digests.values())):
        raise SafetyError("The pre-sign overlay-inclusive digest inventory is malformed")
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
            try:
                signed_pe = path.read_bytes()
            except OSError as exc:
                raise SafetyError(f"Could not read signed PE image ({type(exc).__name__})") from None
            signed_digest = authenticode_pe_digest_bytes(signed_pe)
            content_digest = overlay_inclusive_pe_digest_bytes(signed_pe)
            original_content_digest = inventory["overlayDigests"][relative]
            if content_digest != original_content_digest:
                raise SafetyError(f"Signed executable content changed after build: {relative}")
            _exclusions, certificate = _pe_exclusion_ranges(signed_pe)
            embedded_digests = _embedded_sha256_image_digests(signed_pe, certificate)
            if any(digest.hex() != signed_digest for digest in embedded_digests):
                raise SafetyError(f"Embedded Authenticode message digest mismatch: {relative}")
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
