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
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "spec"
DATA = SPEC / "data"

sys.path.insert(0, str(ROOT / "tools"))
import game_director as director  # noqa: E402


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

    def test_accelerated_full_game_trace_1_16_then_7(self):
        # AREA-03/AREA-04 acceptance (spec criterion 3 — the sole engine-checked one — and criterion
        # 5's 16->7): a deterministic trace OVER THE COMMITTED DATA (not the generated project, and not
        # an execution of the Scratch consume blocks — this is a data-completeness simulation) that
        # walks all 16 normal areas in order then continues at area 7, consuming every record with no
        # unknown handler and no Super-only object. It deliberately does NOT assert monotonic
        # scroll_row: area 14 carries a documented out-of-order row the build reproduces as-is.
        AREA_MAX = 16  # areas 1..16 (docs/spec/area-progression-and-terrain.md)
        AREA_LOOP_BACK = 7  # completing area 16 continues at area 7 — no win screen
        normal_type_max = load_extractor().NORMAL_TYPE_MAX

        payload = json.loads((DATA / "area-schedules.json").read_text())
        by_area = {a["area"]: a for a in payload["areas"]}
        registry = json.loads((DATA / "object-types.json").read_text())["registry"]["types"]
        known_handlers = {t.get("schedule_action") for t in registry} - {"none", None}

        # the accelerated order: 1,2,...,16, then the loop continues at 7 (the extra area proves the
        # wrap target and the absence of a 17th area / win screen).
        self.assertNotIn(17, by_area, "there is no area 17 — the loop returns to 7, not onward")
        order = list(range(1, AREA_MAX + 1)) + [AREA_LOOP_BACK]

        consumed = 0
        for step, area_number in enumerate(order):
            self.assertIn(area_number, by_area, f"step {step}: area {area_number} missing")
            for record in by_area[area_number]["records"]:
                self.assertIn(
                    record["handler"],
                    known_handlers,
                    f"area {area_number}: unknown record kind {record['handler']!r}",
                )
                self.assertLessEqual(
                    record["object_type"],
                    normal_type_max,
                    f"area {area_number}: Super-only object_type {record['object_type']}",
                )
                consumed += 1
        # every record of all 16 areas, plus the wrap re-visit of area 7, consumed in order.
        total_records = sum(len(a["records"]) for a in payload["areas"])
        self.assertEqual(total_records + len(by_area[AREA_LOOP_BACK]["records"]), consumed)
        # the wrap lands on a real, already-existing area (7 <= 16), so play continues — never a win screen.
        self.assertIn(AREA_LOOP_BACK, range(1, AREA_MAX + 1))

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

    def test_all_area_schedules_round_trip_from_json(self):
        # AREA-03: the ingested schedule columns are a FAITHFUL, lossless copy of ALL 16 normal areas'
        # records plus each area's materialized end sentinel, and the two 16-entry index lists give each
        # area's 1-based INCLUSIVE span into the flattened columns. The spans are re-derived
        # INDEPENDENTLY here from the JSON record counts (stride = len(records) + 1) — never read back
        # from the generator's own index lists — and each flattened window is compared to its SOURCE
        # records, so an offset off-by-one that leaked one area into the next (or a dropped/altered
        # field) fails here rather than shipping silently.
        project = json.loads(PROJECT_JSON.read_text())
        stage = next(t for t in project["targets"] if t["isStage"])
        by_name = {value[0]: value[1] for value in stage["lists"].values()}
        handlers = by_name["schedule handler"]
        rows = by_name["schedule trigger row"]
        payloads = by_name["schedule payload"]
        args = by_name["schedule arg"]
        gen_start = by_name["area schedule start"]
        gen_end = by_name["area schedule end"]

        # DIF-01/FORM-01: the runtime `arg` scalar, re-decoded INDEPENDENTLY here from each source
        # record (set-formation offset / fire-mask byte / ground-stop row; 0 otherwise), so a
        # mis-populated column fails here rather than shipping silently.
        def expected_arg(record):
            handler, params = record["handler"], record.get("params", {})
            if handler == "set_flying_formation":
                return params["formation_offset"]
            if handler.startswith("fire_mask_"):
                return params["mask"]
            if handler == "ground_stop_firing_row":
                return params["row"]
            return 0

        areas = json.loads((DATA / "area-schedules.json").read_text())["areas"]
        by_area = {a["area"]: a for a in areas}
        self.assertEqual(16, len(areas))

        # independently re-derive the contiguous 1-based inclusive spans from the JSON record counts.
        expected_start, expected_end = [], []
        cursor = 1
        for area_number in range(1, 17):
            stride = len(by_area[area_number]["records"]) + 1  # records + one materialized sentinel
            expected_start.append(cursor)
            expected_end.append(cursor + stride - 1)
            cursor += stride
        self.assertEqual(expected_start, gen_start, "area schedule start offsets")
        self.assertEqual(expected_end, gen_end, "area schedule end offsets")
        # the four parallel columns are exactly as long as the last span says.
        self.assertEqual(cursor - 1, len(handlers))
        self.assertEqual({len(handlers)}, {len(rows), len(payloads), len(args)})

        # each area's flattened window matches its SOURCE records + materialized sentinel.
        for area_number in range(1, 17):
            area = by_area[area_number]
            start = expected_start[area_number - 1]  # 1-based
            for j, record in enumerate(area["records"]):
                idx = start - 1 + j  # 0-based index into the flattened columns
                self.assertEqual(record["handler"], handlers[idx], f"area {area_number} handler {j}")
                self.assertEqual(
                    record["scroll_row"], rows[idx], f"area {area_number} trigger row {j}"
                )
                self.assertEqual(
                    {"object_type": record["object_type"], "params": record["params"]},
                    json.loads(payloads[idx]),
                    f"area {area_number} payload {j}",
                )
                self.assertEqual(expected_arg(record), args[idx], f"area {area_number} arg {j}")
            # this area's window terminates in the materialized sentinel (its scalar end_sentinel).
            end = expected_end[area_number - 1]  # 1-based, inclusive
            self.assertEqual("sentinel", handlers[end - 1], f"area {area_number} sentinel handler")
            self.assertEqual(area["end_sentinel"], rows[end - 1], f"area {area_number} sentinel row")
            self.assertEqual("", payloads[end - 1], f"area {area_number} sentinel payload")
            self.assertEqual(0, args[end - 1], f"area {area_number} sentinel arg")


