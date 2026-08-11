"""Structural guards over the product spec and its generated data files.

These checks run without the reference checkout: they cannot re-derive the
data (that is ``tools/reference_extract.py --verify``, operator-run against a
fresh clone at the pin), but they hold the committed record consistent — one
pin everywhere, hashes agreeing between the index and every data file's
provenance block, an honest license status present, and no dangling links in
the spec corpus.
"""

import copy
import hashlib
import importlib.util
import json
import math
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


PROJECT_JSON = ROOT / "src" / "xevious" / "project.json"


def _stage_blocks(project):
    stage = next(t for t in project["targets"] if t["isStage"])
    return stage["blocks"]


def _rng_step_first_statement(blocks):
    """The first body statement of the emitted `rng step` custom block."""
    proto_id = next(
        (
            bid
            for bid, b in blocks.items()
            if b["opcode"] == "procedures_prototype"
            and b.get("mutation", {}).get("proccode") == "rng step"
        ),
        None,
    )
    assert proto_id is not None, "no `rng step` prototype in the built Stage"
    assert blocks[proto_id]["mutation"].get("warp") == "true", "rng step must be a warp block"
    for b in blocks.values():
        if b["opcode"] == "procedures_definition":
            custom = b["inputs"].get("custom_block")
            if custom and custom[1] == proto_id:
                return b["next"]
    raise AssertionError("no `rng step` definition in the built Stage")


def _eval_input(blocks, spec, env):
    kind = spec[0]
    if kind == 1:  # literal shadow: [1, [4|10, value]]
        return int(float(spec[1][1]))
    if kind == 2:  # a reporter block reference
        return _eval_block(blocks, spec[1], env)
    if kind == 3:  # [3, [12, name, id], shadow] variable, or [3, block_id, shadow]
        inner = spec[1]
        if isinstance(inner, list):
            return env.get(inner[1], 0)
        return _eval_block(blocks, inner, env)
    raise AssertionError(f"unexpected input spec {spec!r}")


def _eval_block(blocks, block_id, env):
    block = blocks[block_id]
    op = block["opcode"]
    if op == "data_variable":
        return env.get(block["fields"]["VARIABLE"][0], 0)
    if op == "operator_mathop":
        assert block["fields"]["OPERATOR"][0] == "floor", "only floor is interpreted"
        return math.floor(_eval_input(blocks, block["inputs"]["NUM"], env))
    binary = {
        "operator_add": lambda a, b: a + b,
        "operator_subtract": lambda a, b: a - b,
        "operator_multiply": lambda a, b: a * b,
        "operator_divide": lambda a, b: a / b,
        "operator_mod": lambda a, b: a % b,
        "operator_equals": lambda a, b: 1 if a == b else 0,
    }
    if op in binary:
        # Match scratch-vm's operand keys: arithmetic reads NUM1/NUM2, comparison reads
        # OPERAND1/OPERAND2. (The generator's _reporter makes the same distinction; keeping
        # these in lockstep is what makes this interpreter a check on the SHIPPED blocks.)
        slot1, slot2 = ("OPERAND1", "OPERAND2") if op == "operator_equals" else ("NUM1", "NUM2")
        left = _eval_input(blocks, block["inputs"][slot1], env)
        right = _eval_input(blocks, block["inputs"][slot2], env)
        return binary[op](left, right)
    raise AssertionError(f"unexpected reporter opcode {op}")


def _run_statements(blocks, first_id, env):
    block_id = first_id
    while block_id:
        block = blocks[block_id]
        op = block["opcode"]
        if op == "data_setvariableto":
            env[block["fields"]["VARIABLE"][0]] = _eval_input(
                blocks, block["inputs"]["VALUE"], env
            )
        elif op == "control_if":
            if _eval_input(blocks, block["inputs"]["CONDITION"], env):
                substack = block["inputs"].get("SUBSTACK")
                if substack:
                    _run_statements(blocks, substack[1], env)
        else:
            raise AssertionError(f"unexpected statement opcode {op}")
        block_id = block["next"]


class GeneratedRngStep(unittest.TestCase):
    """SYS-04: interpret the *emitted* `rng step` block graph against the committed
    golden sequences — a check on the blocks that actually ship, not a parallel
    Python function (which tests/test_spec_docs.py already covers via rng_step)."""

    def _sequence(self, blocks, first, seed, length):
        env = {"rng state": seed}
        outputs = []
        for _ in range(length):
            _run_statements(blocks, first, env)
            outputs.append(env["rng out"])
        return outputs, env["rng state"]

    def test_generated_rng_step_matches_committed_golden_sequences(self):
        blocks = _stage_blocks(json.loads(PROJECT_JSON.read_text()))
        first = _rng_step_first_statement(blocks)
        fixtures = json.loads((DATA / "rng.json").read_text())["generator"][
            "fixture_sequences"
        ]
        for fixture in fixtures:
            outputs, final = self._sequence(
                blocks, first, fixture["seed"], len(fixture["outputs"])
            )
            self.assertEqual(outputs, fixture["outputs"], f"seed {fixture['seed']} outputs")
            self.assertEqual(
                final, fixture["final_state"], f"seed {fixture['seed']} final state"
            )

    def test_generated_rng_step_negative_fixture(self):
        # Corrupt the emitted 5*low multiply to 4*low and prove the golden comparison
        # reddens — the interpreter genuinely reads the shipped blocks.
        blocks = copy.deepcopy(_stage_blocks(json.loads(PROJECT_JSON.read_text())))
        first = _rng_step_first_statement(blocks)
        mutated = False
        for block in blocks.values():
            if block["opcode"] == "operator_multiply":
                for slot in ("NUM1", "NUM2"):
                    spec = block["inputs"].get(slot)
                    if spec and spec[0] == 1 and int(float(spec[1][1])) == 5:
                        block["inputs"][slot] = [1, [4, 4]]
                        mutated = True
        self.assertTrue(mutated, "expected a 5*low multiply in the rng step body")
        fixture = json.loads((DATA / "rng.json").read_text())["generator"][
            "fixture_sequences"
        ][1]
        outputs, _ = self._sequence(blocks, first, fixture["seed"], len(fixture["outputs"]))
        self.assertNotEqual(outputs, fixture["outputs"])


