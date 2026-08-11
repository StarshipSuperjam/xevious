#!/usr/bin/env python3
"""The core validation dispatcher — a thin core over a kind registry.

The check *inventory* is data (.engine/check/*.json) and the check *logic* is a
small registry of kind callables, so adding a check adds a rule file and never
edits this dispatcher (the validation design). This is the
`core` validation engine the stage-0 seed validator grew into; the engine ships
here, while the engine-self-validation rule *corpus* rides `validators-core`
so only the three grandfathered seed rules are committed
(PR-body completeness, link integrity, and the re-homed protection guard).

The five closed core kinds, plus the `custom/script` escape hatch:
  - presence  — named sections/fields are present and non-empty.
  - schema    — a structured file, or a prose surface's YAML frontmatter, conforms to its
                governing JSON Schema (2020-12), resolved catalog-first (`governing_schema`),
                with a `params.schema` override for the cases the catalog cannot express.
                The loader is chosen by surface class: prose frontmatter is read by the
                YAML `frontmatter` reader, every other target by `load_json`.
  - shape     — a prose instance matches a template's shape-spec (required and
                allowed sections, ordering, a soft length budget).
  - coverage  — referential integrity; `params.mode` selects `links` (every relative
                Markdown link resolves) or `catalog` (every catalogued surface has its
                directory; no orphan surface directory).
  - coherence — the installed module set is consistent (dependency presence, acyclicity,
                version range). A directly-callable library entry the module manager
                invokes after an install; in core it ships built + fixture-tested, with
                the module manager as its live consumer after an install.
  - custom/script — the escape hatch: run a committed script and map its result to
                findings (the guardrail-weakening guards re-home onto this kind).
Module-provided kinds bind by PRESENCE and must NOT extend the hardcoded REGISTRY below (which
holds the closed core set + the `custom/script` escape hatch): a module drops a conforming
`.engine/tools/<module>/kind_<name>.py` exposing `check(rule, ctx)`, and `resolved_registry()`
discovers it and merges it OVER a pristine snapshot of the core (core always wins). See the
discovery block after REGISTRY.

Each kind callable returns a Result: a pass/fail verdict plus zero or more findings
on the canonical finding.v1 base {severity, message, location}. A check finding's
severity is the rule's tier (`hard` | `soft`).

Suites and triggers: a suite is a thin declaration (.engine/suites.json) — a name,
a trigger, and an execution context — never a list of its rules; a rule self-declares
which suites it joins, so a suite's roster is derived. Only a `blocking-gate` context
(the CI suite) lets a hard finding fail the run; `local-nudge` and `report-only`
contexts never block. A rule whose kind is unregistered, or whose callable errors,
FAILS CLOSED in a gating context, so a hard governance rule can never be silently
un-enforced. A malformed suites.json or check/schema file fails loud.

Usage:
  validate.py --suite CI                      # run the CI suite (default)
  validate.py --suite CI --pr-body-file PATH  # supply the PR body explicitly
  validate.py --check engine/check/<id>       # run ONE rule by id, outside any suite

A check is also a directly-callable unit, not only a trigger-driven one: --check
runs the single rule with that `id` and gates on a hard finding (exit 1), with no
suite involved. This is how a guard that must run from the trusted base — the
guardrail-weakening guard — is invoked from its own workflow (engine-guard.yml),
NOT from the head-checkout CI suite, so a pull request cannot run its own edited
guard. The by-id path loads only the check rules, never the
suite declarations, so a broken or loosened suites.json cannot strand or alter it.

The PR body is read from --pr-body-file, else from $GITHUB_EVENT_PATH
(.pull_request.body — the safe path: never interpolated into a shell command), else
treated as unavailable (the PR-body presence check fails OPEN locally, evaluates in CI).
"""
from __future__ import annotations
import datetime
import glob as _glob
import json
import os
import math
import re
import subprocess
import sys

# yaml + jsonschema are the third-party dependencies THIS module needs; they live in the
# uv-managed tool-runtime (.engine/.venv/). They are bound LAZILY (PEP 562 module
# __getattr__) rather than imported at module top, so `import validate` succeeds on the
# Python standard library alone — before that runtime exists. This is load-bearing for
# the first-run instantiator: it is the one engine tool that must run to BOOTSTRAP the
# runtime (it installs uv, then `uv sync`), so it cannot presuppose the packages the
# runtime provides (the tool-runtime bootstrap). When the
# runtime IS present the symbols resolve on first use exactly as a top-level import
# would — every `validate.<symbol>` consumer (e.g. wiring's ontology-entry check, the
# schema-validation tests) and validate's own frontmatter/schema paths are unchanged.
# A genuinely-absent package still raises ImportError at first use (fail-loud), never
# silently. Internal uses below additionally take a local import for the same reason
# (a bare module-global lookup does not trigger this module __getattr__).
_LAZY_THIRD_PARTY = {
    "yaml": ("yaml", None),
    "Draft202012Validator": ("jsonschema", "Draft202012Validator"),
    "SchemaError": ("jsonschema.exceptions", "SchemaError"),
}


def __getattr__(name):                                 # PEP 562: called only for names absent from globals()
    spec = _LAZY_THIRD_PARTY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    module_name, attr = spec
    mod = importlib.import_module(module_name)          # raises ImportError loudly if the runtime is absent
    value = mod if attr is None else getattr(mod, attr)
    globals()[name] = value                            # cache: later access binds directly, skipping __getattr__
    return value


THIS = os.path.abspath(__file__)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(THIS)))  # .engine/tools/validate.py -> repo root
ENGINE_DIR = os.path.join(ROOT, ".engine")
TOOLS_DIR = os.path.dirname(THIS)  # .engine/tools — where a module-provided check-kind callable is discovered
CHECK_DIR = os.path.join(ENGINE_DIR, "check")
SCHEMAS_DIR = os.path.join(ENGINE_DIR, "schemas")
CATALOG_PATH = os.path.join(SCHEMAS_DIR, "surface-catalog.json")
SUITES_PATH = os.path.join(ENGINE_DIR, "suites.json")
SUITES_SCHEMA_PATH = os.path.join(SCHEMAS_DIR, "suites.v1.json")
# The 2020-12 dialect URI. A governing_schema of this value means "this file IS a
# schema; check its well-formedness against the bundled meta-schema" rather than
# "validate this file against a sibling schema". Matched, never fetched (offline).
META_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

LINK_RE = re.compile(r"\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")          # a level-2 (## ) heading; ### does not match
_ANY_HEADING_LINE = re.compile(r"^#{1,6}[ \t]+\S")   # any markdown heading line (## … or ### …), for emptiness
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")             # a fenced-code-block delimiter (``` or ~~~); toggles in/out
PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")             # a prompt token (decoration stripped), e.g. <why this exists>
LIST_MARKER_RE = re.compile(r"^[-*+]\s+")             # a leading unordered-list bullet marker
EMPHASIS_RE = re.compile(r"^(\*\*|__|\*|_)(.+?)\1$")  # a surrounding bold/italic emphasis wrapper


# ---- finding.v1 ------------------------------------------------------------

def finding(severity: str, message: str, location: dict | None = None) -> dict:
    """A finding on the canonical finding.v1 base {severity, message, location}."""
    return {"severity": severity, "message": message, "location": location}


def disclosed_noop(message: str, location: dict | None = None) -> dict:
    """A DISCLOSED not-applicable finding: a check reporting "this doesn't apply in this
    context, nothing to do" — a disclosed no-op, never a silent skip (the design's
    disclosed-not-applicable grammar). Always `soft` (a no-op is by definition non-gating), and
    carries the optional `not_applicable` marker so report() can collapse these dormant notes
    away from the actionable ones. The marker is an additive finding.v1 key (the base fixes no
    closed property set); a finding WITHOUT it defaults to actionable, so the fail-safe is a
    no-op shown in full, never an actionable note hidden."""
    return {"severity": "soft", "message": message, "location": location, "not_applicable": True}


def env_override_path(var: str, default: "str | None" = None) -> "str | None":
    """Resolve an input-substitution env var to a path — the one shared seam the negative-fixture
    meta-check's custom/script units use (StarshipSuperjam/engine-template#286). When `var` is set and non-empty,
    return it resolved under ROOT (an absolute value is used as-is); otherwise return `default`
    unchanged. So when the variable is UNSET — every production run — the caller gets its own
    default and behaviour is byte-unchanged; the seam is inert outside a `run_unit` fixture run,
    which is the only path that sets the variable (around the child, restored after). One helper,
    one relative-to-ROOT resolution rule, so every seam is the same single audit rather than a
    dozen hand-rolled `os.environ`+`join` blocks."""
    value = os.environ.get(var)
    if not value:
        return default
    return value if os.path.isabs(value) else os.path.join(ROOT, value)


def loc(path: str, line: int | None = None) -> dict:
    return {"file": os.path.relpath(path, ROOT), "line": line}


def _is_pos_int(v) -> bool:
    """A positive integer, excluding bool (a Python int subclass) so True/False can
    never pass as a budget or count."""
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


# ---- shared helpers --------------------------------------------------------

def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# A `----- SECTION MARKER -----` prompt fence is two 3+-dash rails around words. To neutralize a line of
# UNTRUSTED text that could forge or prematurely close such a fence, look for the give-away: TWO dash rails
# on ONE line, with a letter present. A linear findall + an alpha scan (no backtracking regex, so no
# pathological input) catches every variant a line-anchored match would miss — text trailing or leading a
# rail, no spaces around the rails, a tab indent — while a SINGLE rail (a horizontal rule, `8<----- cut`),
# a letterless table delimiter row (`| --- | --- |`), an ISO date (`2026-06-01`), or a `--flag` is untouched.
# The rail class is ASCII hyphen plus the look-alike unicode dashes/bars (en/em dash, horizontal bar, minus,
# box-drawing) so a visually-identical forgery in another codepoint cannot slip past — a model reads the
# fence by its shape, not its bytes.
# ASCII hyphen (leading, so literal in the class), the U+2010–U+2015 dash/bar run, the minus sign, and the
# light/heavy box-drawing horizontals — the glyphs a forged rail could be drawn from.
_RAIL_CHARS = "-‐-―−─━"
_PROMPT_FENCE_RAIL_RE = re.compile("[" + _RAIL_CHARS + "]{3,}")

# The imperative relay marker, the OTHER control token an untrusted line could forge. The engine reserves this
# exact phrase for the must-push set: carrying it is what compels the model to push an item to the operator as
# the engine's own words. So a milestone title, a merged-PR title, or a recalled note that SPEAKS it would read
# as a genuine engine alarm ("… their safety gate is off, run this") in every cold-boot pack.
# Held here rather than imported from boot: this is the floor every producer of untrusted AI-facing text
# already calls, and boot imports validate, not the reverse. `test_boot` pins it to `boot.RELAY_MARKER`, so the
# two cannot drift apart silently.
_RELAY_MARKER = "INFORM THE USER THAT"
# Matched across ANY run of whitespace between the words, not the single spaces the engine happens to emit:
# an exact-literal pattern is beaten by typing two spaces, and the paths this guards (a merged-PR title from
# an outside contributor, a finding title quoting a check-run name) are exactly where someone would.
_RELAY_MARKER_RE = re.compile(r"\s+".join(re.escape(w) for w in _RELAY_MARKER.split()), re.IGNORECASE)


def defang_prompt_fence_markers(text: str) -> str:
    """Neutralize any line of UNTRUSTED `text` that could forge a control token the AI-facing briefing
    reserves for the engine's own voice, so content quoted into that briefing cannot speak as the engine.

    Two tokens, the same principle — the words are kept (no information is dropped); only the reserved FORM
    is destroyed:
      - a `----- SECTION MARKER -----` prompt fence: a line carrying TWO-or-more dash rails AND a letter has
        its dash runs trimmed to two dashes, so it can no longer read as a fence delimiter and content cannot
        break out of its region. A line with a single rail (a horizontal rule), no letters (a table delimiter
        row), or no 3-dash run at all (an ISO date, a `--flag`) is left exactly as it is.
      - the imperative relay marker: the reserved phrase is lowercased wherever it appears, so the line no
        longer carries the engine's must-push directive. HONEST BOUND: the fence trim is structural, but this
        one is read by a MODEL, not a parser. Lowercasing removes the reserved token the engine actually
        emits and the glossary defines, which is a real reduction in force — not a proof the model cannot be
        swayed by the words themselves. Do not read more into it than that: some callers additionally quote
        and attribute the text they pass (the recalled-decisions block says "attributed, not confirmed"),
        but others interpolate it bare into a line, so that is a property of those callers and not of this.

    Linear in the text length — no regex backtracking."""
    out = []
    for line in text.split("\n"):
        if len(_PROMPT_FENCE_RAIL_RE.findall(line)) >= 2 and any(c.isalpha() for c in line):
            line = _PROMPT_FENCE_RAIL_RE.sub("--", line)   # trim every rail so the line can't be a fence
        line = _RELAY_MARKER_RE.sub(lambda m: m.group(0).lower(), line)
        out.append(line)
    return "\n".join(out)


