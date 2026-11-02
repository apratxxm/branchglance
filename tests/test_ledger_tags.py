"""Work kind: classification order, `kind:` segment tags, and privacy of stored text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_ledger_support import (  # ledger_guard is autouse
    DAY_END, HOUR_START, claude_file, claude_response, clock_at, codex_count, codex_file, codex_meta,
    ledger_guard, make_roots, stamp, write_jsonl,
)

from token_tracker import ledger_classify as classify_mod
from token_tracker.ledger import Ledger

ALL = {"Claude": True, "Codex": True}


def user(seconds: int, text: str) -> dict:
    return {"type": "user", "timestamp": stamp(seconds), "message": {"role": "user", "content": text}}


def rows(ledger: Ledger) -> dict:
    return {(r["provider"], r["kind"]): r for r in ledger.summary(HOUR_START, DAY_END, show=ALL)["rows"]}


def open_ledger(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    return Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600)), claude


# ----------------------------------------------------------------- classification order

def test_override_beats_tag_and_specific_evidence():
    result = classify_mod.classify(override="build", tag="review", slug="branchglance-sol-review-x")
    assert (result["kind"], result["rule"], result["source"]) == ("build", "override", "user")


def test_tag_beats_slug_task_words():
    result = classify_mod.classify(tag="chore", slug="orchestrator-terra-edge-check", interactive=True)
    assert (result["kind"], result["rule"], result["source"]) == ("chore", "tag", "tag")


def test_specific_task_check_beats_generic_worker():
    result = classify_mod.classify(slug="orchestrator-terra-edge-check")
    assert (result["kind"], result["rule"]) == ("check", "task-check")


def test_review_slug_is_review():
    assert classify_mod.classify(slug="branchglance-sol-review-ledger")["kind"] == "review"


def test_agent_type_beats_generic_worker():
    result = classify_mod.classify(slug="branchglance-terra-build-thing", agent_type="Explore")
    assert (result["kind"], result["rule"]) == ("research", "agent-type")


def test_generic_worker_names_and_lead_and_other():
    assert classify_mod.classify(slug="branchglance-haiku-ledger-p1")["kind"] == "build"
    assert classify_mod.classify(slug="branchglance-sol-plan-x")["kind"] == "review"
    assert classify_mod.classify(interactive=True)["kind"] == "lead"
    assert classify_mod.classify()["kind"] == "other"


def test_exec_review_and_read_only_are_specific():
    assert classify_mod.classify(exec_review=True, interactive=True)["rule"] == "exec-review"
    # Read-only alone is research; read-only with "review" in the title is review.
    alone = classify_mod.classify(read_only=True, interactive=True)
    assert (alone["kind"], alone["rule"]) == ("research", "sandbox-read-only")
    titled = classify_mod.classify(read_only=True, review_title=True)
    assert (titled["kind"], titled["rule"]) == ("review", "sandbox-review")
    assert classify_mod.title_has_review("Please REVIEW this") is True
    assert classify_mod.title_has_review("reviewer notes") is False


def test_recognised_slug_returns_only_the_slug():
    assert classify_mod.recognised_slug("Do the thing in branchglance-haiku-ledger-p1 today") == \
        "branchglance-haiku-ledger-p1"
    assert classify_mod.recognised_slug("no job here") is None


# ----------------------------------------------------------------- kind tag parsing

@pytest.mark.parametrize("text, expected", [
    ("kind: build", "build"),
    ("kind: design", "design"),
    ("kind: explore", "explore"),
    ("kind: chore", "chore"),
    ("Please start.\nkind: review\nthanks", "review"),
    ("```\nkind: review\n```\nkind: check", "check"),          # fenced block ignored
    ("```\nkind: review\n```", None),
    ("> kind: review", None),                                   # quoted text ignored
    ("kind:review", None),                                      # not exactly `kind: <word>`
    ("kind: review ", None),                                    # trailing text
    ("kind: banana", "banana"),                                 # unknown word retained
    ("kind: " + "a" * 33, None),                                # bounded
    ("kind: Review", None),
    ("", None),
    (None, None),
])
def test_kind_tag_parsing(text, expected):
    assert classify_mod.kind_tag(text) == expected


def test_registry_is_read_again_and_unknown_tags_remain_bounded(tmp_path):
    path = tmp_path / "kinds.json"
    first = classify_mod.load_kind_registry(path)
    unknown = classify_mod.classify(tag=classify_mod.kind_tag("kind: banana\nprivate prompt"), registry=first)
    assert (unknown["kind"], unknown["rule"], unknown["rawKind"]) == ("other", "tag", "banana")
    path.write_text(json.dumps([{"kind": "banana", "meaning": "Fruit work"}]), encoding="utf-8")
    second = classify_mod.load_kind_registry(path)
    known = classify_mod.classify(tag="banana", registry=second)
    assert known["kind"] == "banana"
    assert "rawKind" not in known
    assert "build" not in second
    assert classify_mod.classify(tag="a" * 33, registry=second)["rule"] == "unclassified"


def test_registry_invalid_entries_and_invalid_json_fail_closed(tmp_path):
    path = tmp_path / "kinds.json"
    path.write_text(json.dumps([{"kind": "other", "meaning": "reserved"},
                                {"kind": "UPPER", "meaning": "invalid"},
                                {"kind": "valid", "meaning": ""}, None]), encoding="utf-8")
    assert classify_mod.load_kind_registry(path) == {}
    path.write_text("{", encoding="utf-8")
    assert classify_mod.load_kind_registry(path) == {}


def test_registry_changes_take_effect_on_next_ingest(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    registry = tmp_path / "kinds.json"
    ledger = Ledger(data, claude_root=claude, codex_root=codex,
                    kind_registry_path=registry, clock=clock_at(3600))
    path = claude_file(claude, "registry")
    write_jsonl(path, [user(5, "kind: banana"), claude_response(10, "m1", output=5)])
    ledger.ingest(ALL)
    assert rows(ledger)[("Claude", "other")]["outputTotal"] == 5
    assert ledger.coverage()["unknownKinds"]["banana"] == 1
    entries = json.loads(registry.read_text(encoding="utf-8"))
    entries.append({"kind": "banana", "meaning": "Fruit work"})
    registry.write_text(json.dumps(entries), encoding="utf-8")
    from test_ledger_support import append_jsonl
    append_jsonl(path, [user(15, "kind: banana"), claude_response(20, "m2", output=9)])
    ledger.ingest(ALL)
    assert rows(ledger)[("Claude", "banana")]["outputTotal"] == 9
    assert ledger.coverage()["unknownKinds"]["banana"] == 1


# ----------------------------------------------------------------- segments in a thread

def test_thread_build_review_build_attributes_usage_to_each_segment(tmp_path):
    ledger, claude = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [
        user(5, "kind: build\nsecret planning words"), claude_response(10, "m1", output=10),
        user(15, "kind: review"), claude_response(20, "m2", output=20),
        user(25, "kind: build"), claude_response(30, "m3", output=40),
    ])
    ledger.ingest(ALL)
    by_kind = rows(ledger)
    assert by_kind[("Claude", "build")]["outputTotal"] == 50
    assert by_kind[("Claude", "build")]["classification"] == {"status": "inferred", "source": "tag"}
    assert by_kind[("Claude", "review")]["outputTotal"] == 20
    assert by_kind[("Claude", "review")]["classification"]["source"] == "tag"
    assert ("Claude", "lead") not in by_kind


def test_tags_in_code_blocks_and_quotes_are_ignored_unknown_is_other(tmp_path):
    ledger, claude = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [
        user(5, "Example:\n```\nkind: review\n```\n> kind: check"), claude_response(10, "m1", output=5),
        user(15, "kind: review "), claude_response(20, "m2", output=6),
        user(25, "kind: banana"), claude_response(30, "m3", output=7),
    ])
    ledger.ingest(ALL)
    by_kind = rows(ledger)
    assert set(by_kind) == {("Claude", "lead"), ("Claude", "other")}
    assert by_kind[("Claude", "lead")]["outputTotal"] == 11
    assert by_kind[("Claude", "other")]["outputTotal"] == 7
    assert by_kind[("Claude", "other")]["classification"]["source"] == "tag"


def test_stored_database_holds_kinds_not_message_text(tmp_path):
    ledger, claude = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [
        user(5, "kind: build\nsecret planning words"), claude_response(10, "m1", output=10)])
    ledger.ingest(ALL)
    ledger.close()
    data = Path(ledger.db_path).parent
    blob = b"".join(path.read_bytes() for path in data.iterdir() if path.is_file())
    assert b"secret planning words" not in blob
    assert b"kind: build" not in blob


def test_segment_carries_into_resumed_codex_file(tmp_path):
    """The active kind is per thread: a resumed file of the same thread keeps the segment."""
    ledger, _ = open_ledger(tmp_path)
    codex_root = tmp_path / "codex"
    last_a = {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}
    write_jsonl(codex_file(codex_root, "T5-a"),
                codex_meta("T5", 10, prompt="kind: review")
                + [codex_count(11, input_tokens=80, output_tokens=20, total=100, last=last_a)])
    # Resumed file: no tag in it, and the counters continue the same thread.
    write_jsonl(codex_file(codex_root, "T5-b"),
                codex_meta("T5", 100)
                + [codex_count(101, input_tokens=120, output_tokens=40, total=160,
                               last={"input_tokens": 15, "output_tokens": 5, "total_tokens": 20})])
    ledger.ingest(ALL)
    by_kind = rows(ledger)
    assert ("Codex", "lead") not in by_kind
    review = by_kind[("Codex", "review")]
    assert review["outputTotal"] == 40
    assert review["totalTokens"] == 160
    assert review["classification"] == {"status": "inferred", "source": "tag"}
