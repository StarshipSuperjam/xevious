"""The release audit's media check (RELEASE-02, release.audit, StarshipSuperjam/xevious#106, slice 21).

docs/spec/release.md: every image and sound in the built ``.sb3`` appears in the asset credits and in its
provenance record, every credited asset is in the build, and no media without a recorded source ships. This test
builds the project the way ``playtest_package.py`` does and walks all three records against each other:

- every member of the built ``.sb3`` is a media file ``project.json`` references, and either has a record in
  ``src/xevious/assets/provenance.json`` or is a file of the preserved 2017 base project (whose record is
  ``assets/original/provenance.json``);
- every record falls in exactly one credited family, by its origin, and that family's section of
  ``docs/ASSET_CREDITS.md`` exists, names its source, and states the record's license status;
- every shipped sound and every raw Spriters Resource sheet is listed in its table under its exact SHA-256;
- every section and every table row is backed by a shipped asset, unless the document says plainly that it is not
  used by the build (the fan area map) or no longer ships (the Andor Genesis sheet 42386).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREDITS = ROOT / "docs" / "ASSET_CREDITS.md"
OVERLAY_PROVENANCE = ROOT / "src" / "xevious" / "assets" / "provenance.json"
BASE_ARCHIVE = ROOT / "assets" / "original" / "Xevious.sb3"
BASE_PROVENANCE = ROOT / "assets" / "original" / "provenance.json"

sys.path.insert(0, str(ROOT / "tools"))
import scratch_project as scratch  # noqa: E402

MEDIA_SUFFIXES = (".png", ".svg", ".wav", ".mp3")
SPRITERS_SHEET = re.compile(
    r"The Spriters Resource: Xevious \(Arcade\), [^—]+— https://www\.spriters-resource\.com/arcade/xevious/asset/(\d+)/"
)
SOUNDS_SPRITERS = re.compile(r"Sounds Spriters Resource: Xevious \(Arcade\), asset (\d+)")
# Each family: (key, origin pattern, the credits heading it belongs under, a phrase that section must name). The
# order matters only where two patterns could both match one origin; the first match wins, and the test also
# refuses an origin no pattern matches.
FAMILIES = (
    ("extend-sound", SOUNDS_SPRITERS, "Extend / 1UP sound", "sounds.spriters-resource.com/arcade/xevious/asset/449687"),
    ("game-sounds", re.compile(r"assets/game-sounds/\w+\.wav|assets/sounds/bonus_flag\.wav"),
     "Arcade gameplay sound effects", "assets/game-sounds/manifest.json"),
    ("hud-font", re.compile(r'FontStruct: "Xevious HUD font recreation"'), "HUD font", "Patrick H. Lauke"),
    ("terrain", re.compile(r"tools/terrain_render\.py"), "Terrain rendered from the arcade map data",
     "tools/terrain_render.py"),
    ("sol-tower", re.compile(r"tools/sol_tower_render\.py"), "Sol Tower rendered from the arcade sprite data",
     "tools/sol_tower_render.py"),
    ("effects", re.compile(r"tools/effects_sprite_render\.py"),
     "Explosions, bomb and crosshair rendered from the arcade sprite data", "tools/effects_sprite_render.py"),
    ("reference-art", re.compile(r"tools/reference_art_render\.py"),
     "Enemy, shot and sparkle sprites rendered from the arcade sprite data", "tools/reference_art_render.py"),
    ("andor", re.compile(r"tools/andor_sprite_render\.py"), "Andor Genesis parts rendered from the arcade sprite data",
     "tools/andor_sprite_render.py"),
    ("bonus-flag", re.compile(r"assets/amiga/xevious_gfx\.c sprite index 287\b"),
     "Bonus Flag rendered from the arcade sprite data", "sprite index 287"),
    ("bezel", re.compile(r"tools/bezel_panels\.py"), "Cabinet bezel artwork", "tools/bezel_panels.py"),
    ("spriters", SPRITERS_SHEET, "Third-party asset credits", "www.spriters-resource.com/arcade/xevious/"),
)
BASE_HEADING = "The base Scratch project (2017)"
LICENSE_PHRASES = {
    "No reusable license": "No reusable license",
    "Creative Commons Attribution 3.0": "Creative Commons Attribution 3.0",
}
# Sections that describe rather than credit: they carry no asset of their own.
DESCRIPTIVE = {"Rights status", "Gameplay-ready derivatives"}
NOT_USED = "not used by the build"
SHA256 = re.compile(r"`([0-9a-f]{64})`")


def sections(text: str) -> dict[str, str]:
    """Split the credits into {heading: body}; the text before the first `##` is filed under the `#` title."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    head = parts[0]
    title = re.search(r"^# (.+)$", head, re.MULTILINE)
    found = {title.group(1).strip() if title else "": head}
    for heading, body in zip(parts[1::2], parts[2::2]):
        found[heading.strip()] = body
    return found


