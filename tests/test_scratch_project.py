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
            elif target["name"] in {
                "solvalou",
                "solv_death",
                "blaster",
                "area_01a",
                "area_01b",
            }:
                # These targets carry director-managed variables (reload counter,
                # terrain scroll counters) added on top of their historical content.
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
        # The director-state surface stays exactly these five — a tight guard against
        # the Stage accumulating stray game state. SYS-04 adds a named allow-list of
        # machinery variables (the shared stream's state, its output, and the four
        # per-step working values a warp custom block cannot hold as locals); anything
        # outside both sets is an unreviewed addition and fails here.
        director_state_names = {
            "game state",
            "state epoch",
            "reset scope",
            "death outcome",
            "bomb in flight",
        }
        machinery_names = {
            "rng state",
            "rng out",
            "rng high",
            "rng new low",
            "rng new high",
            "rng extend",
            "slot index",
            "tick",
            "hit slot",
        }
        self.assertTrue(director_state_names.isdisjoint(machinery_names))
        stage_variable_names = {name for name, _value in stage["variables"].values()}
        self.assertEqual(director_state_names | machinery_names, stage_variable_names)
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

        def _proccode(definition: dict) -> str:
            prototype = stage["blocks"][definition["inputs"]["custom_block"][1]]
            return prototype["mutation"]["proccode"]

        # Exactly one Stage-owned transition procedure; every transition call routes
        # through it. (The Stage also defines the SYS-04 `rng step` warp block, which is
        # a reporter-free custom block with no caller this slice — not a transition.)
        transition_definitions = [
            block for block in definitions if _proccode(block) == director.PROCCODE
        ]
        self.assertEqual(1, len(transition_definitions))
        calls = [
            block
            for block in stage["blocks"].values()
            if block["opcode"] == "procedures_call"
        ]
        # Every state transition routes through the one transition procedure; the only
        # other Stage-owned calls are the SYS-02/04 machinery blocks (no state write).
        transition_calls = [
            block for block in calls if block["mutation"]["proccode"] == director.PROCCODE
        ]
        self.assertTrue(transition_calls)
        allowed_proccodes = {
            director.PROCCODE,
            director.CLEAR_SLOTS_PROCCODE,
            director.ADVANCE_SLOTS_PROCCODE,
            director.RESOLVE_HIT_PROCCODE,
            director.SCORE_PROCCODE,
        }
        self.assertTrue(
            all(block["mutation"]["proccode"] in allowed_proccodes for block in calls)
        )

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

    @staticmethod
    def _sys02_slot_failures(project: dict) -> set:
        """SYS-02 entity-slot machinery contract — the set of violated labels."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value for value in stage["lists"].values()}
        for label, name in (
            ("slot-type-list-64", "slot type"),
            ("slot-state-list-64", "slot state"),
        ):
            entry = by_name.get(name)
            if entry is None or len(entry[1]) != director.SLOT_COUNT:
                failures.add(label)
            elif any(item != 0 for item in entry[1]):
                failures.add(label)
        blocks = stage["blocks"]
        clear_proto = next(
            (
                b
                for b in blocks.values()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.CLEAR_SLOTS_PROCCODE
            ),
            None,
        )
        if clear_proto is None or clear_proto["mutation"].get("warp") != "true":
            failures.add("clear-slots-warp-defined")
        reset_receiver = any(
            b["opcode"] == "event_whenbroadcastreceived"
            and b["fields"]["BROADCAST_OPTION"][0] == "director reset"
            for b in blocks.values()
        )
        calls_clear = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.CLEAR_SLOTS_PROCCODE
            for b in blocks.values()
        )
        if not (reset_receiver and calls_clear):
            failures.add("reset-clears-slots")
        return failures

    def test_entity_slots_present_and_cleared(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._sys02_slot_failures(project))

    def test_entity_slot_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._sys02_slot_failures(base))

        def shrink_type_list(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for value in stage["lists"].values():
                if value[0] == "slot type":
                    value[1] = value[1][:-1]  # length 63

        def unwarp_clear(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode")
                    == director.CLEAR_SLOTS_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_clear_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode")
                    == director.CLEAR_SLOTS_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        cases = [
            ("slot-type-list-64", shrink_type_list),
            ("clear-slots-warp-defined", unwarp_clear),
            ("reset-clears-slots", drop_clear_call),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._sys02_slot_failures(project), label)

    @staticmethod
    def _central_walk_failures(project: dict) -> set:
        """SYS-04 centralized ordered update contract — violated labels."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        proto_id = next(
            (
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode")
                == director.ADVANCE_SLOTS_PROCCODE
            ),
            None,
        )
        if proto_id is None or blocks[proto_id]["mutation"].get("warp") != "true":
            failures.add("advance-slots-warp")
            return failures
        definition = next(
            (
                b
                for b in blocks.values()
                if b["opcode"] == "procedures_definition"
                and b["inputs"].get("custom_block", [None, None])[1] == proto_id
            ),
            None,
        )
        increments_tick = False
        repeat_times = None
        cursor = definition["next"] if definition else None
        while cursor:
            block = blocks[cursor]
            if (
                block["opcode"] == "data_changevariableby"
                and block["fields"]["VARIABLE"][0] == "tick"
            ):
                increments_tick = True
            if block["opcode"] == "control_repeat":
                times = block["inputs"].get("TIMES")
                if times and times[0] == 1:
                    repeat_times = int(float(times[1][1]))
            cursor = block["next"]
        if not increments_tick:
            failures.add("walk-advances-tick")
        if repeat_times != director.SLOT_COUNT:
            failures.add("walk-sweeps-all-slots")
        driven = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ADVANCE_SLOTS_PROCCODE
            for b in blocks.values()
        )
        if not driven:
            failures.add("walk-driven-while-playing")
        return failures

    def test_central_walk_is_atomic_ordered_pass(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._central_walk_failures(project))

    def test_central_walk_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._central_walk_failures(base))

        def unwarp_walk(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode")
                    == director.ADVANCE_SLOTS_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def shrink_sweep(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            proto_id = next(
                bid
                for bid, b in stage["blocks"].items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode")
                == director.ADVANCE_SLOTS_PROCCODE
            )
            definition = next(
                b
                for b in stage["blocks"].values()
                if b["opcode"] == "procedures_definition"
                and b["inputs"].get("custom_block", [None, None])[1] == proto_id
            )
            cursor = definition["next"]
            while cursor:
                block = stage["blocks"][cursor]
                if block["opcode"] == "control_repeat":
                    block["inputs"]["TIMES"] = [1, [4, 32]]
                cursor = block["next"]

        def drop_tick(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "data_changevariableby"
                    and b["fields"]["VARIABLE"][0] == "tick"
                ):
                    b["fields"]["VARIABLE"] = ["slot index", director.SLOT_INDEX_ID]

        cases = [
            ("advance-slots-warp", unwarp_walk),
            ("walk-sweeps-all-slots", shrink_sweep),
            ("walk-advances-tick", drop_tick),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._central_walk_failures(project), label)

    @staticmethod
    def _shot_cap_failures(project: dict) -> set:
        """A3 player-shot 3-cap contract (behavioral, beyond the B1/B8 shape checks)."""
        failures = set()
        blaster = next(t for t in project["targets"] if t["name"] == "blaster")
        blocks = blaster["blocks"]

        def chain_ops(first_id):
            ops, cursor = [], first_id
            while cursor:
                ops.append(blocks[cursor])
                cursor = blocks[cursor]["next"]
            return ops

        # Allocation writes the shot marker to exactly the three dedicated slots.
        alloc_indices = set()
        for b in blocks.values():
            if (
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][0] == "slot type"
            ):
                index, item = b["inputs"].get("INDEX"), b["inputs"].get("ITEM")
                if (
                    index
                    and index[0] == 1
                    and item
                    and item[0] == 1
                    and int(float(item[1][1])) == director.SHOT_TYPE
                ):
                    alloc_indices.add(int(float(index[1][1])))
        if alloc_indices != {37, 38, 39}:
            failures.add("shot-alloc-three-slots")

        # The spawn (create clone) AND the reload reset live only inside a branch guarded
        # by `alloc result > 0` — reload is consumed only on a successful allocation.
        guard_ok = False
        for b in blocks.values():
            if b["opcode"] != "control_if":
                continue
            condition = b["inputs"].get("CONDITION")
            if not condition or condition[0] != 2:
                continue
            guard = blocks[condition[1]]
            operand1 = guard["inputs"].get("OPERAND1") if guard["inputs"] else None
            if (
                guard["opcode"] == "operator_gt"
                and operand1
                and operand1[0] == 3
                and isinstance(operand1[1], list)
                and operand1[1][1] == "alloc result"
            ):
                substack = b["inputs"].get("SUBSTACK")
                branch = chain_ops(substack[1]) if substack else []
                spawns = any(o["opcode"] == "control_create_clone_of" for o in branch)
                resets_reload = any(
                    o["opcode"] == "data_setvariableto"
                    and o["fields"]["VARIABLE"][0] == "blaster reload"
                    and o["inputs"]["VALUE"] == [1, [4, 0]]
                    for o in branch
                )
                if spawns and resets_reload:
                    guard_ok = True
        if not guard_ok:
            failures.add("fire-consumes-reload-only-on-alloc")

        # The clone frees its slot (type -> 0 at its own `clone slot`) and deletes.
        frees_slot = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][0] == "slot type"
            and b["inputs"].get("ITEM") == [1, [4, 0]]
            and (b["inputs"].get("INDEX") or [None, None])[0] == 3
            and isinstance((b["inputs"]["INDEX"])[1], list)
            and b["inputs"]["INDEX"][1][1] == "clone slot"
            for b in blocks.values()
        )
        deletes = any(
            b["opcode"] == "control_delete_this_clone" for b in blocks.values()
        )
        if not (frees_slot and deletes):
            failures.add("clone-frees-slot")
        return failures

    def test_player_shot_cap_contract(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._shot_cap_failures(project))

    def test_player_shot_cap_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._shot_cap_failures(base))

        def misplace_alloc(p: dict) -> None:
            blaster = next(t for t in p["targets"] if t["name"] == "blaster")
            for b in blaster["blocks"].values():
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][0] == "slot type"
                    and b["inputs"].get("ITEM") == [1, [4, director.SHOT_TYPE]]
                    and b["inputs"]["INDEX"] == [1, [4, 37]]
                ):
                    b["inputs"]["INDEX"] = [1, [4, 50]]

        def unguard_fire(p: dict) -> None:
            # Point the fire guard at the reload counter instead of the alloc result —
            # the spawn/reset-reload would no longer be gated on a successful allocation.
            blaster = next(t for t in p["targets"] if t["name"] == "blaster")
            for b in blaster["blocks"].values():
                if b["opcode"] == "operator_gt":
                    operand1 = b["inputs"].get("OPERAND1")
                    if (
                        operand1
                        and operand1[0] == 3
                        and isinstance(operand1[1], list)
                        and operand1[1][1] == "alloc result"
                    ):
                        operand1[1][1] = "blaster reload"

        def keep_slot(p: dict) -> None:
            blaster = next(t for t in p["targets"] if t["name"] == "blaster")
            for b in blaster["blocks"].values():
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][0] == "slot type"
                    and b["inputs"].get("ITEM") == [1, [4, 0]]
                    and (b["inputs"].get("INDEX") or [None])[0] == 3
                ):
                    b["inputs"]["ITEM"] = [1, [4, director.SHOT_TYPE]]

        cases = [
            ("shot-alloc-three-slots", misplace_alloc),
            ("fire-consumes-reload-only-on-alloc", unguard_fire),
            ("clone-frees-slot", keep_slot),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._shot_cap_failures(project), label)

    @staticmethod
    def _sys03_failures(project: dict) -> set:
        """SYS-03 single-hit-resolution path — violated labels (foundation-only)."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        hit_writes = [
            b
            for b in blocks.values()
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][0] == "slot state"
            and b["inputs"].get("ITEM") == [1, [4, director.SLOT_HIT]]
        ]
        if len(hit_writes) != 1:  # a hit resolves exactly once, through one resolver
            failures.add("single-hit-resolver")
        score_calls = [
            b
            for b in blocks.values()
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.SCORE_PROCCODE
        ]
        if len(score_calls) != 1:  # all scoring flows through the one path
            failures.add("single-score-path")
        defined = {
            b["mutation"]["proccode"]
            for b in blocks.values()
            if b["opcode"] == "procedures_prototype"
        }
        if not {director.RESOLVE_HIT_PROCCODE, director.SCORE_PROCCODE} <= defined:
            failures.add("resolution-path-defined")
        return failures

    def test_collision_single_hit_path(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._sys03_failures(project))

    def test_collision_single_hit_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._sys03_failures(base))

        def double_score(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            call = next(
                b
                for b in stage["blocks"].values()
                if b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == director.SCORE_PROCCODE
            )
            clone = copy.deepcopy(call)
            stage["blocks"]["injected-second-score"] = clone

        def skip_hit_write(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][0] == "slot state"
                    and b["inputs"].get("ITEM") == [1, [4, director.SLOT_HIT]]
                ):
                    b["inputs"]["ITEM"] = [1, [4, 0]]

        cases = [
            ("single-score-path", double_score),
            ("single-hit-resolver", skip_hit_write),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._sys03_failures(project), label)

    def test_collision_groups_match_spec(self) -> None:
        # Exactly five groups, no others; each an (attacker range, victim range) over the
        # recorded slot ranges (core-game-systems SYS-03).
        groups = director.COLLISION_GROUPS
        self.assertEqual(len(groups), 5)
        player = (director.SOLVALOU_SLOT, director.SOLVALOU_SLOT)
        self.assertEqual(
            groups,
            (
                (director.SHOT_SLOTS, director.FLYING_SLOTS),
                ((director.BOMB_SLOT, director.BOMB_SLOT), director.GROUND_SLOTS),
                (director.BULLET_SLOTS, player),
                (director.FLYING_SLOTS, player),
                (director.BACURA_SLOTS, player),
            ),
        )

    def test_slot_ranges_match_capacities(self) -> None:
        # The 64-slot map and its binding capacities (player-craft-and-weapons.md),
        # reproduced as generator constants; also pins the arcade 0xNN <-> index NN+1.
        self.assertEqual(director.SLOT_COUNT, 64)
        span = lambda r: r[1] - r[0] + 1
        self.assertEqual(span(director.GROUND_SLOTS), 16)
        self.assertEqual(span(director.BACURA_SLOTS), 16)
        self.assertEqual(span(director.SHOT_SLOTS), 3)
        self.assertEqual(span(director.BULLET_SLOTS), 19)
        self.assertEqual(span(director.FLYING_SLOTS), 6)
        self.assertEqual(director.GROUND_SLOTS[0], 0x00 + 1)
        self.assertEqual(director.BOMB_SLOT, 0x21 + 1)
        self.assertEqual(director.SOLVALOU_SLOT, 0x23 + 1)
        self.assertEqual(director.SHOT_SLOTS[0], 0x24 + 1)
        self.assertEqual(director.FLYING_SLOTS[1], 0x3F + 1)

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

    @staticmethod
    def _numeric(value: object) -> int | float | None:
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[1], list)
            and len(value[1]) >= 2
        ):
            return value[1][1]
        return None

    def _regression_contract_failures(self, project: dict) -> set[str]:
        """Every restored behavior (audit B1-B10) and removed invention (A1-A2), as a
        static block-shape contract. Returns the set of violated labels — empty means
        the recovery is intact. Bounded structural coverage: it catches removal and
        shape drift of the asserted blocks, not shape-preserving behavioral drift; the
        operator playtest remains the real gameplay backstop.
        """
        blocks = {t["name"]: t["blocks"] for t in project["targets"]}
        num = self._numeric
        fails: set[str] = set()

        def has(name, pred):
            return any(pred(b) for b in blocks[name].values())

        def count(name, opcode, times=None):
            return sum(
                1
                for b in blocks[name].values()
                if b["opcode"] == opcode
                and (times is None or num(b["inputs"].get("TIMES")) == times)
            )

        def broadcasts(name, message):
            return has(
                name,
                lambda b: b["opcode"] == "event_broadcast"
                and b["inputs"].get("BROADCAST_INPUT", [None, [None, None, None]])[1][1]
                == message,
            )

        def receives(name, message):
            return has(
                name,
                lambda b: b["opcode"] == "event_whenbroadcastreceived"
                and b["fields"].get("BROADCAST_OPTION", [None])[0] == message,
            )

        def sets_var(name, var, value):
            return has(
                name,
                lambda b: b["opcode"] == "data_setvariableto"
                and b["fields"].get("VARIABLE", [None])[0] == var
                and num(b["inputs"].get("VALUE")) == value,
            )

        # A1 — READY bubble gone, its 30-tick beat kept as a tick-counted hold.
        if has("solvalou", lambda b: b["opcode"] == "looks_sayforsecs"):
            fails.add("A1-ready-bubble")
        if count("solvalou", "control_repeat", director.READY_HOLD_TICKS) != 1:
            fails.add("A1-ready-hold")
        # A2 — GAME OVER bubble gone.
        if has("solv_death", lambda b: b["opcode"] == "looks_sayforsecs"):
            fails.add("A2-gameover-bubble")

        # B1 — polled fire: no OS-repeat key hat, a space poll, the reload counter.
        if count("blaster", "event_whenkeypressed") != 0:
            fails.add("B1-key-hat")
        if not has("blaster", lambda b: b["opcode"] == "sensing_keypressed"):
            fails.add("B1-poll")
        if not sets_var("blaster", "blaster reload", director.RELOAD_TICKS):
            fails.add("B1-reload-prime")
        if not has(
            "blaster",
            lambda b: b["opcode"] == "operator_gt"
            and num(b["inputs"].get("OPERAND2")) == director.RELOAD_TICKS - 1,
        ):
            fails.add("B1-reload-gate")

        # B8 — one shot clone; expires at the top border at baseline speed; no waits.
        if count("blaster", "control_start_as_clone") != 1:
            fails.add("B8-clone")
        if not has(
            "blaster",
            lambda b: b["opcode"] == "sensing_touchingobjectmenu"
            and b["fields"].get("TOUCHINGOBJECTMENU", [None])[0] == "frame_t",
        ):
            fails.add("B8-top-expiry")
        if not has(
            "blaster",
            lambda b: b["opcode"] == "motion_changeyby" and num(b["inputs"].get("DY")) == 20,
        ):
            fails.add("B8-speed")
        if count("blaster", "control_wait") != 0:
            fails.add("B8-wall-clock")

        # B2 — single bomb: no clone, a guard armed and re-armed, the bomb broadcast.
        if count("bomb", "control_start_as_clone") != 0:
            fails.add("B2-clone")
        if not sets_var("bomb", "bomb in flight", 1):
            fails.add("B2-arm")
        if not sets_var("bomb", "bomb in flight", 0):
            fails.add("B2-rearm")
        if not has(
            "bomb",
            lambda b: b["opcode"] == "operator_equals"
            and num(b["inputs"].get("OPERAND2")) == 0
            and b["inputs"].get("OPERAND1", [None, [None, None]])[1][1] == "bomb in flight",
        ):
            fails.add("B2-idle-test")
        if not broadcasts("bomb", "bomb"):
            fails.add("B2-broadcast")

        # B6 — the crosshair receives the bomb and returns to its base costume.
        if not receives("target_a", "bomb"):
            fails.add("B6-crosshair-receive")
        if not has("target_a", lambda b: b["opcode"] == "looks_switchcostumeto"):
            fails.add("B6-crosshair-costume")

        # B7 — the impact marker receives the bomb and shows (was inert hide-only).
        if not receives("target_b", "bomb"):
            fails.add("B7-marker-receive")
        if not has("target_b", lambda b: b["opcode"] == "looks_show"):
            fails.add("B7-marker-show")

        # B3 — counted-cycle terrain; the fenced position test is gone; no waits.
        for strip in ("area_01a", "area_01b"):
            if not has(
                strip,
                lambda b: b["opcode"] == "operator_gt"
                and num(b["inputs"].get("OPERAND2")) == 689,
            ):
                fails.add(f"B3-count-{strip}")
            if has(strip, lambda b: b["opcode"] == "operator_lt"):
                fails.add(f"B3-position-test-{strip}")
            if count(strip, "control_wait") != 0:
                fails.add(f"B3-wall-clock-{strip}")

        # B4 — the title glides in.
        if not has("start_screen", lambda b: b["opcode"] == "motion_glidesecstoxy"):
            fails.add("B4-glide")

        # B5/B10 — tick-counted explosion holds then the post-death pause; no waits.
        if (
            count("solv_death", "control_repeat", director.EXPLOSION_HOLD_TICKS)
            != director.EXPLOSION_STEPS
        ):
            fails.add("B5B10-explosion")
        if count("solv_death", "control_repeat", director.POST_DEATH_PAUSE_TICKS) != 1:
            fails.add("B5B10-pause")
        if count("solv_death", "control_wait") != 0:
            fails.add("B5B10-wall-clock")

        # B9 — the craft fronts itself; terrain is sent back.
        if not has("solvalou", lambda b: b["opcode"] == "looks_gotofrontback"):
            fails.add("B9-craft-front")
        for strip in ("area_01a", "area_01b"):
            if not has(strip, lambda b: b["opcode"] == "looks_goforwardbackwardlayers"):
                fails.add(f"B9-terrain-back-{strip}")

        # Units rule: no wall-clock wait survives in any touched gameplay script (the
        # blaster/terrain/death checks above plus the bomb, crosshair, and marker).
        for name in ("bomb", "target_a", "target_b"):
            if count(name, "control_wait") != 0:
                fails.add(f"wall-clock-{name}")

        return fails

    def test_game_director_behavioral_contract_is_encoded(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        targets = {target["name"]: target for target in project["targets"]}

        # Retained structural guards.
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
        death = targets["solv_death"]["blocks"]
        self.assertIn("sound_play", {block["opcode"] for block in death.values()})
        self.assertNotIn(
            "sound_playuntildone", {block["opcode"] for block in death.values()}
        )
        self.assertTrue(
            any(
                block["opcode"] == "motion_goto"
                and death[block["inputs"]["TO"][1]]["fields"]["TO"][0] == "solvalou"
                for block in death.values()
            )
        )

        # The full regression-recovery contract (audit B1-B10, A1-A2).
        self.assertEqual(set(), self._regression_contract_failures(project))

    def test_regression_contract_negative_fixtures(self) -> None:
        """Prove the recovery contract can go red: break one restored behavior at a
        time and confirm the matching finding fires (principles: negative fixtures
        proving the tests can fail)."""
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._regression_contract_failures(base))

        def blocks_of(project, name):
            return next(t for t in project["targets"] if t["name"] == name)["blocks"]

        def first(project, name, predicate):
            return next(
                b for b in blocks_of(project, name).values() if predicate(b)
            )

        num = self._numeric

        def break_ready_bubble(p):  # A1: re-invent the READY speech bubble
            b = first(
                p,
                "solvalou",
                lambda b: b["opcode"] == "control_repeat"
                and num(b["inputs"].get("TIMES")) == director.READY_HOLD_TICKS,
            )
            b["opcode"] = "looks_sayforsecs"

        def break_reload_gate(p):  # B1: weaken the reload comparison
            b = first(
                p,
                "blaster",
                lambda b: b["opcode"] == "operator_gt"
                and num(b["inputs"].get("OPERAND2")) == director.RELOAD_TICKS - 1,
            )
            b["inputs"]["OPERAND2"] = [1, [4, 3]]

        def break_shot_expiry(p):  # B8: park the shot at the wrong edge
            b = first(
                p,
                "blaster",
                lambda b: b["opcode"] == "sensing_touchingobjectmenu"
                and b["fields"]["TOUCHINGOBJECTMENU"][0] == "frame_t",
            )
            b["fields"]["TOUCHINGOBJECTMENU"][0] = "frame_b"

        def break_bomb_broadcast(p):  # B2: drop the bomb broadcast
            b = first(
                p,
                "bomb",
                lambda b: b["opcode"] == "event_broadcast",
            )
            b["opcode"] = "control_wait"

        def break_terrain_count(p):  # B3: reinstate the fenced position test
            b = first(
                p,
                "area_01a",
                lambda b: b["opcode"] == "operator_gt"
                and num(b["inputs"].get("OPERAND2")) == 689,
            )
            b["opcode"] = "operator_lt"

        def break_title_glide(p):  # B4: snap the title into place
            b = first(
                p, "start_screen", lambda b: b["opcode"] == "motion_glidesecstoxy"
            )
            b["opcode"] = "motion_gotoxy"

        def break_death_pause(p):  # B10: remove the post-death pause
            b = first(
                p,
                "solv_death",
                lambda b: b["opcode"] == "control_repeat"
                and num(b["inputs"].get("TIMES")) == director.POST_DEATH_PAUSE_TICKS,
            )
            b["inputs"]["TIMES"] = [1, [4, 1]]

        def break_marker(p):  # B7: re-hide the impact marker
            b = first(p, "target_b", lambda b: b["opcode"] == "looks_show")
            b["opcode"] = "looks_hide"

        def break_craft_layer(p):  # B9: stop the craft fronting itself
            b = first(p, "solvalou", lambda b: b["opcode"] == "looks_gotofrontback")
            b["opcode"] = "looks_show"

        def break_gameover_bubble(p):  # A2: re-invent the GAME OVER bubble
            b = first(p, "solv_death", lambda b: b["opcode"] == "looks_show")
            b["opcode"] = "looks_sayforsecs"

        def break_crosshair_receive(p):  # B6: drop the crosshair bomb receiver
            b = first(
                p,
                "target_a",
                lambda b: b["opcode"] == "event_whenbroadcastreceived"
                and b["fields"]["BROADCAST_OPTION"][0] == "bomb",
            )
            b["fields"]["BROADCAST_OPTION"][0] = "target_t"

        def break_explosion_holds(p):  # B5: shorten one explosion hold
            b = first(
                p,
                "solv_death",
                lambda b: b["opcode"] == "control_repeat"
                and num(b["inputs"].get("TIMES")) == director.EXPLOSION_HOLD_TICKS,
            )
            b["inputs"]["TIMES"] = [1, [4, director.EXPLOSION_HOLD_TICKS + 1]]

        def break_bomb_arm(p):  # B2: fail to set the in-flight guard
            b = first(
                p,
                "bomb",
                lambda b: b["opcode"] == "data_setvariableto"
                and b["fields"]["VARIABLE"][0] == "bomb in flight"
                and num(b["inputs"].get("VALUE")) == 1,
            )
            b["inputs"]["VALUE"] = [1, [4, 2]]

        def break_terrain_layer(p):  # B9: stop sending terrain to the back
            for b in blocks_of(p, "area_01a").values():
                if b["opcode"] == "looks_goforwardbackwardlayers":
                    b["opcode"] = "looks_show"

        cases = [
            ("A1-ready-bubble", break_ready_bubble),
            ("A2-gameover-bubble", break_gameover_bubble),
            ("B1-reload-gate", break_reload_gate),
            ("B2-broadcast", break_bomb_broadcast),
            ("B2-arm", break_bomb_arm),
            ("B3-position-test-area_01a", break_terrain_count),
            ("B4-glide", break_title_glide),
            ("B5B10-explosion", break_explosion_holds),
            ("B5B10-pause", break_death_pause),
            ("B6-crosshair-receive", break_crosshair_receive),
            ("B7-marker-show", break_marker),
            ("B8-top-expiry", break_shot_expiry),
            ("B9-craft-front", break_craft_layer),
            ("B9-terrain-back-area_01a", break_terrain_layer),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            failures = self._regression_contract_failures(project)
            self.assertIn(label, failures, f"corruption '{label}' was not caught")

    def test_tick_constants_match_arcade_conversion(self) -> None:
        # 1 build tick = 2 arcade frames (core-game-systems units rule). Pin the
        # generator's tick constants to the arcade-frame values in their locked specs
        # with an independent expected value here, so a wrong constant fails this test
        # rather than moving the build and the shape assertions together silently.
        self.assertEqual(10, director.RELOAD_TICKS)  # WPN-01: 20-frame reload
        self.assertEqual(7, director.EXPLOSION_STEPS)  # PLY-02: 7 cycles
        self.assertEqual(4, director.EXPLOSION_HOLD_TICKS)  # PLY-02: 8-frame hold
        self.assertEqual(28, director.EXPLOSION_STEPS * director.EXPLOSION_HOLD_TICKS)  # 56 frames
        self.assertEqual(16, director.POST_DEATH_PAUSE_TICKS)  # PLY-02: 32-frame pause
        self.assertEqual(30, director.READY_HOLD_TICKS)  # project-defined 30-tick beat

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
            "9d6624f7b8879a7d1deb6d3926ec631a4e6c3d69a8b537e95014215680eb605c",
            build_hash,
        )


if __name__ == "__main__":
    unittest.main()
