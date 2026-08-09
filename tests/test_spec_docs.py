"""Structural guards over the product spec and its generated data files.

These checks run without the reference checkout: they cannot re-derive the
data (that is ``tools/reference_extract.py --verify``, operator-run against a
fresh clone at the pin), but they hold the committed record consistent — one
pin everywhere, hashes agreeing between the index and every data file's
provenance block, an honest license status present, and no dangling links in
the spec corpus.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "spec"
DATA = SPEC / "data"

PIN_RE = re.compile(r"\b[0-9a-f]{40}\b")


def spec_documents():
    return sorted(p for p in SPEC.glob("*.md"))


def data_files():
    return sorted(DATA.glob("*.json"))


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
            if doc.name == "index.md":
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

    def test_provenance_blocks_present_and_honest(self):
        for data in data_files():
            payload = json.loads(data.read_text())
            provenance = payload.get("provenance", {})
            self.assertEqual(provenance.get("reference"), "jotd666/xevious", data.name)
            self.assertTrue(provenance.get("license_status"), f"{data.name}: empty license status")
            self.assertTrue(provenance.get("source_sha256"), f"{data.name}: no source hashes")

    def test_spec_links_resolve(self):
        for doc in spec_documents():
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
