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


def _num_operand(inp):
    """The integer value of a numeric-literal block input (`[shadow, [type, "value"]]`), else None."""
    if (
        isinstance(inp, list)
        and len(inp) >= 2
        and isinstance(inp[1], list)
        and len(inp[1]) >= 2
        and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
    ):
        try:
            return int(inp[1][1])
        except (ValueError, TypeError):
            return None
    return None


def _const_item(block):
    """The integer literal written by a `data_replaceitemoflist` ITEM input, else None."""
    return _num_operand(block["inputs"].get("ITEM"))


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
        # 33: the historical 15 + the generated hud, the sprite-extraction proof, the slice-8 toroid +
        # enemy-bullet renderers, the slice-10 terrazi + kapi + torkan + zoshi + jara renderers, the
        # slice-11 zakato renderer (AIR-07; the two Brag Zakato variants fold into it) + the giddo-spario +
        # brag-spario renderers (AIR-10) + the garu-zakato renderer (AIR-08; all three Spario-style pools
        # reuse the zakato body stand-in by ref) + the bacura renderer (AIR-11; its own reserved band, a
        # single static slab costume with no burst) + the sheonite renderer (AIR-09; the inert escort pair
        # in the shared flying pool, ten costumes with no burst), and the slice-9 barra + garu + logram
        # ground renderers (all reuse proof costumes by ref), and the slice-12 zolbak + derota + garu-derota
        # ground renderers (GND-02/GND-04; all reuse proof costumes by ref), and the slice-13 boza renderer
        # (GND-05; the four outers reuse the Logram proof costumes by ref, the centre adds its own core costume).
        self.assertEqual(37, len(project["targets"]))
        # 162: the historical 98 + the 7 Terrazi roll-frame PNGs (AIR-06) + the 7 Kapi dive-frame PNGs
        # (AIR-05) + the 6 Torkan roll-frame PNGs (AIR-02; the arcade's 7 sprite codes 0x10..0x16 have
        # only 6 distinct ripped frames, so the 7th code-step holds the last frame — see game_director) +
        # the 4 Zoshi spin-frame PNGs (AIR-03) + the 6 Jara spin-frame PNGs (AIR-04) + the 1 Zakato
        # body-frame PNG (AIR-07) + the 8 Bacura slab tumble-frame PNGs (AIR-11) + the 10 Sheonite
        # frame PNGs (AIR-09; 10 distinct costumes for the 10 arcade sprite codes 0x30..0x39) + the 14
        # ground-frame PNGs (GND: 1 Barra idle + 4 Logram open stages + 2 crater variants + 2 Garu base
        # pulse frames + the slice-12 additions: 1 Zolbak idle dome (GND-02) + 1 Derota idle turret + 2 Garu
        # Derota base pulse frames (GND-04) + the slice-13 addition: 1 Boza centre core (GND-05; the four
        # outer domes reuse the Logram open frames by ref, so only the centre is a new crop)) + the 6 arcade
        # gameplay-SFX wavs (AUDIO: the real air_destroy / ground_destroy / zakato-teleport / garu_zakato /
        # bacura / sheonite cues, committed under assets/game-sounds/ and attached to the Stage by
        # tools/hud_glyphs.py; see docs/mechanics/040-arcade-sound-cues.md).
        self.assertEqual(167, len(assets))

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
                # hud_glyphs.py appends its added Stage sounds on top of the historical
                # two (docs/mechanics/010): first the "extend" cue, then the six arcade
                # gameplay-SFX cues in name order (AUDIO; docs/mechanics/040). Verify the
                # exact list, then drop sounds from the general preserved-content comparison.
                self.assertEqual(
                    [sound["name"] for sound in expected["sounds"]]
                    + ["extend", "air_destroy", "bacura", "garu_zakato",
                       "ground_destroy", "sheonite", "zakato"],
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
            "radiating angle",
            # AIR-08 (slice 11): the Garu Zakato detonation temps — the captured Garu x/y and its own slot
            # index, used to place the 16-bullet ring + the 4 adjacent Brag Sparios before it frees its slot.
            "garu det x",
            "garu det y",
            "garu det slot",
            "player row",
            "player col",
            "walk type",
            "spawn cursor",
            "spawn attempts",
            "spawn found",
            # PLY-02 (slice 8): the walk raises `player hit` on craft contact; the non-warp thread
            # clears it and triggers the death. `invuln` is the dormant debug flag (default 0, set only
            # by the test harness) that gates that death so the agency-less headless craft can survive.
            "player hit",
            "invuln",
            # DEBUG (tracked for removal, #119): the T-key family-cycle cursor — a transient dev-tool
            # register, not Stage-write-protected state.
            "debug spawn index",
            # DEBUG (tracked for removal, #119): the G-key GROUND family-cycle cursor — the ground analog
            # of the T-key cursor, likewise a transient dev-tool register, not durable Stage state.
            "debug ground index",
            # DEBUG (tracked for removal, #119): the P-key freeze/resume TOGGLE (1 = frozen) and its
            # previous-tick P sample for rising-edge detection — transient dev-tool registers, not durable
            # Stage state (both default 0; the harness never presses P, so the walk runs every tick).
            "debug paused",
            "debug pause key held",
            # WPN-04 (slice 9): the in-flight bomb's accelerating scroll-axis velocity — a transient
            # working register the walk's `advance bomb` writes each sub-step (the bomb renderer reads
            # it for its falling-frame animation). Machinery, not durable Stage state.
            "bomb dx",
            # AIR-11 (slice 11): the live Bacura spawn pump's registers — the active slab count, the
            # remaining one-per-second increments, the frame countdown to the next increment, and the
            # init loop's band cursor. Stage-written by the pump proc, never sprite-written; transient
            # spawn machinery like the `spawn cursor` family, re-topped per area (not durable state).
            "num bacura",
            "bacura inc cnt",
            "one second cntr",
            "bacura seed slot",
            # AIR-09 (slice 11): the Sheonite escort's schedule on/off flag (Stage-written by
            # sheonite_start/end, read by the walk, cleared per area) plus the two per-tick update
            # temps (the phase snapshot that keeps a mid-tick transition from cascading, and the
            # resolved lateral lock cell). Transient spawn/working machinery, never sprite-written.
            "sheonite end flag",
            "sheonite phase",
            "sheonite lock col",
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
                # GND-05 Boza composite: each outer slot stores the field index of its centre slot, so an
                # outer hit can downgrade the centre's value and the centre hit can cascade the outers.
                "slot link",
                # AIR-06 fire-permission per-slot fields (the shared gate): captured mask + countdown.
                "slot fire mask",
                "slot fire timer",
                # AIR-01/AIR-12 homing-aim tables: the octant quantizer + the speed tiers (24 Toroid,
                # 32 generic bullets; 48 terrazi/torkan — AIR-06, baked ahead of its consumer).
                "octant table",
                "aim dy 24",
                "aim dx 24",
                "aim dy 32",
                "aim dx 32",
                "aim dy 48",
                "aim dx 48",
                "aim dy 64",
                "aim dx 64",
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
                # GND: the three add_ground_object scalar columns (object type / slot / sprite_y).
                "schedule ground type",
                "schedule ground slot",
                "schedule ground sprite y",
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
            # AIR-06 Terrazi family (slice 10): its spawn init and per-tick update, both warp, no
            # state write — dispatched from the same spawner / walk as the Toroid; plus the shared,
            # family-agnostic fire-permission gate it calls to fire under its captured mask.
            director.INIT_TERRAZI_PROCCODE,
            director.UPDATE_TERRAZI_PROCCODE,
            director.FIRE_GATE_PROCCODE,
            # AIR-05 Kapi family (slice 10): its spawn init and per-tick peel-away dive update, both
            # warp, no state write — dispatched from the same spawner / walk, calling the shared gate.
            director.INIT_KAPI_PROCCODE,
            director.UPDATE_KAPI_PROCCODE,
            # AIR-02 Torkan family (slice 10): its spawn init and per-tick attack-and-retreat update,
            # both warp, no state write — dispatched from the same spawner / walk. It fires ONE bullet
            # directly (via the allocator, not the fire gate), so it takes no mask and no gate call.
            director.INIT_TORKAN_PROCCODE,
            director.UPDATE_TORKAN_PROCCODE,
            # AIR-03 Zoshi family (slice 10): three spawn inits (top/bottom/rnd) over ONE shared
            # movement/anim/fire update, all warp, no state write — dispatched from the same spawner /
            # walk. Its update replicates the fire gate INLINE (re-heading the enemy drift and firing
            # share one trigger), so it fires via the allocator, not a FIRE_GATE call.
            director.INIT_ZOSHI_TOP_PROCCODE,
            director.INIT_ZOSHI_BOTTOM_PROCCODE,
            director.INIT_ZOSHI_RND_PROCCODE,
            director.UPDATE_ZOSHI_PROCCODE,
            # AIR-04 Jara family (slice 10, the final aerial): ONE shared spawn init and ONE shared
            # per-tick approach->turn update over both types (0x55 shooter / 0x56 silent), both warp,
            # no state write — dispatched from the same spawner / walk. The shooter fires ONE bullet
            # directly (via the allocator, gated inside the approach->turn transition), so it takes no
            # mask and no fire-gate call; the silent type never fires.
            director.INIT_JARA_PROCCODE,
            director.UPDATE_JARA_PROCCODE,
            # AIR-07 Zakato family (slice 11): ONE shared spawn init and ONE shared per-tick
            # teleport->active->self-destruct update over all four base variants (slow / close-Y / fast /
            # continuous), both warp, no state write beyond the slot's own phase machine — dispatched from
            # the same spawner / walk. Each variant fires ONE aimed bullet directly (via the allocator) at
            # its trigger, then self-destructs awarding nothing, so it takes no mask and no fire-gate call.
            director.INIT_ZAKATO_PROCCODE,
            director.UPDATE_ZAKATO_PROCCODE,
            # AIR-10 (slice 11) air.spario: the Giddo Spario (aim-once 64-tier flyby with its own short
            # burst) and Brag Spario (accelerating homer, spawned only from the Garu detonation) lifecycle
            # procs — one init + one update each, plus the Giddo's distinct burst tick.
            director.INIT_GIDDO_SPARIO_PROCCODE,
            director.UPDATE_GIDDO_SPARIO_PROCCODE,
            director.EXPLODE_GIDDO_SPARIO_PROCCODE,
            director.INIT_BRAG_SPARIO_PROCCODE,
            director.UPDATE_BRAG_SPARIO_PROCCODE,
            # AIR-09 (slice 11) air.sheonite: the escort pair's ONE shared per-tick home->lock->combine->
            # retreat/vanish update over both types (0x31 right / 0x32 left), warp, dispatched from the walk.
            # It writes only the slot's own phase machine plus the stage-owned sheonite scratch vars, and
            # deliberately makes NO CHECK_AIR_HIT call and drives no craft detector — the pair is wholly inert.
            director.UPDATE_SHEONITE_PROCCODE,
            # AIR-08 (slice 11) air.special-pairs: the two Brag Zakato variants (rnd/closeY) share one
            # teleport->active->self-destruct update ending in a 5-bullet radiating fan (`brag zakato
            # shoot`); the Garu Zakato has its own no-teleport straight update whose fuse detonates into a
            # 16-bullet ring + 4 Brag Sparios (`garu zakato detonate`). All warp, no state write beyond the
            # slot's own phase machine — dispatched from the same spawner / walk (the Garu stamped by the
            # debug key). Plus the shared radiating-bullet emitter (AIR-12, slice 11) whose first live
            # callers are this family's fan and ring.
            director.INIT_BRAG_ZAKATO_PROCCODE,
            director.UPDATE_BRAG_ZAKATO_PROCCODE,
            director.BRAG_ZAKATO_SHOOT_PROCCODE,
            director.INIT_GARU_ZAKATO_PROCCODE,
            director.UPDATE_GARU_ZAKATO_PROCCODE,
            director.GARU_ZAKATO_DETONATE_PROCCODE,
            director.RADIATING_EMIT_PROCCODE,
            # AIR-11 (slice 11) air.bacura: the indestructible slab's spawn init and per-tick update, both
            # warp, no state write beyond the slot's own drift. Dispatched from the walk by BAND membership
            # (its own reserved band 17-32), not by type; the init is also called by the debug direct-stamp
            # and (Commit 3) the live per-second spawn pump. Its update deliberately makes NO CHECK_AIR_HIT
            # call — that omission is the shot-invulnerability.
            director.INIT_BACURA_PROCCODE,
            director.UPDATE_BACURA_PROCCODE,
            # AIR-11 (Commit 3): the per-tick live spawn pump (inc counter -> init the band's empty slots),
            # ported from main_fn_5__inc_num_bacura + main_fn_3__init_bacura.
            director.PUMP_BACURA_PROCCODE,
            # WPN-01 (slice 11) player.bacura-bounce: the shot-vs-Bacura detector, called per live slab from
            # `update bacura`. It marks an overlapping player shot SHOT_BOUNCE and never touches the slab —
            # the shot bounces (blaster clone reverses+animates), the Bacura is never destroyed or scored.
            director.CHECK_SHOT_BACURA_PROCCODE,
            # DEBUG / temporary (tracked for removal): the playtest spawn-a-wave tool.
            director.DEBUG_SPAWN_PROCCODE,
            # DEBUG / temporary (tracked for removal, #119): the playtest cycle-a-ground-family tool.
            director.DEBUG_GROUND_SPAWN_PROCCODE,
            # DEBUG / temporary (tracked for removal, #119): the playtest freeze/resume (P) toggle.
            director.DEBUG_PAUSE_PROCCODE,
            director.CULL_SLOT_PROCCODE,
            # WPN-02 (slice 8): the shot-vs-air overlap detector and the struck-Toroid explosion tick.
            director.CHECK_AIR_HIT_PROCCODE,
            director.EXPLODE_TICK_PROCCODE,
            # AIR-12 (slice 8): the enemy-bullet per-tick update (move, craft-collision, cull) and
            # the bullet allocator the shooting Toroid now calls to fire its single aimed bullet.
            director.UPDATE_BULLET_PROCCODE,
            director.ALLOC_BULLET_PROCCODE,
            # WPN-04 (slice 9) player.ground-targeting: the walk tracks the bomb sight ahead of the
            # craft (`track crosshair`) and arms/flies the bomb (`advance bomb`), which on its finish
            # sub-step resolves ground objects under the locked target through `check ground hit`
            # (economy.ground-awards, slice 9). All warp, no state write.
            director.TRACK_CROSSHAIR_PROCCODE,
            director.ADVANCE_BOMB_PROCCODE,
            director.CHECK_GROUND_HIT_PROCCODE,
            # AREA-04 (slice 9) area.ground-dispatch: the terrain-locked scroll+cull one built ground
            # families share, dispatched per OCCUPIED ground slot from the walk. Warp, and the only slot
            # writes are its own scroll of `slot x`; the spawn stamp lives in `advance area`'s schedule
            # consume, not a proc call.
            director.ADVANCE_GROUND_PROCCODE,
            # GND-01 (slice 9) ground.barra: the Barra's thin per-tick wrapper — it advances the HIT
            # explosion/crater clock, then delegates the terrain scroll+cull to `advance ground`.
            # Warp, dispatched per OCCUPIED Barra slot from the walk.
            director.UPDATE_BARRA_PROCCODE,
            # GND-01 (slice 9) ground.barra: the Garu Barra's thin per-tick wrapper — for a HIT node it
            # advances the explode-and-remove clock then removes the slot; base and active node delegate
            # the terrain scroll+cull to `advance ground`. Warp, dispatched per OCCUPIED Garu slot.
            director.UPDATE_GARU_PROCCODE,
            # GND (slice 9) ground.logram: the Logram's per-tick wrapper — a HIT Logram runs the Barra
            # crater clock; an ACTIVE one runs the gated open/close + single-shot cycle; both delegate the
            # terrain scroll+cull to `advance ground`. Warp, dispatched per OCCUPIED Logram slot.
            director.UPDATE_LOGRAM_PROCCODE,
            # GND-02 (slice 12) ground.zolbak: the passive dome's per-tick wrapper — a HIT Zolbak reduces the
            # adaptive AI level by 2 (floored at 0) exactly once, then runs the Barra crater clock; an ACTIVE
            # one just delegates the terrain scroll+cull to `advance ground`. It never fires. Warp, dispatched
            # per OCCUPIED Zolbak slot.
            director.UPDATE_ZOLBAK_PROCCODE,
            # GND-04 (slice 12) ground.derota: the firing turret's per-tick wrapper — a HIT Derota runs the
            # Barra crater clock; an ACTIVE one, only once scrolled past the ground stop-firing row, fires one
            # aimed bullet per masked reload through the shared `fire permission gate`; both delegate the
            # terrain scroll+cull to `advance ground`. Warp, dispatched per OCCUPIED Derota slot.
            director.UPDATE_DEROTA_PROCCODE,
            # GND-04 (slice 12) ground.derota: the Garu Derota's per-tick wrapper — like the Garu Barra it has
            # an indestructible base and a destructible node, but the node FIRES: an ACTIVE node fires one aimed
            # bullet per masked reload through the shared `fire permission gate` (unconditionally, no stop-firing
            # row), a HIT node runs the explode-and-remove clock then removes the slot, and base/active delegate
            # the terrain scroll+cull to `advance ground`. Warp, dispatched per OCCUPIED Garu Derota slot.
            director.UPDATE_GARU_DEROTA_PROCCODE,
            # GND-05 (slice 13) ground.boza-logram: the Boza composite's per-tick wrapper, dispatched per
            # OCCUPIED Boza slot. An outer dome runs the shared Logram open/close + single-shot cycle (gated by
            # the stop-firing row) and, on hit, downgrades its linked centre's value; the centre never fires and,
            # on hit, cascades all four outers to HIT directly (the arcade `destroy_all_outer_lograms`); both
            # delegate the terrain scroll+cull to `advance ground`. Warp.
            director.UPDATE_BOZA_PROCCODE,
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
            director.GROUND_OBJECT_TYPE_ID,
            director.GROUND_OBJECT_SLOT_ID,
            director.GROUND_OBJECT_SPRITE_Y_ID,
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
    def _air06_failures(project: dict) -> set:
        """AIR-06 Terrazi authoring contract — violated labels. Pins the structural facts that make
        Terrazi a faithful live family: its init/update run atomically, the spawner and the ordered walk
        drive it by its own type, it aims on the fast (48-magnitude, 3 px/frame) tier, its update commits
        a glide that reverses its forward/scroll course (the decelerate-and-reverse of `terrazi_main_cont`),
        and it fires periodically through the shared, global-phase fire-permission gate under its captured
        mask."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        # (1) Both Terrazi lifecycle procedures exist and are warp (atomic) — a non-warp walk sub-proc
        # would yield mid-slot, letting a half-moved enemy render or be hit.
        for proccode in (director.INIT_TERRAZI_PROCCODE, director.UPDATE_TERRAZI_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("terrazi-lifecycle-procs-warp")

        # (2) The spawner inits Terrazi by type, so a Terrazi-typed formation slot becomes live; (3) the
        # ordered walk dispatches to its updater, so a spawned Terrazi actually advances.
        if not calls(director.INIT_TERRAZI_PROCCODE):
            failures.add("spawn-inits-terrazi")
        if not calls(director.UPDATE_TERRAZI_PROCCODE):
            failures.add("dispatch-updates-terrazi")

        # (4) The spawn init aims on the FAST tier — it reads both the 48-magnitude aim tables (3
        # px/frame), not the Toroid's 24-magnitude (1.5 px/frame) tables.
        init_lists = {
            b["fields"]["LIST"][1]
            for b in _proc_body_blocks(stage, director.INIT_TERRAZI_PROCCODE)
            if b["opcode"] == "data_itemoflist"
        }
        if not {director.AIM_DX_48_ID, director.AIM_DY_48_ID} <= init_lists:
            failures.add("terrazi-aims-fast-tier")

        # (5) The update commits a glide that REVERSES the forward/scroll course: it latches the slot flag
        # to GLIDE and decelerates the forward velocity (`slot dx`, the `_dX`/scroll axis the arcade
        # `subq #2,_dX` reverses — dir_delta_tbl 2172 fixes `_X` as the scroll axis) — the decelerate-and-
        # reverse. Without the `slot dx` change the enemy would only dive; without the flag latch it would
        # never commit.
        update_body = _proc_body_blocks(stage, director.UPDATE_TERRAZI_PROCCODE)
        writes_slot_dx = any(
            b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_DX_ID
            for b in update_body
        )
        latches_glide = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            for b in update_body
        )
        if not (writes_slot_dx and latches_glide):
            failures.add("terrazi-glide-reverses")

        # ---- Fire-permission gate (the shared, family-agnostic periodic-fire machinery) ----
        # (6) The gate procedure exists and is warp (atomic in the walk).
        gate = proto(director.FIRE_GATE_PROCCODE)
        if gate is None or gate["mutation"].get("warp") != "true":
            failures.add("fire-gate-warp")

        # (7) The spawn init captures the family's fire mask AND seeds the per-slot fire countdown, so a
        # freshly spawned Terrazi carries its own periodic-fire state.
        init_writes = {
            b["fields"]["LIST"][1]
            for b in _proc_body_blocks(stage, director.INIT_TERRAZI_PROCCODE)
            if b["opcode"] == "data_replaceitemoflist"
        }
        if not {director.SLOT_FIRE_MASK_ID, director.SLOT_FIRE_TIMER_ID} <= init_writes:
            failures.add("terrazi-captures-fire-state")

        # (8) The Terrazi update drives the gate, so a live Terrazi actually fires under it.
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
            for b in _proc_body_blocks(stage, director.UPDATE_TERRAZI_PROCCODE)
        ):
            failures.add("terrazi-update-drives-gate")

        # Numeric-shape invariants on the gate body (the harness is frame-blind to cadence, so the
        # every-8th-frame phase and the reload cap are proven statically here).
        gate_blocks = _proc_body_blocks(stage, director.FIRE_GATE_PROCCODE)

        def num_operand(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], list) and len(inp[1]) >= 2 and inp[1][0] in (4, 5, 6, 7, 8, 9, 10):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def var_id(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], list) and len(inp[1]) >= 3 and inp[1][0] in (12, 13):
                return inp[1][2]
            return None

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        # (9) GLOBAL 8-arcade-frame phase: `tick mod FIRE_GATE_PHASE_TICKS` — the tick variable modulo
        # the phase constant. Without the right modulus the every-8th-frame cadence is wrong.
        phased = any(
            b["opcode"] == "operator_mod"
            and var_id(b["inputs"].get("NUM1")) == director.TICK_ID
            and num_operand(b["inputs"].get("NUM2")) == director.FIRE_GATE_PHASE_TICKS
            for b in gate_blocks
        )
        if not phased:
            failures.add("fire-gate-global-phase")

        # (10) Reload = (rng & mask) + 1: an `operator_add` of exactly 1 onto an `operator_mod` that
        # reads the shared stream — the +1 is the spawn/reload asymmetry and the reason there is NO
        # zero-suppression (mask 0 -> reload 1 -> fastest). Dropping the +1 is the zero-suppression bug.
        reload_plus_one = False
        for b in gate_blocks:
            if b["opcode"] != "operator_add" or num_operand(b["inputs"].get("NUM2")) != 1:
                continue
            inner = blocks.get(ref(b["inputs"].get("NUM1")))
            if inner and inner["opcode"] == "operator_mod" and var_id(inner["inputs"].get("NUM1")) == director.RNG_OUT_ID:
                reload_plus_one = True
        if not reload_plus_one:
            failures.add("fire-gate-reload-plus-one")

        # (11) BYTE-underflow decrement: the per-slot countdown is decremented AS A BYTE —
        # `((slot fire timer - 1) + FIRE_TIMER_BYTE_MOD) mod FIRE_TIMER_BYTE_MOD` — so a countdown of 0
        # wraps to 255 and keeps counting down (the reference's `subq.b #1,(_TIMER)` underflow: `0-1`
        # is 255, nonzero, so it does not fire), never going negative and never sticking. Without the
        # mod-256 wrap a 0 draw would decrement to -1 and the `== 0` fire test could never come back
        # around. The decrement's mod is the only `operator_mod` whose divisor is the bare byte modulus
        # (the phase divides by FIRE_GATE_PHASE_TICKS, the reload by an `operator_add` of mask+1).
        byte_wrap = False
        for b in gate_blocks:
            if b["opcode"] != "operator_mod" or num_operand(b["inputs"].get("NUM2")) != director.FIRE_TIMER_BYTE_MOD:
                continue
            add = blocks.get(ref(b["inputs"].get("NUM1")))
            if not (add and add["opcode"] == "operator_add" and num_operand(add["inputs"].get("NUM2")) == director.FIRE_TIMER_BYTE_MOD):
                continue
            sub = blocks.get(ref(add["inputs"].get("NUM1")))
            if not (sub and sub["opcode"] == "operator_subtract" and num_operand(sub["inputs"].get("NUM2")) == 1):
                continue
            item = blocks.get(ref(sub["inputs"].get("NUM1")))
            if item and item["opcode"] == "data_itemoflist" and item["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID:
                byte_wrap = True
        if not byte_wrap:
            failures.add("fire-gate-byte-underflow")
        return failures

    # Roadmap closure evidence for leaf `air.terrazi` (AIR-06.terrazi): Terrazi is a live family —
    # spawned by type from the formation wave, advanced by the ordered walk, aimed on the 3 px/frame
    # tier, committing a glide that reverses its forward/scroll course when it draws level laterally with
    # the craft. The live proof (spawns, moves, glide reverses `slot dx`) is the harness
    # `terrazi-wave-spawns-and-moves` / `terrazi-glides-and-reverses`. The masked periodic fire (the
    # shared fire-permission gate) carries the firing half of AIR-06.
    # roadmap-evidence: AIR-06 success  (test_terrazi_slice_authoring_present — lifecycle procs warp, spawn+dispatch driven by type, fast-tier aim, glide reverses lateral course)
    # roadmap-evidence: AIR-06 failure  (test_terrazi_slice_negative_fixtures — each contract clause corrupted bites)
    def test_terrazi_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air06_failures(project))

    def test_terrazi_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air06_failures(base))

        def unwarp_update(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_TERRAZI_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_init_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.INIT_TERRAZI_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_dispatch_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_TERRAZI_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def aim_slow_tier(p: dict) -> None:
            # Repoint the fast-tier aim reads to the Toroid's 24-magnitude tables → the fast-tier
            # clause no longer holds.
            stage = next(t for t in p["targets"] if t["isStage"])
            swap = {director.AIM_DX_48_ID: ("aim dx 24", director.AIM_DX_24_ID), director.AIM_DY_48_ID: ("aim dy 24", director.AIM_DY_24_ID)}
            for b in _proc_body_blocks(stage, director.INIT_TERRAZI_PROCCODE):
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] in swap:
                    b["fields"]["LIST"] = list(swap[b["fields"]["LIST"][1]])

        def drop_forward_reverse(p: dict) -> None:
            # Repoint the glide's `slot dx` decel write to a scratch list → the forward reverse never
            # happens (the enemy keeps its approach velocity instead of decelerating and peeling away).
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TERRAZI_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DX_ID
                ):
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def unwarp_gate(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_fire_state_capture(p: dict) -> None:
            # Repoint the spawn's `slot fire timer` seed to a scratch list → the capture clause fails.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.INIT_TERRAZI_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID
                ):
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def drop_gate_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TERRAZI_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def break_phase_modulus(p: dict) -> None:
            # Phase every tick (mod 1) instead of every 4th → the every-8th-frame cadence is lost.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.FIRE_GATE_PROCCODE):
                if (
                    b["opcode"] == "operator_mod"
                    and isinstance(b["inputs"].get("NUM1"), list)
                    and isinstance(b["inputs"]["NUM1"][1], list)
                    and b["inputs"]["NUM1"][1][0] == 12
                    and b["inputs"]["NUM1"][1][2] == director.TICK_ID
                ):
                    b["inputs"]["NUM2"] = [1, [4, 1]]

        def drop_reload_plus_one(p: dict) -> None:
            # Reload with + 0 instead of + 1 → the zero-suppression bug (a 0 draw sticks at 0).
            stage = next(t for t in p["targets"] if t["isStage"])
            body = _proc_body_blocks(stage, director.FIRE_GATE_PROCCODE)
            for b in body:
                if b["opcode"] != "operator_add":
                    continue
                num2 = b["inputs"].get("NUM2")
                num1 = b["inputs"].get("NUM1")
                if not (isinstance(num2, list) and isinstance(num2[1], list) and num2[1][0] == 4 and int(num2[1][1]) == 1):
                    continue
                inner = stage["blocks"].get(num1[1]) if isinstance(num1, list) and isinstance(num1[1], str) else None
                if inner and inner["opcode"] == "operator_mod":
                    n1 = inner["inputs"].get("NUM1")
                    if isinstance(n1, list) and isinstance(n1[1], list) and n1[1][0] == 12 and n1[1][2] == director.RNG_OUT_ID:
                        b["inputs"]["NUM2"] = [1, [4, 0]]

        def break_byte_wrap(p: dict) -> None:
            # Widen the decrement's byte modulus off 256 (the only bare-literal 256 mod in the gate) →
            # a 0 countdown decrements to -1 instead of wrapping to 255, so the underflow is gone.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.FIRE_GATE_PROCCODE):
                if b["opcode"] != "operator_mod":
                    continue
                num2 = b["inputs"].get("NUM2")
                if isinstance(num2, list) and isinstance(num2[1], list) and num2[1][0] == 4 and int(num2[1][1]) == director.FIRE_TIMER_BYTE_MOD:
                    b["inputs"]["NUM2"] = [1, [4, str(director.FIRE_TIMER_BYTE_MOD * 2)]]

        cases = [
            ("terrazi-lifecycle-procs-warp", unwarp_update),
            ("spawn-inits-terrazi", drop_init_call),
            ("dispatch-updates-terrazi", drop_dispatch_call),
            ("terrazi-aims-fast-tier", aim_slow_tier),
            ("terrazi-glide-reverses", drop_forward_reverse),
            ("fire-gate-warp", unwarp_gate),
            ("terrazi-captures-fire-state", drop_fire_state_capture),
            ("terrazi-update-drives-gate", drop_gate_call),
            ("fire-gate-global-phase", break_phase_modulus),
            ("fire-gate-reload-plus-one", drop_reload_plus_one),
            ("fire-gate-byte-underflow", break_byte_wrap),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air06_failures(project), label)

    @staticmethod
    def _air08_failures(project: dict) -> set:
        """AIR-08 special-pairs authoring contract — violated labels. Pins the structural facts that make
        the two Brag Zakato variants (rnd 0x16 / closeY 0x17) and the Garu Zakato (0x18) faithful live
        families. The biting contrast between them is the entry: the Brags TELEPORT in (SLOT_TELEPORT,
        indestructible while the sparkle plays) and end in a 5-bullet aimed radiating FAN then vanish
        awarding nothing; the Garu enters IMMEDIATELY ACTIVE, flies straight (dX=48, no aim), and on a
        32-63 fuse DETONATES into a 16-bullet ring + 4 Brag Sparios written into the 4 adjacent flying
        slots, then frees itself with no burst and no score."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def body(proccode):
            return _proc_body_blocks(stage, proccode)

        def call_count(body_blocks, proccode):
            return sum(
                1
                for b in body_blocks
                if b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
            )

        def calls_in(body_blocks, proccode):
            return call_count(body_blocks, proccode) > 0

        def read_lists(body_blocks):
            return {
                b["fields"]["LIST"][1]
                for b in body_blocks
                if b["opcode"] == "data_itemoflist"
            }

        def write_consts(body_blocks, list_id):
            return {
                _const_item(b)
                for b in body_blocks
                if b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == list_id
            }

        def writes_const(body_blocks, list_id, value):
            return value in write_consts(body_blocks, list_id)

        def repeats(body_blocks, times):
            return any(
                b["opcode"] == "control_repeat"
                and _num_operand(b["inputs"].get("TIMES")) == times
                for b in body_blocks
            )

        def changes_var(body_blocks, variable_id, delta):
            return any(
                b["opcode"] == "data_changevariableby"
                and b["fields"]["VARIABLE"][1] == variable_id
                and _num_operand(b["inputs"].get("VALUE")) == delta
                for b in body_blocks
            )

        def sets_var_to(body_blocks, variable_id, value):
            return any(
                b["opcode"] == "data_setvariableto"
                and b["fields"]["VARIABLE"][1] == variable_id
                and _num_operand(b["inputs"].get("VALUE")) == value
                for b in body_blocks
            )

        def var_id(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], list) and len(inp[1]) >= 3 and inp[1][0] in (12, 13):
                return inp[1][2]
            return None

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def seeds_fuse(body_blocks, span, offset):
            # A random fuse draw `(rng mod SPAN) + OFFSET` written into `slot fire timer`: an
            # `operator_add` of OFFSET onto an `operator_mod` of the shared stream by SPAN. Both the Brag
            # (rnd variant: span 64, offset 1) and the Garu (span 32, offset 32) draw this way.
            for b in body_blocks:
                if b["opcode"] != "operator_add" or _num_operand(b["inputs"].get("NUM2")) != offset:
                    continue
                inner = blocks.get(ref(b["inputs"].get("NUM1")))
                if (
                    inner
                    and inner["opcode"] == "operator_mod"
                    and var_id(inner["inputs"].get("NUM1")) == director.RNG_OUT_ID
                    and _num_operand(inner["inputs"].get("NUM2")) == span
                ):
                    return True
            return False

        init_brag = body(director.INIT_BRAG_ZAKATO_PROCCODE)
        update_brag = body(director.UPDATE_BRAG_ZAKATO_PROCCODE)
        shoot = body(director.BRAG_ZAKATO_SHOOT_PROCCODE)
        init_garu = body(director.INIT_GARU_ZAKATO_PROCCODE)
        update_garu = body(director.UPDATE_GARU_ZAKATO_PROCCODE)
        detonate = body(director.GARU_ZAKATO_DETONATE_PROCCODE)

        # (1)/(2) Both families' lifecycle procedures exist and are warp (atomic) — a non-warp walk
        # sub-proc would yield mid-slot, letting a half-moved teleporter/flyer render or be hit.
        for proccode in (
            director.INIT_BRAG_ZAKATO_PROCCODE,
            director.UPDATE_BRAG_ZAKATO_PROCCODE,
            director.BRAG_ZAKATO_SHOOT_PROCCODE,
        ):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("brag-zakato-lifecycle-procs-warp")
        for proccode in (
            director.INIT_GARU_ZAKATO_PROCCODE,
            director.UPDATE_GARU_ZAKATO_PROCCODE,
            director.GARU_ZAKATO_DETONATE_PROCCODE,
        ):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("garu-zakato-lifecycle-procs-warp")

        # (3)-(6) Each family is driven: the formation spawner inits the Brags by type, the ordered walk
        # dispatches to both updaters, and the Garu (no formation entry) is stamped by its own spawner
        # (the debug key) — so a spawned member actually advances.
        if not calls(director.INIT_BRAG_ZAKATO_PROCCODE):
            failures.add("spawn-inits-brag-zakato")
        if not calls(director.UPDATE_BRAG_ZAKATO_PROCCODE):
            failures.add("dispatch-updates-brag-zakato")
        if not calls(director.INIT_GARU_ZAKATO_PROCCODE):
            failures.add("stamp-inits-garu-zakato")
        if not calls(director.UPDATE_GARU_ZAKATO_PROCCODE):
            failures.add("dispatch-updates-garu-zakato")

        # (7)/(8) The BITING CONTRAST — the Brag spawn commits SLOT_TELEPORT (indestructible sparkle),
        # the Garu spawn commits SLOT_ACTIVE (hittable at once, no sparkle). Swapping either is the
        # invulnerability bug.
        if not writes_const(init_brag, director.SLOT_STATE_ID, director.SLOT_TELEPORT):
            failures.add("brag-zakato-spawns-teleporting")
        if not writes_const(init_garu, director.SLOT_STATE_ID, director.SLOT_ACTIVE):
            failures.add("garu-zakato-spawns-active")

        # (9) The Brag spawn stamps its active-phase body sprite code.
        if not writes_const(init_brag, director.SLOT_CODE_ID, director.BRAG_ZAKATO_MAIN_CODE):
            failures.add("brag-zakato-stamps-main-code")

        # (10) The Garu flies STRAIGHT down the scroll axis at spawn (dX=48, no aim) — unlike the Brags
        # it never reads the aim tables at init.
        if not writes_const(init_garu, director.SLOT_DX_ID, director.GARU_STRAIGHT_DX):
            failures.add("garu-zakato-flies-straight")

        # (11) Both Brag variants aim at the craft on the generic (32-magnitude, 2 px/frame) tier when the
        # teleport completes — the shared update reads both aim-32 tables.
        if not {director.AIM_DX_32_ID, director.AIM_DY_32_ID} <= read_lists(update_brag):
            failures.add("brag-zakato-aims-generic-tier")

        # (12) On its terminal trigger the Brag fires the fan (BRAG_ZAKATO_SHOOT) AND flips to
        # SLOT_SELF_EXPLODE (benign, the hit gate ignores it) — the fire-then-vanish.
        if not (
            calls_in(update_brag, director.BRAG_ZAKATO_SHOOT_PROCCODE)
            and writes_const(update_brag, director.SLOT_STATE_ID, director.SLOT_SELF_EXPLODE)
        ):
            failures.add("brag-zakato-fires-fan-then-self-explodes")

        # (13) A self-exploding / shot Brag plays the SHARED ~20-frame burst (explode-tick) before it frees.
        if not calls_in(update_brag, director.EXPLODE_TICK_PROCCODE):
            failures.add("brag-zakato-self-explode-shares-burst")

        # (14) The fan is exactly 5 bullets two angle-steps apart, emitted through the shared radiating
        # emitter — a 5-count repeat that calls the emitter and steps `radiating angle` by 2.
        if not (
            repeats(shoot, director.BRAG_ZAKATO_FAN_COUNT)
            and calls_in(shoot, director.RADIATING_EMIT_PROCCODE)
            and changes_var(shoot, director.RADIATING_ANGLE_ID, director.BRAG_ZAKATO_FAN_STEP)
        ):
            failures.add("brag-zakato-fan-emits-five")

        # (15) The rnd Brag draws its 1-64 fuse `(rng mod 64) + 1` on teleport completion.
        if not seeds_fuse(update_brag, director.BRAG_ZAKATO_RND_FUSE_SPAN, 1):
            failures.add("brag-zakato-rnd-fuse-seeded")

        # (16) The Garu draws its 32-63 fuse `(rng mod 32) + 32` at spawn.
        if not seeds_fuse(init_garu, director.GARU_ZAKATO_FUSE_SPAN, director.GARU_ZAKATO_FUSE_OFFSET):
            failures.add("garu-zakato-fuse-seeded")

        # (17) The Garu detonates when its fuse elapses (no self-destruct fan — the whole point of the
        # family) and (18) plays the shared burst only when SHOT (its HIT branch), never on detonation.
        if not calls_in(update_garu, director.GARU_ZAKATO_DETONATE_PROCCODE):
            failures.add("garu-zakato-detonates-on-fuse")
        if not calls_in(update_garu, director.EXPLODE_TICK_PROCCODE):
            failures.add("garu-zakato-hit-shares-burst")

        # (19) The detonation lays a 16-bullet 360-degree ring: a 16-count repeat calling the shared
        # emitter and stepping `radiating angle` by 2, starting from angle 0.
        if not (
            repeats(detonate, director.GARU_RING_COUNT)
            and calls_in(detonate, director.RADIATING_EMIT_PROCCODE)
            and changes_var(detonate, director.RADIATING_ANGLE_ID, director.GARU_RING_STEP)
            and sets_var_to(detonate, director.RADIATING_ANGLE_ID, 0)
        ):
            failures.add("garu-detonate-lays-ring")

        # (20) The detonation spawns exactly 4 Brag Sparios (one per adjacent slot) through the shared
        # Spario init, stamping the Spario type.
        if not (
            call_count(detonate, director.INIT_BRAG_SPARIO_PROCCODE) == director.GARU_SPARIO_COUNT
            and writes_const(detonate, director.SLOT_TYPE_ID, director.BRAG_SPARIO_TYPE)
        ):
            failures.add("garu-detonate-spawns-four-sparios")

        # (21) The 4 Sparios launch on the CARDINAL velocities from brag_spario_dX/dY_tbl — the detonation
        # writes the opposing x-axis pair (+/-32) into `slot dx` and the opposing y-axis pair into `slot dy`.
        if not (
            {32, -32} <= write_consts(detonate, director.SLOT_DX_ID)
            and {-32, 32} <= write_consts(detonate, director.SLOT_DY_ID)
        ):
            failures.add("garu-detonate-cardinal-velocities")

        # (22) The detonation FREES the Garu slot (type 0 AND state 0) — it vanishes with no burst and no
        # score (the arcade clr TYPE/STATE).
        if not (
            writes_const(detonate, director.SLOT_TYPE_ID, 0)
            and writes_const(detonate, director.SLOT_STATE_ID, 0)
        ):
            failures.add("garu-detonate-frees-garu")
        return failures

    # Roadmap closure evidence for leaf `air.special-pairs` (AIR-08): the Brag Zakato pair and the Garu
    # Zakato are live families — the Brags teleport in, aim on the generic tier, and end in a 5-bullet
    # radiating fan then vanish awarding nothing; the Garu enters active, flies straight on a 32-63 fuse,
    # and detonates into a 16-bullet ring + 4 Brag Sparios written into the adjacent slots, then frees
    # itself with no score. The live proof (fan emission, ring + 4 cardinal Sparios, Garu freed) is the
    # harness `brag-zakato-fires-five-bullet-fan` / `garu-zakato-detonates-into-ring-and-four-sparios`.
    # roadmap-evidence: AIR-08 success  (test_special_pairs_slice_authoring_present — both families' procs warp, spawn/dispatch driven, Brag teleports+fans vs Garu active+detonates, ring + 4 cardinal Sparios, Garu freed)
    # roadmap-evidence: AIR-08 failure  (test_special_pairs_slice_negative_fixtures — each contract clause corrupted bites)
    def test_special_pairs_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air08_failures(project))

    def test_special_pairs_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air08_failures(base))

        def unwarp(proccode):
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in stage["blocks"].values():
                    if (
                        b["opcode"] == "procedures_prototype"
                        and b.get("mutation", {}).get("proccode") == proccode
                    ):
                        b["mutation"]["warp"] = "false"
            return corrupt

        def noop_call(proccode):
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in stage["blocks"].values():
                    if (
                        b["opcode"] == "procedures_call"
                        and b.get("mutation", {}).get("proccode") == proccode
                    ):
                        b["mutation"]["proccode"] = "noop"
            return corrupt

        def noop_call_in(host_proccode, target_proccode):
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in _proc_body_blocks(stage, host_proccode):
                    if (
                        b["opcode"] == "procedures_call"
                        and b.get("mutation", {}).get("proccode") == target_proccode
                    ):
                        b["mutation"]["proccode"] = "noop"
            return corrupt

        def reitem(host_proccode, list_id, new_value):
            # Change the constant a `data_replaceitemoflist` writes into `list_id` within one proc.
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in _proc_body_blocks(stage, host_proccode):
                    if (
                        b["opcode"] == "data_replaceitemoflist"
                        and b["fields"]["LIST"][1] == list_id
                        and _num_operand(b["inputs"].get("ITEM")) is not None
                    ):
                        b["inputs"]["ITEM"] = [1, [4, new_value]]
            return corrupt

        def repoint_write(host_proccode, list_id):
            # Repoint every `data_replaceitemoflist` on `list_id` to a scratch list → the write is lost.
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in _proc_body_blocks(stage, host_proccode):
                    if (
                        b["opcode"] == "data_replaceitemoflist"
                        and b["fields"]["LIST"][1] == list_id
                    ):
                        b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]
            return corrupt

        def repoint_read(host_proccode, from_ids, to_id, to_name):
            # Repoint the fast/generic-tier aim reads to another table → the tier clause no longer holds.
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in _proc_body_blocks(stage, host_proccode):
                    if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] in from_ids:
                        b["fields"]["LIST"] = [to_name, to_id]
            return corrupt

        def rescale_repeat(host_proccode, times):
            # Change a control_repeat count off its faithful value → the emit count is wrong.
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in _proc_body_blocks(stage, host_proccode):
                    if (
                        b["opcode"] == "control_repeat"
                        and _num_operand(b["inputs"].get("TIMES")) == times
                    ):
                        b["inputs"]["TIMES"] = [1, [4, times + 1]]
            return corrupt

        def break_fuse_offset(host_proccode, offset):
            # Zero the `+ offset` of a random fuse draw → the fuse span/base is wrong.
            def corrupt(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in _proc_body_blocks(stage, host_proccode):
                    if (
                        b["opcode"] == "operator_add"
                        and _num_operand(b["inputs"].get("NUM2")) == offset
                    ):
                        b["inputs"]["NUM2"] = [1, [4, 0]]
            return corrupt

        cases = [
            ("brag-zakato-lifecycle-procs-warp", unwarp(director.UPDATE_BRAG_ZAKATO_PROCCODE)),
            ("garu-zakato-lifecycle-procs-warp", unwarp(director.GARU_ZAKATO_DETONATE_PROCCODE)),
            ("spawn-inits-brag-zakato", noop_call(director.INIT_BRAG_ZAKATO_PROCCODE)),
            ("dispatch-updates-brag-zakato", noop_call(director.UPDATE_BRAG_ZAKATO_PROCCODE)),
            ("stamp-inits-garu-zakato", noop_call(director.INIT_GARU_ZAKATO_PROCCODE)),
            ("dispatch-updates-garu-zakato", noop_call(director.UPDATE_GARU_ZAKATO_PROCCODE)),
            ("brag-zakato-spawns-teleporting", reitem(director.INIT_BRAG_ZAKATO_PROCCODE, director.SLOT_STATE_ID, director.SLOT_ACTIVE)),
            ("garu-zakato-spawns-active", reitem(director.INIT_GARU_ZAKATO_PROCCODE, director.SLOT_STATE_ID, director.SLOT_TELEPORT)),
            ("brag-zakato-stamps-main-code", repoint_write(director.INIT_BRAG_ZAKATO_PROCCODE, director.SLOT_CODE_ID)),
            ("garu-zakato-flies-straight", reitem(director.INIT_GARU_ZAKATO_PROCCODE, director.SLOT_DX_ID, 0)),
            ("brag-zakato-aims-generic-tier", repoint_read(director.UPDATE_BRAG_ZAKATO_PROCCODE, {director.AIM_DX_32_ID, director.AIM_DY_32_ID}, director.AIM_DX_48_ID, "aim dx 48")),
            ("brag-zakato-fires-fan-then-self-explodes", noop_call_in(director.UPDATE_BRAG_ZAKATO_PROCCODE, director.BRAG_ZAKATO_SHOOT_PROCCODE)),
            ("brag-zakato-self-explode-shares-burst", noop_call_in(director.UPDATE_BRAG_ZAKATO_PROCCODE, director.EXPLODE_TICK_PROCCODE)),
            ("brag-zakato-fan-emits-five", rescale_repeat(director.BRAG_ZAKATO_SHOOT_PROCCODE, director.BRAG_ZAKATO_FAN_COUNT)),
            ("brag-zakato-rnd-fuse-seeded", break_fuse_offset(director.UPDATE_BRAG_ZAKATO_PROCCODE, 1)),
            ("garu-zakato-fuse-seeded", break_fuse_offset(director.INIT_GARU_ZAKATO_PROCCODE, director.GARU_ZAKATO_FUSE_OFFSET)),
            ("garu-zakato-detonates-on-fuse", noop_call_in(director.UPDATE_GARU_ZAKATO_PROCCODE, director.GARU_ZAKATO_DETONATE_PROCCODE)),
            ("garu-zakato-hit-shares-burst", noop_call_in(director.UPDATE_GARU_ZAKATO_PROCCODE, director.EXPLODE_TICK_PROCCODE)),
            ("garu-detonate-lays-ring", rescale_repeat(director.GARU_ZAKATO_DETONATE_PROCCODE, director.GARU_RING_COUNT)),
            ("garu-detonate-spawns-four-sparios", noop_call_in(director.GARU_ZAKATO_DETONATE_PROCCODE, director.INIT_BRAG_SPARIO_PROCCODE)),
            ("garu-detonate-cardinal-velocities", repoint_write(director.GARU_ZAKATO_DETONATE_PROCCODE, director.SLOT_DX_ID)),
            ("garu-detonate-frees-garu", reitem(director.GARU_ZAKATO_DETONATE_PROCCODE, director.SLOT_STATE_ID, 1)),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air08_failures(project), label)

    @staticmethod
    def _air05_failures(project: dict) -> set:
        """AIR-05 Kapi authoring contract — violated labels. Pins the structural facts that make Kapi a
        faithful live PEEL-AWAY DIVING family: its init/update run atomically, the spawner and the
        ordered walk drive it by its own type, it aims on the generic (32-magnitude, 2 px/frame) tier,
        it spawns via the NO-exclusion draw (can appear over the craft's column), its update commits a
        dive that latches a side then ACCELERATES the lateral axis (`slot dy`, +/-2) AWAY while
        DECELERATING the scroll axis (`slot dx`, -4) — the F1 axis+direction trap — and it fires EVERY
        dive tick through the shared gate with NO suppression (unlike Terrazi's glide)."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def num_operand(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], list) and len(inp[1]) >= 2 and inp[1][0] in (4, 5, 6, 7, 8, 9, 10):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def const_item(b):
            # Integer constant value of a replace's ITEM (an inline shadow), or None if it is an expression.
            it = b["inputs"].get("ITEM")
            if isinstance(it, list) and len(it) >= 2 and isinstance(it[1], list) and len(it[1]) >= 2 and it[1][0] in (4, 5, 6, 7, 8, 9, 10):
                try:
                    return int(it[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def item_op(b):
            # The operator block driving a replace's ITEM value, or None if the ITEM is a constant.
            it = b["inputs"].get("ITEM")
            return blocks.get(it[1]) if isinstance(it, list) and len(it) >= 2 and isinstance(it[1], str) else None

        def reads_list(op_block, list_id):
            inner = blocks.get(ref(op_block["inputs"].get("NUM1"))) if op_block else None
            return bool(inner and inner["opcode"] == "data_itemoflist" and inner["fields"]["LIST"][1] == list_id)

        # (1) Both Kapi lifecycle procedures exist and are warp (atomic) — a non-warp walk sub-proc
        # would yield mid-slot, letting a half-moved diver render or be hit.
        for proccode in (director.INIT_KAPI_PROCCODE, director.UPDATE_KAPI_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("kapi-lifecycle-procs-warp")

        # (2) The spawner inits Kapi by type; (3) the ordered walk dispatches to its updater.
        if not calls(director.INIT_KAPI_PROCCODE):
            failures.add("spawn-inits-kapi")
        if not calls(director.UPDATE_KAPI_PROCCODE):
            failures.add("dispatch-updates-kapi")

        # (4) The spawn init aims on the GENERIC 32-magnitude tier (2 px/frame) — it reads both the
        # 32-magnitude aim tables, not the Terrazi's 48-magnitude (3 px/frame) tables.
        init_lists = {
            b["fields"]["LIST"][1]
            for b in _proc_body_blocks(stage, director.INIT_KAPI_PROCCODE)
            if b["opcode"] == "data_itemoflist"
        }
        if not {director.AIM_DX_32_ID, director.AIM_DY_32_ID} <= init_lists:
            failures.add("kapi-aims-generic-tier")

        update_body = _proc_body_blocks(stage, director.UPDATE_KAPI_PROCCODE)

        # (5) F1 (direction): the dive ACCELERATES the LATERAL axis — a `slot dy` write of `slot dy` +/-
        # KAPI_DIVE_LATERAL_ACCEL (the peel-away swing kinematics). The operand magnitude (2) is what
        # separates it from the scroll decel (4), so an axis swap (dy carrying the 4) fails this clause.
        accel_lateral = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DY_ID
            and (op := item_op(b)) is not None
            and op["opcode"] in ("operator_add", "operator_subtract")
            and num_operand(op["inputs"].get("NUM2")) == director.KAPI_DIVE_LATERAL_ACCEL
            and reads_list(op, director.SLOT_DY_ID)
            for b in update_body
        )
        if not accel_lateral:
            failures.add("kapi-dive-accel-lateral")

        # (6) F1 (axis): the dive DECELERATES the SCROLL axis — a `slot dx` write of `slot dx` -
        # KAPI_DIVE_SCROLL_DECEL (`subq #2,_dX`). The magnitude (4) again pins the axis: a swap (dx
        # carrying the 2) fails here.
        decel_scroll = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DX_ID
            and (op := item_op(b)) is not None
            and op["opcode"] == "operator_subtract"
            and num_operand(op["inputs"].get("NUM2")) == director.KAPI_DIVE_SCROLL_DECEL
            and reads_list(op, director.SLOT_DX_ID)
            for b in update_body
        )
        if not decel_scroll:
            failures.add("kapi-dive-decel-scroll")

        # (7) The dive latches a side into `slot flag` (like the Toroid swing) — without the latch it
        # would never commit to a dive.
        if not any(
            b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            for b in update_body
        ):
            failures.add("kapi-dive-latches-side")

        # (8) The spawn init captures the family's fire mask AND seeds the per-slot fire field (here the
        # initial approach delay), so a freshly spawned Kapi carries its own dive-fire state.
        init_writes = {
            b["fields"]["LIST"][1]
            for b in _proc_body_blocks(stage, director.INIT_KAPI_PROCCODE)
            if b["opcode"] == "data_replaceitemoflist"
        }
        if not {director.SLOT_FIRE_MASK_ID, director.SLOT_FIRE_TIMER_ID} <= init_writes:
            failures.add("kapi-captures-fire-state")

        # (9) The Kapi update drives the shared gate, so a diving Kapi actually fires under it.
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
            for b in update_body
        ):
            failures.add("kapi-update-drives-gate")

        # (10) NO fire suppression: unlike Terrazi's glide (which pins `slot fire timer` to 255), Kapi
        # never suppresses — the update must not write the byte-max suppress constant to the fire timer.
        if any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID
            and const_item(b) == director.TERRAZI_FIRE_SUPPRESS
            for b in update_body
        ):
            failures.add("kapi-no-fire-suppression")

        # (11) ONCE-ONLY latch (the re-home guard). Each write that latches a DIVE side into
        # `slot flag` must sit INSIDE an enclosing `if slot flag == APPROACH`, so the instant it
        # flips the flag off APPROACH the whole latch is unreachable — the "latched once, never
        # recomputed" guarantee of kapi_10_fire (3626-3633). If the latch could run outside that
        # gate it would recompute the peel side every tick and re-home once the lateral velocity
        # crossed zero: the exact latched-swing direction bug this project has hit before. Clause
        # (7) only pins that a latch write EXISTS; this pins that it stays gated. Purely structural
        # — the settling harness advances whole ticks and cannot observe a single re-latched frame.
        id_of = {id(b): bid for bid, b in blocks.items()}

        def approach_gated(write_id: str) -> bool:
            cur = blocks.get(write_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    cond = blocks.get(ref(parent["inputs"].get("CONDITION")))
                    lhs = blocks.get(ref(cond["inputs"].get("OPERAND1"))) if cond else None
                    if (
                        cond is not None
                        and cond["opcode"] == "operator_equals"
                        and lhs is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                        and num_operand(cond["inputs"].get("OPERAND2")) == director.KAPI_FLAG_APPROACH
                    ):
                        return True
                cur = parent
            return False

        dive_latch_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            and const_item(b) in (director.KAPI_FLAG_DIVE_MINUS, director.KAPI_FLAG_DIVE_PLUS)
        ]
        if not dive_latch_ids or not all(approach_gated(wid) for wid in dive_latch_ids):
            failures.add("kapi-dive-latch-approach-gated")
        return failures

    # Roadmap closure evidence for leaf `air.kapi` (AIR-05.kapi): Kapi is a live family — spawned by
    # type from the formation wave (via the no-exclusion draw), advanced by the ordered walk, aimed on
    # the 2 px/frame generic tier, committing a peel-away dive that ACCELERATES its lateral course away
    # from the craft while DECELERATING its scroll course, and firing every dive tick under the shared
    # gate with no suppression. The live proof (spawns, dives away, fires while diving) is the harness
    # `kapi-wave-spawns-and-dives` / `kapi-fires-while-diving`.
    # roadmap-evidence: AIR-05 success  (test_kapi_slice_authoring_present — lifecycle procs warp, spawn+dispatch by type, generic-tier aim, dive accelerates lateral + decelerates scroll, fires without suppression)
    # roadmap-evidence: AIR-05 failure  (test_kapi_slice_negative_fixtures — each contract clause corrupted bites)
    def test_kapi_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air05_failures(project))

    def test_kapi_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air05_failures(base))

        def unwarp_update(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_KAPI_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_init_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.INIT_KAPI_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_dispatch_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_KAPI_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def aim_wrong_tier(p: dict) -> None:
            # Repoint the generic-tier aim reads to the Terrazi's 48-magnitude tables → the generic-tier
            # clause no longer holds.
            stage = next(t for t in p["targets"] if t["isStage"])
            swap = {director.AIM_DX_32_ID: ("aim dx 48", director.AIM_DX_48_ID), director.AIM_DY_32_ID: ("aim dy 48", director.AIM_DY_48_ID)}
            for b in _proc_body_blocks(stage, director.INIT_KAPI_PROCCODE):
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] in swap:
                    b["fields"]["LIST"] = list(swap[b["fields"]["LIST"][1]])

        def drop_lateral_accel(p: dict) -> None:
            # Repoint the dive's `slot dy` accel writes to a scratch list → the lateral peel-away never
            # happens (the F1 direction clause bites).
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_KAPI_PROCCODE):
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_DY_ID:
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def drop_scroll_decel(p: dict) -> None:
            # Repoint the dive's `slot dx` decel writes to a scratch list → the forward course never
            # decelerates (the F1 axis clause bites).
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_KAPI_PROCCODE):
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_DX_ID:
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def drop_side_latch(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_KAPI_PROCCODE):
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID:
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def drop_fire_state_capture(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.INIT_KAPI_PROCCODE):
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_FIRE_MASK_ID:
                    b["fields"]["LIST"] = ["value table", director.VALUE_TABLE_ID]

        def drop_gate_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_KAPI_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def add_fire_suppression(p: dict) -> None:
            # Turn the dive's arm-to-1 fire-timer write into a Terrazi-style suppress-to-255 → the
            # no-suppression clause bites (a Kapi that stopped firing mid-dive).
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_KAPI_PROCCODE):
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID:
                    it = b["inputs"].get("ITEM")
                    if isinstance(it, list) and len(it) >= 2 and isinstance(it[1], list) and it[1][0] in (4, 5, 6, 7, 8, 9, 10):
                        b["inputs"]["ITEM"] = [1, [4, str(director.TERRAZI_FIRE_SUPPRESS)]]

        def ungate_latch(p: dict) -> None:
            # Break the approach gate enclosing the side latch (retarget its `flag == APPROACH`
            # test to a value the flag never holds) → the latch would recompute the peel side every
            # tick (a re-homing dive). The once-only structural guard bites.
            stage = next(t for t in p["targets"] if t["isStage"])
            b = stage["blocks"]
            id_map = {id(v): k for k, v in b.items()}

            def cref(inp):
                return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

            for x in _proc_body_blocks(stage, director.UPDATE_KAPI_PROCCODE):
                it = x["inputs"].get("ITEM") if x["opcode"] == "data_replaceitemoflist" else None
                if not (
                    x["opcode"] == "data_replaceitemoflist"
                    and x["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                    and isinstance(it, list)
                    and isinstance(it[1], list)
                    and int(it[1][1]) in (director.KAPI_FLAG_DIVE_MINUS, director.KAPI_FLAG_DIVE_PLUS)
                ):
                    continue
                cur = b.get(id_map[id(x)])
                while cur is not None:
                    parent = b.get(cur.get("parent")) if cur.get("parent") else None
                    if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                        cond = b.get(cref(parent["inputs"].get("CONDITION")))
                        lhs = b.get(cref(cond["inputs"].get("OPERAND1"))) if cond else None
                        if (
                            cond is not None
                            and cond["opcode"] == "operator_equals"
                            and lhs is not None
                            and lhs["opcode"] == "data_itemoflist"
                            and lhs["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                        ):
                            cond["inputs"]["OPERAND2"] = [1, [4, 77]]
                    cur = parent

        cases = [
            ("kapi-lifecycle-procs-warp", unwarp_update),
            ("spawn-inits-kapi", drop_init_call),
            ("dispatch-updates-kapi", drop_dispatch_call),
            ("kapi-aims-generic-tier", aim_wrong_tier),
            ("kapi-dive-accel-lateral", drop_lateral_accel),
            ("kapi-dive-decel-scroll", drop_scroll_decel),
            ("kapi-dive-latches-side", drop_side_latch),
            ("kapi-dive-latch-approach-gated", ungate_latch),
            ("kapi-captures-fire-state", drop_fire_state_capture),
            ("kapi-update-drives-gate", drop_gate_call),
            ("kapi-no-fire-suppression", add_fire_suppression),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air05_failures(project), label)

    @staticmethod
    def _air02_failures(project: dict) -> set:
        """AIR-02 Torkan authoring contract — violated labels. Pins the structural facts that make Torkan
        a faithful live ATTACK-AND-RETREAT family: its init/update run atomically, the spawner and the
        ordered walk drive it by its own type, it aims TOWARD the craft on the generic (32-magnitude,
        2 px/frame) tier and awards 50 points, its update fires EXACTLY ONE bullet directly via the
        allocator (never the shared fire gate, and gated so it cannot repeat), holds position during the
        hover, then re-aims ONCE 180 degrees AWAY (a half-turn on the folded angle) onto the FAST
        (48-magnitude, 3 px/frame) tier and flees — with every phase transition nested inside its own
        phase gate so the once-only re-aim and single shot cannot recur (the settling harness advances
        whole ticks and cannot observe a single re-fired or re-aimed frame, so it is pinned structurally)."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def num_operand(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], list) and len(inp[1]) >= 2 and inp[1][0] in (4, 5, 6, 7, 8, 9, 10):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def const_item(b):
            it = b["inputs"].get("ITEM")
            if isinstance(it, list) and len(it) >= 2 and isinstance(it[1], list) and len(it[1]) >= 2 and it[1][0] in (4, 5, 6, 7, 8, 9, 10):
                try:
                    return int(it[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        # (1) Both Torkan lifecycle procedures exist and are warp (atomic) — a non-warp walk sub-proc
        # would yield mid-slot, letting a half-moved enemy render or be hit.
        for proccode in (director.INIT_TORKAN_PROCCODE, director.UPDATE_TORKAN_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("torkan-lifecycle-procs-warp")

        # (2) The spawner inits Torkan by type; (3) the ordered walk dispatches to its updater.
        if not calls(director.INIT_TORKAN_PROCCODE):
            failures.add("spawn-inits-torkan")
        if not calls(director.UPDATE_TORKAN_PROCCODE):
            failures.add("dispatch-updates-torkan")

        # (4) The spawn init aims TOWARD the craft on the GENERIC 32-magnitude tier (2 px/frame) — it
        # reads both 32-magnitude aim tables, not the fast 48-magnitude (3 px/frame) tables.
        init_lists = {
            b["fields"]["LIST"][1]
            for b in _proc_body_blocks(stage, director.INIT_TORKAN_PROCCODE)
            if b["opcode"] == "data_itemoflist"
        }
        if not {director.AIM_DX_32_ID, director.AIM_DY_32_ID} <= init_lists:
            failures.add("torkan-aims-generic-tier")

        # (5) The spawn init awards 50 points — it writes `slot pts` = TORKAN_PTS (the 1-based value-table
        # index of 50). A wrong index would score the wrong value on the kill.
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and const_item(b) == director.TORKAN_PTS
            for b in _proc_body_blocks(stage, director.INIT_TORKAN_PROCCODE)
        ):
            failures.add("torkan-awards-50-pts")

        update_body = _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE)
        id_of = {id(b): bid for bid, b in blocks.items()}

        def flag_gated(node_id: str, flag_value: int) -> bool:
            # True when some ancestor of node_id is an `if <slot flag == flag_value>` — the per-phase gate.
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    cond = blocks.get(ref(parent["inputs"].get("CONDITION")))
                    lhs = blocks.get(ref(cond["inputs"].get("OPERAND1"))) if cond else None
                    if (
                        cond is not None
                        and cond["opcode"] == "operator_equals"
                        and lhs is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                        and num_operand(cond["inputs"].get("OPERAND2")) == flag_value
                    ):
                        return True
                cur = parent
            return False

        # The true enclosing `if` of each block, mapped by walking DOWN each if's SUBSTACK/SUBSTACK2
        # `next`-chain. (This builder's `parent` pointers chain forward through siblings, so a parent-walk
        # would land on a sibling `if`, not the gate a block actually sits inside.) Two blocks with the
        # same enclosing-if are direct children of the SAME substack.
        enclosing_if = {}
        for _bid, _b in blocks.items():
            if _b["opcode"] in ("control_if", "control_if_else"):
                for _key in ("SUBSTACK", "SUBSTACK2"):
                    _cur = ref(_b["inputs"].get(_key)) if _key in _b["inputs"] else None
                    while _cur:
                        enclosing_if[_cur] = _bid
                        _cur = blocks[_cur].get("next")

        def immediate_if(node_id: str):
            return enclosing_if.get(node_id)

        # (6) FIRES EXACTLY ONCE. The Torkan fires one bullet DIRECTLY via the allocator (never the shared
        # fire gate — it takes no mask). In the update body there must be exactly ONE allocator call, and
        # it must sit inside the `if flag == APPROACH` gate whose same transition flips the flag off
        # APPROACH — so the instant it fires the approach branch is unreachable and the shot cannot repeat.
        alloc_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
        ]
        if len(alloc_ids) != 1 or not flag_gated(alloc_ids[0], director.TORKAN_FLAG_APPROACH):
            failures.add("torkan-fires-once")

        # (6b) It fires directly, NOT through the shared fire-permission gate (the faithful simplification:
        # a single un-masked shot, torkan_shoot 3379). A gate call would be a periodic-fire regression.
        if any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
            for b in update_body
        ):
            failures.add("torkan-fires-without-gate")

        # (6c) The single shot fires in the SAME expiry gate that flips the flag to HOVER — the allocator
        # call and the `slot flag = HOVER` write share their immediate enclosing `if` (the `slot fire
        # timer <= 0` gate). Clause (6) only asks that the fire sit SOMEWHERE under `if flag == APPROACH`;
        # a fire lifted out of the expiry gate to a bare child of the approach branch is still APPROACH-
        # gated (clause 6 passes) yet would fire on EVERY approach tick. Requiring the fire and the flag-
        # flip to share the expiry gate is what actually pins "exactly once" — the instant it fires it
        # flips to HOVER in the same breath, so the approach branch cannot re-fire it.
        hover_flip_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            and const_item(b) == director.TORKAN_FLAG_HOVER
        ]
        if not (
            len(alloc_ids) == 1
            and len(hover_flip_ids) == 1
            and immediate_if(alloc_ids[0]) is not None
            and immediate_if(alloc_ids[0]) == immediate_if(hover_flip_ids[0])
        ):
            failures.add("torkan-fire-in-expiry-gate")

        # (7) The retreat reads the FAST 48-magnitude tier (3 px/frame) — both 48-magnitude aim tables,
        # not the generic 32-magnitude approach tables. Wrong tier = wrong retreat speed (the fidelity trap).
        update_lists = {b["fields"]["LIST"][1] for b in update_body if b["opcode"] == "data_itemoflist"}
        if not {director.AIM_DX_48_ID, director.AIM_DY_48_ID} <= update_lists:
            failures.add("torkan-reaims-fast-tier")

        # (8) The retreat is AWAY, not homing: the `aim index` is recomputed with a HALF-TURN
        # (TORKAN_REAIM_HALF_TURN) added to the folded angle before indexing the tier — the port's
        # faithful `add.b #0x80` 180-degree flip (torkan_update_dir 3403). Without the half-turn the
        # re-aim would point back TOWARD the craft (a homing retreat — the visible failure).
        if not any(
            b["opcode"] == "operator_add"
            and num_operand(b["inputs"].get("NUM2")) == director.TORKAN_REAIM_HALF_TURN
            for b in update_body
        ):
            failures.add("torkan-reaim-half-turn")

        # (9) HOVER HOLDS: the hover branch zeroes both velocity axes (`slot dx` = 0 and `slot dy` = 0),
        # each nested in the `if flag == HOVER` gate — the no-enemy-scroll mapping of the arcade's
        # scroll-carried hover (record 029). Without it the fired Torkan would keep flying, never hovering.
        holds_dx = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DX_ID
            and const_item(b) == 0
            and flag_gated(id_of[id(b)], director.TORKAN_FLAG_HOVER)
            for b in update_body
        )
        holds_dy = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DY_ID
            and const_item(b) == 0
            and flag_gated(id_of[id(b)], director.TORKAN_FLAG_HOVER)
            for b in update_body
        )
        if not (holds_dx and holds_dy):
            failures.add("torkan-hover-holds")

        # (10) ONCE-ONLY phase transitions (the re-fire / re-aim guard). Every write that flips the flag
        # to HOVER must sit inside the `if flag == APPROACH` gate, and every write that flips it to FLEE
        # inside the `if flag == HOVER` gate — so each transition is reachable only from the phase it
        # leaves, and the single shot and the one-time re-aim cannot recur on a later tick. Purely
        # structural: the settling harness advances whole ticks and cannot see a single re-fired frame.
        hover_writes = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            and const_item(b) == director.TORKAN_FLAG_HOVER
        ]
        flee_writes = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            and const_item(b) == director.TORKAN_FLAG_FLEE
        ]
        transitions_gated = (
            hover_writes
            and flee_writes
            and all(flag_gated(w, director.TORKAN_FLAG_APPROACH) for w in hover_writes)
            and all(flag_gated(w, director.TORKAN_FLAG_HOVER) for w in flee_writes)
        )
        if not transitions_gated:
            failures.add("torkan-phase-transitions-gated")
        return failures

    # Roadmap closure evidence for leaf `air.torkan` (AIR-02.torkan): Torkan is a live family — spawned by
    # type from the formation wave (via the no-exclusion draw), advanced by the ordered walk, aimed on the
    # 2 px/frame generic tier, firing ONE direct un-masked shot at its shot-delay expiry, holding position
    # through the hover, then re-aiming ONCE 180 degrees away onto the 3 px/frame fast tier and fleeing.
    # The live proof (spawns, fires exactly once, then reverses AWAY at speed) is the harness
    # `torkan-approaches-and-fires` / `torkan-reaims-and-flees`.
    # roadmap-evidence: AIR-02 success  (test_torkan_slice_authoring_present — lifecycle procs warp, spawn+dispatch by type, generic-tier aim, 50 pts, fires once directly without the gate, holds the hover, re-aims away on the fast tier, phase transitions gated)
    # roadmap-evidence: AIR-02 failure  (test_torkan_slice_negative_fixtures — each contract clause corrupted bites)
    def test_torkan_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air02_failures(project))

    def test_torkan_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air02_failures(base))

        def unwarp_update(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_TORKAN_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_init_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.INIT_TORKAN_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_dispatch_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_TORKAN_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def aim_wrong_tier(p: dict) -> None:
            # Repoint the init's generic-tier aim reads to the fast 48-magnitude tables → the
            # generic-tier clause no longer holds.
            stage = next(t for t in p["targets"] if t["isStage"])
            swap = {director.AIM_DX_32_ID: ("aim dx 48", director.AIM_DX_48_ID), director.AIM_DY_32_ID: ("aim dy 48", director.AIM_DY_48_ID)}
            for b in _proc_body_blocks(stage, director.INIT_TORKAN_PROCCODE):
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] in swap:
                    b["fields"]["LIST"] = list(swap[b["fields"]["LIST"][1]])

        def wrong_points(p: dict) -> None:
            # Change the awarded value-table index off TORKAN_PTS → the 50-point award clause bites.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.INIT_TORKAN_PROCCODE):
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_PTS_ID:
                    b["inputs"]["ITEM"] = [1, [4, str(director.TORKAN_PTS + 1)]]

        def drop_fire(p: dict) -> None:
            # Silence the single direct shot (retarget the allocator call) → the fires-once clause bites
            # (zero allocator calls, so the count is no longer exactly one).
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def add_fire_gate(p: dict) -> None:
            # Turn the direct allocator call into a shared fire-gate call → the fires-WITHOUT-gate clause
            # bites (a periodic-masked-fire regression). Also trips fires-once (the allocator vanishes).
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.FIRE_GATE_PROCCODE

        def lift_fire_from_expiry(p: dict) -> None:
            # Splice the single shot OUT of the shot-expiry gate and up to the front of the enclosing
            # APPROACH branch — the exact "fires on every approach tick" regression. It stays gated in
            # APPROACH (so the fires-once clause alone still passes) but no longer shares the expiry gate
            # that flips the flag to HOVER → the fire-in-expiry-gate clause bites.
            stage = next(t for t in p["targets"] if t["isStage"])
            b = stage["blocks"]
            id_map = {id(v): k for k, v in b.items()}

            def cref(inp):
                return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

            # Map each block to its enclosing `if` by the same downward SUBSTACK/next walk the check uses.
            encl = {}
            for bid, blk in b.items():
                if blk["opcode"] in ("control_if", "control_if_else"):
                    for key in ("SUBSTACK", "SUBSTACK2"):
                        cur = cref(blk["inputs"].get(key)) if key in blk["inputs"] else None
                        while cur:
                            encl[cur] = bid
                            cur = b[cur].get("next")

            alloc_id = next(
                id_map[id(x)]
                for x in _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE)
                if x["opcode"] == "procedures_call"
                and x.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
            )
            expiry_if = encl[alloc_id]          # the `slot fire timer <= 0` gate
            approach_if = encl[expiry_if]        # the enclosing `if flag == APPROACH` branch
            after_alloc = b[alloc_id].get("next")  # capture before we relink

            # Remove alloc from the expiry gate's substack (point the gate at alloc's successor).
            b[expiry_if]["inputs"]["SUBSTACK"] = [2, after_alloc]
            if after_alloc:
                b[after_alloc]["parent"] = expiry_if
            # Prepend alloc to the APPROACH branch's substack.
            approach_first = cref(b[approach_if]["inputs"].get("SUBSTACK"))
            b[alloc_id]["next"] = approach_first
            b[alloc_id]["parent"] = approach_if
            if approach_first:
                b[approach_first]["parent"] = alloc_id
            b[approach_if]["inputs"]["SUBSTACK"] = [2, alloc_id]

        def reaim_wrong_tier(p: dict) -> None:
            # Repoint the retreat's fast-tier reads to the generic 32-magnitude tables → the fast-tier
            # clause bites (the retreat would flee at the slow approach speed).
            stage = next(t for t in p["targets"] if t["isStage"])
            swap = {director.AIM_DX_48_ID: ("aim dx 32", director.AIM_DX_32_ID), director.AIM_DY_48_ID: ("aim dy 32", director.AIM_DY_32_ID)}
            for b in _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE):
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] in swap:
                    b["fields"]["LIST"] = list(swap[b["fields"]["LIST"][1]])

        def drop_half_turn(p: dict) -> None:
            # Zero the 180-degree half-turn added to the folded angle → the re-aim points back TOWARD the
            # craft (a homing retreat). The away-flip clause bites.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE):
                if b["opcode"] == "operator_add":
                    n2 = b["inputs"].get("NUM2")
                    if isinstance(n2, list) and len(n2) >= 2 and isinstance(n2[1], list) and n2[1][0] in (4, 5, 6, 7, 8, 9, 10):
                        try:
                            if int(n2[1][1]) == director.TORKAN_REAIM_HALF_TURN:
                                b["inputs"]["NUM2"] = [1, [4, "0"]]
                        except (ValueError, TypeError):
                            pass

        def unhold_hover(p: dict) -> None:
            # Make the hover's velocity-zeroing writes non-zero → the Torkan keeps moving instead of
            # holding. The hover-holds clause bites.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] in (director.SLOT_DX_ID, director.SLOT_DY_ID)
                ):
                    it = b["inputs"].get("ITEM")
                    if isinstance(it, list) and len(it) >= 2 and isinstance(it[1], list) and it[1][0] in (4, 5, 6, 7, 8, 9, 10) and int(it[1][1]) == 0:
                        b["inputs"]["ITEM"] = [1, [4, "3"]]

        def ungate_hover_transition(p: dict) -> None:
            # Break the `flag == APPROACH` gate enclosing the HOVER transition (retarget its flag test to a
            # value the flag never holds) → the fire/hover transition could run outside the approach phase
            # and recur. The once-only structural guard bites.
            stage = next(t for t in p["targets"] if t["isStage"])
            b = stage["blocks"]
            id_map = {id(v): k for k, v in b.items()}

            def cref(inp):
                return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

            for x in _proc_body_blocks(stage, director.UPDATE_TORKAN_PROCCODE):
                it = x["inputs"].get("ITEM") if x["opcode"] == "data_replaceitemoflist" else None
                if not (
                    x["opcode"] == "data_replaceitemoflist"
                    and x["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                    and isinstance(it, list)
                    and isinstance(it[1], list)
                    and int(it[1][1]) == director.TORKAN_FLAG_HOVER
                ):
                    continue
                cur = b.get(id_map[id(x)])
                while cur is not None:
                    parent = b.get(cur.get("parent")) if cur.get("parent") else None
                    if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                        cond = b.get(cref(parent["inputs"].get("CONDITION")))
                        lhs = b.get(cref(cond["inputs"].get("OPERAND1"))) if cond else None
                        if (
                            cond is not None
                            and cond["opcode"] == "operator_equals"
                            and lhs is not None
                            and lhs["opcode"] == "data_itemoflist"
                            and lhs["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                            and num_operand(cond["inputs"].get("OPERAND2")) == director.TORKAN_FLAG_APPROACH
                        ):
                            cond["inputs"]["OPERAND2"] = [1, [4, 77]]
                    cur = parent

        def num_operand(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], list) and len(inp[1]) >= 2 and inp[1][0] in (4, 5, 6, 7, 8, 9, 10):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        cases = [
            ("torkan-lifecycle-procs-warp", unwarp_update),
            ("spawn-inits-torkan", drop_init_call),
            ("dispatch-updates-torkan", drop_dispatch_call),
            ("torkan-aims-generic-tier", aim_wrong_tier),
            ("torkan-awards-50-pts", wrong_points),
            ("torkan-fires-once", drop_fire),
            ("torkan-fires-without-gate", add_fire_gate),
            ("torkan-fire-in-expiry-gate", lift_fire_from_expiry),
            ("torkan-reaims-fast-tier", reaim_wrong_tier),
            ("torkan-reaim-half-turn", drop_half_turn),
            ("torkan-hover-holds", unhold_hover),
            ("torkan-phase-transitions-gated", ungate_hover_transition),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air02_failures(project), label)

    @staticmethod
    def _air03_failures(project: dict) -> set:
        """AIR-03 Zoshi authoring contract — violated labels. Pins the structural facts that make Zoshi a
        faithful THREE-TYPES-OVER-ONE-CORE aerial family: three spawn inits (top/bottom/rnd) and one shared
        movement/anim/fire update all run atomically; the spawner inits all three by type and the ordered
        walk drives the one shared updater; every variant aims its INITIAL drift at the craft on the
        24-magnitude toroid tier (1.5 px/frame); top/bottom award 100 points and rnd 70; the bottom variant
        enters at the fixed bottom edge row while top/rnd enter from the top; each captures the Zoshi fire
        mask at spawn. The shared update fires EXACTLY ONE bullet via the allocator (the fire gate replicated
        INLINE, never a FIRE_GATE call), the bullet aimed on the 32-magnitude tier (the shared aimed shot,
        identical for all three), under a masked-periodic phase (fired only when the per-slot countdown hits
        zero on the 4-tick phase boundary, then reloaded under the mask). On each fire it RE-HEADINGS its OWN
        drift — the ONLY per-type branch, keyed on `slot type == ZOSHI_RND_TYPE`: the rnd branch draws a
        RANDOM 24-tier angle from an rng step (index = floor(rng/8)+1), while top/bottom re-aim their drift
        TOWARD the craft via the aim compute. The "random" is the ENEMY'S MOVEMENT, never the shot (all three
        fire the same aimed bullet). The 24-vs-32 tier split is the clean discriminator between enemy drift
        (24) and bullet aim (32); the rnd-vs-aimed branch split is pinned by DOWNWARD substack descent from
        the type fork (this builder's `parent` pointers chain forward through siblings, so a parent-walk would
        mistake a sibling `if` for the branch a block sits inside)."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def rref(inp):
            r = ref(inp)
            return blocks.get(r) if r else None

        def num_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
            ):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def var_operand(inp):
            # The variable id of an inline variable value-spec [3, [12, name, id], ...], or None.
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 3
                and inp[1][0] == 12
            ):
                return inp[1][2]
            return None

        def const_item(b):
            it = b["inputs"].get("ITEM")
            return num_operand(it)

        def descend(start_id):
            # Every block reachable from start_id along `next` and every reporter/substack input — one
            # branch's own subtree, so the rnd (SUBSTACK) and aimed (SUBSTACK2) re-heading branches are
            # inspected apart. Downward, never by parent-walk (parents chain forward to prior siblings).
            seen: set = set()
            frontier = [start_id]
            while frontier:
                bid = frontier.pop()
                if not bid or bid in seen or bid not in blocks:
                    continue
                seen.add(bid)
                blk = blocks[bid]
                frontier.append(blk.get("next"))
                for value in blk.get("inputs", {}).values():
                    if isinstance(value, list) and len(value) >= 2 and isinstance(value[1], str):
                        frontier.append(value[1])
            return [blocks[bid] for bid in seen]

        init_procs = (
            director.INIT_ZOSHI_TOP_PROCCODE,
            director.INIT_ZOSHI_BOTTOM_PROCCODE,
            director.INIT_ZOSHI_RND_PROCCODE,
        )

        # (1) All four lifecycle procedures exist and are warp (atomic) — a non-warp lifecycle proc would
        # yield mid-slot, letting a half-moved/half-aimed Zoshi render or be hit.
        for proccode in (*init_procs, director.UPDATE_ZOSHI_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("zoshi-lifecycle-procs-warp")

        # (2) The spawner inits all THREE types; (3) the ordered walk dispatches to the ONE shared updater.
        if not all(calls(pc) for pc in init_procs):
            failures.add("spawn-inits-zoshi")
        if not calls(director.UPDATE_ZOSHI_PROCCODE):
            failures.add("dispatch-updates-zoshi")

        # (4) EVERY spawn init aims the INITIAL drift TOWARD the craft on the 24-magnitude toroid tier
        # (1.5 px/frame) — it reads both 24-magnitude aim tables (all three variants aim their entry
        # heading; the 0C erratic veer only emerges later, at each fire).
        for pc in init_procs:
            init_lists = {
                b["fields"]["LIST"][1]
                for b in _proc_body_blocks(stage, pc)
                if b["opcode"] == "data_itemoflist"
            }
            if not {director.AIM_DX_24_ID, director.AIM_DY_24_ID} <= init_lists:
                failures.add("zoshi-inits-aim-24-tier")

        def awards(pc, pts):
            return any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                and const_item(b) == pts
                for b in _proc_body_blocks(stage, pc)
            )

        # (5) The point award per variant: top/bottom write `slot pts` = the 100-point index, rnd the
        # 70-point index (1-based value-table indices; a wrong index scores the wrong value on the kill).
        if not awards(director.INIT_ZOSHI_TOP_PROCCODE, director.ZOSHI_PTS_AIMED):
            failures.add("zoshi-top-awards-100")
        if not awards(director.INIT_ZOSHI_BOTTOM_PROCCODE, director.ZOSHI_PTS_AIMED):
            failures.add("zoshi-bottom-awards-100")
        if not awards(director.INIT_ZOSHI_RND_PROCCODE, director.ZOSHI_PTS_RND):
            failures.add("zoshi-rnd-awards-70")

        # (6) The BOTTOM variant enters at the fixed bottom edge row (`slot x` = ZOSHI_BOTTOM_EDGE_X cells,
        # in slot units) — the arcade's `_X = #40` bottom entry, distinct from top/rnd's top-row entry.
        bottom_entry = director.ZOSHI_BOTTOM_EDGE_X * director.SLOT_UNITS_PER_CELL
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_X_ID
            and const_item(b) == bottom_entry
            for b in _proc_body_blocks(stage, director.INIT_ZOSHI_BOTTOM_PROCCODE)
        ):
            failures.add("zoshi-bottom-fixed-edge-entry")

        # (7) EVERY variant captures the Zoshi fire mask at spawn — it writes `slot fire mask` from the
        # `fire mask zoshi` var, so the shared fire cadence reads the right mask.
        for pc in init_procs:
            if not any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_FIRE_MASK_ID
                and var_operand(b["inputs"].get("ITEM")) == director.FIRE_MASK_ZOSHI_ID
                for b in _proc_body_blocks(stage, pc)
            ):
                failures.add("zoshi-captures-fire-mask")

        update_body = _proc_body_blocks(stage, director.UPDATE_ZOSHI_PROCCODE)
        id_of = {id(b): bid for bid, b in blocks.items()}

        # (8) FIRES THE SHARED AIMED BULLET. Exactly ONE allocator call in the update body, and the bullet
        # is aimed on the 32-magnitude tier (both 32-magnitude tables read) — the shared `_fire_aimed_bullet`
        # every family fires, distinct from the enemy's own 24-tier drift.
        alloc_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
        ]
        update_lists = {
            b["fields"]["LIST"][1] for b in update_body if b["opcode"] == "data_itemoflist"
        }
        if len(alloc_ids) != 1 or not (
            {director.AIM_DX_32_ID, director.AIM_DY_32_ID} <= update_lists
        ):
            failures.add("zoshi-fires-shared-aimed-bullet")

        # Map each block to its true enclosing `if` by walking DOWN each if's SUBSTACK/SUBSTACK2 next-chain
        # (parent pointers chain forward through siblings, so a parent-walk would land on a sibling `if`).
        enclosing_if = {}
        for _bid, _b in blocks.items():
            if _b["opcode"] in ("control_if", "control_if_else"):
                for _key in ("SUBSTACK", "SUBSTACK2"):
                    _cur = ref(_b["inputs"].get(_key)) if _key in _b["inputs"] else None
                    while _cur:
                        enclosing_if[_cur] = _bid
                        _cur = blocks[_cur].get("next")

        def cond_of(if_id):
            b = blocks.get(if_id)
            return rref(b["inputs"].get("CONDITION")) if b else None

        def is_eq_listitem_const(cond, list_id, value):
            # cond is `<data_itemoflist LIST=list_id> == value`.
            if cond is None or cond["opcode"] != "operator_equals":
                return False
            lhs = rref(cond["inputs"].get("OPERAND1"))
            return (
                lhs is not None
                and lhs["opcode"] == "data_itemoflist"
                and lhs["fields"]["LIST"][1] == list_id
                and num_operand(cond["inputs"].get("OPERAND2")) == value
            )

        # (9) MASKED-PERIODIC FIRE. The one allocator call sits inside `if slot fire timer == 0`, and that
        # gate sits inside `if tick mod FIRE_GATE_PHASE_TICKS == 0` — the fire fires only when the per-slot
        # countdown expires on the phase boundary. Plus a reload write: `slot fire timer` set to
        # (rng mod (mask+1)) + 1. Without the phase/zero nesting the Zoshi would fire every tick; without the
        # reload it would fire once and never again.
        masked_phase = False
        if len(alloc_ids) == 1:
            zero_gate = enclosing_if.get(alloc_ids[0])
            phase_gate = enclosing_if.get(zero_gate) if zero_gate else None
            reload_present = any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID
                and (item := rref(b["inputs"].get("ITEM"))) is not None
                and item["opcode"] == "operator_add"
                and num_operand(item["inputs"].get("NUM2")) == 1
                and (m := rref(item["inputs"].get("NUM1"))) is not None
                and m["opcode"] == "operator_mod"
                for b in update_body
            )
            phase_cond = cond_of(phase_gate)
            phase_ok = (
                phase_cond is not None
                and phase_cond["opcode"] == "operator_equals"
                and (mod := rref(phase_cond["inputs"].get("OPERAND1"))) is not None
                and mod["opcode"] == "operator_mod"
                and var_operand(mod["inputs"].get("NUM1")) == director.TICK_ID
                and num_operand(mod["inputs"].get("NUM2")) == director.FIRE_GATE_PHASE_TICKS
                and num_operand(phase_cond["inputs"].get("OPERAND2")) == 0
            )
            masked_phase = (
                is_eq_listitem_const(cond_of(zero_gate), director.SLOT_FIRE_TIMER_ID, 0)
                and phase_ok
                and reload_present
            )
        if not masked_phase:
            failures.add("zoshi-fire-masked-phase")

        # The single per-type branch: the re-heading `if slot type == ZOSHI_RND_TYPE`. SUBSTACK is the rnd
        # branch, SUBSTACK2 the aimed branch — distinguished by downward descent, never parent-walk.
        fork = next(
            (
                b
                for b in update_body
                if b["opcode"] == "control_if_else"
                and is_eq_listitem_const(
                    cond_of(id_of[id(b)]) or rref(b["inputs"].get("CONDITION")),
                    director.SLOT_TYPE_ID,
                    director.ZOSHI_RND_TYPE,
                )
            ),
            None,
        )
        rnd_branch = descend(ref(fork["inputs"].get("SUBSTACK"))) if fork else []
        aimed_branch = descend(ref(fork["inputs"].get("SUBSTACK2"))) if fork else []

        def branch_calls(branch, proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in branch
            )

        def branch_reads_24(branch):
            lists = {b["fields"]["LIST"][1] for b in branch if b["opcode"] == "data_itemoflist"}
            return {director.AIM_DX_24_ID, director.AIM_DY_24_ID} <= lists

        # (10) THE RND ENEMY DRIFT IS RANDOM (the erratic flyer). In the rnd branch the drift angle comes
        # from an rng step, indexed floor(rng/8)+1 — a floor(divide(rng out, 8)) reading the stream — and
        # writes the drift from the 24-tier; it does NOT call the aim compute. This is the biting
        # aimed-vs-random pair on the ENEMY MOVEMENT (never the shot): if 0C re-aimed toward the craft it
        # would stop veering.
        rng_indexed = any(
            b["opcode"] == "operator_mathop"
            and b["fields"].get("OPERATOR", [None])[0] == "floor"
            and (div := rref(b["inputs"].get("NUM"))) is not None
            and div["opcode"] == "operator_divide"
            and var_operand(div["inputs"].get("NUM1")) == director.RNG_OUT_ID
            and num_operand(div["inputs"].get("NUM2")) == 8
            for b in rnd_branch
        )
        if not (
            fork is not None
            and branch_calls(rnd_branch, director.RNG_PROCCODE)
            and rng_indexed
            and branch_reads_24(rnd_branch)
            and not branch_calls(rnd_branch, director.COMPUTE_AIM_PROCCODE)
        ):
            failures.add("zoshi-rnd-reheadings-from-rng")

        # (11) THE TOP/BOTTOM ENEMY DRIFT RE-AIMS TOWARD THE CRAFT. The aimed branch calls the aim compute
        # and writes the drift from the 24-tier; it does NOT draw from the rng stream. Without the aim
        # compute the drift would not track the craft.
        if not (
            fork is not None
            and branch_calls(aimed_branch, director.COMPUTE_AIM_PROCCODE)
            and branch_reads_24(aimed_branch)
            and not branch_calls(aimed_branch, director.RNG_PROCCODE)
        ):
            failures.add("zoshi-aimed-reheadings-toward-craft")

        # (12) SPIN ANIMATION. The update writes `slot code` = ZOSHI_INIT_CODE + (tick mod ZOSHI_ANIM_FRAMES)
        # each active tick, so the renderer reads only the Stage slot lists and every Zoshi spins in
        # lockstep. Distinct from the bullet's constant `slot code` write.
        anim_present = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_CODE_ID
            and (item := rref(b["inputs"].get("ITEM"))) is not None
            and item["opcode"] == "operator_add"
            and num_operand(item["inputs"].get("NUM1")) == director.ZOSHI_INIT_CODE
            and (m := rref(item["inputs"].get("NUM2"))) is not None
            and m["opcode"] == "operator_mod"
            and var_operand(m["inputs"].get("NUM1")) == director.TICK_ID
            and num_operand(m["inputs"].get("NUM2")) == director.ZOSHI_ANIM_FRAMES
            for b in update_body
        )
        if not anim_present:
            failures.add("zoshi-anim-spin")
        return failures

    # Roadmap closure evidence for leaf `air.zoshi` (AIR-03): Zoshi is a live THREE-TYPES-OVER-ONE-CORE
    # family — three inits (top random-Y, bottom fixed-edge craft-excluding-Y, rnd random-Y) spawned by type
    # from the formation wave, one shared updater driven by the ordered walk, each aiming its entry drift on
    # the 1.5 px/frame 24-tier and firing the SAME aimed 32-tier bullet under the Zoshi mask. On each fire it
    # re-headings its own drift: top/bottom toward the craft, rnd to a random 24-tier angle drawn from the
    # rng step (the distinctive erratic flyer) — the "random" is the MOVEMENT, not the shot. The live proof
    # (three types spawn and fire aimed shots; 0C veers erratically) is the harness `zoshi-top-aims-and-fires`
    # / `zoshi-bottom-enters-edge` / `zoshi-rnd-veers-erratically`.
    # roadmap-evidence: AIR-03 success  (test_zoshi_slice_authoring_present — four lifecycle procs warp, spawn-inits-three-types + dispatch-one-updater by type, all inits aim the 24-tier, top/bottom 100 pts + rnd 70, bottom fixed-edge entry, all capture the fire mask, fires one shared 32-tier aimed bullet under the masked phase, rnd drift re-heads from the rng draw + top/bottom re-aim toward the craft, spin anim)
    # roadmap-evidence: AIR-03 failure  (test_zoshi_slice_negative_fixtures — each contract clause corrupted bites)
    def test_zoshi_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air03_failures(project))

    def test_zoshi_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air03_failures(base))

        def _cref(inp):
            return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

        def _body(p, proccode):
            stage = next(t for t in p["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, proccode)

        def unwarp_update(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_ZOSHI_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_top_init_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.INIT_ZOSHI_TOP_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_dispatch_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_ZOSHI_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def init_aim_wrong_tier(p: dict) -> None:
            # Repoint the top init's 24-tier aim reads to the 32-tier tables → the initial-drift-aims-24
            # clause bites (the entry drift would move at the wrong speed/tier).
            stage, body = _body(p, director.INIT_ZOSHI_TOP_PROCCODE)
            swap = {
                director.AIM_DX_24_ID: ("aim dx 32", director.AIM_DX_32_ID),
                director.AIM_DY_24_ID: ("aim dy 32", director.AIM_DY_32_ID),
            }
            for b in body:
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] in swap:
                    b["fields"]["LIST"] = list(swap[b["fields"]["LIST"][1]])

        def wrong_top_points(p: dict) -> None:
            stage, body = _body(p, director.INIT_ZOSHI_TOP_PROCCODE)
            for b in body:
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_PTS_ID:
                    b["inputs"]["ITEM"] = [1, [4, str(director.ZOSHI_PTS_AIMED + 1)]]

        def wrong_bottom_points(p: dict) -> None:
            stage, body = _body(p, director.INIT_ZOSHI_BOTTOM_PROCCODE)
            for b in body:
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_PTS_ID:
                    b["inputs"]["ITEM"] = [1, [4, str(director.ZOSHI_PTS_AIMED + 1)]]

        def wrong_rnd_points(p: dict) -> None:
            stage, body = _body(p, director.INIT_ZOSHI_RND_PROCCODE)
            for b in body:
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_PTS_ID:
                    b["inputs"]["ITEM"] = [1, [4, str(director.ZOSHI_PTS_RND + 1)]]

        def move_bottom_entry(p: dict) -> None:
            # Change the bottom variant's fixed edge row off ZOSHI_BOTTOM_EDGE_X → the fixed-edge clause
            # bites (it would no longer enter at the bottom edge).
            stage, body = _body(p, director.INIT_ZOSHI_BOTTOM_PROCCODE)
            entry = director.ZOSHI_BOTTOM_EDGE_X * director.SLOT_UNITS_PER_CELL
            for b in body:
                it = b["inputs"].get("ITEM") if b["opcode"] == "data_replaceitemoflist" else None
                if not (b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_X_ID):
                    continue
                if isinstance(it, list) and len(it) >= 2 and isinstance(it[1], list) and it[1][0] in (4, 5, 6, 7, 8, 9, 10) and int(it[1][1]) == entry:
                    b["inputs"]["ITEM"] = [1, [4, str(entry + director.SLOT_UNITS_PER_CELL)]]

        def drop_mask_capture(p: dict) -> None:
            # Repoint the top init's fire-mask capture to a different var → the captures-fire-mask clause
            # bites (the shared cadence would read the wrong mask).
            stage, body = _body(p, director.INIT_ZOSHI_TOP_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_FIRE_MASK_ID
                ):
                    it = b["inputs"].get("ITEM")
                    if isinstance(it, list) and len(it) >= 2 and isinstance(it[1], list) and it[1][0] == 12:
                        it[1][2] = director.TICK_ID

        def drop_fire(p: dict) -> None:
            # Silence the single shared shot (retarget the allocator call) → the fires-shared-aimed-bullet
            # clause bites (zero allocator calls).
            stage, body = _body(p, director.UPDATE_ZOSHI_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def break_zero_gate(p: dict) -> None:
            # Change the fire-timer-zero gate constant off 0 → the masked-phase clause bites (the fire no
            # longer gates on the countdown reaching zero).
            stage, body = _body(p, director.UPDATE_ZOSHI_PROCCODE)
            for b in body:
                if b["opcode"] != "operator_equals":
                    continue
                lhs = _cref(b["inputs"].get("OPERAND1"))
                lb = stage["blocks"].get(lhs) if lhs else None
                if (
                    lb is not None
                    and lb["opcode"] == "data_itemoflist"
                    and lb["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "99"]]

        def _fork(stage):
            blocks = stage["blocks"]
            for b in _proc_body_blocks(stage, director.UPDATE_ZOSHI_PROCCODE):
                if b["opcode"] != "control_if_else":
                    continue
                cond = blocks.get(_cref(b["inputs"].get("CONDITION")))
                lhs = blocks.get(_cref(cond["inputs"].get("OPERAND1"))) if cond else None
                if (
                    cond is not None
                    and cond["opcode"] == "operator_equals"
                    and lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_TYPE_ID
                ):
                    o2 = cond["inputs"].get("OPERAND2")
                    if isinstance(o2, list) and isinstance(o2[1], list) and int(o2[1][1]) == director.ZOSHI_RND_TYPE:
                        return b
            return None

        def _descend(stage, start_id):
            blocks = stage["blocks"]
            seen: set = set()
            frontier = [start_id]
            while frontier:
                bid = frontier.pop()
                if not bid or bid in seen or bid not in blocks:
                    continue
                seen.add(bid)
                blk = blocks[bid]
                frontier.append(blk.get("next"))
                for value in blk.get("inputs", {}).values():
                    if isinstance(value, list) and len(value) >= 2 and isinstance(value[1], str):
                        frontier.append(value[1])
            return [(bid, blocks[bid]) for bid in seen]

        def unrandom_rnd_branch(p: dict) -> None:
            # Silence the rnd branch's rng draw (retarget its rng-step call) → the rnd-reheadings-from-rng
            # clause bites: the 0C drift would no longer come from the stream (the erratic flyer regression).
            stage = next(t for t in p["targets"] if t["isStage"])
            fork = _fork(stage)
            start = _cref(fork["inputs"].get("SUBSTACK"))
            for _bid, b in _descend(stage, start):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.RNG_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def unaim_aimed_branch(p: dict) -> None:
            # Silence the aimed branch's aim compute (retarget its call) → the aimed-reheadings-toward-craft
            # clause bites: top/bottom would no longer curve toward the craft.
            stage = next(t for t in p["targets"] if t["isStage"])
            fork = _fork(stage)
            start = _cref(fork["inputs"].get("SUBSTACK2"))
            for _bid, b in _descend(stage, start):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.COMPUTE_AIM_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def break_anim(p: dict) -> None:
            # Change the spin base off ZOSHI_INIT_CODE → the anim clause bites (the spin would read the
            # wrong costume band).
            stage, body = _body(p, director.UPDATE_ZOSHI_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if not (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_CODE_ID
                ):
                    continue
                item = blocks.get(_cref(b["inputs"].get("ITEM")))
                if item is None or item["opcode"] != "operator_add":
                    continue
                n1 = item["inputs"].get("NUM1")
                if isinstance(n1, list) and isinstance(n1[1], list) and n1[1][0] in (4, 5, 6, 7, 8, 9, 10):
                    item["inputs"]["NUM1"] = [1, [4, str(director.ZOSHI_INIT_CODE + 7)]]

        cases = [
            ("zoshi-lifecycle-procs-warp", unwarp_update),
            ("spawn-inits-zoshi", drop_top_init_call),
            ("dispatch-updates-zoshi", drop_dispatch_call),
            ("zoshi-inits-aim-24-tier", init_aim_wrong_tier),
            ("zoshi-top-awards-100", wrong_top_points),
            ("zoshi-bottom-awards-100", wrong_bottom_points),
            ("zoshi-rnd-awards-70", wrong_rnd_points),
            ("zoshi-bottom-fixed-edge-entry", move_bottom_entry),
            ("zoshi-captures-fire-mask", drop_mask_capture),
            ("zoshi-fires-shared-aimed-bullet", drop_fire),
            ("zoshi-fire-masked-phase", break_zero_gate),
            ("zoshi-rnd-reheadings-from-rng", unrandom_rnd_branch),
            ("zoshi-aimed-reheadings-toward-craft", unaim_aimed_branch),
            ("zoshi-anim-spin", break_anim),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air03_failures(project), label)

    @staticmethod
    def _air04_failures(project: dict) -> set:
        """AIR-04 Jara authoring contract — violated labels. Pins the structural facts that make Jara a
        faithful TWO-TYPES-OVER-ONE-CORE final aerial (handle_55/handle_56 3502-3599): one shared init and
        one shared update serve both the 0x55 shooter and the 0x56 silent; the spawner inits both by type
        and the ordered walk drives the one shared updater; both run atomically (warp). The shared init
        draws its Y craft-EXCLUDING (jara_init 3580, the +/-8 reject, distinct from the Kapi's craft-
        overlapping draw), aims the entry drift TOWARD the craft on the FAST 48-magnitude tier (3 px/frame,
        3581) and awards 150 points, and captures NO fire mask / seeds NO fire timer (jara_init never sets
        _FFREQ — the distinctive negative vs every prior shooter). The shared update commits a ONE-WAY turn
        only while APPROACHING and only inside the lateral proximity band [LOW, HIGH] (3591-3594): the turn
        peels the LATERAL velocity AWAY from the craft (slot dy ramps by +/-JARA_TURN_LATERAL_ACCEL in the
        latched side, slot dx untouched). At that same transition — and ONLY then — the 0x55 shooter fires
        EXACTLY ONE aimed bullet DIRECTLY via the allocator (jara_shoot 3544), nested inside BOTH the
        `flag == APPROACH` transition gate (so it cannot recur once the flag leaves APPROACH) and the
        `slot type == SHOOTER` gate (so the 0x56 silent never fires); it takes no mask and calls no shared
        fire gate. The 6-frame spin is render-only and lives in the Jara target: static entry frame while
        APPROACHING, the phase-cycled spin once TURNED (the settling harness advances whole ticks and
        cannot see a single fired/turned frame, so the once-only guards are pinned structurally)."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def rref(inp):
            r = ref(inp)
            return blocks.get(r) if r else None

        def num_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
            ):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def const_item(b):
            return num_operand(b["inputs"].get("ITEM"))

        init_body = _proc_body_blocks(stage, director.INIT_JARA_PROCCODE)
        update_body = _proc_body_blocks(stage, director.UPDATE_JARA_PROCCODE)
        id_of = {id(b): bid for bid, b in blocks.items()}

        # A block's condition subtree contains `<data_itemoflist LIST=list_id> == value`.
        def cond_has_eq(cond_id, list_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "operator_equals":
                    lhs = rref(b["inputs"].get("OPERAND1"))
                    if (
                        lhs is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == list_id
                        and num_operand(b["inputs"].get("OPERAND2")) == value
                    ):
                        return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        # A block's condition subtree contains a numeric operand equal to `value` (used to spot the
        # proximity-band constants inside the AND-wrapped turn gate).
        def cond_has_num(cond_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                for key, v in b.get("inputs", {}).items():
                    if num_operand(v) == value:
                        return True
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        # True when some ANCESTOR `if` of node_id satisfies pred(condition_id). The builder chains blocks
        # forward through `next` with `parent` pointing back (to the prior sibling, or the enclosing C-block
        # for a substack's first child), so a parent-walk climbs through siblings up to each enclosing `if`
        # and beyond — reaching every ancestor gate (the same idiom the Torkan/Zoshi checks use).
        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        def gated_by_flag(node_id, value):
            return ancestor_if(node_id, lambda c: cond_has_eq(c, director.SLOT_FLAG_ID, value))

        # (1) Both Jara lifecycle procedures exist and are warp (atomic) — a non-warp lifecycle proc would
        # yield mid-slot, letting a half-moved/half-turned Jara render or be hit.
        for proccode in (director.INIT_JARA_PROCCODE, director.UPDATE_JARA_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("jara-lifecycle-procs-warp")

        # (2) The spawner inits Jara (shared by both types); (3) the ordered walk dispatches to the ONE
        # shared updater. Both branches OR the two types, so a single init/update call covers 0x55 and 0x56.
        if not calls(director.INIT_JARA_PROCCODE):
            failures.add("spawn-inits-jara")
        if not calls(director.UPDATE_JARA_PROCCODE):
            failures.add("dispatch-updates-jara")

        # (4) The spawn init aims the entry drift TOWARD the craft on the FAST 48-magnitude tier
        # (3 px/frame) — it reads both 48-magnitude aim tables (the Terrazi/Torkan fast angle table).
        init_lists = {
            b["fields"]["LIST"][1] for b in init_body if b["opcode"] == "data_itemoflist"
        }
        if not {director.AIM_DX_48_ID, director.AIM_DY_48_ID} <= init_lists:
            failures.add("jara-aims-fast-tier")

        # (5) The spawn init awards 150 points — it writes `slot pts` = JARA_PTS (the 1-based value-table
        # index of 150, both types). A wrong index would score the wrong value on the kill.
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and const_item(b) == director.JARA_PTS
            for b in init_body
        ):
            failures.add("jara-awards-150-pts")

        # (6) The spawn init draws its Y CRAFT-EXCLUDING (jara_init's gen_rnd_spriteY reject): the draw
        # rejects any column whose |player col - candidate| is within SPAWN_CRAFT_GAP — an `abs(...)` fed
        # to an `< SPAWN_CRAFT_GAP` test. This is the clean discriminator from the Kapi's craft-overlapping
        # draw (no such reject), so a Jara never spawns on top of the craft's column.
        if not any(
            b["opcode"] == "operator_lt"
            and (lhs := rref(b["inputs"].get("OPERAND1"))) is not None
            and lhs["opcode"] == "operator_mathop"
            and lhs["fields"].get("OPERATOR", [None])[0] == "abs"
            and num_operand(b["inputs"].get("OPERAND2")) == director.SPAWN_CRAFT_GAP
            for b in init_body
        ):
            failures.add("jara-craft-excluding-draw")

        # (7) NO FIRE MASK / NO FIRE TIMER at spawn (the distinctive negative — jara_init never sets
        # _FFREQ). The shooter's single shot is proximity-gated at the turn, not paced by a captured mask;
        # the silent type never fires. A `slot fire mask` / `slot fire timer` write in the init would be a
        # periodic-fire regression (the shape of every prior shooter), so its ABSENCE is the contract.
        if any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] in (director.SLOT_FIRE_MASK_ID, director.SLOT_FIRE_TIMER_ID)
            for b in init_body
        ):
            failures.add("jara-no-fire-mask")

        # (8) THE SHOOTER FIRES EXACTLY ONCE, GATED IN THE APPROACH->TURN TRANSITION. There is exactly ONE
        # allocator call in the update body, and it sits inside the `flag == APPROACH` transition gate — so
        # the instant it fires the transition flips the flag off APPROACH and the shot cannot recur. It
        # fires DIRECTLY (never the shared fire-permission gate). Structural: the settling harness advances
        # whole ticks and cannot observe a single re-fired frame.
        alloc_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
        ]
        if len(alloc_ids) != 1 or not gated_by_flag(alloc_ids[0], director.JARA_FLAG_APPROACH):
            failures.add("jara-fires-once-in-approach-gate")
        if any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
            for b in update_body
        ):
            failures.add("jara-fires-without-gate")

        # (9) ONLY THE 0x55 SHOOTER FIRES. The one allocator call sits inside `slot type == SHOOTER`, so the
        # 0x56 silent type never fires — the shared update's only per-type branch on the fire path.
        if len(alloc_ids) != 1 or not ancestor_if(
            alloc_ids[0], lambda c: cond_has_eq(c, director.SLOT_TYPE_ID, director.JARA_SHOOTER_TYPE)
        ):
            failures.add("jara-shooter-only-fires")

        # (10) THE TURN IS PROXIMITY-GATED. Every write that latches a turn side (slot flag -> TURN_MINUS or
        # TURN_PLUS) sits inside a gate whose condition tests the lateral proximity band — it carries BOTH
        # band constants (LOW and HIGH). Without the band the Jara would turn immediately instead of cruising
        # to the craft's row first.
        turn_latch_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            and const_item(b) in (director.JARA_FLAG_TURN_MINUS, director.JARA_FLAG_TURN_PLUS)
        ]
        band = lambda c: cond_has_num(c, director.JARA_PROXIMITY_LOW) and cond_has_num(
            c, director.JARA_PROXIMITY_HIGH
        )
        if not turn_latch_ids or not all(
            ancestor_if(t, band) for t in turn_latch_ids
        ):
            failures.add("jara-turn-proximity-gated")

        # (11) THE TURN RAMPS THE LATERAL VELOCITY AWAY. The TURN_MINUS branch decrements `slot dy` by
        # JARA_TURN_LATERAL_ACCEL (an `slot dy - accel` write gated by `flag == TURN_MINUS`) and the
        # TURN_PLUS branch increments it by the same accel (gated by `flag == TURN_PLUS`) — the peel-away
        # ramp, opposite signs on the two sides. `slot dx` is untouched (no scroll decel, unlike the Kapi).
        def ramps_dy(op, flag_value):
            for b in update_body:
                if not (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DY_ID
                ):
                    continue
                item = rref(b["inputs"].get("ITEM"))
                if item is None or item["opcode"] != op:
                    continue
                base = rref(item["inputs"].get("NUM1"))
                if (
                    base is not None
                    and base["opcode"] == "data_itemoflist"
                    and base["fields"]["LIST"][1] == director.SLOT_DY_ID
                    and num_operand(item["inputs"].get("NUM2")) == director.JARA_TURN_LATERAL_ACCEL
                    and gated_by_flag(id_of[id(b)], flag_value)
                ):
                    return True
            return False

        if not (
            ramps_dy("operator_subtract", director.JARA_FLAG_TURN_MINUS)
            and ramps_dy("operator_add", director.JARA_FLAG_TURN_PLUS)
        ):
            failures.add("jara-turn-ramps-dy-away")

        # (12) SPIN ONLY AFTER THE TURN; STATIC WHILE APPROACHING (render-only, in the Jara target). The
        # renderer chooses the active costume by phase: `if slot flag == APPROACH` -> a FIXED entry costume
        # (jara/spin/01, the silent cruise); else the phase-cycled spin (a computed costume). Without the
        # approach gate the Jara would spin during its silent cruise.
        jara = next((t for t in project["targets"] if t.get("name") == director.JARA_TARGET), None)
        static_gated = False
        if jara is not None:
            jblocks = jara["blocks"]

            def jref(inp):
                return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

            for bid, b in jblocks.items():
                if b["opcode"] != "control_if_else":
                    continue
                cond = jblocks.get(jref(b["inputs"].get("CONDITION")))
                if cond is None or cond["opcode"] != "operator_equals":
                    continue
                lhs = jblocks.get(jref(cond["inputs"].get("OPERAND1")))
                rhs = cond["inputs"].get("OPERAND2")
                is_approach_gate = (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                    and num_operand(rhs) == director.JARA_FLAG_APPROACH
                )
                if not is_approach_gate:
                    continue
                # True branch: a FIXED costume switch (COSTUME input is a bare shadow [1, menu]); false
                # branch: a COMPUTED costume (obscured shadow [3, reporter, menu]) — the spin.
                true_first = jref(b["inputs"].get("SUBSTACK"))
                false_first = jref(b["inputs"].get("SUBSTACK2"))
                true_static = (
                    true_first is not None
                    and jblocks[true_first]["opcode"] == "looks_switchcostumeto"
                    and isinstance(jblocks[true_first]["inputs"].get("COSTUME"), list)
                    and jblocks[true_first]["inputs"]["COSTUME"][0] == 1
                )

                def branch_has_computed_costume(first_id):
                    cur = first_id
                    while cur:
                        blk = jblocks.get(cur)
                        if blk is None:
                            break
                        if (
                            blk["opcode"] == "looks_switchcostumeto"
                            and isinstance(blk["inputs"].get("COSTUME"), list)
                            and blk["inputs"]["COSTUME"][0] == 3
                        ):
                            return True
                        # descend one level into a nested if/if_else's substacks too (the spin sits in the
                        # turn-side if_else nested under the false branch).
                        for key in ("SUBSTACK", "SUBSTACK2"):
                            nested = jref(blk["inputs"].get(key)) if key in blk.get("inputs", {}) else None
                            sub = nested
                            while sub:
                                sblk = jblocks.get(sub)
                                if sblk is None:
                                    break
                                if (
                                    sblk["opcode"] == "looks_switchcostumeto"
                                    and isinstance(sblk["inputs"].get("COSTUME"), list)
                                    and sblk["inputs"]["COSTUME"][0] == 3
                                ):
                                    return True
                                sub = sblk.get("next")
                        cur = blk.get("next")
                    return False

                if true_static and branch_has_computed_costume(false_first):
                    static_gated = True
                    break
        if not static_gated:
            failures.add("jara-spin-only-after-turn")

        return failures

    # Roadmap closure evidence for leaf `air.jara` (AIR-04): Jara is a live TWO-TYPES-OVER-ONE-CORE family —
    # the 0x55 shooter and 0x56 silent share one init and one update, spawned by type from the formation wave
    # (adjacent runs, so the visual pair is emergent, never coupled), advanced by the ordered walk. Each
    # cruises its craft-excluding entry aimed on the 3 px/frame fast tier without spinning, then at the
    # lateral proximity band peels AWAY (slot dy ramps, slot dx held) and spins the 6-frame render animation;
    # the shooter fires ONE aimed bullet at the turn (gated so it cannot recur), the silent never fires; 150
    # pts each, scored independently. The live proof (shooter fires at proximity; the turn is reached and the
    # spin/dy grows) is the harness `jara-shooter-fires-at-proximity` / `jara-peels-and-spins`.
    # roadmap-evidence: AIR-04 success  (test_jara_slice_authoring_present — lifecycle procs warp, spawn-inits + dispatch-updates by type, init aims the fast 48-tier, awards 150, craft-excluding draw, no fire mask, shooter fires exactly one aimed bullet gated in the approach->turn transition and only for 0x55, silent never fires, turn proximity-gated, dy ramps away, spin only after the turn)
    # roadmap-evidence: AIR-04 failure  (test_jara_slice_negative_fixtures — each contract clause corrupted bites)
    def test_jara_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air04_failures(project))

    def test_jara_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air04_failures(base))

        def _body(p, proccode):
            stage = next(t for t in p["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, proccode)

        def unwarp_update(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_JARA_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_init_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.INIT_JARA_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_dispatch_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_JARA_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def aim_wrong_tier(p: dict) -> None:
            # Repoint the init's fast 48-tier aim reads to the generic 32-tier tables → the fast-tier clause
            # bites (the entry drift would cruise at the wrong speed).
            stage, body = _body(p, director.INIT_JARA_PROCCODE)
            swap = {
                director.AIM_DX_48_ID: ("aim dx 32", director.AIM_DX_32_ID),
                director.AIM_DY_48_ID: ("aim dy 32", director.AIM_DY_32_ID),
            }
            for b in body:
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] in swap:
                    b["fields"]["LIST"] = list(swap[b["fields"]["LIST"][1]])

        def wrong_points(p: dict) -> None:
            # Change the awarded value-table index off JARA_PTS → the 150-point award clause bites.
            stage, body = _body(p, director.INIT_JARA_PROCCODE)
            for b in body:
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_PTS_ID:
                    b["inputs"]["ITEM"] = [1, [4, str(director.JARA_PTS + 1)]]

        def drop_craft_exclusion(p: dict) -> None:
            # Zero the craft-proximity reject distance (SPAWN_CRAFT_GAP -> 0) so |player col - col| < 0 is
            # never true → the draw stops excluding the craft's column. The craft-excluding clause bites.
            stage, body = _body(p, director.INIT_JARA_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "operator_lt"
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and b["inputs"]["OPERAND2"][1][1] == director.SPAWN_CRAFT_GAP
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "0"]]

        def capture_fire_mask(p: dict) -> None:
            # Repurpose the init's `slot code` write to write `slot fire timer` instead → the init now seeds
            # a fire timer (a periodic-fire regression). The no-fire-mask clause bites.
            stage, body = _body(p, director.INIT_JARA_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_CODE_ID
                ):
                    b["fields"]["LIST"] = ["slot fire timer", director.SLOT_FIRE_TIMER_ID]
                    break

        def ungate_fire_from_approach(p: dict) -> None:
            # Flip the transition gate's `flag == APPROACH` constant to a value the flag never holds → the
            # shooter's shot is no longer nested under an APPROACH gate, so it could re-fire after the turn.
            # The fires-once-in-approach-gate clause bites (the fires-every-tick regression).
            stage, body = _body(p, director.UPDATE_JARA_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = blocks.get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and b["inputs"]["OPERAND2"][1][1] == director.JARA_FLAG_APPROACH
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "99"]]

        def add_fire_gate(p: dict) -> None:
            # Turn the direct allocator call into a shared fire-gate call → the fires-without-gate clause
            # bites (a periodic-masked-fire regression). Also trips fires-once (the allocator vanishes).
            stage, body = _body(p, director.UPDATE_JARA_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.FIRE_GATE_PROCCODE

        def fire_from_silent(p: dict) -> None:
            # Flip the fire's `slot type == SHOOTER` gate constant to the SILENT type → the 0x55 shooter no
            # longer fires and the 0x56 silent would. The shooter-only-fires clause bites.
            stage, body = _body(p, director.UPDATE_JARA_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = blocks.get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_TYPE_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and b["inputs"]["OPERAND2"][1][1] == director.JARA_SHOOTER_TYPE
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, str(director.JARA_SILENT_TYPE)]]

        def ungate_turn_proximity(p: dict) -> None:
            # Change the low proximity-band constant off JARA_PROXIMITY_LOW → the turn gate no longer carries
            # both band constants, so the check can no longer see it as proximity-gated. The clause bites.
            stage, body = _body(p, director.UPDATE_JARA_PROCCODE)
            for b in body:
                if (
                    b["opcode"] in ("operator_lt", "operator_gt")
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and b["inputs"]["OPERAND2"][1][1] == director.JARA_PROXIMITY_LOW
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "-99"]]

        def flatten_turn_ramp(p: dict) -> None:
            # Zero the lateral accel in the TURN_MINUS decrement (accel -> 0) → the peel-away ramp no longer
            # carries JARA_TURN_LATERAL_ACCEL on the minus side. The dy-ramps-away clause bites.
            stage, body = _body(p, director.UPDATE_JARA_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if not (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DY_ID
                ):
                    continue
                item = blocks.get(b["inputs"].get("ITEM", [None, None])[1])
                if item is None or item["opcode"] != "operator_subtract":
                    continue
                n2 = item["inputs"].get("NUM2")
                if isinstance(n2, list) and isinstance(n2[1], list) and int(n2[1][1]) == director.JARA_TURN_LATERAL_ACCEL:
                    item["inputs"]["NUM2"] = [1, [4, "0"]]

        def spin_during_approach(p: dict) -> None:
            # Flip the renderer's `slot flag == APPROACH` costume gate to a value the flag never holds → the
            # static entry frame is never selected, so the Jara would spin during its silent cruise. The
            # spin-only-after-turn clause bites.
            jara = next(t for t in p["targets"] if t.get("name") == director.JARA_TARGET)
            jblocks = jara["blocks"]
            for b in jblocks.values():
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = jblocks.get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and b["inputs"]["OPERAND2"][1][1] == director.JARA_FLAG_APPROACH
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "99"]]

        cases = [
            ("jara-lifecycle-procs-warp", unwarp_update),
            ("spawn-inits-jara", drop_init_call),
            ("dispatch-updates-jara", drop_dispatch_call),
            ("jara-aims-fast-tier", aim_wrong_tier),
            ("jara-awards-150-pts", wrong_points),
            ("jara-craft-excluding-draw", drop_craft_exclusion),
            ("jara-no-fire-mask", capture_fire_mask),
            ("jara-fires-once-in-approach-gate", ungate_fire_from_approach),
            ("jara-fires-without-gate", add_fire_gate),
            ("jara-shooter-only-fires", fire_from_silent),
            ("jara-turn-proximity-gated", ungate_turn_proximity),
            ("jara-turn-ramps-dy-away", flatten_turn_ramp),
            ("jara-spin-only-after-turn", spin_during_approach),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air04_failures(project), label)

    @staticmethod
    def _air07_failures(project: dict) -> set:
        """AIR-07 Zakato authoring contract — violated labels. Pins the structural facts that make the four
        base Zakato variants a faithful TELEPORT->ACTIVE->SELF-DESTRUCT family (handle_12-15 3733-3859):
        ONE shared init and ONE shared update serve slow (0x12) / close-Y (0x13) / fast (0x14) /
        continuous (0x15); the spawner inits all four by type and the ordered walk drives the one shared
        updater; both run atomically (warp). The shared init spawns the slot INDESTRUCTIBLE and NOT MOVING —
        `slot state` = SLOT_TELEPORT (the distinctive negative: every prior aerial spawns SLOT_ACTIVE, so the
        shared `check air hit` gate cannot score a Zakato mid-teleport — the arcade's _STATE=3 at init_teleport
        3995) — draws its entry column craft-INDEPENDENTLY (gen_random_Y_store_obj, no craft reject), stamps ZAKATO_MAIN_CODE, and awards a PER-VARIANT value
        (slow 100 / close-Y 200 / fast 150 / continuous 300, each gated by `walk type`), capturing NO fire mask
        and seeding NO fire timer at spawn. The shared update carries the phase EXPLICITLY in `slot state`:
        TELEPORT advances a sparkle clock and, at ZAKATO_PHASE_FRAMES, commits to ACTIVE — setting the straight
        variants' scroll-axis drift (slot dx = ZAKATO_STRAIGHT_DX, dy 0) or the aimed variants' 32-magnitude
        aim (2 px/frame, the generic tier), and seeding the fused variants' one-shot random fuse (slow mod 256,
        fast mod 64). ACTIVE fires EXACTLY ONE aimed bullet DIRECTLY via the allocator — on the fused fuse
        reaching 0 or the proximity variants' lateral band [LOW, HIGH] — then flips itself to SLOT_SELF_EXPLODE;
        it never calls the shared fire gate. SELF_EXPLODE and a shot-kill (SLOT_HIT) both run the shared
        `explode toroid tick`; only the shot-kill is scored (by the detector, which never runs on a
        self-destructing slot), so the self-destruct awards NOTHING. The teleport(reversed)/self-destruct
        (forward)/hit(forward) sprites are render-only in the Zakato target (the settling harness advances whole
        ticks and cannot see a single fired/committed frame, so the once-only facts are pinned structurally)."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def rref(inp):
            r = ref(inp)
            return blocks.get(r) if r else None

        def num_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
            ):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def const_item(b):
            return num_operand(b["inputs"].get("ITEM"))

        init_body = _proc_body_blocks(stage, director.INIT_ZAKATO_PROCCODE)
        update_body = _proc_body_blocks(stage, director.UPDATE_ZAKATO_PROCCODE)
        id_of = {id(b): bid for bid, b in blocks.items()}

        def cond_has_eq(cond_id, list_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "operator_equals":
                    lhs = rref(b["inputs"].get("OPERAND1"))
                    if (
                        lhs is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == list_id
                        and num_operand(b["inputs"].get("OPERAND2")) == value
                    ):
                        return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def cond_has_num(cond_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                for key, v in b.get("inputs", {}).items():
                    if num_operand(v) == value:
                        return True
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        # A subtree (an op tree rooted at root_id) reads `data_itemoflist` of every list in `list_ids`.
        def subtree_reads_lists(root_id, list_ids):
            seen, frontier, found = set(), [root_id], set()
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "data_itemoflist":
                    found.add(b["fields"]["LIST"][1])
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return set(list_ids) <= found

        # True when some ANCESTOR `if` of node_id satisfies pred(condition_id). Parent pointers chain back
        # through siblings up to each enclosing C-block, so this reaches every ancestor gate (the same idiom
        # the Jara/Torkan/Zoshi checks use).
        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        # Like cond_has_eq but the left side is a VARIABLE read, not a list item — the spawn stamps gate on
        # the `walk type` variable the walk carries. The variable appears INLINE as a primitive operand
        # `[3|1, [12, name, var_id], ...]` (not a referenced block), so match that shape.
        def is_var_operand(inp, var_id):
            return (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 3
                and inp[1][0] == 12
                and inp[1][2] == var_id
            )

        def cond_has_var_eq(cond_id, var_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "operator_equals" and (
                    (
                        is_var_operand(b["inputs"].get("OPERAND1"), var_id)
                        and num_operand(b["inputs"].get("OPERAND2")) == value
                    )
                    or (
                        is_var_operand(b["inputs"].get("OPERAND2"), var_id)
                        and num_operand(b["inputs"].get("OPERAND1")) == value
                    )
                ):
                    return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def gated_by_state(node_id, value):
            return ancestor_if(node_id, lambda c: cond_has_eq(c, director.SLOT_STATE_ID, value))

        def gated_by_walktype(node_id, value):
            return ancestor_if(node_id, lambda c: cond_has_var_eq(c, director.WALK_TYPE_ID, value))

        # (1) Both Zakato lifecycle procedures exist and are warp (atomic) — a non-warp lifecycle proc would
        # yield mid-slot, letting a half-teleported / half-fired Zakato render or be hit.
        for proccode in (director.INIT_ZAKATO_PROCCODE, director.UPDATE_ZAKATO_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("zakato-lifecycle-procs-warp")

        # (2) The spawner inits Zakato (shared by all four types); (3) the ordered walk dispatches the ONE
        # shared updater.
        if not calls(director.INIT_ZAKATO_PROCCODE):
            failures.add("spawn-inits-zakato")
        if not calls(director.UPDATE_ZAKATO_PROCCODE):
            failures.add("dispatch-updates-zakato")

        # (4) THE SPAWN IS INDESTRUCTIBLE — the init stamps `slot state` = SLOT_TELEPORT and NEVER
        # SLOT_ACTIVE (the distinctive discriminator from every prior aerial, which all spawn ACTIVE). The
        # shared hit gate ignores any non-ACTIVE slot, so a mid-teleport Zakato cannot be scored for free.
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and const_item(b) == director.SLOT_TELEPORT
            for b in init_body
        ) or any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and const_item(b) == director.SLOT_ACTIVE
            for b in init_body
        ):
            failures.add("zakato-spawns-teleporting")

        # (5) PER-VARIANT SCORING. The init stamps `slot pts` to each variant's value-table index, each
        # gated by `walk type` == that variant: slow->100, close-Y->200, fast->150, continuous->300. A
        # wrong index or a missing/mis-gated stamp would score the wrong value on a kill-before-fire.
        variant_pts = {
            director.ZAKATO_SLOW_TYPE: director.ZAKATO_SLOW_PTS,
            director.ZAKATO_CLOSEY_TYPE: director.ZAKATO_CLOSEY_PTS,
            director.ZAKATO_FAST_TYPE: director.ZAKATO_FAST_PTS,
            director.ZAKATO_CONT_TYPE: director.ZAKATO_CONT_PTS,
        }
        for vtype, vpts in variant_pts.items():
            stamped = [
                id_of[id(b)]
                for b in init_body
                if b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                and const_item(b) == vpts
            ]
            if not stamped or not any(gated_by_walktype(s, vtype) for s in stamped):
                failures.add("zakato-per-variant-points")

        # (6) NO FIRE MASK / NO FIRE TIMER AT SPAWN (the shooter negative — the fuse is drawn later, at the
        # teleport->active commit, not captured at spawn). A `slot fire mask` write in the init would be the
        # periodic-fire shape of the masked shooters; its ABSENCE is the contract.
        if any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] in (director.SLOT_FIRE_MASK_ID, director.SLOT_FIRE_TIMER_ID)
            for b in init_body
        ):
            failures.add("zakato-no-fire-mask")

        # (7) [removed] The Zakato line draws its entry column CRAFT-INDEPENDENTLY: init_teleport (3994) ->
        # gen_random_Y_store_obj (5147) is the in-range clamp with NO craft-proximity reject, so a Zakato
        # CAN teleport in over/adjacent to the craft's column. (The craft-excluding `gen_rnd_spriteY` at 5156
        # is a DIFFERENT routine the Zakato line never calls.) There is therefore no craft-exclusion contract
        # to pin here — the faithful no-exclusion families, e.g. Kapi/_air05, likewise carry no such clause.

        # (8) THE TELEPORT COMMITS TO ACTIVE. The update writes `slot state` = SLOT_ACTIVE gated under
        # `state == SLOT_TELEPORT` (the phase transition). Without it the Zakato would never become hittable
        # or move.
        commit_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and const_item(b) == director.SLOT_ACTIVE
        ]
        if not commit_ids or not all(gated_by_state(c, director.SLOT_TELEPORT) for c in commit_ids):
            failures.add("zakato-teleport-commits-active")

        # (9) THE COMMIT SETS BOTH MOTION MODELS. The straight variants (slow/close-Y) get the raw
        # scroll-axis drift `slot dx` = ZAKATO_STRAIGHT_DX (16 = 1 px/frame, dY 0); the aimed variants
        # (fast/continuous) read the 32-magnitude generic aim tables (2 px/frame). Both must be present.
        straight_dx = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DX_ID
            and const_item(b) == director.ZAKATO_STRAIGHT_DX
            for b in update_body
        )
        aim_dx = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DX_ID
            and (item := rref(b["inputs"].get("ITEM"))) is not None
            and item["opcode"] == "data_itemoflist"
            and item["fields"]["LIST"][1] == director.AIM_DX_32_ID
            for b in update_body
        )
        aim_dy = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DY_ID
            and (item := rref(b["inputs"].get("ITEM"))) is not None
            and item["opcode"] == "data_itemoflist"
            and item["fields"]["LIST"][1] == director.AIM_DY_32_ID
            for b in update_body
        )
        if not (straight_dx and aim_dx and aim_dy):
            failures.add("zakato-commit-sets-motion")

        # (10) THE FUSED VARIANTS SEED A ONE-SHOT RANDOM FUSE. The commit writes `slot fire timer` from an
        # `rng out` mod, one span per fused variant — slow mod ZAKATO_SLOW_FUSE_SPAN, fast mod
        # ZAKATO_FAST_FUSE_SPAN. Both spans must appear on a `slot fire timer` write's value subtree.
        fuse_writes = [
            b for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID
        ]
        spans = set()
        for b in fuse_writes:
            root = ref(b["inputs"].get("ITEM"))
            for span in (director.ZAKATO_SLOW_FUSE_SPAN, director.ZAKATO_FAST_FUSE_SPAN):
                if root is not None and cond_has_num(root, span):
                    spans.add(span)
        if spans != {director.ZAKATO_SLOW_FUSE_SPAN, director.ZAKATO_FAST_FUSE_SPAN}:
            failures.add("zakato-seeds-random-fuse")

        # (11) FIRES EXACTLY ONCE, THEN SELF-DESTRUCTS. There is exactly ONE allocator call in the update
        # body; it sits under `state == SLOT_ACTIVE`, and the SAME fire branch flips `slot state` to
        # SLOT_SELF_EXPLODE — so the shot cannot recur (an active slot that just fired is no longer ACTIVE).
        alloc_ids = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
        ]
        self_explode_writes = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and const_item(b) == director.SLOT_SELF_EXPLODE
        ]
        if (
            len(alloc_ids) != 1
            or not gated_by_state(alloc_ids[0], director.SLOT_ACTIVE)
            or not self_explode_writes
            or not all(gated_by_state(s, director.SLOT_ACTIVE) for s in self_explode_writes)
        ):
            failures.add("zakato-fires-once-then-self-destructs")

        # (12) FIRES DIRECTLY, NEVER THE SHARED FIRE GATE (a one-shot, not a periodic masked shooter).
        if any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
            for b in update_body
        ):
            failures.add("zakato-fires-without-gate")

        # (13) THE PROXIMITY TRIGGER CARRIES THE LATERAL BAND. Some fire-trigger condition in the update
        # tests the lateral offset band — it carries BOTH band constants (ZAKATO_CLOSEY_LOW and
        # ZAKATO_CLOSEY_HIGH). Without the band the close-Y/continuous variants would never fire on level.
        if not (
            any(cond_has_num(id_of[id(b)], director.ZAKATO_CLOSEY_LOW) for b in update_body)
            and any(cond_has_num(id_of[id(b)], director.ZAKATO_CLOSEY_HIGH) for b in update_body)
        ):
            failures.add("zakato-fire-trigger-proximity-band")

        # (14) SELF-DESTRUCT AWARDS NOTHING; A SHOT-KILL PLAYS THE SHARED EXPLOSION. The SELF_EXPLODE branch
        # runs the shared `explode toroid tick` (gated by state == SELF_EXPLODE); a SLOT_HIT plays the same
        # shared tick (gated by state == SLOT_HIT); and the shot detector is offered on the non-HIT path
        # (`check air hit`, a no-op unless ACTIVE — the ONLY thing that scores a Zakato). The update itself
        # never writes the score list, so a self-destructing slot (never seen by the detector) awards nothing.
        explode_calls = [
            id_of[id(b)]
            for b in update_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.EXPLODE_TICK_PROCCODE
        ]
        self_tick = any(gated_by_state(e, director.SLOT_SELF_EXPLODE) for e in explode_calls)
        hit_tick = any(gated_by_state(e, director.SLOT_HIT) for e in explode_calls)
        offers_detector = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.CHECK_AIR_HIT_PROCCODE
            for b in update_body
        )
        if not (self_tick and hit_tick and offers_detector):
            failures.add("zakato-self-destruct-no-score")

        return failures

    # Roadmap closure evidence for leaf `air.zakato` (AIR-07): the four base Zakato variants are a live
    # TELEPORT->ACTIVE->SELF-DESTRUCT family — slow / close-Y / fast / continuous share one init and one
    # update, spawned by type from the formation wave, advanced by the ordered walk. Each teleports in
    # indestructible (SLOT_TELEPORT, the hit gate ignores it), then becomes ACTIVE and moves — straight on the
    # scroll axis (slow/close-Y) or aimed on the 32-tier (fast/continuous) — fires EXACTLY ONE aimed bullet
    # (fused fuse expiry, or the proximity band for close-Y/continuous), and self-destructs awarding NOTHING;
    # killed by a shot first it scores its per-variant value (100/200/150/300). The live proof (teleports in,
    # becomes hittable, fires once then vanishes) is the harness `zakato-teleports-then-active` /
    # `zakato-fires-once-then-vanishes`.
    # roadmap-evidence: AIR-07 success  (test_zakato_slice_authoring_present — lifecycle procs warp, spawn-inits + dispatch-updates, spawns indestructible SLOT_TELEPORT not ACTIVE, per-variant points gated by walk type, no fire mask at spawn, craft-independent draw, teleport commits ACTIVE, commit sets straight dx and 32-tier aim, seeds slow/fast random fuse, fires exactly one aimed bullet then SELF_EXPLODE, never the fire gate, proximity band carries both constants, self-destruct runs the shared tick and awards nothing while a shot-kill plays the shared explosion)
    # roadmap-evidence: AIR-07 failure  (test_zakato_slice_negative_fixtures — each contract clause corrupted bites)
    def test_zakato_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air07_failures(project))

    def test_zakato_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air07_failures(base))

        def _body(p, proccode):
            stage = next(t for t in p["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, proccode)

        def unwarp_update(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_ZAKATO_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_init_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.INIT_ZAKATO_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def drop_dispatch_call(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_ZAKATO_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def spawn_active(p: dict) -> None:
            # Flip the init's SLOT_TELEPORT stamp to SLOT_ACTIVE → the Zakato spawns hittable/scorable with
            # no teleport phase. The spawns-teleporting clause bites (a free-kill regression).
            stage, body = _body(p, director.INIT_ZAKATO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and _const_item(b) == director.SLOT_TELEPORT
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.SLOT_ACTIVE)]]

        def wrong_points(p: dict) -> None:
            # Change the slow variant's value-table index off ZAKATO_SLOW_PTS → the per-variant-points
            # clause bites (the slow Zakato would score the wrong value on a kill-before-fire).
            stage, body = _body(p, director.INIT_ZAKATO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                    and _const_item(b) == director.ZAKATO_SLOW_PTS
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.ZAKATO_SLOW_PTS + 1)]]

        def capture_fire_mask(p: dict) -> None:
            # Repurpose the init's `slot code` write to write `slot fire mask` instead → the init now
            # captures a fire mask (a periodic-shooter regression). The no-fire-mask clause bites.
            stage, body = _body(p, director.INIT_ZAKATO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_CODE_ID
                ):
                    b["fields"]["LIST"] = ["slot fire mask", director.SLOT_FIRE_MASK_ID]
                    break

        def ungate_commit(p: dict) -> None:
            # Flip the teleport-completion gate's `state == SLOT_TELEPORT` to a value the state never holds →
            # the ACTIVE commit is no longer nested under the teleport gate. The commits-active clause bites.
            stage, body = _body(p, director.UPDATE_ZAKATO_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = blocks.get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and int(b["inputs"]["OPERAND2"][1][1]) == director.SLOT_TELEPORT
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "99"]]

        def flatten_straight_dx(p: dict) -> None:
            # Change the straight-variant commit's `slot dx` = ZAKATO_STRAIGHT_DX to 0 → the commit no longer
            # sets the scroll-axis drift. The commit-sets-motion clause bites.
            stage, body = _body(p, director.UPDATE_ZAKATO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DX_ID
                    and _const_item(b) == director.ZAKATO_STRAIGHT_DX
                ):
                    b["inputs"]["ITEM"] = [1, [4, "0"]]

        def drop_fast_fuse_span(p: dict) -> None:
            # Change the fast fuse span (mod ZAKATO_FAST_FUSE_SPAN) off its value → only one span remains on
            # a `slot fire timer` write subtree. The seeds-random-fuse clause bites.
            stage, body = _body(p, director.UPDATE_ZAKATO_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if b["opcode"] == "operator_mod" and _num_operand(b["inputs"].get("NUM2")) == director.ZAKATO_FAST_FUSE_SPAN:
                    b["inputs"]["NUM2"] = [1, [4, "7"]]

        def ungate_fire_from_active(p: dict) -> None:
            # Flip the fire branch's `state == SLOT_ACTIVE` gate to a value the state never holds → the shot
            # (and the SELF_EXPLODE flip) are no longer under the ACTIVE gate. The fires-once clause bites.
            stage, body = _body(p, director.UPDATE_ZAKATO_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = blocks.get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and int(b["inputs"]["OPERAND2"][1][1]) == director.SLOT_ACTIVE
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "99"]]

        def add_fire_gate(p: dict) -> None:
            # Turn the direct allocator call into a shared fire-gate call → the fires-without-gate clause
            # bites (a periodic-masked-fire regression). Also trips fires-once (the allocator vanishes).
            stage, body = _body(p, director.UPDATE_ZAKATO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.FIRE_GATE_PROCCODE

        def break_proximity_band(p: dict) -> None:
            # Change the low band constant off ZAKATO_CLOSEY_LOW → the proximity trigger no longer carries
            # both band constants. The proximity-band clause bites.
            stage, body = _body(p, director.UPDATE_ZAKATO_PROCCODE)
            for b in body:
                for key in ("OPERAND1", "OPERAND2", "NUM1", "NUM2"):
                    if _num_operand(b["inputs"].get(key)) == director.ZAKATO_CLOSEY_LOW:
                        b["inputs"][key] = [1, [4, "-99"]]

        def self_explode_no_tick(p: dict) -> None:
            # Neuter the SELF_EXPLODE branch's shared-tick call → the self-destruct no longer plays the
            # shared explosion. The self-destruct-no-score clause bites (it needs the shared tick present on
            # both the SELF_EXPLODE and HIT paths).
            stage, body = _body(p, director.UPDATE_ZAKATO_PROCCODE)
            blocks = stage["blocks"]
            id_of = {id(b): bid for bid, b in blocks.items()}

            def gated(node_id, value):
                cur = blocks.get(node_id)
                while cur is not None:
                    parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                    if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                        cond_id = parent["inputs"].get("CONDITION")
                        cond_id = cond_id[1] if isinstance(cond_id, list) and len(cond_id) >= 2 else None
                        seen, frontier = set(), [cond_id]
                        while frontier:
                            cid = frontier.pop()
                            if not cid or cid in seen or cid not in blocks:
                                continue
                            seen.add(cid)
                            bb = blocks[cid]
                            if bb["opcode"] == "operator_equals":
                                lo1 = bb["inputs"].get("OPERAND1")
                                lb = blocks.get(lo1[1]) if isinstance(lo1, list) and len(lo1) >= 2 and isinstance(lo1[1], str) else None
                                o2 = bb["inputs"].get("OPERAND2")
                                if (
                                    lb is not None
                                    and lb["opcode"] == "data_itemoflist"
                                    and lb["fields"]["LIST"][1] == director.SLOT_STATE_ID
                                    and isinstance(o2, list) and isinstance(o2[1], list) and int(o2[1][1]) == value
                                ):
                                    return True
                            for v in bb.get("inputs", {}).values():
                                if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                                    frontier.append(v[1])
                    cur = parent
                return False

            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.EXPLODE_TICK_PROCCODE
                    and gated(id_of[id(b)], director.SLOT_SELF_EXPLODE)
                ):
                    b["mutation"]["proccode"] = "noop"

        cases = [
            ("zakato-lifecycle-procs-warp", unwarp_update),
            ("spawn-inits-zakato", drop_init_call),
            ("dispatch-updates-zakato", drop_dispatch_call),
            ("zakato-spawns-teleporting", spawn_active),
            ("zakato-per-variant-points", wrong_points),
            ("zakato-no-fire-mask", capture_fire_mask),
            ("zakato-teleport-commits-active", ungate_commit),
            ("zakato-commit-sets-motion", flatten_straight_dx),
            ("zakato-seeds-random-fuse", drop_fast_fuse_span),
            ("zakato-fires-once-then-self-destructs", ungate_fire_from_active),
            ("zakato-fires-without-gate", add_fire_gate),
            ("zakato-fire-trigger-proximity-band", break_proximity_band),
            ("zakato-self-destruct-no-score", self_explode_no_tick),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air07_failures(project), label)

    @staticmethod
    def _air10_failures(project: dict) -> set:
        """AIR-10 Spario authoring contract — violated labels. Pins the two Spario families as distinct
        faithful flyers:

        GIDDO SPARIO (handle_08 5219-5257): an aim-ONCE straight flyby. One warp init + one warp update + a
        distinct warp burst tick. The spawner inits it by type and the ordered walk drives its updater. It
        spawns SLOT_ACTIVE (hittable at once — unlike the Zakato's indestructible teleport), aimed once on the
        64-MAGNITUDE tier (4 px/frame, angle_dX_dY_sheonite_tbl 5223) — NOT the 32/48 tiers — awards
        GIDDO_SPARIO_PTS (10), captures NO fire mask and seeds NO fire timer (Giddo never fires), and draws its
        entry column craft-INDEPENDENTLY (gen_random_Y_store_obj, no craft reject). It flies STRAIGHT (no per-tick velocity change). On a shot-kill it plays
        its OWN SHORT burst `explode giddo spario tick` (freed at GIDDO_SPARIO_HIT_DURATION_FRAMES = 8, the one
        documented exception to the shared ~20-frame flying burst), NOT `explode toroid tick`, and it offers the
        shared detector on the non-HIT path (so a shot scores it).

        BRAG SPARIO (handle_09 3080-3121): an accelerating homer. One warp init + one warp update. It is NEVER
        spawned from a formation wave (`spawn flying enemies` never inits it — the Garu Zakato detonation,
        air.special-pairs #82, is the only spawner); the ordered walk still drives its updater. Each active tick
        it nudges its velocity toward the craft by +/-BRAG_SPARIO_ACCEL on BOTH axes (scroll `slot dx`, lateral
        `slot dy`), awards BRAG_SPARIO_PTS (500), plays the SHARED ~20-frame burst on a shot-kill, and offers the
        shared detector on the non-HIT path.

        The families run whole ticks under the settling harness (which cannot see a single mid-flight frame), so
        these once-only structural facts are pinned here."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def calls_in(body, proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in body
            )

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def rref(inp):
            r = ref(inp)
            return blocks.get(r) if r else None

        def const_item(b):
            return _num_operand(b["inputs"].get("ITEM"))

        id_of = {id(b): bid for bid, b in blocks.items()}

        def cond_has_num(cond_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                for key, v in b.get("inputs", {}).items():
                    if _num_operand(v) == value:
                        return True
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def cond_has_eq(cond_id, list_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "operator_equals":
                    lhs = rref(b["inputs"].get("OPERAND1"))
                    if (
                        lhs is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == list_id
                        and _num_operand(b["inputs"].get("OPERAND2")) == value
                    ):
                        return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        def gated_by_state(node_id, value):
            return ancestor_if(node_id, lambda c: cond_has_eq(c, director.SLOT_STATE_ID, value))

        # A `slot <axis>` write whose ITEM adds or subtracts `delta` (an accel nudge). Used to detect the
        # Brag's homing acceleration and, by its ABSENCE, the Giddo's straight flight.
        def accel_writes(body, axis_id, delta):
            found = 0
            for b in body:
                if b["opcode"] != "data_replaceitemoflist" or b["fields"]["LIST"][1] != axis_id:
                    continue
                item = rref(b["inputs"].get("ITEM"))
                if item is None or item["opcode"] not in ("operator_add", "operator_subtract"):
                    continue
                for key in ("NUM1", "NUM2"):
                    if _num_operand(item["inputs"].get(key)) == delta:
                        found += 1
            return found

        def reads_list(body, out_list_id, src_list_id):
            # a `data_replaceitemoflist` on out_list_id whose ITEM subtree reads data_itemoflist of src_list_id
            for b in body:
                if b["opcode"] != "data_replaceitemoflist" or b["fields"]["LIST"][1] != out_list_id:
                    continue
                item = rref(b["inputs"].get("ITEM"))
                if item is not None and item["opcode"] == "data_itemoflist" and item["fields"]["LIST"][1] == src_list_id:
                    return True
            return False

        giddo_init = _proc_body_blocks(stage, director.INIT_GIDDO_SPARIO_PROCCODE)
        giddo_update = _proc_body_blocks(stage, director.UPDATE_GIDDO_SPARIO_PROCCODE)
        giddo_burst = _proc_body_blocks(stage, director.EXPLODE_GIDDO_SPARIO_PROCCODE)
        brag_init = _proc_body_blocks(stage, director.INIT_BRAG_SPARIO_PROCCODE)
        brag_update = _proc_body_blocks(stage, director.UPDATE_BRAG_SPARIO_PROCCODE)
        spawn_body = _proc_body_blocks(stage, director.SPAWN_FLYING_PROCCODE)

        # ---- Giddo Spario ----
        # (1) All three Giddo lifecycle procs exist and are warp (atomic) — a non-warp proc would yield
        # mid-slot, letting a half-flown / half-burst Giddo render or be hit.
        for proccode in (
            director.INIT_GIDDO_SPARIO_PROCCODE,
            director.UPDATE_GIDDO_SPARIO_PROCCODE,
            director.EXPLODE_GIDDO_SPARIO_PROCCODE,
        ):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("giddo-lifecycle-procs-warp")

        # (2) The spawner inits Giddo; (3) the ordered walk dispatches its updater.
        if not calls_in(spawn_body, director.INIT_GIDDO_SPARIO_PROCCODE):
            failures.add("spawn-inits-giddo")
        if not calls(director.UPDATE_GIDDO_SPARIO_PROCCODE):
            failures.add("dispatch-updates-giddo")

        # (4) Giddo spawns SLOT_ACTIVE (hittable at once — the distinctive contrast with the Zakato's
        # indestructible SLOT_TELEPORT spawn); it never stamps SLOT_TELEPORT.
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and const_item(b) == director.SLOT_ACTIVE
            for b in giddo_init
        ) or any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and const_item(b) == director.SLOT_TELEPORT
            for b in giddo_init
        ):
            failures.add("giddo-spawns-active")

        # (5) AIM-ONCE ON THE 64 TIER. The init sets `slot dx`/`slot dy` from the 64-magnitude aim tables
        # (4 px/frame) — NOT the 32-tier (generic bullet) or 48-tier (radiating). Both axes must read tier 64.
        if not (
            reads_list(giddo_init, director.SLOT_DX_ID, director.AIM_DX_64_ID)
            and reads_list(giddo_init, director.SLOT_DY_ID, director.AIM_DY_64_ID)
        ) or reads_list(giddo_init, director.SLOT_DX_ID, director.AIM_DX_32_ID):
            failures.add("giddo-aims-once-64-tier")

        # (6) Giddo awards GIDDO_SPARIO_PTS (value-table position 1 -> 10 pts).
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and const_item(b) == director.GIDDO_SPARIO_PTS
            for b in giddo_init
        ):
            failures.add("giddo-points")

        # (7) NO FIRE MASK / NO FIRE TIMER AT SPAWN — Giddo never fires.
        if any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] in (director.SLOT_FIRE_MASK_ID, director.SLOT_FIRE_TIMER_ID)
            for b in giddo_init
        ):
            failures.add("giddo-no-fire-mask")

        # (8) [removed] The init draws its entry column CRAFT-INDEPENDENTLY: handle_08_Giddo_Spario calls
        # gen_random_Y_store_obj (5222) directly — the in-range clamp with NO craft-proximity reject — so a
        # Giddo CAN appear on the craft's column. (The craft-excluding `gen_rnd_spriteY` at 5156 is a
        # DIFFERENT routine Giddo never calls.) There is therefore no craft-exclusion contract to pin here,
        # matching the faithful no-exclusion families (e.g. Kapi/_air05).

        # (9) Giddo flies STRAIGHT — the update makes NO per-tick velocity change (the distinctive contrast
        # with the Brag's homing acceleration): no `slot dx`/`slot dy` write adds or subtracts an accel step.
        if (
            accel_writes(giddo_update, director.SLOT_DX_ID, director.BRAG_SPARIO_ACCEL)
            or accel_writes(giddo_update, director.SLOT_DY_ID, director.BRAG_SPARIO_ACCEL)
        ):
            failures.add("giddo-flies-straight")

        # (10) OWN SHORT BURST. The update's HIT branch runs Giddo's OWN `explode giddo spario tick` (gated by
        # state == SLOT_HIT), NOT the shared `explode toroid tick`; and that burst tick frees the slot at
        # GIDDO_SPARIO_HIT_DURATION_FRAMES (8) — a `cull slot` call whose gate carries the 8-frame bound.
        own_burst_calls = [
            id_of[id(b)]
            for b in giddo_update
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.EXPLODE_GIDDO_SPARIO_PROCCODE
        ]
        shared_in_giddo = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.EXPLODE_TICK_PROCCODE
            for b in giddo_update
        )
        burst_free_gated = any(
            b["opcode"] == "control_if"
            and cond_has_num(ref(b["inputs"].get("CONDITION")), director.GIDDO_SPARIO_HIT_DURATION_FRAMES)
            for b in giddo_burst
        ) and calls_in(giddo_burst, director.CULL_SLOT_PROCCODE)
        if (
            not own_burst_calls
            or not any(gated_by_state(c, director.SLOT_HIT) for c in own_burst_calls)
            or shared_in_giddo
            or not burst_free_gated
        ):
            failures.add("giddo-own-short-burst")

        # (11) Giddo offers the shared detector on the non-HIT path (so a shot scores it).
        if not calls_in(giddo_update, director.CHECK_AIR_HIT_PROCCODE):
            failures.add("giddo-offers-detector")

        # ---- Brag Spario ----
        # (12) Both Brag lifecycle procs exist and are warp.
        for proccode in (director.INIT_BRAG_SPARIO_PROCCODE, director.UPDATE_BRAG_SPARIO_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("brag-lifecycle-procs-warp")

        # (13) The ordered walk dispatches the Brag updater.
        if not calls(director.UPDATE_BRAG_SPARIO_PROCCODE):
            failures.add("dispatch-updates-brag")

        # (14) BRAG NEVER SPAWNS FROM A FORMATION WAVE. `spawn flying enemies` never inits a Brag — its only
        # spawner is the Garu Zakato detonation (air.special-pairs). The absence in the spawner is the
        # contract (a formation-spawned Brag would be a reachability regression).
        if calls_in(spawn_body, director.INIT_BRAG_SPARIO_PROCCODE):
            failures.add("brag-no-formation-spawn")

        # (15) BRAG ACCELERATES TOWARD THE CRAFT ON BOTH AXES. Each active tick nudges its velocity by
        # +/-BRAG_SPARIO_ACCEL on the scroll axis (`slot dx`) AND the lateral axis (`slot dy`) — one add and
        # one subtract per axis (the arcade's MSB-compare +2/0/-2, 3095-3115). All four nudges must be present.
        if not (
            accel_writes(brag_update, director.SLOT_DX_ID, director.BRAG_SPARIO_ACCEL) >= 2
            and accel_writes(brag_update, director.SLOT_DY_ID, director.BRAG_SPARIO_ACCEL) >= 2
        ):
            failures.add("brag-accelerates-both-axes")

        # (16) Brag awards BRAG_SPARIO_PTS (value-table position 12 -> 500 pts).
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and const_item(b) == director.BRAG_SPARIO_PTS
            for b in brag_init
        ):
            failures.add("brag-points")

        # (17) Brag plays the SHARED ~20-frame burst on a shot-kill (gated by state == SLOT_HIT), NOT Giddo's
        # own short burst.
        shared_calls = [
            id_of[id(b)]
            for b in brag_update
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.EXPLODE_TICK_PROCCODE
        ]
        own_in_brag = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.EXPLODE_GIDDO_SPARIO_PROCCODE
            for b in brag_update
        )
        if not shared_calls or not any(gated_by_state(c, director.SLOT_HIT) for c in shared_calls) or own_in_brag:
            failures.add("brag-shared-burst")

        # (18) Brag offers the shared detector on the non-HIT path (so a shot scores it).
        if not calls_in(brag_update, director.CHECK_AIR_HIT_PROCCODE):
            failures.add("brag-offers-detector")

        return failures

    # Roadmap closure evidence for leaf `air.spario` (AIR-10): the two Spario families are distinct live
    # flyers. The Giddo aims once on the 64-tier (4 px/frame), flies straight, never fires, scores 10, and
    # dies to its OWN short 8-frame burst; the Brag is an accelerating homer that nudges its velocity toward
    # the craft on both axes each tick, scores 500, dies to the shared ~20-frame burst, and NEVER spawns from
    # a formation wave (only from the Garu Zakato detonation, air.special-pairs). Both are dispatched by the
    # ordered walk and scored by the shared detector. The live proof is the harness `giddo-aims-once-64-tier`
    # / `giddo-own-short-burst` / `brag-homing-acceleration`.
    # roadmap-evidence: AIR-10 success  (test_spario_slice_authoring_present — Giddo/Brag lifecycle procs warp, spawn-inits Giddo + dispatch-updates both, Giddo spawns ACTIVE aimed on the 64 tier scoring 10 with no fire mask and a craft-independent draw flying straight and dying to its own 8-frame burst, Brag never formation-spawned, accelerates both axes scoring 500 and dying to the shared burst, both offer the detector)
    # roadmap-evidence: AIR-10 failure  (test_spario_slice_negative_fixtures — each contract clause corrupted bites)
    def test_spario_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air10_failures(project))

    def test_spario_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air10_failures(base))

        def _body(p, proccode):
            stage = next(t for t in p["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, proccode)

        def unwarp(proccode):
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in stage["blocks"].values():
                    if (
                        b["opcode"] == "procedures_prototype"
                        and b.get("mutation", {}).get("proccode") == proccode
                    ):
                        b["mutation"]["warp"] = "false"
            return _mut

        def drop_call_in(proccode_host, proccode_target):
            # Rename the FIRST call to proccode_target inside proccode_host's body → the host no longer calls it.
            def _mut(p: dict) -> None:
                stage, body = _body(p, proccode_host)
                for b in body:
                    if (
                        b["opcode"] == "procedures_call"
                        and b.get("mutation", {}).get("proccode") == proccode_target
                    ):
                        b["mutation"]["proccode"] = "noop"
                        return
            return _mut

        def spawn_giddo_teleporting(p: dict) -> None:
            # Flip the Giddo init's SLOT_ACTIVE stamp to SLOT_TELEPORT → it no longer spawns ACTIVE.
            stage, body = _body(p, director.INIT_GIDDO_SPARIO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and _const_item(b) == director.SLOT_ACTIVE
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.SLOT_TELEPORT)]]

        def giddo_aims_32(p: dict) -> None:
            # Repoint the Giddo init's `slot dx` aim read from the 64 tier to the 32 tier → wrong tier bites.
            stage, body = _body(p, director.INIT_GIDDO_SPARIO_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DX_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], str)
                ):
                    item = blocks.get(b["inputs"]["ITEM"][1])
                    if item is not None and item["opcode"] == "data_itemoflist" and item["fields"]["LIST"][1] == director.AIM_DX_64_ID:
                        item["fields"]["LIST"] = ["aim dx 32", director.AIM_DX_32_ID]

        def giddo_wrong_points(p: dict) -> None:
            stage, body = _body(p, director.INIT_GIDDO_SPARIO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                    and _const_item(b) == director.GIDDO_SPARIO_PTS
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.GIDDO_SPARIO_PTS + 5)]]

        def giddo_capture_fire_mask(p: dict) -> None:
            # Repurpose the Giddo init's `slot code` write to write `slot fire mask` → captures a fire mask.
            stage, body = _body(p, director.INIT_GIDDO_SPARIO_PROCCODE)
            for b in body:
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_CODE_ID:
                    b["fields"]["LIST"] = ["slot fire mask", director.SLOT_FIRE_MASK_ID]
                    break

        def giddo_add_acceleration(p: dict) -> None:
            # Inject a `slot dx` = slot dx + BRAG_SPARIO_ACCEL nudge into the Giddo update (it writes no
            # velocity today — it flies straight). Splice it onto the update definition's `next` so the proc
            # body reaches it. The flies-straight clause then bites.
            stage = next(t for t in p["targets"] if t["isStage"])
            blocks = stage["blocks"]
            proto_id = next(
                bid for bid, b in blocks.items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.UPDATE_GIDDO_SPARIO_PROCCODE
            )
            definition = next(
                b for b in blocks.values()
                if b["opcode"] == "procedures_definition"
                and b.get("inputs", {}).get("custom_block", [None, None])[1] == proto_id
            )
            add_id, write_id = "giddo_accel_add", "giddo_accel_write"
            blocks[add_id] = {
                "opcode": "operator_add", "next": None, "parent": write_id,
                "inputs": {"NUM1": [1, [4, "0"]], "NUM2": [1, [4, str(director.BRAG_SPARIO_ACCEL)]]},
                "fields": {}, "shadow": False, "topLevel": False,
            }
            blocks[write_id] = {
                "opcode": "data_replaceitemoflist", "next": definition.get("next"), "parent": None,
                "inputs": {"INDEX": [1, [7, "1"]], "ITEM": [3, add_id, [10, ""]]},
                "fields": {"LIST": ["slot dx", director.SLOT_DX_ID]},
                "shadow": False, "topLevel": False,
            }
            definition["next"] = write_id

        def giddo_share_burst(p: dict) -> None:
            # Flip the Giddo HIT branch's own burst call to the shared tick → the own-short-burst clause bites.
            stage, body = _body(p, director.UPDATE_GIDDO_SPARIO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.EXPLODE_GIDDO_SPARIO_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.EXPLODE_TICK_PROCCODE

        def brag_formation_spawn(p: dict) -> None:
            # Add an INIT_BRAG call into `spawn flying enemies` → Brag now spawns from a formation wave.
            stage, body = _body(p, director.SPAWN_FLYING_PROCCODE)
            blocks = stage["blocks"]
            # graft a call onto the first Giddo spawn-init call's `next`
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.INIT_GIDDO_SPARIO_PROCCODE
                ):
                    call_id = "brag_formation_call"
                    blocks[call_id] = {
                        "opcode": "procedures_call",
                        "next": b.get("next"),
                        "parent": id_of_host(blocks, b),
                        "inputs": {},
                        "fields": {},
                        "shadow": False,
                        "topLevel": False,
                        "mutation": {
                            "tagName": "mutation",
                            "children": [],
                            "proccode": director.INIT_BRAG_SPARIO_PROCCODE,
                            "argumentids": "[]",
                            "warp": "true",
                        },
                    }
                    b["next"] = call_id
                    return

        def id_of_host(blocks, target_block):
            for bid, bb in blocks.items():
                if bb is target_block:
                    return bb.get("parent")
            return None

        def brag_flatten_accel(p: dict) -> None:
            # Zero the Brag update's `slot dy` accel step → fewer than two lateral nudges remain.
            stage, body = _body(p, director.UPDATE_BRAG_SPARIO_PROCCODE)
            blocks = stage["blocks"]
            for b in body:
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_DY_ID:
                    item = blocks.get(b["inputs"].get("ITEM", [None, None])[1]) if isinstance(b["inputs"].get("ITEM"), list) else None
                    if item is not None and item["opcode"] in ("operator_add", "operator_subtract"):
                        for key in ("NUM1", "NUM2"):
                            if _num_operand(item["inputs"].get(key)) == director.BRAG_SPARIO_ACCEL:
                                item["inputs"][key] = [1, [4, "0"]]

        def brag_wrong_points(p: dict) -> None:
            stage, body = _body(p, director.INIT_BRAG_SPARIO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                    and _const_item(b) == director.BRAG_SPARIO_PTS
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.BRAG_SPARIO_PTS + 1)]]

        def brag_own_burst(p: dict) -> None:
            # Flip the Brag HIT branch's shared tick to Giddo's own burst → the shared-burst clause bites.
            stage, body = _body(p, director.UPDATE_BRAG_SPARIO_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.EXPLODE_TICK_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.EXPLODE_GIDDO_SPARIO_PROCCODE
                    return

        cases = [
            ("giddo-lifecycle-procs-warp", unwarp(director.UPDATE_GIDDO_SPARIO_PROCCODE)),
            ("spawn-inits-giddo", drop_call_in(director.SPAWN_FLYING_PROCCODE, director.INIT_GIDDO_SPARIO_PROCCODE)),
            ("dispatch-updates-giddo", drop_call_in(director.ADVANCE_SLOTS_PROCCODE, director.UPDATE_GIDDO_SPARIO_PROCCODE)),
            ("giddo-spawns-active", spawn_giddo_teleporting),
            ("giddo-aims-once-64-tier", giddo_aims_32),
            ("giddo-points", giddo_wrong_points),
            ("giddo-no-fire-mask", giddo_capture_fire_mask),
            ("giddo-flies-straight", giddo_add_acceleration),
            ("giddo-own-short-burst", giddo_share_burst),
            ("brag-lifecycle-procs-warp", unwarp(director.UPDATE_BRAG_SPARIO_PROCCODE)),
            ("dispatch-updates-brag", drop_call_in(director.ADVANCE_SLOTS_PROCCODE, director.UPDATE_BRAG_SPARIO_PROCCODE)),
            ("brag-no-formation-spawn", brag_formation_spawn),
            ("brag-accelerates-both-axes", brag_flatten_accel),
            ("brag-points", brag_wrong_points),
            ("brag-shared-burst", brag_own_burst),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air10_failures(project), label)

    @staticmethod
    def _air11_failures(project: dict) -> set:
        """AIR-11 Bacura authoring contract — violated labels. Pins the indestructible drifting slab as an
        occupant of its OWN reserved band (BACURA_SLOTS 17-32), architecturally distinct from every flying
        family:

        LIFECYCLE. One warp `init bacura` + one warp `update bacura`; the ordered walk dispatches the updater.
        The dispatch is keyed on BAND MEMBERSHIP (the slot cursor lies in 17..32), NOT on a `walk type == 1`
        equality — because SHOT_TYPE (=1) collides by value with BACURA_TYPE (=1) and a type-1 branch would run
        the Bacura handler over live shot slots. So the branch's gate must carry the band bounds 17 and 32.

        INIT. A stamped slab is SLOT_ACTIVE (arcade _STATE=2 active maps to the port's SLOT_ACTIVE, NOT port
        state 2 = SLOT_HIT), enters at the TOP row (slot x=0, since the arcade never sets _X), drifts at
        BACURA_DRIFT_DX with slot dy=0, and — the distinctive contract — stamps NO `slot pts` (a Bacura is
        never scored).

        UPDATE. The handler DELIBERATELY OMITS the `check air shot hit` call every flying family makes — that
        omission IS the shot-invulnerability: no shot ever hit-tests a Bacura, so there is no HIT state, no
        `explode toroid tick`, and no score. It only (1) kills the craft on contact through the WIDER
        HIT_WINDOW_BACURA (the `player hit` write is gated by an overlap reporter carrying the window's
        distinctive dy-high bound of 11 — unique to the Bacura box), and (2) drifts DOWN the scroll axis (the
        `slot x` write accumulates `slot dx`).

        RENDERER. The Bacura target carries the eight tumble frames `bacura/slab/01`..`08` (no death/explosion
        frame) and switches costume by a `(floor(slot x / 128)) mod 8` index — the port image of the arcade
        `(_X>>7)&7` — so the drawn slab tumbles as it drifts, rather than showing one fixed frame.

        The slab runs whole ticks under the settling harness, so these structural facts — above all the
        band-keyed dispatch and the omitted detector — are pinned here."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def calls_in(body, proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in body
            )

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def const_item(b):
            return _num_operand(b["inputs"].get("ITEM"))

        id_of = {id(b): bid for bid, b in blocks.items()}

        def cond_has_num(cond_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                for key, v in b.get("inputs", {}).items():
                    if _num_operand(v) == value:
                        return True
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        def writes_const(body, list_id, value):
            return any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == list_id
                and const_item(b) == value
                for b in body
            )

        def item_subtree_reads(write_block, src_list_id):
            # does the ITEM subtree of a `data_replaceitemoflist` read `data_itemoflist` of src_list_id
            # (however deeply nested — the drift `slot x = slot x + 4*slot dx` reads `slot dx` under a mul)?
            seen, frontier = set(), [ref(write_block["inputs"].get("ITEM"))]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] == src_list_id:
                    return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        bacura_init = _proc_body_blocks(stage, director.INIT_BACURA_PROCCODE)
        bacura_update = _proc_body_blocks(stage, director.UPDATE_BACURA_PROCCODE)
        adv_body = _proc_body_blocks(stage, director.ADVANCE_SLOTS_PROCCODE)

        # (1) Both Bacura lifecycle procs exist and are warp (atomic) — a non-warp proc would yield mid-slot,
        # letting a half-drifted slab render or be double-advanced.
        for proccode in (director.INIT_BACURA_PROCCODE, director.UPDATE_BACURA_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("bacura-lifecycle-procs-warp")

        # (2) The ordered walk dispatches the Bacura updater.
        if not calls(director.UPDATE_BACURA_PROCCODE):
            failures.add("dispatch-updates-bacura")

        # (3) DISPATCHED BY BAND, NOT BY TYPE. The `update bacura` call in the walk dispatch is gated by a
        # condition carrying the band bounds 17 and 32 (the slot cursor lies in BACURA_SLOTS). This is the
        # mandatory contrast with a `walk type == 1` equality: SHOT_TYPE (=1) collides with BACURA_TYPE (=1),
        # so a type-keyed branch would run this handler over live shot slots. The band gate is the invariant.
        bacura_calls = [
            id_of[id(b)]
            for b in adv_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.UPDATE_BACURA_PROCCODE
        ]
        if not any(
            ancestor_if(
                c,
                lambda cond: cond_has_num(cond, director.BACURA_SLOTS[0])
                and cond_has_num(cond, director.BACURA_SLOTS[1]),
            )
            for c in bacura_calls
        ):
            failures.add("bacura-dispatched-by-band")

        # (4) INIT STAMPS THE SLAB. Type=BACURA_TYPE, state=SLOT_ACTIVE (NOT SLOT_TELEPORT / SLOT_HIT), enters
        # at the top row (slot x=0), drifts at BACURA_DRIFT_DX with slot dy=0.
        if not (
            writes_const(bacura_init, director.SLOT_TYPE_ID, director.BACURA_TYPE)
            and writes_const(bacura_init, director.SLOT_STATE_ID, director.SLOT_ACTIVE)
            and writes_const(bacura_init, director.SLOT_X_ID, director.TOROID_SPAWN_ROW * director.SLOT_UNITS_PER_CELL)
            and writes_const(bacura_init, director.SLOT_DX_ID, director.BACURA_DRIFT_DX)
            and writes_const(bacura_init, director.SLOT_DY_ID, 0)
        ):
            failures.add("bacura-init-stamps-slab")

        # (5) DRIFTS DOWN. The update's `slot x` write accumulates `slot dx` (its ITEM subtree reads slot dx).
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_X_ID
            and item_subtree_reads(b, director.SLOT_DX_ID)
            for b in bacura_update
        ):
            failures.add("bacura-drifts-down")

        # (6) NO SHOT DETECTOR — the invulnerability. The update must NOT call `check air shot hit`; that
        # omission is what makes a Bacura indestructible (no shot ever hit-tests it).
        if calls_in(bacura_update, director.CHECK_AIR_HIT_PROCCODE):
            failures.add("bacura-no-air-hit-detector")

        # (7) NO EXPLOSION. With no detector there is no HIT path: the update runs no `explode toroid tick`
        # and never stamps SLOT_HIT.
        if calls_in(bacura_update, director.EXPLODE_TICK_PROCCODE) or writes_const(
            bacura_update, director.SLOT_STATE_ID, director.SLOT_HIT
        ):
            failures.add("bacura-no-explosion")

        # (8) NEVER SCORED. The init stamps no `slot pts` (a Bacura yields no points — the shared detector,
        # which it is never offered to, is the only thing that reads slot pts).
        if any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            for b in bacura_init
        ):
            failures.add("bacura-never-scored")

        # (9) CRAFT-DEATH ON TOUCH through the WIDER window. The `player hit` write is gated by an overlap
        # reporter carrying HIT_WINDOW_BACURA's distinctive dy-high bound (40-28-1 = 11), unique to the Bacura
        # box among the hit windows — so the death is routed through the wider slab collision, not the flyer box.
        y_bias, y_width, _x_bias, _x_width = director.HIT_WINDOW_BACURA
        bacura_dy_high = y_width - y_bias - 1  # 11
        hit_writes = [
            id_of[id(b)]
            for b in bacura_update
            if b["opcode"] == "data_setvariableto"
            and b.get("fields", {}).get("VARIABLE", [None, None])[1] == director.PLAYER_HIT_ID
            and _num_operand(b["inputs"].get("VALUE")) == 1
        ]
        if not hit_writes or not any(
            ancestor_if(h, lambda c: cond_has_num(c, bacura_dy_high)) for h in hit_writes
        ):
            failures.add("bacura-craft-death-window")

        # (10) RENDERER — the position-driven 8-frame tumble, no death frame. The Bacura target carries exactly
        # the eight tumble frames bacura/slab/01..08, and the render switches costume by a
        # (floor(slot x / 128)) mod 8 index (the port image of the arcade (_X>>7)&7) rather than a fixed frame.
        bacura_target = next(
            (t for t in project["targets"] if t.get("name") == director.BACURA_TARGET), None
        )
        names = [c.get("name") for c in bacura_target["costumes"]] if bacura_target else []
        expected_frames = [f"bacura/slab/0{i}" for i in range(1, director.BACURA_TUMBLE_FRAMES + 1)]
        if names != expected_frames:
            failures.add("bacura-renderer-tumble-frames")

        bacura_blocks = bacura_target["blocks"] if bacura_target else {}
        # A position-driven switch: a `looks_switchcostumeto` whose COSTUME input OBSCURES its shadow with a
        # reporter ([3, reporter, shadow]) — the runtime computes the name — not a static menu ([1, menu]).
        dynamic_switch = any(
            b.get("opcode") == "looks_switchcostumeto"
            and isinstance(b.get("inputs", {}).get("COSTUME"), list)
            and b["inputs"]["COSTUME"][0] == 3
            for b in bacura_blocks.values()
        )
        # The tumble mapping itself: mod by BACURA_TUMBLE_FRAMES (8) over a divide by
        # BACURA_TUMBLE_UNITS_PER_FRAME (128). Pinning both constants ties the render to the arcade cadence.
        mod_by_frames = any(
            b.get("opcode") == "operator_mod"
            and _num_operand(b.get("inputs", {}).get("NUM2")) == director.BACURA_TUMBLE_FRAMES
            for b in bacura_blocks.values()
        )
        div_by_units = any(
            b.get("opcode") == "operator_divide"
            and _num_operand(b.get("inputs", {}).get("NUM2")) == director.BACURA_TUMBLE_UNITS_PER_FRAME
            for b in bacura_blocks.values()
        )
        if not (dynamic_switch and mod_by_frames and div_by_units):
            failures.add("bacura-renderer-position-select")

        # (11) SCHEDULE DISPATCH DRIVES THE PUMP. The area scheduler is the ONLY seam that turns a real area's
        # schedule into live Bacura: set_bacura_count (arcade op 0x22, sub_2_fn_6__set_bacura_inc_cnt $075D)
        # must load the increment quota into `bacura inc cnt`, and reset_bacura_count (op 0x23,
        # sub_2_fn_7__reset_num_bacura $05D8) must clear `num bacura`. Everything above passes on
        # writeVar-primed counters, so without this a wrong handler string or a write to the wrong variable
        # would leave every area Bacura-less with nothing to catch it. Pin the handler->counter wiring here.
        def text_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] == 10
            ):
                return inp[1][1]
            return None

        adv_area = _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE)

        def branch_sets(handler_text, var_id):
            # An `operator_equals(handler_at_cursor(), <handler_text>)` used as some control_if CONDITION whose
            # SUBSTACK writes var_id via data_setvariableto (the schedule dispatch's `set_var_expr`).
            for b in adv_area:
                if b["opcode"] != "operator_equals":
                    continue
                if not any(text_operand(b["inputs"].get(k)) == handler_text for k in ("OPERAND1", "OPERAND2")):
                    continue
                eq_id = id_of[id(b)]
                for c in adv_area:
                    if c["opcode"] not in ("control_if", "control_if_else"):
                        continue
                    if ref(c["inputs"].get("CONDITION")) != eq_id:
                        continue
                    seen2, frontier2 = set(), [ref(c["inputs"].get("SUBSTACK"))]
                    while frontier2:
                        sid = frontier2.pop()
                        if not sid or sid in seen2 or sid not in blocks:
                            continue
                        seen2.add(sid)
                        sb = blocks[sid]
                        if (
                            sb["opcode"] == "data_setvariableto"
                            and sb.get("fields", {}).get("VARIABLE", [None, None])[1] == var_id
                        ):
                            return True
                        frontier2.append(sb.get("next"))
            return False

        if not branch_sets(director.SET_BACURA_COUNT_HANDLER, director.BACURA_INC_CNT_ID):
            failures.add("bacura-schedule-sets-inc-cnt")
        if not branch_sets(director.RESET_BACURA_COUNT_HANDLER, director.NUM_BACURA_ID):
            failures.add("bacura-schedule-resets-count")

        return failures

    # Roadmap closure evidence for leaf `air.bacura` (AIR-11): the indestructible drifting slab lives in its own
    # reserved band (17-32), dispatched by BAND membership — not by a `walk type == 1` equality that would
    # collide with SHOT_TYPE — and its updater DELIBERATELY OMITS the shared shot detector, which is the whole
    # of its shot-invulnerability (no HIT state, no explosion, no score). It stamps an ACTIVE slab that enters
    # at the top and drifts down, kills the craft on contact through the wider HIT_WINDOW_BACURA, and renders a
    # single slab costume. The live proof is the harness Bacura drift/invulnerability/craft-touch scenarios.
    # roadmap-evidence: AIR-11 success  (test_bacura_slice_authoring_present — init/update procs warp, dispatch-updates-bacura by band bounds 17/32, init stamps an ACTIVE slab entering at the top and drifting at BACURA_DRIFT_DX with no points, the update omits the shot detector and runs no explosion, craft-death routes through the wider window's distinctive dy-high bound, the renderer draws one slab costume, and the area scheduler wires set_bacura_count -> `bacura inc cnt` / reset_bacura_count -> `num bacura` so a real area's records drive the pump)
    # roadmap-evidence: AIR-11 failure  (test_bacura_slice_negative_fixtures — each contract clause corrupted bites)
    def test_bacura_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air11_failures(project))

    def test_bacura_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air11_failures(base))

        def _body(p, proccode):
            stage = next(t for t in p["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, proccode)

        def unwarp(proccode):
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in stage["blocks"].values():
                    if (
                        b["opcode"] == "procedures_prototype"
                        and b.get("mutation", {}).get("proccode") == proccode
                    ):
                        b["mutation"]["warp"] = "false"
            return _mut

        def drop_call_in(proccode_host, proccode_target):
            # Rename the FIRST call to proccode_target inside proccode_host's body → the host no longer calls it.
            def _mut(p: dict) -> None:
                stage, body = _body(p, proccode_host)
                for b in body:
                    if (
                        b["opcode"] == "procedures_call"
                        and b.get("mutation", {}).get("proccode") == proccode_target
                    ):
                        b["mutation"]["proccode"] = "noop"
                        return
            return _mut

        def graft_call(host_proccode, target_proccode):
            # Splice a warp call to target_proccode onto host_proccode's definition `next` (first body block).
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                blocks = stage["blocks"]
                proto_id = next(
                    bid for bid, b in blocks.items()
                    if b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == host_proccode
                )
                definition = next(
                    b for b in blocks.values()
                    if b["opcode"] == "procedures_definition"
                    and b.get("inputs", {}).get("custom_block", [None, None])[1] == proto_id
                )
                call_id = f"graft_{target_proccode.replace(' ', '_')}"
                blocks[call_id] = {
                    "opcode": "procedures_call", "next": definition.get("next"), "parent": None,
                    "inputs": {}, "fields": {}, "shadow": False, "topLevel": False,
                    "mutation": {
                        "tagName": "mutation", "children": [], "proccode": target_proccode,
                        "argumentids": "[]", "warp": "true",
                    },
                }
                definition["next"] = call_id
            return _mut

        def unband(p: dict) -> None:
            # Keep the `update bacura` dispatch call but strip the lower band bound (17) from the enclosing
            # gate's condition → the branch is no longer band-keyed. dispatch-updates-bacura still passes.
            stage, body = _body(p, director.ADVANCE_SLOTS_PROCCODE)
            blocks = stage["blocks"]

            def reaches_bacura_call(start_id):
                seen, frontier = set(), [start_id]
                while frontier:
                    cid = frontier.pop()
                    if not cid or cid in seen or cid not in blocks:
                        continue
                    seen.add(cid)
                    b = blocks[cid]
                    if (
                        b["opcode"] == "procedures_call"
                        and b.get("mutation", {}).get("proccode") == director.UPDATE_BACURA_PROCCODE
                    ):
                        return True
                    frontier.append(b.get("next"))
                    for v in b.get("inputs", {}).values():
                        if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                            frontier.append(v[1])
                return False

            def ref(inp):
                return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

            # Strip 17 from the CONDITION of EVERY if that reaches the bacura call. `_proc_body_blocks`
            # yields blocks in set-iteration order (not structural order), so "the first reaching if" is
            # not stable across a regen; the outer `occupied` gate reaches the call too but carries no 17,
            # so replacing 17 across all reaching ifs is a no-op there and bites only the band gate — the
            # one whose condition holds BACURA_SLOTS[0], regardless of iteration order.
            for b in body:
                if b["opcode"] != "control_if":
                    continue
                sub_id = ref(b["inputs"].get("SUBSTACK"))
                if not (sub_id and reaches_bacura_call(sub_id)):
                    continue
                seen, frontier = set(), [ref(b["inputs"].get("CONDITION"))]
                while frontier:
                    cid = frontier.pop()
                    if not cid or cid in seen or cid not in blocks:
                        continue
                    seen.add(cid)
                    bb = blocks[cid]
                    for key, v in list(bb.get("inputs", {}).items()):
                        if _num_operand(v) == director.BACURA_SLOTS[0]:
                            bb["inputs"][key] = [1, [4, "0"]]
                        elif isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                            frontier.append(v[1])

        def break_init_dx(p: dict) -> None:
            # Corrupt the init's `slot dx` stamp off BACURA_DRIFT_DX → the slab no longer drifts at spawn speed.
            stage, body = _body(p, director.INIT_BACURA_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DX_ID
                    and _const_item(b) == director.BACURA_DRIFT_DX
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.BACURA_DRIFT_DX + 1)]]

        def break_drift(p: dict) -> None:
            # Repoint every `slot dx` read in the update to `slot dy` → the `slot x` move no longer reads dx.
            stage, body = _body(p, director.UPDATE_BACURA_PROCCODE)
            for b in body:
                if b["opcode"] == "data_itemoflist" and b["fields"]["LIST"][1] == director.SLOT_DX_ID:
                    b["fields"]["LIST"] = ["slot dy", director.SLOT_DY_ID]

        def add_pts(p: dict) -> None:
            # Repurpose the init's `slot timer` stamp to write `slot pts` → the slab is now scored.
            stage, body = _body(p, director.INIT_BACURA_PROCCODE)
            for b in body:
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID:
                    b["fields"]["LIST"] = ["slot pts", director.SLOT_PTS_ID]
                    return

        def break_window(p: dict) -> None:
            # Zero the distinctive dy-high bound (11) wherever it appears in the update → the craft-death gate
            # no longer carries the Bacura window.
            stage, body = _body(p, director.UPDATE_BACURA_PROCCODE)
            for b in body:
                for key, v in list(b.get("inputs", {}).items()):
                    if _num_operand(v) == 11:
                        b["inputs"][key] = [1, [4, "0"]]

        def strip_tumble_frames(p: dict) -> None:
            # Drop all but the first tumble frame → the Bacura target no longer carries the eight frames the
            # position select needs (a regression to a single static costume).
            target = next(t for t in p["targets"] if t.get("name") == director.BACURA_TARGET)
            target["costumes"] = target["costumes"][:1]

        def pin_render_frame(p: dict) -> None:
            # Collapse the tumble index to a constant (mod BACURA_TUMBLE_FRAMES → mod 1, always 0) → the slab
            # draws one fixed frame instead of tumbling with its position.
            target = next(t for t in p["targets"] if t.get("name") == director.BACURA_TARGET)
            for b in target["blocks"].values():
                if (
                    b.get("opcode") == "operator_mod"
                    and _num_operand(b.get("inputs", {}).get("NUM2")) == director.BACURA_TUMBLE_FRAMES
                ):
                    b["inputs"]["NUM2"] = [1, [4, "1"]]
                    return

        def rebrand_handler(handler_text):
            # Rename the schedule handler literal the dispatch compares against → the branch never matches its
            # record, so the schedule can no longer drive that counter (a wrong-handler-string regression).
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in stage["blocks"].values():
                    if b["opcode"] != "operator_equals":
                        continue
                    for k in ("OPERAND1", "OPERAND2"):
                        v = b["inputs"].get(k)
                        if (
                            isinstance(v, list)
                            and len(v) >= 2
                            and isinstance(v[1], list)
                            and len(v[1]) >= 2
                            and v[1][0] == 10
                            and v[1][1] == handler_text
                        ):
                            v[1][1] = "noop_" + handler_text
            return _mut

        cases = [
            ("bacura-lifecycle-procs-warp", unwarp(director.UPDATE_BACURA_PROCCODE)),
            ("dispatch-updates-bacura", drop_call_in(director.ADVANCE_SLOTS_PROCCODE, director.UPDATE_BACURA_PROCCODE)),
            ("bacura-dispatched-by-band", unband),
            ("bacura-init-stamps-slab", break_init_dx),
            ("bacura-drifts-down", break_drift),
            ("bacura-no-air-hit-detector", graft_call(director.UPDATE_BACURA_PROCCODE, director.CHECK_AIR_HIT_PROCCODE)),
            ("bacura-no-explosion", graft_call(director.UPDATE_BACURA_PROCCODE, director.EXPLODE_TICK_PROCCODE)),
            ("bacura-never-scored", add_pts),
            ("bacura-craft-death-window", break_window),
            ("bacura-renderer-tumble-frames", strip_tumble_frames),
            ("bacura-renderer-position-select", pin_render_frame),
            ("bacura-schedule-sets-inc-cnt", rebrand_handler(director.SET_BACURA_COUNT_HANDLER)),
            ("bacura-schedule-resets-count", rebrand_handler(director.RESET_BACURA_COUNT_HANDLER)),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air11_failures(project), label)

    @staticmethod
    def _air09_failures(project: dict) -> set:
        """AIR-09 Sheonite authoring contract — violated labels. Pins the indestructible escort PAIR
        (right 0x31 / left 0x32) as an occupant of the SHARED flying pool (FLYING_SLOTS 59-64), dispatched
        by `walk type` like every flyer, but WHOLLY INERT — the corrected no-collision-of-any-kind contract.

        LIFECYCLE. One warp `update sheonite` shared by both halves; the ordered walk dispatches it under a
        SINGLE OR over BOTH types (right retreats / left vanishes, but the machine is one). The pair is
        stamped by the area scheduler's `sheonite_start` branch into the two fixed flying slots (0x31 ->
        SHEONITE_RIGHT_SLOT, 0x32 -> SHEONITE_LEFT_SLOT) with the end-flag cleared; `sheonite_end` raises the
        end-flag, releasing the pair from LOCK into the dock/peel-off.

        INERTNESS (the distinctive contract, and the guardrail-relevant invariant B1). Both arcade handlers
        set _STATE=3 (indestructible), which every hit test skips (STATE==2 gate) — the shot/score test AND
        the craft-collision test. So the faithful port has NO collision interaction of ANY kind: `update
        sheonite` DELIBERATELY OMITS the `check air shot hit` call (no shot ever hit-tests it -> no HIT, no
        `explode toroid tick`, no SLOT_HIT), stamps NO `slot pts` and calls no `resolve hit`/`score` (never
        scored), and writes NO `player hit` and drives no craft-overlap reporter (NO craft-death — the key
        contrast with the Bacura, which IS craft-tested because it runs at STATE=2). Omission IS the inertness.

        STATE MACHINE (phase in `slot flag`, snapshotted at the tick top). HOME aims on the shared 64-tier and
        transitions to LOCK; LOCK holds beside the live craft until the end-flag is raised, then -> COMBINE;
        COMBINE docks for SHEONITE_COMBINE_DWELL_FRAMES, then the right half sets SHEONITE_RETREAT_DX and ->
        RETREAT while the left half culls; RETREAT drifts up the scroll axis and culls off the top.

        RENDERER. The Sheonite target carries the ten costumes spin/01..04 + combine/01..06 (no
        death/explosion frame — the pair is inert), shown when its slot holds EITHER type (an OR gate), with a
        position/phase-driven dynamic costume switch."""
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

        def calls(proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in blocks.values()
            )

        def calls_in(body, proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in body
            )

        def ref(inp):
            return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

        id_of = {id(b): bid for bid, b in blocks.items()}

        def cond_has_num(cond_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                for key, v in b.get("inputs", {}).items():
                    if _num_operand(v) == value:
                        return True
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        def writes_at(body, list_id, index_val, item_val):
            # a `data_replaceitemoflist` on list_id whose INDEX literal == index_val and ITEM literal ==
            # item_val (the pair stamp writes each field at a FIXED slot number, unlike the per-slot walk).
            return any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == list_id
                and _num_operand(b["inputs"].get("INDEX")) == index_val
                and _num_operand(b["inputs"].get("ITEM")) == item_val
                for b in body
            )

        def writes_list(body, list_id):
            return any(
                b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == list_id
                for b in body
            )

        def text_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] == 10
            ):
                return inp[1][1]
            return None

        def branch_blocks(host_body, handler_text):
            # The blocks reachable from the SUBSTACK of the `if handler_at_cursor()==handler_text` branch in
            # host_body (the schedule dispatch's on/off flag branches).
            for b in host_body:
                if b["opcode"] != "operator_equals":
                    continue
                if not any(text_operand(b["inputs"].get(k)) == handler_text for k in ("OPERAND1", "OPERAND2")):
                    continue
                eq_id = id_of[id(b)]
                for c in host_body:
                    if c["opcode"] not in ("control_if", "control_if_else"):
                        continue
                    if ref(c["inputs"].get("CONDITION")) != eq_id:
                        continue
                    seen, frontier, out = set(), [ref(c["inputs"].get("SUBSTACK"))], []
                    while frontier:
                        sid = frontier.pop()
                        if not sid or sid in seen or sid not in blocks:
                            continue
                        seen.add(sid)
                        sb = blocks[sid]
                        out.append(sb)
                        frontier.append(sb.get("next"))
                        for v in sb.get("inputs", {}).values():
                            if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                                frontier.append(v[1])
                    return out
            return []

        def sets_var(body, var_id, value):
            return any(
                b["opcode"] == "data_setvariableto"
                and b.get("fields", {}).get("VARIABLE", [None, None])[1] == var_id
                and _num_operand(b["inputs"].get("VALUE")) == value
                for b in body
            )

        update = _proc_body_blocks(stage, director.UPDATE_SHEONITE_PROCCODE)
        adv_body = _proc_body_blocks(stage, director.ADVANCE_SLOTS_PROCCODE)
        adv_area = _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE)

        # (1) The shared updater exists and is warp — a non-warp proc would yield mid-tick, letting a
        # half-advanced half render or double-advance.
        p = proto(director.UPDATE_SHEONITE_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("sheonite-update-proc-warp")

        # (2) The ordered walk dispatches the updater.
        if not calls(director.UPDATE_SHEONITE_PROCCODE):
            failures.add("dispatch-updates-sheonite")

        # (3) DISPATCHED FOR BOTH TYPES BY ONE OR. The `update sheonite` call in the walk is gated by a
        # condition carrying BOTH RIGHT_SHEONITE_TYPE and LEFT_SHEONITE_TYPE (the single OR branch). Strip
        # either type and only one half of the pair would ever walk.
        sheo_calls = [
            id_of[id(b)]
            for b in adv_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.UPDATE_SHEONITE_PROCCODE
        ]
        if not any(
            ancestor_if(
                c,
                lambda cond: cond_has_num(cond, director.RIGHT_SHEONITE_TYPE)
                and cond_has_num(cond, director.LEFT_SHEONITE_TYPE),
            )
            for c in sheo_calls
        ):
            failures.add("sheonite-dispatched-both-types")

        # (4) NO SHOT DETECTOR — the shot-invulnerability. The update must NOT call `check air shot hit`.
        if calls_in(update, director.CHECK_AIR_HIT_PROCCODE):
            failures.add("sheonite-no-air-hit-detector")

        # (5) NO EXPLOSION. With no detector there is no HIT path: no `explode toroid tick`, never SLOT_HIT.
        if calls_in(update, director.EXPLODE_TICK_PROCCODE) or writes_list(update, director.SLOT_STATE_ID) and any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and _num_operand(b["inputs"].get("ITEM")) == director.SLOT_HIT
            for b in update
        ):
            failures.add("sheonite-no-explosion")

        # (6) NEVER SCORED. The update calls no `resolve hit`/`score`, and the `sheonite_start` stamp writes
        # no `slot pts` (the pair yields no points — it is never offered to the shared detector).
        start_body = branch_blocks(adv_area, director.SHEONITE_START_HANDLER)
        if (
            calls_in(update, director.RESOLVE_HIT_PROCCODE)
            or calls_in(update, director.SCORE_PROCCODE)
            or writes_list(start_body, director.SLOT_PTS_ID)
        ):
            failures.add("sheonite-never-scored")

        # (7) INERT — NO CRAFT-DEATH. The update writes NO `player hit` and drives no craft-overlap reporter.
        # This is the corrected contract's keystone: the Sheonite is craft-inert (STATE=3 skips the craft
        # collision test too), unlike the Bacura, which DOES kill on touch because it runs at STATE=2.
        if any(
            b["opcode"] == "data_setvariableto"
            and b.get("fields", {}).get("VARIABLE", [None, None])[1] == director.PLAYER_HIT_ID
            for b in update
        ):
            failures.add("sheonite-inert-no-craft-death")

        # (8) START STAMPS THE PAIR. `sheonite_start` stamps the right type at SHEONITE_RIGHT_SLOT and the
        # left type at SHEONITE_LEFT_SLOT, each SLOT_ACTIVE and phase HOME (arcade never SLOT_HIT — inert).
        if not (
            writes_at(start_body, director.SLOT_TYPE_ID, director.SHEONITE_RIGHT_SLOT, director.RIGHT_SHEONITE_TYPE)
            and writes_at(start_body, director.SLOT_TYPE_ID, director.SHEONITE_LEFT_SLOT, director.LEFT_SHEONITE_TYPE)
            and writes_at(start_body, director.SLOT_STATE_ID, director.SHEONITE_RIGHT_SLOT, director.SLOT_ACTIVE)
            and writes_at(start_body, director.SLOT_FLAG_ID, director.SHEONITE_RIGHT_SLOT, director.SHEONITE_PHASE_HOME)
        ):
            failures.add("sheonite-start-stamps-pair")

        # (9) START CLEARS THE END-FLAG (the pair holds in LOCK until sheonite_end).
        if not sets_var(start_body, director.SHEONITE_END_FLAG_ID, 0):
            failures.add("sheonite-start-clears-end-flag")

        # (10) END RAISES THE END-FLAG (releasing the pair from LOCK into dock/peel-off).
        end_body = branch_blocks(adv_area, director.SHEONITE_END_HANDLER)
        if not sets_var(end_body, director.SHEONITE_END_FLAG_ID, 1):
            failures.add("sheonite-end-raises-flag")

        # (11) HOME AIMS AND LOCKS. The update aims on the shared quantizer (`compute aim index`) and writes
        # phase LOCK — the HOME->LOCK transition.
        if not (
            calls_in(update, director.COMPUTE_AIM_PROCCODE)
            and any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
                and _num_operand(b["inputs"].get("ITEM")) == director.SHEONITE_PHASE_LOCK
                for b in update
            )
        ):
            failures.add("sheonite-home-aims-and-locks")

        # (12) LOCK WAITS ON THE END-FLAG. The update reads `sheonite end flag` and writes phase COMBINE — the
        # LOCK->COMBINE transition is gated by the end-flag, not immediate. Variable reads are INLINE operands
        # ([12, name, id]) in this generator, not standalone data_variable blocks, so scan the input trees.
        def reads_var(body, var_id):
            def walk(v):
                if isinstance(v, list):
                    if len(v) >= 3 and v[0] == 12 and v[2] == var_id:
                        return True
                    return any(walk(x) for x in v)
                return False
            return any(walk(v) for b in body for v in b.get("inputs", {}).values())

        reads_end_flag = reads_var(update, director.SHEONITE_END_FLAG_ID)
        writes_combine = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            and _num_operand(b["inputs"].get("ITEM")) == director.SHEONITE_PHASE_COMBINE
            for b in update
        )
        if not (reads_end_flag and writes_combine):
            failures.add("sheonite-lock-waits-end-flag")

        # (13) COMBINE EXIT — right retreats, left culls. After the dwell the update writes SHEONITE_RETREAT_DX
        # and phase RETREAT (right) and calls `cull slot` (left vanishes / right off-top).
        writes_retreat_dx = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_DX_ID
            and _num_operand(b["inputs"].get("ITEM")) == director.SHEONITE_RETREAT_DX
            for b in update
        )
        writes_retreat_phase = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_FLAG_ID
            and _num_operand(b["inputs"].get("ITEM")) == director.SHEONITE_PHASE_RETREAT
            for b in update
        )
        if not (writes_retreat_dx and writes_retreat_phase and calls_in(update, director.CULL_SLOT_PROCCODE)):
            failures.add("sheonite-combine-exit")

        # (14) RENDERER FRAMES — the ten inert escort costumes, no death frame.
        target = next((t for t in project["targets"] if t.get("name") == director.SHEONITE_TARGET), None)
        names = [c.get("name") for c in target["costumes"]] if target else []
        expected = (
            [f"sheonite/spin/0{i}" for i in range(1, director.SHEONITE_SPIN_FRAMES + 1)]
            + [f"sheonite/combine/0{i}" for i in range(1, 2 * director.SHEONITE_COMBINE_ANIM_FRAMES + 1)]
        )
        if names != expected:
            failures.add("sheonite-renderer-frames")

        tblocks = target["blocks"] if target else {}
        # (15) RENDERER SHOWS BOTH TYPES on ONE OR. An `operator_or` whose subtree references BOTH type codes.
        def or_covers_both():
            for b in tblocks.values():
                if b.get("opcode") != "operator_or":
                    continue
                seen, frontier, nums = set(), [id for id in (ref(b["inputs"].get("OPERAND1")), ref(b["inputs"].get("OPERAND2"))) if id], set()
                while frontier:
                    cid = frontier.pop()
                    if not cid or cid in seen or cid not in tblocks:
                        continue
                    seen.add(cid)
                    bb = tblocks[cid]
                    for v in bb.get("inputs", {}).values():
                        n = _num_operand(v)
                        if n is not None:
                            nums.add(n)
                        if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                            frontier.append(v[1])
                if director.RIGHT_SHEONITE_TYPE in nums and director.LEFT_SHEONITE_TYPE in nums:
                    return True
            return False

        if not or_covers_both():
            failures.add("sheonite-renderer-both-types")

        # (16) RENDERER IS POSITION/PHASE-DRIVEN. A `looks_switchcostumeto` whose COSTUME input OBSCURES its
        # shadow with a reporter ([3, reporter, shadow]) — the runtime computes the costume from the slot's
        # phase/clock — not a static menu; else the pair would show one fixed frame instead of animating.
        if not any(
            b.get("opcode") == "looks_switchcostumeto"
            and isinstance(b.get("inputs", {}).get("COSTUME"), list)
            and b["inputs"]["COSTUME"][0] == 3
            for b in tblocks.values()
        ):
            failures.add("sheonite-renderer-dynamic-costume")

        return failures

    # Roadmap closure evidence for leaf `air.sheonite` (AIR-09): the indestructible escort PAIR shares the
    # flying pool (59-64), dispatched by ONE OR over both types (right 0x31 / left 0x32), and is WHOLLY INERT —
    # the corrected no-collision contract. `update sheonite` omits the shot detector (no shot, no HIT, no
    # explosion), stamps no points and calls no resolve-hit/score (never scored), and writes no `player hit`
    # (NO craft-death — the keystone contrast with the STATE=2 Bacura). It runs the shared home->lock->combine
    # ->retreat/vanish machine; the area scheduler's sheonite_start stamps the pair + clears the end-flag and
    # sheonite_end raises it. The live proof is the harness Sheonite home/lock/dock/peel-off + inertness
    # scenarios (area 9 natural spawn, or the T-key).
    # roadmap-evidence: AIR-09 success  (test_sheonite_slice_authoring_present — the shared updater is warp and dispatched under one OR over both types; it omits the shot detector, runs no explosion and never SLOT_HIT, stamps no points and calls no resolve-hit/score, and writes NO player hit so the pair is craft-inert; sheonite_start stamps both fixed slots ACTIVE/HOME and clears the end-flag while sheonite_end raises it; the machine aims+locks, waits on the end-flag to combine, and on dwell-end the right retreats on SHEONITE_RETREAT_DX while the pair culls; the renderer carries the ten inert costumes shown on an OR over both types with a dynamic costume switch)
    # roadmap-evidence: AIR-09 failure  (test_sheonite_slice_negative_fixtures — each contract clause corrupted bites, above all the inertness clauses: grafting a shot detector, an explosion, a score path or a `player hit` write each trips its own label)
    def test_sheonite_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air09_failures(project))

    def test_sheonite_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air09_failures(base))

        def _body(p, proccode):
            stage = next(t for t in p["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, proccode)

        def unwarp(proccode):
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in stage["blocks"].values():
                    if (
                        b["opcode"] == "procedures_prototype"
                        and b.get("mutation", {}).get("proccode") == proccode
                    ):
                        b["mutation"]["warp"] = "false"
            return _mut

        def drop_call_in(host, target):
            def _mut(p: dict) -> None:
                stage, body = _body(p, host)
                for b in body:
                    if (
                        b["opcode"] == "procedures_call"
                        and b.get("mutation", {}).get("proccode") == target
                    ):
                        b["mutation"]["proccode"] = "noop"
                        return
            return _mut

        def graft_call(host_proccode, target_proccode):
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                blocks = stage["blocks"]
                proto_id = next(
                    bid for bid, b in blocks.items()
                    if b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == host_proccode
                )
                definition = next(
                    b for b in blocks.values()
                    if b["opcode"] == "procedures_definition"
                    and b.get("inputs", {}).get("custom_block", [None, None])[1] == proto_id
                )
                call_id = f"graft_{target_proccode.replace(' ', '_')}"
                blocks[call_id] = {
                    "opcode": "procedures_call", "next": definition.get("next"), "parent": None,
                    "inputs": {}, "fields": {}, "shadow": False, "topLevel": False,
                    "mutation": {
                        "tagName": "mutation", "children": [], "proccode": target_proccode,
                        "argumentids": "[]", "warp": "true",
                    },
                }
                definition["next"] = call_id
            return _mut

        def graft_player_hit(p: dict) -> None:
            # Splice a `set player hit = 1` onto the update's definition `next` → the pair now kills on touch,
            # violating the corrected craft-inert contract.
            stage = next(t for t in p["targets"] if t["isStage"])
            blocks = stage["blocks"]
            proto_id = next(
                bid for bid, b in blocks.items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.UPDATE_SHEONITE_PROCCODE
            )
            definition = next(
                b for b in blocks.values()
                if b["opcode"] == "procedures_definition"
                and b.get("inputs", {}).get("custom_block", [None, None])[1] == proto_id
            )
            blocks["graft_player_hit"] = {
                "opcode": "data_setvariableto", "next": definition.get("next"), "parent": None,
                "inputs": {"VALUE": [1, [4, "1"]]},
                "fields": {"VARIABLE": ["player hit", director.PLAYER_HIT_ID]},
                "shadow": False, "topLevel": False,
            }
            definition["next"] = "graft_player_hit"

        def add_pts_to_start(p: dict) -> None:
            # Repoint the right-slot `slot timer` stamp in sheonite_start to `slot pts` → the pair is scored.
            stage, body = _body(p, director.ADVANCE_AREA_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                    and _num_operand(b["inputs"].get("INDEX")) == director.SHEONITE_RIGHT_SLOT
                ):
                    b["fields"]["LIST"] = ["slot pts", director.SLOT_PTS_ID]
                    return

        def strip_left_from_dispatch(p: dict) -> None:
            # Replace LEFT_SHEONITE_TYPE with a bogus code wherever it appears in the walk dispatch → only the
            # right half dispatches. dispatch-updates-sheonite still passes (right still matches).
            stage, body = _body(p, director.ADVANCE_SLOTS_PROCCODE)
            for b in body:
                for key, v in list(b.get("inputs", {}).items()):
                    if _num_operand(v) == director.LEFT_SHEONITE_TYPE:
                        b["inputs"][key] = [1, [4, "999"]]

        def corrupt_start_type(p: dict) -> None:
            # Corrupt the right-slot type stamp in sheonite_start off RIGHT_SHEONITE_TYPE.
            stage, body = _body(p, director.ADVANCE_AREA_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TYPE_ID
                    and _num_operand(b["inputs"].get("INDEX")) == director.SHEONITE_RIGHT_SLOT
                    and _num_operand(b["inputs"].get("ITEM")) == director.RIGHT_SHEONITE_TYPE
                ):
                    b["inputs"]["ITEM"] = [1, [4, "999"]]
                    return

        def rebrand_handler(handler_text):
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t["isStage"])
                for b in stage["blocks"].values():
                    if b["opcode"] != "operator_equals":
                        continue
                    for k in ("OPERAND1", "OPERAND2"):
                        v = b["inputs"].get(k)
                        if (
                            isinstance(v, list)
                            and len(v) >= 2
                            and isinstance(v[1], list)
                            and len(v[1]) >= 2
                            and v[1][0] == 10
                            and v[1][1] == handler_text
                        ):
                            v[1][1] = "noop_" + handler_text
            return _mut

        def break_home_aim(p: dict) -> None:
            # Drop the `compute aim index` call from the update → HOME no longer aims.
            stage, body = _body(p, director.UPDATE_SHEONITE_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.COMPUTE_AIM_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"
                    return

        def break_lock_end_flag(p: dict) -> None:
            # Repoint every inline `sheonite end flag` READ ([12, name, id]) in the update to another var →
            # LOCK no longer waits on it.
            stage, body = _body(p, director.UPDATE_SHEONITE_PROCCODE)

            def repoint(v):
                if isinstance(v, list):
                    if len(v) >= 3 and v[0] == 12 and v[2] == director.SHEONITE_END_FLAG_ID:
                        v[1] = "sheonite phase"
                        v[2] = director.SHEONITE_PHASE_TMP_ID
                    else:
                        for x in v:
                            repoint(x)

            for b in body:
                for v in b.get("inputs", {}).values():
                    repoint(v)

        def break_retreat_dx(p: dict) -> None:
            # Corrupt the retreat `slot dx` write off SHEONITE_RETREAT_DX → the right half no longer retreats.
            stage, body = _body(p, director.UPDATE_SHEONITE_PROCCODE)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DX_ID
                    and _num_operand(b["inputs"].get("ITEM")) == director.SHEONITE_RETREAT_DX
                ):
                    b["inputs"]["ITEM"] = [1, [4, "0"]]
                    return

        def strip_render_frames(p: dict) -> None:
            target = next(t for t in p["targets"] if t.get("name") == director.SHEONITE_TARGET)
            target["costumes"] = target["costumes"][:1]

        def strip_left_from_render(p: dict) -> None:
            target = next(t for t in p["targets"] if t.get("name") == director.SHEONITE_TARGET)
            for b in target["blocks"].values():
                for key, v in list(b.get("inputs", {}).items()):
                    if _num_operand(v) == director.LEFT_SHEONITE_TYPE:
                        b["inputs"][key] = [1, [4, "999"]]

        def pin_render_costume(p: dict) -> None:
            # Replace every dynamic costume switch with a static menu → the pair shows one fixed frame.
            target = next(t for t in p["targets"] if t.get("name") == director.SHEONITE_TARGET)
            for b in target["blocks"].values():
                if (
                    b.get("opcode") == "looks_switchcostumeto"
                    and isinstance(b.get("inputs", {}).get("COSTUME"), list)
                    and b["inputs"]["COSTUME"][0] == 3
                ):
                    b["inputs"]["COSTUME"] = [1, "static_menu"]

        cases = [
            ("sheonite-update-proc-warp", unwarp(director.UPDATE_SHEONITE_PROCCODE)),
            ("dispatch-updates-sheonite", drop_call_in(director.ADVANCE_SLOTS_PROCCODE, director.UPDATE_SHEONITE_PROCCODE)),
            ("sheonite-dispatched-both-types", strip_left_from_dispatch),
            ("sheonite-no-air-hit-detector", graft_call(director.UPDATE_SHEONITE_PROCCODE, director.CHECK_AIR_HIT_PROCCODE)),
            ("sheonite-no-explosion", graft_call(director.UPDATE_SHEONITE_PROCCODE, director.EXPLODE_TICK_PROCCODE)),
            ("sheonite-never-scored", graft_call(director.UPDATE_SHEONITE_PROCCODE, director.RESOLVE_HIT_PROCCODE)),
            ("sheonite-never-scored", add_pts_to_start),
            ("sheonite-inert-no-craft-death", graft_player_hit),
            ("sheonite-start-stamps-pair", corrupt_start_type),
            ("sheonite-start-clears-end-flag", rebrand_handler(director.SHEONITE_START_HANDLER)),
            ("sheonite-end-raises-flag", rebrand_handler(director.SHEONITE_END_HANDLER)),
            ("sheonite-home-aims-and-locks", break_home_aim),
            ("sheonite-lock-waits-end-flag", break_lock_end_flag),
            ("sheonite-combine-exit", break_retreat_dx),
            ("sheonite-renderer-frames", strip_render_frames),
            ("sheonite-renderer-both-types", strip_left_from_render),
            ("sheonite-renderer-dynamic-costume", pin_render_costume),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air09_failures(project), label)

    @staticmethod
    def _audio_failures(project: dict) -> set:
        """AUDIO cross-cutting contract (docs/mechanics/040-arcade-sound-cues.md) — violated labels.

        Each of the six real arcade gameplay-SFX cues committed under assets/game-sounds/ must be
        actually PLAYED (a sound_play, whose only builder is Blocks.play_sound, emits a
        sound_sounds_menu naming the sound) at its verified play point. The five Stage-thread cues
        (air_destroy / ground_destroy / zakato-teleport / garu_zakato / sheonite) play from the Stage,
        whose walk/detector procs own those seams. The Bacura-bounce cue is special: the bounce runs on
        a blaster CLONE, which cannot play a Stage-owned sound directly, so it broadcasts `sfx bacura`
        and the Stage plays BACURA_HIT_SND on a matching receiver — the cue must NOT be played on the
        cloning blaster target."""
        failures = set()
        targets = {t.get("name"): t for t in project["targets"]}
        stage = next(t for t in project["targets"] if t.get("isStage"))

        def menu_names(target):
            return {
                b["fields"]["SOUND_MENU"][0]
                for b in target.get("blocks", {}).values()
                if b["opcode"] == "sound_sounds_menu" and "SOUND_MENU" in b.get("fields", {})
            }

        all_played = set()
        for t in project["targets"]:
            all_played |= menu_names(t)
        for name in ("air_destroy", "ground_destroy", "zakato", "garu_zakato", "sheonite", "bacura"):
            if name not in all_played:
                failures.add(f"cue-missing:{name}")

        stage_played = menu_names(stage)
        for name in ("air_destroy", "ground_destroy", "zakato", "garu_zakato", "sheonite"):
            if name not in stage_played:
                failures.add(f"stage-cue-missing:{name}")

        # Bacura routing: on the Stage via a `sfx bacura` receiver; the blaster broadcasts it and
        # never plays the Stage-owned sound on the clone.
        if "bacura" not in stage_played:
            failures.add("bacura-not-on-stage")

        def has_receive(target, message):
            return any(
                b["opcode"] == "event_whenbroadcastreceived"
                and b.get("fields", {}).get("BROADCAST_OPTION", [None])[0] == message
                for b in target.get("blocks", {}).values()
            )

        def has_broadcast(target, message):
            return any(
                b["opcode"] in ("event_broadcast", "event_broadcastandwait")
                and isinstance(b.get("inputs", {}).get("BROADCAST_INPUT"), list)
                and b["inputs"]["BROADCAST_INPUT"][1][1] == message
                for b in target.get("blocks", {}).values()
            )

        if not has_receive(stage, "sfx bacura"):
            failures.add("bacura-no-stage-receiver")
        blaster = targets.get("blaster")
        if blaster is None or not has_broadcast(blaster, "sfx bacura"):
            failures.add("bacura-no-blaster-broadcast")
        if blaster is not None and "bacura" in menu_names(blaster):
            failures.add("bacura-played-on-clone")
        return failures

    def test_audio_cues_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._audio_failures(project))

    def test_audio_cues_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._audio_failures(base))

        def drop_menu(name):
            # Rename every sound_sounds_menu naming `name` on the Stage (turns off that cue's play).
            def _mut(p: dict) -> None:
                stage = next(t for t in p["targets"] if t.get("isStage"))
                for b in stage["blocks"].values():
                    if (
                        b["opcode"] == "sound_sounds_menu"
                        and b.get("fields", {}).get("SOUND_MENU", [None])[0] == name
                    ):
                        b["fields"]["SOUND_MENU"][0] = "wrong"
            return _mut

        def drop_bacura_receiver(p: dict) -> None:
            stage = next(t for t in p["targets"] if t.get("isStage"))
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "event_whenbroadcastreceived"
                    and b.get("fields", {}).get("BROADCAST_OPTION", [None])[0] == "sfx bacura"
                ):
                    b["fields"]["BROADCAST_OPTION"][0] = "wrong"

        cases = [
            ("stage-cue-missing:air_destroy", drop_menu("air_destroy")),
            ("stage-cue-missing:ground_destroy", drop_menu("ground_destroy")),
            ("stage-cue-missing:zakato", drop_menu("zakato")),
            ("stage-cue-missing:garu_zakato", drop_menu("garu_zakato")),
            ("stage-cue-missing:sheonite", drop_menu("sheonite")),
            ("bacura-not-on-stage", drop_menu("bacura")),
            ("bacura-no-stage-receiver", drop_bacura_receiver),
        ]
        for label, mutate in cases:
            project = load_source(scratch.SOURCE_DIR)
            mutate(project)
            self.assertIn(label, self._audio_failures(project), label)

    @staticmethod
    def _wpn01_failures(project: dict) -> set:
        """WPN-01 player.bacura-bounce contract — violated labels. A player shot that overlaps a Bacura is
        REFLECTED, not consumed: the slab is indestructible and worthless, so the only consequence is the
        shot's own rebound.

        DETECTOR. `check shot bacura` is a warp proc, called per live slab from `update bacura` (a sibling of
        `check air shot hit`, never a reuse). On an overlapping ACTIVE shot it stamps ONLY that shot slot's
        state to SHOT_BOUNCE, through the doubled HIT_WINDOW_SHOT_BACURA overlap (recognised by its
        distinctive low bound -y_bias). It NEVER resolves a hit: no `resolve hit` call, no `hit slot` /
        `award value` write, and it never writes a Bacura field — the slab drifts on untouched.

        SHOT. The blaster clone reads SHOT_BOUNCE when its travel loop ends and, instead of vanishing at
        once, reverses (motion_changeyby BACURA_BOUNCE_DY, the negative step) and runs BACURA_BOUNCE_FRAMES
        costume frames before the shared free+delete. That reversal + visible travel is the whole of
        "bounces"."""
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

        def calls_in(body, proccode):
            return any(
                b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == proccode
                for b in body
            )

        def ref(inp):
            return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

        id_of = {id(b): bid for bid, b in blocks.items()}

        def cond_has_num(cond_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                for v in b.get("inputs", {}).values():
                    if _num_operand(v) == value:
                        return True
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        detector = _proc_body_blocks(stage, director.CHECK_SHOT_BACURA_PROCCODE)
        bacura_update = _proc_body_blocks(stage, director.UPDATE_BACURA_PROCCODE)

        # (1) The detector proc exists and is warp (atomic — a mid-sweep yield could let a shot render or be
        # re-tested between marks).
        p = proto(director.CHECK_SHOT_BACURA_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("bounce-detector-warp")

        # (2) `update bacura` runs the shot-bounce detector every tick — the Bacura's ONLY interaction with a
        # shot (it still omits `check air shot hit`, its shot-invulnerability).
        if not calls_in(bacura_update, director.CHECK_SHOT_BACURA_PROCCODE):
            failures.add("bacura-update-calls-bounce")

        # (3) MARKS THE SHOT. The detector writes slot state = SHOT_BOUNCE — the rebound signal the blaster
        # clone reads. (SHOT_BOUNCE is distinct from SHOT_SPENT so a bounce is told apart from an air-kill.)
        bounce_writes = [
            id_of[id(b)]
            for b in detector
            if b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and _const_item(b) == director.SHOT_BOUNCE
        ]
        if not bounce_writes:
            failures.add("bounce-marks-shot")

        # (4) THROUGH THE OVERLAP WINDOW. Each SHOT_BOUNCE write is gated by an overlap `if` carrying
        # HIT_WINDOW_SHOT_BACURA's distinctive low bound (-y_bias) — so the mark is a real overlap test with
        # the doubled slab window, not an unconditional stamp.
        y_bias = director.HIT_WINDOW_SHOT_BACURA[0]
        if not bounce_writes or not any(
            ancestor_if(w, lambda c: cond_has_num(c, -y_bias)) for w in bounce_writes
        ):
            failures.add("bounce-window")

        # (5) NEVER SCORES / NEVER TOUCHES THE SLAB. The Bacura is indestructible and worthless: the detector
        # makes no `resolve hit` call and writes neither `hit slot` nor `award value`. It writes only the shot
        # slot's own state (clause 3) — never a Bacura field.
        if calls_in(detector, director.RESOLVE_HIT_PROCCODE) or any(
            b["opcode"] == "data_setvariableto"
            and b.get("fields", {}).get("VARIABLE", [None, None])[1]
            in (director.HIT_SLOT_ID, director.AWARD_VALUE_ID)
            for b in detector
        ):
            failures.add("bounce-not-scored")

        # (6) THE SHOT REBOUNDS. In the blaster sprite a control_if gated on SHOT_BOUNCE runs the reversal: a
        # control_repeat of BACURA_BOUNCE_FRAMES whose body has a motion_changeyby of BACURA_BOUNCE_DY (the
        # negative, reversed step). A forward step or a missing branch is not a bounce.
        blaster = next((t for t in project["targets"] if t.get("name") == "blaster"), None)
        bb = blaster["blocks"] if blaster else {}

        def bref(inp):
            return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

        def subtree_has_num(root_id, value):
            seen, frontier = set(), [root_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in bb:
                    continue
                seen.add(cid)
                b = bb[cid]
                for v in b.get("inputs", {}).values():
                    if _num_operand(v) == value:
                        return True
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def stack_of(root_id):
            out, cur = [], root_id
            while cur and cur in bb:
                out.append(cur)
                cur = bb[cur].get("next")
            return out

        def substack_reverses(if_id):
            for sid in stack_of(bref(bb[if_id]["inputs"].get("SUBSTACK"))):
                b = bb[sid]
                if b["opcode"] != "control_repeat":
                    continue
                if _num_operand(b["inputs"].get("TIMES")) != director.BACURA_BOUNCE_FRAMES:
                    continue
                if any(
                    bb[i]["opcode"] == "motion_changeyby"
                    and _num_operand(bb[i]["inputs"].get("DY")) == director.BACURA_BOUNCE_DY
                    for i in stack_of(bref(b["inputs"].get("SUBSTACK")))
                ):
                    return True
            return False

        if not any(
            b["opcode"] == "control_if"
            and subtree_has_num(bref(b["inputs"].get("CONDITION")), director.SHOT_BOUNCE)
            and substack_reverses(bid)
            for bid, b in bb.items()
        ):
            failures.add("shot-reverses-and-animates")

        return failures

    # Roadmap closure evidence for leaf `player.bacura-bounce` (WPN-01): a player shot that overlaps a Bacura
    # is reflected, never consumed. The dedicated `check shot bacura` detector (a sibling of the air detector,
    # never a reuse) marks the overlapping shot SHOT_BOUNCE through the doubled HIT_WINDOW_SHOT_BACURA and
    # touches neither score nor slab; the blaster clone reads that mark and reverses (BACURA_BOUNCE_DY) for
    # BACURA_BOUNCE_FRAMES before deleting. The live proof is the harness shot-bounce scenarios (slab lives,
    # shot reverses).
    # roadmap-evidence: WPN-01 success  (test_bacura_bounce_authoring_present — the detector proc is warp and called from update bacura, marks a shot SHOT_BOUNCE through the doubled window, never resolves a hit or writes a Bacura field, and the blaster clone reverses at BACURA_BOUNCE_DY for BACURA_BOUNCE_FRAMES)
    # roadmap-evidence: WPN-01 failure  (test_bacura_bounce_negative_fixtures — each contract clause corrupted bites)
    def test_bacura_bounce_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._wpn01_failures(project))

    def test_bacura_bounce_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._wpn01_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def unwarp_detector(p: dict) -> None:
            for b in _stage(p)["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.CHECK_SHOT_BACURA_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_bounce_call(p: dict) -> None:
            stage = _stage(p)
            for b in _proc_body_blocks(stage, director.UPDATE_BACURA_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.CHECK_SHOT_BACURA_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"
                    return

        def break_mark(p: dict) -> None:
            # Repoint EVERY SHOT_BOUNCE write (one per shot slot) off `slot state` → no shot is ever marked
            # for the bounce. (Also trips bounce-window, which keys off the same writes; assertIn only needs
            # the target label.)
            stage = _stage(p)
            for b in _proc_body_blocks(stage, director.CHECK_SHOT_BACURA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and _const_item(b) == director.SHOT_BOUNCE
                ):
                    b["fields"]["LIST"] = ["slot timer", director.SLOT_TIMER_ID]

        def break_window(p: dict) -> None:
            # Zero the detector's distinctive low bound (-y_bias) → the mark is no longer an overlap test.
            stage = _stage(p)
            low = -director.HIT_WINDOW_SHOT_BACURA[0]
            for b in _proc_body_blocks(stage, director.CHECK_SHOT_BACURA_PROCCODE):
                for key, v in list(b.get("inputs", {}).items()):
                    if _num_operand(v) == low:
                        b["inputs"][key] = [1, [4, "0"]]

        def add_score(p: dict) -> None:
            # Graft a `resolve hit` call onto the detector's definition `next` → it now scores the slab.
            stage = _stage(p)
            blocks = stage["blocks"]
            proto_id = next(
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "procedures_prototype"
                and b.get("mutation", {}).get("proccode") == director.CHECK_SHOT_BACURA_PROCCODE
            )
            definition = next(
                b
                for b in blocks.values()
                if b["opcode"] == "procedures_definition"
                and b.get("inputs", {}).get("custom_block", [None, None])[1] == proto_id
            )
            blocks["graft_resolve_hit"] = {
                "opcode": "procedures_call", "next": definition.get("next"), "parent": None,
                "inputs": {}, "fields": {}, "shadow": False, "topLevel": False,
                "mutation": {
                    "tagName": "mutation", "children": [], "proccode": director.RESOLVE_HIT_PROCCODE,
                    "argumentids": "[]", "warp": "true",
                },
            }
            definition["next"] = "graft_resolve_hit"

        def forward_bounce(p: dict) -> None:
            # Flip the blaster's reversed bounce step to its positive → a forward shove, not a rebound.
            blaster = next(t for t in p["targets"] if t.get("name") == "blaster")
            for b in blaster["blocks"].values():
                if (
                    b["opcode"] == "motion_changeyby"
                    and _num_operand(b["inputs"].get("DY")) == director.BACURA_BOUNCE_DY
                ):
                    b["inputs"]["DY"] = [1, [4, str(-director.BACURA_BOUNCE_DY)]]

        cases = [
            ("bounce-detector-warp", unwarp_detector),
            ("bacura-update-calls-bounce", drop_bounce_call),
            ("bounce-marks-shot", break_mark),
            ("bounce-window", break_window),
            ("bounce-not-scored", add_score),
            ("shot-reverses-and-animates", forward_bounce),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._wpn01_failures(project), label)

    @staticmethod
    def _air12_radiating_failures(project: dict) -> set:
        """AIR-12 radiating-spread emission contract — violated labels. Pins the shared `emit radiating
        bullet` mechanism (init_radiating_bullet 32C4 -> cpy_dY_dX_to_obj 3383) that the Brag Zakato fan
        and Garu Zakato ring (slice 11 air.special-pairs) drive: allocate an idle enemy bullet from the
        shared 19-slot pool, copy the FIRING slot's cell into it, and give it the 48-magnitude (3 px/frame)
        velocity for a CALLER-SUPPLIED explicit direction (`radiating angle`, 0..31) — distinct from
        `_fire_aimed_bullet`, which computes a craft-aimed index on the 32-magnitude (2 px/frame) generic
        tier. Every placement write is gated on a successful allocation. The allocator stamps the slot an
        ordinary BULLET_TYPE, so the emitted bullet then flies straight under the one shared bullet update
        (the port folds the arcade's straight handle_07 into that update, 026) — this leaf is only the
        EMISSION shape. The harness `radiating-bullet-emits-at-explicit-angle` drives it live (two angles ->
        two distinct 48-tier vectors)."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        body = _proc_body_blocks(stage, director.RADIATING_EMIT_PROCCODE)
        id_of = {id(b): bid for bid, b in blocks.items()}

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

        def ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def walk(root_id):
            # DFS over block refs from root_id; return (lists_read, vars_read) — data_itemoflist LIST
            # fields and inline variable operands ([., [12, name, id], .]) anywhere in the subtree.
            seen, frontier, lists, vars_ = set(), [root_id], set(), set()
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "data_itemoflist":
                    lists.add(b["fields"]["LIST"][1])
                for v in b.get("inputs", {}).values():
                    if (
                        isinstance(v, list)
                        and len(v) >= 2
                        and isinstance(v[1], list)
                        and len(v[1]) >= 3
                        and v[1][0] == 12
                    ):
                        vars_.add(v[1][2])
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return lists, vars_

        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        def writes(list_id):
            return [
                b
                for b in body
                if b["opcode"] == "data_replaceitemoflist" and b["fields"]["LIST"][1] == list_id
            ]

        def item_reads(write, want):
            r = ref(write["inputs"].get("ITEM"))
            return want in (walk(r)[0] if r else set())

        # (1) the emitter exists and is warp (atomic) — a mid-emit yield could double-spend a bullet slot.
        p = proto(director.RADIATING_EMIT_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("radiating-emit-proc-warp")

        # (2) it allocates an idle bullet from the shared pool (the same allocator the aimed shooters use).
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
            for b in body
        ):
            failures.add("radiating-emit-allocates")

        dx_writes, dy_writes = writes(director.SLOT_DX_ID), writes(director.SLOT_DY_ID)
        dx_lists, dx_vars = (walk(ref(dx_writes[0]["inputs"]["ITEM"])) if dx_writes else (set(), set()))
        dy_lists, dy_vars = (walk(ref(dy_writes[0]["inputs"]["ITEM"])) if dy_writes else (set(), set()))
        body_lists = {b["fields"]["LIST"][1] for b in body if b["opcode"] == "data_itemoflist"}
        body_vars = set()
        for b in body:
            for v in b.get("inputs", {}).values():
                if (
                    isinstance(v, list)
                    and len(v) >= 2
                    and isinstance(v[1], list)
                    and len(v[1]) >= 3
                    and v[1][0] == 12
                ):
                    body_vars.add(v[1][2])

        # (3) THE 48 TIER, NOT THE 32 TIER. `slot dx` reads `aim dx 48`, `slot dy` reads `aim dy 48`, and
        # the body reads NEITHER generic-32 aim list — the discriminator from craft-aimed `_fire_aimed_bullet`.
        if (
            director.AIM_DX_48_ID not in dx_lists
            or director.AIM_DY_48_ID not in dy_lists
            or director.AIM_DX_32_ID in body_lists
            or director.AIM_DY_32_ID in body_lists
        ):
            failures.add("radiating-emit-48-tier")

        # (4) EXPLICIT CALLER ANGLE. Both velocity indices read the `radiating angle` variable, and the body
        # NEITHER calls the craft-aim quantizer NOR reads its resolved `aim index` — so the direction is the
        # caller's, not one computed toward the craft (the negative that separates radiating from aimed fire).
        calls_compute = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.COMPUTE_AIM_PROCCODE
            for b in body
        )
        if (
            director.RADIATING_ANGLE_ID not in dx_vars
            or director.RADIATING_ANGLE_ID not in dy_vars
            or calls_compute
            or director.AIM_INDEX_ID in body_vars
        ):
            failures.add("radiating-emit-explicit-angle")

        # (5) the bullet spawns at the FIRING slot's cell — `slot x`/`slot y` writes copy that slot's own
        # `slot x`/`slot y` (init_radiating_bullet's set_state_and_copy_obj_coords 32A5).
        if not (
            dx_writes
            and writes(director.SLOT_X_ID)
            and item_reads(writes(director.SLOT_X_ID)[0], director.SLOT_X_ID)
            and writes(director.SLOT_Y_ID)
            and item_reads(writes(director.SLOT_Y_ID)[0], director.SLOT_Y_ID)
        ):
            failures.add("radiating-emit-copies-firing-cell")

        # (6) every placement is gated on a SUCCESSFUL allocation — the `slot dx` write sits under an
        # ancestor `if` whose condition reads `bullet alloc result` (else a failed alloc would clobber slot 0).
        if not (
            dx_writes
            and ancestor_if(
                id_of[id(dx_writes[0])],
                lambda c: director.BULLET_ALLOC_RESULT_ID in walk(c)[1],
            )
        ):
            failures.add("radiating-emit-gated-on-alloc")

        return failures

    # roadmap-evidence: AIR-12 success  (test_radiating_emission_authoring_present — emitter warp, allocates from the shared pool, 48-tier not 32-tier velocity, explicit caller angle not craft-aim, copies the firing cell, every placement gated on a successful alloc)
    # roadmap-evidence: AIR-12 failure  (test_radiating_emission_negative_fixtures — each contract clause corrupted bites)
    def test_radiating_emission_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air12_radiating_failures(project))

    def test_radiating_emission_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air12_radiating_failures(base))

        def _body(p):
            stage = next(t for t in p["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, director.RADIATING_EMIT_PROCCODE)

        def _ref(inp):
            if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str):
                return inp[1]
            return None

        def unwarp(p: dict) -> None:
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in stage["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.RADIATING_EMIT_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_alloc(p: dict) -> None:
            # Rename the emitter's allocator call → nothing reserves a bullet slot. The allocates clause bites.
            stage, body = _body(p)
            for b in body:
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def use_32_tier(p: dict) -> None:
            # Flip the `slot dx` velocity source from the 48 tier to the generic 32 tier → the bullet would
            # fly at the slower aimed speed. The 48-tier clause bites.
            stage, body = _body(p)
            blocks = stage["blocks"]
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_DX_ID
                ):
                    item = blocks.get(_ref(b["inputs"].get("ITEM")))
                    if item is not None and item["opcode"] == "data_itemoflist" and item["fields"]["LIST"][1] == director.AIM_DX_48_ID:
                        item["fields"]["LIST"] = ["aim dx 32", director.AIM_DX_32_ID]

        def constant_angle(p: dict) -> None:
            # Replace one velocity index's `radiating angle mod 32` input with a constant → that index no
            # longer reads the caller's angle. The explicit-caller-angle clause bites (it requires BOTH).
            stage, body = _body(p)
            for b in body:
                if b["opcode"] == "operator_mod" and (
                    isinstance(b["inputs"].get("NUM1"), list)
                    and isinstance(b["inputs"]["NUM1"][1], list)
                    and len(b["inputs"]["NUM1"][1]) >= 3
                    and b["inputs"]["NUM1"][1][2] == director.RADIATING_ANGLE_ID
                ):
                    b["inputs"]["NUM1"] = [1, [4, "1"]]
                    break

        def drop_position_copy(p: dict) -> None:
            # Replace the `slot x` copy's ITEM with a constant → the bullet no longer spawns at the firing
            # cell. The copies-firing-cell clause bites.
            stage, body = _body(p)
            for b in body:
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_X_ID
                ):
                    b["inputs"]["ITEM"] = [1, [4, "0"]]

        def ungate_alloc(p: dict) -> None:
            # Point the alloc-success guard's `> 0` test at a different variable → the placement is no longer
            # gated on a successful allocation. The gated-on-alloc clause bites.
            stage, body = _body(p)
            for b in body:
                if b["opcode"] == "operator_gt" and (
                    isinstance(b["inputs"].get("OPERAND1"), list)
                    and isinstance(b["inputs"]["OPERAND1"][1], list)
                    and len(b["inputs"]["OPERAND1"][1]) >= 3
                    and b["inputs"]["OPERAND1"][1][2] == director.BULLET_ALLOC_RESULT_ID
                ):
                    b["inputs"]["OPERAND1"] = [3, [12, "slot index", director.SLOT_INDEX_ID], [10, ""]]

        cases = [
            ("radiating-emit-proc-warp", unwarp),
            ("radiating-emit-allocates", drop_alloc),
            ("radiating-emit-48-tier", use_32_tier),
            ("radiating-emit-explicit-angle", constant_angle),
            ("radiating-emit-copies-firing-cell", drop_position_copy),
            ("radiating-emit-gated-on-alloc", ungate_alloc),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air12_radiating_failures(project), label)

    # ------------------------------------------------------------------ slice-9 ground guards
    # The settling harness advances whole ticks and drives play to rest, so a family's per-tick,
    # single-frame facts (fires exactly at the fully-open midpoint, craters PERSISTENTLY vs
    # explode-and-remove, the arm/cadence gates, the top-of-field spawn) are pinned structurally here,
    # each with a severing negative in test_*_negative_fixtures — the ground analogue of the _air0N guards.

    @staticmethod
    def _gnd_dispatch_failures(project: dict) -> set:
        """AREA-02 ground-dispatch (#69) authoring contract — violated labels. Pins the terrain-locked
        substrate every ground family shares: `advance ground` is the single warp scroll+cull one built
        ground families delegate to (slot x advances by AREA_PROGRESS_STEP each tick — the first
        terrain-locked scroller, glued DOWN the field — then culls off the bottom edge); the ordered walk
        routes each built ground type to its wrapper (Barra/Garu/Logram); and add_ground_object spawns a
        built object at the TOP of the field (slot x = 0), ACTIVE, only for the families built this PR
        (every other scheduled ground type advances the cursor without stamping a slot)."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        id_of = {id(b): bid for bid, b in blocks.items()}

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

        def rref(inp):
            r = inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None
            return blocks.get(r) if r else None

        def num_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
            ):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        advance_body = _proc_body_blocks(stage, director.ADVANCE_GROUND_PROCCODE)
        slots_body = _proc_body_blocks(stage, director.ADVANCE_SLOTS_PROCCODE)

        # (1) `advance ground` exists and is warp (atomic) — a non-warp scroll would yield mid-slot.
        p = proto(director.ADVANCE_GROUND_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("advance-ground-warp")

        # (2) TERRAIN-LOCKED SCROLL: `advance ground` sets `slot x` to `slot x + AREA_PROGRESS_STEP`.
        # A flying enemy moves by its own (dx,dy); a ground object only ever advances the scroll axis by
        # the fixed terrain step, so it stays glued to the terrain as the field scrolls down.
        def scrolls_by_step():
            for b in advance_body:
                if not (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_X_ID
                ):
                    continue
                item = rref(b["inputs"].get("ITEM"))
                if item is None or item["opcode"] != "operator_add":
                    continue
                base = rref(item["inputs"].get("NUM1"))
                if (
                    base is not None
                    and base["opcode"] == "data_itemoflist"
                    and base["fields"]["LIST"][1] == director.SLOT_X_ID
                    and num_operand(item["inputs"].get("NUM2")) == director.AREA_PROGRESS_STEP
                ):
                    return True
            return False

        if not scrolls_by_step():
            failures.add("advance-ground-scrolls-terrain")

        # (3) OFF-FIELD CULL: `advance ground` culls the slot once it scrolls past the bottom — a
        # `cull slot` call whose enclosing gate carries the CULL_ROW_MAX bound. Without it a ground object
        # (or its persistent crater) would live forever after scrolling off the bottom of the field.
        cull_ids = [
            id_of[id(b)]
            for b in advance_body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
        ]
        has_cull_bound = any(
            b["opcode"] == "operator_lt"
            and num_operand(b["inputs"].get("OPERAND2")) == director.CULL_ROW_MAX
            for b in advance_body
        )
        if not cull_ids or not has_cull_bound:
            failures.add("advance-ground-culls-off-field")

        # (4) THE ORDERED WALK DISPATCHES EACH BUILT GROUND TYPE to its wrapper proc: BARRA_TYPE ->
        # `update barra`, GARU_BARRA_TYPE -> `update garu`, LOGRAM_TYPE -> `update logram`. Each wrapper
        # call sits directly under an `if walk type == <type>` branch, so a slot only runs its own family's
        # per-tick behaviour. `walk type` is an inline variable reporter (a [3,[12,...]] operand).
        def var_id(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 3
                and inp[1][0] == 12
            ):
                return inp[1][2]
            return None

        def dispatches(type_value, proccode):
            for b in slots_body:
                if not (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == proccode
                ):
                    continue
                parent = blocks.get(b.get("parent"))
                if parent is None or parent["opcode"] not in ("control_if", "control_if_else"):
                    continue
                cond = rref(parent["inputs"].get("CONDITION"))
                if cond is None or cond["opcode"] != "operator_equals":
                    continue
                if (
                    var_id(cond["inputs"].get("OPERAND1")) == director.WALK_TYPE_ID
                    and num_operand(cond["inputs"].get("OPERAND2")) == type_value
                ):
                    return True
            return False

        if not (
            dispatches(director.BARRA_TYPE, director.UPDATE_BARRA_PROCCODE)
            and dispatches(director.GARU_BARRA_TYPE, director.UPDATE_GARU_PROCCODE)
            and dispatches(director.LOGRAM_TYPE, director.UPDATE_LOGRAM_PROCCODE)
        ):
            failures.add("dispatch-routes-ground-types")

        # (5) SPAWN AT TOP OF FIELD: the add_ground_object spawn seeds a built object's `slot x` = 0 (the
        # arcade leaves _X = 0 at the top and `advance ground` scrolls it down). A non-zero spawn x would
        # drop the object mid-field. Spot the spawn's `slot x` replace with a literal 0 (the crosshair/bomb
        # slot writes use variables/leads, never a bare 0, so this is the ground-spawn discriminator).
        spawn_body = _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE)
        spawns_top = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_X_ID
            and num_operand(b["inputs"].get("ITEM")) == 0
            for b in spawn_body
        )
        if not spawns_top:
            failures.add("spawn-stamps-top-of-field")

        # (6) SPAWN SCOPED TO BUILT TYPES: the single-slot spawn runs only for Barra OR Logram, and the
        # Garu two-slot spawn only for GARU_BARRA_TYPE — every other scheduled ground type advances the
        # cursor WITHOUT stamping a slot, so no unbuilt family renders a live-but-inert object. Structural:
        # a `ground type == GARU_BARRA_TYPE` gate exists (the Garu branch) and a Barra-or-Logram OR gate
        # guards the single-slot spawn.
        def eq_ground_type(bid_cond, value):
            seen, frontier = set(), [bid_cond]
            while frontier:
                x = frontier.pop()
                if not x or x in seen or x not in blocks:
                    continue
                seen.add(x)
                bb = blocks[x]
                if bb["opcode"] == "operator_equals":
                    lhs = rref(bb["inputs"].get("OPERAND1"))
                    if (
                        lhs is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == director.GROUND_OBJECT_TYPE_ID
                        and num_operand(bb["inputs"].get("OPERAND2")) == value
                    ):
                        return True
                for v in bb.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        conds = [
            b["inputs"]["CONDITION"][1]
            for b in spawn_body
            if b["opcode"] in ("control_if", "control_if_else")
            and isinstance(b["inputs"].get("CONDITION"), list)
            and len(b["inputs"]["CONDITION"]) >= 2
            and isinstance(b["inputs"]["CONDITION"][1], str)
        ]
        garu_gated = any(eq_ground_type(c, director.GARU_BARRA_TYPE) for c in conds)
        built_or_gated = any(
            eq_ground_type(c, director.BARRA_TYPE) and eq_ground_type(c, director.LOGRAM_TYPE)
            for c in conds
        )
        if not (garu_gated and built_or_gated):
            failures.add("spawn-scoped-to-built-types")

        return failures

    @staticmethod
    def _gnd_awards_failures(project: dict) -> set:
        """ECO-01 ground-awards (#68) authoring contract — violated labels. Pins the bomb-vs-ground
        detector `check ground hit` (handle_bombed_obj_and_award_points $19EE): a single warp proc that
        sweeps all 16 ground slots INTERNALLY against the locked bomb target, and for each ACTIVE object on
        target routes through the ONE shared `resolve hit` -> `score` path reading its value from the value
        table by `slot pts`, then zeroes `slot timer` so the crater clock starts at 0. The ACTIVE gate is
        what makes the Garu Barra base (a non-ACTIVE sentinel) indestructible for free."""
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

        def rref(inp):
            r = inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None
            return blocks.get(r) if r else None

        def num_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
            ):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        body = _proc_body_blocks(stage, director.CHECK_GROUND_HIT_PROCCODE)

        # (1) `check ground hit` exists and is warp — the whole 16-slot sweep + scoring resolves atomically.
        p = proto(director.CHECK_GROUND_HIT_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("check-ground-hit-warp")

        # (2) SWEEPS ALL 16 GROUND SLOTS, ACTIVE-ONLY. The detector reads `slot state[s] == SLOT_ACTIVE`
        # for every ground slot s in 1..16 (unrolled). This is both the 16-wide sweep and the ACTIVE gate
        # that excludes the Garu base sentinel; a missing slot leaves a bombable object unscoreable, a
        # wrong state constant would score the indestructible base.
        active_slots = set()
        for b in body:
            if b["opcode"] != "operator_equals":
                continue
            lhs = rref(b["inputs"].get("OPERAND1"))
            if (
                lhs is not None
                and lhs["opcode"] == "data_itemoflist"
                and lhs["fields"]["LIST"][1] == director.SLOT_STATE_ID
                and num_operand(lhs["inputs"].get("INDEX")) is not None
                and num_operand(b["inputs"].get("OPERAND2")) == director.SLOT_ACTIVE
            ):
                active_slots.add(num_operand(lhs["inputs"].get("INDEX")))
        if active_slots != set(range(director.GROUND_SLOTS[0], director.GROUND_SLOTS[1] + 1)):
            failures.add("sweeps-16-active-ground-slots")

        # (3) SCORES THROUGH THE SHARED PATH: the detector reads the award from the value table indexed by
        # `slot pts` and calls `resolve hit` (the single score path), so every ground object scores its own
        # value exactly like an air kill. A dropped `resolve hit` would mark nothing / score nothing.
        # the award reads value table[slot pts[s]] — a value-table item-of whose INDEX is a slot pts item-of
        awards_from_value_table = any(
            b["opcode"] == "data_itemoflist"
            and b["fields"]["LIST"][1] == director.VALUE_TABLE_ID
            and (idx := rref(b["inputs"].get("INDEX"))) is not None
            and idx["opcode"] == "data_itemoflist"
            and idx["fields"]["LIST"][1] == director.SLOT_PTS_ID
            for b in body
        )
        calls_resolve = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.RESOLVE_HIT_PROCCODE
            for b in body
        )
        if not (awards_from_value_table and calls_resolve):
            failures.add("scores-through-shared-path")

        # (4) RESETS THE CRATER CLOCK: on each scored object the detector zeroes `slot timer` (the hit tick)
        # so the ground explosion clock — floor(slot timer / 8) through the burst then the persistent
        # crater — starts from 0. Without it the crater would begin mid-animation from a stale clock.
        resets_clock = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
            and num_operand(b["inputs"].get("ITEM")) == 0
            for b in body
        )
        if not resets_clock:
            failures.add("resets-crater-clock")

        return failures

    @staticmethod
    def _gnd01_failures(project: dict) -> set:
        """GND-01 ground.barra (#70) authoring contract — violated labels. Pins the two Barra wrappers.
        The Barra (handle_1E_Barra) never fires and never moves under its own power — it only scrolls with
        the terrain (`advance ground`) — and once bombed craters PERSISTENTLY (handle_bomb_explosion $3186):
        its HIT branch advances the explosion/crater clock and scrolls, and is NEVER freed on the clock (the
        arcade crater scrolls forever, culled only when it leaves the field — the clean discriminator from
        the flying explode-tick, which frees on its own clock). The Garu Barra (handle_20_Garu_Barra) is
        two slots of ONE type: the indestructible base (a non-ACTIVE sentinel the detector rejects) and a
        destructible node whose HIT branch explode-and-removes (explode_and_remove_object $3216 — advances
        the burst clock, then CULLS at GARU_REMOVE_FRAMES; no crater, unlike the Barra)."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        id_of = {id(b): bid for bid, b in blocks.items()}

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

        def rref(inp):
            r = inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None
            return blocks.get(r) if r else None

        def num_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
            ):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        barra_body = _proc_body_blocks(stage, director.UPDATE_BARRA_PROCCODE)
        garu_body = _proc_body_blocks(stage, director.UPDATE_GARU_PROCCODE)

        def advances_clock(body):
            return any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                and (item := rref(b["inputs"].get("ITEM"))) is not None
                and item["opcode"] == "operator_add"
                and (base := rref(item["inputs"].get("NUM1"))) is not None
                and base["opcode"] == "data_itemoflist"
                and base["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                for b in body
            )

        def calls(body, proccode):
            return any(
                b["opcode"] == "procedures_call" and b.get("mutation", {}).get("proccode") == proccode
                for b in body
            )

        # (1) Both Barra wrappers exist and are warp (atomic) — a non-warp wrapper would yield mid-tick.
        for proccode in (director.UPDATE_BARRA_PROCCODE, director.UPDATE_GARU_PROCCODE):
            p = proto(proccode)
            if p is None or p["mutation"].get("warp") != "true":
                failures.add("barra-garu-procs-warp")

        # (2) THE BARRA NEVER FIRES. `update barra` allocates no bullet and calls no fire gate — the passive
        # terrain target. A bullet allocation here would be a fires-like-a-shooter regression (the ABSENCE
        # is the contract, as with the Jara silent).
        if calls(barra_body, director.ALLOC_BULLET_PROCCODE) or calls(barra_body, director.FIRE_GATE_PROCCODE):
            failures.add("barra-never-fires")

        # (3) THE BARRA CRATERS PERSISTENTLY. `update barra` advances the explosion/crater clock (a
        # `slot timer + step` write) and delegates the terrain scroll to `advance ground`, and it NEVER
        # frees on that clock — there is NO `cull slot` call inside `update barra` itself (the only removal
        # path is `advance ground`'s off-field cull). This is the discriminator from the flying explode-tick.
        if not (advances_clock(barra_body) and calls(barra_body, director.ADVANCE_GROUND_PROCCODE)):
            failures.add("barra-craters-and-scrolls")
        if calls(barra_body, director.CULL_SLOT_PROCCODE):
            failures.add("barra-crater-persists")

        # (4) THE GARU NODE EXPLODES-AND-REMOVES. `update garu`'s HIT branch advances the burst clock and,
        # once it reaches GARU_REMOVE_FRAMES, CULLS the node (vanish, no crater). Structural: `update garu`
        # advances the clock AND contains a `cull slot` call whose gate carries the GARU_REMOVE_FRAMES
        # bound — the clean discriminator from the Barra's persistent, never-culled-on-clock crater.
        garu_bounded_cull = calls(garu_body, director.CULL_SLOT_PROCCODE) and any(
            b["opcode"] == "operator_lt"
            and num_operand(b["inputs"].get("OPERAND2")) == director.GARU_REMOVE_FRAMES
            for b in garu_body
        )
        if not (advances_clock(garu_body) and garu_bounded_cull):
            failures.add("garu-node-explodes-and-removes")

        # (5) THE GARU BASE IS INDESTRUCTIBLE (spawn fact). The Garu spawn stamps the base slot's state as
        # the SLOT_GARU_BASE sentinel (not ACTIVE) and the node as ACTIVE worth GARU_BARRA_PTS, so the
        # detector's ==ACTIVE gate scores the node but never the base. A base stamped ACTIVE would be
        # bombable — the indestructible-base regression.
        spawn_body = _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE)
        base_sentinel = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and num_operand(b["inputs"].get("ITEM")) == director.SLOT_GARU_BASE
            for b in spawn_body
        )
        node_pts = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and num_operand(b["inputs"].get("ITEM")) == director.GARU_BARRA_PTS
            for b in spawn_body
        )
        if not (base_sentinel and node_pts):
            failures.add("garu-base-indestructible")

        # (6) THE BARRA SCORES 100 (spawn fact): the spawn stamps `slot pts` = BARRA_PTS for a Barra. A
        # wrong index would award the wrong value on the bomb kill.
        barra_pts = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and num_operand(b["inputs"].get("ITEM")) == director.BARRA_PTS
            for b in spawn_body
        )
        if not barra_pts:
            failures.add("barra-awards-100")

        return failures

    @staticmethod
    def _gnd03_failures(project: dict) -> set:
        """GND-03 ground.logram (#71) authoring contract — violated labels. Pins the Logram's open/close +
        single-shot cycle (handle_logram_main $1B64). Its ACTIVE timer phase is gated two ways before it
        advances — ARMING (`cur_row <= ground stop firing row`, handle_logram_exit only scrolls below that)
        and CADENCE (the arcade every-8th-frame phase, `tick mod FIRE_GATE_PHASE_TICKS == 0`). It fires
        EXACTLY ONE aimed bullet DIRECTLY (no fire gate) at `slot fire timer == LOGRAM_FIRE_TIMER` (stage 3,
        dome fully open), walks the dome ordinal triangle (OPEN_FRAME_COUNT - |stage - PEAK|), and at the
        recycle stage re-rolls a fresh masked-random wait. Once bombed it craters PERSISTENTLY exactly like
        the Barra (the SAME handle_bomb_explosion, not the Garu node's explode-and-remove)."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        id_of = {id(b): bid for bid, b in blocks.items()}

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

        def ref(inp):
            return inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None

        def rref(inp):
            r = ref(inp)
            return blocks.get(r) if r else None

        def num_operand(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 2
                and inp[1][0] in (4, 5, 6, 7, 8, 9, 10)
            ):
                try:
                    return int(inp[1][1])
                except (ValueError, TypeError):
                    return None
            return None

        def var_id(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 3
                and inp[1][0] == 12
            ):
                return inp[1][2]
            return None

        body = _proc_body_blocks(stage, director.UPDATE_LOGRAM_PROCCODE)

        def cond_has_eq(cond_id, list_id, value):
            seen, frontier = set(), [cond_id]
            while frontier:
                cid = frontier.pop()
                if not cid or cid in seen or cid not in blocks:
                    continue
                seen.add(cid)
                b = blocks[cid]
                if b["opcode"] == "operator_equals":
                    lhs = rref(b["inputs"].get("OPERAND1"))
                    if (
                        lhs is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == list_id
                        and num_operand(b["inputs"].get("OPERAND2")) == value
                    ):
                        return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def ancestor_if(node_id, pred):
            cur = blocks.get(node_id)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    if pred(ref(parent["inputs"].get("CONDITION"))):
                        return True
                cur = parent
            return False

        # (1) `update logram` exists and is warp (atomic).
        p = proto(director.UPDATE_LOGRAM_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("logram-warp")

        # (2) FIRES AT THE FULLY-OPEN MIDPOINT ONLY. Every allocator call in the body sits inside a
        # `slot fire timer == LOGRAM_FIRE_TIMER` gate (there are two allocator sites — animate_step is
        # built fresh at the WAIT->ANIMATE fall-through and in the steady ANIMATE branch — and BOTH are so
        # gated), and there is at least one. So the Logram fires exactly the single tick its timer is 12.
        alloc_ids = [
            id_of[id(b)]
            for b in body
            if b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
        ]
        if not alloc_ids or not all(
            ancestor_if(a, lambda c: cond_has_eq(c, director.SLOT_FIRE_TIMER_ID, director.LOGRAM_FIRE_TIMER))
            for a in alloc_ids
        ):
            failures.add("logram-fires-at-full-open")

        # (3) FIRES DIRECTLY, NOT THROUGH THE SHARED FIRE GATE (the aimed one-shot allocator, like the
        # Torkan/Jara). A fire-gate call would make it a periodic masked shooter.
        if any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
            for b in body
        ):
            failures.add("logram-fires-directly")

        # (4) ARMING-GATED: the timer phase only advances while the object is high enough on the field —
        # a gate testing `cur_row > ground stop firing row` (armed = NOT that). Below the row the arcade
        # only scrolls. Spot the `operator_gt` whose right operand is the `ground stop firing row` variable.
        armed_gate = any(
            b["opcode"] == "operator_gt"
            and var_id(b["inputs"].get("OPERAND2")) == director.GROUND_STOP_FIRING_ROW_ID
            for b in body
        )
        if not armed_gate:
            failures.add("logram-arming-gated")

        # (5) CADENCE-GATED: the timer phase advances on the arcade's every-8th-frame phase — a
        # `(tick mod FIRE_GATE_PHASE_TICKS) == 0` gate. Spot an `operator_equals` whose left operand is a
        # `tick mod FIRE_GATE_PHASE_TICKS` and whose right operand is 0.
        cadence_gate = any(
            b["opcode"] == "operator_equals"
            and (m := rref(b["inputs"].get("OPERAND1"))) is not None
            and m["opcode"] == "operator_mod"
            and var_id(m["inputs"].get("NUM1")) == director.TICK_ID
            and num_operand(m["inputs"].get("NUM2")) == director.FIRE_GATE_PHASE_TICKS
            and num_operand(b["inputs"].get("OPERAND2")) == 0
            for b in body
        )
        if not cadence_gate:
            failures.add("logram-cadence-gated")

        # (6) DOME OPEN/CLOSE TRIANGLE: the animate step writes `slot code` = OPEN_FRAME_COUNT -
        # |stage - STAGE_PEAK|, the triangle {1,2,3,4,3,2,1} that opens the dome to its peak then closes it.
        # Spot a `slot code` replace whose ITEM is `OPEN_FRAME_COUNT - abs(...)`.
        dome_triangle = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_CODE_ID
            and (item := rref(b["inputs"].get("ITEM"))) is not None
            and item["opcode"] == "operator_subtract"
            and num_operand(item["inputs"].get("NUM1")) == director.LOGRAM_OPEN_FRAME_COUNT
            and (absr := rref(item["inputs"].get("NUM2"))) is not None
            and absr["opcode"] == "operator_mathop"
            and absr["fields"].get("OPERATOR", [None])[0] == "abs"
            for b in body
        )
        if not dome_triangle:
            failures.add("logram-dome-triangle")

        # (7) RECYCLES AT STAGE 7: at the recycle stage the cycle re-rolls a fresh masked-random wait
        # (an `rng step` call in the body) and returns to WAIT. Spot the `stage == LOGRAM_RECYCLE_STAGE`
        # gate (an equals whose left is a `... mod LOGRAM_STAGE_MOD` and whose right is the recycle stage)
        # and the presence of the RNG re-roll call.
        recycle_gate = any(
            b["opcode"] == "operator_equals"
            and (m := rref(b["inputs"].get("OPERAND1"))) is not None
            and m["opcode"] == "operator_mod"
            and num_operand(m["inputs"].get("NUM2")) == director.LOGRAM_STAGE_MOD
            and num_operand(b["inputs"].get("OPERAND2")) == director.LOGRAM_RECYCLE_STAGE
            for b in body
        )
        rerolls = any(
            b["opcode"] == "procedures_call" and b.get("mutation", {}).get("proccode") == director.RNG_PROCCODE
            for b in body
        )
        if not (recycle_gate and rerolls):
            failures.add("logram-recycles-at-stage-7")

        # (8) HIT CRATERS PERSISTENTLY like the Barra: the HIT branch advances `slot timer` and scrolls via
        # `advance ground`, and `update logram` frees the slot only through that shared off-field cull (no
        # `cull slot` call inside `update logram` itself). Same persistent-crater shape as `update barra`.
        advances_clock = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
            and (item := rref(b["inputs"].get("ITEM"))) is not None
            and item["opcode"] == "operator_add"
            and (base := rref(item["inputs"].get("NUM1"))) is not None
            and base["opcode"] == "data_itemoflist"
            and base["fields"]["LIST"][1] == director.SLOT_TIMER_ID
            for b in body
        )
        scrolls = any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
            for b in body
        )
        if not (advances_clock and scrolls):
            failures.add("logram-hit-craters")
        if any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
            for b in body
        ):
            failures.add("logram-crater-persists")

        return failures

    @staticmethod
    def _gnd02_failures(project: dict) -> set:
        """GND-02 ground.zolbak (#85) authoring contract — violated labels. The Zolbak (handle_1F_Zolbak)
        IS the Barra crater model: it never fires, scrolls with the terrain while ACTIVE, and once bombed
        craters PERSISTENTLY (the SAME handle_bomb_explosion, never freed on its own clock). Its ONE extra
        behaviour is on the FIRST HIT tick only — uniquely marked by `slot timer == 0`, since the detector
        zeroed the crater clock at the hit and `update zolbak` only climbs it afterwards — it reduces the
        adaptive enemy AI level by EXACTLY 2 (reduce_enemy_ai_by_2 $1B1F: `subq #2; jcc; moveq #0`),
        clamped to 0 on the unsigned underflow, reading and writing the SAME `ai level` the difficulty
        director grows. The reduction runs ONCE per kill, never again on the later crater ticks."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        id_of = {id(b): bid for bid, b in blocks.items()}

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

        def rref(inp):
            r = inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None
            return blocks.get(r) if r else None

        def var_id(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 3
                and inp[1][0] == 12
            ):
                return inp[1][2]
            return None

        def calls(body, proccode):
            return any(
                b["opcode"] == "procedures_call" and b.get("mutation", {}).get("proccode") == proccode
                for b in body
            )

        def advances_clock(body):
            return any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                and (item := rref(b["inputs"].get("ITEM"))) is not None
                and item["opcode"] == "operator_add"
                and (base := rref(item["inputs"].get("NUM1"))) is not None
                and base["opcode"] == "data_itemoflist"
                and base["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                for b in body
            )

        def enclosing_cond(bid, pred):
            # Walk the block's parent chain (which threads back through the enclosing if/if-else) and test
            # each control condition — used to prove a statement sits inside a specific guard.
            cur = blocks.get(bid)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    cond = parent["inputs"].get("CONDITION")
                    cb = (
                        blocks.get(cond[1])
                        if isinstance(cond, list) and len(cond) >= 2 and isinstance(cond[1], str)
                        else None
                    )
                    if pred(cb):
                        return True
                cur = parent
            return False

        def is_timer_zero(cb):
            return (
                cb is not None
                and cb["opcode"] == "operator_equals"
                and (lhs := rref(cb["inputs"].get("OPERAND1"))) is not None
                and lhs["opcode"] == "data_itemoflist"
                and lhs["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                and _num_operand(cb["inputs"].get("OPERAND2")) == 0
            )

        def is_ai_below_zero(cb):
            return (
                cb is not None
                and cb["opcode"] == "operator_lt"
                and var_id(cb["inputs"].get("OPERAND1")) == director.AI_LEVEL_ID
                and _num_operand(cb["inputs"].get("OPERAND2")) == 0
            )

        body = _proc_body_blocks(stage, director.UPDATE_ZOLBAK_PROCCODE)
        spawn_body = _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE)

        # (1) `update zolbak` exists and is warp (atomic).
        p = proto(director.UPDATE_ZOLBAK_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("zolbak-warp")

        # (2) THE ZOLBAK NEVER FIRES — no bullet allocation and no fire gate (the passive dome).
        if calls(body, director.ALLOC_BULLET_PROCCODE) or calls(body, director.FIRE_GATE_PROCCODE):
            failures.add("zolbak-never-fires")

        # (3) CRATERS PERSISTENTLY like the Barra: advances the crater clock (`slot timer + step`) and
        # scrolls via `advance ground`, and (4) is NEVER freed on its own clock (no `cull slot` inside
        # `update zolbak` — the only removal path is `advance ground`'s off-field cull).
        if not (advances_clock(body) and calls(body, director.ADVANCE_GROUND_PROCCODE)):
            failures.add("zolbak-craters-and-scrolls")
        if calls(body, director.CULL_SLOT_PROCCODE):
            failures.add("zolbak-crater-persists")

        # (5) REDUCES THE AI LEVEL BY EXACTLY 2, ONCE PER KILL: a `change ai level by -2` that sits inside a
        # `slot timer == 0` guard (the first-HIT-tick marker), reading/writing the difficulty director's
        # `ai level`. The guard is what makes the drop fire once per kill, not on every crater tick.
        reduces = any(
            b["opcode"] == "data_changevariableby"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.AI_LEVEL_ID
            and _num_operand(b["inputs"].get("VALUE")) == -director.AI_LEVEL_ZOLBAK_DROP
            and enclosing_cond(id_of[id(b)], is_timer_zero)
            for b in body
        )
        if not reduces:
            failures.add("zolbak-reduces-ai")

        # (6) FLOORED AT ZERO: a `set ai level = 0` clamp gated by `ai level < 0` (the arcade `jcc; moveq #0`
        # underflow clamp). Without it, a kill at level 1 would leave the AI level negative.
        floored = any(
            b["opcode"] == "data_setvariableto"
            and b["fields"].get("VARIABLE", [None, None])[1] == director.AI_LEVEL_ID
            and _num_operand(b["inputs"].get("VALUE")) == 0
            and enclosing_cond(id_of[id(b)], is_ai_below_zero)
            for b in body
        )
        if not floored:
            failures.add("zolbak-ai-floored")

        # (7) AWARDS 200 (spawn fact): the spawn stamps `slot pts` = ZOLBAK_PTS.
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and _num_operand(b["inputs"].get("ITEM")) == director.ZOLBAK_PTS
            for b in spawn_body
        ):
            failures.add("zolbak-awards-200")

        return failures

    @staticmethod
    def _gnd04_failures(project: dict) -> set:
        """GND-04 ground.derota (#86) authoring contract — the firing turret and the firing pair. The Derota
        (handle_1B_Derota) is a plain periodic aimed turret: NO open/close dome (unlike the Logram), it
        drives the SHARED fire-permission gate (chk_timer_fire_bullet_reinit_timer) one aimed bullet per
        masked reload, but ONLY while still high enough on the field — armed by `cur_row <= gnd_stop_firing_row`
        — and once bombed craters PERSISTENTLY like the Barra. The Garu Derota (handle_21_Garu_Derota) is the
        Garu Barra's two-slot shape (indestructible SLOT_GARU_BASE base + destructible node) with ONE
        difference: the node FIRES the shared gate UNCONDITIONALLY of the row (garu_derota_handler omits the
        stop-firing-row gate), scores 2000, and on death explode-and-removes (vanishes at GARU_REMOVE_FRAMES,
        no crater); the base never scores."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]
        id_of = {id(b): bid for bid, b in blocks.items()}

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

        def rref(inp):
            r = inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None
            return blocks.get(r) if r else None

        def var_id(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 3
                and inp[1][0] == 12
            ):
                return inp[1][2]
            return None

        def num(inp):
            return _num_operand(inp)

        def calls(body, proccode):
            return any(
                b["opcode"] == "procedures_call" and b.get("mutation", {}).get("proccode") == proccode
                for b in body
            )

        def advances_clock(body):
            return any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                and (item := rref(b["inputs"].get("ITEM"))) is not None
                and item["opcode"] == "operator_add"
                and (base := rref(item["inputs"].get("NUM1"))) is not None
                and base["opcode"] == "data_itemoflist"
                and base["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                for b in body
            )

        def fire_ids(body):
            return [
                id_of[id(b)]
                for b in body
                if b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
            ]

        def subtree_has(cid, pred):
            seen, frontier = set(), [cid]
            while frontier:
                x = frontier.pop()
                if not x or x in seen or x not in blocks:
                    continue
                seen.add(x)
                b = blocks[x]
                if pred(b):
                    return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def enclosing_cond_id(bid, pred):
            cur = blocks.get(bid)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    cond = parent["inputs"].get("CONDITION")
                    cid = (
                        cond[1]
                        if isinstance(cond, list) and len(cond) >= 2 and isinstance(cond[1], str)
                        else None
                    )
                    if cid and pred(cid):
                        return True
                cur = parent
            return False

        def is_stoprow_gt(b):
            return (
                b["opcode"] == "operator_gt"
                and var_id(b["inputs"].get("OPERAND2")) == director.GROUND_STOP_FIRING_ROW_ID
            )

        def is_state_active(cid):
            c = blocks.get(cid)
            return (
                c is not None
                and c["opcode"] == "operator_equals"
                and (lhs := rref(c["inputs"].get("OPERAND1"))) is not None
                and lhs["opcode"] == "data_itemoflist"
                and lhs["fields"]["LIST"][1] == director.SLOT_STATE_ID
                and num(c["inputs"].get("OPERAND2")) == director.SLOT_ACTIVE
            )

        def preceding_type_gate(bid, typ):
            # A spawn stamp is Garu-Derota-specific when its enclosing `ground type == GARU_DEROTA_TYPE`
            # gate is on the parent chain (the Garu Barra base carries only GARU_BARRA_TYPE, never 0x21).
            cur = blocks.get(bid)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    cond = parent["inputs"].get("CONDITION")
                    cb = (
                        blocks.get(cond[1])
                        if isinstance(cond, list) and len(cond) >= 2 and isinstance(cond[1], str)
                        else None
                    )
                    if cb is not None and cb["opcode"] == "operator_equals" and num(cb["inputs"].get("OPERAND2")) == typ:
                        return True
                cur = parent
            return False

        derota_body = _proc_body_blocks(stage, director.UPDATE_DEROTA_PROCCODE)
        garu_body = _proc_body_blocks(stage, director.UPDATE_GARU_DEROTA_PROCCODE)
        spawn_body = _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE)

        # ---- Derota (single-slot firing turret) ----
        # (1) `update derota` exists and is warp (atomic).
        p = proto(director.UPDATE_DEROTA_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("derota-warp")

        # (2) FIRES THROUGH THE SHARED GATE (the periodic masked turret, unlike the Logram's direct one-shot).
        d_fire = fire_ids(derota_body)
        if not d_fire:
            failures.add("derota-fires-via-gate")

        # (3) FIRE IS ARM-GATED ON THE STOP-FIRING ROW: every fire-gate call sits inside a guard whose
        # condition references `ground stop firing row` (armed = NOT cur_row > row). Below the row the Derota
        # is silent — the gate never even runs, so its countdown is untouched.
        if not (
            d_fire and all(enclosing_cond_id(f, lambda cid: subtree_has(cid, is_stoprow_gt)) for f in d_fire)
        ):
            failures.add("derota-fire-arm-gated")

        # (4) CRATERS PERSISTENTLY on HIT: advances the crater clock and scrolls, and (5) is never freed on
        # its own clock (no `cull slot` inside `update derota`).
        if not (advances_clock(derota_body) and calls(derota_body, director.ADVANCE_GROUND_PROCCODE)):
            failures.add("derota-hit-craters")
        if calls(derota_body, director.CULL_SLOT_PROCCODE):
            failures.add("derota-crater-persists")

        # (6) AWARDS 1000 (spawn fact): the spawn stamps `slot pts` = DEROTA_PTS.
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and num(b["inputs"].get("ITEM")) == director.DEROTA_PTS
            for b in spawn_body
        ):
            failures.add("derota-awards-1000")

        # ---- Garu Derota (indestructible base + firing node) ----
        # (7) `update garu derota` exists and is warp (atomic).
        pg = proto(director.UPDATE_GARU_DEROTA_PROCCODE)
        if pg is None or pg["mutation"].get("warp") != "true":
            failures.add("garu-derota-warp")

        # (8) THE NODE FIRES UNCONDITIONALLY OF THE ROW: a fire-gate call gated only on `slot state == ACTIVE`
        # (so the base never fires), and — the one difference from the single Derota — NO stop-firing-row gate
        # anywhere in the wrapper, so the node fires even below a row that would silence a Derota.
        g_fire = fire_ids(garu_body)
        node_fires = bool(g_fire) and all(enclosing_cond_id(f, is_state_active) for f in g_fire)
        no_row_gate = not any(is_stoprow_gt(b) for b in garu_body)
        if not (node_fires and no_row_gate):
            failures.add("garu-derota-node-fires")

        # (9) THE NODE EXPLODES-AND-REMOVES (like the Garu Barra node): advances the burst clock and CULLS at
        # GARU_REMOVE_FRAMES — it vanishes, no persistent crater.
        g_bounded_cull = calls(garu_body, director.CULL_SLOT_PROCCODE) and any(
            b["opcode"] == "operator_lt" and num(b["inputs"].get("OPERAND2")) == director.GARU_REMOVE_FRAMES
            for b in garu_body
        )
        if not (advances_clock(garu_body) and g_bounded_cull):
            failures.add("garu-derota-node-removes")

        # (10) THE BASE IS INDESTRUCTIBLE (spawn fact): under the GARU_DEROTA_TYPE gate the base slot is
        # stamped the SLOT_GARU_BASE sentinel (not ACTIVE), so the detector's ==ACTIVE gate never scores it.
        base_ok = any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and num(b["inputs"].get("ITEM")) == director.SLOT_GARU_BASE
            and preceding_type_gate(id_of[id(b)], director.GARU_DEROTA_TYPE)
            for b in spawn_body
        )
        if not base_ok:
            failures.add("garu-derota-base-indestructible")

        # (11) THE NODE AWARDS 2000 (spawn fact): the node slot is stamped `slot pts` = GARU_DEROTA_PTS.
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and num(b["inputs"].get("ITEM")) == director.GARU_DEROTA_PTS
            for b in spawn_body
        ):
            failures.add("garu-derota-node-awards-2000")

        return failures

    @staticmethod
    def _gnd05_failures(project: dict) -> set:
        """GND-05 ground.boza-logram (#87) authoring contract — the FIVE-slot composite (handle_2D_Boza_Logram
        $1CDE): four OUTER domes (base+0..3) + one CENTRE (base+4), all sharing BOZA_LOGRAM_TYPE and ONE update
        proc that branches on `slot link` (the port of the arcade `_EXTRA` pointer; the centre stores 0).
          * An OUTER (link > 0) is a lone Logram: the arm+cadence-gated open/close/fire cycle firing ONE aimed
            bullet at the full-open midpoint through the DIRECT `_fire_aimed_bullet` (an `alloc bullet slot`
            call, NOT the shared fire-permission gate), armed by `cur_row <= gnd_stop_firing_row`, and cratering
            PERSISTENTLY on HIT. Its one extra behaviour on HIT is to rewrite the linked CENTRE slot's
            `slot pts` to the 600-point position (update_centre_points_value: the arcade's `_EXTRA->_PTS`).
          * The CENTRE (link == 0) never fires (handle_boza_logram_centre); on HIT it craters persistently AND
            cascades — destroy_all_outer_lograms sets all four outer slots' state DIRECTLY to HIT (index =
            slot index - 1..-4), bypassing the per-slot award sweep. So a centre-first bomb clears the outers
            for NO score (only a directly-bombed OUTER is still ACTIVE when `check ground hit` runs, so only it
            scores and downgrades the centre) — the "only a directly-bombed outer scores" asymmetry falls out
            of the direct cascade write, no per-family scoring special case.
        Spawn facts (add_ground_object consume, under the BOZA_LOGRAM_TYPE gate): five ACTIVE slots; the four
        outers stamped 300 pts + a `slot link` to the centre; the centre stamped 2,000 pts + `slot link` 0."""
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

        def rref(inp):
            r = inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None
            return blocks.get(r) if r else None

        def var_id(inp):
            if (
                isinstance(inp, list)
                and len(inp) >= 2
                and isinstance(inp[1], list)
                and len(inp[1]) >= 3
                and inp[1][0] == 12
            ):
                return inp[1][2]
            return None

        def num(inp):
            return _num_operand(inp)

        def branch_ids(block, key):
            # Every block reachable from a control block's SUBSTACK/SUBSTACK2 (following `next` and every
            # nested input), so a check can inspect exactly one branch of the link discriminator.
            sub = block["inputs"].get(key) if block else None
            start = sub[1] if isinstance(sub, list) and len(sub) >= 2 and isinstance(sub[1], str) else None
            seen, frontier = set(), [start]
            while frontier:
                x = frontier.pop()
                if not x or x in seen or x not in blocks:
                    continue
                seen.add(x)
                b = blocks[x]
                frontier.append(b.get("next"))
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return seen

        def subtree_has(cid, pred):
            seen, frontier = set(), [cid]
            while frontier:
                x = frontier.pop()
                if not x or x in seen or x not in blocks:
                    continue
                seen.add(x)
                b = blocks[x]
                if pred(b):
                    return True
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return False

        def enclosing_cond_has(bid, pred):
            cur = blocks.get(bid)
            while cur is not None:
                parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                    cond = parent["inputs"].get("CONDITION")
                    cid = (
                        cond[1]
                        if isinstance(cond, list) and len(cond) >= 2 and isinstance(cond[1], str)
                        else None
                    )
                    if cid and subtree_has(cid, pred):
                        return True
                cur = parent
            return False

        def is_stoprow_gt(b):
            return (
                b["opcode"] == "operator_gt"
                and var_id(b["inputs"].get("OPERAND2")) == director.GROUND_STOP_FIRING_ROW_ID
            )

        def calls(idset, proccode):
            return any(
                blocks[x]["opcode"] == "procedures_call"
                and blocks[x].get("mutation", {}).get("proccode") == proccode
                for x in idset
            )

        def alloc_ids_of(idset):
            return [
                x
                for x in idset
                if blocks[x]["opcode"] == "procedures_call"
                and blocks[x].get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
            ]

        def advances_clock(idset):
            # A `slot timer` write whose value is `slot timer + N` (the crater/burst clock advance) — NOT the
            # fired bullet's `slot timer = 0` reset, which is indexed by the bullet slot, so this stays specific.
            for x in idset:
                b = blocks[x]
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                    and (item := rref(b["inputs"].get("ITEM"))) is not None
                    and item["opcode"] == "operator_add"
                    and (base := rref(item["inputs"].get("NUM1"))) is not None
                    and base["opcode"] == "data_itemoflist"
                    and base["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                ):
                    return True
            return False

        boza_body = _proc_body_blocks(stage, director.UPDATE_BOZA_PROCCODE)
        spawn_body = _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE)

        # (1) `update boza` exists and is warp (atomic).
        p = proto(director.UPDATE_BOZA_PROCCODE)
        if p is None or p["mutation"].get("warp") != "true":
            failures.add("boza-warp")

        # (2) THE LINK DISCRIMINATOR: one `if slot link == 0 / else` splits the CENTRE branch (then) from the
        # OUTER branch (else) — the port of the arcade dispatch on the object's role.
        def is_link_zero(cid):
            c = blocks.get(cid)
            return (
                c is not None
                and c["opcode"] == "operator_equals"
                and (lhs := rref(c["inputs"].get("OPERAND1"))) is not None
                and lhs["opcode"] == "data_itemoflist"
                and lhs["fields"]["LIST"][1] == director.SLOT_LINK_ID
                and num(c["inputs"].get("OPERAND2")) == 0
            )

        top = None
        for b in boza_body:
            if b["opcode"] == "control_if_else":
                cond = b["inputs"].get("CONDITION")
                cid = cond[1] if isinstance(cond, list) and len(cond) >= 2 and isinstance(cond[1], str) else None
                if cid and is_link_zero(cid):
                    top = b
                    break
        centre_ids = branch_ids(top, "SUBSTACK") if top else set()
        outer_ids = branch_ids(top, "SUBSTACK2") if top else set()
        if top is None or not centre_ids or not outer_ids:
            failures.add("boza-link-branch")

        # ---- OUTER (link > 0): the lone-Logram machine + the centre-value downgrade on HIT ----
        # (3) FIRES the direct aimed one-shot (an `alloc bullet slot` call — NOT the shared gate).
        outer_alloc = alloc_ids_of(outer_ids)
        if not outer_alloc:
            failures.add("boza-outer-fires")
        # (4) THE FIRE IS ARM-GATED ON THE STOP-FIRING ROW: every fire sits inside a guard referencing
        # `ground stop firing row` (armed = NOT cur_row > row). Below the row the dome is silent.
        if not (outer_alloc and all(enclosing_cond_has(x, is_stoprow_gt) for x in outer_alloc)):
            failures.add("boza-outer-arm-gated")
        # (5) CRATERS PERSISTENTLY on HIT: advances the crater clock and scrolls, and (6) never frees itself.
        if not (advances_clock(outer_ids) and calls(outer_ids, director.ADVANCE_GROUND_PROCCODE)):
            failures.add("boza-outer-craters")
        if calls(outer_ids, director.CULL_SLOT_PROCCODE):
            failures.add("boza-outer-crater-persists")
        # (7) DOWNGRADES THE LINKED CENTRE on HIT: writes the centre slot (indexed by `slot link`) `slot pts`
        # to the 600-point position.
        if not any(
            blocks[x]["opcode"] == "data_replaceitemoflist"
            and blocks[x]["fields"]["LIST"][1] == director.SLOT_PTS_ID
            and num(blocks[x]["inputs"].get("ITEM")) == director.BOZA_CENTRE_DOWNGRADED_PTS
            and (idx := rref(blocks[x]["inputs"].get("INDEX"))) is not None
            and idx["opcode"] == "data_itemoflist"
            and idx["fields"]["LIST"][1] == director.SLOT_LINK_ID
            for x in outer_ids
        ):
            failures.add("boza-outer-downgrades-centre")

        # ---- CENTRE (link == 0): never fires; on HIT craters + cascades all four outers to HIT ----
        # (8) NEVER FIRES: no `alloc bullet slot` anywhere in the centre branch.
        if alloc_ids_of(centre_ids):
            failures.add("boza-centre-never-fires")
        # (9) CASCADES: exactly BOZA_CENTRE_OFFSET (4) direct `slot state = HIT` writes, each indexed by
        # `slot index - k` (k = 1..4) — the four outer slots, set directly (bypassing the award sweep).
        cascade = [
            x
            for x in centre_ids
            if blocks[x]["opcode"] == "data_replaceitemoflist"
            and blocks[x]["fields"]["LIST"][1] == director.SLOT_STATE_ID
            and num(blocks[x]["inputs"].get("ITEM")) == director.SLOT_HIT
        ]

        def indexed_by_offset(x):
            idx = rref(blocks[x]["inputs"].get("INDEX"))
            return (
                idx is not None
                and idx["opcode"] == "operator_subtract"
                and var_id(idx["inputs"].get("NUM1")) == director.SLOT_INDEX_ID
                and 1 <= (num(idx["inputs"].get("NUM2")) or 0) <= director.BOZA_CENTRE_OFFSET
            )

        if not (len(cascade) == director.BOZA_CENTRE_OFFSET and all(indexed_by_offset(x) for x in cascade)):
            failures.add("boza-centre-cascades")
        # (10) CENTRE also craters PERSISTENTLY on HIT: advances the crater clock and scrolls.
        if not (advances_clock(centre_ids) and calls(centre_ids, director.ADVANCE_GROUND_PROCCODE)):
            failures.add("boza-centre-craters")

        # ---- Spawn facts (add_ground_object consume, under the BOZA_LOGRAM_TYPE gate) ----
        boza_spawn = set()
        for b in spawn_body:
            if b["opcode"] == "control_if":
                c = rref(b["inputs"].get("CONDITION"))
                if c is not None and c["opcode"] == "operator_equals" and num(c["inputs"].get("OPERAND2")) == director.BOZA_LOGRAM_TYPE:
                    boza_spawn = branch_ids(b, "SUBSTACK")
                    break

        def spawn_writes(list_id, value):
            return [
                x
                for x in boza_spawn
                if blocks[x]["opcode"] == "data_replaceitemoflist"
                and blocks[x]["fields"]["LIST"][1] == list_id
                and num(blocks[x]["inputs"].get("ITEM")) == value
            ]

        # (11) FIVE ACTIVE SLOTS.
        if len(spawn_writes(director.SLOT_STATE_ID, director.SLOT_ACTIVE)) != director.BOZA_SLOT_COUNT:
            failures.add("boza-five-slots")
        # (12) THE FOUR OUTERS STAMPED 300 PTS.
        if len(spawn_writes(director.SLOT_PTS_ID, director.BOZA_OUTER_PTS)) != director.BOZA_CENTRE_OFFSET:
            failures.add("boza-outer-awards-300")
        # (13) THE CENTRE STAMPED 2,000 PTS.
        if len(spawn_writes(director.SLOT_PTS_ID, director.BOZA_CENTRE_PTS)) != 1:
            failures.add("boza-centre-awards-2000")
        # (14) THE LINK STAMPS: five `slot link` writes, exactly one 0 (the centre marker; the four outers link
        # to the non-zero centre index).
        link_writes = [
            x
            for x in boza_spawn
            if blocks[x]["opcode"] == "data_replaceitemoflist"
            and blocks[x]["fields"]["LIST"][1] == director.SLOT_LINK_ID
        ]
        if len(link_writes) != director.BOZA_SLOT_COUNT or len([x for x in link_writes if num(blocks[x]["inputs"].get("ITEM")) == 0]) != 1:
            failures.add("boza-outer-links-centre")

        return failures

    # Roadmap closure evidence for leaf `area.ground-dispatch` (AREA-02): the terrain-locked ground
    # substrate — `advance ground` scrolls every ground object DOWN the field by the fixed terrain step and
    # culls it off the bottom; the ordered walk routes each built ground type to its wrapper; and
    # add_ground_object spawns the built families (Barra/Garu/Logram) at the top of the field, ACTIVE,
    # scoped so no unbuilt type stamps a slot. The live proof (a scheduled area spawns the built families
    # into the ground band and they scroll 32/tick) is the harness `ground-dispatch-spawns-scoped` /
    # `ground-object-scrolls-with-terrain`.
    # roadmap-evidence: AREA-02 success  (test_ground_dispatch_authoring_present — advance ground warp, terrain-locked scroll, off-field cull, walk routes barra/garu/logram, spawns at top-of-field scoped to built types)
    # roadmap-evidence: AREA-02 failure  (test_ground_dispatch_negative_fixtures — each contract clause corrupted bites)
    def test_ground_dispatch_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd_dispatch_failures(project))

    def test_ground_dispatch_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd_dispatch_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def unwarp(p, proccode):
            for b in _stage(p)["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == proccode
                ):
                    b["mutation"]["warp"] = "false"

        def rename_call(p, proccode, proc_from, proc_to, once=True):
            done = 0
            for b in _proc_body_blocks(_stage(p), proccode):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == proc_from
                ):
                    b["mutation"]["proccode"] = proc_to
                    done += 1
                    if once:
                        break
            return done

        def zero_scroll(p):
            # Zero the terrain step in `advance ground`'s slot-x add (AREA_PROGRESS_STEP -> 0): the object
            # no longer scrolls, so the terrain-lock clause bites.
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_GROUND_PROCCODE):
                if not (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_X_ID
                ):
                    continue
                item = _stage(p)["blocks"].get(b["inputs"].get("ITEM", [None, None])[1])
                if item is None or item["opcode"] != "operator_add":
                    continue
                n2 = item["inputs"].get("NUM2")
                if isinstance(n2, list) and isinstance(n2[1], list) and int(n2[1][1]) == director.AREA_PROGRESS_STEP:
                    item["inputs"]["NUM2"] = [1, [4, "0"]]

        def drop_cull(p):
            rename_call(p, director.ADVANCE_GROUND_PROCCODE, director.CULL_SLOT_PROCCODE, "noop")

        def drop_logram_dispatch(p):
            rename_call(p, director.ADVANCE_SLOTS_PROCCODE, director.UPDATE_LOGRAM_PROCCODE, "noop")

        def move_spawn_off_top(p):
            # Every ground-spawn `slot x = 0` write (Barra + Garu base) becomes non-zero, so the object no
            # longer spawns at the top of the field: the top-of-field clause bites.
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_X_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], list)
                    and str(b["inputs"]["ITEM"][1][1]) == "0"
                ):
                    b["inputs"]["ITEM"] = [1, [4, "99"]]

        def unscope_garu(p):
            # Flip the Garu spawn's `ground type == GARU_BARRA_TYPE` gate to a value the column never holds
            # -> the Garu branch is no longer scoped, so the built-types scoping clause bites.
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = _stage(p)["blocks"].get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.GROUND_OBJECT_TYPE_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and str(b["inputs"]["OPERAND2"][1][1]) == str(director.GARU_BARRA_TYPE)
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "199"]]

        cases = [
            ("advance-ground-warp", lambda p: unwarp(p, director.ADVANCE_GROUND_PROCCODE)),
            ("advance-ground-scrolls-terrain", zero_scroll),
            ("advance-ground-culls-off-field", drop_cull),
            ("dispatch-routes-ground-types", drop_logram_dispatch),
            ("spawn-stamps-top-of-field", move_spawn_off_top),
            ("spawn-scoped-to-built-types", unscope_garu),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd_dispatch_failures(project), label)

    # Roadmap closure evidence for leaf `economy.ground-awards` (ECO-01): the bomb-vs-ground detector
    # `check ground hit` sweeps the 16 ground slots against the locked bomb target and scores each ACTIVE
    # object on target once through the shared `resolve hit` -> `score` path by its value-table entry, and
    # zeroes its crater clock — the ACTIVE gate making the Garu base indestructible for free. The live proof
    # (a bomb on an active Barra scores exactly 100; the window is bounded) is the harness
    # `bomb-kills-ground-and-scores` / `bomb-ground-window-bounded`. This same detector is the ground-HIT
    # resolution reached by the bomb's own finish (WPN-05), proven live by `bomb-finish-resolves-ground`.
    # roadmap-evidence: ECO-01 success  (test_ground_awards_authoring_present — check ground hit warp, sweeps 16 active-only slots, scores through the shared value-table path, resets the crater clock)
    # roadmap-evidence: ECO-01 failure  (test_ground_awards_negative_fixtures — each contract clause corrupted bites)
    # roadmap-evidence: WPN-05 success  (test_ground_awards_authoring_present ground-hit resolution; harness bomb-finish-resolves-ground reaches it from the bomb finish)
    # roadmap-evidence: WPN-05 failure  (test_ground_awards_negative_fixtures scores-through-shared-path; harness bomb-finish-resolves-ground negative neutralizes `check ground hit`)
    def test_ground_awards_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd_awards_failures(project))

    def test_ground_awards_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd_awards_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def unwarp(p):
            for b in _stage(p)["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.CHECK_GROUND_HIT_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def drop_one_active_gate(p):
            # Flip the FIRST `slot state[s] == SLOT_ACTIVE` gate to the Garu-base sentinel so that slot drops
            # out of the swept active set: the 16-active-slots clause bites (and an indestructible base would
            # score there).
            st = _stage(p)
            for b in _proc_body_blocks(st, director.CHECK_GROUND_HIT_PROCCODE):
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = st["blocks"].get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and str(b["inputs"]["OPERAND2"][1][1]) == str(director.SLOT_ACTIVE)
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, str(director.SLOT_GARU_BASE)]]
                    return

        def drop_resolve(p):
            for b in _proc_body_blocks(_stage(p), director.CHECK_GROUND_HIT_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.RESOLVE_HIT_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def misread_award_source(p):
            # Repoint the value-table read off VALUE_TABLE_ID (award no longer sourced from the value table
            # by `slot pts`) while leaving `resolve hit` intact -> severs the OTHER half of
            # scores-through-shared-path, which drop_resolve does not reach.
            for b in _proc_body_blocks(_stage(p), director.CHECK_GROUND_HIT_PROCCODE):
                if (
                    b["opcode"] == "data_itemoflist"
                    and b["fields"]["LIST"][1] == director.VALUE_TABLE_ID
                ):
                    b["fields"]["LIST"] = ["slot pts", director.SLOT_PTS_ID]

        def keep_stale_clock(p):
            # Change every `slot timer = 0` reset in the detector to non-zero, so the crater clock is not
            # zeroed on the hit: the crater-clock-reset clause bites.
            for b in _proc_body_blocks(_stage(p), director.CHECK_GROUND_HIT_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], list)
                    and str(b["inputs"]["ITEM"][1][1]) == "0"
                ):
                    b["inputs"]["ITEM"] = [1, [4, "5"]]

        cases = [
            ("check-ground-hit-warp", unwarp),
            ("sweeps-16-active-ground-slots", drop_one_active_gate),
            ("scores-through-shared-path", drop_resolve),
            ("scores-through-shared-path", misread_award_source),
            ("resets-crater-clock", keep_stale_clock),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd_awards_failures(project), label)

    # Roadmap closure evidence for leaf `ground.barra` (GND-01): the Barra is the passive terrain target —
    # it never fires, scrolls with the terrain, and once bombed craters PERSISTENTLY (never freed on its
    # clock, culled only off-field); the Garu Barra is one type over two slots — an indestructible base (a
    # non-ACTIVE sentinel the detector rejects) plus a destructible node that explode-and-removes (vanishes
    # at the burst end, no crater). The live proof (bomb craters + scrolls + scores 100; blaster cannot
    # destroy; node scores 300 and vanishes; base indestructible) is the harness
    # `barra-craters-persists-and-scrolls` / `barra-blaster-cannot-destroy` / `garu-node-scores-and-vanishes`
    # / `garu-base-is-indestructible`.
    # roadmap-evidence: GND-01 success  (test_barra_slice_authoring_present — barra/garu wrappers warp, barra never fires, craters persistently + scrolls, garu node explode-and-removes, garu base indestructible sentinel, barra awards 100)
    # roadmap-evidence: GND-01 failure  (test_barra_slice_negative_fixtures — each contract clause corrupted bites)
    def test_barra_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd01_failures(project))

    def test_barra_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd01_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def unwarp_barra(p):
            for b in _stage(p)["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_BARRA_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def make_barra_fire(p):
            # Repurpose an `advance ground` call in `update barra` into a bullet allocation -> the Barra now
            # fires: the never-fires clause bites (the other advance-ground call keeps it scrolling).
            for b in _proc_body_blocks(_stage(p), director.UPDATE_BARRA_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.ALLOC_BULLET_PROCCODE
                    return

        def freeze_barra_clock(p):
            # Pin `update barra`'s crater-clock write to a literal (no `slot timer + step`) -> the crater
            # clock never advances: the craters-and-scrolls clause bites.
            for b in _proc_body_blocks(_stage(p), director.UPDATE_BARRA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                ):
                    b["inputs"]["ITEM"] = [1, [4, "0"]]

        def barra_culls_on_clock(p):
            # Turn an `advance ground` call in `update barra` into a `cull slot` -> the Barra now frees itself
            # inside its own wrapper (the flying explode-tick shape): the crater-persists clause bites.
            for b in _proc_body_blocks(_stage(p), director.UPDATE_BARRA_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.CULL_SLOT_PROCCODE
                    return

        def garu_never_removes(p):
            # Drop the node's bounded `cull slot` in `update garu` -> a bombed node never vanishes: the
            # explode-and-removes clause bites.
            for b in _proc_body_blocks(_stage(p), director.UPDATE_GARU_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def garu_base_active(p):
            # Stamp the Garu base ACTIVE (not the SLOT_GARU_BASE sentinel) -> the detector would score the
            # indestructible outer: the indestructible-base clause bites.
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], list)
                    and str(b["inputs"]["ITEM"][1][1]) == str(director.SLOT_GARU_BASE)
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.SLOT_ACTIVE)]]

        def wrong_barra_pts(p):
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], list)
                    and str(b["inputs"]["ITEM"][1][1]) == str(director.BARRA_PTS)
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.BARRA_PTS + 1)]]

        cases = [
            ("barra-garu-procs-warp", unwarp_barra),
            ("barra-never-fires", make_barra_fire),
            ("barra-craters-and-scrolls", freeze_barra_clock),
            ("barra-crater-persists", barra_culls_on_clock),
            ("garu-node-explodes-and-removes", garu_never_removes),
            ("garu-base-indestructible", garu_base_active),
            ("barra-awards-100", wrong_barra_pts),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd01_failures(project), label)

    # Roadmap closure evidence for leaf `ground.logram` (GND-03): the Logram opens and closes its dome on
    # the arcade cadence while armed high on the field, fires EXACTLY ONE aimed bullet directly at the
    # fully-open midpoint, re-rolls a fresh masked-random wait at the recycle stage, and once bombed craters
    # persistently like the Barra. The live proof (fires once per cycle at full-open; craters when bombed)
    # is the harness `logram-fires-once-at-full-open` / `logram-craters-when-bombed`.
    # roadmap-evidence: GND-03 success  (test_logram_slice_authoring_present — update logram warp, fires one direct aimed bullet at full-open, arm/cadence gated, dome triangle, recycles at stage 7, craters persistently on hit)
    # roadmap-evidence: GND-03 failure  (test_logram_slice_negative_fixtures — each contract clause corrupted bites)
    def test_logram_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd03_failures(project))

    def test_logram_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd03_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def body(p):
            return _proc_body_blocks(_stage(p), director.UPDATE_LOGRAM_PROCCODE)

        def unwarp(p):
            for b in _stage(p)["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_LOGRAM_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def move_fire_off_midpoint(p):
            # Flip both `slot fire timer == LOGRAM_FIRE_TIMER` fire guards to a value the timer never holds
            # -> the aimed shot is no longer gated at the fully-open midpoint: the fires-at-full-open clause
            # bites (mirrors the harness changeListItemEqualsOperand negative).
            st = _stage(p)
            for b in body(p):
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = st["blocks"].get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                if (
                    lhs is not None
                    and lhs["opcode"] == "data_itemoflist"
                    and lhs["fields"]["LIST"][1] == director.SLOT_FIRE_TIMER_ID
                    and isinstance(b["inputs"].get("OPERAND2"), list)
                    and isinstance(b["inputs"]["OPERAND2"][1], list)
                    and str(b["inputs"]["OPERAND2"][1][1]) == str(director.LOGRAM_FIRE_TIMER)
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "999"]]

        def add_fire_gate(p):
            for b in body(p):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.FIRE_GATE_PROCCODE
                    return

        def break_arming(p):
            # Replace the arming gate's `ground stop firing row` variable operand with a number -> the gate
            # no longer references the row, so the arming clause bites (the Logram would fire off-field).
            for b in body(p):
                if b["opcode"] != "operator_gt":
                    continue
                o2 = b["inputs"].get("OPERAND2")
                if (
                    isinstance(o2, list)
                    and len(o2) >= 2
                    and isinstance(o2[1], list)
                    and len(o2[1]) >= 3
                    and o2[1][0] == 12
                    and o2[1][2] == director.GROUND_STOP_FIRING_ROW_ID
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "999"]]
                    return

        def break_cadence(p):
            # Change the cadence `tick mod FIRE_GATE_PHASE_TICKS` divisor -> the every-8th-frame phase is
            # gone, so the cadence clause bites.
            st = _stage(p)
            for b in body(p):
                if b["opcode"] != "operator_mod":
                    continue
                n1 = b["inputs"].get("NUM1")
                is_tick = (
                    isinstance(n1, list)
                    and len(n1) >= 2
                    and isinstance(n1[1], list)
                    and len(n1[1]) >= 3
                    and n1[1][0] == 12
                    and n1[1][2] == director.TICK_ID
                )
                if is_tick:
                    b["inputs"]["NUM2"] = [1, [4, "999"]]
                    return

        def break_dome(p):
            # Change the dome-triangle base OPEN_FRAME_COUNT -> the `slot code` ordinal no longer forms the
            # open->peak->close triangle, so the dome clause bites.
            st = _stage(p)
            for b in body(p):
                if not (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_CODE_ID
                ):
                    continue
                itm = b["inputs"].get("ITEM")
                ref = itm[1] if isinstance(itm, list) and len(itm) >= 2 and isinstance(itm[1], str) else None
                item = st["blocks"].get(ref) if ref else None
                if item is None or item["opcode"] != "operator_subtract":
                    continue
                n1 = item["inputs"].get("NUM1")
                if isinstance(n1, list) and isinstance(n1[1], list) and str(n1[1][1]) == str(director.LOGRAM_OPEN_FRAME_COUNT):
                    # animate_step() is built fresh twice, so there are two dome writes; the guard's `any`
                    # would still see an intact one — break every dome-triangle write, not just the first.
                    item["inputs"]["NUM1"] = [1, [4, "0"]]

        def break_recycle(p):
            # Flip the `stage == LOGRAM_RECYCLE_STAGE` gate off -> the cycle never re-rolls its wait, so the
            # recycle clause bites.
            st = _stage(p)
            for b in body(p):
                if b["opcode"] != "operator_equals":
                    continue
                o1 = b["inputs"].get("OPERAND1")
                lhs = st["blocks"].get(o1[1]) if isinstance(o1, list) and len(o1) >= 2 and isinstance(o1[1], str) else None
                o2 = b["inputs"].get("OPERAND2")
                if (
                    lhs is not None
                    and lhs["opcode"] == "operator_mod"
                    and isinstance(o2, list)
                    and isinstance(o2[1], list)
                    and str(o2[1][1]) == str(director.LOGRAM_RECYCLE_STAGE)
                ):
                    # animate_step() is built fresh twice — flip every recycle gate, not just the first,
                    # or the guard's `any` still sees an intact one.
                    b["inputs"]["OPERAND2"] = [1, [4, "99"]]

        def freeze_hit_clock(p):
            for b in body(p):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                ):
                    b["inputs"]["ITEM"] = [1, [4, "0"]]

        def logram_culls_on_clock(p):
            for b in body(p):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.CULL_SLOT_PROCCODE
                    return

        cases = [
            ("logram-warp", unwarp),
            ("logram-fires-at-full-open", move_fire_off_midpoint),
            ("logram-fires-directly", add_fire_gate),
            ("logram-arming-gated", break_arming),
            ("logram-cadence-gated", break_cadence),
            ("logram-dome-triangle", break_dome),
            ("logram-recycles-at-stage-7", break_recycle),
            ("logram-hit-craters", freeze_hit_clock),
            ("logram-crater-persists", logram_culls_on_clock),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd03_failures(project), label)

    # Roadmap closure evidence for leaf `ground.barra-variants` (GND-01.variants, #84): the Barra family's
    # only variant object codes at the pin are base Barra 0x1E and Garu Barra 0x20 (src/xevious_main.68k
    # 2644-2852 — there is no other Barra-family code), and the Garu Barra was already built and delivered in
    # slice 9 (PR #128, record GND-01). This leaf is closed as ALREADY-DELIVERED: the variant contract is the
    # two-slot Garu Barra authoring the `_gnd01_failures` guard already pins (indestructible SLOT_GARU_BASE
    # base + destructible node that explode-and-removes). The live proof is the slice-9 harness
    # `garu-node-scores-and-vanishes` / `garu-base-is-indestructible`. No new gameplay code is added for #84.
    # roadmap-evidence: GND-01 success  (test_barra_variants_closure_present — the delivered Garu Barra variant authoring is intact: node explode-and-removes, base indestructible sentinel)
    # roadmap-evidence: GND-01 failure  (test_barra_variants_closure_negative — corrupting the Garu variant authoring bites)
    def test_barra_variants_closure_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        # GND-01.variants closes against the already-built Garu Barra: the variant clauses of the GND-01
        # guard hold on the delivered project.
        failures = self._gnd01_failures(project)
        self.assertNotIn("garu-node-explodes-and-removes", failures)
        self.assertNotIn("garu-base-indestructible", failures)

    def test_barra_variants_closure_negative(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd01_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def garu_never_removes(p):
            for b in _proc_body_blocks(_stage(p), director.UPDATE_GARU_PROCCODE):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def garu_base_active(p):
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], list)
                    and str(b["inputs"]["ITEM"][1][1]) == str(director.SLOT_GARU_BASE)
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.SLOT_ACTIVE)]]

        cases = [
            ("garu-node-explodes-and-removes", garu_never_removes),
            ("garu-base-indestructible", garu_base_active),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd01_failures(project), label)

    # Roadmap closure evidence for leaf `ground.zolbak` (GND-02, #85): the Zolbak is the Barra crater model
    # with one extra behaviour — it never fires, scores 200 through the shared ground detector, craters
    # persistently on a bomb, and on the first HIT tick reduces the adaptive enemy AI level by 2, floored at
    # 0. The live proof (bomb craters + scores 200 + drops the AI level once, floored at 0) is the harness
    # `zolbak-craters-and-reduces-ai` / `zolbak-ai-reduction-floors-at-zero`.
    # roadmap-evidence: GND-02 success  (test_zolbak_slice_authoring_present — update zolbak warp, never fires, craters persistently + scrolls, reduces the AI level by 2 once per kill, floored at 0, awards 200)
    # roadmap-evidence: GND-02 failure  (test_zolbak_slice_negative_fixtures — each contract clause corrupted bites)
    def test_zolbak_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd02_failures(project))

    def test_zolbak_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd02_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def body(p):
            return _proc_body_blocks(_stage(p), director.UPDATE_ZOLBAK_PROCCODE)

        def unwarp(p):
            for b in _stage(p)["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_ZOLBAK_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def make_zolbak_fire(p):
            # Turn one `advance ground` call in `update zolbak` into a bullet allocation -> the Zolbak now
            # fires: the never-fires clause bites (the other advance-ground keeps it scrolling).
            for b in body(p):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.ALLOC_BULLET_PROCCODE
                    return

        def freeze_clock(p):
            for b in body(p):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                ):
                    b["inputs"]["ITEM"] = [1, [4, "0"]]

        def culls_on_clock(p):
            for b in body(p):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.CULL_SLOT_PROCCODE
                    return

        def weaken_drop(p):
            # Change the `-2` AI-level drop to 0 -> the reduction no longer eases pressure: the reduces-ai
            # clause bites (mirrors the harness freezeVariableChange negative).
            for b in body(p):
                if (
                    b["opcode"] == "data_changevariableby"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.AI_LEVEL_ID
                ):
                    b["inputs"]["VALUE"] = [1, [4, "0"]]

        def unfloor(p):
            # Change the `set ai level = 0` clamp to a nonzero literal -> the underflow is no longer floored:
            # the ai-floored clause bites (mirrors the harness pinVariableSet negative).
            for b in body(p):
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.AI_LEVEL_ID
                ):
                    b["inputs"]["VALUE"] = [1, [4, "9"]]

        def wrong_pts(p):
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], list)
                    and str(b["inputs"]["ITEM"][1][1]) == str(director.ZOLBAK_PTS)
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.ZOLBAK_PTS + 1)]]

        cases = [
            ("zolbak-warp", unwarp),
            ("zolbak-never-fires", make_zolbak_fire),
            ("zolbak-craters-and-scrolls", freeze_clock),
            ("zolbak-crater-persists", culls_on_clock),
            ("zolbak-reduces-ai", weaken_drop),
            ("zolbak-ai-floored", unfloor),
            ("zolbak-awards-200", wrong_pts),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd02_failures(project), label)

    # Roadmap closure evidence for leaf `ground.derota` (GND-04, #86): the Derota is a plain periodic aimed
    # turret (1000 pts) that fires the shared gate only while armed above the stop-firing row and craters when
    # bombed; the Garu Derota is the Garu Barra's two-slot shape (2000 pts) whose node fires the shared gate
    # unconditionally of the row and explode-and-removes, while its base is indestructible. The live proof is
    # the harness `derota-fires-when-armed-silent-past-stop-row` / `derota-craters-when-bombed` /
    # `garu-derota-base-indestructible` / `garu-derota-node-fires-scores-and-vanishes`.
    # roadmap-evidence: GND-04 success  (test_derota_slice_authoring_present — derota warp, fires via the shared gate arm-gated on the stop-firing row, craters persistently, awards 1000; garu derota warp, node fires unconditionally under state==ACTIVE, explode-and-removes, base indestructible, node awards 2000)
    # roadmap-evidence: GND-04 failure  (test_derota_slice_negative_fixtures — each contract clause corrupted bites)
    def test_derota_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd04_failures(project))

    def test_derota_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd04_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def dbody(p):
            return _proc_body_blocks(_stage(p), director.UPDATE_DEROTA_PROCCODE)

        def gbody(p):
            return _proc_body_blocks(_stage(p), director.UPDATE_GARU_DEROTA_PROCCODE)

        def unwarp(proccode):
            def _do(p):
                for b in _stage(p)["blocks"].values():
                    if (
                        b["opcode"] == "procedures_prototype"
                        and b.get("mutation", {}).get("proccode") == proccode
                    ):
                        b["mutation"]["warp"] = "false"

            return _do

        def neutralize_fire(body_fn):
            def _do(p):
                for b in body_fn(p):
                    if (
                        b["opcode"] == "procedures_call"
                        and b.get("mutation", {}).get("proccode") == director.FIRE_GATE_PROCCODE
                    ):
                        b["mutation"]["proccode"] = "noop"
                        return

            return _do

        def break_arming(p):
            # Replace the arm gate's `ground stop firing row` variable operand with a literal -> the fire is
            # no longer row-gated, so the arm clause bites (mirrors the Logram break_arming negative).
            for b in dbody(p):
                if b["opcode"] != "operator_gt":
                    continue
                o2 = b["inputs"].get("OPERAND2")
                if (
                    isinstance(o2, list)
                    and len(o2) >= 2
                    and isinstance(o2[1], list)
                    and len(o2[1]) >= 3
                    and o2[1][0] == 12
                    and o2[1][2] == director.GROUND_STOP_FIRING_ROW_ID
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "999"]]
                    return

        def freeze_clock(body_fn):
            def _do(p):
                for b in body_fn(p):
                    if (
                        b["opcode"] == "data_replaceitemoflist"
                        and b["fields"]["LIST"][1] == director.SLOT_TIMER_ID
                    ):
                        b["inputs"]["ITEM"] = [1, [4, "0"]]

            return _do

        def culls_on_clock(p):
            for b in dbody(p):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.CULL_SLOT_PROCCODE
                    return

        def wrong_pts(target_pts):
            def _do(p):
                # Corrupt EVERY spawn write of this value-table position, not just the first. Two reasons:
                # (1) _proc_body_blocks returns blocks in set-iteration order, so "first match" is not source
                # order; (2) as of slice 13 the Garu Derota node and the Boza centre share value-table
                # position 19 (both 2,000 pts), so a single-match mutation could corrupt the boza centre and
                # leave the garu-derota node intact, and this negative would not bite. Corrupting all writes
                # of the value is order-independent and still guarantees the garu-derota node is corrupted;
                # _gnd04_failures inspects only the derota/garu-derota writes, so the boza centre is moot here.
                for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                    if (
                        b["opcode"] == "data_replaceitemoflist"
                        and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                        and isinstance(b["inputs"].get("ITEM"), list)
                        and isinstance(b["inputs"]["ITEM"][1], list)
                        and str(b["inputs"]["ITEM"][1][1]) == str(target_pts)
                    ):
                        b["inputs"]["ITEM"] = [1, [4, str(target_pts + 1)]]

            return _do

        def garu_never_removes(p):
            for b in gbody(p):
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def garu_base_active(p):
            # Flip the Garu Derota base sentinel (the SLOT_GARU_BASE stamp under the GARU_DEROTA_TYPE gate) to
            # ACTIVE -> the indestructible outer would score: the base-indestructible clause bites. The Garu
            # Barra base (under GARU_BARRA_TYPE) is left untouched, so only the Garu Derota base is affected.
            blocks = _stage(p)["blocks"]

            def under_derota_gate(bid):
                cur = blocks.get(bid)
                while cur is not None:
                    parent = blocks.get(cur.get("parent")) if cur.get("parent") else None
                    if parent is not None and parent["opcode"] in ("control_if", "control_if_else"):
                        cond = parent["inputs"].get("CONDITION")
                        cb = (
                            blocks.get(cond[1])
                            if isinstance(cond, list) and len(cond) >= 2 and isinstance(cond[1], str)
                            else None
                        )
                        if (
                            cb is not None
                            and cb["opcode"] == "operator_equals"
                            and _num_operand(cb["inputs"].get("OPERAND2")) == director.GARU_DEROTA_TYPE
                        ):
                            return True
                    cur = parent
                return False

            id_of = {id(b): bid for bid, b in blocks.items()}
            for b in _proc_body_blocks(_stage(p), director.ADVANCE_AREA_PROCCODE):
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and isinstance(b["inputs"].get("ITEM"), list)
                    and isinstance(b["inputs"]["ITEM"][1], list)
                    and str(b["inputs"]["ITEM"][1][1]) == str(director.SLOT_GARU_BASE)
                    and under_derota_gate(id_of[id(b)])
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.SLOT_ACTIVE)]]
                    return

        cases = [
            ("derota-warp", unwarp(director.UPDATE_DEROTA_PROCCODE)),
            ("derota-fires-via-gate", neutralize_fire(dbody)),
            ("derota-fire-arm-gated", break_arming),
            ("derota-hit-craters", freeze_clock(dbody)),
            ("derota-crater-persists", culls_on_clock),
            ("derota-awards-1000", wrong_pts(director.DEROTA_PTS)),
            ("garu-derota-warp", unwarp(director.UPDATE_GARU_DEROTA_PROCCODE)),
            ("garu-derota-node-fires", neutralize_fire(gbody)),
            ("garu-derota-node-removes", garu_never_removes),
            ("garu-derota-base-indestructible", garu_base_active),
            ("garu-derota-node-awards-2000", wrong_pts(director.GARU_DEROTA_PTS)),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd04_failures(project), label)

    # Roadmap closure evidence for leaf ground.boza-logram (GND-05, #87): the five-slot Boza Logram composite
    # (handle_2D_Boza_Logram) — four outer domes that each behave as a lone Logram (arm/cadence-gated
    # open/close cycle, one direct aimed shot at the full-open midpoint, persistent crater on bomb, 300 pts)
    # and, on hit, downgrade the shared centre's value to 600; plus a centre that never fires, scores 2,000,
    # craters persistently, and on hit cascades — setting all four outer slots to HIT directly, which bypasses
    # the award sweep so a centre-first bomb clears the outers for free (only a directly-bombed outer scores).
    # The live proof is the harness boza scenarios (an outer scores 300 and downgrades the centre to 600; the
    # centre scores 2,000 and its cascade clears the outers for no score).
    # roadmap-evidence: GND-05 success  (test_boza_slice_authoring_present — boza warp; the link discriminator splits centre/outer; outers fire the direct aimed shot arm-gated on the stop-firing row, crater persistently, and downgrade the linked centre to 600; the centre never fires, craters persistently, and cascades all four outers to HIT directly; spawn stamps five ACTIVE slots — four outers at 300 linked to the centre, the centre at 2,000 with link 0)
    # roadmap-evidence: GND-05 failure  (test_boza_slice_negative_fixtures — each contract clause corrupted bites)
    def test_boza_slice_authoring_present(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd05_failures(project))

    def test_boza_slice_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._gnd05_failures(base))

        def _stage(p):
            return next(t for t in p["targets"] if t["isStage"])

        def _rref(blocks, inp):
            r = inp[1] if isinstance(inp, list) and len(inp) >= 2 and isinstance(inp[1], str) else None
            return blocks.get(r) if r else None

        def _reach(blocks, start):
            seen, frontier = set(), [start]
            while frontier:
                x = frontier.pop()
                if not x or x in seen or x not in blocks:
                    continue
                seen.add(x)
                b = blocks[x]
                frontier.append(b.get("next"))
                for v in b.get("inputs", {}).values():
                    if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                        frontier.append(v[1])
            return seen

        def _branches(p):
            # (blocks, centre-branch ids, outer-branch ids) for the `slot link == 0` discriminator.
            stage = _stage(p)
            blocks = stage["blocks"]
            top = None
            for b in _proc_body_blocks(stage, director.UPDATE_BOZA_PROCCODE):
                if b["opcode"] == "control_if_else":
                    c = _rref(blocks, b["inputs"].get("CONDITION"))
                    if (
                        c is not None
                        and c["opcode"] == "operator_equals"
                        and (lhs := _rref(blocks, c["inputs"].get("OPERAND1"))) is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == director.SLOT_LINK_ID
                        and _num_operand(c["inputs"].get("OPERAND2")) == 0
                    ):
                        top = b
                        break

            def side(key):
                sub = top["inputs"].get(key) if top else None
                start = sub[1] if isinstance(sub, list) and len(sub) >= 2 and isinstance(sub[1], str) else None
                return _reach(blocks, start)

            return blocks, side("SUBSTACK"), side("SUBSTACK2")

        def _boza_spawn(p):
            stage = _stage(p)
            blocks = stage["blocks"]
            for b in _proc_body_blocks(stage, director.ADVANCE_AREA_PROCCODE):
                if b["opcode"] == "control_if":
                    c = _rref(blocks, b["inputs"].get("CONDITION"))
                    if (
                        c is not None
                        and c["opcode"] == "operator_equals"
                        and _num_operand(c["inputs"].get("OPERAND2")) == director.BOZA_LOGRAM_TYPE
                    ):
                        sub = b["inputs"].get("SUBSTACK")
                        start = sub[1] if isinstance(sub, list) and len(sub) >= 2 and isinstance(sub[1], str) else None
                        return blocks, _reach(blocks, start)
            return blocks, set()

        def unwarp(p):
            for b in _stage(p)["blocks"].values():
                if (
                    b["opcode"] == "procedures_prototype"
                    and b.get("mutation", {}).get("proccode") == director.UPDATE_BOZA_PROCCODE
                ):
                    b["mutation"]["warp"] = "false"

        def break_link_branch(p):
            # Change the discriminator from `slot link == 0` to `== 99`: the centre/outer split no longer keys
            # on the link, so the branch-shape clause bites.
            stage = _stage(p)
            blocks = stage["blocks"]
            for b in _proc_body_blocks(stage, director.UPDATE_BOZA_PROCCODE):
                if b["opcode"] == "control_if_else":
                    c = _rref(blocks, b["inputs"].get("CONDITION"))
                    if (
                        c is not None
                        and c["opcode"] == "operator_equals"
                        and (lhs := _rref(blocks, c["inputs"].get("OPERAND1"))) is not None
                        and lhs["opcode"] == "data_itemoflist"
                        and lhs["fields"]["LIST"][1] == director.SLOT_LINK_ID
                        and _num_operand(c["inputs"].get("OPERAND2")) == 0
                    ):
                        c["inputs"]["OPERAND2"] = [1, [4, "99"]]
                        return

        def neutralize_outer_fire(p):
            # The dome fires from BOTH the WAIT->ANIMATE fall-through and the steady ANIMATE branch, so neutralize
            # every `alloc bullet slot` call in the outer branch, not just the first.
            blocks, _centre, outer = _branches(p)
            for x in outer:
                b = blocks[x]
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = "noop"

        def break_arming(p):
            # Replace the arm gate's `ground stop firing row` operand with a literal -> the fire is no longer
            # row-gated (mirrors the Derota break_arming negative).
            blocks, _centre, outer = _branches(p)
            for x in outer:
                b = blocks[x]
                if b["opcode"] != "operator_gt":
                    continue
                o2 = b["inputs"].get("OPERAND2")
                if (
                    isinstance(o2, list)
                    and len(o2) >= 2
                    and isinstance(o2[1], list)
                    and len(o2[1]) >= 3
                    and o2[1][0] == 12
                    and o2[1][2] == director.GROUND_STOP_FIRING_ROW_ID
                ):
                    b["inputs"]["OPERAND2"] = [1, [4, "999"]]
                    return

        def freeze_clock(side):
            def _do(p):
                blocks = _branches(p)[0]
                ids = _branches(p)[1] if side == "centre" else _branches(p)[2]
                for x in ids:
                    b = blocks[x]
                    if b["opcode"] != "data_replaceitemoflist" or b["fields"]["LIST"][1] != director.SLOT_TIMER_ID:
                        continue
                    item = b["inputs"].get("ITEM")
                    it = blocks.get(item[1]) if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], str) else None
                    if it is not None and it["opcode"] == "operator_add":
                        b["inputs"]["ITEM"] = [1, [4, "0"]]
                        return

            return _do

        def cull_outer(p):
            blocks, _centre, outer = _branches(p)
            for x in outer:
                b = blocks[x]
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.CULL_SLOT_PROCCODE
                    return

        def drop_downgrade(p):
            blocks, _centre, outer = _branches(p)
            for x in outer:
                b = blocks[x]
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_PTS_ID
                    and _num_operand(b["inputs"].get("ITEM")) == director.BOZA_CENTRE_DOWNGRADED_PTS
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.BOZA_CENTRE_PTS)]]
                    return

        def make_centre_fire(p):
            # Retarget a centre `advance ground` call to `alloc bullet slot`: the centre now fires -> bites.
            blocks, centre, _outer = _branches(p)
            for x in centre:
                b = blocks[x]
                if (
                    b["opcode"] == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ADVANCE_GROUND_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.ALLOC_BULLET_PROCCODE
                    return

        def break_cascade(p):
            # Flip one cascade `slot state = HIT` write to ACTIVE -> fewer than four outers cascade.
            blocks, centre, _outer = _branches(p)
            for x in centre:
                b = blocks[x]
                if (
                    b["opcode"] == "data_replaceitemoflist"
                    and b["fields"]["LIST"][1] == director.SLOT_STATE_ID
                    and _num_operand(b["inputs"].get("ITEM")) == director.SLOT_HIT
                ):
                    b["inputs"]["ITEM"] = [1, [4, str(director.SLOT_ACTIVE)]]
                    return

        def spawn_wrong(list_id, value, new_value):
            def _do(p):
                blocks, ids = _boza_spawn(p)
                for x in ids:
                    b = blocks[x]
                    if (
                        b["opcode"] == "data_replaceitemoflist"
                        and b["fields"]["LIST"][1] == list_id
                        and _num_operand(b["inputs"].get("ITEM")) == value
                    ):
                        b["inputs"]["ITEM"] = [1, [4, str(new_value)]]
                        return

            return _do

        cases = [
            ("boza-warp", unwarp),
            ("boza-link-branch", break_link_branch),
            ("boza-outer-fires", neutralize_outer_fire),
            ("boza-outer-arm-gated", break_arming),
            ("boza-outer-craters", freeze_clock("outer")),
            ("boza-outer-crater-persists", cull_outer),
            ("boza-outer-downgrades-centre", drop_downgrade),
            ("boza-centre-never-fires", make_centre_fire),
            ("boza-centre-cascades", break_cascade),
            ("boza-centre-craters", freeze_clock("centre")),
            ("boza-five-slots", spawn_wrong(director.SLOT_STATE_ID, director.SLOT_ACTIVE, director.SLOT_HIT)),
            ("boza-outer-awards-300", spawn_wrong(director.SLOT_PTS_ID, director.BOZA_OUTER_PTS, director.BOZA_OUTER_PTS + 1)),
            ("boza-centre-awards-2000", spawn_wrong(director.SLOT_PTS_ID, director.BOZA_CENTRE_PTS, director.BOZA_CENTRE_PTS + 1)),
            ("boza-outer-links-centre", spawn_wrong(director.SLOT_LINK_ID, 0, 1)),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._gnd05_failures(project), label)

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
        # The bomb-resolution path still resolves a hit through exactly one HIT write, inside `resolve hit`.
        # GND-05 adds one deliberate exception: the Boza centre, when bombed, cascades by setting its four
        # outer slots to HIT directly (faithful to the arcade `destroy_all_outer_lograms`, which bulk-sets the
        # outer state and bypasses the per-slot award path — this is exactly why a cascaded outer scores
        # nothing). Those cascade writes live inside UPDATE_BOZA's body. So the invariant is: exactly one HIT
        # write in the resolver, and every other HIT write on the Stage is a Boza-cascade write — nothing stray.
        boza_body_ids = {id(b) for b in _proc_body_blocks(stage, director.UPDATE_BOZA_PROCCODE)}
        resolver_hits = [bid for bid in hit_writes if bid in resolve_body]
        non_resolver_hits = [bid for bid in hit_writes if bid not in resolve_body]
        cascade_only = all(id(blocks[bid]) in boza_body_ids for bid in non_resolver_hits)
        if len(resolver_hits) != 1 or not cascade_only:
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
        """PLY-02 walk-driven player death (slice 8, replacing the retired D/G debug keys): contact
        raises `player hit` in the flying AND bullet updates; the walk's death check, gated on
        `player hit` = 1 AND `invuln` = 0, spends a craft, clears the flag, and runs the player-dead
        transition; and the death-complete handler decides respawn vs game over from the craft counter."""
        # roadmap-evidence: PLY-02 success  (test_death_decision_is_lives_driven — contact raises the hit flag, the invuln-gated death check spends a craft and transitions, the counter decides; harness death-respawn + death-game-over run it live)
        # roadmap-evidence: PLY-02 failure  (test_death_decision_negative_fixtures — each clause corrupted bites; harness death-respawn/death-game-over negatives remove the death edges)
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def reachable(start: str) -> set:
            seen, stack = set(), [start]
            while stack:
                bid = stack.pop()
                if not bid or bid in seen or bid not in blocks:
                    continue
                seen.add(bid)
                b = blocks[bid]
                stack.append(b.get("next"))
                for slot in ("SUBSTACK", "SUBSTACK2"):
                    val = b["inputs"].get(slot)
                    if isinstance(val, list) and len(val) > 1 and isinstance(val[1], str):
                        stack.append(val[1])
            return seen

        def raises_player_hit(proccode: str) -> bool:
            return any(
                b["opcode"] == "data_setvariableto"
                and b["fields"].get("VARIABLE", [None, None])[1] == director.PLAYER_HIT_ID
                and b["inputs"].get("VALUE") == [1, [4, 1]]
                for b in _proc_body_blocks(stage, proccode)
            )

        # Both live craft-collision participants raise the hit flag.
        if not raises_player_hit(director.UPDATE_TOROID_PROCCODE):
            failures.add("flying-raises-player-hit")
        if not raises_player_hit(director.UPDATE_BULLET_PROCCODE):
            failures.add("bullet-raises-player-hit")

        # The walk's death gate: a control_if whose CONDITION is an AND of (player hit == 1) and
        # (invuln == 0) — so contact kills only when not invulnerable.
        def equals_var(op_spec) -> str | None:
            if not (isinstance(op_spec, list) and len(op_spec) > 1 and isinstance(op_spec[1], str)):
                return None
            eq = blocks.get(op_spec[1])
            if not eq or eq["opcode"] != "operator_equals":
                return None
            lhs = eq["inputs"].get("OPERAND1")
            if isinstance(lhs, list) and len(lhs) > 1 and isinstance(lhs[1], list) and lhs[1][0] == 12:
                return lhs[1][2]
            return None

        death_if = None
        for bid, b in blocks.items():
            if b["opcode"] != "control_if":
                continue
            cond = b["inputs"].get("CONDITION")
            if not (isinstance(cond, list) and len(cond) > 1 and isinstance(cond[1], str)):
                continue
            cb = blocks.get(cond[1])
            if not cb or cb["opcode"] != "operator_and":
                continue
            refs = {equals_var(cb["inputs"].get("OPERAND1")), equals_var(cb["inputs"].get("OPERAND2"))}
            if {director.PLAYER_HIT_ID, director.INVULN_ID} <= refs:
                death_if = bid
                break
        if death_if is None:
            failures.add("death-gated-on-hit-and-invuln")
        else:
            body = reachable(death_if)
            spends = any(
                blocks[bid]["opcode"] == "data_changevariableby"
                and blocks[bid]["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID
                and blocks[bid]["inputs"].get("VALUE") == [1, [4, -1]]
                for bid in body
            )
            clears = any(
                blocks[bid]["opcode"] == "data_setvariableto"
                and blocks[bid]["fields"].get("VARIABLE", [None, None])[1] == director.PLAYER_HIT_ID
                and blocks[bid]["inputs"].get("VALUE") == [1, [4, 0]]
                for bid in body
            )
            transitions = any(
                blocks[bid]["opcode"] == "procedures_call"
                and blocks[bid].get("mutation", {}).get("proccode") == director.PROCCODE
                for bid in body
            )
            if not (spends and clears and transitions):
                failures.add("death-spends-craft-and-transitions")

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

        def break_flying_hit(p):
            # Neutralise the flying update's `set player hit = 1` (retarget to another flag) so a
            # Toroid on the craft's cell no longer registers a hit.
            s = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(s, director.UPDATE_TOROID_PROCCODE):
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.PLAYER_HIT_ID
                    and b["inputs"].get("VALUE") == [1, [4, 1]]
                ):
                    b["fields"]["VARIABLE"] = ["invuln", director.INVULN_ID]
                    return
            raise AssertionError("no `set player hit = 1` in update toroid to break")

        def break_bullet_hit(p):
            # Same, in the enemy-bullet update: a bullet on the craft's cell no longer registers.
            s = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(s, director.UPDATE_BULLET_PROCCODE):
                if (
                    b["opcode"] == "data_setvariableto"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.PLAYER_HIT_ID
                    and b["inputs"].get("VALUE") == [1, [4, 1]]
                ):
                    b["fields"]["VARIABLE"] = ["invuln", director.INVULN_ID]
                    return
            raise AssertionError("no `set player hit = 1` in update bullet to break")

        def break_death_gate(p):
            # Drop `invuln` from the death gate's AND so contact would kill even while invulnerable —
            # retarget the invuln equals-operand to the score, defeating the guard.
            s = next(t for t in p["targets"] if t["isStage"])
            blocks = s["blocks"]
            for b in blocks.values():
                if b["opcode"] != "operator_and":
                    continue
                for slot in ("OPERAND1", "OPERAND2"):
                    spec = b["inputs"].get(slot)
                    if not (isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], str)):
                        continue
                    eq = blocks.get(spec[1])
                    if not eq or eq["opcode"] != "operator_equals":
                        continue
                    lhs = eq["inputs"].get("OPERAND1")
                    if (
                        isinstance(lhs, list)
                        and len(lhs) > 1
                        and isinstance(lhs[1], list)
                        and lhs[1][0] == 12
                        and lhs[1][2] == director.INVULN_ID
                    ):
                        lhs[1][1:] = ["score", director.SCORE_ID]
                        return
            raise AssertionError("no invuln equals-operand in a death gate to break")

        def break_death_body(p):
            # Death registers but never spends a craft: neutralise the `change craft by -1` reachable
            # from the death gate.
            s = next(t for t in p["targets"] if t["isStage"])
            blocks = s["blocks"]

            def equals_var(op_spec):
                if not (isinstance(op_spec, list) and len(op_spec) > 1 and isinstance(op_spec[1], str)):
                    return None
                eq = blocks.get(op_spec[1])
                if not eq or eq["opcode"] != "operator_equals":
                    return None
                lhs = eq["inputs"].get("OPERAND1")
                if isinstance(lhs, list) and len(lhs) > 1 and isinstance(lhs[1], list) and lhs[1][0] == 12:
                    return lhs[1][2]
                return None

            death_if = None
            for bid, b in blocks.items():
                if b["opcode"] != "control_if":
                    continue
                cond = b["inputs"].get("CONDITION")
                if not (isinstance(cond, list) and len(cond) > 1 and isinstance(cond[1], str)):
                    continue
                cb = blocks.get(cond[1])
                if not cb or cb["opcode"] != "operator_and":
                    continue
                refs = {equals_var(cb["inputs"].get("OPERAND1")), equals_var(cb["inputs"].get("OPERAND2"))}
                if {director.PLAYER_HIT_ID, director.INVULN_ID} <= refs:
                    death_if = bid
                    break
            assert death_if is not None
            seen, stack = set(), [death_if]
            while stack:
                bid = stack.pop()
                if not bid or bid in seen or bid not in blocks:
                    continue
                seen.add(bid)
                b = blocks[bid]
                stack.append(b.get("next"))
                for slot in ("SUBSTACK", "SUBSTACK2"):
                    val = b["inputs"].get(slot)
                    if isinstance(val, list) and len(val) > 1 and isinstance(val[1], str):
                        stack.append(val[1])
            for bid in seen:
                b = blocks[bid]
                if (
                    b["opcode"] == "data_changevariableby"
                    and b["fields"].get("VARIABLE", [None, None])[1] == director.LIVES_ID
                    and b["inputs"].get("VALUE") == [1, [4, -1]]
                ):
                    b["inputs"]["VALUE"] = [1, [4, 0]]
                    return
            raise AssertionError("no `change craft by -1` in the death body to break")

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
            ("flying-raises-player-hit", break_flying_hit),
            ("bullet-raises-player-hit", break_bullet_hit),
            ("death-gated-on-hit-and-invuln", break_death_gate),
            ("death-spends-craft-and-transitions", break_death_body),
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

    @staticmethod
    def _live_pressure_failures(project: dict) -> set:
        """DIF-01/FORM-01/DIF-03 (.play): the live-pressure WIRING, structurally. The density chain's
        spawn refill repeats `formation count` times and fills only EMPTY flying slots (so a denser
        formation fills more slots), and the shared fire gate RELOADS the per-slot fire countdown from
        an expression reading the captured fire MASK (the DIF-03 fire-frequency mechanism — `rng mod
        (mask + 1)`). The exact table CORRESPONDENCE and the live dynamics are the scratch-vm harness
        scenarios' job (live-pressure-density / live-pressure-adaptive / terrazi-fires-under-mask);
        this only catches the wiring going missing."""
        failures = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def refs_var(spec, var_id):
            return (
                isinstance(spec, list)
                and len(spec) > 1
                and isinstance(spec[1], list)
                and spec[1][2:3] == [var_id]
            )

        def reads_list(bid, list_id, seen):
            if not isinstance(bid, str) or bid in seen or bid not in blocks:
                return False
            seen.add(bid)
            blk = blocks[bid]
            if (
                blk["opcode"] == "data_itemoflist"
                and blk["fields"].get("LIST", [None, None])[1] == list_id
            ):
                return True
            return any(
                isinstance(v, list) and len(v) > 1 and reads_list(v[1], list_id, seen)
                for v in blk["inputs"].values()
            )

        def body_ids(loop_id):
            sub = blocks[loop_id]["inputs"].get("SUBSTACK")
            out, bid = [], (sub[1] if isinstance(sub, list) and len(sub) > 1 else None)
            while bid:
                out.append(bid)
                bid = blocks[bid].get("next")
            return out

        # DIF-01/FORM-01 density -> spawn: the refill loop repeats `formation count` times, so a denser
        # formation (a larger table entry at the live AI-level index) fills more slots this tick.
        loop = next(
            (
                bid
                for bid, b in blocks.items()
                if b["opcode"] == "control_repeat"
                and refs_var(b["inputs"].get("TIMES"), director.FORMATION_COUNT_ID)
            ),
            None,
        )
        if loop is None:
            failures.add("spawn-loop-times-formation-count")
        else:
            ids = body_ids(loop)
            # FORM-01: the refill only fills EMPTY slots — a control_if in the loop body whose
            # condition compares the current slot's type (== 0).
            if not any(
                blocks[x]["opcode"] == "control_if"
                and isinstance(blocks[x]["inputs"].get("CONDITION"), list)
                and len(blocks[x]["inputs"]["CONDITION"]) > 1
                and blocks[blocks[x]["inputs"]["CONDITION"][1]]["opcode"] == "operator_equals"
                and reads_list(blocks[x]["inputs"]["CONDITION"][1], director.SLOT_TYPE_ID, set())
                for x in ids
            ):
                failures.add("spawn-gates-empty-slot")
            # the loop advances its own spawn cursor by 1 each iteration.
            if not any(
                blocks[x]["opcode"] == "data_changevariableby"
                and blocks[x]["fields"].get("VARIABLE", [None, None])[1] == director.SPAWN_CURSOR_ID
                and blocks[x]["inputs"].get("VALUE") == [1, [4, 1]]
                for x in ids
            ):
                failures.add("spawn-advances-cursor")

        # DIF-03 mechanism: the shared fire gate reloads the per-slot fire timer from an expression that
        # reads the captured fire MASK (rng mod (mask + 1)) — mask 0 => reload 1 (fastest), larger mask
        # => rarer. This is the fire-FREQUENCY cap, not an on/off permission (the ground-only schedule
        # permission gate is `gnd_stop_firing_row`, deferred to the ground slice).
        if not any(
            b["opcode"] == "data_replaceitemoflist"
            and b["fields"].get("LIST", [None, None])[1] == director.SLOT_FIRE_TIMER_ID
            and isinstance(b["inputs"].get("ITEM"), list)
            and len(b["inputs"]["ITEM"]) > 1
            and reads_list(b["inputs"]["ITEM"][1], director.SLOT_FIRE_MASK_ID, set())
            for b in blocks.values()
        ):
            failures.add("fire-reload-reads-mask")

        return failures

    def test_live_pressure_contract(self) -> None:
        project = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._live_pressure_failures(project))

    def test_live_pressure_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._live_pressure_failures(base))

        def stage_of(p):
            return next(t for t in p["targets"] if t["isStage"])

        def refs_var(spec, var_id):
            return (
                isinstance(spec, list)
                and len(spec) > 1
                and isinstance(spec[1], list)
                and spec[1][2:3] == [var_id]
            )

        def spawn_loop(p):
            b = stage_of(p)["blocks"]
            for bid, blk in b.items():
                if blk["opcode"] == "control_repeat" and refs_var(
                    blk["inputs"].get("TIMES"), director.FORMATION_COUNT_ID
                ):
                    return bid
            raise AssertionError("no spawn refill loop")

        def body_ids(p, loop_id):
            b = stage_of(p)["blocks"]
            sub = b[loop_id]["inputs"].get("SUBSTACK")
            out, bid = [], (sub[1] if isinstance(sub, list) and len(sub) > 1 else None)
            while bid:
                out.append(bid)
                bid = b[bid].get("next")
            return out

        def find_list_read(p, root, list_id):
            b = stage_of(p)["blocks"]
            seen, stack = set(), [root]
            while stack:
                bid = stack.pop()
                if not isinstance(bid, str) or bid in seen or bid not in b:
                    continue
                seen.add(bid)
                if (
                    b[bid]["opcode"] == "data_itemoflist"
                    and b[bid]["fields"].get("LIST", [None, None])[1] == list_id
                ):
                    return bid
                for v in b[bid]["inputs"].values():
                    if isinstance(v, list) and len(v) > 1:
                        stack.append(v[1])
            raise AssertionError(f"no read of {list_id} under {root}")

        def break_loop_times(p):
            b = stage_of(p)["blocks"]
            b[spawn_loop(p)]["inputs"]["TIMES"] = [1, [4, "3"]]  # a constant, no longer the count

        def break_empty_gate(p):
            # retarget the empty-slot read so the loop no longer gates on the slot's type.
            b = stage_of(p)["blocks"]
            for x in body_ids(p, spawn_loop(p)):
                if b[x]["opcode"] == "control_if" and isinstance(b[x]["inputs"].get("CONDITION"), list):
                    cond = b[x]["inputs"]["CONDITION"][1]
                    read = find_list_read(p, cond, director.SLOT_TYPE_ID)
                    b[read]["fields"]["LIST"] = ["slot state", director.SLOT_STATE_ID]
                    return
            raise AssertionError("no empty gate in the spawn loop")

        def break_fire_reload(p):
            # The mask->reload mechanism is wired in more than one place (the shared gate plus the
            # families that inline their own fire reload), so sever the mask read in EVERY timer-reload
            # that reads it — the `any` guard only reddens once the mechanism is gone entirely.
            b = stage_of(p)["blocks"]
            broken = 0
            for bid, blk in list(b.items()):
                if (
                    blk["opcode"] == "data_replaceitemoflist"
                    and blk["fields"].get("LIST", [None, None])[1] == director.SLOT_FIRE_TIMER_ID
                    and isinstance(blk["inputs"].get("ITEM"), list)
                    and len(blk["inputs"]["ITEM"]) > 1
                ):
                    try:
                        read = find_list_read(p, blk["inputs"]["ITEM"][1], director.SLOT_FIRE_MASK_ID)
                    except AssertionError:
                        continue
                    b[read]["fields"]["LIST"] = ["slot fire timer", director.SLOT_FIRE_TIMER_ID]
                    broken += 1
            if not broken:
                raise AssertionError("no mask-reading fire reload")

        def break_spawn_cursor(p):
            # sever the cursor advance: freeze it at +0 so the loop no longer walks its spawn
            # cursor (it would re-examine the same slot every iteration instead of the next).
            b = stage_of(p)["blocks"]
            broken = 0
            for x in body_ids(p, spawn_loop(p)):
                if (
                    b[x]["opcode"] == "data_changevariableby"
                    and b[x]["fields"].get("VARIABLE", [None, None])[1] == director.SPAWN_CURSOR_ID
                ):
                    b[x]["inputs"]["VALUE"] = [1, [4, 0]]  # advance by 0 — the cursor never moves
                    broken += 1
            if not broken:
                raise AssertionError("no spawn-cursor advance in the spawn loop")

        cases = [
            ("spawn-loop-times-formation-count", break_loop_times),
            ("spawn-gates-empty-slot", break_empty_gate),
            ("spawn-advances-cursor", break_spawn_cursor),
            ("fire-reload-reads-mask", break_fire_reload),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._live_pressure_failures(project), label)

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
        # half-pixel "shadow" units as (y_bias, y_width, x_bias, x_width). The bullet/flying
        # window is live this slice (craft-overlap check); Bacura stays dormant until slice 11.
        # Pinned to independent literals so a wrong window reddens here.
        self.assertEqual(director.HIT_WINDOW_BULLET_FLYING, (8, 16, 4, 8))
        self.assertEqual(director.HIT_WINDOW_BACURA, (28, 40, 8, 16))
        # WPN-02 shot-vs-flying window: DOUBLED from the reference (16,32,8,16) as a recorded,
        # playtest-driven deviation — the reference height (2 cells) is under the shot's 2.5-cell/frame
        # step (tunneling) and covers only ~40% of our 36-px rendered Toroid. See B8-no-tunnel and the
        # HIT_WINDOW_SHOT_FLYING comment in game_director.py.
        self.assertEqual(director.HIT_WINDOW_SHOT_FLYING, (32, 64, 16, 32))
        # The bullet allocator's result var is its own, never the blaster's (no coupling).
        self.assertNotEqual(director.BULLET_ALLOC_RESULT_ID, director.ALLOC_RESULT_ID)
        self.assertEqual(director.BULLET_TYPE, 2)

    def _enemy_bullet_pool_failures(self, project: dict) -> set:
        # AIR-12 live allocator: defined on the Stage, sweeps the 19 bullet slots with its own
        # result var, marks the bullet type — and (this slice) is CALLED from the shooting Toroid's
        # update to fire a single aimed bullet. Structural only; the aimed vector/movement/pulse are
        # exercised by their own AIR-12 checks and the harness.
        fails = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        sblocks = stage["blocks"]
        if not any(
            b.get("opcode") == "procedures_prototype"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
            for b in sblocks.values()
        ):
            fails.add("bullet-alloc-defined")
        # The shooting Toroid now fires: the allocator is called from `update toroid`.
        called_in_update = any(
            b.get("opcode") == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
            for b in _proc_body_blocks(stage, director.UPDATE_TOROID_PROCCODE)
        )
        if not called_in_update:
            fails.add("bullet-alloc-live")
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

        def break_alloc_live(p):  # the shooting Toroid never fires (its allocator call removed)
            # Scope to `update toroid`: the allocator now has a SECOND live caller (`update logram`), so a
            # blanket "first allocator call anywhere" would corrupt the wrong one and leave the Toroid's
            # intact — the guard checks the Toroid's stack specifically.
            stage = next(t for t in p["targets"] if t["isStage"])
            for b in _proc_body_blocks(stage, director.UPDATE_TOROID_PROCCODE):
                if (
                    b.get("opcode") == "procedures_call"
                    and b.get("mutation", {}).get("proccode") == director.ALLOC_BULLET_PROCCODE
                ):
                    b["mutation"]["proccode"] = director.ALLOC_SHOT_PROCCODE
                    b["mutation"]["argumentids"] = "[]"
                    b["inputs"] = {}
                    return
            raise AssertionError("no allocator call to break")

        for label, corrupt in (
            ("bullet-type", break_bullet_type),
            ("bullet-cap", break_bullet_cap),
            ("bullet-alloc-live", break_alloc_live),
        ):
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(
                label,
                self._enemy_bullet_pool_failures(project),
                f"corruption '{label}' was not caught",
            )

    @staticmethod
    def _air12_failures(project: dict) -> set:
        # AIR-12 live enemy bullet: once fired it flies straight on its aimed velocity and culls off any
        # edge (update bullet), and the shooting Toroid aims it at the craft on the 32-magnitude tier at
        # the moment it fires (update toroid's fire path). Structural; the movement/kill is the harness
        # (enemy-bullet-fires) and the operator playtest.
        # roadmap-evidence: AIR-12 success  (test_enemy_bullet_flight_and_fire — move on both axes, edge cull, aimed-32 fire)
        # roadmap-evidence: AIR-12 failure  (test_enemy_bullet_flight_negative_fixtures — each clause corrupted bites)
        fails = set()
        stage = next(t for t in project["targets"] if t["isStage"])
        blocks = stage["blocks"]

        def reads_list(item_spec, list_name: str) -> bool:
            # ITEM input points to a `item N of <list_name>` reporter.
            if not (isinstance(item_spec, list) and len(item_spec) > 1 and isinstance(item_spec[1], str)):
                return False
            b = blocks.get(item_spec[1])
            return (
                b is not None
                and b["opcode"] == "data_itemoflist"
                and b["fields"].get("LIST", [None])[0] == list_name
            )

        def is_reporter(item_spec, opcode: str) -> bool:
            if not (isinstance(item_spec, list) and len(item_spec) > 1 and isinstance(item_spec[1], str)):
                return False
            b = blocks.get(item_spec[1])
            return b is not None and b["opcode"] == opcode

        bullet_body = _proc_body_blocks(stage, director.UPDATE_BULLET_PROCCODE)
        # The bullet moves on both axes each tick (velocity-scaled add into its own slot).
        for axis, list_name in (("x", "slot x"), ("y", "slot y")):
            if not any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"].get("LIST", [None])[0] == list_name
                and is_reporter(b["inputs"].get("ITEM"), "operator_add")
                for b in bullet_body
            ):
                fails.add(f"bullet-moves-{axis}")
        # The bullet is culled when it leaves the field (a cull-slot call guarded by an if).
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
            for b in bullet_body
        ):
            fails.add("bullet-culls")
        # The bullet update must actually be DISPATCHED from the slot walk (a wrong type constant would
        # leave a correct body that never runs) — `advance slots` calls `update bullet`.
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.UPDATE_BULLET_PROCCODE
            for b in _proc_body_blocks(stage, director.ADVANCE_SLOTS_PROCCODE)
        ):
            fails.add("bullet-dispatched")

        toroid_body = _proc_body_blocks(stage, director.UPDATE_TOROID_PROCCODE)
        # The fire path resolves an aim (compute aim index) and writes the bullet's velocity from the
        # 32-magnitude tables — so the bullet is aimed at the craft, not launched on a fixed vector.
        if not any(
            b["opcode"] == "procedures_call"
            and b.get("mutation", {}).get("proccode") == director.COMPUTE_AIM_PROCCODE
            for b in toroid_body
        ):
            fails.add("fire-computes-aim")
        for axis, list_name in (("dx", "aim dx 32"), ("dy", "aim dy 32")):
            slot_list = "slot dx" if axis == "dx" else "slot dy"
            if not any(
                b["opcode"] == "data_replaceitemoflist"
                and b["fields"].get("LIST", [None])[0] == slot_list
                and reads_list(b["inputs"].get("ITEM"), list_name)
                for b in toroid_body
            ):
                fails.add(f"fire-aims-{axis}")
        return fails

    def test_enemy_bullet_flight_and_fire(self) -> None:
        self.assertEqual(set(), self._air12_failures(load_source(scratch.SOURCE_DIR)))

    def test_enemy_bullet_flight_negative_fixtures(self) -> None:
        base = load_source(scratch.SOURCE_DIR)
        self.assertEqual(set(), self._air12_failures(base))

        def body_of(project, proccode):
            stage = next(t for t in project["targets"] if t["isStage"])
            return stage, _proc_body_blocks(stage, proccode)

        def break_move_x(p):  # bullet stops advancing on the scroll axis
            _stage, body = body_of(p, director.UPDATE_BULLET_PROCCODE)
            b = next(
                b
                for b in body
                if b["opcode"] == "data_replaceitemoflist"
                and b["fields"].get("LIST", [None])[0] == "slot x"
            )
            b["inputs"]["ITEM"] = [1, [4, 0]]

        def break_cull(p):  # bullet never leaves the pool (retarget its cull call)
            _stage, body = body_of(p, director.UPDATE_BULLET_PROCCODE)
            b = next(
                b
                for b in body
                if b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == director.CULL_SLOT_PROCCODE
            )
            b["mutation"]["proccode"] = director.RESOLVE_HIT_PROCCODE
            b["mutation"]["argumentids"] = "[]"
            b["inputs"] = {}

        def break_fire_aim(p):  # the fired bullet is no longer aimed on the 32 tier
            _stage, body = body_of(p, director.UPDATE_TOROID_PROCCODE)
            b = next(
                b
                for b in body
                if b["opcode"] == "data_replaceitemoflist"
                and b["fields"].get("LIST", [None])[0] == "slot dx"
            )
            b["inputs"]["ITEM"] = [1, [4, 0]]

        def break_dispatch(p):  # the walk no longer dispatches the bullet update (wrong type constant)
            _stage, body = body_of(p, director.ADVANCE_SLOTS_PROCCODE)
            b = next(
                b
                for b in body
                if b["opcode"] == "procedures_call"
                and b.get("mutation", {}).get("proccode") == director.UPDATE_BULLET_PROCCODE
            )
            b["mutation"]["proccode"] = director.CULL_SLOT_PROCCODE
            b["mutation"]["argumentids"] = "[]"
            b["inputs"] = {}

        for label, corrupt in (
            ("bullet-moves-x", break_move_x),
            ("bullet-culls", break_cull),
            ("fire-aims-dx", break_fire_aim),
            ("bullet-dispatched", break_dispatch),
        ):
            project = copy.deepcopy(base)
            corrupt(project)
            self.assertIn(label, self._air12_failures(project), f"'{label}' not caught")

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
        # B8-no-tunnel: the shot's per-frame vertical advance must not exceed the shot-vs-air hit
        # window's height, or a real fired shot steps clean OVER a Toroid between collision samples
        # (every shot in a held stream shares the craft-row phase, so a Toroid in a gap is immune to
        # the whole stream — the operator saw "multiple rounds and nothing happens"). The headless
        # harness cannot reproduce per-frame timing (it runs threads to settling), so this numeric
        # invariant is the guard. shot step = DY / RENDER_ROW_STAGE cells; window height = y_width /
        # SHADOW_PER_CELL cells. Require ~1 cell of margin for the enemy's own closing motion.
        dy_blocks = [
            num(b["inputs"].get("DY"))
            for b in blocks["blaster"].values()
            if b["opcode"] == "motion_changeyby"
        ]
        shot_dy = max(dy_blocks) if dy_blocks else 0
        shot_step_cells = shot_dy / director.RENDER_ROW_STAGE
        window_height_cells = director.HIT_WINDOW_SHOT_FLYING[1] / director.SHADOW_PER_CELL
        if window_height_cells < shot_step_cells + 1.0:
            fails.add("B8-no-tunnel")

        # B2 — single guarded bomb. WPN-04 (slice 9) moved the bomb logic OFF the bomb sprite (now a
        # pure slot renderer) and INTO the Stage walk (`advance bomb`): the walk arms the one-bomb
        # guard, re-arms it at the finish, tests idle before arming, and broadcasts the drop. The bomb
        # sprite keeps no clone and only RECEIVES the drop/land sounds.
        if count("bomb", "control_start_as_clone") != 0:
            fails.add("B2-clone")
        if not sets_var("Stage", "bomb in flight", 1):
            fails.add("B2-arm")
        if not sets_var("Stage", "bomb in flight", 0):
            fails.add("B2-rearm")
        if not has(
            "Stage",
            lambda b: b["opcode"] == "operator_equals"
            and num(b["inputs"].get("OPERAND2")) == 0
            and b["inputs"].get("OPERAND1", [None, [None, None]])[1][1] == "bomb in flight",
        ):
            fails.add("B2-idle-test")
        if not broadcasts("Stage", "bomb"):
            fails.add("B2-broadcast")
        # The bomb sprite renderer still receives the drop-sound broadcast.
        if not receives("bomb", "bomb"):
            fails.add("B2-drop-receive")

        # B6 — the crosshair is a pure slot renderer (slice 9): it no longer receives the bomb
        # broadcast; it switches to the targeting reticle costume off its slot state.
        if receives("target_a", "bomb"):
            fails.add("B6-crosshair-not-receiver")
        if not has("target_a", lambda b: b["opcode"] == "looks_switchcostumeto"):
            fails.add("B6-crosshair-costume")

        # B7 — the impact marker is a pure slot renderer (slice 9): no bomb-broadcast receiver; it
        # shows when its slot is active.
        if receives("target_b", "bomb"):
            fails.add("B7-marker-not-receiver")
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
        # WPN-04 (slice 9): the craft now clamps its OWN top bound (frame_t), mirroring
        # update_solvalou_sprite_XY's hard clamp on both axes. The interim crosshair-driven
        # `target_t` broadcast that used to stand in for the top bound is retired, so the craft
        # touches all four frame edges directly.
        self.assertEqual({"frame_b", "frame_t", "frame_l", "frame_r"}, touched_frames)
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

        def break_bomb_broadcast(p):  # B2: drop the Stage walk's bomb-drop broadcast
            b = first(
                p,
                "Stage",
                lambda b: b["opcode"] == "event_broadcast"
                and b["inputs"].get("BROADCAST_INPUT", [None, [None, None, None]])[1][1]
                == "bomb",
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

        def break_crosshair_costume(p):  # B6: drop the crosshair reticle costume switch
            b = first(p, "target_a", lambda b: b["opcode"] == "looks_switchcostumeto")
            b["opcode"] = "looks_show"

        def break_drop_receive(p):  # B2: drop the bomb sprite's drop-sound receiver
            b = first(
                p,
                "bomb",
                lambda b: b["opcode"] == "event_whenbroadcastreceived"
                and b["fields"]["BROADCAST_OPTION"][0] == "bomb",
            )
            b["fields"]["BROADCAST_OPTION"][0] = "director stop"

        def couple_crosshair_to_bomb(p):  # B6: regress the crosshair back to a bomb receiver
            b = first(p, "target_a", lambda b: b["opcode"] == "event_whenbroadcastreceived")
            b["fields"]["BROADCAST_OPTION"][0] = "bomb"

        def couple_marker_to_bomb(p):  # B7: regress the marker back to a bomb receiver
            b = first(p, "target_b", lambda b: b["opcode"] == "event_whenbroadcastreceived")
            b["fields"]["BROADCAST_OPTION"][0] = "bomb"

        def break_explosion_holds(p):  # B5: shorten one explosion hold
            b = first(
                p,
                "solv_death",
                lambda b: b["opcode"] == "control_repeat"
                and num(b["inputs"].get("TIMES")) == director.EXPLOSION_HOLD_TICKS,
            )
            b["inputs"]["TIMES"] = [1, [4, director.EXPLOSION_HOLD_TICKS + 1]]

        def break_bomb_arm(p):  # B2: fail to set the in-flight guard on arm (now Stage-owned)
            b = first(
                p,
                "Stage",
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
            ("B2-drop-receive", break_drop_receive),
            ("B3-position-test-area_01a", break_terrain_count),
            ("B4-glide", break_title_glide),
            ("B5B10-explosion", break_explosion_holds),
            ("B5B10-pause", break_death_pause),
            ("B6-crosshair-costume", break_crosshair_costume),
            ("B6-crosshair-not-receiver", couple_crosshair_to_bomb),
            ("B7-marker-show", break_marker),
            ("B7-marker-not-receiver", couple_marker_to_bomb),
            ("B8-top-expiry", break_shot_expiry),
            ("B9-craft-front", break_craft_layer),
            ("B9-terrain-back-area_01a", break_terrain_layer),
        ]
        for label, corrupt in cases:
            project = copy.deepcopy(base)
            corrupt(project)
            failures = self._regression_contract_failures(project)
            self.assertIn(label, failures, f"corruption '{label}' was not caught")

    # Roadmap closure evidence for leaf `player.ground-targeting` (WPN-03 target-lock, WPN-04 bomb-flight).
    # The crosshair (slot 35) leads the craft by the fixed 96-px forward lead and locks the bomb target
    # (slot 33) ahead of the craft; the bomb (slot 34) is a single guarded weapon flown by the walk, not a
    # player-steered sight. The structural pin below proves the sight/target are craft-driven, never
    # arrow-steered; the live lead + lock + flight are the harness `bomb-crosshair-leads-craft` /
    # `bomb-target-locks-ahead` (WPN-03) and the single-guarded accelerating flight to finish
    # `bomb-target-locks-ahead` / `bomb-finish-resolves-ground` (WPN-04).
    # roadmap-evidence: WPN-03 success  (test_bomb_sight_is_not_player_movable — the Stage walk drives the sight/target by craft cell + fixed lead + scroll, sensing no arrow key; harness bomb-crosshair-leads-craft / bomb-target-locks-ahead lead and lock live)
    # roadmap-evidence: WPN-03 failure  (test_bomb_sight_is_not_player_movable negative re-adds an arrow-key poll on the Stage; harness bomb-crosshair-leads-craft negative zeroes the lead so the sight sits on the craft)
    # roadmap-evidence: WPN-04 success  (test_bomb_sight_is_not_player_movable — the bomb is a single guarded weapon flown off the sprite by the walk; harness bomb-target-locks-ahead / bomb-finish-resolves-ground fly the accelerating bomb to its finish)
    # roadmap-evidence: WPN-04 failure  (test_bomb_sight_is_not_player_movable negative; harness bomb-target-locks-ahead negative breaks the arm guard so no bomb flies)
    def test_bomb_sight_is_not_player_movable(self) -> None:
        # WPN-04 (slice 9, player.ground-targeting #67): the bomb crosshair (slot 35) and the locked
        # bomb target (slot 33) are driven ONLY by the craft cell + the fixed 96-px forward lead +
        # terrain scroll — never by arrow keys. The interim reticle was arrow-movable; that branch is
        # retired (init_bombing's target follows the craft, it is not steered). Arrow-key movement lives
        # solely on the solvalou sprite (its four arrow reads clamp the craft itself); the Stage walk,
        # which owns the sight/target slot lists, must sense NO arrow key. Re-adding an arrow-key branch
        # to steer the target would poll an arrow key on the Stage and trip this guard.
        project = load_source(scratch.SOURCE_DIR)
        stage = next(t for t in project["targets"] if t["isStage"])
        arrow_keys = {"up arrow", "down arrow", "left arrow", "right arrow"}

        def stage_sensed_keys(st: dict) -> set:
            return {
                b["fields"]["KEY_OPTION"][0]
                for b in st["blocks"].values()
                if b["opcode"] == "sensing_keyoptions"
            }

        sensed = stage_sensed_keys(stage)
        self.assertEqual(
            set(), sensed & arrow_keys, "the Stage walk must not steer the bomb sight by arrow keys"
        )
        # Only the bomb-arm 'b' poll, the debug-spawn 't' poll, the debug-ground 'g' poll, and the
        # debug-pause 'p' poll are expected Stage key reads ('t'/'g'/'p' are temporary dev tools tracked
        # for removal, #119).
        self.assertLessEqual(sensed, {"b", "t", "g", "p"}, sensed)

        # Negative: re-add an arrow-key branch (an arrow-key poll on the Stage) → the guard fires.
        corrupt = copy.deepcopy(project)
        cstage = next(t for t in corrupt["targets"] if t["isStage"])
        first_keyopt = next(
            b for b in cstage["blocks"].values() if b["opcode"] == "sensing_keyoptions"
        )
        first_keyopt["fields"]["KEY_OPTION"][0] = "left arrow"
        self.assertTrue(stage_sensed_keys(cstage) & arrow_keys)

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
            "46a6972d6709c29c19bdef4c8fd7c71f587781114194211cb9f9441177b9f23f",
            build_hash,
        )


if __name__ == "__main__":
    unittest.main()