def load_json(path: str):
    """Parse a JSON file to a data object. Raises (loud) on a missing or malformed
    file — the halt-on-malformed posture: a broken structured file fails loud rather
    than misleading the AI (the halt-on-malformed design commitment)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _json_model(obj):
    """Render a YAML-loaded object into the JSON data model JSON Schema governs. YAML's
    native types are richer than JSON's: an unquoted ISO-8601 value (e.g. `date: 2026-06-03`)
    loads as a datetime.date, which a `{"type": "string"}` schema would reject — so a
    date/datetime scalar is rendered to its ISO-8601 string form, while numbers, booleans,
    null, and strings stay native (so `persistence: 3` stays a number). Recurses through
    mappings and sequences. This keeps frontmatter authors free of quoting rituals: the
    reader, not the document, reconciles YAML's type system with the schema's."""
    if isinstance(obj, dict):
        return {k: _json_model(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_model(v) for v in obj]
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return obj


def frontmatter(path: str) -> dict:
    """Parse a prose file's YAML frontmatter (the block between the first two `---`
    fences) to a data object, normalized to the JSON data model (see _json_model). This
    is the frontmatter reader the validation foundation calls for ("parses
    a file or its YAML frontmatter to a data object before validating"). A file that does not
    open with a `---` fence yields {} — the governing
    schema's `required` then catches a frontmatter-less file. Malformed YAML RAISES (loud),
    caught by the caller as a fail-closed finding (the halt-on-malformed posture).
    `safe_load` only, never `load`: no arbitrary object construction from frontmatter."""
    import yaml                                         # lazy: see the module __getattr__ note (tool-runtime dep)
    text = read(path)
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)          # maxsplit=2: a `---` thematic break in the body stays in the body
    if len(parts) < 3:
        return {}
    data = yaml.safe_load(parts[1])
    return _json_model(data) if isinstance(data, dict) else {}


def target_files(rule: dict) -> list:
    """Repository files a rule's target.path glob selects (absolute paths, sorted).
    A target without a path (e.g. a `context` target) selects no files here."""
    pattern = (rule.get("target") or {}).get("path")
    if not pattern:
        return []
    matched = _glob.glob(os.path.join(ROOT, pattern), recursive=True)
    return sorted(p for p in matched if os.path.isfile(p))


def _body_without_frontmatter(text: str) -> str:
    """The prose body with a leading YAML frontmatter block (`---` ... `---`) removed.
    Templates govern the body only, so frontmatter is neither a
    section nor counted against the body length budget. A file with no opening `---`
    fence is returned unchanged; a `---` thematic break in the body stays in the body
    (only the first two fences are consumed — the same split the `frontmatter` reader uses)."""
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)          # maxsplit=2: keep any later `---` in the body
    return parts[2] if len(parts) >= 3 else text


def section_blocks(body: str) -> dict:
    """{heading_text: section_body} for each level-2 heading in the body. A `## ` line
    inside a fenced code block (``` or ~~~) is code, not a heading, and is not counted."""
    blocks, current, buf, in_fence = {}, None, [], False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            m = None
        else:
            m = None if in_fence else HEADING_RE.match(line)
        if m:
            if current is not None:
                blocks[current] = "\n".join(buf)
            current, buf = m.group(1), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        blocks[current] = "\n".join(buf)
    return blocks


def section_order(body: str) -> list:
    """The level-2 heading texts in order, skipping any `## ` inside a fenced code block."""
    order, in_fence = [], False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and (m := HEADING_RE.match(line)):
            order.append(m.group(1))
    return order


def _placeholder_only(line: str) -> bool:
    """True if the line, once its Markdown scaffolding is stripped — a leading list
    marker and any surrounding bold/italic emphasis — is nothing but a single <...>
    prompt token. So a decorated, unfilled template slot (**<summary>**, - <detail>,
    *<Impact: ...>*) still counts as a placeholder, while a line carrying real text
    (even one with an inline <token>) does not reduce to a bare token and is kept."""
    s = LIST_MARKER_RE.sub("", line.strip()).strip()
    prev = None
    while prev != s:                       # peel possibly-nested emphasis wrappers
        prev = s
        m = EMPHASIS_RE.match(s)
        if m:
            s = m.group(2).strip()
    return bool(PLACEHOLDER_RE.match(s))


def _label_remainder(line: str, label: str):
    """If `line` is the labelled sub-line for `label`, return the text after the label
    (possibly ''); otherwise None. The line is recognized when, after its list marker
    and whole-line emphasis are stripped, the label leads it — the shipped italic form
    (`*Impact: ...*`) or a bare `Impact: ...`, or wholly wrapped in the old sentinel
    (`<Impact: ...>`, always treated as unfilled, so it returns ''). Keyed to the specific
    label, so an unrelated labelled line like `See: <url>` is never matched — this is what
    keeps the emptiness leg of other presence checks untouched."""
    s = LIST_MARKER_RE.sub("", line.strip()).strip()
    prev = None
    while prev != s:                       # peel possibly-nested whole-line emphasis
        prev = s
        m = EMPHASIS_RE.match(s)
        if m:
            s = m.group(2).strip()
    want = label.lower() + ":"
    if s.startswith("<") and s.endswith(">") and s[1:].lower().startswith(want):
        return ""                          # old fully-wrapped sentinel is always unfilled
    if s.lower().startswith(want):
        return s[len(want):].strip()
    return None


def is_empty_section(text: str, label: str | None = None) -> bool:
    """Empty if every line is blank or a placeholder slot (decorated or bare). Any line
    with real content makes the section non-empty. When `label` is given, that labelled
    line (e.g. `Impact:`) is NOT counted as content either — it is judged by its own fill
    leg, so a section carrying only a filled Impact line still needs real summary/bullet
    content. With no label the behaviour is unchanged, so other presence checks are exact."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _placeholder_only(line):
            continue
        if _ANY_HEADING_LINE.match(stripped):
            continue                          # a bare (sub-)heading is scaffolding, not filled content:
            # a `### Foo` line inside a section must not by itself flip that section from empty to filled,
            # or shipping a subsection heading into a template section would silently defeat this gate.
        if label is not None and _label_remainder(line, label) is not None:
            continue
        return False
    return True


def section_presence_findings(body: str, sections: list, tier: str, message: str, where: str,
                              label: str | None = None) -> list:
    """For each named section: a finding if it is missing, or present but empty
    (only blank/placeholder lines). `where` names the source for the message; `label`,
    when set, excludes that labelled line from the content count (see is_empty_section)."""
    blocks = section_blocks(body)
    findings = []
    for name in sections:
        if name not in blocks:
            findings.append(finding(tier, f"Required section '## {name}' is missing from "
                            f"the {where}. {message}"))
        elif is_empty_section(blocks[name], label):
            findings.append(finding(tier, f"Required section '## {name}' in the {where} is "
                            f"empty or only contains the template placeholder. {message}"))
    return findings


def _subsection_line_status(text: str, label: str) -> str:
    """Within a section body, report the state of its labelled sub-line (e.g. `Impact:`):
    'filled', 'unfilled', or 'missing'. A line is that sub-line when `_label_remainder`
    recognizes it (the shipped italic `*Impact: ...*`, a bare `Impact: ...`, or the old
    sentinel `<Impact: ...>`). It is 'filled' only when real, non-placeholder text follows the
    label; an empty label or a `<...>` prompt slot after it (either template form) is
    'unfilled'. Presence, not prose quality — matching the section-presence leg's posture."""
    status = "missing"
    for raw in text.splitlines():
        remainder = _label_remainder(raw, label)
        if remainder is None:
            continue
        if remainder and not PLACEHOLDER_RE.match(remainder):
            return "filled"
        status = "unfilled"                 # bare `Impact:`, `Impact: <slot>`, or old sentinel
    return status


def subsection_fill_findings(body: str, sections: list, label: str, tier: str,
                             message: str, where: str) -> list:
    """For each named section that is present and non-empty, a finding when it lacks a
    filled `<label>:` line. A missing or wholly-empty section is already reported by the
    section-presence leg, so its sub-line gap is not double-counted here."""
    blocks = section_blocks(body)
    findings = []
    for name in sections:
        if name not in blocks or is_empty_section(blocks[name], label):
            continue
        status = _subsection_line_status(blocks[name], label)
        if status == "filled":
            continue
        why = ("its {L} line is still the unfilled '<...>' template placeholder"
               if status == "unfilled" else "it has no {L} line at all").format(L=label)
        findings.append(finding(tier, f"Required section '## {name}' in the {where} has no "
                        f"filled {label} line ({why}); each section must carry a filled "
                        f"'{label}:' line — the one-line consequence a reviewer reads first. "
                        f"{message}"))
    return findings


def phrase_presence_findings(phrases: list, body: str, tier: str, message: str, where: str) -> list:
    """ONE finding when any required anchor of the consent preamble is absent from `body`. This
    guards a fixed anchor a template scan of `## ` headings cannot see — the pull-request preamble
    blockquote, which sits above the first heading. Each anchor is a whole-body substring match, so
    it must sit on one physical line (a hard wrap inside an anchor reads as absent). The findings
    are consolidated into a single entry that lists every missing anchor, so a whole-preamble drop —
    the common case — reads as one defect, not one wall of text per anchor. This is a set of
    structural anchors for one consent surface, never a growable registry of arbitrary required
    sentences, and never a judgement of the prose around them."""
    missing = [p for p in phrases if p not in body]
    if not missing:
        return []
    absent = "; ".join(f'"{p}"' for p in missing)
    return [finding(tier, f"The {where} is missing the consent preamble — the italic note at the very "
                    f"top that tells a reader a green check shows conformance, not correctness, and that "
                    f"their merge is the binding gate. These anchors of it are absent: {absent}. It drops "
                    f"when a body is reconstructed rather than filled from the template verbatim; if the "
                    f"preamble looks present, check that no line wrap falls inside an anchor (each must sit "
                    f"on one unwrapped line). Restore the preamble. {message}")]


# ---- kind: presence --------------------------------------------------------

def kind_presence(rule, ctx):
    """Named sections are present and non-empty. The target is either the
    pull-request body (target.context == 'pull-request-body') or a prose file
    (target.path). A section is empty if, after dropping blank lines and template
    placeholder lines, no substantive content remains — so an auto-populated
    template body does NOT pass on its own. When params carry a
    `filled_subsection_label`, each non-empty section must ALSO carry a filled line
    under that label (e.g. `Impact:`), and that labelled line is not itself counted as
    section content. When params carry `required_phrases`, each listed phrase must appear
    verbatim in the body — for a fixed anchor a heading scan cannot see, such as the consent
    preamble above the first `## ` heading. Each leg is skipped when its param is absent, so
    other presence checks are exactly unaffected. Presence + non-emptiness (and the labelled
    line / required phrases, when configured) are gated; truthfulness is posture (this cannot
    judge whether the content is accurate)."""
    tier = rule["tier"]
    params = rule.get("params") or {}
    sections = params.get("sections", [])
    label = params.get("filled_subsection_label")
    phrases = params.get("required_phrases")
    message = rule["message"]
    target = rule.get("target") or {}
    if target.get("context") == "pull-request-body":
        body = ctx.get("pr_body")
        if body is None:
            return True, [disclosed_noop("PR body not available; completeness not "
                                         "evaluated here (the CI run evaluates it).")]
        findings = section_presence_findings(body, sections, tier, message, "pull-request body", label)
        if label:
            findings += subsection_fill_findings(body, sections, label, tier, message,
                                                 "pull-request body")
        if phrases:
            findings += phrase_presence_findings(phrases, body, tier, message, "pull-request body")
        return (len(findings) == 0), findings
    findings = []
    for path in target_files(rule):
        where = os.path.relpath(path, ROOT)
        text = read(path)
        findings.extend(section_presence_findings(text, sections, tier, message, where, label))
        if label:
            findings.extend(subsection_fill_findings(text, sections, label, tier, message, where))
        if phrases:
            findings.extend(phrase_presence_findings(phrases, text, tier, message, where))
    return (len(findings) == 0), findings


# ---- kind: schema ----------------------------------------------------------

def _surface_record_for(rel_path: str) -> dict | None:
    """The catalog surface record whose `location` is a directory prefix of
    rel_path (the longest match wins). None if no surface owns the file."""
    try:
        surfaces = load_json(CATALOG_PATH).get("surfaces", {})
    except Exception:
        return None
    best = None
    for rec in surfaces.values():
        location = rec.get("location", "")
        if location and rel_path.startswith(location) and (best is None
                or len(location) > len(best.get("location", ""))):
            best = rec
    return best


def _governing_schema(rule: dict, rel_path: str):
    """Resolve the governing schema for a target file, then return the loaded
    schema object to validate the file against. Resolution is CATALOG-FIRST
    (there is no separate routing table): the surface's
    `governing_schema`. A rule's `params.schema` is an OVERRIDE only — for the two
    cases the catalog cannot express: a well-formedness check, and the catalog's
    own self-governance (surface-catalog.json is governed by its meta-contract, not
    by the meta-schema URL its `schema`-surface record carries). Returns:
      None                       -> file is not schema-governed; nothing to check.
      Draft202012Validator.META_SCHEMA -> the 2020-12 dialect (well-formedness).
      <loaded schema object>     -> validate the file against it.
    Schema-path references resolve on disk, never over the network."""
    from jsonschema import Draft202012Validator        # lazy: see the module __getattr__ note (tool-runtime dep)
    params = rule.get("params") or {}
    if params.get("schema"):                       # explicit override (repo-root-relative or the dialect URI)
        ref, base = params["schema"], ROOT
    else:                                          # catalog routing (relative to the schemas dir, or the URI)
        rec = _surface_record_for(rel_path)
        ref = rec.get("governing_schema") if rec else None
        base = SCHEMAS_DIR
    if not ref:
        return None
    if ref == META_SCHEMA_URI or ref.startswith("http"):
        return Draft202012Validator.META_SCHEMA    # bundled offline; never fetched
    return load_json(os.path.normpath(os.path.join(base, ref)))


def _template_shape_spec(rel_path: str):
    """The shape-spec (required_sections / allowed_sections / length_budget) for a target file,
    read from its surface's TEMPLATE frontmatter — the single source the AI authors from and the
    validator checks (catalog -> template -> shape rules -> instance, so
    authored-from and checked-against cannot drift). The catalog surface record's `template`
    reference (relative to the schemas dir, exactly like `governing_schema`) names the template;
    its frontmatter carries the template.v1 spec. Returns None when the surface has no template
    (a non-prose or template-less surface) — the caller treats that as a misconfigured shape rule."""
    rec = _surface_record_for(rel_path)
    ref = rec.get("template") if rec else None
    if not ref:
        return None
    return frontmatter(os.path.normpath(os.path.join(SCHEMAS_DIR, ref)))


def _load_structured(path: str):
    """Load a structured (non-prose) schema target by extension: a `.toml` target is read
    with the stdlib's read-only TOML parser (its parsed form maps onto the same JSON data
    model every schema check validates); everything else is read by `load_json`, exactly as
    before. TOML targets exist because the Codex runtime's own files are TOML — the engine
    validates them where they live rather than mirroring them into JSON."""
    if path.endswith(".toml"):
        import tomllib
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    return load_json(path)


def kind_schema(rule, ctx):
    """A structured file — or a prose file's YAML frontmatter — conforms to its governing
    JSON Schema (2020-12). Validates the PARSED data, not raw text. The loader is chosen by
    the target's surface CLASS (catalog-resolved, the same routing _governing_schema uses):
    a `prose` surface's frontmatter is read (and normalized to the JSON data model) by
    `frontmatter`; every other target — structured surfaces, and the override-schema targets
    that carry no surface record at all — is read by `_load_structured` (JSON, or TOML for a
    `.toml` target). A rule carrying an explicit `params.schema` OVERRIDE always loads its
    target as structured, whatever surface home the file sits in: the override exists exactly
    for whole-file data contracts the catalog cannot route (a data file living inside a prose
    surface's home — the provider-exception ledger under .engine/policies/ — has no
    frontmatter to read). A
    malformed file, an unresolvable or offline schema reference, or a malformed governing
    schema is a loud finding (the halt-on-malformed posture), never an uncaught error and
    never a network fetch."""
    from jsonschema import Draft202012Validator        # lazy: see the module __getattr__ note (tool-runtime dep)
    from jsonschema.exceptions import SchemaError
    tier = rule["tier"]
    overridden = bool((rule.get("params") or {}).get("schema"))
    findings = []
    for path in target_files(rule):
        rel = os.path.relpath(path, ROOT)
        rec = _surface_record_for(rel)                 # None for override-schema targets (engine.json, state.json, manifests)
        is_prose = bool(rec) and rec.get("class") == "prose" and not overridden
        try:
            data = frontmatter(path) if is_prose else _load_structured(path)
        except Exception as exc:
            malformed = ("has a malformed settings block (its YAML frontmatter could not be read)"
                         if is_prose else
                         ("is not valid TOML" if path.endswith(".toml") else "is not valid JSON"))
            findings.append(finding(tier, f"'{rel}' {malformed} and cannot be "
                            f"schema-checked: {exc}. {rule['message']}", loc(path)))
            continue
        try:
            schema = _governing_schema(rule, rel)
            if schema is None:
                continue                           # not schema-governed; nothing to check
            for err in Draft202012Validator(schema).iter_errors(data):
                where = "/".join(str(p) for p in err.absolute_path) or "(root)"
                findings.append(finding(tier, f"'{rel}' does not match its schema at "
                                f"{where}: {err.message}. {rule['message']}", loc(path)))
        except SchemaError as exc:                 # the governing schema is itself malformed
            findings.append(finding(tier, f"'{rel}' is governed by a schema that is not "
                            f"well-formed: {exc.message}. {rule['message']}", loc(path)))
        except Exception as exc:                   # unresolvable/offline reference, missing schema file, ...
            findings.append(finding(tier, f"'{rel}' could not be schema-checked (its schema "
                            f"is unresolvable offline or missing): {exc}. {rule['message']}", loc(path)))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- kind: shape -----------------------------------------------------------

def kind_shape(rule, ctx):
    """A prose instance matches a template's shape-spec: the required sections are
    present and in the declared order, no section falls outside required+allowed, and
    the body stays within a soft length budget. Section STRUCTURE is the control —
    a missing required or an out-of-allowed section is the rule's tier (hard for a
    governance-critical surface, soft for a lighter one). LENGTH only nudges — over
    the budget is always SOFT, never the rule's hard tier. The
    shape-spec (required_sections, allowed_sections, length_budget) is read from the
    surface's TEMPLATE frontmatter via the catalog (catalog -> template -> shape -> instance),
    so the thing the AI authors from is the thing the validator checks and the two cannot
    drift. The frontmatter and any fenced code block are excluded from both section detection
    and the length count — templates govern the prose body only. An optional
    params.length_budget_overrides {rel: {budget, why}} stays on the (guarded) rule, not the
    template, and carries a recorded, consented higher ceiling for one named operation (raising
    a budget stays a deliberate act needing the operator's sign-off); an entry that is malformed
    (no integer budget, no recorded why) or names a file that no longer exists fails at the
    rule's tier, so a stale or unexplained override cannot rot into a silent grant."""
    tier = rule["tier"]
    params = rule.get("params") or {}
    overrides = params.get("length_budget_overrides") or {}
    findings = []
    for path in target_files(rule):
        rel = os.path.relpath(path, ROOT)
        # The shape-spec's single source is the surface's TEMPLATE (catalog -> template). A target that is NOT a
        # catalogued surface — the negative-fixture meta-check's seeded input — has no template and falls back to
        # an inlined spec carried on the rule itself; a target that is neither catalogued nor carries an inlined
        # spec is a misconfigured rule. Every live shape rule targets a catalogued surface, so it single-sources
        # from the template; the inlined-spec fallback is exercised only by the meta-check fixture.
        spec = _template_shape_spec(rel)
        if spec is None:
            spec = params
        required = spec.get("required_sections")
        if required is None:
            findings.append(finding(tier, f"'{rel}' cannot be shape-checked: its surface names no template in the "
                            f"catalog and the rule carries no inlined shape-spec. {rule['message']}", loc(path)))
            continue
        allowed = set(required) | set(spec.get("allowed_sections", []))
        budget = spec.get("length_budget")
        body = _body_without_frontmatter(read(path))
        present = section_order(body)
        present_set = set(present)
        # required present
        for name in required:
            if name not in present_set:
                findings.append(finding(tier, f"'{rel}' is missing the required section "
                                f"'## {name}'. {rule['message']}", loc(path)))
        # required ordering: the required sections that ARE present must keep their order
        seen = [n for n in present if n in required]
        if seen != [n for n in required if n in present_set]:
            findings.append(finding(tier, f"'{rel}' has its required sections out of order; "
                            f"expected the order {required}. {rule['message']}", loc(path)))
        # no section outside required+allowed
        for name in present:
            if name not in allowed:
                findings.append(finding(tier, f"'{rel}' has section '## {name}', which the "
                                f"template does not allow. {rule['message']}", loc(path)))
        # length budget — a soft nudge only, regardless of the rule's tier. A per-file
        # override (a recorded, consented higher ceiling for one named operation) replaces
        # the rule-wide budget for its file; absent or malformed, the rule-wide budget
        # applies (a malformed override is caught as a hard finding below).
        ov = overrides.get(rel)
        ov_budget = ov.get("budget") if isinstance(ov, dict) else None
        file_budget = ov_budget if _is_pos_int(ov_budget) else budget
        if file_budget is not None:
            lines = len(body.splitlines())
            if lines > file_budget:
                findings.append(finding("soft", f"'{rel}' is {lines} lines, over its "
                                f"{file_budget}-line budget — a nudge to trim, never a block.", loc(path)))
    # Each override must be well-formed and live: an integer `budget` (the line ceiling) and a
    # recorded `why` (StarshipSuperjam/engine-template#273's recorded-rationale, made mechanical so a budget cannot be raised
    # without a stated reason), keyed to a file that still exists. A malformed entry, or a key
    # left dangling by a rename, would otherwise sit as inert, consented config that grants
    # nothing while looking like a live budget. Each failure is the rule's hard tier so a dead
    # grant cannot accumulate. Existence is checked on disk directly (not via the iterated file
    # list) so the guard is independent of which subset of files a given run evaluates.
    for key, ov in overrides.items():
        ov_budget = ov.get("budget") if isinstance(ov, dict) else None
        ov_why = ov.get("why") if isinstance(ov, dict) else None
        if not _is_pos_int(ov_budget) or not (isinstance(ov_why, str) and ov_why.strip()):
            findings.append(finding(tier, f"the length-budget override for '{key}' is incomplete — "
                            f"every override must give an integer 'budget' (the line ceiling) and a "
                            f"'why' (the recorded reason for the raise). Add both before merging.", None))
        if not os.path.isfile(os.path.join(ROOT, key)):
            findings.append(finding(tier, f"the length-budget override names '{key}', which is not a "
                            f"file in this project — a stale or mistyped key (often left by a rename). "
                            f"Update length_budget_overrides in this rule: remove the entry or repoint it.",
                            None))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- kind: coverage (referential integrity: links + catalog-coverage) ------

def kind_coverage(rule, ctx):
    """Referential-integrity coverage; `params.mode` selects the check. `links` folds in
    the former link-integrity (every relative Markdown link resolves). `catalog` is
    catalog-coverage (every catalogued surface has its location directory; no orphan
    surface directory). An unrecognized mode fails closed as a finding — `mode` is OPEN,
    not a fixed enum, so a later mode (e.g. knowledge fingerprint-coverage) adds no edit
    here. Core ships this kind; the catalog-coverage RULE rides validators-core."""
    mode = (rule.get("params") or {}).get("mode")
    if mode == "links":
        return _coverage_links(rule, ctx)
    if mode == "catalog":
        return _coverage_catalog(rule, ctx)
    if mode == "fingerprint":
        return _coverage_fingerprint(rule, ctx)
    tier = rule["tier"]
    return False, [finding(tier, f"Check rule '{rule.get('id')}' (kind 'coverage') names an "
                   f"unrecognized mode '{mode}'; cannot evaluate (fails closed).")]


def _coverage_fingerprint(rule, ctx):
    """Knowledge fingerprint-coverage (the knowledge fingerprint detection relay): re-derive the
    knowledge graph from the current surfaces and compare to the committed entities, so a surface
    that changed/was added/was removed without an entity regen is caught at CI. Detection is
    KNOWLEDGE's — this RELAYS to knowledge_gen.check() (the self-map drift-gate model), holding zero
    derivation logic here. The import is DEFERRED (function-local) so the coverage kind loads no
    module at import time; it resolves wherever this rule runs (.engine/tools/ is on sys.path). Any
    failure to load or derive FAILS CLOSED as a hard finding at the rule's tier — the gate cannot
    silently pass when the graph is missing or the generator is broken. The drift finding (when it
    fires) is knowledge's own plain-language message naming the regenerate fix."""
    tier = rule["tier"]
    try:
        import knowledge_gen  # deferred sibling import; resolves in CLI/import/test contexts
        f = knowledge_gen.check(tier=tier)
    except Exception as exc:
        return False, [finding(tier, f"Check rule '{rule.get('id')}' could not verify the knowledge "
                       f"graph (fingerprint coverage): {exc} (fails closed). {rule.get('message', '')}")]
    findings = [f] if f.get("severity") == "hard" else []
    return (len(findings) == 0), findings


def _coverage_links(rule, ctx):
    """Every relative Markdown link must resolve to an existing file. A link that resolves
    OUTSIDE the repo cannot be checked in a CI checkout, so it is a soft note, never hard. A link into a
    surface owned by a module this deployment DECLINED is likewise a note, never hard — the target is gone
    because the operator opted the module out, not because the link is broken (StarshipSuperjam/engine-template#646)."""
    import module_surfaces as _module_surfaces  # lazy: avoids a validate<->module_surfaces import cycle
    tier = rule["tier"]
    exclude = set((rule.get("params") or {}).get("exclude_dirs", []))
    findings = []
    for path in markdown_files(exclude):
        text = read(path)
        base = os.path.dirname(path)
        for m in LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or target.startswith(("#", "mailto:")) or "://" in target:
                continue
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            resolved = os.path.normpath(os.path.join(base, target))
            if os.path.exists(resolved):
                continue
            inside = os.path.abspath(resolved).startswith(ROOT + os.sep)
            line_no = text[:m.start()].count("\n") + 1
            declined_owner = _module_surfaces.declined_surface_owner(resolved) if inside else None
            if declined_owner:
                findings.append(finding("soft", f"Markdown link to '{target}', a surface of the declined "
                                f"module '{declined_owner}' (absent here because it was opted out, not a broken "
                                f"link). {rule['message']}", loc(path, line_no)))
                continue
            sev = tier if inside else "soft"
            findings.append(finding(sev, f"Broken Markdown link to '{target}'. {rule['message']}",
                            loc(path, line_no)))
    return (not any(f["severity"] == "hard" for f in findings)), findings


def catalog_coverage_findings(surfaces: dict, present_locations: set, tier: str,
                              message: str, infra=()) -> list:
    """Pure catalog-coverage: given the catalogued surfaces {name: record} and the set of
    surface-location strings present on disk, return findings — a catalogued surface whose
    `location` directory is absent; a present surface-location no surface claims (an orphan),
    skipping a known non-surface infra location; and an infra exemption that names a catalogued
    surface's own home (which must never happen — an exemption may not shadow a surface's
    coverage). The spec's third coverage leg — 'no uncatalogued surface is in use' — is, for the
    surface-shaped-instance case, a build-spec leaf resolved as authoring judgment at the
    cataloguing pull request (ontology 'The catalog': a surface's soundness is weighed when it is
    catalogued, not re-attested by a green coverage check), because a general mechanical rule
    cannot tell an uncatalogued surface apart from a legitimate non-surface bucket — module
    `provides` legitimately groups non-surface files (module.v1: 'a foundation group … that are
    not a surface'). What is mechanized here beyond the two directory legs is the disjointness
    invariant below. Kept pure so it is testable without the live filesystem."""
    catalogued = {rec.get("location") for rec in surfaces.values()}
    findings = []
    for name, rec in surfaces.items():
        if rec.get("location") not in present_locations:
            findings.append(finding(tier, f"Catalogued surface '{name}' has no directory at "
                            f"'{rec.get('location')}'. {message}"))
    for location in sorted(present_locations):
        if location not in catalogued and location not in set(infra):
            findings.append(finding(tier, f"Directory '{location}' exists but no catalogued "
                            f"surface claims it (orphan surface directory). {message}"))
    for location in sorted(set(infra) & catalogued):
        findings.append(finding(tier, f"Infrastructure exemption '{location}' names a catalogued "
                        f"surface's home directory; an infra exemption must never shadow a "
                        f"surface's coverage. {message}"))
    return findings


def _coverage_catalog(rule, ctx):
    """catalog-coverage over the live surface catalog + filesystem (see the pure
    catalog_coverage_findings); non-surface infra directories are passed via
    params.infra_dirs. The catalog source and the walk root default to the live globals
    (CATALOG_PATH / ROOT — what CI runs); run_unit (StarshipSuperjam/engine-template#286) may override BOTH
    via ctx (coverage_catalog / coverage_root) to point the REAL callable at a seeded
    mini-tree, so the meta-check witnesses this exact entry point. Production callers pass
    neither key, so the behaviour is byte-unchanged."""
    tier = rule["tier"]
    catalog_path = ctx.get("coverage_catalog", CATALOG_PATH)
    base = ctx.get("coverage_root", ROOT)
    try:
        surfaces = load_json(catalog_path).get("surfaces", {})
    except Exception as exc:
        return False, [finding(tier, f"Could not read the surface catalog to check coverage: "
                       f"{exc}. {rule['message']}", loc(catalog_path))]
    infra = set((rule.get("params") or {}).get("infra_dirs", []))
    present = set()
    for root in (".engine", ".claude", ".codex", ".agents"):
        abs_root = os.path.join(base, root)
        if os.path.isdir(abs_root):
            for name in sorted(os.listdir(abs_root)):
                if os.path.isdir(os.path.join(abs_root, name)):
                    present.add(f"{root}/{name}/")
    findings = catalog_coverage_findings(surfaces, present, tier, rule["message"], infra)
    return (not any(f["severity"] == "hard" for f in findings)), findings


def markdown_files(exclude_dirs: set) -> list:
    out = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d != ".git"
                   and os.path.relpath(os.path.join(dirpath, d), ROOT) not in exclude_dirs]
        out.extend(os.path.join(dirpath, f) for f in files if f.endswith(".md"))
    return out


# ---- kind: coherence (the installed module set is consistent) --------------

def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")) or (0,)


def _ver_key(v):
    """A LENGTH-NORMALIZED version tuple for RANGE-boundary comparison, so a two-part key ('0.4') and its
    three-part form ('0.4.0') compare EQUAL rather than '0.4' sorting BELOW '0.4.0' as a tuple prefix.

    THE SINGLE NORMALIZER for version-key range comparison: called (via a re-export) by module_manager's
    select_migrations and select_retired_capabilities, and reached by release_cut._norm_ver for BOTH release-cut
    accumulation guards. It lives HERE (moved from module_manager) so version_key_duplicate_findings — a
    coherence leg in the lowest layer — can share the one normalizer.

    The MAJOR.MINOR.PATCH key FORMAT is now enforced at authoring, going forward, by module.v1.json's
    `propertyNames` pattern on the migrations / retired_capabilities blocks. The padding here is KEPT regardless:
    the release-cut accumulation guards compare a candidate against PREVIOUSLY-SHIPPED baseline manifests, which
    may legitimately still hold a pre-rule two-part key (the test_rekeyed_migration_is_not_a_false_drop
    scenario), and it is defense in depth at the module-manager / instantiator gate, which does NOT run the
    schema. Padding is a no-op for a conventional three-part (or any 4+-part) tuple, so behaviour is unchanged
    for every real key in the tree."""
    t = _ver_tuple(v)
    return (t + (0,) * (3 - len(t))) if len(t) < 3 else t


# The manifest blocks whose KEYS are module versions (selected by RANGE at upgrade). The schema constrains each
# key's FORMAT (propertyNames); this list drives the coherence leg that forbids two keys meaning one version. A
# future third version-keyed block inherits the collision check by being added here.
VERSION_KEYED_BLOCKS = ("migrations", "retired_capabilities")


class _KeyPairDict(dict):
    """A dict that also records, on `.duplicated`, the keys that appeared MORE THAN ONCE in the source JSON
    object. `json.load` silently collapses a duplicate key (last value wins) before any check can see it, so a
    leg that reads the parsed dict alone cannot catch a literally-duplicated version key — this carries the
    fact forward on the parsed object."""


def _load_manifest_keypairs(path: str) -> dict:
    """Load a manifest as `_KeyPairDict`s, so every object remembers its literally-duplicated keys. Used only by
    version_key_duplicate_findings; a `_KeyPairDict` IS a dict, so nothing else is affected."""
    def _hook(pairs):
        d = _KeyPairDict(pairs)
        seen, dup = set(), []
        for k, _v in pairs:
            if k in seen:
                dup.append(k)
            else:
                seen.add(k)
        d.duplicated = dup
        return d
    with open(path, encoding="utf-8") as fh:
        return json.load(fh, object_pairs_hook=_hook)


def version_key_duplicate_findings(manifest_ids: list, tier: str, message: str) -> list:
    """Coherence leg (StarshipSuperjam/engine-template#694): a manifest's migrations / retired_capabilities block may not declare two keys that
    mean the SAME version. Two keys collide when they normalise equal under `_ver_key`: distinct spellings
    ('0.4' & '0.4.0', or the leading-zero '0.04.0' & '0.4.0'), OR a LITERAL duplicate ('0.4.0' twice, which
    `json.load` collapses — caught by re-reading the raw file). Either way an upgrade would act on that version
    twice, or silently drop one entry (a migration that never runs, a retirement notice that never shows).

    This is NOT shadowed by the schema `propertyNames` format rule. That rule runs at engine-ci (the
    module-manifest schema check) and the release cut, but NOT at the module-manager / instantiator gate
    (install / uninstall / upgrade / arrival) — where this leg, via `check_coherence`, is the sole
    version-key-collision guard for a locally-authored manifest — and a leading-zero or literal-duplicate
    collision passes the format rule regardless. `manifest_ids` is a list of (manifest_path, module_id) pairs;
    the leg reads raw JSON to see collapsed duplicates. Returns one finding per colliding group."""
    findings = []
    for path, mid in manifest_ids:
        try:
            man = _load_manifest_keypairs(path)
        except Exception as exc:  # noqa: BLE001 — a re-read failure (a race after discovery already loud-parsed
            #   the file) is SURFACED, never silently passed: reporting clean when a manifest could not be
            #   re-checked would break the codebase's halt-on-malformed posture (cf. validate.load_json).
            findings.append(finding(
                tier, f"Could not re-read module '{mid}' manifest to verify its version keys "
                f"({type(exc).__name__}); a duplicate-version key cannot be ruled out for it. {message}"))
            continue
        if not isinstance(man, dict):
            continue
        for block in VERSION_KEYED_BLOCKS:
            obj = man.get(block)
            if not isinstance(obj, dict):
                continue
            authored = list(obj.keys()) + list(getattr(obj, "duplicated", ()))
            groups: dict = {}
            for k in authored:
                groups.setdefault(_ver_key(k), []).append(k)
            for _norm, keys in sorted(groups.items()):
                if len(keys) > 1:
                    findings.append(finding(
                        tier, f"Module '{mid}' declares version keys {sorted(keys)} in its '{block}' block that "
                        f"mean the same version — an upgrade would act on that version twice, or silently drop "
                        f"one. Keep exactly one key per version. {message}"))
    return findings


def _version_in_range(version: str, spec: str) -> bool:
    """A pragmatic version-range check on dotted-integer versions: a space/comma list of
    comparators (>=, >, <=, <, ==/=, ^). The exact manifest range grammar is pinned by the
    module-system manifest schema; this is the stable presence/acyclicity/range
    seam the module manager calls — the module system may extend the comparator set."""
    vt = _ver_tuple(version)
    for part in re.split(r"[,\s]+", (spec or "").strip()):
        if not part:
            continue
        m = re.match(r"(\^|>=|<=|==|=|>|<)?\s*(.+)", part)
        op, ref = (m.group(1) or "=="), _ver_tuple(m.group(2))
        if op in ("==", "=") and vt != ref:
            return False
        if op == ">=" and vt < ref:
            return False
        if op == ">" and vt <= ref:
            return False
        if op == "<=" and vt > ref:
            return False
        if op == "<" and vt >= ref:
            return False
        if op == "^" and not (vt >= ref and vt[:1] == ref[:1]):
            return False
    return True


def _dependency_cycle(by_id: dict) -> list:
    """A dependency cycle as a list of module ids, or [] if acyclic (DFS over `depends`)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in by_id}
    stack = []

    def visit(node):
        color[node] = GRAY
        stack.append(node)
        for dep in (by_id.get(node, {}).get("depends") or {}):
            if dep not in by_id:
                continue
            if color.get(dep) == GRAY:
                return stack[stack.index(dep):] + [dep]
            if color.get(dep) == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for n in list(by_id):
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return []


def coherence_findings(manifests: list, tier: str, message: str) -> list:
    """Pure module-set coherence: given installed module manifests
    [{id, version, depends: {id: range}}, ...], return findings for an absent dependency,
    a version outside a declared range, or a dependency cycle. The module manager
    imports this directly (a library call, not a suite trigger)."""
    by_id = {m.get("id"): m for m in manifests}
    findings = []
    for m in manifests:
        for dep_id, dep_range in (m.get("depends") or {}).items():
            if dep_id not in by_id:
                findings.append(finding(tier, f"Module '{m.get('id')}' depends on '{dep_id}', "
                                f"which is not installed. {message}"))
            elif dep_range and not _version_in_range(by_id[dep_id].get("version"), dep_range):
                findings.append(finding(tier, f"Module '{m.get('id')}' needs '{dep_id}' {dep_range}, "
                                f"but version {by_id[dep_id].get('version')} is installed. {message}"))
    cycle = _dependency_cycle(by_id)
    if cycle:
        findings.append(finding(tier, f"Module dependency cycle: {' -> '.join(cycle)}. {message}"))
    return findings


def topological_order(manifests: list) -> list:
    """The present manifests in dependency (topological) order — every module appears AFTER the
    modules it `depends` on. Deterministic and INPUT-ORDER-INDEPENDENT: ready modules are emitted in
    alphabetical id order (Kahn's algorithm), so the order is a function of the dependency edges, not
    of the input sequence. A `depends` id not present in the set is ignored (an absent dependency is a
    separate coherence finding, not this function's concern — mirrors `_dependency_cycle`). CYCLE-SAFE:
    if a dependency cycle leaves modules unresolved (a cycle is flagged hard by `coherence_findings`),
    the unresolved modules are appended in alphabetical id order so a caller never crashes — the result
    stays deterministic. Pure (no IO): the self-map renders modules in this order and the
    module manager installs/migrates in it (the module system's dependency resolution
    — "the build order is its topological sort")."""
    by_id = {m.get("id"): m for m in manifests}
    indeg = {mid: 0 for mid in by_id}            # count of THIS module's deps present in the set
    dependents: dict = {mid: [] for mid in by_id}  # dep id -> modules that depend on it
    for mid, m in by_id.items():
        for dep in (m.get("depends") or {}):
            if dep in by_id:
                indeg[mid] += 1
                dependents[dep].append(mid)
    ready = sorted(mid for mid, d in indeg.items() if d == 0)
    order: list = []
    while ready:
        node = ready.pop(0)                       # alphabetically-smallest ready id
        order.append(node)
        newly = []
        for child in dependents[node]:
            indeg[child] -= 1
            if indeg[child] == 0:
                newly.append(child)
        if newly:
            ready = sorted(ready + newly)          # keep ready alphabetical -> deterministic
    if len(order) < len(by_id):                    # cycle fallback: emit the rest alphabetically
        emitted = set(order)
        order.extend(sorted(mid for mid in by_id if mid not in emitted))
    return [by_id[mid] for mid in order]


def ownership_findings(inventory: list, claims: dict, exempt, tier: str, message: str) -> list:
    """Pure ownership coherence — the third coherence leg, beside dependency-coherence
    (coherence_findings) in the validation foundation. Given the engine file `inventory`
    (relpaths), a `claims` map {relpath: [module-id, ...]} of which present modules'
    `provides` match each file, and the set of `exempt` relpaths a module legitimately need
    not claim (the named foundation infrastructure artifacts + the module manifests
    themselves), return a finding for every ORPHAN (an engine file no module claims and that
    is not exempt) and every DOUBLE-CLAIM (a file two or more modules claim). The leg is
    kept pure — the filesystem walk and the glob matching that build `inventory`/`claims`,
    and the policy of what is exempt, live in the module-coherence consumer — so it is
    testable without the live filesystem and the module manager reuses it."""
    exempt = set(exempt)
    findings = []
    for rel in inventory:
        owners = claims.get(rel) or []
        if len(owners) > 1:
            findings.append(finding(tier, f"Engine file '{rel}' is claimed by more than one "
                            f"module ({', '.join(sorted(owners))}); exactly one module must "
                            f"own each engine file. {message}", loc(os.path.join(ROOT, rel))))
        elif not owners and rel not in exempt:
            findings.append(finding(tier, f"Engine file '{rel}' belongs to no module (an "
                            f"orphan); add it to a module's 'provides', or remove it. "
                            f"{message}", loc(os.path.join(ROOT, rel))))
    return findings


def wiring_findings(declared: list, tier: str, message: str) -> list:
    """Pure forward wiring coherence (declared -> applied) — the wiring leg of module coherence,
    beside dependency (coherence_findings) and ownership (ownership_findings). Given `declared`, a
    list of (module_id, seam_type, target_label, is_applied: bool) — one per `wires` directive of
    each present manifest, with the applied flag computed live by the module-coherence consumer (so
    this leg stays pure and filesystem-free, exactly like ownership_findings) — return a hard `tier`
    finding for every directive NOT applied in its shared target. Uniform across all five seams: a
    declared wire the engine has not applied, or whose applied entry has drifted, is real coherence
    drift.

    The MCP carve-out is APPROVAL-BLINDNESS, not a soft tier: the applied flag for an `mcp` wire
    reflects the committed `.mcp.json` definition (engine wiring), never the operator's runtime
    approval (operator state — a server that is not live for a session, whether unapproved or awaiting
    an app restart, shows up as an ABSENT tool and is surfaced to the operator by boot's AI-observed
    live-helper check and the control plane's PR-Validation surface, not here; availability subsumes the
    approval case) — so a defined-but-unapproved server is simply is_applied=True and never flags
    (module coherence's MCP-registration leg). FORWARD direction only; the orphan-wire
    REVERSE direction (nothing engine-identified applied that no manifest declares) is the companion
    `orphan_wire_findings` below, over the module-coherence consumer's per-seam applied-wire enumerator."""
    findings = []
    for module_id, seam_type, target_label, applied in declared:
        if not applied:
            findings.append(finding(tier, f"Module '{module_id}' declares a {seam_type} wire that is "
                            f"not applied in {target_label}; re-run the install / wiring step to apply "
                            f"it, then re-check. {message}"))
    return findings


def orphan_wire_findings(applied: list, declared_ids, tier: str, message: str) -> list:
    """Pure REVERSE wiring coherence (applied -> declared) — the orphan-wire leg, the inverse of the
    forward wiring_findings (declared -> applied). Together they are the full bidirectional
    "Declared wiring <-> applied wiring" leg the module system mandates
    (module coherence): "everything a present manifest's `wires`
    declares is applied in the shared files, AND nothing engine-identified is applied that no manifest
    declares."

    Given `applied`, a list of (seam_type, identity_key, target_label) for every ENGINE-IDENTIFIED entry
    currently applied in the platform-shared files (built live by the module-coherence consumer, so this
    leg stays pure and filesystem-free exactly like ownership_findings / wiring_findings), and
    `declared_ids`, the set of (seam_type, identity_key) every present manifest's `wires` declares, return
    a hard `tier` finding for any applied engine entry whose identity NO manifest declares — a stale
    leftover after an incomplete uninstall.

    Two carve-outs make the live seam set the three PLATFORM-SHARED-file seams (hook, mcp, gitignore) —
    the only place an orphan has no other governance:
      - PERMISSION is absent from `applied` by construction: a bare permission string is not
        engine-identifiable, so reversal "errs toward leaving it" and coherence cannot reach that honest
        residue (the wiring library). The consumer's enumerator never emits one.
      - ONTOLOGY-ENTRY is out of scope HERE: its target is the engine-OWNED catalog, already covered by
        the ownership leg (every .engine/ file must be claimed) and the SEPARATE locked catalog-coverage
        gate ("ontology catalog coverage is an already-separate locked gate, so [it is] not part of module
        coherence"). The forward leg still checks a declared ontology-entry wire
        is applied; only the reverse (orphan) direction defers to those gates. The asymmetry is sound: the
        reverse leg exists to catch orphans in the platform-shared files that NOTHING ELSE watches; an
        engine-owned-file orphan is already watched twice.

    Drift is reported ONCE, by the forward leg, for the NAME/KEY-identity seams (mcp, gitignore): their
    identity is the server name / fence id, so a content-drifted entry keeps the same identity, still
    matches a declared directive here, and is therefore not an orphan. A HOOK is different by the spec's
    own identity model — a hook's identity is the full {event, matcher, type, command} tuple
    (the wiring library) — so editing an engine hook's command is, by that definition,
    the declared hook gone (forward leg: not applied) AND a new undeclared engine hook present (reverse
    leg: orphan). Those are two accurate findings about two real facts, not a double-count of one."""
    declared = set(declared_ids)
    findings = []
    for seam_type, key, target_label in applied:
        if (seam_type, key) not in declared:
            findings.append(finding(tier, f"An engine-identified {seam_type} setting is present in "
                            f"{target_label} that no installed module declares — a leftover from an "
                            f"incomplete removal. Remove it, or re-install the module it belonged to, "
                            f"then re-check. {message}"))
    return findings


# The two authority tiers the ontology reserves to the self-referential core (the ontology's
# "Authority, enforcement, escalation"; eADR-0016): `contract` is the SOLE `decisions` surface and `policy`
# the SOLE `standing-rules` surface. The reservation is a BIJECTION — a reserved surface holds exactly its
# reserved tier, and a reserved tier sits on no other surface — so it is broken by BOTH a squatter (an added
# surface climbing to a reserved rank) AND a downgrade/swap (a reserved surface knocked off its rank). Homed
# once here (issue StarshipSuperjam/engine-template#401) and consumed by the write-time seam guard (wiring.catalog_add) and the merge-gate
# scan (authority_reservation_findings) alike, so the law lives in exactly one place.
_RESERVED_AUTHORITY = {"contract": "decisions", "policy": "standing-rules"}
_RESERVED_TIER_OWNER = {tier: name for name, tier in _RESERVED_AUTHORITY.items()}


def _reserved_rank_phrase(authority: str) -> tuple:
    """(rank adjective, the plain name of what the engine keeps that rank for) for a reserved tier — used to
    write the plain-language reservation reason without leaking the tier vocabulary."""
    if authority == "decisions":
        return "highest", "core rulebook (the 'contract' surface)"
    return "second-highest", "core standing rules (the 'policy' surface)"


def reserved_authority_reason(name: str, authority) -> "str | None":
    """The single-homed authority-tier reservation law (issue StarshipSuperjam/engine-template#401): a plain-language, DISPOSITION-NEUTRAL
    reason iff the (surface name, authority) pair breaks the reserved bijection {contract<->decisions,
    policy<->standing-rules}; None when the pair is allowed (every additive surface holds a lower tier). The
    reason states only the VIOLATION (never "accepted"/"refused"), so the write-time seam guard can append its
    own "the engine made no change" while the merge-gate finding reads correctly over an already-committed
    catalog. TOTAL: an absent/None (or otherwise non-matching) authority is never a violation — an incomplete
    record is the schema layer's concern, not this rule's."""
    # isinstance guards keep the law TOTAL: a non-string name/authority (a JSON list/object reaching here from
    # a malformed catalog or an under-constrained module wire) is simply "no reserved match", never an
    # unhashable-key TypeError — the schema layer owns rejecting the malformed shape.
    required = _RESERVED_AUTHORITY.get(name) if isinstance(name, str) else None
    if required is not None and authority != required:
        rank, keeper = _reserved_rank_phrase(required)
        return (f"The engine's {keeper} has to hold the engine's {rank} authority rank and no other — the "
                f"engine keeps that ranking fixed so nothing can quietly outrank its own rules — and this "
                f"record puts it at a different rank.")
    owner = _RESERVED_TIER_OWNER.get(authority) if isinstance(authority, str) else None
    if owner is not None and name != owner:
        rank, keeper = _reserved_rank_phrase(authority)
        return (f"The surface '{name}' is set to the engine's {rank} authority rank — the rank the engine "
                f"keeps only for its own {keeper}. Nothing added to the engine is allowed to outrank that.")
    return None


def authority_reservation_findings(catalog: dict, manifests: list, tier: str, message: str) -> list:
    """Pure authority-tier reservation scan (issue StarshipSuperjam/engine-template#401) — the merge-gate half of the reservation law, beside
    the write-time seam guard in wiring.catalog_add. Two legs over the live set:
      LEG A (catalogued surfaces): every surface must satisfy the reserved bijection (reserved_authority_reason)
        — catches a hand-edited surface-catalog.json where an added surface climbs to a reserved rank, OR
        `contract`/`policy` is knocked off its rank.
      LEG B (module manifests): NO non-core module may declare an `ontology-entry` wire that touches the
        reserved space at all — a reserved NAME (`contract`/`policy`) or a reserved TIER (`decisions`/
        `standing-rules`) — catching a module install that would mint or HIJACK a reserved-rank surface at its
        source. This is the OWNER-based half the name-bound seam guard cannot see (it has no module identity),
        so a non-core module re-declaring `contract` under its own record — which the seam passes by name — is
        caught here.
    `catalog` is the parsed surface-catalog ({"surfaces": {name: record}}); `manifests` is a list of manifest
    dicts (the check script feeds module_coherence.discover_manifests() unpacked to dicts). Both legs read every
    field defensively so the scan stays TOTAL on a malformed record/wire. Findings are deduped by surface name
    (one root cause reported once), preferring the Leg-B finding that names the owning module."""
    by_name: dict = {}
    surfaces = (catalog or {}).get("surfaces")
    if isinstance(surfaces, dict):
        for name, record in surfaces.items():
            authority = record.get("authority") if isinstance(record, dict) else None
            reason = reserved_authority_reason(name, authority)
            if reason:
                by_name[name] = finding(tier, f"{reason} {message}", loc(CATALOG_PATH))
    reserved_names = set(_RESERVED_AUTHORITY)
    reserved_tiers = set(_RESERVED_TIER_OWNER)
    for m in manifests:
        if not isinstance(m, dict) or m.get("id") == "core":
            continue
        mid = m.get("id")
        for wire in (m.get("wires") or []):
            if not isinstance(wire, dict) or wire.get("type") != "ontology-entry":
                continue
            wname = wire.get("name")
            record = wire.get("record")
            authority = record.get("authority") if isinstance(record, dict) else None
            # isinstance guards before set membership — a non-string name/authority never raises unhashable-type
            if (isinstance(wname, str) and wname in reserved_names) or \
                    (isinstance(authority, str) and authority in reserved_tiers):
                by_name[wname] = finding(tier, f"The module '{mid}', which is not the engine's own core, "
                                f"declares the surface '{wname}' in the space the engine keeps only for its own "
                                f"core rulebook and core standing rules (its two highest authority ranks). Only "
                                f"the engine's core may define those. {message}")
    return list(by_name.values())


def interface_resolution_findings(interfaces: list, present_impls: dict, present_handles, tier: str,
                                  message: str) -> list:
    """Single-active resolution + structural conformance for the declared interfaces — the locked
    `coherence` posture for interfaces (introduces no new check-kind, surfaces never silently picks).
    The third coherence concern beside dependency-coherence (coherence_findings) and ownership
    (ownership_findings). Pure + fixture-testable.

    `interfaces` is the list of interface declaration dicts; `present_impls` maps an interface id to
    the present NON-DEFAULT implementations [{'handle': ..., 'operations': [op-name, ...]}, ...]
    discovered by presence; `present_handles` is the set of all present/answerable implementation
    handles (default fallback and non-default alike). Findings:
      - HARD: more than one non-default implementation present for one interface — exactly one may
        answer (single-active; a silent arbitrary pick is the trust breach the design forbids).
      - HARD: a present implementation that does not provide every operation the interface declares
        (structural conformance; full behavioural equivalence is a test concern, not a check).
      - NOTE: an interface with no non-default implementation whose named fallback is not present —
        the built-in that answers this capability is not currently wired, so it can't answer until it
        is set up again, surfaced as setup rather than coherence drift.

    A live consumer (interface_coherence_check.py, the engine/check/interface-coherence CI check) runs
    this over the real disk state: it reads the present answerable handles from .mcp.json (so the
    fallback-not-present NOTE fires for real), and the present non-default implementations from the
    deployment's installed set. Implementations are discovered by the presence of a conforming file, so
    today — with no install mechanism that drops a second conforming implementation — nothing non-default
    is present to detect, and the single-active / conformance findings are exercised only by the check's
    negative fixture. They begin firing on real inputs the day an install mechanism can add one.
    Operator-facing wording stays plain capability-language and never names the interface's internal id."""
    present_handles = set(present_handles or [])
    findings = []
    for decl in interfaces:
        iface_id = decl.get("id")
        # The operator reads these lines: name the capability in plain language (the declaration's title),
        # never its internal id, and never the backstage server handles.
        name = decl.get("title") or iface_id
        # Guard the declaration's own shape: a file that is valid JSON but structurally wrong (operations not a
        # list of objects, fallback not an object) must not crash the coherence read — its shape is caught by
        # the declaration's own schema check; here it simply contributes no operation/handle rather than raising.
        declared_ops = {op.get("name") for op in (decl.get("operations") or []) if isinstance(op, dict)}
        fallback = decl.get("fallback")
        fallback_handle = fallback.get("handle") if isinstance(fallback, dict) else None
        impls = [im for im in (present_impls.get(iface_id) or [])
                 if isinstance(im, dict) and im.get("handle") != fallback_handle]
        if len(impls) > 1:
            # "only one can be active" is the stable, plain phrase the negative fixture matches on. The removal
            # is the engine's to do (it edits the wiring, not the operator) — only the choice of which to keep
            # is the operator's, so the offer keeps that decision with them and hands off the mechanical part.
            findings.append(finding(tier, f"Two or more tools are installed to answer '{name}', but only "
                            f"one can be active at a time — a silent pick between them is exactly what the "
                            f"engine must never do. Tell me which one to keep and I'll remove the others. {message}"))
        for im in impls:
            missing = sorted(declared_ops - set(im.get("operations") or []))
            if missing:
                findings.append(finding(tier, f"A tool installed to answer '{name}' is missing something it "
                                f"must be able to do, so it can't reliably stand in. Fix it or remove it. "
                                f"{message}"))
        if not impls and fallback_handle not in present_handles:
            findings.append(finding("note", f"The engine's built-in way to answer '{name}' is not currently "
                            f"wired, so the engine can't answer that until it is set up again. {message}"))
    return findings


def agent_coherence_findings(agents: list, tier: str, message: str) -> list:
    """Pure persona-set coherence — the agent surface's coherence leg, beside dependency
    (coherence_findings), ownership (ownership_findings), forward wiring (wiring_findings), and
    interface resolution (interface_resolution_findings). Given the present personas' parsed
    frontmatter [{name, role, lens?, model-tier, ...}, ...], return a finding for:

      - a `role` outside the closed set {plan-review, worker, pre-submission-review, audit} (an
        'unknown role' is impossible by construction once caught),
      - a `model-tier` outside the closed demand set {judgment, mechanical},
      - a `lens` declared by a `worker` or `audit` role — the symmetric guard to the closed-role
        check: those two roles carry no lens ("a worker or audit instance that
        declares one is a coherence finding"). Scoped to the two KNOWN lensless roles, not "any
        non-review role", so an unknown role carrying a lens yields only the role finding (no
        redundant second finding), and a review role's lens is valid.
      - a `permissions: read-only` persona that does not actually BLOCK the authoritative-write
        tools (Edit, Write, NotebookEdit) — the realization of the design's "permissions maps to the
        Claude Code tool/permission restrictions the platform enforces" (agent.v1 `permissions` /
        `tools` / `disallowedTools`). A read-only persona blocks a write tool iff it lists it
        in `disallowedTools` OR declares a `tools` allowlist (a list) that omits it; a read-only
        persona that declares NEITHER inherits every tool (the inherit-all trap) and is a finding.
        HONEST LIMIT: this enforces only that the native file-writing tools (Edit/Write/NotebookEdit)
        are blocked — it deliberately does NOT police `Bash` (which the execution roles
        pre-submission-review/audit legitimately keep to run the suite in a scratch worktree —
        qa-review dry-run) nor any write-capable MCP tools the session may expose; confining
        those tool-/shell-side writes is the orchestration worktree's + the protected-branch merge
        gate's job, not a frontmatter invariant this static leg can see. A STRING-valued
        disallowedTools/tools is treated CONSERVATIVELY (a string denylist blocks nothing here; a
        string `tools` is not a write-excluding allowlist), so blocking must come from the list
        form — this errs toward a false finding, never a false pass.

    It does NOT do the dangling/unconsumed-lens check (an installed review lens nothing in the
    orchestration consumes): that is a SEPARATE pure leg, `dangling_lens_findings` (below), driven
    by the lens-consumption consumer (`lens_consumption_check.py`) that discovers the personas and
    reads build-orchestration's consumed-review-lenses set. This leg owns only persona-internal
    coherence; the closed sets live HERE, NOT as agent.v1 enums: agent.v1 governs role/model-tier/lens
    as well-formed strings and this leg owns membership, so each set is defined in one place (the
    locked grammar routes membership through the coherence kind, not the schema). `lens` stays an
    OPEN vocabulary — only role/model-tier are closed sets.

    Pure + fixture-testable, mirroring the other legs: the persona frontmatter is parsed by the
    consumer (`agent_coherence_check.engine_agents`) and passed in, so this stays filesystem-free.
    The review/audit persona instances now ship, so this leg's role/model-tier/lens/permissions
    rules fire live every CI through the agent-coherence check."""
    roles = {"plan-review", "worker", "pre-submission-review", "audit"}
    lensless_roles = {"worker", "audit"}   # the recognized roles that carry no lens
    tiers = {"judgment", "mechanical"}
    write_tools = ("Edit", "Write", "NotebookEdit")   # the authoritative-write tools a read-only persona must block
    findings = []
    for a in agents:
        name = a.get("name", "(unnamed)")
        role = a.get("role")
        if role not in roles:
            findings.append(finding(tier, f"Persona '{name}' declares role '{role}', which is not a "
                            f"recognized role ({sorted(roles)}). {message}"))
        mtier = a.get("model-tier")
        if mtier not in tiers:
            findings.append(finding(tier, f"Persona '{name}' declares model-tier '{mtier}', which is "
                            f"not a recognized demand level ({sorted(tiers)}). {message}"))
        if a.get("lens") and role in lensless_roles:
            findings.append(finding(tier, f"Persona '{name}' has role '{role}', which carries no lens, "
                            f"but declares lens '{a.get('lens')}'; only the review roles carry a "
                            f"lens. {message}"))
        if a.get("permissions") == "read-only":
            allow, deny = a.get("tools"), a.get("disallowedTools")
            allow_list = allow if isinstance(allow, list) else None        # a STRING tools (e.g. "inherit") is not an excluding allowlist
            deny_set = {str(t) for t in deny} if isinstance(deny, list) else set()
            if allow is None and deny is None:
                findings.append(finding(tier, f"Persona '{name}' declares permissions: read-only but "
                                f"declares neither a tools allowlist nor a disallowedTools denylist, so it "
                                f"inherits every tool — including the authoritative-write tools "
                                f"{list(write_tools)}. Block them via disallowedTools (or a write-excluding "
                                f"tools allowlist). {message}"))
            else:
                unblocked = [t for t in write_tools
                             if t not in deny_set and not (allow_list is not None and t not in allow_list)]
                if unblocked:
                    findings.append(finding(tier, f"Persona '{name}' declares permissions: read-only but does "
                                    f"not block the authoritative-write tool(s) {unblocked}: a read-only persona "
                                    f"must block Edit/Write/NotebookEdit via disallowedTools or omit them from a "
                                    f"tools allowlist. {message}"))
    return findings


def dangling_lens_findings(agents: list, consumed: set, tier: str, message: str) -> list:
    """Pure lens-consumption coherence — the realization of the agents surface's dangling-lens
    posture (the dangling-check-kind). Given the present personas'
    parsed frontmatter and the CONSUMED lens set a build stage records (build-orchestration's
    consumed-review-lenses block, read by the lens-consumption consumer), return a finding for each
    INSTALLED review lens that no stage consumes: an installed-yet-unconsumed review lens is a
    coherence finding, disclosed — never a check-only signal the operator may never run.

    Scoped to the two review roles (plan-review, pre-submission-review): only those carry a lens a
    gate consumes (worker/audit carry none — the symmetric agent_coherence_findings guard). The diff
    is strictly installed − consumed. A CONSUMED lens with ZERO installed agents is NOT a finding
    here — that is a gate that ran no review, disclosed as such by build-orchestration, not a
    coherence error (0..N agents per lens is valid), so the reverse direction is a
    disclosed no-op, not an error.

    Pure + fixture-testable, mirroring agent_coherence_findings: the consumer discovers the personas
    (`agent_coherence_check.engine_agents`) and parses the consumed set, and passes both in, so this
    stays filesystem-free. Fail-closed on an unreadable/unparseable consumed set is the CONSUMER's
    job (a raise → the custom/script kind's hard fail-closed finding), so a discovery/parse miss can
    never reach here as a silent empty set that reads as 'nothing dangling'."""
    review_roles = {"plan-review", "pre-submission-review"}
    findings = []
    for a in agents:
        lens = a.get("lens")
        if a.get("role") in review_roles and lens and lens not in consumed:
            name = a.get("name", "(unnamed)")
            findings.append(finding(tier, f"The review '{name}' declares lens '{lens}', but no build "
                            f"stage consumes it — so it is installed yet never runs against your "
                            f"changes. {message}"))
    return findings


def skill_coherence_findings(skills: list, tier: str, message: str) -> list:
    """Pure skill-set coherence — the skill surface's coherence leg, beside dependency
    (coherence_findings), ownership (ownership_findings), forward wiring (wiring_findings),
    interface resolution (interface_resolution_findings), and persona coherence
    (agent_coherence_findings). Given the present skills' parsed SKILL.md frontmatter
    [{description, invocation?, disable-model-invocation?, user-invocable?, ...}, ...], return a
    finding for:

      - an `invocation` outside the closed set {model-auto, operator-typed, model-only} (an
        OMITTED invocation is model-auto, the platform default — NOT a finding),
      - an invocation that DISAGREES with the real platform flags the instance carries — the
        self-election leak-guard, the load-bearing CROSS-FIELD rule: the engine reads
        `invocation`, Claude Code reads the flags (disable-model-invocation: true makes a skill
        operator-typed; user-invocable: false makes it model-only), and the two must agree.
        `operator-typed` without `disable-model-invocation: true` is the safety case — the model
        could still self-invoke. A restricting flag carried under a model-auto (or omitted)
        declaration is the symmetric case — the skill behaves restricted but does not declare it.

    An invocation OUTSIDE the set yields only the one membership finding (the flag-mapping is
    skipped for that instance — the agent-leg precedent: one unknown value, one finding). The
    closed set lives HERE (the leg), NOT as a skill.v1 enum: skill.v1 governs `invocation` as a
    well-formed string and this leg owns both membership AND the cross-field flag-mapping a schema
    enum cannot express, so the set is defined in one place (the invocation
    axis, the platform-flag table).

    Pure + fixture-testable, mirroring the other legs: the SKILL.md frontmatter is parsed by the
    consumer and passed in, so this stays filesystem-free. Its live consumer — skill_coherence_check.py,
    driven by the operator verbs that discover the present skill set and decide the
    engine-vs-operator scope — runs this every CI and proves the Build-entry verb's self-election
    safety on the live platform, the interface_resolution_findings / agent_coherence_findings
    precedent (built + fixture-tested, then wired to a live consumer). The engine ships operator-typed
    verbs, so the leg has live subjects to fire on."""
    invocations = {"model-auto", "operator-typed", "model-only"}
    findings = []
    for s in skills:
        name = s.get("name") or "(unnamed)"
        inv = s.get("invocation")
        if inv is not None and inv not in invocations:
            findings.append(finding(tier, f"Skill '{name}' declares invocation '{inv}', which is not "
                            f"a recognized invocation value ({sorted(invocations)}). {message}"))
            continue   # an unknown value yields only the one finding (skip the flag-mapping)
        effective = inv or "model-auto"   # an omitted invocation is the platform default
        dmi = s.get("disable-model-invocation") is True
        uif = s.get("user-invocable") is False
        # Expected flags per effective invocation: model-auto -> neither; operator-typed -> dmi
        # only; model-only -> uif only. Any mismatch is a self-election / leak-guard finding.
        if effective == "operator-typed":
            if not dmi:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'operator-typed' "
                                f"but does not carry 'disable-model-invocation: true', so the model "
                                f"could still self-invoke it. {message}"))
            if uif:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'operator-typed' "
                                f"but also carries 'user-invocable: false' (model-only); the two "
                                f"conflict. {message}"))
        elif effective == "model-only":
            if not uif:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'model-only' but "
                                f"does not carry 'user-invocable: false', so it is not hidden from the "
                                f"operator's menu. {message}"))
            if dmi:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'model-only' but "
                                f"also carries 'disable-model-invocation: true' (operator-typed); the "
                                f"two conflict. {message}"))
        elif dmi or uif:   # effective is model-auto (declared or omitted) but a flag restricts it
            carried = "disable-model-invocation: true" if dmi else "user-invocable: false"
            declared = ("declares invocation 'model-auto'" if inv
                        else "carries no invocation (defaulting to model-auto)")
            findings.append(finding(tier, f"Skill '{name}' {declared} but carries '{carried}', which "
                            f"restricts who may invoke it; declare the matching invocation "
                            f"(operator-typed or model-only) so the engine and the platform agree. "
                            f"{message}"))
    return findings


