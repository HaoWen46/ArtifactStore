"""Public API surface (PLAN §9)."""
from __future__ import annotations

from pathlib import Path

import pytest

from artifactstore import ArtifactStore
from artifactstore.grants import AccessDenied


FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore.init(tmp_path / "store.db")


def _put_pytest(store: ArtifactStore) -> str:
    raw = (FIXTURES / "pytest_auth_expiry.log").read_text()
    return store.put_artifact(
        tool_name="pytest", artifact_type="pytest_failure",
        raw_text=raw, creator_agent_id="t", session_id="s",
    )


def test_store_init_opens_db(tmp_path: Path):
    store = _store(tmp_path)
    assert store.conn is not None


def test_put_artifact_round_trip(tmp_path: Path):
    store = _store(tmp_path)
    aid = _put_pytest(store)
    assert aid.startswith("art_")
    row = store.conn.execute(
        "SELECT artifact_type, raw_hash, preview, raw_blob, token_count "
        "FROM artifacts WHERE artifact_id = ?", (aid,)
    ).fetchone()
    assert row["artifact_type"] == "pytest_failure"
    assert len(row["raw_hash"]) == 64           # sha256 hex
    assert row["preview"].startswith("[pytest_failure")
    assert row["raw_blob"].startswith("=========")  # original log header
    assert row["token_count"] > 0


def test_put_artifact_creates_spans_and_fts(tmp_path: Path):
    store = _store(tmp_path)
    aid = _put_pytest(store)
    n_spans = store.conn.execute(
        "SELECT COUNT(*) FROM artifact_spans WHERE artifact_id = ?", (aid,)
    ).fetchone()[0]
    assert n_spans > 0, "extractor produced no spans"
    fts = store.conn.execute(
        "SELECT artifact_type FROM artifact_fts WHERE artifact_id = ?", (aid,)
    ).fetchone()
    assert fts is not None
    assert fts["artifact_type"] == "pytest_failure"


def test_search_via_supervisor_grant(tmp_path: Path):
    store = _store(tmp_path)
    _put_pytest(store)
    out = store.search("expired", grant_id="__supervisor__",
                       token_budget=2000)
    assert out, "expected at least one hit on 'expired'"
    assert all(r["preview"].startswith("[pytest_failure") for r in out)


def test_get_spans_filters_by_type(tmp_path: Path):
    store = _store(tmp_path)
    aid = _put_pytest(store)
    spans = store.get_spans(aid, grant_id="__supervisor__",
                            span_types=["assertion"], token_budget=2000)
    assert spans
    assert all(s["type"] == "assertion" for s in spans)


def test_expand_view_enforces_budget(tmp_path: Path):
    store = _store(tmp_path)
    aid = _put_pytest(store)
    big = store.expand_view(aid, grant_id="__supervisor__",
                            view="raw", token_budget=2000)
    tight = store.expand_view(aid, grant_id="__supervisor__",
                              view="raw", token_budget=20)
    assert len(tight) < len(big), "tight budget did not truncate"


def test_create_grant_round_trip(tmp_path: Path):
    store = _store(tmp_path)
    aid = _put_pytest(store)
    gid = store.create_grant(
        subject_agent_id="worker", issuer_agent_id="supervisor",
        artifact_predicate={"session_id": "s",
                            "artifact_types": ["pytest_failure"]},
        allowed_ops=["search", "get_spans", "expand_view"],
        allowed_views=["preview", "evidence"],
        max_tokens=2000, ttl_seconds=600,
    )
    assert gid.startswith("grant_")
    spans = store.get_spans(aid, grant_id=gid, token_budget=2000)
    assert spans


def test_grant_blocks_disallowed_view(tmp_path: Path):
    store = _store(tmp_path)
    aid = _put_pytest(store)
    gid = store.create_grant(
        subject_agent_id="worker", issuer_agent_id="supervisor",
        artifact_predicate={"session_id": "s"},
        allowed_ops=["expand_view"],
        allowed_views=["preview", "evidence"],
        max_tokens=2000, ttl_seconds=600,
    )
    # preview ok
    out = store.expand_view(aid, grant_id=gid, view="preview",
                            token_budget=500)
    assert out
    # raw denied
    with pytest.raises(AccessDenied):
        store.expand_view(aid, grant_id=gid, view="raw", token_budget=500)
    # And the denial was logged.
    rows = store.audit(gid)
    assert any(r["allowed"] in (0, False) and r["view"] == "raw" for r in rows)


def test_grant_blocks_wrong_predicate(tmp_path: Path):
    store = _store(tmp_path)
    aid = _put_pytest(store)
    gid = store.create_grant(
        subject_agent_id="worker", issuer_agent_id="supervisor",
        artifact_predicate={"session_id": "different"},
        allowed_ops=["expand_view"], allowed_views=["preview"],
        max_tokens=2000, ttl_seconds=600,
    )
    with pytest.raises(AccessDenied):
        store.expand_view(aid, grant_id=gid, view="preview", token_budget=500)


def test_audit_returns_rows(tmp_path: Path):
    store = _store(tmp_path)
    _put_pytest(store)
    store.search("anything", grant_id="__supervisor__")
    rows = store.audit("__supervisor__")
    assert any(r["operation"] == "search" for r in rows)