class DifficultyAndFormations(unittest.TestCase):
    """DIF-01 / FORM-01: the difficulty AI level and normal flying formations, modelled over the
    COMMITTED data independently of the generator (the engine half of the acceptance criteria). The
    RULE is re-implemented here, not read back from the dispatch, so a wrong index formula fails
    rather than laundering itself; only the build's DIP-index and numeric constants come from the
    generator."""

    def _stage_lists(self):
        project = json.loads(PROJECT_JSON.read_text())
        stage = next(t for t in project["targets"] if t["isStage"])
        return {value[0]: value[1] for value in stage["lists"].values()}

    def _formation_table(self):
        entries = json.loads((DATA / "formations.json").read_text())["formation_table"]["entries"]
        ordered = sorted(entries, key=lambda e: e["index"])
        counts = [e["enemy_count"] for e in ordered]
        offsets = [e["type_table_offset"] for e in ordered]
        return ordered, counts, offsets

    def test_baked_tables_match_committed_data(self):
        # The generator bakes difficulty.json / formations.json faithfully into the Stage lists.
        lists = self._stage_lists()
        diff = json.loads((DATA / "difficulty.json").read_text())["difficulty_tbl"]["values"]
        self.assertEqual([2, 0, 6, 16], diff)  # arcade increments, sanity-anchored
        self.assertEqual(diff, lists["difficulty increment"])
        ordered, counts, offsets = self._formation_table()
        self.assertEqual(
            list(range(director.FORMATION_MIN_INDEX, director.FORMATION_MIN_INDEX + director.FORMATION_TABLE_LEN)),
            [e["index"] for e in ordered],
            "formation table domain -32..127",
        )
        self.assertEqual(counts, lists["formation count table"])
        self.assertEqual(offsets, lists["formation type offset table"])

    def test_formation_lookup_reproduces_committed_table(self):
        # FORM-01 selection: slot = index - MIN (0-based into the two ordered lists) reproduces every
        # committed (count, type-offset) pair across the whole domain.
        ordered, counts, offsets = self._formation_table()
        by_index = {e["index"]: (e["enemy_count"], e["type_table_offset"]) for e in ordered}
        for index in range(director.FORMATION_MIN_INDEX, director.FORMATION_MIN_INDEX + director.FORMATION_TABLE_LEN):
            slot = index - director.FORMATION_MIN_INDEX
            self.assertEqual(by_index[index], (counts[slot], offsets[slot]), f"index {index}")

    def test_ai_level_fold_back(self):
        # DIF-01 raise math: ai += increment; fold ONCE at >= 0x80 by subtracting 0x40, so the level
        # (which the raise re-select uses as the formation index) stays below 0x80.
        inc = json.loads((DATA / "difficulty.json").read_text())["difficulty_tbl"]["values"][
            director.DIFFICULTY_DIP_INDEX
        ]

        def raise_once(ai):
            ai += inc
            if ai >= director.AI_LEVEL_FOLD_THRESHOLD:
                ai -= director.AI_LEVEL_FOLD_SUBTRACT
            return ai

        self.assertEqual(inc, raise_once(0))
        self.assertEqual(64, raise_once(126))  # 128 -> fold -> 64
        self.assertEqual(65, raise_once(127))  # 129 -> fold -> 65
        self.assertLess(raise_once(127), director.AI_LEVEL_FOLD_THRESHOLD)

    def test_score_retune_rule(self):
        # DIF-02 score-adaptive re-tune (sub_2_fn_23 / avg_score_per_solvalou): the addend is the
        # player's score in thousands divided by the craft in reserve, floored, capped at 16, and
        # only when reserve > 0 (no divide-by-zero). Reserve is the live `craft` count (the reference
        # divides by solvalou_number with no subtraction).
        def retune(score, craft):
            if craft <= 0:
                return 0
            return min(16, (score // 1000) // craft)

        self.assertEqual(0, retune(500, 3), "score below 1000 adds nothing")
        self.assertEqual(1, retune(3000, 3), "3k over 3 craft -> 1")
        self.assertEqual(5, retune(20000, 4), "20k over 4 craft -> 5")
        self.assertEqual(16, retune(200000, 3), "66 over 3 = 22, capped at 16")
        self.assertEqual(0, retune(50000, 0), "zero craft is guarded, adds nothing")

    def test_formation_index_in_domain_over_committed_schedules(self):
        # FORM-01 / DIF-01 range proof: walk the committed schedules in the accelerated 1..16 then
        # 7..16 loop order, tracking the AI level through raises (fold-back) and picking the formation
        # index exactly as the emitted dispatch does — set-formation: the record offset; raise: the
        # folded AI level. Assert EVERY index lands in the table's -32..127 domain, so the generator's
        # two-sided guard is a proven-dead defensive branch under this slice's dynamics, and that BOTH
        # selection paths are actually exercised. (DIF-02's score term, a later commit, is the only
        # thing that could push the index out of domain; that is recorded with DIF-02.)
        areas = {a["area"]: a["records"] for a in json.loads((DATA / "area-schedules.json").read_text())["areas"]}
        inc = json.loads((DATA / "difficulty.json").read_text())["difficulty_tbl"]["values"][
            director.DIFFICULTY_DIP_INDEX
        ]
        lo = director.FORMATION_MIN_INDEX
        hi = director.FORMATION_MIN_INDEX + director.FORMATION_TABLE_LEN - 1
        order = list(range(1, 17)) + list(range(7, 17)) * 6  # a few loop cycles for steady state
        ai = 0
        raises = sets = 0
        for area in order:
            for record in areas[area]:
                handler = record["handler"]
                if handler == "raise_ai_level_and_set_formation":
                    ai += inc
                    if ai >= director.AI_LEVEL_FOLD_THRESHOLD:
                        ai -= director.AI_LEVEL_FOLD_SUBTRACT
                    index = ai
                    raises += 1
                elif handler == "set_flying_formation":
                    index = record["params"]["formation_offset"]
                    sets += 1
                else:
                    continue
                self.assertTrue(lo <= index <= hi, f"formation index {index} out of domain (area {area})")
        self.assertGreater(raises, 0, "the raise re-select path must be exercised")
        self.assertGreater(sets, 0, "the set-formation path must be exercised")


if __name__ == "__main__":
    unittest.main()