def block_budget_findings(blocks: list, tier: str, message: str, *, stances) -> list:
    """Pure block-registry coherence — the hooks substrate's coherence leg, beside dependency
    (coherence_findings), ownership (ownership_findings), forward wiring (wiring_findings), interface
    resolution (interface_resolution_findings), persona coherence (agent_coherence_findings), and
    skill coherence (skill_coherence_findings). It asserts TWO cross-field rules over the block-eligible
    registry (the multi-rule agent_coherence_findings shape), so one leg — and the one first-class check
    that wraps it — validates the whole invariant, never half of it:

      1. BLOCK BUDGET (the block-budget law): only PreToolUse
         and Stop may HARD-BLOCK; every other event nudges or injects. The platform would let PreCompact /
         UserPromptSubmit / SubagentStop block too — the Engine declines (a local hard-block buys friction
         without proportional trust).
      2. MODE DIMENSION (mode-awareness, eADR-0022): every block behavior DECLARES the
         modes it is active in — "the dimension is the law; the bindings are membership." This makes the
         mode-activeness DECLARED DATA rather than code-only: a block must carry a non-empty `modes` list
         drawn from the valid stance vocabulary (`stances`, passed in so the canonical set lives once in
         `modes` — this leg never hardcodes it). Honest: it verifies the dimension is
         declared and well-formed, NOT the un-mechanizable "satisfiable without a human present" (that
         stays a reviewed property the declaration now makes visible at the merge).

    Given the present block-eligible registrations [{event, name?, owner?, modes}, ...] — assembled by
    the consumer from the owning systems' declarations and passed in (filesystem-free, the agent/skill
    precedent) — return a finding per violated rule. It covers every DECLARED block; a PreToolUse/Stop
    deny that fires in code but is never registered here escapes both this leg and the check that wraps
    it (the registry is consumer-assembled by hand — owes → 25's registry-discovery pattern), so the leg
    is honest about validating the declared set, not "every block that can fire".

    The block-eligible invariant set STARTS EMPTY: this leg names no invariant itself (hooks owns the
    BUDGET — which events may block — not the invariants). Owning systems register their block
    additively — close's findings-disposition Stop block, modes' explore write-gate + engine-Issue
    reroute PreToolUse blocks. No live rule wires it in core: the registration source is the owning
    systems' declarations, run live by module_coherence.check_coherence and by the first-class
    block-coherence check — the interface_resolution_findings / agent_coherence_findings precedent (a
    pure leg wrapped by a custom/script check, no data rule). The closed eligible set lives HERE (the
    leg) and in the runtime harness (hooks.py); the locked hooks contract is the single source both cite."""
    eligible = {"PreToolUse", "Stop"}
    valid_stances = set(stances)
    findings = []
    for b in blocks:
        name = b.get("name") or b.get("owner") or "(unnamed)"
        event = b.get("event")
        if event not in eligible:
            findings.append(finding(tier, f"The hook block '{name}' is declared on the '{event}' "
                            f"event, but only {sorted(eligible)} may hard-block; every other event "
                            f"nudges or injects. {message}"))
        declared = b.get("modes")
        if not isinstance(declared, list) or not declared:
            findings.append(finding(tier, f"The hook block '{name}' does not declare the modes it is "
                            f"active in — every block behavior must name a non-empty set of stances "
                            f"(the mode dimension is declared data, not code-only). {message}"))
        else:
            unknown = [m for m in declared if m not in valid_stances]
            if unknown:
                findings.append(finding(tier, f"The hook block '{name}' declares unknown mode(s) "
                                f"{unknown}; the valid stances are {sorted(valid_stances)}. {message}"))
    return findings


