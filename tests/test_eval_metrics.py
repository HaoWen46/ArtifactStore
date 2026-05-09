"""Eval metrics module — tests are pure (no model calls)."""
from __future__ import annotations

from pathlib import Path

from artifactstore import ArtifactStore
from eval.metrics import (
    citation_validity,
    evidence_recall,
    exact_evidence_recovery,
    extract_citations,
    task_success,
    blocked_reads,
)


def _gold(keywords, must=()):
    return {
        "ground_truth": {
            "diagnosis_keywords": list(keywords),
            "key_evidence": [{"must_contain": m} for m in must],
        }
    }


def test_evidence_recall_partial():
    g = _gold(["timezone", "utc", "naive"])
    assert evidence_recall("naive UTC timezone bug", g) == 1.0
    assert evidence_recall("uses naive datetime in UTC", g) == 2 / 3  # no 'timezone'
    assert evidence_recall("uses naive datetime", g) == 1 / 3
    assert evidence_recall("hello world", g) == 0.0


def test_evidence_recall_case_insensitive():
    g = _gold(["UTC"])
    assert evidence_recall("compares against utc", g) == 1.0


def test_task_success_threshold():
    g = _gold(["a", "b", "c", "d"])
    # 50% threshold: needs 2/4 keywords
    assert task_success("a b", g) is True
    assert task_success("a", g) is False


def test_task_success_rejects_max_turns_marker():
    g = _gold(["x"])
    text = "[agent=foo hit max_turns=10 without termination]"
    assert task_success(text, g) is False


def test_extract_citations():
    text = "see art_12345678/span_abcdef01 and art_aaaaaaaa/span_bbbbbbbb"
    assert extract_citations(text) == [
        "art_12345678/span_abcdef01",
        "art_aaaaaaaa/span_bbbbbbbb",
    ]
    assert extract_citations("none here") == []


def test_citation_validity_against_real_db(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    store.conn.execute(
        "INSERT INTO artifacts(artifact_id, session_id, artifact_type, raw_hash) "
        "VALUES (?, ?, ?, ?)",
        ("art_12345678", "s", "pytest_failure", "deadbeef"),
    )
    store.conn.execute(
        "INSERT INTO artifact_spans(span_id, artifact_id, span_type, text) "
        "VALUES (?, ?, ?, ?)",
        ("span_abcdef01", "art_12345678", "assertion", "boom"),
    )
    cits = ["art_12345678/span_abcdef01", "art_99999999/span_99999999"]
    resolved, valid = citation_validity(cits, store.conn)
    assert resolved == 1
    assert valid == 0.5


def test_citation_validity_empty():
    assert citation_validity([], None) == (0, 0.0)


def test_exact_evidence_recovery():
    gold = _gold([], must=["AssertionError", "token expired"])
    read = "found AssertionError: token expired prematurely in span"
    assert exact_evidence_recovery(read, gold) == 1.0
    assert exact_evidence_recovery("nothing", gold) == 0.0
    assert exact_evidence_recovery("AssertionError only", gold) == 0.5


def test_blocked_reads_counts_denials():
    rows = [
        {"allowed": 1, "operation": "search"},
        {"allowed": 0, "operation": "expand_view"},
        {"allowed": False, "operation": "expand_view"},
        {"allowed": True, "operation": "get_spans"},
    ]
    assert blocked_reads(rows) == 2
