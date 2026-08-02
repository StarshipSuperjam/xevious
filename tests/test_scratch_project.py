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
import game_director as director  # noqa: E402


ASSET_ONE = (
    b"\x89PNG\r\n\x1a\n"
    b"project-test-asset-one"
)
ASSET_TWO = (
    b"\x89PNG\r\n\x1a\n"
    b"project-test-asset-two"
)
SPRITE_SHEET_HASHES = {
    "Logo & Title Screen": (
        "c8b88f131701e4db2d79284eafda2f5fea7589b412ed47a3373b3e78811c42a0"
    ),
    "Solvalou": (
        "0c88cd5cb440bebcc59aeeb20d8e141f62a5be4f4ff607be06a72ae1b8afdeaf"
    ),
    "Ground Enemies": (
        "bfcb48cb942c959bfcf482f86dca7c9a98f36d58913fb09133ee6529f0c566cf"
    ),
    "Andor Genesis": (
        "4ca80d9f5d8894c86d5557cafaf8b5fb8dff368c69ec36f16cbde69dd3891d68"
    ),
    "Aerial Enemies": (
        "0cd8361108354d74c2ea9bfa9e22836acc66158c963eafdc5a02c9021f5b9da8"
    ),
}


def asset_name(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest() + ".png"


def write_project(source: Path, project: dict) -> None:
    (source / scratch.PROJECT_JSON).write_bytes(
        scratch._ordered_json_bytes(project)
    )


def load_source(source: Path) -> dict:
    return json.loads((source / scratch.PROJECT_JSON).read_text(encoding="utf-8"))


def load_overlay_provenance(source: Path) -> dict[str, dict]:
    return json.loads(
        (
            source
            / scratch.OVERLAY_DIRNAME
            / scratch.OVERLAY_PROVENANCE
        ).read_text(encoding="utf-8")
    )["assets"]


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
        self.assertEqual(16, len(project["targets"]))
        self.assertEqual(67, len(assets))

    def test_canonical_source_preserves_untouched_historical_content(self) -> None:
        original = json.loads(
            scratch.read_safe_archive(scratch.ORIGINAL_ARCHIVE)[
                scratch.PROJECT_JSON
            ].decode("utf-8")
        )
        source = load_source(scratch.SOURCE_DIR)
        self.assertEqual(list(original), list(source))
        historical_targets = copy.deepcopy(source["targets"][:len(original["targets"])])
        original_solvalou = next(
            target
            for target in original["targets"]
            if target["name"] == "solvalou"
        )
        source_solvalou = next(
            target
            for target in historical_targets
            if target["name"] == "solvalou"
        )
        source_solvalou["costumes"] = source_solvalou["costumes"][
            :len(original_solvalou["costumes"])
        ]
        changed_scripts = {
            "Stage",
            "solvalou",
            "blaster",
            "area_01a",
            "area_01b",
            "start_screen",
            "solv_death",
            "target_a",
            "target_b",
            "bomb",
        }
        original_by_name = {target["name"]: target for target in original["targets"]}
        for target in historical_targets:
            expected = copy.deepcopy(original_by_name[target["name"]])
            if target["name"] not in changed_scripts:
                self.assert_ordered_json_equal(
                    expected,
                    target,
                    f"$.targets[{target['name']} ]",
                )
                continue
            expected.pop("blocks")
            actual = copy.deepcopy(target)
            actual.pop("blocks")
            if target["name"] == "Stage":
                for key in ("variables", "lists", "broadcasts"):
                    expected.pop(key)
                    actual.pop(key)
            elif target["name"] in {"solvalou", "solv_death"}:
                expected.pop("variables")
                actual.pop("variables")
            self.assert_ordered_json_equal(
                expected,
                actual,
                f"$.targets[{target['name']}].preserved",
            )
        self.assertEqual(
            "toroid_sprite_proof",
            source["targets"][-2]["name"],
        )
        self.assertEqual("sprite_sheets", source["targets"][-1]["name"])
        for key in original.keys() - {"targets"}:
            self.assert_ordered_json_equal(original[key], source[key], f"$.{key}")

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
        scratch.import_project(
            built,
            imported,
            asset_provenance=load_overlay_provenance(scratch.SOURCE_DIR),
        )
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
        existing_provenance = load_overlay_provenance(source)
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
            asset_provenance=existing_provenance,
        )
        self.assertEqual(
            set(existing_provenance) | {replacement_name, new_name},
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
        existing_provenance = load_overlay_provenance(source)
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

        all_records = {**existing_provenance, **records}
        scratch.import_project(
            built,
            imported,
            asset_provenance=all_records,
        )
        actual = json.loads(
            (
                imported
                / scratch.OVERLAY_DIRNAME
                / scratch.OVERLAY_PROVENANCE
            ).read_text(encoding="utf-8")
        )["assets"]
        self.assertEqual(all_records, actual)

    def test_import_names_every_asset_missing_provenance(self) -> None:
        source = self.copy_source()
        existing_provenance = load_overlay_provenance(source)
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
            scratch.import_project(
                built,
                self.temp / "imported",
                asset_provenance=existing_provenance,
            )
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

    def test_sprite_sheet_mechanics_record_is_complete(self) -> None:
        mechanics.validate_record(
            ROOT / "docs" / "mechanics" / "001-sprite-sheet-library.md"
        )

    def test_sprite_extraction_mechanics_record_is_complete(self) -> None:
        mechanics.validate_record(
            ROOT / "docs" / "mechanics" / "002-sprite-extraction-proof.md"
        )

    def test_game_director_mechanics_record_is_complete(self) -> None:
        mechanics.validate_record(
            ROOT / "docs" / "mechanics" / "003-game-director-and-state-reset.md"
        )

    def test_game_director_generator_is_current(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(
            scratch._ordered_json_bytes(project),
            director.project_bytes(director.expected_project(project)),
        )

    def test_game_director_generator_refuses_dirty_editor_source(self) -> None:
        with (
            mock.patch.object(director, "source_has_local_changes", return_value=True),
            self.assertRaisesRegex(SystemExit, "refusing to overwrite"),
        ):
            director.generate()

    def test_game_director_has_one_stage_owned_transition_path(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        stage = next(target for target in project["targets"] if target["isStage"])
        self.assertEqual(
            {
                "game state",
                "state epoch",
                "reset scope",
                "death outcome",
            },
            {name for name, _value in stage["variables"].values()},
        )
        self.assertEqual(
            [
                "boot -> title",
                "title -> ready",
                "ready -> playing",
                "playing -> player-dead",
                "player-dead -> respawning",
                "player-dead -> game-over",
                "respawning -> playing",
                "game-over -> title",
            ],
            stage["lists"][director.ALLOWED_ID][1],
        )
        definitions = [
            block
            for block in stage["blocks"].values()
            if block["opcode"] == "procedures_definition"
        ]
        calls = [
            block
            for block in stage["blocks"].values()
            if block["opcode"] == "procedures_call"
        ]
        self.assertEqual(1, len(definitions))
        self.assertTrue(calls)
        self.assertTrue(all(block["mutation"]["proccode"] == director.PROCCODE for block in calls))

        director_variable_ids = {
            director.STATE_ID,
            director.EPOCH_ID,
            director.SCOPE_ID,
            director.OUTCOME_ID,
        }
        for target in project["targets"]:
            if target["isStage"]:
                continue
            writes = {
                block["fields"].get("VARIABLE", [None, None])[1]
                for block in target["blocks"].values()
                if block["opcode"] in {"data_setvariableto", "data_changevariableby"}
            }
            self.assertTrue(director_variable_ids.isdisjoint(writes), target["name"])

    def test_transition_cleanup_is_serialized_before_state_entry(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        stage = next(target for target in project["targets"] if target["isStage"])
        definition_id, definition = next(
            (block_id, block)
            for block_id, block in stage["blocks"].items()
            if block["opcode"] == "procedures_definition"
        )
        guard = stage["blocks"][definition["next"]]
        self.assertEqual("control_if", guard["opcode"])
        condition = stage["blocks"][guard["inputs"]["CONDITION"][1]]
        self.assertEqual("data_listcontainsitem", condition["opcode"])
        self.assertEqual(
            ["allowed transitions", director.ALLOWED_ID],
            condition["fields"]["LIST"],
        )
        opcodes = []
        cursor = guard["inputs"]["SUBSTACK"][1]
        while cursor is not None:
            block = stage["blocks"][cursor]
            opcodes.append(block["opcode"])
            cursor = block["next"]
        self.assertEqual(
            [
                "data_changevariableby",
                "data_setvariableto",
                "event_broadcastandwait",
                "sound_stopallsounds",
                "data_setvariableto",
                "control_if",
                "event_broadcastandwait",
                "data_setvariableto",
                "event_broadcast",
            ],
            opcodes,
            definition_id,
        )

    def test_game_director_behavioral_contract_is_encoded(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        targets = {target["name"]: target for target in project["targets"]}

        def numeric(value: object) -> int | float | None:
            if (
                isinstance(value, list)
                and len(value) >= 2
                and isinstance(value[1], list)
                and len(value[1]) >= 2
            ):
                return value[1][1]
            return None

        solvalou = targets["solvalou"]["blocks"]
        self.assertNotIn("motion_ifonedgebounce", {b["opcode"] for b in solvalou.values()})
        self.assertEqual(
            4,
            sum(block["opcode"] == "sensing_keypressed" for block in solvalou.values()),
        )
        touched_frames = {
            block["fields"]["TOUCHINGOBJECTMENU"][0]
            for block in solvalou.values()
            if block["opcode"] == "sensing_touchingobjectmenu"
        }
        self.assertEqual({"frame_b", "frame_l", "frame_r"}, touched_frames)

        ready = [
            block
            for block in solvalou.values()
            if block["opcode"] == "looks_sayforsecs"
            and block["inputs"]["MESSAGE"][1][1] == "READY"
        ]
        self.assertEqual([1], [numeric(block["inputs"]["SECS"]) for block in ready])

        death = targets["solv_death"]["blocks"]
        self.assertIn("sound_play", {block["opcode"] for block in death.values()})
        self.assertNotIn("sound_playuntildone", {block["opcode"] for block in death.values()})
        death_repeats = [
            block
            for block in death.values()
            if block["opcode"] == "control_repeat"
            and numeric(block["inputs"]["TIMES"]) == 7
        ]
        self.assertEqual(1, len(death_repeats))
        repeat_first = death[death_repeats[0]["inputs"]["SUBSTACK"][1]]
        death_wait = death[repeat_first["next"]]
        self.assertEqual("looks_nextcostume", repeat_first["opcode"])
        self.assertEqual("control_wait", death_wait["opcode"])
        self.assertEqual(0.1, numeric(death_wait["inputs"]["DURATION"]))
        self.assertTrue(
            any(
                block["opcode"] == "motion_goto"
                and death[block["inputs"]["TO"][1]]["fields"]["TO"][0] == "solvalou"
                for block in death.values()
            )
        )
        game_over = [
            block
            for block in death.values()
            if block["opcode"] == "looks_sayforsecs"
            and block["inputs"]["MESSAGE"][1][1] == "GAME OVER"
        ]
        self.assertEqual([2], [numeric(block["inputs"]["SECS"]) for block in game_over])

        for name in ("blaster", "bomb"):
            blocks = targets[name]["blocks"]
            clone_hats = [
                block for block in blocks.values()
                if block["opcode"] == "control_start_as_clone"
            ]
            self.assertEqual(1, len(clone_hats), name)
            cursor = clone_hats[0]["next"]
            clone_opcodes = []
            while cursor is not None:
                clone_opcodes.append(blocks[cursor]["opcode"])
                cursor = blocks[cursor]["next"]
            self.assertNotIn("motion_gotoxy", clone_opcodes, name)
            key_hats = [
                block for block in blocks.values()
                if block["opcode"] == "event_whenkeypressed"
            ]
            self.assertEqual(1, len(key_hats), name)
            self.assertEqual("control_if", blocks[key_hats[0]["next"]]["opcode"])

        target_b = targets["target_b"]["blocks"]
        self.assertNotIn("looks_show", {block["opcode"] for block in target_b.values()})

    def test_reset_scope_matrix_has_canonical_and_preserving_paths(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        targets = {target["name"]: target for target in project["targets"]}

        def scope_literals(blocks: dict[str, dict[str, object]]) -> set[str]:
            values = set()
            for block in blocks.values():
                if block["opcode"] != "operator_equals":
                    continue
                left = block["inputs"].get("OPERAND1")
                right = block["inputs"].get("OPERAND2")
                if (
                    isinstance(left, list)
                    and len(left) > 1
                    and isinstance(left[1], list)
                    and len(left[1]) == 3
                    and left[1][2] == director.SCOPE_ID
                    and isinstance(right, list)
                    and len(right) > 1
                    and isinstance(right[1], list)
                ):
                    values.add(right[1][1])
            return values

        for name in ("area_01a", "area_01b"):
            blocks = targets[name]["blocks"]
            self.assertEqual({"cold-start", "new-game"}, scope_literals(blocks))
            self.assertEqual(2, sum(b["opcode"] == "motion_gotoxy" for b in blocks.values()))

        player = targets["solvalou"]["blocks"]
        self.assertEqual(
            {"cold-start", "new-game", "new-life", "game-over"},
            scope_literals(player),
        )
        for name in ("blaster", "bomb", "target_a", "target_b", "solv_death"):
            blocks = targets[name]["blocks"]
            reset_hats = [
                block for block in blocks.values()
                if block["opcode"] == "event_whenbroadcastreceived"
                and block["fields"]["BROADCAST_OPTION"][0] == "director reset"
            ]
            self.assertEqual(1, len(reset_hats), name)
            self.assertTrue(
                any(block["opcode"] == "looks_hide" for block in blocks.values()),
                name,
            )

    def test_reset_handlers_are_finite_and_legacy_begin_is_removed(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        stage = next(target for target in project["targets"] if target["isStage"])
        self.assertNotIn("begin", stage["broadcasts"].values())
        self.assertNotIn("death", (value[0] for value in stage["variables"].values()))
        loop_opcodes = {"control_forever", "control_repeat", "control_repeat_until"}
        for target in project["targets"]:
            blocks = target["blocks"]
            for hat_id, hat in blocks.items():
                if (
                    hat["opcode"] != "event_whenbroadcastreceived"
                    or hat["fields"].get("BROADCAST_OPTION", [None])[0]
                    != "director reset"
                ):
                    continue
                pending = [hat["next"]]
                seen = set()
                while pending:
                    block_id = pending.pop()
                    if block_id is None or block_id in seen:
                        continue
                    seen.add(block_id)
                    block = blocks[block_id]
                    self.assertNotIn(block["opcode"], loop_opcodes, target["name"])
                    pending.append(block["next"])
                    for name in ("SUBSTACK", "SUBSTACK2"):
                        if name in block["inputs"]:
                            pending.append(block["inputs"][name][1])

    def test_timed_state_completions_are_epoch_guarded(self) -> None:
        project = load_source(scratch.SOURCE_DIR)

        def variable_id(value: object) -> str | None:
            if (
                isinstance(value, list)
                and len(value) >= 2
                and value[0] == 3
                and isinstance(value[1], list)
                and len(value[1]) == 3
                and value[1][0] == 12
            ):
                return value[1][2]
            return None

        expected = {
            "solvalou": (director.SOLVALOU_EPOCH_ID, "ready complete"),
            "solv_death": (director.DEATH_EPOCH_ID, "death complete"),
        }
        for target in project["targets"]:
            if target["name"] not in expected:
                continue
            local_id, completion = expected[target["name"]]
            self.assertEqual(["entry epoch", 0], target["variables"][local_id])
            comparisons = [
                block
                for block in target["blocks"].values()
                if block["opcode"] == "operator_equals"
                and variable_id(block["inputs"].get("OPERAND1")) == local_id
                and variable_id(block["inputs"].get("OPERAND2")) == director.EPOCH_ID
            ]
            self.assertTrue(comparisons, target["name"])
            self.assertTrue(
                any(
                    block["opcode"] == "event_broadcast"
                    and block["inputs"]["BROADCAST_INPUT"][1][1] == completion
                    for block in target["blocks"].values()
                ),
                target["name"],
            )

    def test_every_scratch_block_reference_resolves(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        for target in project["targets"]:
            blocks = target["blocks"]
            for block_id, block in blocks.items():
                for field in ("next", "parent"):
                    reference = block[field]
                    if reference is not None:
                        self.assertIn(reference, blocks, f"{target['name']}:{block_id}.{field}")
                for input_name, value in block["inputs"].items():
                    if (
                        isinstance(value, list)
                        and len(value) >= 2
                        and value[0] in (1, 2, 3)
                        and isinstance(value[1], str)
                    ):
                        self.assertIn(
                            value[1],
                            blocks,
                            f"{target['name']}:{block_id}.{input_name}",
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

    def test_media_transfer_record_requires_provenance_attestation(self) -> None:
        record = self.temp / "missing-media-attestation.md"
        text = (
            ROOT / "docs" / "mechanics" / "001-sprite-sheet-library.md"
        ).read_text(encoding="utf-8")
        record.write_text(
            text.replace(mechanics.MEDIA_PROVENANCE_ATTESTATION, ""),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            mechanics.MechanicsRecordError,
            "required checked attestations",
        ):
            mechanics.validate_record(record)

    def test_mechanics_record_requires_source_copy_attestation(self) -> None:
        record = self.temp / "missing-source-copy-attestation.md"
        text = (
            ROOT / "docs" / "mechanics" / "001-sprite-sheet-library.md"
        ).read_text(encoding="utf-8")
        record.write_text(
            text.replace(mechanics.NO_SOURCE_COPY_ATTESTATION, ""),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            mechanics.MechanicsRecordError,
            "required checked attestations",
        ):
            mechanics.validate_record(record)

    def test_mechanics_record_requires_rom_handling_attestation(self) -> None:
        record = self.temp / "missing-rom-handling-attestation.md"
        text = (
            ROOT / "docs" / "mechanics" / "000-historical-baseline.md"
        ).read_text(encoding="utf-8")
        record.write_text(
            text.replace(mechanics.NO_ROM_HANDLING_ATTESTATION, ""),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            mechanics.MechanicsRecordError,
            "required checked attestations",
        ):
            mechanics.validate_record(record)

    def test_legacy_blanket_no_transfer_attestation_is_rejected(self) -> None:
        record = self.temp / "legacy-attestation.md"
        text = (
            ROOT / "docs" / "mechanics" / "000-historical-baseline.md"
        ).read_text(encoding="utf-8")
        text = text.replace(mechanics.NO_SOURCE_COPY_ATTESTATION, "")
        text = text.replace(
            mechanics.NO_ROM_HANDLING_ATTESTATION,
            "- [x] No external code, ROM data, or lookup tables were transferred.",
        )
        record.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(
            mechanics.MechanicsRecordError,
            "required checked attestations",
        ):
            mechanics.validate_record(record)

    def test_sprite_sheet_library_is_hidden_and_credited(self) -> None:
        project, _project_bytes, assets = scratch.validate_source()
        library = next(
            target
            for target in project["targets"]
            if target["name"] == "sprite_sheets"
        )
        self.assertFalse(library["visible"])
        self.assertEqual({}, library["blocks"])
        self.assertEqual([], library["sounds"])
        self.assertEqual(
            list(SPRITE_SHEET_HASHES),
            [costume["name"] for costume in library["costumes"]],
        )
        provenance = json.loads(
            (
                scratch.SOURCE_DIR
                / scratch.OVERLAY_DIRNAME
                / scratch.OVERLAY_PROVENANCE
            ).read_text(encoding="utf-8")
        )["assets"]
        self.assertTrue(
            {costume["md5ext"] for costume in library["costumes"]}
            <= set(provenance)
        )
        for costume in library["costumes"]:
            name = costume["name"]
            asset = costume["md5ext"]
            self.assertEqual(
                SPRITE_SHEET_HASHES[name],
                hashlib.sha256(assets[asset]).hexdigest(),
            )
            self.assertIn(
                "spriters-resource.com/arcade/xevious",
                provenance[asset]["origin"],
            )
            self.assertIn(
                "No reusable license specified",
                provenance[asset]["license"],
            )
            self.assertIn("did not create", provenance[asset]["notes"])

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
            "592345f70df1111eaed9bc182921e4a272854ba1cbdbf2c840f83b58078f027b",
            build_hash,
        )


if __name__ == "__main__":
    unittest.main()