def effective_policy_values(default: dict, override: dict, *, structural_keys, tier: str,
                            message: str) -> tuple[dict, list]:
    """Merge a per-deployment operator policy-override over a policy's shipped default tuning values,
    per-key at read time — the core merge mechanism for the operator policy-override. Returns
    (effective, findings): the effective value map a consumer reads, plus a finding per refused key.

    Given the shipped `default` value map (read from the policy frontmatter by the consumer) and a sparse
    `override` map (committed operator config — its file path/format belong to the config-authoring step, NOT
    this function), merge each override key over the default, EXCEPT:

      - a key in `structural_keys` — a value that encodes a structural LAW an override may never retune
        (for attention, the partition precedence + trim order, so "blocking-debt-first holds by
        construction") — is REFUSED and surfaced; the shipped default value stands.
      - a key absent from `default` — a STALE key (a knob the policy no longer carries after an upgrade) —
        falls back to the default (it is simply not present in the result) and is surfaced (the
        freshly-stale catch at the merge; a lingering one is the audit's job).

    An unset eligible key (a partial override) silently keeps the default — the normal case, no finding.
    Eligibility is the CALLER'S parameter (`structural_keys`), so one mechanism serves attention (structural
    = precedence + trim) and telemetry's triage-threshold (its own set, empty) without re-derivation — and a
    consumer never imports another substrate's tool to merge, so there is no layering inversion.

    PURE + fixture-testable: the merged value is static data, so a deterministic consumer (attention's
    ranking function) stays deterministic — the merge adds another recorded input; no clock, no IO. The
    override is taken as DATA: this function does not read a file — the consumer loads it and passes it in.
    Findings are ordered by key (sorted) for a reproducible result. No data rule wires this in core: the leg
    is pure and fixture-tested, and it is consumed live by attention's `load_policy_values` (every ranking,
    via the core merge — never re-implemented). A separate live `custom/script` stale-key rule classifies
    saved overrides against the same inputs to surface them in plain operator language, but does not call this
    function."""
    structural = set(structural_keys)
    effective = dict(default)
    findings = []
    for key in sorted(override):
        if key in structural:
            findings.append(finding(tier, f"Override key '{key}' sets a structural-law value that is not "
                            f"override-eligible; it is refused and the shipped default stands. {message}"))
        elif key not in default:
            findings.append(finding(tier, f"Override key '{key}' is not carried by the policy's shipped "
                            f"default; it is ignored and falls back to the default. {message}"))
        elif not _is_tunable_number(override[key]):
            # A value the engine cannot measure against is refused HERE, at the read, because this is the
            # only place every reader passes through. The tuning command checks what it writes, but the
            # override is a committed file an operator can hand-edit, and JSON round-trips the non-standard
            # `Infinity`/`NaN` literals — so the write-side check alone leaves the door open. What comes
            # through it is not cosmetic: an endless bar silently drops even a safety check that could not
            # run (every severity is below it), "not a number" compares false against everything so it
            # blocks what should pass, and a string raises deep in the ranking and costs the whole session's
            # priority list rather than one value. The shipped default stands instead, and the refusal is
            # surfaced rather than swallowed.
            findings.append(finding(tier, f"Override key '{key}' is set to '{override[key]}', which is not "
                            f"an ordinary number the engine can measure against; it is refused and the "
                            f"shipped default stands. {message}"))
        else:
            effective[key] = override[key]
    return effective, findings


