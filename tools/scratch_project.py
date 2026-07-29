#!/usr/bin/env python3
"""Build, import, and validate the Xevious Scratch 3 project.

The historic SB3 remains an immutable asset base. Editable Scratch structure lives
under src/xevious, while only new or modified assets are stored as overlays.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from datetime import datetime
from urllib.parse import urlparse
import uuid
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_ARCHIVE = ROOT / "assets" / "original" / "Xevious.sb3"
ORIGINAL_PROVENANCE = ROOT / "assets" / "original" / "provenance.json"
SOURCE_DIR = ROOT / "src" / "xevious"
PROJECT_JSON = "project.json"
OVERLAY_DIRNAME = "assets"
OVERLAY_PROVENANCE = "provenance.json"
DIST_ARCHIVE = ROOT / "dist" / "Xevious.sb3"

MAX_ARCHIVE_ENTRIES = 4096
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = 2 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ALLOWED_ASSET_SIGNATURES = {
    "png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "wav": lambda data: (
        len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WAVE"
    ),
    "mp3": lambda data: (
        data.startswith(b"ID3")
        or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    ),
}


class ScratchProjectError(RuntimeError):
    """A project or archive failed a safety or integrity check."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_bytes(data: bytes) -> str:
    # Scratch 3 asset identifiers are MD5 content hashes.
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _load_json_bytes(data: bytes, label: str) -> dict:
    def reject_constant(value: str) -> object:
        raise ScratchProjectError(
            f"{label} contains non-standard JSON number {value}"
        )

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScratchProjectError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ScratchProjectError(f"{label} must contain one JSON object")
    if not isinstance(value.get("targets"), list):
        raise ScratchProjectError(f"{label} must contain a targets list")
    return value


def _ordered_json_bytes(value: object) -> bytes:
    # sort_keys is deliberately false. Scratch VM builds its ordered hat-script list
    # from block-map iteration order, so existing object order is behavior-relevant.
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ScratchProjectError(
            f"cannot serialize canonical project JSON: {exc}"
        ) from exc
    return (text + "\n").encode("utf-8")


