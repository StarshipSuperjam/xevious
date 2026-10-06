"""Measure scripts in the Scratch 3 editor's own block renderer, to check tools/script_layout.py.

script_layout models how tall scratch-blocks draws each block; tests/test_script_layout.py
compares that model with heights measured in the real renderer, held in
tests/fixtures/script_layout_editor_measurements.json. This tool repeats the measurement, for
the fixture's sample scripts or for any sb3 (such as the shipped build):

1. Get the renderer: in any folder outside the repository, `npm pack scratch-blocks@1.3.0`
   downloads scratch-blocks-1.3.0.tgz. The VM is the harness's (`npm ci` in harness/).
2. `python3 tools/script_layout_measure.py prepare OUT --scratch-blocks path/to/scratch-blocks-1.3.0.tgz`
   fills the folder OUT with the measuring page, the renderer, the VM, `measure.sb3` and
   `model.json` (the model's height for every block). With no `--sb3`, `measure.sb3` is
   dist/Xevious.sb3 with the fixture's sample scripts in a sprite named "script-layout samples";
   `--sb3 FILE` measures that file as it is.
3. `python3 -m http.server 8000 --bind 127.0.0.1 --directory OUT` and open
   http://127.0.0.1:8000/ in a browser. The page renders every sprite as the editor does and
   lists, per sprite, any blocks that failed to load, scripts that overlap or sit other than 48
   apart, and blocks whose height differs from the model.
4. To record the samples: save the page's "script-layout samples" measurements as
   OUT/measured.json and run `python3 tools/script_layout_measure.py write-fixture OUT`.

To measure a new kind of block: open OUT/measure.sb3 in the Scratch editor, add example scripts
to the "script-layout samples" sprite, save it, run step 2 again with `--sb3` pointing at the
saved file, then steps 3 and 4. A block the model cannot place yet shows `model: null`.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import script_layout  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/script_layout_editor_measurements.json"
PAGE = ROOT / "tools/script_layout_measure.html"
VM = ROOT / "harness/node_modules/scratch-vm/dist/web/scratch-vm.js"
SHIPPED = ROOT / "dist/Xevious.sb3"
SAMPLES = "script-layout samples"
RENDERER = "scratch-blocks 1.3.0 dist/web/vertical.js (the block renderer behind the Scratch 3 editor)"
LOADED_THROUGH = (
    "scratch-vm 5.0.300: loadProject, setEditingTarget, then the workspaceUpdate XML into "
    "Blockly.Xml.clearWorkspaceAndLoadFromXml, as scratch-gui does"
)
METHOD = (
    "Measured with tools/script_layout_measure.py (its docstring has the steps): the sample blocks "
    "below, in a sprite of an sb3, opened in that renderer at scale 1. block_heights is each drawn "
    "block's `height` (scratch-blocks BlockSvg.height); stack_heights is each top-level block's "
    "getHeightWidth().height. A shadow covered by another block is not drawn, so it has no measurement."
)


def _declare_names(project: dict[str, Any], target: dict[str, Any]) -> None:
    """Declare every variable, list and message the sample blocks name, so the VM loads them."""
    stage = next(t for t in project["targets"] if t["isStage"])

    def visit(value: Any) -> None:
        if isinstance(value, list) and len(value) >= 3 and value[0] in (11, 12, 13):
            kind, name, item_id = value[:3]
            if kind == 12:
                target["variables"].setdefault(item_id, [name, 0])
            elif kind == 13:
                target["lists"].setdefault(item_id, [name, []])
            else:
                stage["broadcasts"].setdefault(item_id, name)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for block in target["blocks"].values():
        for value in block["inputs"].values():
            visit(value)
        for field, (name, item_id) in ((f, v[:2]) for f, v in block["fields"].items()):
            if item_id is None:
                continue
            if field == "VARIABLE":
                target["variables"].setdefault(item_id, [name, 0])
            elif field == "LIST":
                target["lists"].setdefault(item_id, [name, []])
            elif field == "BROADCAST_OPTION":
                stage["broadcasts"].setdefault(item_id, name)


def _samples_sb3() -> bytes:
    """dist/Xevious.sb3 with its last sprite renamed "script-layout samples" and its scripts
    replaced by the fixture's sample blocks."""
    with zipfile.ZipFile(SHIPPED) as source:
        project = json.loads(source.read("project.json"))
        files = {name: source.read(name) for name in source.namelist() if name != "project.json"}
    target = [t for t in project["targets"] if not t["isStage"]][-1]
    target["name"] = SAMPLES
    target["blocks"] = json.loads(FIXTURE.read_text())["blocks"]
    target["comments"] = {}
    script_layout.lay_out(target["blocks"])
    _declare_names(project, target)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as sb3:
        sb3.writestr("project.json", json.dumps(project))
        for name, data in files.items():
            sb3.writestr(name, data)
    return out.getvalue()