def _is_tunable_number(value) -> bool:
    """A real, finite number — the only thing a policy dial can hold. Rejects a bool (it is an int in
    Python, and `True` is not a threshold anyone means), infinity and not-a-number (they survive `float()`
    and a JSON round-trip), and anything non-numeric."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def kind_coherence(rule, ctx):
    """The installed module set is consistent. A directly-callable library entry the
    module manager invokes right after an install; the manifests it reads come from installed
    modules, so in core it ships built + fixture-tested with no live rule. As a
    kind callable it reads the manifest set from ctx['manifests'] (empty in core when no module is installed)."""
    tier = rule["tier"]
    findings = coherence_findings(ctx.get("manifests") or [], tier, rule.get("message", ""))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- kind: custom/script (the escape hatch; runs a committed script) -------

def kind_custom_script(rule, ctx):
    """Run a committed, in-repo script (params.script, resolved under ROOT) with the engine
    interpreter (sys.executable, so it inherits the engine venv). The rule's tier is passed
    via ENGINE_RULE_TIER; the repo token (GITHUB_TOKEN) is passed ONLY if the rule opts in
    with params.pass_token, so the secret reaches only the scripts that need it. CONTRACT:
      exit 0 + stdout a parseable finding.v1 JSON array -> those findings pass through (the
        script sets each severity: the rule tier for a real finding, `soft` for a
        could-not-evaluate note — the fail-open-locally pattern). An empty array = pass.
      non-zero exit OR unparseable stdout -> ONE hard fail-closed finding regardless of the
        rule's tier, so a crashing or uninstalled guard can never silently pass.
    stdout is the machine channel (JSON only); human prose lives in each finding's message."""
    tier = rule["tier"]
    script = (rule.get("params") or {}).get("script")
    if not script:
        return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind 'custom/script') "
                       f"names no params.script; cannot evaluate (fails closed).")]
    path = os.path.normpath(os.path.join(ROOT, script))
    if not (os.path.abspath(path) == ROOT or os.path.abspath(path).startswith(ROOT + os.sep)):
        return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind 'custom/script') names "
                       f"a script outside the repository ('{script}'); refusing to run it (fails "
                       f"closed). A custom/script must be a committed, reviewed, in-repo file.")]
    if not os.path.isfile(path):
        return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind 'custom/script') script "
                       f"'{script}' does not exist; cannot evaluate (fails closed).")]
    # Scope the child environment: a custom/script does NOT inherit the CI repository token
    # unless its rule opts in (params.pass_token), so the secret reaches only the scripts that
    # need it (the protection guard) rather than every script the suite runs.
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    env["ENGINE_RULE_TIER"] = tier
    if (rule.get("params") or {}).get("pass_token") and os.environ.get("GITHUB_TOKEN"):
        env["GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True,
                              env=env, timeout=120)
    except Exception as exc:
        return False, [finding("hard", f"Check '{rule.get('id')}' could not run '{script}': "
                       f"{exc} (fails closed).")]
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr or f"exit {proc.returncode}").strip()
        return False, [finding("hard", f"Check '{rule.get('id')}' could not verify — its script "
                       f"exited with an error: {detail[:300]} (fails closed).")]
    try:
        raw = json.loads(proc.stdout or "[]")
        if not isinstance(raw, list):
            raise ValueError("expected a JSON array of findings")
    except Exception:
        return False, [finding("hard", f"Check '{rule.get('id')}' produced unreadable output; "
                       f"cannot verify (fails closed).")]
    findings = []
    for f in raw:
        if not isinstance(f, dict):
            return False, [finding("hard", f"Check '{rule.get('id')}' produced a malformed finding "
                           f"(not an object); cannot verify (fails closed).")]
        # Reconstruct on the finding.v1 base with an EXPLICIT allow-list — severity, message,
        # location, plus the optional `not_applicable` disclosed-no-op marker — so a script's
        # disclosed_noop() survives re-ingestion (report() can collapse it) while no other
        # author-controllable key leaks through this trust boundary.
        rebuilt = finding(f.get("severity", tier), f.get("message", ""), f.get("location"))
        if f.get("not_applicable"):
            rebuilt["not_applicable"] = True
        findings.append(rebuilt)
    return (not any(f["severity"] == "hard" for f in findings)), findings