def section_for(found: dict[str, str], prefix: str) -> tuple[str, str] | None:
    hits = [(h, b) for h, b in found.items() if h.startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


def spriters_rows(body: str) -> dict[str, tuple[str, str]]:
    """The supplied-sheet table: {asset id: (file name, SHA-256)}."""
    rows = {}
    for line in body.splitlines():
        match = re.match(r"\| `(\d+)\.png` \|.*asset/(\d+)/\).*\| `([0-9a-f]{64})` \|$", line)
        if match:
            rows[match.group(2)] = (f"{match.group(1)}.png", match.group(3))
    return rows


def audit_failures(credits: str, records: dict, shipped: dict[str, str], referenced: set[str],
                   base_names: set[str], base_record: dict) -> set[str]:
    """Every way the credits, the provenance records and the built media disagree. `shipped` maps each built media
    member to its SHA-256; `referenced` is the md5ext set project.json names."""
    failures = set()
    found = sections(credits)
    hit_sections = set()

    for name in sorted(shipped):
        if not name.endswith(MEDIA_SUFFIXES):
            failures.add(f"not-media:{name}")
        if name not in referenced:
            failures.add(f"unreferenced:{name}")

    for name in sorted(shipped):
        if name in records:
            continue
        if name not in base_names:
            failures.add(f"untraced:{name}")
            continue
        hit_sections.add(BASE_HEADING)

    if any(name in base_names and name not in records for name in shipped):
        base = found.get(BASE_HEADING)
        if base is None:
            failures.add("missing-section:base")
        elif (str(BASE_ARCHIVE.relative_to(ROOT)) not in base
              or base_record.get("scratch_project", "\0") not in base
              or "assets/original/provenance.json" not in base):
            failures.add("base-section-unsourced")

    sounds_listed = {}
    spriters = spriters_rows(next(iter(found.values())))
    spriters_shipped = set()
    for name in sorted(shipped):
        record = records.get(name)
        if record is None:
            continue
        origin = record.get("origin", "")
        family = next((f for f in FAMILIES if f[1].search(origin)), None)
        if family is None:
            failures.add(f"unclassified:{name}")
            continue
        key, _pattern, prefix, phrase = family
        located = section_for(found, prefix)
        if located is None:
            failures.add(f"missing-section:{key}")
            continue
        heading, body = located
        hit_sections.add(heading)
        flat = " ".join(body.split())
        if phrase not in flat:
            failures.add(f"section-unsourced:{key}")
        license_text = record.get("license", "")
        phrase_for_license = next((v for k, v in LICENSE_PHRASES.items() if license_text.startswith(k)), None)
        # The supplied sheets' table states their license status in the section that follows it.
        stated = flat + (" " + " ".join(found.get("Rights status", "").split()) if key == "spriters" else "")
        if phrase_for_license is None or phrase_for_license not in stated:
            failures.add(f"license-unstated:{key}")
        if name.endswith(".wav"):
            sounds_listed[name] = (key, shipped[name] in SHA256.findall(body))
        if key == "spriters":
            asset = SPRITERS_SHEET.search(origin).group(1)
            spriters_shipped.add(asset)
            if asset not in spriters:
                failures.add(f"spriters-sheet-uncredited:{asset}")
            elif not origin.startswith("Gameplay-ready derivative") and shipped[name] != spriters[asset][1]:
                failures.add(f"spriters-sheet-hash:{asset}")

    for name, (key, listed) in sounds_listed.items():
        if not listed:
            failures.add(f"sound-unlisted:{name}")
    shipped_hashes = set(shipped.values())
    for key, prefix in (("extend-sound", "Extend / 1UP sound"), ("game-sounds", "Arcade gameplay sound effects")):
        located = section_for(found, prefix)
        if located is not None:
            for digest in SHA256.findall(located[1]):
                if digest not in shipped_hashes:
                    failures.add(f"credited-sound-not-shipped:{key}:{digest[:12]}")

    flat_credits = " ".join(credits.split())
    for asset, (file_name, _digest) in spriters.items():
        retired = re.search(rf"`{re.escape(file_name)}`[^.]*?no longer ships", flat_credits) is not None
        if retired and asset in spriters_shipped:
            failures.add(f"spriters-retired-but-shipped:{asset}")
        if not retired and asset not in spriters_shipped:
            failures.add(f"spriters-credited-not-shipped:{asset}")

    for heading in found:
        if heading in DESCRIPTIVE or heading in hit_sections or NOT_USED in heading:
            continue
        failures.add(f"credited-not-shipped:{heading}")
    return failures


class ReleaseMediaAudit(unittest.TestCase):
    # roadmap-evidence: RELEASE-02 success  (every member of the built .sb3 is referenced media with a provenance
    #   record or a base-project file; every record's family has its credited, sourced, licensed section; every
    #   sound and raw sheet is listed by SHA-256; every credit is backed by a shipped asset or marked unused)
    # roadmap-evidence: RELEASE-02 failure  (an untraced, unreferenced, unclassified or uncredited asset, a credit
    #   with nothing shipped behind it, a missing license statement or a sound off its table each fails)

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            built = Path(tmp) / "Xevious.sb3"
            scratch.build_project(output=built)
            with zipfile.ZipFile(built) as archive:
                project = json.loads(archive.read("project.json"))
                cls.shipped = {
                    name: hashlib.sha256(archive.read(name)).hexdigest()
                    for name in archive.namelist() if name != "project.json"
                }
        cls.referenced = {
            entry["md5ext"]
            for target in project["targets"]
            for entry in target.get("costumes", []) + target.get("sounds", [])
        }
        records = json.loads(OVERLAY_PROVENANCE.read_text(encoding="utf-8"))
        cls.records = records.get("assets", records)
        with zipfile.ZipFile(BASE_ARCHIVE) as archive:
            cls.base_names = set(archive.namelist()) - {"project.json"}
        cls.base_record = json.loads(BASE_PROVENANCE.read_text(encoding="utf-8"))
        cls.credits = CREDITS.read_text(encoding="utf-8")

    def args(self, **overrides):
        values = dict(credits=self.credits, records=self.records, shipped=self.shipped, referenced=self.referenced,
                      base_names=self.base_names, base_record=self.base_record)
        values.update(overrides)
        return values

    def test_build_ships_media_only(self):
        self.assertTrue(self.shipped)
        self.assertTrue(all(name.endswith(MEDIA_SUFFIXES) for name in self.shipped))

    def test_base_archive_is_the_recorded_one(self):
        self.assertEqual(hashlib.sha256(BASE_ARCHIVE.read_bytes()).hexdigest(), self.base_record["sha256"])

    def test_every_record_ships(self):
        # A provenance record with nothing shipped behind it would be a credit for an asset the build dropped.
        self.assertEqual(sorted(set(self.records) - set(self.shipped)), [])

    def test_credits_provenance_and_build_agree(self):
        self.assertEqual(audit_failures(**self.args()), set())

    def test_audit_bites(self):
        records = self.records
        flag_sheet = "6ca6cf679d389290d64bd6417973df2c.png"
        bgm = next(n for n, r in records.items() if "assets/game-sounds/bgm.wav" in r["origin"])
        bgm_hash = self.shipped[bgm]
        andor_only = {n: r for n, r in records.items() if "tools/andor_sprite_render.py" not in r["origin"]}
        andor_shipped = {n: h for n, h in self.shipped.items() if n in andor_only or n not in records}
        stray = "0" * 32 + ".png"
        mislabelled = dict(records)
        mislabelled[flag_sheet] = {**records[flag_sheet], "origin": "Drawn by hand"}
        relicensed = dict(records)
        relicensed[flag_sheet] = {**records[flag_sheet], "license": "Creative Commons Attribution 3.0 (CC BY 3.0)"}
        retired_crop = {"origin": "Gameplay-ready derivative of The Spriters Resource: Xevious (Arcade), Andor Genesis "
                                  "— https://www.spriters-resource.com/arcade/xevious/asset/42386/",
                        "license": records[flag_sheet]["license"]}
        cases = {
            "spriters-retired-but-shipped:42386": self.args(
                shipped={**self.shipped, stray: "0" * 64}, referenced=self.referenced | {stray},
                records={**records, stray: retired_crop}),
            "untraced:" + stray: self.args(shipped={**self.shipped, stray: "0" * 64},
                                           referenced=self.referenced | {stray}),
            "unreferenced:" + stray: self.args(shipped={**self.shipped, stray: "0" * 64},
                                               records={**records, stray: records[flag_sheet]}),
            "not-media:notes.txt": self.args(shipped={**self.shipped, "notes.txt": "0" * 64},
                                             referenced=self.referenced | {"notes.txt"},
                                             records={**records, "notes.txt": records[flag_sheet]}),
            "unclassified:" + flag_sheet: self.args(records=mislabelled),
            "missing-section:bonus-flag": self.args(
                credits=self.credits.replace("## Bonus Flag rendered", "## Bonus pennant rendered")),
            "license-unstated:bonus-flag": self.args(records=relicensed),
            "section-unsourced:andor": self.args(
                credits=self.credits.replace("by `tools/andor_sprite_render.py` into one",
                                             "by the Andor tool into one")),
            "sound-unlisted:" + bgm: self.args(credits=self.credits.replace(bgm_hash, "f" * 64)),
            "credited-sound-not-shipped:game-sounds:" + bgm_hash[:12]: self.args(
                shipped={**self.shipped, bgm: "e" * 64}),
            "spriters-credited-not-shipped:42386": self.args(
                credits=self.credits.replace("`42386.png` (Andor Genesis), no longer\nships",
                                             "`42386.png` (Andor Genesis), is\nkept")),
            "spriters-sheet-hash:42387": self.args(credits=self.credits.replace(
                "0cd8361108354d74c2ea9bfa9e22836acc66158c963eafdc5a02c9021f5b9da8", "a" * 64)),
            "credited-not-shipped:Andor Genesis parts rendered from the arcade sprite data": self.args(
                records=andor_only, shipped=andor_shipped),
            "base-section-unsourced": self.args(
                credits=self.credits.replace("https://scratch.mit.edu/projects/195680409/", "the Scratch site")),
            "credited-not-shipped:Terrain area map (fan map, cross-check only)": self.args(
                credits=self.credits.replace("## Terrain area map (fan map, cross-check only — not used by the build)",
                                             "## Terrain area map (fan map, cross-check only)")),
        }
        for expected, args in cases.items():
            with self.subTest(expected):
                self.assertIn(expected, audit_failures(**args))


CATALOG = ROOT / "docs" / "MECHANICS_CATALOG.md"
MECHANICS = ROOT / "docs" / "mechanics"
README = ROOT / "README.md"
SPEC = ROOT / "docs" / "spec"
# Wording that leaves a capability claim open. docs/spec/release.md states the rule itself, so it is not a
# capability document and is not scanned.
OPEN_UNCERTAINTY = re.compile(
    r"\*Uncertain:?\*|recorded (?:as )?uncertain|recorded uncertainty|carries a recorded|uncertain in practical",
    re.IGNORECASE,
)


def uncertainty_failures(docs: dict[str, str]) -> set[str]:
    """docs/spec/release.md: every Uncertain marker in the capability documents is resolved against the source or
    accepted as a recorded deviation — none is left open."""
    failures = set()
    for name, text in docs.items():
        for number, line in enumerate(text.splitlines(), 1):
            if OPEN_UNCERTAINTY.search(line):
                failures.add(f"open-uncertainty:{name}:{number}")
    return failures


def catalog_failures(catalog: str, records: set[str]) -> set[str]:
    """docs/spec/release.md: every catalog row is built, excluded, or an accepted deviation recorded in a mechanics
    record. A `present` row must point at the mechanics record(s) that built it; an `excluded` row must give its
    reason."""
    failures = set()
    rows = re.findall(r"^\| ([A-Z]+-\d+) \| [^|]+ \| (\w+) \|(.*)$", catalog, re.MULTILINE)
    if not rows:
        failures.add("no-rows")
    for rid, status, rest in rows:
        if status == "present":
            linked = re.findall(r"\]\(mechanics/([^)]+\.md)\)", rest)
            if not linked:
                failures.add(f"present-without-record:{rid}")
            for name in linked:
                if name not in records:
                    failures.add(f"dangling-record:{rid}:{name}")
        elif status == "excluded":
            cells = [cell.strip() for cell in rest.split("|")]
            if len(cells) < 3 or not cells[-2]:
                failures.add(f"excluded-without-reason:{rid}")
        else:
            failures.add(f"open-row:{rid}:{status}")
    return failures


class ReleaseCatalogAudit(unittest.TestCase):
    # roadmap-evidence: RELEASE-02 success  (every mechanics-catalog row is present with the record that built it,
    #   or excluded with its reason; the README carries the release notes and the shipped controls)
    # roadmap-evidence: RELEASE-02 failure  (a partial, missing, uncertain or unknown-status row, a present row
    #   with no or a dangling record, or an exclusion with no reason fails)

    def setUp(self):
        self.catalog = CATALOG.read_text(encoding="utf-8")
        self.records = {path.name for path in MECHANICS.glob("*.md")}

    def test_every_row_is_present_or_excluded(self):
        self.assertEqual(catalog_failures(self.catalog, self.records), set())

    def test_catalog_check_bites(self):
        cases = {
            "open-row:GND-01:partial": self.catalog.replace("| GND-01 | Barra and Garu Barra | present |",
                                                            "| GND-01 | Barra and Garu Barra | partial |"),
            "open-row:DIF-02:built": self.catalog.replace("| DIF-02 | Score-per-life adaptive AI | present |",
                                                          "| DIF-02 | Score-per-life adaptive AI | built |"),
            "present-without-record:GND-02": re.sub(r"(\| GND-02 \|.*?)\(\[041\]\(mechanics/041-ground-domes-and-turrets\.md\)\)",
                                                    r"\1", self.catalog),
            "dangling-record:GND-07:043-grobda-and-domogram.md": self.catalog,
            "excluded-without-reason:EX-06": self.catalog.replace("| Area 16 loops to area 7. |", "|  |"),
        }
        records = {
            "dangling-record:GND-07:043-grobda-and-domogram.md": self.records - {"043-grobda-and-domogram.md"},
        }
        for expected, text in cases.items():
            with self.subTest(expected):
                self.assertIn(expected, catalog_failures(text, records.get(expected, self.records)))

    def test_no_uncertain_marker_is_left_open(self):
        # roadmap-evidence: RELEASE-02 success  (the seven Uncertain markers the audit found are resolved against
        #   the pinned source or accepted as recorded deviations; no capability document leaves one open)
        docs = {path.name: path.read_text(encoding="utf-8") for path in SPEC.glob("*.md") if path.name != "release.md"}
        self.assertGreater(len(docs), 10)
        self.assertEqual(uncertainty_failures(docs), set())

    def test_uncertainty_check_bites(self):
        # roadmap-evidence: RELEASE-02 failure  (an open "*Uncertain:*" or "recorded uncertainty" line fails)
        for marker in ("*Uncertain:* the re-arm path is not pinned.", "This carries a recorded uncertainty.",
                       "a variant recorded as uncertain in practical reach"):
            with self.subTest(marker):
                self.assertEqual(uncertainty_failures({"x.md": "fine\n" + marker}), {"open-uncertainty:x.md:2"})

    def test_readme_carries_the_release(self):
        readme = README.read_text(encoding="utf-8")
        self.assertIn("## Release notes", readme)
        controls = readme.split("## Controls", 1)[1].split("\n## ", 1)[0]
        for stale in ("fixture", "proof of concept"):
            self.assertNotIn(stale, readme.split("## Repository layout", 1)[0])
        self.assertNotRegex(controls, r"(?m)^- [DGSPT]:")


# The keys the shipped game reads: the arrows steer the craft and the title/initials selectors, Space fires and
# starts, B bombs, C inserts a coin (docs/spec/core-game-systems.md, control mapping).
SHIPPED_KEYS = {"left arrow", "right arrow", "up arrow", "down arrow", "space", "b", "c"}
INVULN = "invuln"
# The game is played on the keyboard alone: no click hat, mouse read or typed-answer prompt ships.
POINTER_AND_PROMPT_OPCODES = {
    "event_whenthisspriteclicked",
    "event_whenstageclicked",
    "sensing_mousedown",
    "sensing_mousex",
    "sensing_mousey",
    "sensing_askandwait",
    "sensing_answer",
}


def debug_control_failures(project: dict) -> set[str]:
    """Every way a development control could survive into the build: a key read outside the control mapping or a
    key read whose key is computed (so no menu names it), a click hat, a mouse read or a typed-answer prompt, a
    variable, list, broadcast or custom block named for debugging, or any block that sets the harness-only
    invulnerability hook (the game never sets it, so no key or control can reach it)."""
    failures = set()
    for target in project["targets"]:
        name = target["name"]
        for kind in ("variables", "lists", "broadcasts"):
            for value in target.get(kind, {}).values():
                label = value[0] if isinstance(value, list) else value
                if "debug" in str(label).lower():
                    failures.add(f"debug-{kind}:{name}:{label}")
        blocks = target["blocks"]
        for block in blocks.values():
            if not isinstance(block, dict):
                continue
            opcode = block.get("opcode")
            if opcode in ("sensing_keyoptions", "event_whenkeypressed"):
                key = block["fields"]["KEY_OPTION"][0]
                if key not in SHIPPED_KEYS:
                    failures.add(f"key:{name}:{key}")
            elif opcode == "sensing_keypressed":
                # The key must be the plain menu shadow; a reporter dropped over it (input type 3) could name any
                # key at run time, and the menu check above would never see it.
                key_input = block["inputs"].get("KEY_OPTION")
                menu = blocks.get(key_input[1]) if key_input and isinstance(key_input[1], str) else None
                if key_input is None or key_input[0] != 1 or not (
                    isinstance(menu, dict) and menu.get("opcode") == "sensing_keyoptions"
                ):
                    failures.add(f"computed-key:{name}")
            elif opcode in POINTER_AND_PROMPT_OPCODES:
                failures.add(f"pointer:{name}:{opcode}")
            elif opcode == "procedures_prototype" and "debug" in block["mutation"]["proccode"].lower():
                failures.add(f"debug-proc:{name}:{block['mutation']['proccode']}")
            elif opcode in ("data_setvariableto", "data_changevariableby"):
                if block["fields"]["VARIABLE"][0] == INVULN:
                    failures.add(f"invuln-written:{name}")
    return failures


class ReleaseNoDebugControls(unittest.TestCase):
    # roadmap-evidence: RELEASE-02 success  (the built project reads only the mapped keys — arrows, Space, B, C —
    #   names no debug variable, list, broadcast or custom block, and never sets the invulnerability hook)
    # roadmap-evidence: RELEASE-02 failure  (a T key read, a debug-named variable or custom block, or a block that
    #   sets `invuln` each fails)

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            built = Path(tmp) / "Xevious.sb3"
            scratch.build_project(output=built)
            with zipfile.ZipFile(built) as archive:
                cls.project = json.loads(archive.read("project.json"))

    def test_no_debug_control_ships(self):
        self.assertEqual(debug_control_failures(self.project), set())
        stage = next(t for t in self.project["targets"] if t["isStage"])
        self.assertIn(INVULN, {value[0] for value in stage["variables"].values()})

    def test_debug_control_check_bites(self):
        def corrupt(edit):
            project = json.loads(json.dumps(self.project))
            stage = next(t for t in project["targets"] if t["isStage"])
            edit(stage)
            return debug_control_failures(project)

        def key_t(stage):
            stage["blocks"]["neg-key"] = {"opcode": "sensing_keyoptions", "fields": {"KEY_OPTION": ["t", None]},
                                          "inputs": {}, "next": None, "parent": None, "shadow": True,
                                          "topLevel": False}

        def debug_var(stage):
            stage["variables"]["neg-var"] = ["debug spawn index", 0]

        def debug_proc(stage):
            stage["blocks"]["neg-proc"] = {"opcode": "procedures_prototype", "fields": {}, "inputs": {},
                                           "mutation": {"proccode": "debug pause toggle"}, "next": None,
                                           "parent": None, "shadow": True, "topLevel": False}

        def set_invuln(stage):
            invuln_id = next(key for key, value in stage["variables"].items() if value[0] == INVULN)
            stage["blocks"]["neg-set"] = {"opcode": "data_setvariableto", "fields": {"VARIABLE": [INVULN, invuln_id]},
                                          "inputs": {}, "next": None, "parent": None, "shadow": False,
                                          "topLevel": True}

        def computed_key(stage):
            stage["blocks"]["neg-reporter"] = {"opcode": "data_variable", "fields": {}, "inputs": {}, "next": None,
                                               "parent": "neg-pressed", "shadow": False, "topLevel": False}
            stage["blocks"]["neg-menu"] = {"opcode": "sensing_keyoptions", "fields": {"KEY_OPTION": ["space", None]},
                                           "inputs": {}, "next": None, "parent": "neg-pressed", "shadow": True,
                                           "topLevel": False}
            stage["blocks"]["neg-pressed"] = {"opcode": "sensing_keypressed",
                                              "inputs": {"KEY_OPTION": [3, "neg-reporter", "neg-menu"]},
                                              "fields": {}, "next": None, "parent": None, "shadow": False,
                                              "topLevel": True}

        def click_hat(stage):
            stage["blocks"]["neg-click"] = {"opcode": "event_whenstageclicked", "fields": {}, "inputs": {},
                                            "next": None, "parent": None, "shadow": False, "topLevel": True}

        self.assertIn("key:Stage:t", corrupt(key_t))
        self.assertIn("computed-key:Stage", corrupt(computed_key))
        self.assertIn("pointer:Stage:event_whenstageclicked", corrupt(click_hat))
        self.assertIn("debug-variables:Stage:debug spawn index", corrupt(debug_var))
        self.assertIn("debug-proc:Stage:debug pause toggle", corrupt(debug_proc))
        self.assertIn("invuln-written:Stage", corrupt(set_invuln))


# The lists the build ships on purpose although no block reads them, each with its reason. Anything else no block
# names is left over from a retired mechanic (generation keeps variables it does not recognise).
UNREAD_DATA_TABLES = {
    # ECO-03: the five-craft bonus tables, carried beside the three-craft ones the port's fixed lives setting reads,
    # so the whole arcade table ships (scores.json).
    "eco-first-bonus-5",
    "eco-repeat-bonus-5",
    # AREA-02: every schedule record's raw parameters, carried whole so no field of the arcade table is dropped;
    # the runtime reads the decoded columns.
    "area-schedule-payload",
}


def _named_ids(node, out: set[str]) -> None:
    """Collect every variable or list id a block names: in a VARIABLE/LIST field, or a compact [12|13, name, id]
    reporter in an input."""
    if isinstance(node, dict):
        for value in node.values():
            _named_ids(value, out)
    elif isinstance(node, list):
        if len(node) >= 3 and node[0] in (12, 13) and isinstance(node[2], str):
            out.add(node[2])
        for value in node:
            _named_ids(value, out)


def unused_state_failures(project: dict) -> set[str]:
    named: set[str] = set()
    for target in project["targets"]:
        for block in target["blocks"].values():
            if isinstance(block, dict):
                _named_ids(block.get("inputs", {}), named)
                for field in block.get("fields", {}).values():
                    if isinstance(field, list) and len(field) >= 2 and isinstance(field[1], str):
                        named.add(field[1])
            else:
                _named_ids(block, named)
    failures = set()
    shipped = set()
    for target in project["targets"]:
        for kind in ("variables", "lists"):
            for var_id, value in target.get(kind, {}).items():
                shipped.add(var_id)
                if var_id not in named and var_id not in UNREAD_DATA_TABLES:
                    failures.add(f"unused-{kind}:{target['name']}:{value[0]}")
    for var_id in UNREAD_DATA_TABLES:
        if var_id not in shipped or var_id in named:
            failures.add(f"stale-exception:{var_id}")
    return failures


class ReleaseNoUnusedState(unittest.TestCase):
    """The release review found a title clone counter left in the build after the Stage clock replaced it. Every
    variable and list the build ships is named by some block, apart from the data tables listed above."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            built = Path(tmp) / "Xevious.sb3"
            scratch.build_project(output=built)
            with zipfile.ZipFile(built) as archive:
                cls.project = json.loads(archive.read("project.json"))

    def test_every_shipped_variable_is_used(self):
        self.assertEqual(unused_state_failures(self.project), set())

    def test_unused_state_check_bites(self):
        project = json.loads(json.dumps(self.project))
        screen = next(t for t in project["targets"] if t["name"] == "start_screen")
        screen["variables"]["attract-display-tick"] = ["attract tick", 0]
        self.assertEqual(unused_state_failures(project), {"unused-variables:start_screen:attract tick"})
        # A listed table that a block starts to read must leave the exception list.
        stage = next(t for t in project["targets"] if t["isStage"])
        stage["blocks"]["neg-read"] = {"opcode": "data_itemoflist", "fields": {"LIST": ["schedule payload",
                                       "area-schedule-payload"]}, "inputs": {}, "next": None, "parent": None,
                                       "shadow": False, "topLevel": True}
        self.assertIn("stale-exception:area-schedule-payload", unused_state_failures(project))


if __name__ == "__main__":
    unittest.main()
