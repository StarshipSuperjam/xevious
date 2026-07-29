from __future__ import annotations

from contextlib import redirect_stderr
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import scratch_project as scratch  # noqa: E402
import check_mechanics_record as mechanics  # noqa: E402


ASSET_ONE = (
    b"\x89PNG\r\n\x1a\n"
    b"project-test-asset-one"
)
ASSET_TWO = (
    b"\x89PNG\r\n\x1a\n"
    b"project-test-asset-two"
)


def asset_name(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest() + ".png"


def write_project(source: Path, project: dict) -> None:
    (source / scratch.PROJECT_JSON).write_bytes(
        scratch._ordered_json_bytes(project)
    )


def load_source(source: Path) -> dict:
    return json.loads((source / scratch.PROJECT_JSON).read_text(encoding="utf-8"))


def add_overlay(source: Path, name: str, data: bytes, *, origin: str = "test") -> None:
    overlay = source / scratch.OVERLAY_DIRNAME
    (overlay / name).write_bytes(data)
    provenance_path = overlay / scratch.OVERLAY_PROVENANCE
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["assets"][name] = {"origin": origin, "license": "CC0-1.0"}
    provenance_path.write_bytes(scratch._ordered_json_bytes(provenance))


def write_archive(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in members:
            archive.writestr(scratch._zip_info(name), data)


class ScratchProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="xevious-tests-")
        self.temp = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def copy_source(self, name: str = "source") -> Path:
        destination = self.temp / name
        shutil.copytree(scratch.SOURCE_DIR, destination)
        return destination

    def assert_ordered_json_equal(
        self,
        expected: object,
        actual: object,
        path: str = "$",
    ) -> None:
        self.assertIs(type(expected), type(actual), path)
        if isinstance(expected, dict):
            self.assertEqual(list(expected), list(actual), path)
            for key in expected:
                self.assert_ordered_json_equal(
                    expected[key],
                    actual[key],
                    f"{path}.{key}",
                )
        elif isinstance(expected, list):
            self.assertEqual(len(expected), len(actual), path)
            for index, (expected_item, actual_item) in enumerate(
                zip(expected, actual)
            ):
                self.assert_ordered_json_equal(
                    expected_item,
                    actual_item,
                    f"{path}[{index}]",
                )
        else:
            self.assertEqual(expected, actual, path)

    def test_original_archive_matches_guarded_hash(self) -> None:
        self.assertEqual(
            scratch.verify_original(),
            "3a870e4402d18027d26daa06c006be7ab9973f594558a282ac14b7ee032a274e",
        )

    def test_original_provenance_byte_count_is_enforced(self) -> None:
        original_dir = self.temp / "original"
        original_dir.mkdir()
        archive = original_dir / "Xevious.sb3"
        provenance_path = original_dir / "provenance.json"
        shutil.copy2(scratch.ORIGINAL_ARCHIVE, archive)
        provenance = json.loads(
            scratch.ORIGINAL_PROVENANCE.read_text(encoding="utf-8")
        )
        provenance["bytes"] += 1
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        with self.assertRaisesRegex(scratch.ScratchProjectError, "records"):
            scratch.verify_original(archive, provenance_path)

    def test_original_provenance_schema_version_is_enforced(self) -> None:
        original_dir = self.temp / "original"
        original_dir.mkdir()
        archive = original_dir / "Xevious.sb3"
        provenance_path = original_dir / "provenance.json"
        shutil.copy2(scratch.ORIGINAL_ARCHIVE, archive)
        provenance = json.loads(
            scratch.ORIGINAL_PROVENANCE.read_text(encoding="utf-8")
        )
        provenance["version"] = 2
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        with self.assertRaisesRegex(scratch.ScratchProjectError, "version 1"):
            scratch.verify_original(archive, provenance_path)

    def test_repository_has_no_root_sb3(self) -> None:
        self.assertEqual([], list(ROOT.glob("*.sb3")))

    def test_current_source_validates(self) -> None:
        project, _project_bytes, assets = scratch.validate_source()
        self.assertEqual(14, len(project["targets"]))
        self.assertEqual(52, len(assets))

    def test_canonical_source_preserves_original_json_values_and_order(self) -> None:
        original = json.loads(
            scratch.read_safe_archive(scratch.ORIGINAL_ARCHIVE)[
                scratch.PROJECT_JSON
            ].decode("utf-8")
        )
        source = load_source(scratch.SOURCE_DIR)
        self.assert_ordered_json_equal(original, source)

    def test_two_clean_processes_build_identical_bytes(self) -> None:
        first = self.temp / "first.sb3"
        second = self.temp / "second.sb3"
        for output in (first, second):
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "scratch_project.py"),
                    "build",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_current_source_survives_build_import_roundtrip(self) -> None:
        built = self.temp / "built.sb3"
        imported = self.temp / "imported"
        scratch.build_project(output=built)
        scratch.import_project(built, imported)
        self.assertEqual(
            (scratch.SOURCE_DIR / scratch.PROJECT_JSON).read_bytes(),
            (imported / scratch.PROJECT_JSON).read_bytes(),
        )
        expected = {
            path.name: path.read_bytes()
            for path in (scratch.SOURCE_DIR / scratch.OVERLAY_DIRNAME).iterdir()
        }
        actual = {
            path.name: path.read_bytes()
            for path in (imported / scratch.OVERLAY_DIRNAME).iterdir()
        }
        self.assertEqual(expected, actual)

    def test_project_json_edit_reaches_built_archive(self) -> None:
        source = self.copy_source()
        project = load_source(source)
        stage = next(target for target in project["targets"] if target["isStage"])
        variable_id = next(iter(stage["variables"]))
        stage["variables"][variable_id][1] = 42
        write_project(source, project)

        built = self.temp / "edited.sb3"
        scratch.build_project(source, built)
        built_project, _assets = scratch.validate_archive(built)
        built_stage = next(
            target for target in built_project["targets"] if target["isStage"]
        )
        self.assertEqual(42, built_stage["variables"][variable_id][1])

    def test_modified_and_new_assets_survive_import_build_roundtrip(self) -> None:
        source = self.copy_source()
        project = load_source(source)
        stage = next(target for target in project["targets"] if target["isStage"])

        replacement_name = asset_name(ASSET_ONE)
        replacement_id, replacement_format = replacement_name.rsplit(".", 1)
        stage["costumes"][0].update({
            "assetId": replacement_id,
            "dataFormat": replacement_format,
            "md5ext": replacement_name,
        })
        add_overlay(source, replacement_name, ASSET_ONE)

        new_name = asset_name(ASSET_TWO)
        new_id, new_format = new_name.rsplit(".", 1)
        new_costume = copy.deepcopy(stage["costumes"][0])
        new_costume.update({
            "name": "test-added-costume",
            "assetId": new_id,
            "dataFormat": new_format,
            "md5ext": new_name,
        })
        stage["costumes"].append(new_costume)
        add_overlay(source, new_name, ASSET_TWO)
        write_project(source, project)

        built = self.temp / "assets.sb3"
        imported = self.temp / "imported"
        rebuilt = self.temp / "rebuilt.sb3"
        scratch.build_project(source, built)
        scratch.import_project(
            built,
            imported,
            asset_origin="Generated test fixture",
            asset_license="CC0-1.0",
        )
        self.assertEqual(
            {replacement_name, new_name},
            {
                path.name
                for path in (imported / scratch.OVERLAY_DIRNAME).iterdir()
                if path.name != scratch.OVERLAY_PROVENANCE
            },
        )
        scratch.build_project(imported, rebuilt)
        self.assertEqual(built.read_bytes(), rebuilt.read_bytes())

    def test_import_accepts_per_asset_provenance_for_mixed_media(self) -> None:
        source = self.copy_source()
        project = load_source(source)
        stage = next(target for target in project["targets"] if target["isStage"])
        records = {}
        for index, data in enumerate((ASSET_ONE, ASSET_TWO), start=1):
            name = asset_name(data)
            asset_id, data_format = name.rsplit(".", 1)
            costume = copy.deepcopy(stage["costumes"][0])
            costume.update({
                "name": f"mixed-provenance-{index}",
                "assetId": asset_id,
                "dataFormat": data_format,
                "md5ext": name,
            })
            stage["costumes"].append(costume)
            add_overlay(source, name, data)
            records[name] = {
                "origin": f"Independent source {index}",
                "license": f"Test-License-{index}",
            }
        write_project(source, project)
        built = self.temp / "mixed.sb3"
        imported = self.temp / "imported"
        scratch.build_project(source, built)

        scratch.import_project(
            built,
            imported,
            asset_provenance=records,
        )
        actual = json.loads(
            (
                imported
                / scratch.OVERLAY_DIRNAME
                / scratch.OVERLAY_PROVENANCE
            ).read_text(encoding="utf-8")
        )["assets"]
        self.assertEqual(records, actual)

    def test_import_names_every_asset_missing_provenance(self) -> None:
        source = self.copy_source()
        project = load_source(source)
        stage = next(target for target in project["targets"] if target["isStage"])
        names = []
        for index, data in enumerate((ASSET_ONE, ASSET_TWO), start=1):
            name = asset_name(data)
            names.append(name)
            asset_id, data_format = name.rsplit(".", 1)
            costume = copy.deepcopy(stage["costumes"][0])
            costume.update({
                "name": f"missing-provenance-{index}",
                "assetId": asset_id,
                "dataFormat": data_format,
                "md5ext": name,
            })
            stage["costumes"].append(costume)
            add_overlay(source, name, data)
        write_project(source, project)
        built = self.temp / "missing-provenance.sb3"
        scratch.build_project(source, built)

        with self.assertRaises(scratch.ScratchProjectError) as raised:
            scratch.import_project(built, self.temp / "imported")
        for name in names:
            self.assertIn(name, str(raised.exception))

    def test_repository_verification_supports_documented_overlays(self) -> None:
        source = self.copy_source()
        project = load_source(source)
        stage = next(target for target in project["targets"] if target["isStage"])
        name = asset_name(ASSET_ONE)
        asset_id, data_format = name.rsplit(".", 1)
        costume = copy.deepcopy(stage["costumes"][0])
        costume.update({
            "name": "verification-overlay",
            "assetId": asset_id,
            "dataFormat": data_format,
            "md5ext": name,
        })
        stage["costumes"].append(costume)
        add_overlay(source, name, ASSET_ONE)
        write_project(source, project)

        original_hash, build_hash = scratch.verify_repository(source)
        self.assertEqual(64, len(original_hash))
        self.assertEqual(64, len(build_hash))

    def test_import_preserves_existing_block_order(self) -> None:
        source = self.copy_source()
        built = self.temp / "built.sb3"
        reordered = self.temp / "reordered.sb3"
        scratch.build_project(source, built)
        members = scratch.read_safe_archive(built)
        project = json.loads(members[scratch.PROJECT_JSON].decode("utf-8"))
        stage = next(target for target in project["targets"] if target["isStage"])
        original_order = list(
            next(
                target
                for target in load_source(source)["targets"]
                if target["isStage"]
            )["blocks"]
        )
        stage["blocks"] = dict(reversed(list(stage["blocks"].items())))
        members[scratch.PROJECT_JSON] = scratch._ordered_json_bytes(project)
        write_archive(reordered, list(members.items()))

        changed, _backup = scratch.import_project(reordered, source, force=True)
        imported_stage = next(
            target for target in load_source(source)["targets"] if target["isStage"]
        )
        self.assertIn("Stage", changed)
        self.assertEqual(original_order, list(imported_stage["blocks"]))

    def test_import_appends_new_blocks_in_editor_order_for_multiple_targets(
        self,
    ) -> None:
        source = self.copy_source()
        built = self.temp / "built.sb3"
        edited = self.temp / "edited.sb3"
        scratch.build_project(source, built)
        members = scratch.read_safe_archive(built)
        project = json.loads(members[scratch.PROJECT_JSON].decode("utf-8"))
        original = load_source(source)
        target_names = [
            next(target["name"] for target in project["targets"] if target["isStage"]),
            next(target["name"] for target in project["targets"] if not target["isStage"]),
        ]
        expected_orders: dict[str, list[str]] = {}
        for target_name in target_names:
            target = next(
                target
                for target in project["targets"]
                if target["name"] == target_name
            )
            original_target = next(
                target
                for target in original["targets"]
                if target["name"] == target_name
            )
            old_order = list(original_target["blocks"])
            new_ids = [
                f"test-new-block-a-{target_name}",
                f"test-new-block-b-{target_name}",
            ]
            target["blocks"] = {
                new_ids[0]: {},
                **dict(reversed(list(target["blocks"].items()))),
                new_ids[1]: {},
            }
            expected_orders[target_name] = old_order + new_ids
        members[scratch.PROJECT_JSON] = scratch._ordered_json_bytes(project)
        write_archive(edited, list(members.items()))

        changed, _backup = scratch.import_project(edited, source, force=True)
        imported = load_source(source)
        self.assertEqual(set(target_names), set(changed))
        for target_name in target_names:
            target = next(
                target
                for target in imported["targets"]
                if target["name"] == target_name
            )
            self.assertEqual(expected_orders[target_name], list(target["blocks"]))

    def test_missing_asset_is_rejected(self) -> None:
        source = self.copy_source()
        project = load_source(source)
        stage = next(target for target in project["targets"] if target["isStage"])
        missing = "0" * 32 + ".png"
        stage["costumes"][0].update({
            "assetId": "0" * 32,
            "dataFormat": "png",
            "md5ext": missing,
        })
        write_project(source, project)
        with self.assertRaisesRegex(scratch.ScratchProjectError, "unavailable assets"):
            scratch.validate_source(source)

    def test_orphan_overlay_is_rejected(self) -> None:
        source = self.copy_source()
        add_overlay(source, asset_name(ASSET_ONE), ASSET_ONE)
        with self.assertRaisesRegex(scratch.ScratchProjectError, "unreferenced assets"):
            scratch.validate_source(source)

    def test_overlay_hash_mismatch_is_rejected(self) -> None:
        source = self.copy_source()
        add_overlay(source, asset_name(ASSET_ONE), ASSET_TWO)
        with self.assertRaisesRegex(scratch.ScratchProjectError, "content hash mismatch"):
            scratch.validate_source(source)

    def test_archive_parent_traversal_is_rejected(self) -> None:
        unsafe = self.temp / "unsafe.sb3"
        write_archive(unsafe, [("../project.json", b"{}")])
        with self.assertRaisesRegex(scratch.ScratchProjectError, "root-level"):
            scratch.read_safe_archive(unsafe)

    def test_duplicate_archive_member_is_rejected(self) -> None:
        duplicate = self.temp / "duplicate.sb3"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            write_archive(
                duplicate,
                [
                    (scratch.PROJECT_JSON, b"{}"),
                    (scratch.PROJECT_JSON, b"{}"),
                ],
            )
        with self.assertRaisesRegex(scratch.ScratchProjectError, "duplicate"):
            scratch.read_safe_archive(duplicate)

    def test_case_colliding_archive_members_are_rejected(self) -> None:
        collision = self.temp / "collision.sb3"
        write_archive(
            collision,
            [
                (scratch.PROJECT_JSON, b"{}"),
                (scratch.PROJECT_JSON.upper(), b"{}"),
            ],
        )
        with self.assertRaisesRegex(scratch.ScratchProjectError, "case-colliding"):
            scratch.read_safe_archive(collision)

    def test_backslash_archive_member_is_rejected(self) -> None:
        unsafe = self.temp / "backslash.sb3"
        write_archive(unsafe, [(r"folder\project.json", b"{}")])
        with self.assertRaisesRegex(scratch.ScratchProjectError, "unsafe"):
            scratch.read_safe_archive(unsafe)

    def test_symlink_archive_member_is_rejected(self) -> None:
        unsafe = self.temp / "symlink.sb3"
        info = scratch._zip_info(scratch.PROJECT_JSON)
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(unsafe, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(info, b"target")
        with self.assertRaisesRegex(scratch.ScratchProjectError, "symlinks"):
            scratch.read_safe_archive(unsafe)

    def test_archive_entry_bound_is_enforced(self) -> None:
        oversized = self.temp / "too-many.sb3"
        write_archive(
            oversized,
            [
                (scratch.PROJECT_JSON, b"{}"),
                ("extra.json", b"{}"),
            ],
        )
        with (
            mock.patch.object(scratch, "MAX_ARCHIVE_ENTRIES", 1),
            self.assertRaisesRegex(scratch.ScratchProjectError, "entries"),
        ):
            scratch.read_safe_archive(oversized)

    def test_archive_member_size_bound_is_enforced(self) -> None:
        oversized = self.temp / "too-large.sb3"
        write_archive(oversized, [(scratch.PROJECT_JSON, b"{}")])
        with (
            mock.patch.object(scratch, "MAX_MEMBER_BYTES", 1),
            self.assertRaisesRegex(scratch.ScratchProjectError, "larger"),
        ):
            scratch.read_safe_archive(oversized)

    def test_nonstandard_json_numbers_are_rejected(self) -> None:
        invalid = self.temp / "nan.sb3"
        write_archive(
            invalid,
            [(scratch.PROJECT_JSON, b'{"targets":[],"value":NaN}')],
        )
        with self.assertRaisesRegex(scratch.ScratchProjectError, "non-standard"):
            scratch.validate_archive(invalid)
        with self.assertRaisesRegex(scratch.ScratchProjectError, "serialize"):
            scratch._ordered_json_bytes({"targets": [], "value": float("nan")})

    def test_script_bearing_svg_asset_is_rejected(self) -> None:
        data = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
        name = hashlib.md5(data, usedforsecurity=False).hexdigest() + ".svg"
        with self.assertRaisesRegex(scratch.ScratchProjectError, "unsafe SVG"):
            scratch._validate_asset(name, data)

    def test_harmless_svg_and_mp3_assets_are_accepted(self) -> None:
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<defs><linearGradient id="g"/></defs>'
            b'<rect width="10" height="10" style="fill:url(#g)"/>'
            b"</svg>"
        )
        svg_name = hashlib.md5(svg, usedforsecurity=False).hexdigest() + ".svg"
        scratch._validate_asset(svg_name, svg)

        mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
        mp3_name = hashlib.md5(mp3, usedforsecurity=False).hexdigest() + ".mp3"
        scratch._validate_asset(mp3_name, mp3)

    def test_external_svg_references_are_rejected_in_every_supported_form(
        self,
    ) -> None:
        fixtures = [
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<rect fill="url(https://example.com/fill)"/></svg>'
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg"><style>'
                b'rect { fill: url(data:image/png;base64,AAAA); }'
                b"</style><rect/></svg>"
            ),
            (
                b'<?xml-stylesheet href="https://example.com/style.css"?>'
                b'<svg xmlns="http://www.w3.org/2000/svg"/>'
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<rect fill="u\\72l(https://example.com/fill)"/></svg>'
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg"><style>'
                b'@\\69mport u\\72l(https://example.com/style.css);'
                b"</style></svg>"
            ),
            (
                '<?xml-stylesheet href="https://example.com/style.css"?>'
                '<svg xmlns="http://www.w3.org/2000/svg"/>'
            ).encode("utf-16"),
        ]
        for data in fixtures:
            with self.subTest(data=data):
                name = (
                    hashlib.md5(data, usedforsecurity=False).hexdigest()
                    + ".svg"
                )
                with self.assertRaisesRegex(
                    scratch.ScratchProjectError,
                    "unsafe|external|obfuscated|UTF-8",
                ):
                    scratch._validate_asset(name, data)

    def test_asset_signature_must_match_extension(self) -> None:
        data = b"not a PNG"
        name = asset_name(data)
        with self.assertRaisesRegex(scratch.ScratchProjectError, "does not match"):
            scratch._validate_asset(name, data)

    def test_build_cannot_overwrite_canonical_source(self) -> None:
        source = self.copy_source()
        project_path = source / scratch.PROJECT_JSON
        before = project_path.read_bytes()
        with self.assertRaisesRegex(scratch.ScratchProjectError, "protected"):
            scratch.build_project(source, project_path)
        self.assertEqual(before, project_path.read_bytes())

    def test_build_rejects_case_variant_source_alias_on_case_insensitive_fs(
        self,
    ) -> None:
        source = self.copy_source("CaseSource")
        alias = source.with_name(source.name.swapcase())
        if not alias.exists() or not os.path.samefile(source, alias):
            self.skipTest("requires a case-insensitive filesystem")
        project_path = source / scratch.PROJECT_JSON
        before = project_path.read_bytes()
        with self.assertRaisesRegex(scratch.ScratchProjectError, "protected"):
            scratch.build_project(source, alias / scratch.PROJECT_JSON)
        self.assertEqual(before, project_path.read_bytes())

    def test_nonidentical_baseline_overlay_collision_is_rejected(self) -> None:
        source = self.copy_source()
        _project, _project_bytes, assets = scratch.validate_source(source)
        name = next(iter(assets))
        add_overlay(source, name, b"different bytes")
        with (
            mock.patch.object(scratch, "_validate_asset"),
            self.assertRaisesRegex(scratch.ScratchProjectError, "immutable baseline"),
        ):
            scratch.validate_source(source)

    def test_overlay_provenance_is_required(self) -> None:
        source = self.copy_source()
        name = asset_name(ASSET_ONE)
        (source / scratch.OVERLAY_DIRNAME / name).write_bytes(ASSET_ONE)
        with self.assertRaisesRegex(scratch.ScratchProjectError, "missing provenance"):
            scratch.validate_source(source)

    def test_forced_import_retains_complete_recoverable_backup(self) -> None:
        source = self.copy_source()
        built = self.temp / "built.sb3"
        scratch.build_project(source, built)
        local_note = source / "ignored-local-note.txt"
        local_note.write_text("recover me", encoding="utf-8")
        with mock.patch.object(
            scratch,
            "_git_changes_for_source",
            return_value=[],
        ):
            _changed, backup = scratch.import_project(built, source, force=True)
        self.assertIsNotNone(backup)
        self.assertEqual(
            "recover me",
            (backup / local_note.name).read_text(encoding="utf-8"),
        )
        self.assertFalse((source / local_note.name).exists())
        scratch.validate_source(source)

    def test_forced_import_refuses_uncommitted_source_work(self) -> None:
        source = self.copy_source()
        built = self.temp / "built.sb3"
        scratch.build_project(source, built)
        before = (source / scratch.PROJECT_JSON).read_bytes()
        with (
            mock.patch.object(
                scratch,
                "_git_changes_for_source",
                return_value=[" M src/xevious/project.json"],
            ),
            self.assertRaisesRegex(
                scratch.ScratchProjectError,
                "commit or stash",
            ),
        ):
            scratch.import_project(built, source, force=True)
        self.assertEqual(before, (source / scratch.PROJECT_JSON).read_bytes())

    def test_baseline_mechanics_record_is_complete(self) -> None:
        mechanics.validate_record(
            ROOT / "docs" / "mechanics" / "000-historical-baseline.md"
        )

    def test_incomplete_mechanics_record_is_rejected(self) -> None:
        record = self.temp / "incomplete.md"
        record.write_text("# Incomplete\n", encoding="utf-8")
        with self.assertRaisesRegex(mechanics.MechanicsRecordError, "missing"):
            mechanics.validate_record(record)

    def test_mechanics_record_requires_a_named_mechanic(self) -> None:
        baseline = (
            ROOT / "docs" / "mechanics" / "000-historical-baseline.md"
        ).read_text(encoding="utf-8")
        without_mechanic = "\n".join(
            line
            for line in baseline.splitlines()
            if not line.startswith("- Mechanic:")
        )
        record = self.temp / "unnamed.md"
        record.write_text(without_mechanic, encoding="utf-8")
        with self.assertRaisesRegex(mechanics.MechanicsRecordError, "Mechanic"):
            mechanics.validate_record(record)

    def test_symlink_mechanics_record_is_rejected(self) -> None:
        record = self.temp / "record.md"
        record.symlink_to(
            ROOT / "docs" / "mechanics" / "000-historical-baseline.md"
        )
        with self.assertRaisesRegex(mechanics.MechanicsRecordError, "non-symlink"):
            mechanics.validate_record(record)

    def test_project_change_requires_changed_mechanics_record(self) -> None:
        with (
            mock.patch.object(
                mechanics,
                "changed_paths",
                return_value=[mechanics.PROJECT_SOURCE],
            ),
            self.assertRaisesRegex(
                mechanics.MechanicsRecordError,
                "without a changed record",
            ),
        ):
            mechanics.check("unused")

    def test_project_change_accepts_complete_changed_mechanics_record(self) -> None:
        record_name = "docs/mechanics/000-historical-baseline.md"
        with mock.patch.object(
            mechanics,
            "changed_paths",
            return_value=[mechanics.PROJECT_SOURCE, record_name],
        ):
            self.assertEqual([ROOT / record_name], mechanics.check("unused"))

    def test_cli_formats_expected_filesystem_errors(self) -> None:
        errors = io.StringIO()
        with (
            mock.patch.object(
                scratch,
                "build_project",
                side_effect=FileExistsError("destination is a directory"),
            ),
            redirect_stderr(errors),
        ):
            result = scratch.main(["build"])
        self.assertEqual(2, result)
        self.assertIn("error: destination is a directory", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_full_repository_verification(self) -> None:
        original_hash, build_hash = scratch.verify_repository()
        self.assertEqual(
            "3a870e4402d18027d26daa06c006be7ab9973f594558a282ac14b7ee032a274e",
            original_hash,
        )
        self.assertEqual(
            "7b95c6d710697f23478ed491fe59f890994be4bd3f6c1d1718d2c38cc65df8ca",
            build_hash,
        )


if __name__ == "__main__":
    unittest.main()