# The closed core kind registry: the five closed kinds + the `custom/script` escape hatch.
# Module-provided kinds are NOT added here — they bind by PRESENCE at dispatch (resolved_registry
# below). This dict stays the closed core, and a discovered kind can never override it.
REGISTRY = {
    "presence": kind_presence,
    "schema": kind_schema,
    "shape": kind_shape,
    "coverage": kind_coverage,
    "coherence": kind_coherence,
    "custom/script": kind_custom_script,
}

# ---- module-provided check-kind discovery by presence --------
# A module adds a validation kind by dropping a conforming callable, discovered because it is
# present — NEVER by editing REGISTRY above or threading a wiring seam (the closed seam vocabulary
# has no check-kind directive). CORE ALWAYS WINS on a name collision: resolved_registry() snapshots
# the core registry BEFORE running any discovery import, then merges discovered kinds UNDER that
# snapshot — so a discovered kind, imported in-process, cannot rewrite the core callables for the run
# that discovered it. A discovered file that collides with a core kind, collides with another
# module's kind, or cannot be imported is EXCLUDED from dispatch (its rules then hit the fail-closed
# dangling path) and surfaced as a hard coherence finding (kind_discovery_findings) — never a silent
# bind. Both the dispatcher and the negative-fixture meta-check resolve through resolved_registry()
# so their kind rosters cannot desync.
_CLOSED_CORE_KINDS = frozenset(REGISTRY) - {"custom/script"}  # the five closed kinds with bespoke fixture drivers
_KIND_FILE_RE = re.compile(r"^kind_(.+)\.py$")  # a discovered kind file: kind_<name>.py -> kind name <name>


def _load_kind_callable(path: str, name: str):
    """Import a discovered kind file BY PATH (no package requirement) and return its module-level
    `check(rule, ctx) -> (pass/fail, [finding, ...])` callable, or `(None, reason)` on failure. The
    callable runs in-process (the locked callable model); the core callables are snapshotted before
    discovery, so a discovered kind cannot neuter them. A discovered kind receives `(rule, ctx)` and
    MUST NOT rely on `import validate` identity — under the CLI run (`validate.py` as `__main__`) that
    would bind a second module instance. Never raises: a bad file becomes a reason string."""
    import importlib.util  # stdlib; bound locally to keep the module-top stdlib-only bootstrap intact
    try:
        spec = importlib.util.spec_from_file_location(f"engine_kind_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — any import failure is a fault, never a crash
        return None, f"could not be imported ({exc})"
    fn = getattr(module, "check", None)
    if not callable(fn):
        return None, "exposes no `check(rule, ctx)` callable"
    return fn, None


def _discover_module_kinds(kind_dir: str, core_names):
    """Discover module-provided check-kind callables by PRESENCE under `kind_dir`: every
    `<kind_dir>/<module>/kind_<name>.py` binds kind `<name>`. ONE level deep — a module owns
    `.engine/tools/<module>/*.py`, the only depth at which it owns and can uninstall its kind file
    (a top-level `.engine/tools/kind_*.py` would be core's non-recursive glob, so it is not a module
    kind and is not discovered here). `core_names` is the set of names a discovered kind may not
    shadow (the caller's core-registry snapshot). Returns `(valid, faults)`: `valid` maps each
    cleanly-resolved kind name to its callable; `faults` is a list of `{kind, path, reason}` for a
    file that shadows a core kind, collides with another discovered file of the same name, or cannot
    be imported — each EXCLUDED from `valid` so its rules fail closed, and surfaced loudly by
    kind_discovery_findings. Never raises."""
    valid: dict = {}
    faults: list = []
    claimed: dict = {}  # kind name -> the first file that claimed it (to catch duplicates loudly)
    if not os.path.isdir(kind_dir):
        return valid, faults
    # Snapshot the core registry so a discovered kind's IN-PROCESS import cannot persist a mutation of it — the
    # restore below undoes any such tampering (the S4 hardening), applied on EVERY discovery path (so both
    # resolved_registry and kind_discovery_findings are protected, not just the former).
    saved = dict(REGISTRY)
    try:
        for sub in sorted(os.listdir(kind_dir)):
            subdir = os.path.join(kind_dir, sub)
            if not os.path.isdir(subdir):
                continue
            for entry in sorted(os.listdir(subdir)):
                match = _KIND_FILE_RE.match(entry)
                if not match:
                    continue
                name = match.group(1)
                rel = os.path.relpath(os.path.join(subdir, entry), ROOT)
                if name in core_names:  # never shadow a core kind (the core set is closed)
                    faults.append({"kind": name, "path": rel,
                                   "reason": f"names the closed core kind '{name}', which a module may not redefine"})
                    continue
                if name in claimed:  # two provided files claim the same kind name -> both unresolvable
                    faults.append({"kind": name, "path": rel,
                                   "reason": f"provides kind '{name}', already provided by '{claimed[name]}'"})
                    valid.pop(name, None)
                    continue
                claimed[name] = rel
                fn, reason = _load_kind_callable(os.path.join(subdir, entry), name)
                if fn is None:
                    faults.append({"kind": name, "path": rel, "reason": reason})
                    continue
                valid[name] = fn
    except OSError:  # an unreadable subtree (permission / TOCTOU) -> return what resolved so far; never raises
        pass
    finally:
        if REGISTRY != saved:  # a discovered kind's import mutated the core registry -> undo it (never persists)
            REGISTRY.clear()
            REGISTRY.update(saved)
    return valid, faults


def resolved_registry(kind_dir: str | None = None) -> dict:
    """The dispatch registry: the closed core kinds plus module-provided kinds DISCOVERED BY PRESENCE,
    with CORE ALWAYS WINNING. `kind_dir` defaults to the real `.engine/tools/` via the
    `ENGINE_KIND_DIR` seam, so discovery is LIVE in production — it finds zero kinds today (no v1
    module ships one) but a real module's kind IS found, never dormant. Both the dispatcher
    (_evaluate/run_check/run_unit) and the negative-fixture meta-check call this with no argument, so
    they read the SAME seam and their kind rosters cannot desync. A colliding/unimportable file is
    excluded here (surfaced by kind_discovery_findings) so its rules hit the fail-closed dangling
    path, never a silent bind."""
    core = dict(REGISTRY)  # snapshot BEFORE discovery; _discover_module_kinds restores REGISTRY if an import tampers with it
    kind_dir = kind_dir or env_override_path("ENGINE_KIND_DIR", TOOLS_DIR)
    valid, _faults = _discover_module_kinds(kind_dir, set(core))
    return {**valid, **core}  # core wins by construction — a discovered kind cannot shadow it


def kind_discovery_findings(tier: str = "hard", message: str = "", kind_dir: str | None = None) -> list:
    """The module-set-consistency leg for the kind-discovery seam: a module-provided
    check-kind file that names a closed core kind, collides with another module's kind, or cannot be
    imported is a HARD finding naming the file — never a silent drop. The dispatcher already
    EXCLUDES such a file (resolved_registry); this is the loud, CI-reaching account of why, run by
    module_coherence.check_coherence() over the real tree (green while the real tree carries no module kind)."""
    kind_dir = kind_dir or env_override_path("ENGINE_KIND_DIR", TOOLS_DIR)
    _valid, faults = _discover_module_kinds(kind_dir, set(REGISTRY))
    out = []
    for fault in faults:
        detail = (f"The check-kind file '{fault['path']}' {fault['reason']}; it is not used, so any "
                  f"check of kind '{fault['kind']}' cannot run. Rename the file so it provides a "
                  f"distinct kind, or remove it.")
        out.append(finding(tier, (detail + (" " + message if message else "")),
                           loc(os.path.join(ROOT, fault["path"]))))
    return out


def _run_kind(registry: dict, rule: dict, ctx: dict):
    """Dispatch `rule`'s kind via `registry` and return `(verdict, findings)`, FAILING CLOSED on every
    fault — a dangling/unregistered kind, an erroring callable, OR a malformed return (a kind that does
    not honour the `(pass/fail, [finding, ...])` contract). The one dispatch helper the suite
    (_evaluate), by-id (run_check), and meta-check (run_unit) paths share, so a discovered module kind
    — a new module-authored trust surface — can neither crash the validator nor slip a malformed result
    past the annotation/report loops that iterate findings OUTSIDE any try. Never raises."""
    kind = rule.get("kind")
    tier = rule.get("tier", "hard")
    fn = registry.get(kind)
    if fn is None:  # dangling kind: fail closed (a finding at the rule's tier)
        return False, [finding(tier, f"Check rule '{rule.get('id')}' names "
                       f"unregistered kind '{kind}'; cannot evaluate (fails closed).")]
    try:
        verdict, found = fn(rule, ctx)
        # The findings are iterated by report()/fmt() and the gate loops OUTSIDE this try, which hard-index
        # `severity`/`message`; so validate the FULL finding.v1 shape here (not merely list-of-dicts), or a kind
        # returning e.g. [{}] would pass and then crash downstream with KeyError instead of failing closed.
        if not isinstance(found, list) or not all(
                isinstance(item, dict) and item.get("severity") in ("hard", "soft")
                and isinstance(item.get("message"), str) for item in found):
            raise TypeError("a check kind must return (pass/fail, a list of finding.v1 objects "
                            "{severity: hard|soft, message: str, location})")
    except Exception as exc:  # an erroring OR malformed callable fails closed
        return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind "
                       f"'{kind}') errored and could not evaluate: {exc}")]
    return verdict, found


