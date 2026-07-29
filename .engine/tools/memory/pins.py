"""pins.py — durable operator intent, saved the moment they ask for it.

WHAT A PIN IS FOR. Most of what a project decides has a better home than memory: a merged pull request, a
decision record, the code itself. A pin is for the residue — a standing preference, a way of working, a "never
do that again" — that has no canonical artifact to live in and would otherwise survive only as a sentence in a
conversation nobody thinks to search for. eADR-0038 puts it in the one substrate as a record-type rather than a
sixth store, and that is exactly what this is: an ordinary ledger record that ordinary recall surfaces.

WHY IT IS DELIBERATE AND SMALL. Pins are the one thing here nothing ages out and nothing summarises away, and
the cold-start briefing carries them into every session. That is the whole point, and it is also why minting
one is an explicit act rather than something inferred: a store that pins generously stops being a small set of
standing intentions and becomes another stream to wade through, at a cost paid on every session start forever.
So there is a verb, the operator says the word, and `remove` is a first-class verb rather than an afterthought.

WHAT THE PROVENANCE FIELD DOES AND DOES NOT CLAIM. A pin records the route it arrived by
(`records.PIN_VIA_KEY`) and nothing stronger. When a model calls the write tool it is transcribing what the
operator asked for, and its context may also hold a page it recalled, a file it read, or tool output — text
shaped like an instruction that nobody typed. Nothing downstream can tell those apart. So every reader presents
a pin as something saved when the operator asked, never as a verified quotation, and this module's job is to
make sure the field needed to say that honestly is always present.

SCRUBBED ON THE WAY IN. A pin does not travel through capture, so capture's secret scrub never sees it. A
credential pasted into a session and then pinned would be stored unscrubbed and read into every future
briefing — worse than the exposure the scrub exists to prevent. `add` runs the same scrubber, and says so.

REMOVING A PIN IS WITHHOLDING IT. There is no second retirement path: `remove` calls `forget.withhold`, so a
removed pin stops being surfaced, stays in the ledger, and comes back with `forget.restore` like anything else
the operator withheld. One mechanism, one mental model, and the append-only law untouched.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import forget, ledger, records, scrub  # noqa: E402

# What one pin may carry. A pin is a standing intention, not a document: the briefing reads every live pin at
# every session start, so an unbounded one would quietly spend a growing share of the pack forever. The cap is
# generous enough for a real preference stated in full sentences and refuses rather than truncating, because a
# silently halved instruction is worse than one that was not saved.
MAX_PIN_CHARS = 1000

# How many pins the briefing carries. Not a cap on how many may exist — `list_pins` returns them all — but the
# newest-first slice a cold start is shown, so the pack stays bounded however many accumulate.
BOOT_PINS = 5


class PinRefused(ValueError):
    """A pin could not be saved, with the plain-language reason. Raised rather than returned: the operator
    asked for something to be remembered, and a verb that quietly declined would leave them believing it was."""


def add(text: str, *, session_id: "str | None" = None, via: str = records.PIN_VIA_ASSISTANT,
        path: "str | None" = None, now: "int | None" = None) -> dict:
    """Save one pin and return the record as written. Raises PinRefused on empty or over-long text.

    The text is scrubbed before it is stored (module docstring), so what lands in the ledger is what every
    later reader sees — there is no unscrubbed copy anywhere. `session_id` records where it was asked for, so
    the conversation around the request stays reachable with the window reader; a pin minted outside a session
    simply carries none. `via` records the route, never an authority claim.

    Appends under the single-writer lock and bumps the ledger generation, exactly as the withhold verbs do and
    for the same reason: without it the fast index stays stamped current and the pin the operator just saved is
    missing from the next search, answered as though the index were authoritative."""
    if not isinstance(text, str) or not text.strip():
        raise PinRefused("there was nothing to save — a pin needs some words.")
    cleaned = scrub.scrub_text(text.strip())
    if len(cleaned) > MAX_PIN_CHARS:
        raise PinRefused(
            f"that is longer than a pin holds ({len(cleaned)} characters against a limit of {MAX_PIN_CHARS}). "
            "Nothing was saved. Shorten it to the standing instruction itself, or let it live in the "
            "conversation, which stays searchable either way."
        )
    if via not in (records.PIN_VIA_ASSISTANT, records.PIN_VIA_CLI):
        via = records.PIN_VIA_ASSISTANT
    from memory import capture  # lazy: keep capture off the module-load path (cycle discipline)
    target = path if path is not None else ledger.ledger_path()
    data_dir = os.path.dirname(target) or "."
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        # `None` is not proof of contention: the same value comes back when the store cannot be opened at all.
        # "Try again in a moment" over a permissions problem or a full disk is advice that can never work.
        writable = os.access(data_dir, os.W_OK)
        raise PinRefused(
            "another memory write is in progress, so nothing was saved. Try again in a moment."
            if writable else
            f"memory could not be written to ({data_dir} is not writable), so nothing was saved. This will "
            "not clear on its own — check the folder's permissions and that its disk is mounted and has room."
        )
    try:
        record = {
            "v": capture.RECORD_VERSION,
            "kind": records.PIN_KIND,
            records.RECORD_ID_KEY: records.new_record_id(),
            "text": cleaned,
            "ts": int(time.time()) if now is None else now,
            "tags": [records.PIN_TAG],
            records.PIN_VIA_KEY: via,
        }
        if isinstance(session_id, str) and session_id:
            record[records.PIN_SOURCE_SESSION_KEY] = session_id
        ledger.bump_index_epoch(for_path=target)
        ledger.append(record, path=path)
        return record
    except PinRefused:
        raise
    except Exception as exc:
        raise PinRefused(f"the pin could not be saved ({exc}).") from exc
    finally:
        capture._release_lock(lock_fd)


def list_pins(*, path: "str | None" = None, limit: "int | None" = None) -> list:
    """Every live pin, newest first. Reads through `live_records`, so a pin the operator removed is absent
    exactly as it is absent from recall — one definition of "live", never a second one that could disagree.

    The operator is promised they can ask what is saved and have it read back, so the default returns all of
    them; `limit` is for the callers that must stay bounded, like the briefing."""
    src = ledger.ledger_path() if path is None else path
    out = [r for r in forget.live_records(path=src)
           if isinstance(r, dict) and r.get("kind") == records.PIN_KIND]
    out.sort(key=lambda r: (isinstance(r.get("ts"), int) and not isinstance(r.get("ts"), bool),
                            r.get("ts") if isinstance(r.get("ts"), int) else 0,
                            r.get(records.RECORD_ID_KEY) or ""), reverse=True)
    return out[:limit] if isinstance(limit, int) and limit >= 0 else out


def remove(record_id: str, *, path: "str | None" = None) -> dict:
    """Stop surfacing one pin. Withholds it (module docstring) — nothing is deleted and `forget.restore` on the
    same id brings it back."""
    return forget.withhold(record_id=record_id, path=path)


def _print_list(path: "str | None" = None) -> int:
    live = list_pins(path=path)
    if not live:
        print("Nothing is pinned. Say \"remember this\" with what you want kept and it will be saved here.")
        return 0
    print(f"{len(live)} pinned:" if len(live) != 1 else "1 pinned:")
    for record in live:
        when = record.get("ts")
        stamp = time.strftime("%Y-%m-%d", time.localtime(when)) if isinstance(when, int) else "unknown date"
        print(f"  [{record.get(records.RECORD_ID_KEY)}] {stamp}  {record.get('text')}")
    return 0


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="pins.py", description="Save and read back durable operator intent.")
    sub = parser.add_subparsers(dest="cmd")
    add_cmd = sub.add_parser("add", help="save a pin")
    add_cmd.add_argument("text", help="the standing instruction to keep")
    add_cmd.add_argument("--session", default=None, help="the session it was asked for in")
    sub.add_parser("list", help="read back every live pin")
    rm = sub.add_parser("remove", help="stop surfacing one pin (reversible)")
    rm.add_argument("record_id", help="the pin's id, as shown by `list`")
    args = parser.parse_args(argv)
    if args.cmd == "add":
        try:
            record = add(args.text, session_id=args.session, via=records.PIN_VIA_CLI)
        except PinRefused as exc:
            print(f"Not saved: {exc}")
            return 1
        print(f"Pinned [{record[records.RECORD_ID_KEY]}].")
        return 0
    if args.cmd == "remove":
        try:
            remove(args.record_id)
        except forget.ControlNotRecorded as exc:
            # The shared verb speaks of "a single note, or a whole session" because it serves both; this
            # command takes a pin id and nothing else, so offering a session here names a choice the operator
            # was never given.
            reason = str(exc).replace("name exactly one thing to act on — a single note, or a whole session.",
                                      "no pin identifier was given.")
            reason = reason.replace("there is no note in memory with that identifier",
                                    "there is no pin with that identifier")
            print(f"Not removed: {reason}")
            return 1
        print("Removed from recall. It is still saved — ask to restore it any time.")
        return 0
    if args.cmd == "list":
        return _print_list()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
