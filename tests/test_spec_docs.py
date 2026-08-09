"""Structural guards over the product spec and its generated data files.

These checks run without the reference checkout: they cannot re-derive the
data (that is ``tools/reference_extract.py --verify``, operator-run against a
fresh clone at the pin), but they hold the committed record consistent — one
pin everywhere, hashes agreeing between the index and every data file's
provenance block, an honest license status present, and no dangling links in
the spec corpus.
"""

import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "spec"
DATA = SPEC / "data"


def load_extractor():
    spec = importlib.util.spec_from_file_location(
        "reference_extract", ROOT / "tools" / "reference_extract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

PIN_RE = re.compile(r"\b[0-9a-f]{40}\b")


def spec_documents():
    return sorted(p for p in SPEC.glob("*.md"))


def data_files():
    # The manifest carries digests of the others, not provenance of its own.
    return sorted(p for p in DATA.glob("*.json") if p.name != "manifest.json")


def index_frontmatter_pin():
    text = (SPEC / "index.md").read_text()
    match = re.search(r"^reference_pin:\s*([0-9a-f]{40})\s*$", text, re.M)
    assert match, "index.md frontmatter must carry reference_pin"
    return match.group(1)


class SpecDataConsistency(unittest.TestCase):
    def test_one_pin_everywhere(self):
        pin = index_frontmatter_pin()
        for doc in spec_documents():
            for found in PIN_RE.findall(doc.read_text()):
                self.assertEqual(found, pin, f"{doc.name} cites a different commit")
        for data in data_files():
            payload = json.loads(data.read_text())
            self.assertEqual(
                payload["provenance"]["commit"], pin, f"{data.name} pinned elsewhere"
            )
        extractor = (ROOT / "tools" / "reference_extract.py").read_text()
        pins = set(PIN_RE.findall(extractor))
        self.assertEqual(pins, {pin}, "extractor pin differs from the index")

    def test_every_capability_doc_verified_at_the_pin(self):
        pin = index_frontmatter_pin()
        for doc in spec_documents():
            if doc.name in ("index.md", "build-plan.md"):
                # The index carries reference_pin; the build order is a living
                # planning document with no reference-derived values.
                continue
            match = re.search(
                r"^reference_verified_at:\s*([0-9a-f]{40})\s*$", doc.read_text(), re.M
            )
            self.assertIsNotNone(match, f"{doc.name} missing reference_verified_at")
            self.assertEqual(match.group(1), pin, f"{doc.name} verified at another commit")

    def test_index_hash_table_matches_data_provenance(self):
        index = (SPEC / "index.md").read_text()
        index_hashes = dict(
            (m.group(1), m.group(2))
            for m in re.finditer(
                r"`(src/[^`]+|readme\.md)`\s*\|\s*`([0-9a-f]{64})`", index
            )
        )
        self.assertGreaterEqual(len(index_hashes), 5, "index hash table incomplete")
        for data in data_files():
            payload = json.loads(data.read_text())
            for path, digest in payload["provenance"]["source_sha256"].items():
                self.assertEqual(
                    index_hashes.get(path),
                    digest,
                    f"{data.name}: hash for {path} disagrees with the index",
                )

    def test_every_data_file_has_a_producer(self):
        # A data file with no generator in the committed extractor would be
        # indistinguishable from a hand-written one; hold the two in lockstep.
        module = load_extractor()
        declared = set(module.DATA_FILE_NAMES) | {module.MANIFEST_NAME}
        on_disk = {p.name for p in DATA.glob("*.json")}
        self.assertEqual(
            declared, on_disk,
            "docs/spec/data contents and the extractor's declared outputs differ",
        )

    def test_data_files_match_their_manifest_digests(self):
        # Hand-editing any generated file breaks its recorded digest, so a
        # silent edit is catchable without the reference clone.
        module = load_extractor()
        manifest = json.loads((DATA / module.MANIFEST_NAME).read_text())
        for name in module.DATA_FILE_NAMES:
            digest = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
            self.assertEqual(
                manifest["files"].get(name), digest,
                f"{name} does not match its manifest digest — regenerate, never hand-edit",
            )


class ExtractorUnits(unittest.TestCase):
    """The pure decode functions, exercised without the reference checkout."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_extractor()

    def test_signed_byte_boundaries(self):
        self.assertEqual(self.mod.signed_byte(127), 127)
        self.assertEqual(self.mod.signed_byte(128), -128)
        self.assertEqual(self.mod.signed_byte(255), -1)
        self.assertEqual(self.mod.signed_byte(0), 0)

    def test_bcd_and_score_decode(self):
        self.assertEqual(self.mod.bcd_decode(0x40), 40)
        self.assertEqual(self.mod.bcd_decode(0x100), 100)
        self.assertEqual(self.mod.score_triple(0x01, 0x00, 0x00), 10)
        self.assertEqual(self.mod.score_triple(0x00, 0x02, 0x00), 2000)
        self.assertEqual(self.mod.score_triple(0x00, 0x10, 0x00), 10000)
        self.assertEqual(self.mod.score_triple(0x99, 0x99, 0x99), 9999990)

    def test_parse_value_rejects_out_of_range(self):
        self.assertEqual(self.mod.parse_value("0xFF"), 255)
        with self.assertRaises(self.mod.ExtractionError):
            self.mod.parse_value("300")
        with self.assertRaises(self.mod.ExtractionError):
            self.mod.parse_value("-1")
        with self.assertRaises(self.mod.ExtractionError):
            self.mod.parse_value("0x1FF")
        self.assertEqual(self.mod.parse_value("0x1FF", limit=0xFFFF), 511)

    def test_rng_step_matches_committed_golden_sequences(self):
        # The rule implementation and the committed fixtures must agree; a
        # mutation to either side fails here.
        sequences = json.loads((DATA / "rng.json").read_text())["generator"][
            "fixture_sequences"
        ]
        for fixture in sequences:
            state = fixture["seed"]
            outputs = []
            for _ in range(len(fixture["outputs"])):
                state, out = self.mod.rng_step(state)
                outputs.append(out)
            self.assertEqual(outputs, fixture["outputs"], f"seed {fixture['seed']}")
            self.assertEqual(state, fixture["final_state"])


class CrossTableInvariants(unittest.TestCase):
    """Relations between data files that a hand-edit would likely break."""

    def test_formation_targets_stay_inside_the_type_table(self):
        formations = json.loads((DATA / "formations.json").read_text())
        types = json.loads((DATA / "object-types.json").read_text())
        codes = types["flying_enemy_type_table"]["codes"]
        registry = {t["code"]: t for t in types["registry"]["types"]}
        for entry in formations["formation_table"]["entries"]:
            offset, count = entry["type_table_offset"], entry["enemy_count"]
            self.assertGreaterEqual(offset, 0)
            self.assertLessEqual(offset + count, len(codes))
            if offset + count <= 120:  # the recorded never-reached tail starts at 120
                for code in codes[offset : offset + count]:
                    self.assertIn(code, registry)
                    self.assertFalse(registry[code]["super_only"])

    def test_schedule_formation_offsets_resolve(self):
        schedules = json.loads((DATA / "area-schedules.json").read_text())
        formations = json.loads((DATA / "formations.json").read_text())
        indices = {e["index"] for e in formations["formation_table"]["entries"]}
        for area in schedules["areas"]:
            for record in area["records"]:
                if record["handler"] == "set_flying_formation":
                    self.assertIn(record["params"]["formation_offset"], indices)

    def test_master_value_table_is_the_recorded_ladder(self):
        tables = json.loads((DATA / "scores.json").read_text())["tables"]
        points = [e["points"] for e in tables["master_value_table"]["entries"]]
        self.assertEqual(points, sorted(points))
        self.assertEqual(points[0], 10)
        self.assertEqual(points[-1], 10000)

    def test_provenance_blocks_present_and_honest(self):
        for data in data_files():
            payload = json.loads(data.read_text())
            provenance = payload.get("provenance", {})
            self.assertEqual(provenance.get("reference"), "jotd666/xevious", data.name)
            self.assertTrue(provenance.get("license_status"), f"{data.name}: empty license status")
            self.assertTrue(provenance.get("source_sha256"), f"{data.name}: no source hashes")

    def test_spec_links_resolve(self):
        docs = list(spec_documents()) + [
            p for p in (ROOT / "docs").rglob("*.md") if SPEC not in p.parents
        ]
        for doc in docs:
            for target in re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", doc.read_text()):
                if target.startswith(("http://", "https://")):
                    continue
                resolved = (doc.parent / target).resolve()
                self.assertTrue(resolved.exists(), f"{doc.name} links to missing {target}")

    def test_schedule_data_shape_holds(self):
        payload = json.loads((DATA / "area-schedules.json").read_text())
        areas = payload["areas"]
        self.assertEqual(len(areas), 16)
        for area in areas:
            self.assertEqual(area["end_sentinel"], 0x0D)
            self.assertTrue(area["records"], f"area {area['area']} has no records")
            for record in area["records"]:
                self.assertLessEqual(
                    record["object_type"], 0x57,
                    f"area {area['area']} schedules a Super-only type",
                )

    def test_formation_indices_stay_reachable(self):
        payload = json.loads((DATA / "formations.json").read_text())
        indices = [e["index"] for e in payload["formation_table"]["entries"]]
        self.assertEqual(min(indices), -32)
        self.assertEqual(max(indices), 127, "entries beyond a sign-extended byte's reach")

    def test_score_tables_shape_holds(self):
        tables = json.loads((DATA / "scores.json").read_text())["tables"]
        master = tables["master_value_table"]["entries"]
        self.assertEqual(len(master), 22)
        self.assertEqual(master[0]["points"], 10)
        self.assertEqual(master[-1]["points"], 10000)
        self.assertEqual(tables["starting_lives"]["values"], [5, 2, 1, 3])
        for key in ("first_bonus_thresholds", "repeat_bonus_increments"):
            for table in ("table_5", "table_123"):
                values = tables[key][table]
                self.assertEqual(len(values), 8)
                self.assertIsNone(values[7], f"{key}.{table} lost its disabled sentinel")


if __name__ == "__main__":
    unittest.main()