# ---- dispatcher ------------------------------------------------------------

def load_rules() -> list:
    if not os.path.isdir(CHECK_DIR):
        return []
    return [load_json(os.path.join(CHECK_DIR, n))
            for n in sorted(os.listdir(CHECK_DIR)) if n.endswith(".json")]


def load_suites() -> dict:
    """The suite declarations {name: {trigger, context}}, validated against
    suites.v1.json. Raises (loud) if the file is missing, malformed, or does not
    conform — the dispatcher's own config follows the halt-on-malformed posture."""
    from jsonschema import Draft202012Validator        # lazy: see the module __getattr__ note (tool-runtime dep)
    data = load_json(SUITES_PATH)
    schema = load_json(SUITES_SCHEMA_PATH)
    errs = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errs:
        raise ValueError(".engine/suites.json does not conform to suites.v1.json: "
                         + "; ".join(e.message for e in errs))
    return data["suites"]


def get_pr_body(body_file: str | None) -> str | None:
    if body_file:
        return read(body_file)
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and os.path.exists(event):
        pr = (load_json(event).get("pull_request") or {})
        return pr.get("body") or ""
    return None


def get_pr_author() -> str | None:
    """The PR author's login from the trusted event context (.pull_request.user.login —
    GitHub-stamped from the real author, not fork-forgeable), or None when unavailable: a
    local run, a --pr-body-file invocation, or a malformed/partial event. NEVER github.actor
    (a re-run attributes that to the re-runner — the documented spoof vector). Read only; the
    sole consumer is run()'s ci_author_exempt honoring in the merge-gating suite. Degrades to
    None — and therefore to ENFORCING the rule — on any doubt, never to a falsely-exempt author."""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and os.path.exists(event):
        try:
            pr = (load_json(event).get("pull_request") or {})
            return (pr.get("user") or {}).get("login")
        except (OSError, ValueError, AttributeError, TypeError):
            return None                    # unreadable / malformed / type-confused event → no author
    return None


def get_pr_labels() -> list:
    """The PR's label names from the trusted event context (.pull_request.labels[].name), or an
    EMPTY list when unavailable: a local run, a --pr-body-file invocation, or a malformed/partial
    event. Read only; the sole consumer is _evaluate()'s ci_label_exempt honoring in the merge-gating
    suite. Degrades to [] — and therefore to ENFORCING the rule — on any doubt (a non-list labels
    field, a label without a string name, an unreadable event), never to a falsely-exempt label.
    Mirrors get_pr_author()'s fail-safe posture: empty means 'no exemption', never 'skip the check'."""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and os.path.exists(event):
        try:
            labels = (load_json(event).get("pull_request") or {}).get("labels")
            if not isinstance(labels, list):
                return []                  # absent / type-confused labels → no exemption (enforce)
            return [lbl["name"] for lbl in labels
                    if isinstance(lbl, dict) and isinstance(lbl.get("name"), str)]
        except (OSError, ValueError, AttributeError, TypeError):
            return []                      # unreadable / malformed / type-confused event → no labels
    return []


def _exemption_note(rule: dict, ctx: dict) -> "str | None":
    """The disclosed not-applicable note when a merge-gating rule does not bind for THIS pull
    request — waived by its author (ci_author_exempt) or by a label it carries (ci_label_exempt) —
    or None when the rule binds normally and its kind must run. Called by _evaluate ONLY in the
    blocking-gate suite (so the waiver lands exactly where a rule would otherwise block a merge);
    the by-id run_check() path never reaches here, so the guardrail-weakening guard is never
    exempt. Exact-match only (no case-folding — silent widening is a spoof concern). Author is
    checked first; both forms emit a stated pass that names WHY the rule did not apply, never a
    silent green."""
    author = ctx.get("pr_author")
    if author in (rule.get("ci_author_exempt") or []):
        return (f"NOT APPLICABLE — check '{rule.get('id')}' does not bind for pull requests "
                f"authored by {author} in the merge gate, so it was not evaluated "
                f"here (a disclosed not-applicable pass — not a verification). This narrative "
                f"check is waived for this author only; any guardrail-touching change in the pull "
                f"request is still judged by the weakening guard (blocking at its killswitch tier).")
    matched = sorted(set(ctx.get("pr_labels") or []) & set(rule.get("ci_label_exempt") or []))
    if matched:
        return (f"NOT APPLICABLE — check '{rule.get('id')}' does not bind for pull requests "
                f"labelled '{matched[0]}' in the merge gate, so it was not evaluated here (a "
                f"disclosed not-applicable pass — not a verification). This narrative check is "
                f"waived for this single-purpose pull-request class only, which carries its own "
                f"deliberate plain-language body; any guardrail-touching change in the pull "
                f"request is still gated by the guardrail-ack label the maintainer applies.")
    return None


def _evaluate(rules: list, suite: str, gates: bool, ctx: dict, with_source: bool = False,
              rule_filter=None) -> list:
    """Dispatch every rule that joins `suite` through its kind and return the collected
    findings. The shared core behind both run() (which prints + computes an exit code) and
    collect() (which returns the data). `gates` is the suite's blocking-gate context — it
    decides only where ci_author_exempt waives, never what is collected.

    `rule_filter` (a `rule -> bool` predicate, default None = every suite member) narrows the
    roster WITHOUT a second dispatch path — the touched-file subset (PostToolUse) passes it to run
    only the rules whose target selects an edited file, riding the SAME fail-closed/exempt dispatch
    as a full run so the incremental pass can never diverge from the whole-suite one.

    With `with_source`, each finding is annotated with the rule that emitted it — `source_rule`
    (the rule id) and `source_kind` (its kind) — so a programmatic consumer can tell, say, a
    soft length-budget nudge (kind `shape`) apart from another soft finding firing in the same
    suite. The finding.v1 base allows these extra keys (it fixes no closed property set), and the
    default (off) leaves run()'s and the existing feed's findings byte-for-byte unchanged."""
    findings = []
    registry = resolved_registry()  # resolve ONCE per run; the meta-check reads the same seam, so rosters can't desync
    for rule in [r for r in rules if suite in r.get("suites", [])]:
        if rule_filter is not None and not rule_filter(rule):
            continue
        kind, tier = rule.get("kind"), rule.get("tier", "hard")
        # Honor ci_author_exempt / ci_label_exempt at the engine layer — before any check-kind
        # runs, so the closed kinds stay author- and label-agnostic. They bind ONLY in the
        # merge-gating (blocking-gate) suite (`gates`, derived from the suite's context, not its
        # name): the exemption waives exactly where a rule would otherwise block a merge. A matched
        # author OR a matched label yields a DISCLOSED not-applicable pass (a soft note, never
        # gating), never a silent green and never a workflow skip that would leave the required
        # check pending. Exact-match only (no case-folding — silent widening is a spoof concern).
        # The by-id run_check() path carries no suite and so never reaches here: the
        # guardrail-weakening guard is never exempt.
        exempt_note = _exemption_note(rule, ctx) if gates else None
        if exempt_note is not None:
            # NOT a disclosed_noop: this is a check WAIVED in the merge-gating context (an exempt
            # author/label), a consequential disclosure that must stay prominent in the CI log — never
            # collapsed into the dormant "nothing to do" summary. It also never fires on a clean local
            # run, so it is outside the soft-note noise StarshipSuperjam/engine-template#322 targets.
            found = [finding("soft", exempt_note)]
        else:
            _verdict, found = _run_kind(registry, rule, ctx)  # fail-closed on dangling/erroring/malformed
        if with_source:
            for f in found:
                f["source_rule"] = rule.get("id")
                f["source_kind"] = kind
        findings.extend(found)
    return findings


def collect(suite: str, ctx: dict, *, with_source: bool = False, rule_filter=None) -> list:
    """The machine-readable seam behind run(): evaluate `suite` and RETURN its findings
    (each {severity, message, location}) as data, rather than printing a human report. A
    programmatic consumer — the audit soft-findings feed, and the local pre-commit/pre-close/
    touched-file nudges — reads the report-only findings here instead of scraping run()'s stdout.
    RAISES (ValueError / the loader's exception) on a config error (undeclared suite, unloadable
    suites/rules); the caller decides how to surface it (run() turns it into the loud exit-2 path,
    the feed into an honest marker, a local nudge into silence via _safe_collect).

    `with_source` annotates each finding with its emitting rule (`source_rule`/`source_kind`) —
    off by default, so the existing feed reads the bare base shape unchanged. `rule_filter`
    (a `rule -> bool` predicate) narrows the roster for the touched-file subset."""
    suites = load_suites()
    decl = suites.get(suite)
    if decl is None:
        raise ValueError(f"suite '{suite}' is not declared in .engine/suites.json "
                         f"(declared: {', '.join(sorted(suites))}).")
    gates = decl.get("context") == "blocking-gate"
    return _evaluate(load_rules(), suite, gates, ctx, with_source=with_source, rule_filter=rule_filter)


def run(suite: str, ctx: dict) -> int:
    try:
        suites = load_suites()
    except Exception as exc:  # a broken suites.json/schema halts loudly (config error)
        print(f"\nCONFIG ERROR: cannot load the suite declarations: {exc}", file=sys.stderr)
        return 2
    decl = suites.get(suite)
    if decl is None:
        print(f"\nCONFIG ERROR: suite '{suite}' is not declared in .engine/suites.json "
              f"(declared: {', '.join(sorted(suites))}).", file=sys.stderr)
        return 2
    gates = decl.get("context") == "blocking-gate"  # only a blocking-gate context can fail the run
    try:
        rules = load_rules()
    except Exception as exc:  # a broken check rule file halts loudly (config error), in plain language
        print(f"\nCONFIG ERROR: cannot load the check rules: {exc}", file=sys.stderr)
        return 2
    # with_source so report() can name the checks whose disclosed-no-op notes it collapses;
    # the extra source_rule/source_kind keys are inert for the hard/actionable render paths.
    findings = _evaluate(rules, suite, gates, ctx, with_source=True)
    report(suite, findings, gates)
    # Gate on the authoritative signal — any hard-severity finding — but only where
    # the suite's context is a blocking-gate. A callable's verdict flag is advisory;
    # the rule's tier (carried as the finding severity) and the suite context decide
    # where teeth land, so the exit code and report() can never disagree.
    hard_fired = any(f["severity"] == "hard" for f in findings)
    return 1 if (gates and hard_fired) else 0


def run_check(check_id: str, ctx: dict) -> int:
    """Run ONE check rule, selected by its `id` field, directly — the "a check is a
    directly-callable unit, not only trigger-driven" path the validation foundation blesses.
    It loads ONLY the check rules (NOT suites.json), so a broken or loosened suite
    declaration can never strand or alter a directly-invoked guard — the isolation the
    guardrail-weakening guard relies on. It dispatches via the same kind registry as
    run(), and a dangling kind or an erroring callable FAILS CLOSED (a hard finding),
    exactly as in run(). A by-id run always gates (it is invoked deliberately, outside a
    suite's context): exit 1 on any hard finding, 0 if clean, 2 on an unknown id (a loud
    config error, like run()'s undeclared-suite path)."""
    try:
        rules = load_rules()
    except Exception as exc:  # a broken check rule file halts loudly (config error), in plain language
        print(f"\nCONFIG ERROR: cannot load the check rules: {exc}", file=sys.stderr)
        return 2
    matches = [r for r in rules if r.get("id") == check_id]
    if not matches:
        print(f"\nCONFIG ERROR: no check rule has id '{check_id}'.", file=sys.stderr)
        return 2
    findings = []
    registry = resolved_registry()  # same resolved core+discovered set as run() — a by-id guard dispatches identically
    for rule in matches:
        _verdict, found = _run_kind(registry, rule, ctx)  # fail-closed on dangling/erroring/malformed
        findings.extend(found)
    report(check_id, findings, True)  # a by-id run always gates
    hard_fired = any(f["severity"] == "hard" for f in findings)
    return 1 if hard_fired else 0


def run_unit(unit, target=None, ctx=None):
    """Run ONE check-logic unit against a caller-substituted target and return its
    (passed, findings) exactly as production would — the target-substitution affordance the
    negative-fixture meta-check (StarshipSuperjam/engine-template#286) needs to witness that each hard check
    actually BITES a seeded bad input. It is NOT a production entry point: run()/run_check()/
    --check never call it, so those paths and every existing finding are byte-unchanged.

    `unit` is the rule to run — its `kind` selects the REAL REGISTRY callable (a custom/script
    unit carries params.script). For the closed kinds it is a transient rule the caller crafts;
    for a custom/script it is the real committed rule. `target` (a dict) substitutes the input
    by unit class, overlaying only the keys that class reads and leaving the rest of `ctx`:
      - path:               presence/schema/shape — the fixture glob, set as target.path
                            (target_files resolves it under ROOT); the real callable is unchanged.
      - coverage_catalog,
        coverage_root:      coverage — the catalog source + walk root the real callable reads.
      - manifests:          coherence — the manifest set the real callable reads (ctx['manifests']).
      - ctx:                a module-provided kind — a dict overlaid onto ctx, the generic seam a
                            module kind's negative fixture uses to inject its seeded input (the closed
                            kinds read the structural keys above; a module kind reads whatever it
                            declared, handed to it here).
      - env:                custom/script — env vars set around the child and restored after, so a
                            script reading a substituted GITHUB_EVENT_PATH (or another agreed
                            variable the fixture's script honours) sees the seeded input. (No edit
                            to kind_custom_script — it already builds its child env from os.environ.)
    NOTE on `env`: it mutates the process-global os.environ for the duration of the call (restored
    in a finally), so it is meaningful ONLY for the custom/script kind — which builds its child env
    from os.environ; the in-process closed kinds never read the environment. It is therefore not
    safe to call run_unit with `env` from multiple threads at once (no current/planned caller does).
    Dispatch mirrors run()/run_check(): an unregistered kind or an erroring callable FAILS CLOSED
    (a hard finding), so the meta-check witnesses exactly what the gate does."""
    target = target or {}
    ctx = dict(ctx or {})
    rule = dict(unit)
    if "path" in target:
        rule["target"] = {**(rule.get("target") or {}), "path": target["path"]}
    for key in ("coverage_catalog", "coverage_root", "manifests"):
        if key in target:
            ctx[key] = target[key]
    if isinstance(target.get("ctx"), dict):  # generic ctx overlay — a module kind's fixture injects its input here
        ctx.update(target["ctx"])
    # Dispatch through the SAME resolved registry + fail-closed helper as run()/run_check(), so a
    # module kind's fixture is driven exactly as production drives the kind (and a dangling/erroring/
    # malformed kind fails closed identically). resolved_registry() reads the ENGINE_KIND_DIR seam, so
    # a meta-check pointed at a fixture kind dir sees the same roster the dispatcher would.
    registry = resolved_registry()
    env = target.get("env")
    if not env:
        return _run_kind(registry, rule, ctx)
    saved = {k: os.environ.get(k) for k in env}      # set the substituted target in the child env...
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        return _run_kind(registry, rule, ctx)
    finally:                                          # ...and restore os.environ no matter what
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# ---- local triggers: the pre-commit / pre-close / touched-file nudges ----------
# The four v1 triggers are declared in suites.json; CI is the merge gate (teeth). These wire the three
# LOCAL ones as best-effort ADVICE that NEVER blocks: the same rules run, but a hard finding surfaces as
# a nudge, not a wall. The handlers below
# return ONLY hooks.proceed()/hooks.inject() — never block()/decide(...): on a block-eligible event
# (PreToolUse) the harness WOULD honor a block or a deny, so keeping to proceed/inject is what holds the
# block budget to modes + close. The block-budget coherence check CANNOT see this (validate registers no
# invariant, and PreToolUse is eligible anyway), so a regression test in test_validate exercises each
# handler across finding states as a backstop — code discipline, not a structural guarantee: a NEW hook
# handler added here later needs its own never-block test. `hooks` is imported LAZILY inside each handler:
# validate must import on the stdlib alone (the first-run bootstrap), and hooks imports validate.

# `apply_patch` (Codex's edit tool) is normalized to Edit before any handler runs; its membership
# here is the second-belt defense, inert on Claude (which never emits the name) — mirrors modes.py.
_MUTATING_FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})


def local_ctx() -> dict:
    """The ctx a LOCAL suite run builds — the same shape main() builds for CI, degrading to None/[] with
    no PR and no network: get_pr_body/author/labels read only $GITHUB_EVENT_PATH plus a local file, never
    a subprocess or a network call, so a local nudge can never hang a git commit or a turn-close."""
    return {"pr_body": get_pr_body(None), "pr_author": get_pr_author(), "pr_labels": get_pr_labels()}