def _load_provenance(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_MEMBER_BYTES:
            raise ScratchProjectError(f"provenance record is too large: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScratchProjectError(f"missing provenance record: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ScratchProjectError(f"cannot read provenance record {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScratchProjectError(f"provenance record must be a JSON object: {path}")
    return value


def verify_original(
    archive: Path = ORIGINAL_ARCHIVE,
    provenance_path: Path = ORIGINAL_PROVENANCE,
) -> str:
    if not provenance_path.is_file() or provenance_path.is_symlink():
        raise ScratchProjectError(
            f"missing or unsafe provenance record: {provenance_path}"
        )
    provenance = _load_provenance(provenance_path)
    if provenance.get("version") != 1:
        raise ScratchProjectError(
            f"{provenance_path} must use provenance schema version 1"
        )
    expected = provenance.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ScratchProjectError(
            f"{provenance_path} must contain the original archive's SHA-256"
        )
    if not archive.is_file() or archive.is_symlink():
        raise ScratchProjectError(f"missing or unsafe original archive: {archive}")
    expected_file = provenance.get("file")
    if expected_file != archive.name:
        raise ScratchProjectError(
            f"{provenance_path} names {expected_file!r}, expected {archive.name!r}"
        )
    expected_bytes = provenance.get("bytes")
    if expected_bytes != archive.stat().st_size:
        raise ScratchProjectError(
            f"{provenance_path} records {expected_bytes!r} bytes, "
            f"found {archive.stat().st_size}"
        )
    source_commit = provenance.get("preserved_from_commit")
    if not isinstance(source_commit, str) or len(source_commit) != 40 or any(
        char not in "0123456789abcdef" for char in source_commit
    ):
        raise ScratchProjectError(
            f"{provenance_path} has no valid preserved source commit"
        )
    scratch_project = provenance.get("scratch_project")
    parsed_url = urlparse(scratch_project) if isinstance(scratch_project, str) else None
    if (
        parsed_url is None
        or parsed_url.scheme != "https"
        or parsed_url.netloc != "scratch.mit.edu"
        or not parsed_url.path.startswith("/projects/")
    ):
        raise ScratchProjectError(
            f"{provenance_path} has no valid Scratch project URL"
        )
    for field in ("created", "last_publicly_modified"):
        timestamp = provenance.get(field)
        try:
            if not isinstance(timestamp, str):
                raise ValueError
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScratchProjectError(
                f"{provenance_path} has no valid {field} timestamp"
            ) from exc
    if not isinstance(provenance.get("notes"), str) or not provenance["notes"].strip():
        raise ScratchProjectError(f"{provenance_path} has no provenance notes")
    actual = _sha256_file(archive)
    if actual != expected:
        raise ScratchProjectError(
            f"original archive hash changed: expected {expected}, found {actual}"
        )
    if (ROOT / ".git").exists():
        historical = subprocess.run(
            ["git", "show", f"{source_commit}:Xevious.sb3"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        if historical.returncode != 0:
            raise ScratchProjectError(
                f"cannot verify preserved archive at source commit {source_commit}"
            )
        historical_hash = hashlib.sha256(historical.stdout).hexdigest()
        if historical_hash != expected:
            raise ScratchProjectError(
                f"source commit {source_commit} contains a different Xevious.sb3"
            )
    return actual


def _safe_member_name(name: str) -> str:
    if not name or "\\" in name:
        raise ScratchProjectError(f"unsafe ZIP member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in (".", ".."):
        raise ScratchProjectError(f"ZIP members must be root-level files: {name!r}")
    return path.name


def _preflight_zip_directory(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise ScratchProjectError(
            f"archive is larger than {MAX_ARCHIVE_BYTES} bytes on disk"
        )
    tail_size = min(size, 22 + 65535)
    with path.open("rb") as stream:
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
    signature = b"PK\x05\x06"
    position = len(tail)
    record = None
    while True:
        position = tail.rfind(signature, 0, position)
        if position < 0:
            break
        if len(tail) - position >= 22:
            candidate = struct.unpack_from("<4s4H2LH", tail, position)
            comment_length = candidate[-1]
            if position + 22 + comment_length == len(tail):
                record = candidate
                break
        position -= 1
    if record is None:
        raise ScratchProjectError("archive has no valid ZIP end-of-directory record")
    (
        _signature,
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        _comment_length,
    ) = record
    if disk_number != 0 or central_disk != 0 or disk_entries != total_entries:
        raise ScratchProjectError("multi-disk ZIP archives are not supported")
    if total_entries == 0xFFFF or central_size == 0xFFFFFFFF:
        raise ScratchProjectError("ZIP64 archives are not supported")
    if total_entries > MAX_ARCHIVE_ENTRIES:
        raise ScratchProjectError(
            f"archive has {total_entries} entries; maximum is {MAX_ARCHIVE_ENTRIES}"
        )
    if central_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise ScratchProjectError(
            "archive central directory exceeds the safety bound"
        )
    if central_offset + central_size > size - 22:
        raise ScratchProjectError("archive central directory is out of bounds")


def read_safe_archive(path: Path) -> dict[str, bytes]:
    if not path.is_file() or path.is_symlink():
        raise ScratchProjectError(f"archive is missing or unsafe: {path}")
    _preflight_zip_directory(path)
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ScratchProjectError(f"cannot open Scratch archive {path}: {exc}") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ScratchProjectError(
                f"archive has {len(infos)} entries; maximum is {MAX_ARCHIVE_ENTRIES}"
            )
        names: set[str] = set()
        folded: set[str] = set()
        total = 0
        result: dict[str, bytes] = {}
        for info in infos:
            name = _safe_member_name(info.filename)
            folded_name = name.casefold()
            if name in names or folded_name in folded:
                raise ScratchProjectError(f"duplicate or case-colliding ZIP member: {name}")
            names.add(name)
            folded.add(folded_name)
            if info.is_dir():
                raise ScratchProjectError(f"directories are not allowed in an SB3: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ScratchProjectError(f"symlinks are not allowed in an SB3: {name}")
            if info.flag_bits & 0x1:
                raise ScratchProjectError(f"encrypted ZIP members are not allowed: {name}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ScratchProjectError(
                    f"ZIP member {name} is larger than {MAX_MEMBER_BYTES} bytes"
                )
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise ScratchProjectError(
                    f"archive expands beyond {MAX_ARCHIVE_BYTES} bytes"
                )
            if (
                info.file_size > 1024 * 1024
                and info.compress_size > 0
                and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise ScratchProjectError(
                    f"ZIP member {name} exceeds the compression-ratio safety bound"
                )
            try:
                result[name] = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ScratchProjectError(f"cannot read ZIP member {name}: {exc}") from exc
    if PROJECT_JSON not in result:
        raise ScratchProjectError("Scratch archive is missing project.json")
    return result


def _asset_references(project: dict) -> set[str]:
    references: set[str] = set()
    target_names: set[str] = set()
    for target_index, target in enumerate(project["targets"]):
        if not isinstance(target, dict):
            raise ScratchProjectError(f"target {target_index} must be a JSON object")
        name = target.get("name")
        if not isinstance(name, str) or not name:
            raise ScratchProjectError(f"target {target_index} has no valid name")
        if name in target_names:
            raise ScratchProjectError(f"duplicate Scratch target name: {name}")
        target_names.add(name)
        blocks = target.get("blocks")
        if not isinstance(blocks, dict):
            raise ScratchProjectError(f"target {name} must contain a blocks object")
        for collection in ("costumes", "sounds"):
            entries = target.get(collection)
            if not isinstance(entries, list):
                raise ScratchProjectError(f"target {name} must contain a {collection} list")
            for index, asset in enumerate(entries):
                label = f"{name}.{collection}[{index}]"
                if not isinstance(asset, dict):
                    raise ScratchProjectError(f"{label} must be a JSON object")
                asset_id = asset.get("assetId")
                data_format = asset.get("dataFormat")
                md5ext = asset.get("md5ext")
                if not all(isinstance(value, str) and value for value in (
                    asset_id,
                    data_format,
                    md5ext,
                )):
                    raise ScratchProjectError(
                        f"{label} must declare assetId, dataFormat, and md5ext"
                    )
                expected = f"{asset_id}.{data_format}"
                if md5ext != expected:
                    raise ScratchProjectError(
                        f"{label} has incoherent asset identity: {md5ext} != {expected}"
                    )
                if _safe_member_name(md5ext) != md5ext:
                    raise ScratchProjectError(f"{label} has an unsafe asset name")
                if len(asset_id) != 32 or any(
                    char not in "0123456789abcdef" for char in asset_id
                ):
                    raise ScratchProjectError(f"{label} has a non-MD5 assetId")
                references.add(md5ext)
    return references


def _validate_svg_css(name: str, value: str) -> None:
    lowered = value.strip().lower()
    if "\\" in lowered or "/*" in lowered:
        raise ScratchProjectError(
            f"obfuscated SVG style is not allowed in asset: {name}"
        )
    if any(
        token in lowered
        for token in ("@import", "expression(", "javascript:")
    ):
        raise ScratchProjectError(f"unsafe SVG style in asset: {name}")
    references = re.findall(r"url\(([^)]*)\)", lowered)
    if any(
        not reference.strip(" '\"").startswith("#")
        for reference in references
    ):
        raise ScratchProjectError(
            f"external SVG style reference in asset: {name}"
        )


def _validate_svg(name: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScratchProjectError(
            f"SVG asset must use UTF-8 encoding: {name}"
        ) from exc
    lowered = text.lower()
    if (
        "<!doctype" in lowered
        or "<!entity" in lowered
        or "<?xml-stylesheet" in lowered
    ):
        raise ScratchProjectError(f"unsafe SVG declaration in asset: {name}")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ScratchProjectError(f"invalid SVG asset {name}: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ScratchProjectError(f"SVG asset has no svg root element: {name}")
    unsafe_elements = {
        "audio",
        "embed",
        "foreignobject",
        "iframe",
        "object",
        "script",
        "video",
    }
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1].lower()
        if local_name in unsafe_elements:
            raise ScratchProjectError(
                f"unsafe SVG element {local_name} in asset: {name}"
            )
        if local_name == "style":
            _validate_svg_css(name, "".join(element.itertext()))
        for raw_attribute, raw_value in element.attrib.items():
            attribute = raw_attribute.rsplit("}", 1)[-1].lower()
            value = raw_value.strip().lower()
            if attribute.startswith("on"):
                raise ScratchProjectError(
                    f"unsafe SVG event attribute in asset: {name}"
                )
            if attribute == "href" and value and not value.startswith("#"):
                raise ScratchProjectError(
                    f"external or embedded SVG reference in asset: {name}"
                )
            _validate_svg_css(name, value)


def _validate_asset(name: str, data: bytes) -> None:
    safe_name = _safe_member_name(name)
    if "." not in safe_name:
        raise ScratchProjectError(f"asset filename has no extension: {name}")
    asset_id, extension = safe_name.rsplit(".", 1)
    if extension == "svg":
        _validate_svg(name, data)
    elif extension in ALLOWED_ASSET_SIGNATURES:
        if not ALLOWED_ASSET_SIGNATURES[extension](data):
            raise ScratchProjectError(
                f"asset content does not match its {extension} type: {name}"
            )
    else:
        raise ScratchProjectError(
            f"unsupported asset type for this project: {name}"
        )
    actual = _md5_bytes(data)
    if asset_id != actual:
        raise ScratchProjectError(
            f"asset content hash mismatch for {name}: expected {asset_id}, found {actual}"
        )


def validate_archive(path: Path) -> tuple[dict, dict[str, bytes]]:
    members = read_safe_archive(path)
    project = _load_json_bytes(members[PROJECT_JSON], f"{path}:project.json")
    references = _asset_references(project)
    assets = {name: data for name, data in members.items() if name != PROJECT_JSON}
    for name, data in assets.items():
        _validate_asset(name, data)
    missing = references - assets.keys()
    unknown = assets.keys() - references
    if missing:
        raise ScratchProjectError(
            "archive is missing referenced assets: " + ", ".join(sorted(missing))
        )
    if unknown:
        raise ScratchProjectError(
            "archive contains unreferenced assets: " + ", ".join(sorted(unknown))
        )
    return project, assets


def _validate_asset_provenance(
    assets: dict[str, dict],
    label: str,
) -> None:
    for name, record in assets.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise ScratchProjectError(f"invalid asset provenance entry for {name!r}")
        _safe_member_name(name)
        for field in ("origin", "license"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ScratchProjectError(
                    f"{label} asset {name} has no recorded {field}"
                )


def _read_asset_provenance(path: Path) -> dict[str, dict]:
    if path.is_symlink() or not path.is_file():
        raise ScratchProjectError(
            f"asset provenance must be a regular, non-symlink file: {path}"
        )
    provenance = _load_provenance(path)
    if provenance.get("version") != 1 or not isinstance(provenance.get("assets"), dict):
        raise ScratchProjectError(
            f"{path} must contain version 1 and an assets object"
        )
    assets: dict[str, dict] = provenance["assets"]
    _validate_asset_provenance(assets, str(path))
    return assets


def _read_overlay_provenance(overlay_dir: Path) -> dict[str, dict]:
    return _read_asset_provenance(overlay_dir / OVERLAY_PROVENANCE)


def _read_overlay_assets(overlay_dir: Path) -> tuple[dict[str, bytes], dict[str, dict]]:
    if overlay_dir.is_symlink() or not overlay_dir.is_dir():
        raise ScratchProjectError(f"overlay asset directory is missing or unsafe: {overlay_dir}")
    provenance_path = overlay_dir / OVERLAY_PROVENANCE
    if not provenance_path.is_file() or provenance_path.is_symlink():
        raise ScratchProjectError(
            f"overlay provenance is missing or unsafe: {provenance_path}"
        )
    provenance = _read_overlay_provenance(overlay_dir)
    assets: dict[str, bytes] = {}
    folded: set[str] = set()
    total = 0
    entries = [
        path
        for path in sorted(overlay_dir.iterdir(), key=lambda item: item.name)
        if path.name != OVERLAY_PROVENANCE
    ]
    if len(entries) > MAX_ARCHIVE_ENTRIES - 1:
        raise ScratchProjectError(
            f"overlay contains more than {MAX_ARCHIVE_ENTRIES - 1} assets"
        )
    for path in entries:
        if path.is_symlink() or not path.is_file():
            raise ScratchProjectError(f"overlay contains an unsafe entry: {path}")
        name = _safe_member_name(path.name)
        folded_name = name.casefold()
        if folded_name in folded:
            raise ScratchProjectError(f"case-colliding overlay asset: {name}")
        folded.add(folded_name)
        data = path.read_bytes()
        if len(data) > MAX_MEMBER_BYTES:
            raise ScratchProjectError(f"overlay asset is too large: {name}")
        total += len(data)
        if total > MAX_ARCHIVE_BYTES:
            raise ScratchProjectError(
                f"overlay assets exceed {MAX_ARCHIVE_BYTES} bytes"
            )
        _validate_asset(name, data)
        assets[name] = data
    if assets.keys() != provenance.keys():
        missing_records = assets.keys() - provenance.keys()
        stale_records = provenance.keys() - assets.keys()
        details = []
        if missing_records:
            details.append("missing provenance: " + ", ".join(sorted(missing_records)))
        if stale_records:
            details.append("stale provenance: " + ", ".join(sorted(stale_records)))
        raise ScratchProjectError("; ".join(details))
    return assets, provenance


def _original_asset_base(
    archive: Path = ORIGINAL_ARCHIVE,
    provenance_path: Path = ORIGINAL_PROVENANCE,
) -> dict[str, bytes]:
    verify_original(archive, provenance_path)
    _project, assets = validate_archive(archive)
    return assets


def validate_source(
    source_dir: Path = SOURCE_DIR,
    original: Path = ORIGINAL_ARCHIVE,
    original_provenance: Path = ORIGINAL_PROVENANCE,
) -> tuple[dict, bytes, dict[str, bytes]]:
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise ScratchProjectError(f"source directory is missing or unsafe: {source_dir}")
    project_path = source_dir / PROJECT_JSON
    if project_path.is_symlink() or not project_path.is_file():
        raise ScratchProjectError(f"source project.json is missing or unsafe: {project_path}")
    if project_path.stat().st_size > MAX_MEMBER_BYTES:
        raise ScratchProjectError(f"source project.json is too large: {project_path}")
    project_bytes = project_path.read_bytes()
    project = _load_json_bytes(project_bytes, str(project_path))
    references = _asset_references(project)
    base_assets = _original_asset_base(original, original_provenance)
    overlay_assets, _provenance = _read_overlay_assets(
        source_dir / OVERLAY_DIRNAME
    )
    for name in base_assets.keys() & overlay_assets.keys():
        if base_assets[name] != overlay_assets[name]:
            raise ScratchProjectError(
                f"overlay collides with immutable baseline asset using different bytes: {name}"
            )
    orphaned = overlay_assets.keys() - references
    if orphaned:
        raise ScratchProjectError(
            "overlay contains unreferenced assets: " + ", ".join(sorted(orphaned))
        )
    resolved: dict[str, bytes] = {}
    missing = []
    for name in sorted(references):
        if name in overlay_assets:
            resolved[name] = overlay_assets[name]
        elif name in base_assets:
            resolved[name] = base_assets[name]
        else:
            missing.append(name)
    if missing:
        raise ScratchProjectError(
            "source references unavailable assets: " + ", ".join(missing)
        )
    return project, project_bytes, resolved


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.flag_bits = 0
    info.extra = b""
    info.comment = b""
    return info


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return False


def _path_is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        pass
    for candidate in (path, *path.parents):
        if _same_file(candidate, directory):
            return True
    return False


def build_project(
    source_dir: Path = SOURCE_DIR,
    output: Path = DIST_ARCHIVE,
    original: Path = ORIGINAL_ARCHIVE,
    original_provenance: Path = ORIGINAL_PROVENANCE,
) -> str:
    resolved_output = output.resolve()
    resolved_source = source_dir.resolve()
    protected_files = {
        original.resolve(),
        original_provenance.resolve(),
    }
    if (
        _path_is_within(resolved_output, resolved_source)
        or resolved_output in protected_files
        or any(_same_file(resolved_output, protected) for protected in protected_files)
    ):
        raise ScratchProjectError(
            f"build output would overwrite protected project input: {output}"
        )
    _project, project_bytes, assets = validate_source(
        source_dir, original, original_provenance
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(_zip_info(PROJECT_JSON), project_bytes)
            for name in sorted(assets):
                archive.writestr(_zip_info(name), assets[name])
        validate_archive(temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return _sha256_file(output)


def _preserve_existing_block_order(existing: dict, incoming: dict) -> list[str]:
    existing_targets = {
        target.get("name"): target
        for target in existing.get("targets", [])
        if isinstance(target, dict) and isinstance(target.get("name"), str)
    }
    changed: list[str] = []
    for target in incoming.get("targets", []):
        if not isinstance(target, dict):
            continue
        old_target = existing_targets.get(target.get("name"))
        old_blocks = old_target.get("blocks") if isinstance(old_target, dict) else None
        new_blocks = target.get("blocks")
        if not isinstance(old_blocks, dict) or not isinstance(new_blocks, dict):
            continue
        old_keys = list(old_blocks)
        incoming_existing = [key for key in new_blocks if key in old_blocks]
        old_remaining = [key for key in old_keys if key in new_blocks]
        if incoming_existing != old_remaining:
            changed.append(str(target.get("name")))
        ordered = {
            key: new_blocks[key]
            for key in old_keys
            if key in new_blocks
        }
        ordered.update({
            key: value
            for key, value in new_blocks.items()
            if key not in old_blocks
        })
        target["blocks"] = ordered
    return changed


def _write_overlay_provenance(path: Path, assets: dict[str, dict]) -> None:
    path.write_bytes(_ordered_json_bytes({"version": 1, "assets": assets}))


def _git_changes_for_source(source_dir: Path) -> list[str]:
    try:
        relative = source_dir.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return []
    if not (ROOT / ".git").exists():
        return []
    result = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            str(relative),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ScratchProjectError(
            f"cannot check source worktree state: {result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line]


def import_project(
    archive: Path,
    source_dir: Path = SOURCE_DIR,
    *,
    force: bool = False,
    asset_origin: str | None = None,
    asset_license: str | None = None,
    asset_notes: str | None = None,
    asset_provenance: dict[str, dict] | None = None,
    original: Path = ORIGINAL_ARCHIVE,
    original_provenance: Path = ORIGINAL_PROVENANCE,
) -> list[str]:
    incoming_project, incoming_assets = validate_archive(archive)
    base_assets = _original_asset_base(original, original_provenance)

    existing_project = None
    existing_provenance: dict[str, dict] = {}
    if source_dir.exists():
        if not force:
            raise ScratchProjectError(
                f"source already exists at {source_dir}; pass --force to replace it"
            )
        changes = _git_changes_for_source(source_dir)
        if changes:
            raise ScratchProjectError(
                "source contains uncommitted work; commit or stash it before "
                "importing: " + ", ".join(changes)
            )
        current_project_path = source_dir / PROJECT_JSON
        if current_project_path.is_file() and not current_project_path.is_symlink():
            existing_project = _load_json_bytes(
                current_project_path.read_bytes(), str(current_project_path)
            )
        current_overlay = source_dir / OVERLAY_DIRNAME
        if current_overlay.is_dir() and not current_overlay.is_symlink():
            existing_provenance = _read_overlay_provenance(current_overlay)

    reordered_targets: list[str] = []
    if existing_project is not None:
        reordered_targets = _preserve_existing_block_order(
            existing_project, incoming_project
        )

    overlay_assets: dict[str, bytes] = {}
    overlay_provenance: dict[str, dict] = {}
    unresolved_provenance: list[str] = []
    supplied_provenance = asset_provenance or {}
    _validate_asset_provenance(supplied_provenance, "supplied provenance")
    for name, data in incoming_assets.items():
        if name in base_assets:
            if base_assets[name] != data:
                raise ScratchProjectError(
                    f"import collides with immutable baseline asset using different bytes: {name}"
                )
            continue
        overlay_assets[name] = data
        if name in existing_provenance:
            overlay_provenance[name] = existing_provenance[name]
        elif name in supplied_provenance:
            overlay_provenance[name] = supplied_provenance[name]
        else:
            if not asset_origin or not asset_license:
                unresolved_provenance.append(name)
                continue
            record = {"origin": asset_origin, "license": asset_license}
            if asset_notes:
                record["notes"] = asset_notes
            overlay_provenance[name] = record
    if unresolved_provenance:
        raise ScratchProjectError(
            "import needs provenance for these new or modified assets: "
            + ", ".join(sorted(unresolved_provenance))
            + "; provide --asset-provenance or both --asset-origin and "
            "--asset-license"
        )
    unused_provenance = supplied_provenance.keys() - overlay_assets.keys()
    if unused_provenance:
        raise ScratchProjectError(
            "asset provenance contains entries not introduced by this import: "
            + ", ".join(sorted(unused_provenance))
        )

    source_parent = source_dir.parent
    source_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{source_dir.name}.import-", dir=source_parent))
    backup: Path | None = None
    try:
        (stage / OVERLAY_DIRNAME).mkdir()
        (stage / PROJECT_JSON).write_bytes(_ordered_json_bytes(incoming_project))
        for name, data in overlay_assets.items():
            (stage / OVERLAY_DIRNAME / name).write_bytes(data)
        _write_overlay_provenance(
            stage / OVERLAY_DIRNAME / OVERLAY_PROVENANCE,
            overlay_provenance,
        )
        validate_source(stage, original, original_provenance)

        if source_dir.exists():
            backup = source_parent / f".{source_dir.name}.backup-{uuid.uuid4().hex}"
            source_dir.rename(backup)
        try:
            stage.rename(source_dir)
        except Exception:
            if backup is not None and backup.exists() and not source_dir.exists():
                backup.rename(source_dir)
            raise
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                print(
                    f"warning: imported source is installed, but the backup "
                    f"could not be removed: {backup}: {exc}",
                    file=sys.stderr,
                )
            backup = None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup is not None and backup.exists() and not source_dir.exists():
            backup.rename(source_dir)
    return reordered_targets


def verify_repository(
    source_dir: Path = SOURCE_DIR,
    original: Path = ORIGINAL_ARCHIVE,
    original_provenance: Path = ORIGINAL_PROVENANCE,
) -> tuple[str, str]:
    original_hash = verify_original(original, original_provenance)
    root_archives = sorted(path.name for path in ROOT.glob("*.sb3"))
    if root_archives:
        raise ScratchProjectError(
            "SB3 files are not allowed at the repository root: "
            + ", ".join(root_archives)
        )
    validate_source(source_dir, original, original_provenance)
    with tempfile.TemporaryDirectory(prefix="xevious-verify-") as directory:
        temporary = Path(directory)
        first = temporary / "first.sb3"
        second = temporary / "second.sb3"
        first_hash = build_project(
            source_dir,
            first,
            original,
            original_provenance,
        )
        second_hash = build_project(
            source_dir,
            second,
            original,
            original_provenance,
        )
        if first.read_bytes() != second.read_bytes():
            raise ScratchProjectError("two clean builds produced different bytes")
        roundtrip = temporary / "roundtrip"
        overlay_provenance = _read_overlay_provenance(
            source_dir / OVERLAY_DIRNAME
        )
        import_project(
            first,
            roundtrip,
            asset_origin="Repository round-trip verification",
            asset_license="See preserved overlay provenance",
            original=original,
            original_provenance=original_provenance,
        )
        _write_overlay_provenance(
            roundtrip / OVERLAY_DIRNAME / OVERLAY_PROVENANCE,
            overlay_provenance,
        )
        validate_source(roundtrip, original, original_provenance)
        original_source = (source_dir / PROJECT_JSON).read_bytes()
        roundtrip_source = (roundtrip / PROJECT_JSON).read_bytes()
        if original_source != roundtrip_source:
            raise ScratchProjectError("project.json changed during build/import round trip")
        original_overlay = source_dir / OVERLAY_DIRNAME
        roundtrip_overlay = roundtrip / OVERLAY_DIRNAME
        original_files = {
            path.name: path.read_bytes()
            for path in original_overlay.iterdir()
            if path.is_file() and not path.is_symlink()
        }
        roundtrip_files = {
            path.name: path.read_bytes()
            for path in roundtrip_overlay.iterdir()
            if path.is_file() and not path.is_symlink()
        }
        if original_files != roundtrip_files:
            raise ScratchProjectError("asset overlay changed during build/import round trip")
    return original_hash, first_hash


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build, import, and validate the Xevious Scratch project"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build a deterministic SB3")
    build.add_argument("--source", type=_path, default=SOURCE_DIR)
    build.add_argument("--output", type=_path, default=DIST_ARCHIVE)

    import_command = commands.add_parser(
        "import", help="import an SB3 into the canonical source tree"
    )
    import_command.add_argument("archive", type=_path)
    import_command.add_argument("--source", type=_path, default=SOURCE_DIR)
    import_command.add_argument("--force", action="store_true")
    import_command.add_argument("--asset-origin")
    import_command.add_argument("--asset-license")
    import_command.add_argument("--asset-notes")
    import_command.add_argument(
        "--asset-provenance",
        type=_path,
        help="version 1 JSON map for assets with different origins or licenses",
    )

    validate = commands.add_parser("validate", help="validate source or one SB3")
    validate.add_argument("--archive", type=_path)
    validate.add_argument("--source", type=_path, default=SOURCE_DIR)

    commands.add_parser(
        "verify", help="run repository, deterministic-build, and round-trip checks"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            digest = build_project(args.source, args.output)
            print(f"built {args.output} sha256={digest}")
        elif args.command == "import":
            supplied_provenance = (
                _read_asset_provenance(args.asset_provenance)
                if args.asset_provenance
                else None
            )
            reordered = import_project(
                args.archive,
                args.source,
                force=args.force,
                asset_origin=args.asset_origin,
                asset_license=args.asset_license,
                asset_notes=args.asset_notes,
                asset_provenance=supplied_provenance,
            )
            print(f"imported {args.archive} into {args.source}")
            if reordered:
                print(
                    "preserved existing script order for targets whose editor export "
                    "reordered blocks: " + ", ".join(reordered)
                )
        elif args.command == "validate":
            if args.archive:
                project, assets = validate_archive(args.archive)
                print(
                    f"valid archive: {len(project['targets'])} targets, "
                    f"{len(assets)} assets"
                )
            else:
                project, _project_bytes, assets = validate_source(args.source)
                print(
                    f"valid source: {len(project['targets'])} targets, "
                    f"{len(assets)} resolved assets"
                )
        elif args.command == "verify":
            original_hash, build_hash = verify_repository()
            print(f"original preserved: sha256={original_hash}")
            print(f"deterministic build verified: sha256={build_hash}")
        return 0
    except (ScratchProjectError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
