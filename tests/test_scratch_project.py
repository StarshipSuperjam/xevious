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


def _proc_body_blocks(stage: dict, proccode: str) -> list:
    """Every block reachable from a custom-procedure definition's body (following `next` and
    every SUBSTACK / reporter input), so a structural check can inspect exactly one proc's stack.
    Returns the block dicts; empty when the proccode is absent."""
    blocks = stage["blocks"]
    proto_ids = {
        bid
        for bid, b in blocks.items()
        if b["opcode"] == "procedures_prototype"
        and b.get("mutation", {}).get("proccode") == proccode
    }
    definition = next(
        (
            b
            for b in blocks.values()
            if b["opcode"] == "procedures_definition"
            and b.get("inputs", {}).get("custom_block", [None, None])[1] in proto_ids
        ),
        None,
    )
    if definition is None:
        return []
    seen: set = set()
    frontier = [definition.get("next")]
    while frontier:
        bid = frontier.pop()
        if not bid or bid in seen or bid not in blocks:
            continue
        seen.add(bid)
        block = blocks[bid]
        frontier.append(block.get("next"))
        for value in block.get("inputs", {}).values():
            if isinstance(value, list) and len(value) >= 2 and isinstance(value[1], str):
                frontier.append(value[1])
    return [blocks[bid] for bid in seen]


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
        # 18: the historical 15 + the generated hud, the sprite-extraction proof, and the slice-8
        # toroid gameplay renderer (which reuses the proof's costumes by reference).
        self.assertEqual(18, len(project["targets"]))
        self.assertEqual(98, len(assets))

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
                # hud_glyphs.py appends one new "extend" sound on top of the
                # historical two (docs/mechanics/010); verify it precisely, then
                # drop sounds from the general preserved-content comparison.
                self.assertEqual(
                    [sound["name"] for sound in expected["sounds"]] + ["extend"],
                    [sound["name"] for sound in actual["sounds"]],
                )
                expected.pop("sounds")
                actual.pop("sounds")
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

    def test_runtime_identifier_manifest_is_current(self) -> None:
        # The committed manifest the JS harness reads must equal what the generator
        # emits from the current project, so a variable rename cannot leave the harness
        # reading a stale name. Regenerate with tools/game_director.py generate.
        project = load_source(scratch.SOURCE_DIR)
        expected = director.expected_project(project)
        self.assertEqual(
            director.MANIFEST_PATH.read_bytes(),
            director.manifest_bytes(expected),
        )

    def test_runtime_identifier_manifest_covers_scoped_duplicates(self) -> None:
        # Guards the harness's reason for existing: names that repeat across targets
        # ("entry epoch" on solvalou and solv_death; "scroll step" on both strips) must
        # resolve to distinct scoped entries, never collapse to one global name.
        project = load_source(scratch.SOURCE_DIR)
        manifest = director.identifier_manifest(director.expected_project(project))
        variables = manifest["variables"]
        entry_epochs = {
            vid: info for vid, info in variables.items() if info["name"] == "entry epoch"
        }
        self.assertEqual(
            {info["scope"] for info in entry_epochs.values()},
            {"solvalou", "solv_death"},
        )
        scroll_steps = {
            info["scope"] for info in variables.values() if info["name"] == "scroll step"
        }
        self.assertEqual(scroll_steps, {"area_01a", "area_01b"})

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
            "bullet alloc result",
            "bullet cursor",
            # ECO-01 award-value seam: set by the collision detector a later slice wires
            # (parallel to `hit slot`), so it is machinery, not Stage-write-protected state.
            "award value",
            # ECO-04 best-five verdict: Stage-computed only (never a sprite write, unlike
            # `award value`) — added to the write-forbid set below too.
            "qualified",
            # FORM-01 transient formation-lookup register (overwritten on every selection).
            "formation index",
            # DIF-02 transient score re-tune addend (computed then added to the AI level).
            "ai adjust",
            # AIR-01 Toroid live-combat machinery (slice 8): the aim quantizer's working vars, the
            # cached craft cell, the per-dispatch type register, and the spawner's cursor/attempt/
            # found registers — all transient, none Stage-write-protected state.
            "aim dx diff",
            "aim dy diff",
            "aim large",
            "aim small",
            "aim swap",
            "aim base",
            "aim fine",
            "aim index",
            "player row",
            "player col",
            "walk type",
            "spawn cursor",
            "spawn attempts",
            "spawn found",
        }
        # ECO economy state — Stage-written, HUD reads only. Held in its own category and
        # enforced Stage-only-write below (a HUD sprite writing `score` is the bug this guards).
        economy_names = {
            "score",
            "high score",
            "craft",
            "next bonus",
        }
        # AREA-01/AREA-02 area state — durable Stage-owned position/schedule authority read
        # across ticks and across the death/reset boundary. It is NOT machinery (the
        # sprite-writable working-register bucket): it is Stage-written, sprite-read, and
        # write-forbidden below, like the economy vars.
        area_state_names = {
            "area progress",
            "area number",
            "scroll row",
            "terrain column",
            "schedule cursor",
            "schedule fired",
        }
        # DIF-01/FORM-01 difficulty-director state — Stage-written, sprite-read, write-forbidden
        # (like area/economy state, NOT machinery): the accumulating AI level and the incoming
        # wave's size + type-table offset. `formation index` is the transient lookup register and
        # is machinery, above.
        difficulty_state_names = {
            "ai level",
            "formation count",
            "formation type offset",
            # DIF-03 per-family fire-permission masks + the ground-stop-firing row.
            "ground stop firing row",
            "fire mask derota",
            "fire mask logram",
            "fire mask zoshi",
            "fire mask terrazi",
            "fire mask kapi",
            "fire mask boza logram",
            "fire mask domogram",
            "fire mask andor genesis",
        }
        self.assertTrue(director_state_names.isdisjoint(machinery_names))
        self.assertTrue(economy_names.isdisjoint(machinery_names | director_state_names))
        self.assertTrue(
            area_state_names.isdisjoint(machinery_names | director_state_names | economy_names)
        )
        self.assertTrue(
            difficulty_state_names.isdisjoint(
                machinery_names | director_state_names | economy_names | area_state_names
            )
        )
        stage_variable_names = {name for name, _value in stage["variables"].values()}
        self.assertEqual(
            director_state_names
            | machinery_names
            | economy_names
            | area_state_names
            | difficulty_state_names,
            stage_variable_names,
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
        # The Stage's LIST surface is pinned as tightly as its variable surface: exactly these
        # lists, no strays. The reference/data tables among them are read-only authority and
        # are additionally sprite-write-forbidden below.
        stage_list_names = {name for name, _value in stage["lists"].values()}
        self.assertEqual(
            {
                "allowed transitions",
                "slot type",
                "slot state",
                # SYS-02 per-slot position/motion fields (slice 8).
                "slot x",
                "slot y",
                "slot dx",
                "slot dy",
                "slot timer",
                "slot code",
                "slot pts",
                "slot flag",
                # AIR-01/AIR-12 homing-aim tables (slice 8): the octant quantizer + two speed tiers.
                "octant table",
                "aim dy 24",
                "aim dx 24",
                "aim dy 32",
                "aim dx 32",
                "flying type table",
                "toroid frame",
                "value table",
                "starting lives",
                "first bonus 123",
                "first bonus 5",
                "repeat bonus 123",
                "repeat bonus 5",
                "high score table",
                "area map column",
                "schedule handler",
                "schedule trigger row",
                "schedule payload",
                "schedule arg",
                "area schedule start",
                "area schedule end",
                "difficulty increment",
                "formation count table",
                "formation type offset table",
            },
            stage_list_names,
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
            director.ADVANCE_AREA_PROCCODE,
            director.RESOLVE_HIT_PROCCODE,
            director.SCORE_PROCCODE,
            director.CHECK_BONUS_PROCCODE,
            # AIR-01 Toroid live-combat machinery (slice 8), all warp, no state write: the aim
            # quantizer, the craft-cell read, the spawner and its Toroid init/update/cull, and the
            # shared RNG step the spawn draw now consumes (its first live consumer).
            director.RNG_PROCCODE,
            director.COMPUTE_AIM_PROCCODE,
            director.READ_PLAYER_PROCCODE,
            director.SPAWN_FLYING_PROCCODE,
            director.INIT_TOROID_PROCCODE,
            director.UPDATE_TOROID_PROCCODE,
            director.CULL_SLOT_PROCCODE,
            # WPN-02 (slice 8): the shot-vs-air overlap detector and the struck-Toroid explosion tick.
            director.CHECK_AIR_HIT_PROCCODE,
            director.EXPLODE_TICK_PROCCODE,
        }
        self.assertTrue(
            all(block["mutation"]["proccode"] in allowed_proccodes for block in calls)
        )

        # Only the Stage writes the director-control vars AND the economy vars: no sprite may
        # write them (a HUD sprite touching `score` is exactly the bug this guards). The
        # award-value seam is deliberately absent — the enemy slice's detector (which may be a
        # sprite) sets it, like `hit slot`.
        director_variable_ids = {
            director.STATE_ID,
            director.EPOCH_ID,
            director.SCOPE_ID,
            director.OUTCOME_ID,
            director.SCORE_ID,
            director.HIGH_SCORE_ID,
            director.LIVES_ID,
            director.NEXT_BONUS_ID,
            director.QUALIFIED_ID,
            # AREA-01/AREA-02 area state: durable position/schedule authority, Stage-only-written.
            director.AREA_PROGRESS_ID,
            director.AREA_NUMBER_ID,
            director.SCROLL_ROW_ID,
            director.TERRAIN_COLUMN_ID,
            director.SCHEDULE_CURSOR_ID,
            director.SCHEDULE_FIRED_ID,
            # DIF-01/FORM-01/DIF-03 difficulty-director state: Stage-only-written.
            director.AI_LEVEL_ID,
            director.FORMATION_COUNT_ID,
            director.FORMATION_TYPE_OFFSET_ID,
            director.GROUND_STOP_FIRING_ROW_ID,
            *(mask_id for _suffix, _name, mask_id in director.FIRE_MASK_FAMILIES),
        }
        # Read-only reference tables: ingested, hash-pinned authority data no sprite may
        # mutate (the mutable slot lists are deliberately excluded — allocators write those).
        reference_list_ids = {
            director.VALUE_TABLE_ID,
            director.STARTING_LIVES_ID,
            director.FIRST_BONUS_123_ID,
            director.FIRST_BONUS_5_ID,
            director.REPEAT_BONUS_123_ID,
            director.REPEAT_BONUS_5_ID,
            director.HIGH_SCORE_TABLE_ID,
            director.AREA_MAP_COLUMN_ID,
            director.SCHEDULE_HANDLER_ID,
            director.SCHEDULE_TRIGGER_ROW_ID,
            director.SCHEDULE_PAYLOAD_ID,
            director.SCHEDULE_ARG_ID,
            director.AREA_SCHEDULE_START_ID,
            director.AREA_SCHEDULE_END_ID,
            director.DIFFICULTY_INCREMENT_ID,
            director.FORMATION_COUNT_TABLE_ID,
            director.FORMATION_TYPE_OFFSET_TABLE_ID,
        }
        list_write_opcodes = {
            "data_addtolist",
            "data_replaceitemoflist",
            "data_deleteoflist",
            "data_deletealloflist",
            "data_insertatlist",
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
            list_writes = {
                block["fields"].get("LIST", [None, None])[1]
                for block in target["blocks"].values()
                if block["opcode"] in list_write_opcodes
            }
            self.assertTrue(reference_list_ids.isdisjoint(list_writes), target["name"])

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
        # Every per-slot position/motion field is a length-64 all-zero list at generation (a slot
        # is initialized on allocation); a stray non-zero or wrong length would poison the walk.
        for _list_id, name in director.SLOT_FIELD_LISTS:
            entry = by_name.get(name)
            if entry is None or len(entry[1]) != director.SLOT_COUNT or any(i != 0 for i in entry[1]):
                failures.add("slot-field-lists-zero")
        blocks = stage["blocks"]
        # `clear slots` must zero EVERY registered slot list — not just type/state. A new field added
        # to the pool but omitted from clear-slots would break the seeded-replay clean slate silently.
        cleared_lists = {
            b["fields"]["LIST"][1]
            for b in _proc_body_blocks(stage, director.CLEAR_SLOTS_PROCCODE)
            if b["opcode"] == "data_replaceitemoflist"
        }
        every_slot_list = {
            director.SLOT_TYPE_ID,
            director.SLOT_STATE_ID,
            *(list_id for list_id, _name in director.SLOT_FIELD_LISTS),
        }
        if not every_slot_list <= cleared_lists:
            failures.add("clear-slots-covers-every-slot-list")
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

    # Roadmap closure evidence for leaf #57 (core.entity-lifecycle, SYS-02.live): the slice-8 live
    # entity now owns per-slot position/motion fields and a complete clear. The live-participant proof
    # (a Toroid occupying a slot through its lifecycle) is the harness scenario added with the walk.
    # roadmap-evidence: SYS-02 success  (test_entity_slots_present_and_cleared — fields present, clear covers every slot list)
    # roadmap-evidence: SYS-02 failure  (test_entity_slot_negative_fixtures — slot-field-lists-zero, clear-slots-covers-every-slot-list)
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

        def dirty_slot_field(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for value in stage["lists"].values():
                if value[0] == "slot dx":
                    value[1][0] = 7  # a non-zero at generation

        def drop_field_clear(p: dict) -> None:
            # Redirect one field's clear-write off the slot pool so clear-slots no longer covers it.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.CLEAR_SLOTS_PROCCODE):
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_DY_ID:
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        cases = [
            ("slot-type-list-64", shrink_type_list),
            ("clear-slots-warp-defined", unwarp_clear),
            ("reset-clears-slots", drop_clear_call),
            ("slot-field-lists-zero", dirty_slot_field),
            ("clear-slots-covers-every-slot-list", drop_field_clear),
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
    def _air01_failures(project: dict) -> set:
        """AIR-01 Toroid vertical-slice authoring contract — violated labels. Pins the structural
        facts that make the Toroid a faithful live entity: its lifecycle procedures run atomically,
        the formation spawner and the ordered walk actually drive it, the cull inherits scroll
        position (the coded refill), and the spawn-column draw is bounded so no seed can hang it."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def proto(proccode):
            return next(
                (
                    b
                    for b in blocks.values()
                    if b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == proccode
                ),
                None,
            )

        # (1) Every Toroid-lifecycle procedure exists and is warp (atomic) — a non-warp walk sub-proc
        # would yield mid-slot, letting a half-updated enemy render or be hit.
        for proccode in (
            director.INIT_TOROID_PROCCODE,
            director.UPDATE_TOROID_PROCCODE,
            director.SPAWN_FLYING_PROCCODE,
            director.CULL_SLOT_PROCCODE,
        ):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("toroid-lifecycle-procs-warp")

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        # (2) The spawner is driven, so a formation wave reaches live slots; (3) the ordered walk
        # dispatches to the updater, so a spawned Toroid actually advances.
        if not calls(director.SPAWN_FLYING_PROCCODE):
            failures.add("spawn-driven")
        if not calls(director.UPDATE_TOROID_PROCCODE):
            failures.add("dispatch-updates-toroid")

        # (4) The cull frees occupancy (type + state) but leaves the position fields, so a refilled
        # slot inherits the previous occupant's scroll-axis position — the coded refill deviation.
        cull_lists = {
            b["fields"]["LIST"][1]
            for b in _proc_body_blocks(stage, director.CULL_SLOT_PROCCODE)
            if b["opcode"] == "data_replaceitemoflist"
        }
        if not {director.SLOT_TYPE_ID, director.SLOT_STATE_ID} <= cull_lists:
            failures.add("cull-frees-occupancy")
        if {director.SLOT_X_ID, director.SLOT_Y_ID} & cull_lists:
            failures.add("cull-keeps-position")

        # (5) The spawn-column draw is bounded: init toroid draws inside a repeat-until AND increments
        # an attempt counter, so an unlucky seed cannot spin the warp thread forever (16-attempt cap).
        init_body = _proc_body_blocks(stage, director.INIT_TOROID_PROCCODE)
        has_until = any(b["opcode"] == "control_repeat_until" for b in init_body)
        counts_attempts = any(
            b["opcode"] == "data_changevariableby"
            and b["fields"]["VARIABLE"][0] == "spawn attempts"
            for b in init_body
        )
        if not (has_until and counts_attempts):
            failures.add("spawn-draw-bounded")
        return failures

    # Roadmap closure evidence for leaf #65 (air.toroid, AIR-01.toroid): the Toroid is a live entity
    # — spawned from the formation wave, advanced by the ordered walk, culled with inherited scroll
    # position, its spawn draw bounded. The live proof (spawns, moves, six clones) is the harness
    # scenario `toroid-wave-spawns-and-moves`; the seeded draw order is `rng-draw-order`.
    # roadmap-evidence: AIR-01 success  (test_toroid_slice_authoring_present — lifecycle procs, spawn+dispatch driven, cull inherits position, bounded draw)
    # roadmap-evidence: AIR-01 failure  (test_toroid_slice_negative_fixtures — each contract clause corrupted bites)
    # This commit also makes SYS-04 a live consumer (the ordered walk now dispatches an occupant to
    # `update toroid`, and the spawner draws the shared RNG in walk order) and lights the AREA-02 air
    # path (the formation wave, not add_object, spawns live flying enemies). Both are proven live in
    # the harness (`toroid-wave-spawns-and-moves`, `rng-draw-order`), each with a biting negative.
    # roadmap-evidence: SYS-04 success  (test_toroid_slice_authoring_present dispatch/spawn-driven clauses; harness toroid-wave-spawns-and-moves + rng-draw-order run live)
    # roadmap-evidence: SYS-04 failure  (test_toroid_slice_negative_fixtures dispatch-updates-toroid; harness rng-draw-order neutralizes `rng step`)
    # roadmap-evidence: AREA-02 success  (test_toroid_slice_authoring_present spawn-driven clause; harness toroid-wave-spawns-and-moves fills flying slots from the formation)
    # roadmap-evidence: AREA-02 failure  (test_toroid_slice_negative_fixtures spawn-driven; harness toroid-wave-spawns-and-moves neutralizes `update toroid`)
    def test_toroid_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air01_failures(project))

    def test_toroid_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air01_failures(base))

        def unwarp_update(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode")
                    == director.UPDATE_TOROID_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_spawn_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode")
                    == director.SPAWN_FLYING_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_dispatch_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode")
                    == director.UPDATE_TOROID_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def cull_skips_type(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.CULL_SLOT_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TYPE_ID
                ):
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def cull_clears_position(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.CULL_SLOT_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                ):
                    b["fields"]["LIST"] = ["slot x", director.SLOT_X_ID]

        def drop_attempts_count(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.INIT_TOROID_PROCCODE):
                if (
                    b["opcode"] == "data_changevariableby"
                    and b["fields"]["VARIABLE"][0] == "spawn attempts"
                ):
                    b["fields"]["VARIABLE"] = ["spawn found", director.SPAWN_FOUND_ID]

        cases = [
            ("toroid-lifecycle-procs-warp", unwarp_update),
            ("spawn-driven", drop_spawn_call),
            ("dispatch-updates-toroid", drop_dispatch_call),
            ("cull-frees-occupancy", cull_skips_type),
            ("cull-keeps-position", cull_clears_position),
            ("spawn-draw-bounded", drop_attempts_count),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air01_failures(project), label)

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
        # The one slot-state -> HIT write must live inside `resolve hit` (not merely
        # exist somewhere on the Stage) — a hit resolves exactly once, through the one
        # resolver.
        resolve_proto = next(
            (
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode")
                == director.RESOLVE_HIT_PROCCODE
            ),
            None,
        )
        resolve_body = set()
        if resolve_proto is not None:
            definition = next(
                (
                    b
                    for b in blocks.values()
                    if b["opcode"] == "procedures_definition"
                    and b["inputs"].get("custom_block", [None, None])[1] == resolve_proto
                ),
                None,
            )
            cursor = definition["next"] if definition else None
            while cursor:
                resolve_body.add(cursor)
                cursor = blocks[cursor]["next"]
        hit_writes = [
            bid
            for bid, b in blocks.items()
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][0] == "slot state"
            and b["inputs"].get("ITEM") == [1, [4, director.SLOT_HIT]]
        ]
        if len(hit_writes) != 1 or hit_writes[0] not in resolve_body:
            failures.add("single-hit-resolver")
        # SYS-03's guarantee: a resolved hit scores exactly once — the `score` call lives in
        # the resolver body, once. (The one `score` PROC is the single scoring path; ECO-01's
        # contract enforces that nothing writes `score` outside it. Other legitimate callers of
        # that proc — e.g. the debug scoring fixture — are ECO-01's concern, not SYS-03's.)
        score_calls_in_resolver = [
            bid
            for bid in resolve_body
            if blocks[bid]["opcode"] == "procedures_call"
            and blocks[bid].get("mutation", {}).get("proccode") == director.SCORE_PROCCODE
        ]
        if len(score_calls_in_resolver) != 1:  # one hit resolves to one award
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
            # Make the resolver score TWICE for one hit — the SYS-03 violation.
            stage = next(t for t in p["targets"] if t["isStage"])
            blocks = stage["blocks"]
            proto = next(
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.RESOLVE_HIT_PROCCODE
            )
            definition = next(
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "procedures_definition"
                and b["inputs"].get("custom_block", [None, None])[1] == proto
            )
            cursor = blocks[definition]["next"]
            score_call_id = None
            while cursor:
                b = blocks[cursor]
                if b["opcode"] == "procedures_call" and b.get("mutation", {}).get(
                    "proccode"
                ) == director.SCORE_PROCCODE:
                    score_call_id = cursor
                    break
                cursor = b["next"]
            clone = copy.deepcopy(blocks[score_call_id])
            clone["next"] = blocks[score_call_id]["next"]
            blocks[score_call_id]["next"] = "injected-second-resolver-score"
            blocks["injected-second-resolver-score"] = clone

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

    @staticmethod
    def _wpn02_failures(project: dict) -> set:
        """WPN-02 blaster-to-air kill contract — violated labels. The detector runs in the walk, the
        shot mirrors its position for it to read, and a struck enemy explodes then frees."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def proto_warp(proccode):
            p = next(
                (
                    b
                    for b in blocks.values()
                    if b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == proccode
                ),
                None,
            )
            return p is not None and p["mutation"].get("warp") == "true"

        # (1) The detector and the explosion tick exist and are atomic (warp) — a mid-slot yield could
        # score a half-resolved hit or render a torn explosion.
        if not (proto_warp(director.CHECK_AIR_HIT_PROCCODE) and proto_warp(director.EXPLODE_TICK_PROCCODE)):
            failures.add("air-combat-procs-warp")

        # (2) The detector produces award value from the value table and resolves through the one path.
        detector = _proc_body_blocks(stage, director.CHECK_AIR_HIT_PROCCODE)
        sets_award = any(
            b["opcode"] == "data_setvariableto"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.AWARD_VALUE_ID
            for b in detector
        )
        reads_table = any(
            b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] == director.VALUE_TABLE_ID
            for b in detector
        )
        resolves = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.RESOLVE_HIT_PROCCODE
            for b in detector
        )
        if not (sets_award and reads_table and resolves):
            failures.add("air-hit-awards-and-resolves")

        # (3) The struck-enemy branch frees the slot: the explosion tick culls when its clock elapses.
        explode = _proc_body_blocks(stage, director.EXPLODE_TICK_PROCCODE)
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
            for b in explode
        ):
            failures.add("explosion-frees-slot")

        # (4) `update toroid` runs the explosion for a HIT enemy (the HIT branch calls the explode tick).
        update = _proc_body_blocks(stage, director.UPDATE_TOROID_PROCCODE)
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.EXPLODE_TICK_PROCCODE
            for b in update
        ):
            failures.add("hit-runs-explosion")

        # (5) The blaster shot mirrors its live position into BOTH slot x and slot y (indexed by its
        # clone slot), so the walk's detector can read the shot from the slot lists.
        blaster = next(t for t in project["targets"] if t.get("name") == "blaster")
        mirrored = {
            b["fields"]["LIST"][1]
            for b in blaster["blocks"].values()
            if b["opcode"] == "data_replaceitemoflist"
            and b["inputs"].get("INDEX", [None, [None, None]])[1] == [12, "clone slot", director.CLONE_SLOT_ID]
        }
        if not {director.SLOT_X_ID, director.SLOT_Y_ID} <= mirrored:
            failures.add("shot-position-mirrored")
        return failures

    # Roadmap closure evidence for leaves #62 (air.hit, WPN-02.air-hit), #63 (ECO-01 air awards), and
    # #58 (SYS-03.live): the blaster-to-air hit resolves through the single score path, awards the
    # enemy's value, and explodes then frees the slot. Live proof (a kill scores 30 and the enemy is
    # gone) is the harness `blaster-kills-toroid-and-scores`; the S fixture is retired.
    # roadmap-evidence: WPN-02 success  (test_air_kill_contract_present — detector, award, explosion, mirror)
    # roadmap-evidence: WPN-02 failure  (test_air_kill_negative_fixtures — each clause corrupted bites)
    # roadmap-evidence: SYS-03 success  (test_collision_single_hit_path — one HIT write in resolve hit, one score call; now with a live detector driving it)
    # roadmap-evidence: SYS-03 failure  (test_collision_single_hit_negative_fixtures; harness blaster-kills-toroid-and-scores negative)
    # roadmap-evidence: ECO-01 success  (test_air_kill_contract_present award-from-value-table clause; harness blaster-kills-toroid-and-scores raises the score by the enemy value)
    # roadmap-evidence: ECO-01 failure  (test_air_kill_negative_fixtures air-hit-awards-and-resolves; harness negative widens the window so no kill scores)
    def test_air_kill_contract_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._wpn02_failures(project))

    def test_air_kill_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._wpn02_failures(base))

        def unwarp_detector(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.CHECK_AIR_HIT_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_resolve(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.CHECK_AIR_HIT_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.RESOLVE_HIT_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_explosion_cull(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.EXPLODE_TICK_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_hit_branch(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TOROID_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.EXPLODE_TICK_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_mirror(p: dict) -> None:
            blaster = next(t for t in p["targets"] if t.get("name") == "blaster")
            for b in blaster["blocks"].values():
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_X_ID
                    and b["inputs"].get("INDEX", [None, [None, None]])[1]
                    == [12, "clone slot", director.CLONE_SLOT_ID]
                ):
                    b["fields"]["LIST"] = ["slot timer", director.SLOT_TIMER_ID]

        cases = [
            ("air-combat-procs-warp", unwarp_detector),
            ("air-hit-awards-and-resolves", drop_resolve),
            ("explosion-frees-slot", drop_explosion_cull),
            ("hit-runs-explosion", drop_hit_branch),
            ("shot-position-mirrored", drop_mirror),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._wpn02_failures(project), label)

    @staticmethod
    def _eco01_failures(project: dict) -> set:
        """ECO-01 single scoring path — award, 9,999,990 cap, high-score track, bonus tail,
        and no score bypass. Structure only; the arithmetic is the operator's playtest."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def refs(spec, var_id: str) -> bool:
            return (
                isinstance(spec, list)
                and len(spec) >= 2
                and isinstance(spec[1], list)
                and len(spec[1]) >= 3
                and spec[1][0] == 12
                and spec[1][2] == var_id
            )

        proto = next(
            (
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.SCORE_PROCCODE
            ),
            None,
        )
        definition = (
            None
            if proto is None
            else next(
                (
                    bid
                    for bid, b in blocks.items()
                    if b["opcode"] == "procedures_definition"
                    and b["inputs"].get("custom_block", [None, None])[1] == proto
                ),
                None,
            )
        )
        if definition is None:
            return {"score-proc-defined"}
        body, cur = [], blocks[definition]["next"]
        while cur:
            body.append(cur)
            cur = blocks[cur]["next"]

        # award -> score: `change score by (award value)`. (NOT `set score = score + award
        # value`: a `set var = operator(...)` value-input does not evaluate in the Scratch VM,
        # so the score path adds via `change ... by` the award-value variable directly.)
        if not any(
            b["opcode"] == "data_changevariableby"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.SCORE_ID
            and refs(b["inputs"].get("VALUE"), director.AWARD_VALUE_ID)
            for b in blocks.values()
        ):
            failures.add("score-add-award")

        # cap: a `score > 9,999,990` test and a set-score to the ceiling literal.
        gt_cap = any(
            b["opcode"] == "operator_gt"
            and refs(b["inputs"].get("OPERAND1"), director.SCORE_ID)
            and b["inputs"].get("OPERAND2") == [1, [4, director.SCORE_CAP]]
            for b in blocks.values()
        )
        set_cap = any(
            b["opcode"] == "data_setvariableto"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.SCORE_ID
            and b["inputs"].get("VALUE") == [1, [4, director.SCORE_CAP]]
            for b in blocks.values()
        )
        if not (gt_cap and set_cap):
            failures.add("score-cap")

        # high score: a `score > high score` test and a set-high-score to `score`.
        gt_high = any(
            b["opcode"] == "operator_gt"
            and refs(b["inputs"].get("OPERAND1"), director.SCORE_ID)
            and refs(b["inputs"].get("OPERAND2"), director.HIGH_SCORE_ID)
            for b in blocks.values()
        )
        set_high = any(
            b["opcode"] == "data_setvariableto"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.HIGH_SCORE_ID
            and refs(b["inputs"].get("VALUE"), director.SCORE_ID)
            for b in blocks.values()
        )
        if not (gt_high and set_high):
            failures.add("high-score-track")

        # the bonus-life check runs after every award — the tail of the score path.
        if not (
            body
            and blocks[body[-1]]["opcode"] == "procedures_call"
            and blocks[body[-1]].get("mutation", {}).get("proccode")
            == director.CHECK_BONUS_PROCCODE
        ):
            failures.add("bonus-check-tail")

        # no bypass: the ONLY `change score by` in the whole project is the single award inside
        # the score proc — a second one anywhere is the classic scoring bypass.
        score_changes = [
            bid
            for target in project["targets"]
            for bid, b in target["blocks"].items()
            if b["opcode"] == "data_changevariableby"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.SCORE_ID
        ]
        if len(score_changes) != 1 or score_changes[0] not in body:
            failures.add("score-no-bypass")
        return failures

    def test_scoring_path_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco01_failures(project))

    def test_scoring_path_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco01_failures(base))
        stage = next(t for t in base["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def break_award(p: dict) -> None:
            s = next(t for t in p["targets"] if t["isStage"])
            chg = next(
                b
                for b in s["blocks"].values()
                if b["opcode"] == "data_changevariableby"
                and b["fields"].get("VARIABLE", [None, None])[1] == director.SCORE_ID
            )
            chg["inputs"]["VALUE"] = [1, [4, 0]]  # award nothing, not `award value`

        def break_cap(p: dict) -> None:
            for b in next(t for t in p["targets"] if t["isStage"])["blocks"].values():
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.SCORE_ID
                    and b["inputs"].get("VALUE") == [1, [4, director.SCORE_CAP]]
                ):
                    b["inputs"]["VALUE"] = [1, [4, director.SCORE_CAP + 10]]

        def break_high(p: dict) -> None:
            for b in next(t for t in p["targets"] if t["isStage"])["blocks"].values():
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1]
                    == director.HIGH_SCORE_ID
                ):
                    b["fields"]["VARIABLE"] = ["score", director.SCORE_ID]

        def break_bonus_tail(p: dict) -> None:
            s = next(t for t in p["targets"] if t["isStage"])
            proto = next(
                bid
                for bid, b in s["blocks"].items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.SCORE_PROCCODE
            )
            definition = next(
                bid
                for bid, b in s["blocks"].items()
                if b["opcode"] == "procedures_definition"
                and b["inputs"].get("custom_block", [None, None])[1] == proto
            )
            cur = s["blocks"][definition]["next"]
            prev = definition
            while s["blocks"][cur]["next"]:
                prev = cur
                cur = s["blocks"][cur]["next"]
            s["blocks"][prev]["next"] = None  # drop the trailing check-bonus call

        def inject_bypass(p: dict) -> None:
            s = next(t for t in p["targets"] if t["isStage"])
            s["blocks"]["injected-score-bypass"] = {
                "opcode": "data_changevariableby",
                "next": None,
                "parent": None,
                "inputs": {"VALUE": [1, [4, 100]]},
                "fields": {"VARIABLE": ["score", director.SCORE_ID]},
                "shadow": False,
                "topLevel": False,
            }

        cases = [
            ("score-add-award", break_award),
            ("score-cap", break_cap),
            ("high-score-track", break_high),
            ("bonus-check-tail", break_bonus_tail),
            ("score-no-bypass", inject_bypass),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._eco01_failures(project), label)

    def test_value_table_matches_scores_json(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value[1] for value in stage["lists"].values()}
        data = json.loads((ROOT / "docs" / "spec" / "data" / "scores.json").read_text())
        expected = [e["points"] for e in data["tables"]["master_value_table"]["entries"]]
        self.assertEqual(expected, by_name["value table"])

    def test_air_hit_replaces_score_fixture_as_award_producer(self) -> None:
        # The debug S fixture is retired in slice 8: the real producer of `award value` is now the
        # blaster-to-air detector, which reads the struck enemy's `slot pts` into the value table and
        # resolves the hit through the one `score` path. The S key hat must be gone entirely.
        project = load_source(scratch.SOURCE_DIR)
        stage = next(t for t in project["targets"] if t["isStage"])
        s_hats = [
            b
            for b in stage["blocks"].values()
            if b["opcode"] == "event_whenkeypressed"
            and b["fields"].get("KEY_OPTION", [None])[0] == "s"
        ]
        self.assertEqual([], s_hats, "the debug S scoring fixture is removed")
        body = _proc_body_blocks(stage, director.CHECK_AIR_HIT_PROCCODE)
        sets_award = any(
            b["opcode"] == "data_setvariableto"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.AWARD_VALUE_ID
            for b in body
        )
        reads_value_table = any(
            b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] == director.VALUE_TABLE_ID
            for b in body
        )
        calls_resolve = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.RESOLVE_HIT_PROCCODE
            for b in body
        )
        self.assertTrue(sets_award, "the detector produces award value")
        self.assertTrue(reads_value_table, "award value comes from the value table (slot pts)")
        self.assertTrue(calls_resolve, "the detector resolves the hit through the one score path")

    @staticmethod
    def _eco03_failures(project: dict) -> set:
        """ECO-03 lives/bonus economy — enabled guard, cap quirk, threshold grant + advance,
        the grant's craft/sound/signal, and the DIP-seeded starting craft and first threshold."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        vals = list(blocks.values())

        def refs(spec, var_id: str) -> bool:
            return (
                isinstance(spec, list)
                and len(spec) >= 2
                and isinstance(spec[1], list)
                and len(spec[1]) >= 3
                and spec[1][0] == 12
                and spec[1][2] == var_id
            )

        def sets_from_list(var_id: str, list_name: str) -> bool:
            for b in vals:
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1] == var_id
                ):
                    val = b["inputs"].get("VALUE")
                    if isinstance(val, list) and len(val) >= 2 and isinstance(val[1], str):
                        child = blocks.get(val[1], {})
                        if (
                            child.get("opcode") == "data_itemoflist"
                            and child["fields"].get("LIST", [None])[0] == list_name
                        ):
                            return True
            return False

        # the bonus check only runs when enabled (threshold sentinel non-zero).
        if not any(
            b["opcode"] == "operator_gt"
            and refs(b["inputs"].get("OPERAND1"), director.NEXT_BONUS_ID)
            and b["inputs"].get("OPERAND2") == [1, [4, director.BONUS_DISABLED]]
            for b in vals
        ):
            failures.add("bonus-enabled-guard")
        # cap quirk: an at-cap test (score == 9,999,990) drives an every-award grant branch.
        if not any(
            b["opcode"] == "operator_equals"
            and refs(b["inputs"].get("OPERAND1"), director.SCORE_ID)
            and b["inputs"].get("OPERAND2") == [1, [4, director.SCORE_CAP]]
            for b in vals
        ):
            failures.add("cap-quirk")
        # grant: +1 craft, the extend sound, and the craft-changed HUD signal.
        if not any(
            b["opcode"] == "data_changevariableby"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID
            and b["inputs"].get("VALUE") == [1, [4, 1]]
            for b in vals
        ):
            failures.add("bonus-craft-grant")
        if not any(
            b["opcode"] == "sound_sounds_menu"
            and b["fields"].get("SOUND_MENU", [None])[0] == "extend"
            for b in vals
        ):
            failures.add("bonus-extend-sound")
        if not any(
            b["opcode"] == "event_broadcast"
            and b["inputs"].get("BROADCAST_INPUT", [None, [None, None]])[1][1] == "craft changed"
            for b in vals
        ):
            failures.add("bonus-craft-changed")
        # advance: next bonus += the per-setting increment read from the repeat table.
        advance = any(
            b["opcode"] == "data_setvariableto"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.NEXT_BONUS_ID
            and isinstance(b["inputs"].get("VALUE"), list)
            and isinstance(b["inputs"]["VALUE"][1], str)
            and blocks.get(b["inputs"]["VALUE"][1], {}).get("opcode") == "operator_add"
            for b in vals
        )
        repeat_read = any(
            b["opcode"] == "data_itemoflist"
            and b["fields"].get("LIST", [None])[0] == "repeat bonus 123"
            for b in vals
        )
        if not (advance and repeat_read):
            failures.add("bonus-advance")
        # DIP seeds: starting craft and the first threshold, read from the ingested tables.
        if not sets_from_list(director.LIVES_ID, "starting lives"):
            failures.add("lives-seeded")
        if not sets_from_list(director.NEXT_BONUS_ID, "first bonus 123"):
            failures.add("bonus-seeded")
        return failures

    def test_bonus_economy_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco03_failures(project))

    def test_bonus_economy_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco03_failures(base))

        def first(pred):
            def f(p):
                stage = next(t for t in p["targets"] if t["isStage"])
                return next(b for b in stage["blocks"].values() if pred(b))
            return f

        def each(p, pred):
            stage = next(t for t in p["targets"] if t["isStage"])
            return [b for b in stage["blocks"].values() if pred(b)]

        def break_guard(p):
            b = first(
                lambda b: b["opcode"] == "operator_gt"
                and b["inputs"].get("OPERAND2") == [1, [4, director.BONUS_DISABLED]]
                and isinstance(b["inputs"].get("OPERAND1"), list)
                and b["inputs"]["OPERAND1"][1][2] == director.NEXT_BONUS_ID
            )(p)
            b["inputs"]["OPERAND1"] = [1, [4, 1]]

        def break_cap(p):
            b = first(
                lambda b: b["opcode"] == "operator_equals"
                and b["inputs"].get("OPERAND2") == [1, [4, director.SCORE_CAP]]
            )(p)
            b["inputs"]["OPERAND2"] = [1, [4, 0]]

        def break_grant(p):
            # the grant is emitted at both branches (cap quirk + normal) — break every one.
            for b in each(
                p,
                lambda b: b["opcode"] == "data_changevariableby"
                and b["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID,
            ):
                b["inputs"]["VALUE"] = [1, [4, 0]]

        def break_sound(p):
            for b in each(
                p,
                lambda b: b["opcode"] == "sound_sounds_menu"
                and b["fields"].get("SOUND_MENU", [None])[0] == "extend",
            ):
                b["fields"]["SOUND_MENU"] = ["pop", None]

        def break_signal(p):
            for b in each(
                p,
                lambda b: b["opcode"] == "event_broadcast"
                and b["inputs"].get("BROADCAST_INPUT", [None, [None, None]])[1][1]
                == "craft changed",
            ):
                b["inputs"]["BROADCAST_INPUT"][1][1] = "director stop"

        def break_advance(p):
            b = first(
                lambda b: b["opcode"] == "data_itemoflist"
                and b["fields"].get("LIST", [None])[0] == "repeat bonus 123"
            )(p)
            b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def break_lives_seed(p):
            b = first(
                lambda b: b["opcode"] == "data_itemoflist"
                and b["fields"].get("LIST", [None])[0] == "starting lives"
            )(p)
            b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def break_bonus_seed(p):
            b = first(
                lambda b: b["opcode"] == "data_itemoflist"
                and b["fields"].get("LIST", [None])[0] == "first bonus 123"
            )(p)
            b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        cases = [
            ("bonus-enabled-guard", break_guard),
            ("cap-quirk", break_cap),
            ("bonus-craft-grant", break_grant),
            ("bonus-extend-sound", break_sound),
            ("bonus-craft-changed", break_signal),
            ("bonus-advance", break_advance),
            ("lives-seeded", break_lives_seed),
            ("bonus-seeded", break_bonus_seed),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._eco03_failures(project), label)

    def test_bonus_and_lives_data_match_scores_json(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value[1] for value in stage["lists"].values()}
        data = json.loads((ROOT / "docs" / "spec" / "data" / "scores.json").read_text())["tables"]

        def sentinel(values: list) -> list:
            return [director.BONUS_DISABLED if v is None else v for v in values]

        self.assertEqual(data["starting_lives"]["values"], by_name["starting lives"])
        self.assertEqual(
            sentinel(data["first_bonus_thresholds"]["table_123"]), by_name["first bonus 123"]
        )
        self.assertEqual(
            sentinel(data["first_bonus_thresholds"]["table_5"]), by_name["first bonus 5"]
        )
        self.assertEqual(
            sentinel(data["repeat_bonus_increments"]["table_123"]), by_name["repeat bonus 123"]
        )
        self.assertEqual(
            sentinel(data["repeat_bonus_increments"]["table_5"]), by_name["repeat bonus 5"]
        )

    @staticmethod
    def _eco02_failures(project: dict) -> set:
        """ECO-02 HUD render — clone spawning/dispatch, the score and high-score digit
        costume-switch expressions, the craft/craft-changed life-icon wiring, the
        flashing 1UP label, and the HUD's read-only invariant (structure only; the
        arithmetic and on-screen layout are the operator's playtest)."""
        failures = set()
        hud = next(t for t in project["targets"] if t.get("name") == "hud")
        blocks = hud["blocks"]

        def refs(spec, var_id: str) -> bool:
            return (
                isinstance(spec, list)
                and len(spec) >= 2
                and isinstance(spec[1], list)
                and len(spec[1]) >= 3
                and spec[1][0] == 12
                and spec[1][2] == var_id
            )

        if not any(b["opcode"] == "control_create_clone_of" for b in blocks.values()):
            failures.add("hud-spawns-clones")
        if not any(b["opcode"] == "control_start_as_clone" for b in blocks.values()):
            failures.add("hud-clone-handler")

        # A digit clone's costume comes from a switch fed (walking up the reporter tree
        # from a `score`/`high score` division) through floor -> mod -> ... -> the
        # switch-costume block: floor(value / divisor) mod 10, joined into "digit/<n>".
        def digit_costume_chain(var_id: str) -> bool:
            for div in blocks.values():
                if div["opcode"] != "operator_divide" or not refs(
                    div["inputs"].get("NUM1"), var_id
                ):
                    continue
                floor = blocks.get(div.get("parent"))
                if (
                    floor is None
                    or floor["opcode"] != "operator_mathop"
                    or floor["fields"].get("OPERATOR", [None])[0] != "floor"
                ):
                    continue
                mod = blocks.get(floor.get("parent"))
                if mod is None or mod["opcode"] != "operator_mod":
                    continue
                cursor = mod.get("parent")
                for _ in range(4):
                    node = blocks.get(cursor)
                    if node is None:
                        break
                    if node["opcode"] == "looks_switchcostumeto":
                        return True
                    cursor = node.get("parent")
            return False

        if not digit_costume_chain(director.SCORE_ID):
            failures.add("score-digit-costume")
        if not digit_costume_chain(director.HIGH_SCORE_ID):
            failures.add("high-score-digit-costume")

        if not any(
            refs(value, director.LIVES_ID)
            for b in blocks.values()
            for value in b.get("inputs", {}).values()
        ):
            failures.add("craft-referenced")
        if not any(
            b["opcode"] == "event_whenbroadcastreceived"
            and b["fields"].get("BROADCAST_OPTION", [None])[0] == "craft changed"
            for b in blocks.values()
        ):
            failures.add("craft-changed-listener")

        # A flashing "1UP" is a loop whose body both shows and hides.
        def has_flash_loop() -> bool:
            for b in blocks.values():
                if b["opcode"] not in ("control_repeat_until", "control_repeat"):
                    continue
                substack = b["inputs"].get("SUBSTACK")
                if not substack:
                    continue
                cursor, opcodes = substack[1], set()
                while cursor:
                    node = blocks[cursor]
                    opcodes.add(node["opcode"])
                    cursor = node["next"]
                if {"looks_show", "looks_hide"} <= opcodes:
                    return True
            return False

        if not has_flash_loop():
            failures.add("flashing-1up")

        # Regression guard for the "header flashes then vanishes" bug: a clone's keep-alive
        # `repeat until` must LOOP while the HUD is visible and stop only on return to
        # title/boot, so its condition is "state is title or boot" (operator_or) — never the
        # negation, which is true during play and exits the loop immediately (the clone then
        # falls straight through to hide + delete).
        for b in blocks.values():
            if b["opcode"] != "control_repeat_until":
                continue
            cond = b["inputs"].get("CONDITION")
            if (
                isinstance(cond, list)
                and len(cond) > 1
                and isinstance(cond[1], str)
                and blocks.get(cond[1], {}).get("opcode") == "operator_not"
            ):
                failures.add("hud-loop-inverted")

        # Regression guard: a life clone must switch to the life/ship costume, not inherit
        # whatever glyph the sprite last held at spawn (which rendered the icons as a letter).
        if not any(
            b["opcode"] == "looks_costume"
            and b["fields"].get("COSTUME", [None])[0] == "life/ship"
            for b in blocks.values()
        ):
            failures.add("life-ship-costume")

        # Read-only invariant: every hud-owned variable write targets a hud-local id —
        # never a Stage variable (score/high score/craft included). Reinforces (at the
        # hud target specifically) the extended Stage-variable write-forbid guard above.
        allowed = {
            director.HUD_ROLE_ID,
            director.HUD_PLACE_ID,
            director.HUD_DIVISOR_ID,
            director.HUD_LIFE_INDEX_ID,
            director.HUD_LIFE_COUNT_ID,
            director.HUD_IS_CLONE_ID,
        }
        writes = {
            b["fields"].get("VARIABLE", [None, None])[1]
            for b in blocks.values()
            if b["opcode"] in {"data_setvariableto", "data_changevariableby"}
        }
        if not writes <= allowed:
            failures.add("hud-writes-only-local")

        # Life-icon row cap (usability fix): the render loop's TIMES reads the capped
        # `hud life count` local (never the uncapped `craft` directly), and that local is
        # clamped to HUD_LIFE_MAX before the loop runs.
        loop_capped = any(
            b["opcode"] == "control_repeat"
            and refs(b["inputs"].get("TIMES"), director.HUD_LIFE_COUNT_ID)
            for b in blocks.values()
        )
        if not loop_capped:
            failures.add("hud-life-spawn-loop-capped")
        cap_present = any(
            b["opcode"] == "control_if"
            and isinstance(b["inputs"].get("CONDITION"), list)
            and len(b["inputs"]["CONDITION"]) > 1
            and blocks.get(b["inputs"]["CONDITION"][1], {}).get("opcode") == "operator_gt"
            and refs(
                blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND1"),
                director.HUD_LIFE_COUNT_ID,
            )
            and blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND2")
            == [1, [4, director.HUD_LIFE_MAX]]
            for b in blocks.values()
        )
        if not cap_present:
            failures.add("hud-life-count-capped")

        # "HIGH SCORE" switches to the yellow hs/* costume set (director.HUD_HIGH_SCORE_LABEL),
        # distinct from the white glyph/* set every digit and the other labels use.
        hs_names = {glyph for glyph, _slot in director.HUD_HIGH_SCORE_LABEL}
        costume_menu_names = {
            b["fields"].get("COSTUME", [None, None])[0]
            for b in blocks.values()
            if b["opcode"] == "looks_costume"
        }
        if not (
            hs_names
            and all(name.startswith("hs/") for name in hs_names)
            and hs_names <= costume_menu_names
        ):
            failures.add("hud-high-score-label-yellow")
        return failures

    def test_hud_render_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco02_failures(project))

    def test_hud_render_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco02_failures(base))

        def refs(spec, var_id: str) -> bool:
            return (
                isinstance(spec, list)
                and len(spec) >= 2
                and isinstance(spec[1], list)
                and len(spec[1]) >= 3
                and spec[1][0] == 12
                and spec[1][2] == var_id
            )

        def hud_blocks(p: dict) -> dict:
            return next(t for t in p["targets"] if t.get("name") == "hud")["blocks"]

        def break_spawn(p: dict) -> None:
            for b in hud_blocks(p).values():
                if b["opcode"] == "control_create_clone_of":
                    b["opcode"] = "control_create_clone_of_disabled"

        def break_clone_handler(p: dict) -> None:
            for b in hud_blocks(p).values():
                if b["opcode"] == "control_start_as_clone":
                    b["opcode"] = "control_start_as_clone_disabled"

        def break_score_digit(p: dict) -> None:
            for b in hud_blocks(p).values():
                if b["opcode"] == "operator_divide" and refs(
                    b["inputs"].get("NUM1"), director.SCORE_ID
                ):
                    b["inputs"]["NUM1"] = [1, [4, 0]]

        def break_high_score_digit(p: dict) -> None:
            for b in hud_blocks(p).values():
                if b["opcode"] == "operator_divide" and refs(
                    b["inputs"].get("NUM1"), director.HIGH_SCORE_ID
                ):
                    b["inputs"]["NUM1"] = [1, [4, 0]]

        def break_craft_reference(p: dict) -> None:
            for b in hud_blocks(p).values():
                for key, value in list(b.get("inputs", {}).items()):
                    if refs(value, director.LIVES_ID):
                        b["inputs"][key] = [1, [4, 0]]

        def break_craft_changed(p: dict) -> None:
            for b in hud_blocks(p).values():
                if (
                    b["opcode"] == "event_whenbroadcastreceived"
                    and b["fields"].get("BROADCAST_OPTION", [None])[0] == "craft changed"
                ):
                    b["fields"]["BROADCAST_OPTION"] = [
                        "director stop",
                        director.MESSAGES["director stop"],
                    ]

        def break_flash(p: dict) -> None:
            blocks = hud_blocks(p)
            for b in blocks.values():
                if b["opcode"] not in ("control_repeat_until", "control_repeat"):
                    continue
                substack = b["inputs"].get("SUBSTACK")
                if not substack:
                    continue
                cursor, nodes = substack[1], []
                opcodes = set()
                while cursor:
                    node = blocks[cursor]
                    opcodes.add(node["opcode"])
                    nodes.append(node)
                    cursor = node["next"]
                if {"looks_show", "looks_hide"} <= opcodes:
                    for node in nodes:
                        if node["opcode"] == "looks_hide":
                            node["opcode"] = "looks_show"

        def break_write_only_local(p: dict) -> None:
            blocks = hud_blocks(p)
            blocks["injected-hud-score-write"] = {
                "opcode": "data_setvariableto",
                "next": None,
                "parent": None,
                "inputs": {"VALUE": [1, [4, 0]]},
                "fields": {"VARIABLE": ["score", director.SCORE_ID]},
                "shadow": False,
                "topLevel": False,
            }

        def break_life_spawn_loop_cap(p: dict) -> None:
            for b in hud_blocks(p).values():
                if b["opcode"] == "control_repeat" and refs(
                    b["inputs"].get("TIMES"), director.HUD_LIFE_COUNT_ID
                ):
                    b["inputs"]["TIMES"] = [3, [12, "craft", director.LIVES_ID], [10, ""]]

        def break_life_count_cap(p: dict) -> None:
            blocks = hud_blocks(p)
            for b in blocks.values():
                if b["opcode"] != "control_if":
                    continue
                condition = b["inputs"].get("CONDITION")
                if not (isinstance(condition, list) and len(condition) > 1):
                    continue
                cond = blocks.get(condition[1])
                if (
                    cond is not None
                    and cond["opcode"] == "operator_gt"
                    and refs(cond["inputs"].get("OPERAND1"), director.HUD_LIFE_COUNT_ID)
                ):
                    cond["inputs"]["OPERAND2"] = [1, [4, 999]]

        def break_high_score_label_yellow(p: dict) -> None:
            hs_names = {glyph for glyph, _slot in director.HUD_HIGH_SCORE_LABEL}
            for b in hud_blocks(p).values():
                if b["opcode"] == "looks_costume" and b["fields"].get(
                    "COSTUME", [None, None]
                )[0] in hs_names:
                    name = b["fields"]["COSTUME"][0]
                    b["fields"]["COSTUME"][0] = name.replace("hs/", "glyph/")

        def break_loop_inverted(p: dict) -> None:
            # re-introduce the "flash then vanish" bug: make a keep-alive loop's condition a
            # negation (true during play), so `repeat until` exits at once.
            blocks = hud_blocks(p)
            for b in blocks.values():
                if b["opcode"] == "control_repeat_until":
                    cond = b["inputs"].get("CONDITION")
                    if isinstance(cond, list) and len(cond) > 1 and isinstance(cond[1], str):
                        blocks[cond[1]]["opcode"] = "operator_not"
                        break

        def break_life_ship_costume(p: dict) -> None:
            for b in hud_blocks(p).values():
                if (
                    b["opcode"] == "looks_costume"
                    and b["fields"].get("COSTUME", [None])[0] == "life/ship"
                ):
                    b["fields"]["COSTUME"][0] = "digit/0"

        cases = [
            ("hud-spawns-clones", break_spawn),
            ("hud-clone-handler", break_clone_handler),
            ("score-digit-costume", break_score_digit),
            ("high-score-digit-costume", break_high_score_digit),
            ("craft-referenced", break_craft_reference),
            ("craft-changed-listener", break_craft_changed),
            ("flashing-1up", break_flash),
            ("hud-writes-only-local", break_write_only_local),
            ("hud-life-spawn-loop-capped", break_life_spawn_loop_cap),
            ("hud-life-count-capped", break_life_count_cap),
            ("hud-high-score-label-yellow", break_high_score_label_yellow),
            ("hud-loop-inverted", break_loop_inverted),
            ("life-ship-costume", break_life_ship_costume),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._eco02_failures(project), label)

    @staticmethod
    def _ply02_failures(project: dict) -> set:
        """PLY-02: the death outcome is decided from the craft counter, the D/G triggers drive
        the counter without hardcoding an outcome, and a new life restarts the terrain."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def key_hat(key: str):
            return next(
                (
                    bid
                    for bid, b in blocks.items()
                    if b["opcode"] == "event_whenkeypressed"
                    and b["fields"].get("KEY_OPTION", [None])[0] == key
                ),
                None,
            )

        def reachable(start: str) -> set:
            seen, stack = set(), [start]
            while stack:
                bid = stack.pop()
                if bid in seen or bid not in blocks:
                    continue
                seen.add(bid)
                b = blocks[bid]
                if b.get("next"):
                    stack.append(b["next"])
                for slot in ("SUBSTACK", "SUBSTACK2"):
                    val = b["inputs"].get(slot)
                    if isinstance(val, list) and len(val) > 1 and isinstance(val[1], str):
                        stack.append(val[1])
            return seen

        d_body = reachable(key_hat("d")) if key_hat("d") else set()
        g_body = reachable(key_hat("g")) if key_hat("g") else set()
        # D takes one hit: change craft by -1.
        if not any(
            blocks[bid]["opcode"] == "data_changevariableby"
            and blocks[bid]["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID
            and blocks[bid]["inputs"].get("VALUE") == [1, [4, -1]]
            for bid in d_body
        ):
            failures.add("d-decrements-craft")
        # G drains to terminal: set craft to 0.
        if not any(
            blocks[bid]["opcode"] == "data_setvariableto"
            and blocks[bid]["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID
            and blocks[bid]["inputs"].get("VALUE") == [1, [4, 0]]
            for bid in g_body
        ):
            failures.add("g-drains-craft")
        # neither trigger hardcodes a death outcome any more (the counter decides).
        if any(
            blocks[bid]["opcode"] == "data_setvariableto"
            and blocks[bid]["fields"].get("VARIABLE", [None, None])[1] == director.OUTCOME_ID
            for bid in (d_body | g_body)
        ):
            failures.add("trigger-hardcodes-outcome")
        # the death-complete handler decides from craft > 0: respawn vs game over.
        decision = next(
            (
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "control_if_else"
                and isinstance(b["inputs"].get("CONDITION"), list)
                and blocks.get(b["inputs"]["CONDITION"][1], {}).get("opcode") == "operator_gt"
                and blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND1", [None, [None]])[1][2:3]
                == [director.LIVES_ID]
            ),
            None,
        )
        if decision is None:
            failures.add("lives-driven-decision")
        else:
            # each branch (respawn / game over) must reach its own transition call.
            def branch_transitions(slot: str) -> int:
                spec = blocks[decision]["inputs"].get(slot)
                if not (isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], str)):
                    return 0
                return sum(
                    blocks[bid]["opcode"] == "procedures_call"
                    and blocks[bid].get("mutation", {}).get("proccode") == director.PROCCODE
                    for bid in reachable(spec[1])
                )

            if branch_transitions("SUBSTACK") < 1 or branch_transitions("SUBSTACK2") < 1:
                failures.add("lives-driven-decision")
        return failures

    def test_death_decision_is_lives_driven(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._ply02_failures(project))

    def test_death_decision_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._ply02_failures(base))
        stage = next(t for t in base["targets"] if t["isStage"])

        def find(pred):
            def f(p):
                s = next(t for t in p["targets"] if t["isStage"])
                return next(b for b in s["blocks"].values() if pred(b))
            return f

        def break_d(p):
            b = find(
                lambda b: b["opcode"] == "data_changevariableby"
                and b["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID
                and b["inputs"].get("VALUE") == [1, [4, -1]]
            )(p)
            b["inputs"]["VALUE"] = [1, [4, 0]]

        def break_g(p):
            # retarget G's `set craft to 0` to a different variable
            s = next(t for t in p["targets"] if t["isStage"])
            gid = next(
                bid
                for bid, b in s["blocks"].items()
                if b["opcode"] == "event_whenkeypressed"
                and b["fields"].get("KEY_OPTION", [None])[0] == "g"
            )
            # walk to the set-craft-0 in g's body
            for b in s["blocks"].values():
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID
                    and b["inputs"].get("VALUE") == [1, [4, 0]]
                ):
                    b["fields"]["VARIABLE"] = ["award value", director.AWARD_VALUE_ID]
                    break

        def break_decision(p):
            # Target the DEATH-decision `craft > threshold` specifically — the operator_gt that is the
            # CONDITION of the control_if_else (as _ply02_failures identifies it), not any other craft
            # comparison on the Stage (e.g. DIF-02's `craft > 0` re-tune guard).
            s = next(t for t in p["targets"] if t["isStage"])
            blocks = s["blocks"]
            decision = next(
                b
                for b in blocks.values()
                if b["opcode"] == "control_if_else"
                and isinstance(b["inputs"].get("CONDITION"), list)
                and blocks.get(b["inputs"]["CONDITION"][1], {}).get("opcode") == "operator_gt"
                and blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND1", [None, [None]])[1][2:3]
                == [director.LIVES_ID]
            )
            cond = blocks[decision["inputs"]["CONDITION"][1]]
            cond["inputs"]["OPERAND1"][1][2] = director.SCORE_ID  # decide from score, not craft

        cases = [
            ("d-decrements-craft", break_d),
            ("g-drains-craft", break_g),
            ("lives-driven-decision", break_decision),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._ply02_failures(project), label)

    @staticmethod
    def _area01_failures(project: dict) -> set:
        """AREA-01 area clock: `advance area` runs before the slot walk, steps the monotonic
        `area progress` by 32, derives the scroll row once, completes an area at row 14 with the
        16 -> 7 wrap, and the near-end checkpoint advances the area on a new life for a frozen
        scroll row in [14, 67]. Structure only; the row VALUES are checked in test_spec_docs."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def reachable(start):
            seen, stack = set(), [start]
            while stack:
                bid = stack.pop()
                if bid in seen or bid not in blocks:
                    continue
                seen.add(bid)
                b = blocks[bid]
                if b.get("next"):
                    stack.append(b["next"])
                for slot in ("SUBSTACK", "SUBSTACK2"):
                    v = b["inputs"].get(slot)
                    if isinstance(v, list) and len(v) > 1 and isinstance(v[1], str):
                        stack.append(v[1])
            return seen

        def literal(spec):
            if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], list):
                return spec[1][1]
            return None

        def refs_var(spec, var_id):
            return (
                isinstance(spec, list)
                and len(spec) > 1
                and isinstance(spec[1], list)
                and spec[1][2:3] == [var_id]
            )

        def eq_var_num(cond_spec, var_id, num):
            if not (isinstance(cond_spec, list) and len(cond_spec) > 1):
                return False
            b = blocks.get(cond_spec[1])
            return (
                b is not None
                and b["opcode"] == "operator_equals"
                and refs_var(b["inputs"].get("OPERAND1"), var_id)
                and literal(b["inputs"].get("OPERAND2")) == num
            )

        def is_area_wrap(bid):
            b = blocks.get(bid)
            if b is None or b["opcode"] != "control_if_else":
                return False
            if not eq_var_num(b["inputs"].get("CONDITION"), director.AREA_NUMBER_ID, director.AREA_MAX):
                return False
            then_spec = b["inputs"].get("SUBSTACK")
            if not (isinstance(then_spec, list) and len(then_spec) > 1):
                return False
            sets_loop_back = any(
                blocks[x]["opcode"] == "data_setvariableto"
                and blocks[x]["fields"].get("VARIABLE", [None, None])[1] == director.AREA_NUMBER_ID
                and blocks[x]["inputs"].get("VALUE") == [1, [4, director.AREA_LOOP_BACK]]
                for x in reachable(then_spec[1])
            )
            return sets_loop_back

        # 1. advance area exists.
        proto = next(
            (
                b
                for b in blocks.values()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.ADVANCE_AREA_PROCCODE
            ),
            None,
        )
        if proto is None:
            failures.add("advance-area-exists")
            return failures
        definition_id = proto["parent"]
        body = reachable(blocks[definition_id]["next"]) if blocks[definition_id].get("next") else set()

        # 2. phase order: the advance-area call is immediately followed by the advance-slots call.
        area_call = next(
            (
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == director.ADVANCE_AREA_PROCCODE
            ),
            None,
        )
        nxt = blocks[area_call].get("next") if area_call else None
        if not (
            nxt
            and blocks.get(nxt, {}).get("opcode") == "procedures_call"
            and blocks[nxt].get("mutation", {}).get("proccode") == director.ADVANCE_SLOTS_PROCCODE
        ):
            failures.add("advance-area-before-slots")

        # 3. area progress steps by exactly 32.
        if not any(
            blocks[bid]["opcode"] == "data_changevariableby"
            and blocks[bid]["fields"].get("VARIABLE", [None, None])[1] == director.AREA_PROGRESS_ID
            and blocks[bid]["inputs"].get("VALUE") == [1, [4, director.AREA_PROGRESS_STEP]]
            for bid in body
        ):
            failures.add("progress-steps-32")

        # 4. scroll row = floor(divide(mod(subtract(3328, area progress), 65536), 256)).
        derived_ok = False
        for bid in body:
            b = blocks[bid]
            if b["opcode"] != "data_setvariableto":
                continue
            if b["fields"].get("VARIABLE", [None, None])[1] != director.SCROLL_ROW_ID:
                continue
            val = b["inputs"].get("VALUE")
            if not (isinstance(val, list) and val[0] == 3 and isinstance(val[1], str)):
                continue
            floor_b = blocks.get(val[1])
            if not floor_b or floor_b["opcode"] != "operator_mathop":
                continue
            if floor_b["fields"].get("OPERATOR", [None])[0] != "floor":
                continue
            div = blocks.get(floor_b["inputs"].get("NUM", [None, None])[1])
            if not div or div["opcode"] != "operator_divide":
                continue
            if literal(div["inputs"].get("NUM2")) != director.AREA_ROW_DIVISOR:
                continue
            mod = blocks.get(div["inputs"].get("NUM1", [None, None])[1])
            if not mod or mod["opcode"] != "operator_mod":
                continue
            if literal(mod["inputs"].get("NUM2")) != director.AREA_COUNTER_WRAP:
                continue
            sub = blocks.get(mod["inputs"].get("NUM1", [None, None])[1])
            if not sub or sub["opcode"] != "operator_subtract":
                continue
            if literal(sub["inputs"].get("NUM1")) != director.AREA_COUNTER_INIT:
                continue
            if not refs_var(sub["inputs"].get("NUM2"), director.AREA_PROGRESS_ID):
                continue
            derived_ok = True
            break
        if not derived_ok:
            failures.add("scroll-row-derived")

        # 5. completion at row == 14 advances the area (a wrap in its THEN body). The block is a
        # plain `if` when AREA-02's consume is absent and an `if/else` once it is present.
        completion = next(
            (
                bid
                for bid in body
                if blocks[bid]["opcode"] in ("control_if", "control_if_else")
                and eq_var_num(
                    blocks[bid]["inputs"].get("CONDITION"),
                    director.SCROLL_ROW_ID,
                    director.AREA_COMPLETE_ROW,
                )
            ),
            None,
        )
        then_spec = blocks[completion]["inputs"].get("SUBSTACK") if completion else None
        if not (
            completion
            and isinstance(then_spec, list)
            and len(then_spec) > 1
            and any(is_area_wrap(x) for x in reachable(then_spec[1]))
        ):
            failures.add("completion-at-14")

        # 6. every 16 -> 7 wrap is well-formed, and at least one exists.
        wrap_conditions = [
            bid
            for bid, b in blocks.items()
            if b["opcode"] == "control_if_else"
            and eq_var_num(b["inputs"].get("CONDITION"), director.AREA_NUMBER_ID, director.AREA_MAX)
        ]
        if not wrap_conditions or not all(is_area_wrap(bid) for bid in wrap_conditions):
            failures.add("area-wrap-16-7")

        # 7. near-end checkpoint: a control_if on AND(scroll row > 13, 68 > scroll row) whose
        # body advances the area — the window [14, 67] (13 and 68 exclusive).
        checkpoint_ok = False
        for bid, b in blocks.items():
            if b["opcode"] != "control_if":
                continue
            cond = b["inputs"].get("CONDITION")
            if not (isinstance(cond, list) and len(cond) > 1):
                continue
            and_b = blocks.get(cond[1])
            if not and_b or and_b["opcode"] != "operator_and":
                continue
            gts = [
                blocks.get(and_b["inputs"].get(slot, [None, None])[1])
                for slot in ("OPERAND1", "OPERAND2")
            ]
            if any(g is None or g["opcode"] != "operator_gt" for g in gts):
                continue
            low_ok = any(
                refs_var(g["inputs"].get("OPERAND1"), director.SCROLL_ROW_ID)
                and literal(g["inputs"].get("OPERAND2")) == director.AREA_CHECKPOINT_LOW_EXCL
                for g in gts
            )
            high_ok = any(
                literal(g["inputs"].get("OPERAND1")) == director.AREA_CHECKPOINT_HIGH_EXCL
                and refs_var(g["inputs"].get("OPERAND2"), director.SCROLL_ROW_ID)
                for g in gts
            )
            then_spec = b["inputs"].get("SUBSTACK")
            advances = (
                isinstance(then_spec, list)
                and len(then_spec) > 1
                and any(is_area_wrap(x) for x in reachable(then_spec[1]))
            )
            if low_ok and high_ok and advances:
                checkpoint_ok = True
                break
        if not checkpoint_ok:
            failures.add("checkpoint-window")

        return failures

    def test_area_clock_contract(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._area01_failures(project))

    def test_area_clock_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._area01_failures(base))

        def stage_of(p):
            return next(t for t in p["targets"] if t["isStage"])

        def break_phase_order(p):
            s = stage_of(p)
            call = next(
                bid
                for bid, b in s["blocks"].items()
                if b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == director.ADVANCE_AREA_PROCCODE
            )
            s["blocks"][call]["next"] = None

        def break_progress_step(p):
            s = stage_of(p)
            b = next(
                b
                for b in s["blocks"].values()
                if b["opcode"] == "data_changevariableby"
                and b["fields"].get("VARIABLE", [None, None])[1] == director.AREA_PROGRESS_ID
                and b["inputs"].get("VALUE") == [1, [4, director.AREA_PROGRESS_STEP]]
            )
            b["inputs"]["VALUE"] = [1, [4, director.AREA_PROGRESS_STEP - 1]]

        def break_row_wrap_constant(p):
            s = stage_of(p)
            b = next(
                b
                for b in s["blocks"].values()
                if b["opcode"] == "operator_mod"
                and (b["inputs"].get("NUM2") or [None, [None, None]])[1][1] == director.AREA_COUNTER_WRAP
            )
            b["inputs"]["NUM2"] = [1, [4, director.AREA_COUNTER_WRAP - 1]]

        def break_completion_row(p):
            s = stage_of(p)
            # the completion compare: operator_equals(scroll row, 14).
            b = next(
                b
                for b in s["blocks"].values()
                if b["opcode"] == "operator_equals"
                and isinstance(b["inputs"].get("OPERAND1"), list)
                and b["inputs"]["OPERAND1"][1][2:3] == [director.SCROLL_ROW_ID]
                and (b["inputs"].get("OPERAND2") or [None, [None, None]])[1][1] == director.AREA_COMPLETE_ROW
            )
            b["inputs"]["OPERAND2"] = [1, [4, director.AREA_TOP_ROW]]

        def break_wrap_target(p):
            s = stage_of(p)
            # retarget one wrap's `set area number to 7` to a non-loop value.
            b = next(
                b
                for b in s["blocks"].values()
                if b["opcode"] == "data_setvariableto"
                and b["fields"].get("VARIABLE", [None, None])[1] == director.AREA_NUMBER_ID
                and b["inputs"].get("VALUE") == [1, [4, director.AREA_LOOP_BACK]]
            )
            b["inputs"]["VALUE"] = [1, [4, 1]]

        def break_checkpoint_low(p):
            s = stage_of(p)
            b = next(
                b
                for b in s["blocks"].values()
                if b["opcode"] == "operator_gt"
                and isinstance(b["inputs"].get("OPERAND1"), list)
                and b["inputs"]["OPERAND1"][1][2:3] == [director.SCROLL_ROW_ID]
                and (b["inputs"].get("OPERAND2") or [None, [None, None]])[1][1] == director.AREA_CHECKPOINT_LOW_EXCL
            )
            b["inputs"]["OPERAND2"] = [1, [4, director.AREA_CHECKPOINT_LOW_EXCL + 2]]

        def break_checkpoint_high(p):
            s = stage_of(p)
            b = next(
                b
                for b in s["blocks"].values()
                if b["opcode"] == "operator_gt"
                and (b["inputs"].get("OPERAND1") or [None, [None, None]])[1][1] == director.AREA_CHECKPOINT_HIGH_EXCL
                and isinstance(b["inputs"].get("OPERAND2"), list)
                and b["inputs"]["OPERAND2"][1][2:3] == [director.SCROLL_ROW_ID]
            )
            b["inputs"]["OPERAND1"] = [1, [4, director.AREA_CHECKPOINT_HIGH_EXCL - 2]]

        cases = [
            ("advance-area-before-slots", break_phase_order),
            ("progress-steps-32", break_progress_step),
            ("scroll-row-derived", break_row_wrap_constant),
            ("completion-at-14", break_completion_row),
            ("area-wrap-16-7", break_wrap_target),
            ("checkpoint-window", break_checkpoint_low),
            ("checkpoint-window", break_checkpoint_high),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._area01_failures(project), label)

    @staticmethod
    def _area02_failures(project: dict) -> set:
        """AREA-02/AREA-03 area object scheduler: all 16 normal areas flattened into four parallel
        columns (handler, trigger row, payload, and DIF-01/FORM-01's runtime `arg`; each area = its
        records + one materialized sentinel), partitioned by two 16-entry index lists into contiguous
        1-based inclusive per-area spans; an ordered consume loop guarded by `cursor > end` OR
        `trigger != scroll row`, a per-record dispatch that now carries the DIF-01/FORM-01 handler
        branches, and the observable that advances the cursor and counts fires. Structure only; the
        per-area JSON-faithful content is the round-trip golden's job (test_spec_docs), and the dynamic
        fire-once/in-order and dispatch behaviour is the scratch-vm harness and the operator playtest's."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        by_name = {value[0]: value[1] for value in stage["lists"].values()}

        def refs_var(spec, var_id):
            return (
                isinstance(spec, list)
                and len(spec) > 1
                and isinstance(spec[1], list)
                and spec[1][2:3] == [var_id]
            )

        # the three parallel columns are equal length and the two 16-entry index lists partition them
        # into contiguous 1-based inclusive per-area spans (guards a column/index-list mismatch or a
        # leaked/dropped entry). The JSON-faithful per-area CONTENT is the round-trip golden's job.
        handlers = by_name.get("schedule handler", [])
        rows = by_name.get("schedule trigger row", [])
        payloads = by_name.get("schedule payload", [])
        args = by_name.get("schedule arg", [])
        starts = by_name.get("area schedule start", [])
        ends = by_name.get("area schedule end", [])
        total = len(handlers)
        contiguous = (
            len(starts) == director.AREA_MAX
            and len(ends) == director.AREA_MAX
            and starts[:1] == [1]
            and ends[-1:] == [total]
            and all(starts[i] == ends[i - 1] + 1 for i in range(1, director.AREA_MAX))
            and all(starts[i] <= ends[i] for i in range(director.AREA_MAX))
        )
        if not (
            total > 0
            and len(rows) == total
            and len(payloads) == total
            and len(args) == total  # DIF-01/FORM-01: the 4th parallel column stays in lockstep
            and contiguous
        ):
            failures.add("schedule-lists-length")

        # EVERY area's slice ends in the materialized sentinel (handler 'sentinel', trigger 0x0D) — a
        # per-area check, so an interior area's sentinel corruption cannot hide behind the global tail.
        if not (starts and ends and len(starts) == len(ends)):
            failures.add("schedule-sentinel")
        else:
            for start, end in zip(starts, ends):
                if not (
                    1 <= end <= total
                    and handlers[end - 1] == director.SCHEDULE_SENTINEL_HANDLER
                    and rows[end - 1] == director.AREA_TOP_ROW
                ):
                    failures.add("schedule-sentinel")
                    break

        def body_ids(loop_id):
            sub = blocks[loop_id]["inputs"].get("SUBSTACK")
            out, bid = [], (sub[1] if isinstance(sub, list) and len(sub) > 1 else None)
            while bid:
                out.append(bid)
                bid = blocks[bid].get("next")
            return out

        # the consume loop: a repeat_until whose body advances the schedule cursor.
        loop = None
        for bid, b in blocks.items():
            if b["opcode"] != "control_repeat_until":
                continue
            if any(
                blocks[x]["opcode"] == "data_changevariableby"
                and blocks[x]["fields"].get("VARIABLE", [None, None])[1] == director.SCHEDULE_CURSOR_ID
                for x in body_ids(bid)
            ):
                loop = bid
                break
        if loop is None:
            failures.add("consume-loop")
            return failures
        ids = body_ids(loop)

        if not any(
            blocks[x]["opcode"] == "data_changevariableby"
            and blocks[x]["fields"].get("VARIABLE", [None, None])[1] == director.SCHEDULE_CURSOR_ID
            and blocks[x]["inputs"].get("VALUE") == [1, [4, 1]]
            for x in ids
        ):
            failures.add("consume-advances-cursor")

        if not any(
            blocks[x]["opcode"] == "data_changevariableby"
            and blocks[x]["fields"].get("VARIABLE", [None, None])[1] == director.SCHEDULE_FIRED_ID
            and blocks[x]["inputs"].get("VALUE") == [1, [4, 1]]
            for x in ids
        ):
            failures.add("consume-counts-fired")

        # DIF-01 / FORM-01: the per-record dispatch is now WIRED — the loop body carries at least one
        # handler-keyed branch (a control_if whose condition reads the `schedule handler` column).
        # The spawn/boss handlers stay unwired (slice 8); the dispatched BEHAVIOUR is the harness and
        # model fixtures' job, not this structural guard — this only catches the dispatch going missing.
        def reads_handler_list(bid: str, seen: set) -> bool:
            if bid in seen or bid not in blocks:
                return False
            seen.add(bid)
            blk = blocks[bid]
            if (
                blk["opcode"] == "data_itemoflist"
                and blk["fields"].get("LIST", [None, None])[1] == director.SCHEDULE_HANDLER_ID
            ):
                return True
            return any(
                isinstance(v, list) and len(v) > 1 and isinstance(v[1], str) and reads_handler_list(v[1], seen)
                for v in blk["inputs"].values()
            )

        if not any(
            blocks[x]["opcode"] == "control_if"
            and isinstance(blocks[x]["inputs"].get("CONDITION"), list)
            and len(blocks[x]["inputs"]["CONDITION"]) > 1
            and reads_handler_list(blocks[x]["inputs"]["CONDITION"][1], set())
            for x in ids
        ):
            failures.add("dispatch-present")

        # stop condition: operator_or( gt(cursor, end), not( eq(trigger, scroll row) ) ).
        def subtree(bid, acc):
            if bid in acc or bid not in blocks:
                return
            acc.add(bid)
            for v in blocks[bid]["inputs"].values():
                if isinstance(v, list) and len(v) > 1 and isinstance(v[1], str):
                    subtree(v[1], acc)

        cond = blocks[loop]["inputs"].get("CONDITION")
        cond_ids = set()
        if isinstance(cond, list) and len(cond) > 1:
            subtree(cond[1], cond_ids)
        cond_ops = {blocks[x]["opcode"] for x in cond_ids}
        row_match = any(
            blocks[x]["opcode"] == "operator_equals"
            and (
                refs_var(blocks[x]["inputs"].get("OPERAND1"), director.SCROLL_ROW_ID)
                or refs_var(blocks[x]["inputs"].get("OPERAND2"), director.SCROLL_ROW_ID)
            )
            for x in cond_ids
        )
        if not ({"operator_or", "operator_gt", "operator_not", "operator_equals"} <= cond_ops and row_match):
            failures.add("consume-stop-condition")

        return failures

    def test_area_scheduler_contract(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._area02_failures(project))

    def test_area_scheduler_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._area02_failures(base))

        def stage_of(p):
            return next(t for t in p["targets"] if t["isStage"])

        def list_named(p, name):
            return next(v for v in stage_of(p)["lists"].values() if v[0] == name)

        def consume_loop(p):
            s = stage_of(p)
            b = s["blocks"]

            def body(loop_id):
                sub = b[loop_id]["inputs"].get("SUBSTACK")
                out, bid = [], (sub[1] if isinstance(sub, list) and len(sub) > 1 else None)
                while bid:
                    out.append(bid)
                    bid = b[bid].get("next")
                return out

            for bid, blk in b.items():
                if blk["opcode"] == "control_repeat_until" and any(
                    b[x]["opcode"] == "data_changevariableby"
                    and b[x]["fields"].get("VARIABLE", [None, None])[1] == director.SCHEDULE_CURSOR_ID
                    for x in body(bid)
                ):
                    return bid, body(bid)
            raise AssertionError("no consume loop")

        def break_length(p):
            list_named(p, "schedule trigger row")[1].append(99)

        def break_sentinel(p):
            # corrupt an INTERIOR area's sentinel (area 1's, at its span end), not the global tail —
            # a global-tail-only check would miss this; the per-area check must catch it.
            area1_end = list_named(p, "area schedule end")[1][0]
            list_named(p, "schedule handler")[1][area1_end - 1] = "add_ground_object"

        def break_cursor_advance(p):
            s = stage_of(p)
            _, ids = consume_loop(p)
            blk = next(
                s["blocks"][x]
                for x in ids
                if s["blocks"][x]["fields"].get("VARIABLE", [None, None])[1] == director.SCHEDULE_CURSOR_ID
            )
            blk["inputs"]["VALUE"] = [1, [4, 0]]

        def break_fired_count(p):
            s = stage_of(p)
            _, ids = consume_loop(p)
            blk = next(
                s["blocks"][x]
                for x in ids
                if s["blocks"][x]["fields"].get("VARIABLE", [None, None])[1] == director.SCHEDULE_FIRED_ID
            )
            blk["inputs"]["VALUE"] = [1, [4, 0]]

        def break_missing_dispatch(p):
            # strip the DIF-01/FORM-01 handler branches from the loop body, leaving only the two
            # counters — regressing to the old empty seam; the dispatch-present guard must catch it.
            s = stage_of(p)
            b = s["blocks"]
            loop_id, ids = consume_loop(p)
            counters = [
                x for x in ids if b[x]["opcode"] == "data_changevariableby"
            ]
            b[loop_id]["inputs"]["SUBSTACK"] = [2, counters[0]]
            b[counters[0]]["parent"] = loop_id
            for left, right in zip(counters, counters[1:]):
                b[left]["next"] = right
                b[right]["parent"] = left
            b[counters[-1]]["next"] = None

        def break_stop_condition(p):
            s = stage_of(p)
            loop_id, _ = consume_loop(p)
            cond = s["blocks"][loop_id]["inputs"]["CONDITION"][1]
            # find the operator_not in the condition subtree and neutralize it.
            seen, stack = set(), [cond]
            while stack:
                bid = stack.pop()
                if bid in seen or bid not in s["blocks"]:
                    continue
                seen.add(bid)
                if s["blocks"][bid]["opcode"] == "operator_not":
                    s["blocks"][bid]["opcode"] = "operator_and"
                    return
                for v in s["blocks"][bid]["inputs"].values():
                    if isinstance(v, list) and len(v) > 1 and isinstance(v[1], str):
                        stack.append(v[1])
            raise AssertionError("no operator_not in the stop condition")

        cases = [
            ("schedule-lists-length", break_length),
            ("schedule-sentinel", break_sentinel),
            ("consume-advances-cursor", break_cursor_advance),
            ("consume-counts-fired", break_fired_count),
            ("dispatch-present", break_missing_dispatch),
            ("consume-stop-condition", break_stop_condition),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._area02_failures(project), label)

    def test_generated_schedule_has_no_super_or_unknown_object(self) -> None:
        # AREA-03 acceptance guard on the BAKED project: every scheduled record decodes to a normal
        # object type (<= NORMAL_TYPE_MAX — INCLUSIVE; the max real type equals that ceiling) and a
        # known handler; the materialized sentinel (empty payload) is skipped. Keyed off the canonical
        # threshold (tools/reference_extract) and the reference registry, never a re-derived literal —
        # so a Super-only or unknown record smuggled into the flattened columns fails here.
        import reference_extract  # noqa: E402

        registry = json.loads(
            (ROOT / "docs" / "spec" / "data" / "object-types.json").read_text()
        )["registry"]["types"]
        known_handlers = {t.get("schedule_action") for t in registry} - {"none", None}

        project = load_source(scratch.SOURCE_DIR)
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value[1] for value in stage["lists"].values()}
        handlers = by_name["schedule handler"]
        payloads = by_name["schedule payload"]
        self.assertEqual(len(handlers), len(payloads))

        for i, (handler, payload) in enumerate(zip(handlers, payloads)):
            if handler == director.SCHEDULE_SENTINEL_HANDLER:
                self.assertEqual("", payload, f"sentinel payload not empty at {i}")
                continue
            self.assertIn(handler, known_handlers, f"unknown handler {handler!r} at {i}")
            obj_type = json.loads(payload)["object_type"]
            self.assertLessEqual(
                obj_type,
                reference_extract.NORMAL_TYPE_MAX,
                f"Super-only object_type {obj_type} at index {i}",
            )

    def test_spec_data_loader_verifies_manifest_hash(self) -> None:
        # The AREA ingest loader hard-fails at build time on a data file whose bytes do not match
        # the pinned SHA-256 in manifest.json (mirroring hud_glyphs.py) — so a stale or hand-edited
        # terrain/schedule file can never silently bake into project.json.
        import json as _json
        import tempfile
        from pathlib import Path as _Path

        # the real committed file loads cleanly (positive).
        self.assertIsNotNone(director._load_spec_data("terrain.json"))

        # a tampered file against the real manifest hash raises loudly (negative).
        manifest = _json.loads((director.SPEC_DATA_DIR / "manifest.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = _Path(tmp)
            (tmp_dir / "manifest.json").write_text(_json.dumps(manifest), encoding="utf-8")
            (tmp_dir / "terrain.json").write_text('{"tampered": true}', encoding="utf-8")
            with self.assertRaises(SystemExit):
                director._load_spec_data("terrain.json", data_dir=tmp_dir)

    @staticmethod
    def _eco04_failures(project: dict) -> set:
        """ECO-04 game over — the 64-tick GAME OVER hold immediately followed by the same
        if_epoch_state/DEATH_EPOCH_ID guard the death timing above it uses (so a superseding
        transition cancels a stale hold and the broadcast is never sent outside the guard),
        the HUD's "GAME OVER" glyph text spawned only while `game state` is game-over with its
        own clone role, and the best-five check that compares the final score to the table's
        5th entry and records `qualified` before the transition back to title. Structure only;
        on-screen layout is the operator's playtest."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        stage_blocks = stage["blocks"]
        death_blocks = next(t for t in project["targets"] if t["name"] == "solv_death")["blocks"]
        hud_blocks = next(t for t in project["targets"] if t["name"] == "hud")["blocks"]

        def refs(spec, var_id: str) -> bool:
            return (
                isinstance(spec, list)
                and len(spec) >= 2
                and isinstance(spec[1], list)
                and len(spec[1]) >= 3
                and spec[1][0] == 12
                and spec[1][2] == var_id
            )

        def is_epoch_state_guard(block_id, blocks_map, local_id: str, state: str) -> bool:
            block = blocks_map.get(block_id)
            if block is None or block["opcode"] != "control_if":
                return False
            cond = block["inputs"].get("CONDITION")
            if not (isinstance(cond, list) and len(cond) > 1 and isinstance(cond[1], str)):
                return False
            and_block = blocks_map.get(cond[1])
            if and_block is None or and_block["opcode"] != "operator_and":
                return False
            op1_id = and_block["inputs"].get("OPERAND1", [None, None])[1]
            op2_id = and_block["inputs"].get("OPERAND2", [None, None])[1]
            op1 = blocks_map.get(op1_id)
            op2 = blocks_map.get(op2_id)
            epoch_ok = (
                op1 is not None
                and op1["opcode"] == "operator_equals"
                and refs(op1["inputs"].get("OPERAND1"), local_id)
            )
            state_ok = (
                op2 is not None
                and op2["opcode"] == "operator_equals"
                and op2["inputs"].get("OPERAND2") == [1, [10, state]]
            )
            return epoch_ok and state_ok

        def broadcasts_to(block_id, blocks_map, message: str) -> bool:
            block = blocks_map.get(block_id)
            return (
                block is not None
                and block["opcode"] == "event_broadcast"
                and block["inputs"].get("BROADCAST_INPUT", [None, [None, None]])[1][1]
                == message
            )

        # The 64-tick hold, followed directly by the epoch/state("game-over") guard, whose
        # substack directly broadcasts `game over complete`.
        hold = next(
            (
                bid
                for bid, b in death_blocks.items()
                if b["opcode"] == "control_repeat"
                and b["inputs"].get("TIMES") == [1, [4, director.GAME_OVER_HOLD_TICKS]]
            ),
            None,
        )
        if hold is None:
            failures.add("game-over-hold-64-ticks")
        else:
            guard = death_blocks[hold].get("next")
            guarded_ok = is_epoch_state_guard(
                guard, death_blocks, director.DEATH_EPOCH_ID, "game-over"
            )
            if guarded_ok:
                substack = death_blocks[guard]["inputs"].get("SUBSTACK")
                broadcast_id = (
                    substack[1] if isinstance(substack, list) and len(substack) > 1 else None
                )
                guarded_ok = broadcasts_to(broadcast_id, death_blocks, "game over complete")
            if not guarded_ok:
                failures.add("game-over-hold-epoch-guarded")

        # No bypass: every `game over complete` broadcast in solv_death sits inside SOME
        # epoch/state("game-over") guard — never sent unconditionally.
        guarded_broadcast_ids = set()
        for bid, b in death_blocks.items():
            if not is_epoch_state_guard(bid, death_blocks, director.DEATH_EPOCH_ID, "game-over"):
                continue
            substack = b["inputs"].get("SUBSTACK")
            if isinstance(substack, list) and len(substack) > 1:
                guarded_broadcast_ids.add(substack[1])
        all_broadcasts = {
            bid for bid in death_blocks if broadcasts_to(bid, death_blocks, "game over complete")
        }
        if not all_broadcasts <= guarded_broadcast_ids:
            failures.add("game-over-broadcast-not-guarded")

        # HUD: the "GAME OVER" glyph clones are spawned only under a `game state` ==
        # game-over check (nested inside the broader HUD-visible gate).
        game_over_gate = next(
            (
                bid
                for bid, b in hud_blocks.items()
                if b["opcode"] == "control_if"
                and isinstance(b["inputs"].get("CONDITION"), list)
                and len(b["inputs"]["CONDITION"]) > 1
                and hud_blocks.get(b["inputs"]["CONDITION"][1], {}).get("opcode")
                == "operator_equals"
                and refs(
                    hud_blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND1"),
                    director.STATE_ID,
                )
                and hud_blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND2")
                == [1, [10, "game-over"]]
            ),
            None,
        )
        if game_over_gate is None:
            failures.add("hud-game-over-glyphs-gated")
        else:
            substack = hud_blocks[game_over_gate]["inputs"].get("SUBSTACK")
            cursor = substack[1] if isinstance(substack, list) and len(substack) > 1 else None
            gated_ids = set()
            while cursor:
                gated_ids.add(cursor)
                cursor = hud_blocks[cursor]["next"]
            role_sets = sum(
                1
                for bid in gated_ids
                if hud_blocks[bid]["opcode"] == "data_setvariableto"
                and hud_blocks[bid]["fields"].get("VARIABLE", [None, None])[1]
                == director.HUD_ROLE_ID
                and hud_blocks[bid]["inputs"].get("VALUE")
                == [1, [4, director.HUD_ROLE_GAME_OVER_GLYPH]]
            )
            clones = sum(
                1 for bid in gated_ids if hud_blocks[bid]["opcode"] == "control_create_clone_of"
            )
            expected = len(director.HUD_GAME_OVER_LABEL)
            if role_sets < expected or clones < expected:
                failures.add("hud-game-over-glyphs-spawned")

        # The clone script dispatches the distinct game-over-glyph role (never colliding with
        # the digit/life/label roles) to a static show — the shared director-stop clone-clear
        # retires it, so it never deletes itself.
        role_dispatch = any(
            b["opcode"] == "control_if"
            and isinstance(b["inputs"].get("CONDITION"), list)
            and len(b["inputs"]["CONDITION"]) > 1
            and hud_blocks.get(b["inputs"]["CONDITION"][1], {}).get("opcode")
            == "operator_equals"
            and refs(
                hud_blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND1"),
                director.HUD_ROLE_ID,
            )
            and hud_blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND2")
            == [1, [4, director.HUD_ROLE_GAME_OVER_GLYPH]]
            for b in hud_blocks.values()
        )
        if not role_dispatch:
            failures.add("hud-game-over-role-dispatch")

        # Best-five check: `score > high score table item 5`, and the set-`qualified` that
        # follows it reaches the transition-procedure call in the `game over complete` receiver
        # (computed before the transition resets `reset scope` and, via cold-start, the score).
        def reachable(start) -> set:
            seen, stack = set(), [start] if start else []
            while stack:
                bid = stack.pop()
                if bid is None or bid in seen or bid not in stage_blocks:
                    continue
                seen.add(bid)
                b = stage_blocks[bid]
                if b.get("next"):
                    stack.append(b["next"])
                for slot in ("SUBSTACK", "SUBSTACK2"):
                    val = b["inputs"].get(slot)
                    if isinstance(val, list) and len(val) > 1 and isinstance(val[1], str):
                        stack.append(val[1])
            return seen

        receiver = next(
            (
                bid
                for bid, b in stage_blocks.items()
                if b["opcode"] == "event_whenbroadcastreceived"
                and b["fields"].get("BROADCAST_OPTION", [None])[0] == "game over complete"
            ),
            None,
        )
        body = reachable(receiver)

        def is_fifth_place_item(spec) -> bool:
            if not (isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], str)):
                return False
            item = stage_blocks.get(spec[1])
            return (
                item is not None
                and item["opcode"] == "data_itemoflist"
                and item["fields"].get("LIST", [None, None])[1] == director.HIGH_SCORE_TABLE_ID
                and item["inputs"].get("INDEX") == [1, [4, 5]]
            )

        # The comparison reporter is nested inside the set-`qualified` VALUE input (not on
        # the command next-chain `body` walks), so it is found by shape, like ECO-01's
        # score-add-award/cap/high-score-track checks scan `blocks.values()` directly.
        compares_fifth = any(
            b["opcode"] == "operator_gt"
            and refs(b["inputs"].get("OPERAND1"), director.SCORE_ID)
            and is_fifth_place_item(b["inputs"].get("OPERAND2"))
            for b in stage_blocks.values()
        )
        if not compares_fifth:
            failures.add("qualified-compares-fifth-place")

        qualify_block = next(
            (
                bid
                for bid in body
                if stage_blocks[bid]["opcode"] == "data_setvariableto"
                and stage_blocks[bid]["fields"].get("VARIABLE", [None, None])[1]
                == director.QUALIFIED_ID
            ),
            None,
        )
        reaches_transition = False
        if qualify_block is not None:
            cursor, steps = stage_blocks[qualify_block]["next"], 0
            while cursor and steps < 10:
                b = stage_blocks[cursor]
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.PROCCODE
                ):
                    reaches_transition = True
                    break
                cursor, steps = b["next"], steps + 1
        if qualify_block is None or not reaches_transition:
            failures.add("qualified-is-set")

        return failures

    def test_game_over_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco04_failures(project))

    def test_high_score_table_matches_scores_json(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value[1] for value in stage["lists"].values()}
        data = json.loads((ROOT / "docs" / "spec" / "data" / "scores.json").read_text())
        expected = data["tables"]["high_score_defaults"]["scores"]
        self.assertEqual(expected, by_name["high score table"])

    def test_game_over_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._eco04_failures(base))

        def death_blocks(p: dict) -> dict:
            return next(t for t in p["targets"] if t["name"] == "solv_death")["blocks"]

        def hud_blocks(p: dict) -> dict:
            return next(t for t in p["targets"] if t["name"] == "hud")["blocks"]

        def stage_blocks(p: dict) -> dict:
            return next(t for t in p["targets"] if t["isStage"])["blocks"]

        def break_hold_ticks(p: dict) -> None:
            blocks = death_blocks(p)
            for b in blocks.values():
                if b["opcode"] == "control_repeat" and b["inputs"].get("TIMES") == [
                    1,
                    [4, director.GAME_OVER_HOLD_TICKS],
                ]:
                    b["inputs"]["TIMES"] = [1, [4, director.GAME_OVER_HOLD_TICKS - 1]]

        def break_hold_guard(p: dict) -> None:
            blocks = death_blocks(p)
            hold = next(
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "control_repeat"
                and b["inputs"].get("TIMES") == [1, [4, director.GAME_OVER_HOLD_TICKS]]
            )
            guard = blocks[hold]["next"]
            and_block = blocks[blocks[guard]["inputs"]["CONDITION"][1]]
            epoch_eq = blocks[and_block["inputs"]["OPERAND1"][1]]
            epoch_eq["inputs"]["OPERAND1"][1][2] = "corrupted-epoch-id"

        def inject_unguarded_broadcast(p: dict) -> None:
            blocks = death_blocks(p)
            blocks["injected-gameover-bypass"] = {
                "opcode": "event_broadcast",
                "next": None,
                "parent": None,
                "inputs": {
                    "BROADCAST_INPUT": [
                        1,
                        [11, "game over complete", director.MESSAGES["game over complete"]],
                    ]
                },
                "fields": {},
                "shadow": False,
                "topLevel": False,
            }

        def break_hud_gate(p: dict) -> None:
            blocks = hud_blocks(p)
            for b in blocks.values():
                if (
                    b["opcode"] == "control_if"
                    and isinstance(b["inputs"].get("CONDITION"), list)
                    and len(b["inputs"]["CONDITION"]) > 1
                    and blocks.get(b["inputs"]["CONDITION"][1], {}).get("opcode")
                    == "operator_equals"
                    and blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND2")
                    == [1, [10, "game-over"]]
                    and blocks[b["inputs"]["CONDITION"][1]]["inputs"]["OPERAND1"][1][2]
                    == director.STATE_ID
                ):
                    blocks[b["inputs"]["CONDITION"][1]]["inputs"]["OPERAND2"] = [1, [10, "title"]]

        def break_hud_spawn_count(p: dict) -> None:
            blocks = hud_blocks(p)
            for b in blocks.values():
                if b["opcode"] == "data_setvariableto" and b["inputs"].get("VALUE") == [
                    1,
                    [4, director.HUD_ROLE_GAME_OVER_GLYPH],
                ]:
                    b["inputs"]["VALUE"] = [1, [4, director.HUD_ROLE_LABEL_HIGH_SCORE]]

        def break_hud_role_dispatch(p: dict) -> None:
            blocks = hud_blocks(p)
            for b in blocks.values():
                if (
                    b["opcode"] == "control_if"
                    and isinstance(b["inputs"].get("CONDITION"), list)
                    and len(b["inputs"]["CONDITION"]) > 1
                    and blocks.get(b["inputs"]["CONDITION"][1], {}).get("opcode")
                    == "operator_equals"
                    and blocks[b["inputs"]["CONDITION"][1]]["inputs"].get("OPERAND2")
                    == [1, [4, director.HUD_ROLE_GAME_OVER_GLYPH]]
                ):
                    blocks[b["inputs"]["CONDITION"][1]]["inputs"]["OPERAND2"] = [1, [4, 99]]

        def break_fifth_place_index(p: dict) -> None:
            blocks = stage_blocks(p)
            for b in blocks.values():
                if b["opcode"] == "data_itemoflist" and b["fields"].get(
                    "LIST", [None, None]
                )[1] == director.HIGH_SCORE_TABLE_ID:
                    b["inputs"]["INDEX"] = [1, [4, 1]]

        def break_qualified_set(p: dict) -> None:
            blocks = stage_blocks(p)
            for b in blocks.values():
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.QUALIFIED_ID
                ):
                    b["fields"]["VARIABLE"] = ["score", director.SCORE_ID]

        cases = [
            ("game-over-hold-64-ticks", break_hold_ticks),
            ("game-over-hold-epoch-guarded", break_hold_guard),
            ("game-over-broadcast-not-guarded", inject_unguarded_broadcast),
            ("hud-game-over-glyphs-gated", break_hud_gate),
            ("hud-game-over-glyphs-spawned", break_hud_spawn_count),
            ("hud-game-over-role-dispatch", break_hud_role_dispatch),
            ("qualified-compares-fifth-place", break_fifth_place_index),
            ("qualified-is-set", break_qualified_set),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._eco04_failures(project), label)

    @staticmethod
    def _rng_reseed_guard_scopes(project: dict) -> set:
        """The reset scopes that guard the `rng state` reseed (should be exactly the two
        world-reset scopes) — so seeded runs repeat and a mid-game reset never reseeds."""
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        # The reseed sets `rng state` to a literal (the cold-start seed); the rng-step
        # block also writes `rng state`, but to a reporter expression — exclude it.
        seed_ids = [
            bid
            for bid, b in blocks.items()
            if b["opcode"] == "data_setvariableto"
            and b["fields"]["VARIABLE"][0] == "rng state"
            and b["inputs"].get("VALUE", [None])[0] == 1
        ]
        if len(seed_ids) != 1:
            return set()
        seed_id = seed_ids[0]
        guard = None
        for b in blocks.values():
            if b["opcode"] != "control_if":
                continue
            substack = b["inputs"].get("SUBSTACK")
            cursor = substack[1] if substack else None
            while cursor:
                if cursor == seed_id:
                    guard = b
                    break
                cursor = blocks[cursor]["next"]
            if guard:
                break
        if guard is None:
            return set()
        condition = blocks[guard["inputs"]["CONDITION"][1]]
        if condition["opcode"] != "operator_or":
            return set()
        scopes = set()
        for key in ("OPERAND1", "OPERAND2"):
            equals = blocks[condition["inputs"][key][1]]
            scopes.add(equals["inputs"]["OPERAND2"][1][1])
        return scopes

    def test_rng_reseed_scoped_to_world_reset(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(
            {"cold-start", "new-game"}, self._rng_reseed_guard_scopes(project)
        )
        # Negative: widen the guard to new-life and the scope set no longer matches.
        corrupted = copy.deepcopy(project)
        stage = next(t for t in corrupted["targets"] if t["isStage"])
        for b in stage["blocks"].values():
            if (
                b["opcode"] == "operator_equals"
                and b["inputs"].get("OPERAND2", [None, [None, None]])[1][1] == "new-game"
            ):
                b["inputs"]["OPERAND2"][1][1] = "new-life"
        self.assertNotEqual(
            {"cold-start", "new-game"}, self._rng_reseed_guard_scopes(corrupted)
        )

    def test_collision_groups_match_spec(self) -> None:
        # Exactly five groups, no others; each an (attacker range, victim range) over the
        # recorded slot ranges (core-game-systems SYS-03).
        groups = director.COLLISION_GROUPS
        self.assertEqual(len(groups), 5)
        # Independent literals (arcade slot 0xNN -> index NN+1), so the check pins the
        # groups against the spec's five interactions, not against the generator's own
        # constants that built the tuple.
        self.assertEqual(
            groups,
            (
                ((37, 39), (59, 64)),  # player shots (0x24-0x26) vs air enemies (0x3A-0x3F)
                ((34, 34), (1, 16)),   # bomb (0x21) vs ground objects (0x00-0x0F)
                ((40, 58), (36, 36)),  # enemy shots (0x27-0x39) vs player (0x23)
                ((59, 64), (36, 36)),  # air enemies (0x3A-0x3F) vs player (0x23)
                ((17, 32), (36, 36)),  # Bacura (0x10-0x1F) vs player (0x23)
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

    def test_hit_windows_match_spec(self) -> None:
        # PLY-02 collision hit windows (player-craft-and-weapons.md), in the reference's
        # half-pixel "shadow" units as (y_bias, y_width, x_bias, x_width). Dormant data
        # this slice; pinned to independent literals so a wrong window reddens here.
        self.assertEqual(director.HIT_WINDOW_BULLET_FLYING, (8, 16, 4, 8))
        self.assertEqual(director.HIT_WINDOW_BACURA, (28, 40, 8, 16))
        # The bullet allocator's result var is its own, never the blaster's (no coupling).
        self.assertNotEqual(director.BULLET_ALLOC_RESULT_ID, director.ALLOC_RESULT_ID)
        self.assertEqual(director.BULLET_TYPE, 2)

    def _enemy_bullet_pool_failures(self, project: dict) -> set:
        # AIR-12 dormant allocator: defined on the Stage, sweeps the 19 bullet slots with
        # its own result var, marks the bullet type — and is NOT called this slice (no
        # firer). Structural only; the aimed vector/movement/pulse are the air slice.
        fails = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        sblocks = stage["blocks"]
        if not any(
            b.get("opcode") == "procedures_prototype"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
            for b in sblocks.values()
        ):
            fails.add("bullet-alloc-defined")
        called = any(
            b.get("opcode") == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
            for t in project["targets"]
            for b in t["blocks"].values()
        )
        if called:
            fails.add("bullet-alloc-dormant")
        if not any(
            b.get("opcode") == "data_replaceitemoflist"
            and b["fields"].get("LIST", [None])[0] == "slot type"
            and b["inputs"].get("ITEM") == [1, [4, director.BULLET_TYPE]]
            for b in sblocks.values()
        ):
            fails.add("bullet-type")
        span = director.BULLET_SLOTS[1] - director.BULLET_SLOTS[0] + 1
        if not any(
            b.get("opcode") == "control_repeat"
            and self._numeric(b["inputs"].get("TIMES")) == span
            for b in sblocks.values()
        ):
            fails.add("bullet-cap")
        if not any(
            b.get("opcode") == "data_setvariableto"
            and b["fields"].get("VARIABLE", [None])[0] == "bullet alloc result"
            for b in sblocks.values()
        ):
            fails.add("bullet-result-var")
        return fails

    def test_enemy_bullet_pool_foundation(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._enemy_bullet_pool_failures(base))

    def test_enemy_bullet_pool_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._enemy_bullet_pool_failures(base))

        def stage_blocks_of(project):
            return next(t for t in project["targets"] if t["isStage"])["blocks"]

        def break_bullet_type(p):  # allocator writes the wrong occupancy type
            b = next(
                b
                for b in stage_blocks_of(p).values()
                if b.get("opcode") == "data_replaceitemoflist"
                and b["fields"].get("LIST", [None])[0] == "slot type"
                and b["inputs"].get("ITEM") == [1, [4, director.BULLET_TYPE]]
            )
            b["inputs"]["ITEM"] = [1, [4, director.SHOT_TYPE]]

        def break_bullet_cap(p):  # allocator sweeps the wrong number of slots
            span = director.BULLET_SLOTS[1] - director.BULLET_SLOTS[0] + 1
            b = next(
                b
                for b in stage_blocks_of(p).values()
                if b.get("opcode") == "control_repeat"
                and self._numeric(b["inputs"].get("TIMES")) == span
            )
            b["inputs"]["TIMES"] = [1, [4, span - 1]]

        for label, corrupt in (
            ("bullet-type", break_bullet_type),
            ("bullet-cap", break_bullet_cap),
        ):
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(
                label,
                self._enemy_bullet_pool_failures(project),
                f"corruption '{label}' was not caught",
            )

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
        self.assertEqual(64, director.GAME_OVER_HOLD_TICKS)  # ECO-04: 128-frame hold

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
            # PLY-02 / audit B11: a new life now restarts the current area from its top, so the
            # terrain rewinds on new-life too (retiring the interim preserve-terrain fixture).
            self.assertEqual({"cold-start", "new-game", "new-life"}, scope_literals(blocks))
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
            "3779968d0ee46315af9be39579e5a194470a027d0b93b618b770e8eed63f219c",
            build_hash,
        )


if __name__ == "__main__":
    unittest.main()