class GeneratedAreaClock(unittest.TestCase):
    """AREA-01: interpret the EMITTED scroll-row derivation against hand-verified boundary
    values (a check on the blocks that ship, not a parallel Python formula), and check the
    ingested terrain-column list against the committed reference data."""

    def _row_reporter(self, blocks):
        # The derived `set scroll row` — a reporter VALUE ([3, block, shadow]), distinct from
        # the plain `set scroll row to 13` re-tops (VALUE kind 1).
        for block in blocks.values():
            if (
                block["opcode"] == "data_setvariableto"
                and block["fields"]["VARIABLE"][0] == "scroll row"
                and block["inputs"]["VALUE"][0] == 3
            ):
                return block["inputs"]["VALUE"][1]
        raise AssertionError("no derived `scroll row` setter found in the emitted blocks")

    def test_generated_scroll_row_derivation(self):
        blocks = _stage_blocks(json.loads(PROJECT_JSON.read_text()))
        reporter = self._row_reporter(blocks)
        # area progress -> derived arcade scroll row (hand-verified): the descent 0x0D..0x00
        # wraps to 0xFF and continues down, and the area completes at the first row 0x0E, which
        # is progress 65056 (not 65280 — the clock resets before that).
        cases = {0: 13, 256: 12, 3328: 0, 3584: 255, 64800: 15, 65024: 15, 65056: 14, 65280: 14}
        for progress, expected in cases.items():
            row = _eval_block(blocks, reporter, {"area progress": progress})
            self.assertEqual(expected, row, f"area progress {progress}")

    def test_generated_scroll_row_never_skips_a_row(self):
        # 32 units/tick against a 256-wide row means every row is visited, so no schedule
        # trigger is stepped over: each tick drops the row by 0 or 1 (mod 256, so the 0x00->0xFF
        # wrap counts as 1), and completion (row 14) is reached at the end of the sweep.
        blocks = _stage_blocks(json.loads(PROJECT_JSON.read_text()))
        reporter = self._row_reporter(blocks)
        prev = _eval_block(blocks, reporter, {"area progress": 0})
        progress = 32
        while progress <= 65056:
            row = _eval_block(blocks, reporter, {"area progress": progress})
            self.assertIn((prev - row) % 256, (0, 1), f"progress {progress}: {prev}->{row}")
            prev = row
            progress += 32
        self.assertEqual(14, prev)

    def test_area_map_column_matches_terrain_json(self):
        project = json.loads(PROJECT_JSON.read_text())
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value[1] for value in stage["lists"].values()}
        expected = json.loads((DATA / "terrain.json").read_text())[
            "area_offset_in_map_tbl"
        ]["values"]
        self.assertEqual(expected, by_name["area map column"])

    def test_area1_schedule_round_trips_from_json(self):
        # AREA-02: the ingested schedule columns are a FAITHFUL, lossless copy of area 1's records
        # (53) plus the materialized end sentinel (= 54), with the opaque payload decodable back to
        # object_type + params — so no handler's parameters are silently dropped.
        project = json.loads(PROJECT_JSON.read_text())
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value[1] for value in stage["lists"].values()}
        handlers = by_name["schedule handler"]
        rows = by_name["schedule trigger row"]
        payloads = by_name["schedule payload"]

        area = next(
            a
            for a in json.loads((DATA / "area-schedules.json").read_text())["areas"]
            if a["area"] == 1
        )
        records = area["records"]
        self.assertEqual(len(records) + 1, len(handlers))  # + materialized sentinel
        self.assertEqual({len(handlers), len(rows)}, {len(payloads)})

        for i, record in enumerate(records):
            self.assertEqual(record["handler"], handlers[i], f"handler {i}")
            self.assertEqual(record["scroll_row"], rows[i], f"trigger row {i}")
            decoded = json.loads(payloads[i])
            self.assertEqual(
                {"object_type": record["object_type"], "params": record["params"]},
                decoded,
                f"payload {i}",
            )

        # the terminal row is the sentinel: the JSON's scalar end_sentinel, not a record.
        self.assertEqual("sentinel", handlers[-1])
        self.assertEqual(area["end_sentinel"], rows[-1])
        self.assertEqual("", payloads[-1])

        # every area maps to area 1's table this slice (the honest slice-6 seam).
        self.assertEqual([1] * 16, by_name["area schedule start"])
        self.assertEqual([len(handlers)] * 16, by_name["area schedule end"])


if __name__ == "__main__":
    unittest.main()