def _safe_collect(suite: str, ctx: dict = None, *, rule_filter=None) -> list:
    """collect() for a LOCAL advisory: return [] on ANY failure rather than raising, so an advisory run
    degrades to silence and never strands the session — the local fail-open by design (a broken
    kind never strands the working session; teeth are at CI). A per-rule kind error is already a
    fail-closed FINDING inside collect() and still surfaces in the nudge; this guards the total-config
    failure that locally is advice-that-couldn't-run. `ctx` defaults to local_ctx() BUILT INSIDE the
    guard — get_pr_body raises on a malformed $GITHUB_EVENT_PATH (unlike its siblings), so building the
    ctx here keeps even that off the hook path (the CI ctx in main() is unchanged and still fails loud)."""
    try:
        return collect(suite, local_ctx() if ctx is None else ctx, with_source=True, rule_filter=rule_filter)
    except Exception:  # noqa: BLE001 — a local advisory never raises into a hook; the gate is CI
        return []


def _nudge_context(findings: list) -> "str | None":
    """The advisory text a local nudge injects, or None when there is nothing to say. AI-FACING: it is
    injected as additionalContext to the assistant, NOT shown to the operator (the operator-facing honesty
    lines live in the PR body + boot orientation). It states plainly that the local run is advice and the
    merge-time check is the only gate, then names each hard finding. It never blocks."""
    hard = [f for f in findings if f.get("severity") == "hard"]
    if not hard:
        return None
    lines = "\n".join("  - " + fmt(f) for f in hard)
    return ("A local advisory check ran while working — it does NOT block, and it is not the gate. The "
            "automatic check that runs when a change is proposed for merge is the only thing that can stop "
            f"a risky merge. This early run flagged:\n{lines}\n"
            "Worth resolving before the change is proposed for merge, but your call.")


def _touched_paths(payload: dict) -> list:
    """EVERY file a mutating tool call edited (Edit/Write/MultiEdit → tool_input.file_path;
    NotebookEdit → notebook_path; a normalized multi-file edit — Codex's batch apply_patch — carries
    the full list in tool_input.file_paths), or [] for any non-file tool (a Bash/Read call has
    nothing to re-check). Degrades safe on a malformed payload."""
    if not isinstance(payload, dict) or payload.get("tool_name") not in _MUTATING_FILE_TOOLS:
        return []
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return []
    out = []
    single = ti.get("file_path") or ti.get("notebook_path")
    if isinstance(single, str) and single:
        out.append(single)
    many = ti.get("file_paths")
    if isinstance(many, list):
        out += [p for p in many if isinstance(p, str) and p and p not in out]
    return out


def _abs_under_root(path: str) -> str:
    """A touched path normalized to the absolute form target_files emits (glob under ROOT)."""
    return os.path.abspath(path) if os.path.isabs(path) else os.path.abspath(os.path.join(ROOT, path))


def _rule_touches(rule: dict, touched_abs: set) -> bool:
    """True iff a rule's `target.path` glob selects one of the touched files. A context-targeted rule
    (no target.path) selects no files (target_files → []), so it never joins the touched-file subset —
    correct: those rules examine the whole change set, not a single edit. No v1 pre-commit rule is
    path-targeted, so this subset is dormant against the current ruleset and activates for a deployed
    repo that adds a file-scoped rule."""
    return bool(set(target_files(rule)) & touched_abs)


# The check kinds ambient capture records: FILE-SCOPED and IN-PROCESS. schema/shape/presence each evaluate a
# named file, so a per-edit run over ONE touched file is cheap and meaningful. coverage and custom/script are
# EXCLUDED — they walk the whole tree / spawn a child, so they have no business on the per-edit hot path.
# (This is a code-level roster, not a declared suite — a deliberate v1 choice so the writer needs no edit to
# 20+ rule JSONs; expressing it as an `ambient` suite is a legibility refinement for when the ruleset grows.)
_AMBIENT_KINDS = frozenset({"schema", "shape", "presence"})


def evaluate_touched_fires(paths: list, ctx: dict = None) -> list:
    """The ambient writer's check-run (telemetry OWNS the record + cache; this — the check-running hook side —
    RELAYS the verdicts). For each touched file, run each FILE-SCOPED IN-PROCESS rule that selects it against
    THAT ONE FILE (run_unit's single-target substitution — NOT the rule's whole glob, so editing file A never
    records a fire for a broken sibling B, and the hot path never re-validates the tree), and return one
    `(rule_id, passed, target)` per fire — `passed` is the check's verdict (no HARD finding), read from the
    check, not from any hook exit. `target` is the touched file relative to ROOT (what the vanished-target
    auto-resolve later checks). Best-effort: a per-rule error drops that one fire, never raises into the
    caller (the PostToolUse hot path). Draws from the FULL rule corpus — a policy/schema edit fires real
    file-scoped checks — so ambient capture is genuinely live, not tied to the (currently empty) pre-commit
    touched-file subset."""
    ctx = ctx if ctx is not None else local_ctx()
    touched = [(p, _abs_under_root(p)) for p in paths]
    fires = []
    for rule in load_rules():
        if rule.get("kind") not in _AMBIENT_KINDS or not (rule.get("target") or {}).get("path"):
            continue
        selected = set(target_files(rule))
        for _display, abs_path in touched:
            if abs_path not in selected:
                continue
            try:
                _, findings = run_unit(rule, target={"path": abs_path}, ctx=ctx)
                passed = not any(f.get("severity") == "hard" for f in findings)
            except Exception:  # noqa: BLE001 — a broken rule drops its fire, never strands the hot path
                continue
            fires.append((rule.get("id"), passed, os.path.relpath(abs_path, ROOT)))
    return fires


def _precommit_handler(payload: dict) -> dict:
    """PreToolUse: on a `git commit`, run the pre-commit suite and NUDGE (inject) any hard finding —
    ADVICE only. Returns proceed()/inject() ONLY, never block()/decide(...). Any other tool call, or a
    clean run, proceeds silently."""
    import hooks  # lazy (stdlib-only bootstrap; hooks imports validate)
    if not hooks._is_git_commit(payload):
        return hooks.proceed()
    context = _nudge_context(_safe_collect("pre-commit"))   # ctx built inside the guard (fail-open)
    return hooks.inject(context) if context else hooks.proceed()


def _accept_handler(payload: dict) -> dict:
    """PostToolUse: after an edit, (1) RELAY the edit to telemetry's ambient capture — record each local
    file-scoped check's pass/fail over the touched file to the gitignored ambient cache (telemetry owns the
    record + cache; this hook, the one that runs checks, relays — the detection-relay seam); and (2) run the touched-file
    subset of the pre-commit NUDGE and inject any hard finding. Both are ADVICE only — PostToolUse cannot
    block by contract; this returns proceed()/inject() ONLY. The nudge subset is context-targeted in v1 (so it
    stays quiet), but ambient capture draws from the full file-scoped corpus, so it is genuinely live. The
    capture is wrapped so a failure never disturbs the tool call (append_ambient is itself best-effort too)."""
    import hooks  # lazy (see _precommit_handler)
    paths = _touched_paths(payload)
    if not paths:
        return hooks.proceed()
    try:
        import telemetry  # lazy: telemetry imports validate (a back-edge safe only lazily)
        import moment  # lazy: keep the module-top stdlib-only bootstrap contract (moment is itself stdlib-only)
        telemetry.capture_touched_fires(paths, moment.utc_now())
    except Exception:  # noqa: BLE001 — ambient capture is best-effort and NEVER gates a tool call
        pass
    touched = {_abs_under_root(p) for p in paths}
    findings = _safe_collect("pre-commit", rule_filter=lambda r: _rule_touches(r, touched))
    context = _nudge_context(findings)
    return hooks.inject(context) if context else hooks.proceed()


def run_files(paths: list) -> int:
    """The touched-file subset over explicit paths — the CLI form of the PostToolUse pass (the demo and a
    manual incremental check use it). Runs the pre-commit rules whose target selects any given path,
    reports, and exits 0 (a local nudge never gates)."""
    touched = {_abs_under_root(p) for p in paths}
    findings = _safe_collect("pre-commit", rule_filter=lambda r: _rule_touches(r, touched))
    report("pre-commit", findings, False)  # local-nudge context: advisory, never gates
    return 0


def fmt(f: dict) -> str:
    where = ""
    if f.get("location"):
        l = f["location"]
        where = f"  [{l.get('file')}" + (f":{l['line']}" if l.get("line") else "") + "]"
    return f["message"] + where


def report(suite: str, findings: list, gates: bool) -> None:
    hard = [f for f in findings if f["severity"] == "hard"]
    soft = [f for f in findings if f["severity"] != "hard"]
    # Partition soft notes so an actionable one stands out from the dormant "nothing to do" ones.
    # Only a no-op we can NAME (it carries a source_rule) is collapsed into the summary line; an
    # actionable note — OR a marked no-op with no source rule to name (the by-id `--check` path does
    # not set one, and a single deliberately-invoked check is never noise) — renders in full. A
    # finding WITHOUT the marker defaults to actionable (`.get`, never `[]`), so the fail-safe is a
    # note shown in full (harmless), never an actionable note hidden. The collapsed no-ops stay
    # DISCLOSED (named + counted, never a silent skip); only their boilerplate prose folds away.
    collapsible, shown = [], []
    for f in soft:
        (collapsible if f.get("not_applicable") and f.get("source_rule") else shown).append(f)
    displayed = len(shown) + (1 if collapsible else 0)
    if displayed:
        print(f"\nnotes ({displayed}):")
        for f in shown:
            print("  - " + fmt(f))
        if collapsible:
            names = list(dict.fromkeys(str(f["source_rule"]) for f in collapsible))
            print(f"  - {len(names)} check(s) not applicable here (nothing to do): " + ", ".join(names))
    if hard and gates:
        print(f"\nFAIL ({len(hard)} hard finding(s)) [suite: {suite}] — blocks the merge:")
        for f in hard:
            print("  - " + fmt(f))
    elif hard:
        print(f"\n{len(hard)} hard finding(s) [suite: {suite}] — advisory here, not blocking:")
        for f in hard:
            print("  - " + fmt(f))
    else:
        print(f"\nOK — suite '{suite}' passed, no hard findings.")


def _demo(argv: list) -> int:
    """Operator-runnable, self-checking demo of the local triggers — a falsification that can FAIL. It
    exercises the REAL handlers (no reimplementation) and asserts the load-bearing claims: a `git commit`
    never blocks (only proceed/inject), a non-commit no-ops, the touched-file subset selects a
    path-targeted rule but NOT a v1 context-targeted rule (the honest dormancy), and a broken run fails
    open. The real repo and committed files are never touched (payloads + a synthetic rule dict only)."""
    ok = True
    commit = {"tool_name": "Bash", "tool_input": {"command": "git add -A && git commit -m x"}}
    status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}

    print("(i) On a `git commit`, the pre-commit advisory runs and NEVER blocks (only proceed/inject):")
    d = _precommit_handler(commit)
    ok = ok and d.get("action") in ("proceed", "inject")
    print(f"    action={d.get('action')!r}  (a block/deny here would be the alarm)")

    print("(ii) On a non-commit tool call, it no-ops (proceed):")
    d2 = _precommit_handler(status)
    ok = ok and d2 == {"action": "proceed"}
    print(f"    action={d2.get('action')!r}")

    print("(iii) The touched-file subset selects a path-targeted rule, but not a whole-change (context) "
          "rule — so it is dormant against every v1 pre-commit rule:")
    synthetic = {"id": "demo/synthetic-path", "kind": "presence", "tier": "soft", "suites": ["pre-commit"],
                 "target": {"path": ".engine/tools/validate.py"}}
    context_rule = {"id": "demo/whole-change", "kind": "custom/script", "tier": "hard",
                    "suites": ["pre-commit"], "target": {"context": "product-spec"}}
    touched = {_abs_under_root(".engine/tools/validate.py")}
    sel_synth, sel_ctx = _rule_touches(synthetic, touched), _rule_touches(context_rule, touched)
    ok = ok and sel_synth and not sel_ctx
    print(f"    path-targeted rule selected: {sel_synth}   context-targeted rule selected: {sel_ctx}")

    print("(iv) A broken advisory run fails OPEN — returns no findings instead of raising:")
    failed_open = _safe_collect("no-such-suite", local_ctx()) == []
    ok = ok and failed_open
    print(f"    unknown suite -> [] (no exception): {failed_open}")

    if not ok:
        print("\nDEMO UNEXPECTED: a local trigger must never block, must no-op off a commit, must select "
              "only a path-targeted rule, and must fail open.", file=sys.stderr)
        return 1
    print("\nDone — the local triggers nudge as advice, never block, and fail open. The merge-time check "
          "stays the only gate.")
    return 0


def _demo_kinds(argv: list) -> int:
    """Operator-runnable, self-checking demo of module check-kind discovery (leg 3 of StarshipSuperjam/engine-template#405) — a falsification that
    can FAIL. It writes a SYNTHETIC kind into a temp dir (never the real repo) and exercises the REAL resolver:
    a dropped kind is discovered and merged OVER the core, a file named for a core kind CANNOT shadow it, a bad
    file is a loud fault (not a crash), and with no module kind present the registry is exactly the closed core."""
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        mod = os.path.join(tmp, "demomod")
        os.makedirs(mod)
        for name, body in (("demo", "def check(rule, ctx):\n    return True, []\n"),
                           ("schema", "def check(rule, ctx):\n    return True, []\n"),   # tries to shadow core
                           ("broken", "raise RuntimeError('boom')\n")):                   # unimportable
            with open(os.path.join(mod, f"kind_{name}.py"), "w", encoding="utf-8") as fh:
                fh.write(body)

        print("(i) A module drops `demomod/kind_demo.py`; discovery finds it and merges it OVER the core kinds:")
        reg = resolved_registry(kind_dir=tmp)
        found_demo = "demo" in reg and all(k in reg for k in REGISTRY)
        ok = ok and found_demo
        print(f"    'demo' discovered and every core kind still present: {found_demo}")

        print("(ii) A file named `kind_schema.py` CANNOT shadow the closed core `schema` kind:")
        core_wins = reg.get("schema") is REGISTRY["schema"]
        ok = ok and core_wins
        print(f"    resolved 'schema' is still the core callable: {core_wins}")

        print("(iii) A core-name collision and an unimportable file are LOUD faults (hard findings), not silent:")
        faults = kind_discovery_findings(kind_dir=tmp)
        loud = (any("core kind 'schema'" in f["message"] for f in faults)
                and any("could not be imported" in f["message"] for f in faults))
        ok = ok and loud
        print(f"    collision + unimportable both surfaced as hard findings: {loud}")

    print("(iv) With no module kind present, the registry is EXACTLY the closed core (production today):")
    prod_clean = resolved_registry(kind_dir=os.path.join(tempfile.gettempdir(), "engine-no-such-kind-dir")) == REGISTRY
    ok = ok and prod_clean
    print(f"    resolved registry == the closed core: {prod_clean}")

    if not ok:
        print("\nDEMO UNEXPECTED: discovery must find a dropped kind, never let it shadow core, surface a bad "
              "file loudly, and reduce to the closed core when none is present.", file=sys.stderr)
        return 1
    print("\nDone — a module extends validation by dropping a kind file; the core stays closed, and a colliding "
          "or broken kind is surfaced loudly rather than silently binding.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "hook":            # the PreToolUse pre-commit nudge (settings.json wires this)
        import hooks
        return hooks.run_hook("PreToolUse", _precommit_handler)
    if argv and argv[0] == "accept-hook":     # the PostToolUse touched-file nudge
        import hooks
        return hooks.run_hook("PostToolUse", _accept_handler)
    if argv and argv[0] == "demo":
        return _demo(argv[1:])
    if argv and argv[0] == "demo-kinds":      # the module check-kind discovery self-check (leg 3 of StarshipSuperjam/engine-template#405)
        return _demo_kinds(argv[1:])
    if argv and argv[0] == "--files":         # the CLI form of the touched-file subset over given paths
        return run_files(argv[1:])
    suite, body_file, check_id, i = "CI", None, None, 0
    while i < len(argv):
        if argv[i] == "--suite" and i + 1 < len(argv):
            suite, i = argv[i + 1], i + 2
        elif argv[i] == "--pr-body-file" and i + 1 < len(argv):
            body_file, i = argv[i + 1], i + 2
        elif argv[i] == "--check" and i + 1 < len(argv):
            check_id, i = argv[i + 1], i + 2
        else:
            print(f"unknown argument: {argv[i]}", file=sys.stderr)
            return 2
    ctx = {"pr_body": get_pr_body(body_file),     # the same ctx both entry points build
           "pr_author": get_pr_author(),           # honored by run() for ci_author_exempt (CI gate only)
           "pr_labels": get_pr_labels()}           # honored by run() for ci_label_exempt (CI gate only)
    if check_id is not None:
        return run_check(check_id, ctx)
    return run(suite, ctx)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