def _model_heights(sb3: bytes) -> dict[str, dict[str, int | None]]:
    """The model's height for every block of every target; None where it cannot place one."""
    with zipfile.ZipFile(io.BytesIO(sb3)) as source:
        project = json.loads(source.read("project.json"))
    heights: dict[str, dict[str, int | None]] = {}
    for target in project["targets"]:
        blocks = target["blocks"]
        per_block: dict[str, int | None] = {}
        for block_id, block in blocks.items():
            if not isinstance(block, dict):
                continue
            try:
                per_block[block_id] = script_layout.block_height(blocks, block_id)
            except ValueError:
                per_block[block_id] = None
        heights[target["name"]] = per_block
    return heights


def prepare(out: Path, scratch_blocks: Path, sb3_path: Path | None) -> None:
    if not VM.exists():
        raise SystemExit(f"no scratch-vm at {VM}: run `npm ci` in harness/ first")
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(scratch_blocks) as package:
        renderer = package.extractfile("package/dist/web/vertical.js")
        if renderer is None:
            raise SystemExit(f"{scratch_blocks} has no package/dist/web/vertical.js")
        (out / "vertical.js").write_bytes(renderer.read())
        for member in package.getmembers():
            if member.isfile() and member.name.startswith("package/media/"):
                destination = out / "media" / member.name[len("package/media/"):]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(package.extractfile(member).read())
    shutil.copyfile(VM, out / "scratch-vm.js")
    shutil.copyfile(PAGE, out / "index.html")
    sb3 = sb3_path.read_bytes() if sb3_path else _samples_sb3()
    (out / "measure.sb3").write_bytes(sb3)
    (out / "model.json").write_text(json.dumps(_model_heights(sb3)))
    print(f"ready: python3 -m http.server 8000 --bind 127.0.0.1 --directory {out}")


def write_fixture(out: Path) -> None:
    measured = json.loads((out / "measured.json").read_text())
    with zipfile.ZipFile(out / "measure.sb3") as source:
        project = json.loads(source.read("project.json"))
    target = next((t for t in project["targets"] if t["name"] == SAMPLES), None)
    if target is None:
        raise SystemExit(f"{out / 'measure.sb3'} has no sprite named {SAMPLES!r}")
    fixture = {
        "renderer": RENDERER,
        "loaded_through": LOADED_THROUGH,
        "method": METHOD,
        "blocks": target["blocks"],
        "block_heights": dict(sorted(measured["blocks"].items())),
        "stack_heights": dict(sorted(measured["stacks"].items())),
    }
    FIXTURE.write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"wrote {FIXTURE.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="fill a folder with the measuring page and what it measures")
    prep.add_argument("out", type=Path)
    prep.add_argument("--scratch-blocks", type=Path, required=True, help="scratch-blocks-1.3.0.tgz from npm pack")
    prep.add_argument("--sb3", type=Path, help="measure this sb3 as it is, instead of the fixture's samples")
    record = commands.add_parser("write-fixture", help="record a folder's sample measurements in the fixture")
    record.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare(args.out, args.scratch_blocks, args.sb3)
    else:
        write_fixture(args.out)


if __name__ == "__main__":
    main()
