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
import shutil
import stat
import sys
import tempfile
import uuid
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
MAX_COMPRESSION_RATIO = 200
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class ScratchProjectError(RuntimeError):
    """A project or archive failed a safety or integrity check."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    try:
        value = json.loads(data.decode("utf-8"))
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
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


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
    expected = provenance.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ScratchProjectError(
            f"{provenance_path} must contain the original archive's SHA-256"
        )
    if not archive.is_file() or archive.is_symlink():
        raise ScratchProjectError(f"missing or unsafe original archive: {archive}")
    actual = _sha256_file(archive)
    if actual != expected:
        raise ScratchProjectError(
            f"original archive hash changed: expected {expected}, found {actual}"
        )
    return actual


def _safe_member_name(name: str) -> str:
    if not name or "\\" in name:
        raise ScratchProjectError(f"unsafe ZIP member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in (".", ".."):
        raise ScratchProjectError(f"ZIP members must be root-level files: {name!r}")
    return path.name


def read_safe_archive(path: Path) -> dict[str, bytes]:
    if not path.is_file() or path.is_symlink():
        raise ScratchProjectError(f"archive is missing or unsafe: {path}")
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


def _validate_asset(name: str, data: bytes) -> None:
    safe_name = _safe_member_name(name)
    if "." not in safe_name:
        raise ScratchProjectError(f"asset filename has no extension: {name}")
    asset_id, _extension = safe_name.rsplit(".", 1)
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


def _read_overlay_provenance(overlay_dir: Path) -> dict[str, dict]:
    path = overlay_dir / OVERLAY_PROVENANCE
    provenance = _load_provenance(path)
    if provenance.get("version") != 1 or not isinstance(provenance.get("assets"), dict):
        raise ScratchProjectError(
            f"{path} must contain version 1 and an assets object"
        )
    assets: dict[str, dict] = provenance["assets"]
    for name, record in assets.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise ScratchProjectError(f"invalid overlay provenance entry for {name!r}")
        for field in ("origin", "license"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ScratchProjectError(
                    f"overlay asset {name} has no recorded {field}"
                )
    return assets


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


def build_project(
    source_dir: Path = SOURCE_DIR,
    output: Path = DIST_ARCHIVE,
    original: Path = ORIGINAL_ARCHIVE,
    original_provenance: Path = ORIGINAL_PROVENANCE,
) -> str:
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


def import_project(
    archive: Path,
    source_dir: Path = SOURCE_DIR,
    *,
    force: bool = False,
    asset_origin: str | None = None,
    asset_license: str | None = None,
    asset_notes: str | None = None,
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
        else:
            if not asset_origin or not asset_license:
                raise ScratchProjectError(
                    "import introduces new or modified assets; provide both "
                    "--asset-origin and --asset-license"
                )
            record = {"origin": asset_origin, "license": asset_license}
            if asset_notes:
                record["notes"] = asset_notes
            overlay_provenance[name] = record

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
            shutil.rmtree(backup)
            backup = None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup is not None and backup.exists() and not source_dir.exists():
            backup.rename(source_dir)
    return reordered_targets


def verify_repository() -> tuple[str, str]:
    original_hash = verify_original()
    root_archives = sorted(path.name for path in ROOT.glob("*.sb3"))
    if root_archives:
        raise ScratchProjectError(
            "SB3 files are not allowed at the repository root: "
            + ", ".join(root_archives)
        )
    validate_source()
    with tempfile.TemporaryDirectory(prefix="xevious-verify-") as directory:
        temporary = Path(directory)
        first = temporary / "first.sb3"
        second = temporary / "second.sb3"
        first_hash = build_project(output=first)
        second_hash = build_project(output=second)
        if first.read_bytes() != second.read_bytes():
            raise ScratchProjectError("two clean builds produced different bytes")
        roundtrip = temporary / "roundtrip"
        import_project(first, roundtrip)
        original_source = (SOURCE_DIR / PROJECT_JSON).read_bytes()
        roundtrip_source = (roundtrip / PROJECT_JSON).read_bytes()
        if original_source != roundtrip_source:
            raise ScratchProjectError("project.json changed during build/import round trip")
        original_overlay = SOURCE_DIR / OVERLAY_DIRNAME
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
            reordered = import_project(
                args.archive,
                args.source,
                force=args.force,
                asset_origin=args.asset_origin,
                asset_license=args.asset_license,
                asset_notes=args.asset_notes,
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
    except ScratchProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
