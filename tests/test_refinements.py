"""Tests for the three system refinements:

  1. Cumulative grant budget — `max_tokens` ticks down across reads,
     denials kick in when exhausted.
  2. Span-aware preview — preview body inlines top-importance spans
     when extractor produced any (not just head-of-raw lines).
  3. Auto-derived-from links — `put_artifact(parent_artifact_id=...)`
     writes a `derived_from` row to artifact_links automatically.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from artifactstore import ArtifactStore
from artifactstore.grants import AccessDenied

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def _seed_pytest(store: ArtifactStore, *, session: str = "s") -> str:
    return store.put_artifact(
        tool_name="pytest", artifact_type="pytest_failure",
        raw_text=(FIXTURES / "pytest_auth_expiry.log").read_text(),
        creator_agent_id="t", session_id=session,
    )


# ---------------------------------------------------------------------------
# Refinement 1: cumulative grant budget
# ---------------------------------------------------------------------------

def test_grant_budget_ticks_down_on_read(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = _seed_pytest(store)
    gid = store.create_grant(
        subject_agent_id="w", issuer_agent_id="s",
        artifact_predicate={"session_id": "s"},
        allowed_ops=["expand_view"], allowed_views=["preview", "evidence"],
        max_tokens=10_000, ttl_seconds=600,
    )
    store.expand_view(aid, grant_id=gid, view="preview", token_budget=200)
    row = store.conn.execute(
        "SELECT consumed_tokens FROM artifact_grants WHERE grant_id = ?",
        (gid,),
    ).fetchone()
    assert row["consumed_tokens"] > 0, "grant budget should have decremented"


def test_grant_budget_denies_when_exhausted(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = _seed_pytest(store)
    # Give a tiny budget that the first read will blow through.
    gid = store.create_grant(
        subject_agent_id="w", issuer_agent_id="s",
        artifact_predicate={"session_id": "s"},
        allowed_ops=["expand_view"], allowed_views=["preview", "evidence"],
        max_tokens=10, ttl_seconds=600,  # 10 tokens — first read exhausts it
    )
    # First read succeeds (we check budget BEFORE reading).
    store.expand_view(aid, grant_id=gid, view="preview", token_budget=500)
    # Second read should be denied — we're past the cap.
    with pytest.raises(AccessDenied, match="grant budget exhausted"):
        store.expand_view(aid, grant_id=gid, view="evidence", token_budget=500)
    # Audit log records the denial with a useful reason.
    rows = store.audit(gid)
    assert any(r["allowed"] in (0, False)
               and "budget exhausted" in (r["denial_reason"] or "")
               for r in rows)


def test_supervisor_grant_has_unlimited_budget(tmp_path: Path):
    """The seeded __supervisor__ grant has max_tokens=NULL — it must never
    trip the budget check, no matter how much we read."""
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = _seed_pytest(store)
    for _ in range(5):
        store.expand_view(aid, grant_id="__supervisor__", view="raw",
                          token_budget=2000)
    # Still works — no AccessDenied.
    out = store.expand_view(aid, grant_id="__supervisor__", view="evidence",
                            token_budget=2000)
    assert out


def test_grant_budget_separate_per_grant(tmp_path: Path):
    """Two grants minted from one supervisor must accrue consumption
    independently — exhausting grant_a does not affect grant_b."""
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = _seed_pytest(store)
    pred = {"session_id": "s"}
    ops = ["expand_view"]
    views = ["preview", "evidence"]
    a = store.create_grant(subject_agent_id="a", issuer_agent_id="sup",
                            artifact_predicate=pred, allowed_ops=ops,
                            allowed_views=views,
                            max_tokens=10, ttl_seconds=600)
    b = store.create_grant(subject_agent_id="b", issuer_agent_id="sup",
                            artifact_predicate=pred, allowed_ops=ops,
                            allowed_views=views,
                            max_tokens=10_000, ttl_seconds=600)
    store.expand_view(aid, grant_id=a, view="preview", token_budget=500)
    # a should exhaust quickly; b stays fat.
    with pytest.raises(AccessDenied):
        store.expand_view(aid, grant_id=a, view="preview", token_budget=500)
    # b still allows reads.
    store.expand_view(aid, grant_id=b, view="preview", token_budget=500)


# ---------------------------------------------------------------------------
# Refinement 2: span-aware preview
# ---------------------------------------------------------------------------

def test_preview_inlines_top_importance_spans(tmp_path: Path):
    """The pytest_failure extractor produces an `assertion` span (importance
    0.95) for the failing assertion line. After this refinement, the
    artifact's preview body must contain that span, not just the
    test-collection header."""
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = _seed_pytest(store)
    row = store.conn.execute(
        "SELECT preview FROM artifacts WHERE artifact_id = ?", (aid,)
    ).fetchone()
    preview = row["preview"]
    # Header is unchanged: still the [type | summary | tokens] line.
    assert preview.startswith("[pytest_failure")
    # Body: top-importance spans inline with span_type@loc prefix.
    assert "assertion@" in preview, \
        "preview did not inline the assertion span"
    assert "token expired prematurely" in preview, \
        "preview did not include the assertion text"


def test_preview_falls_back_when_no_spans(tmp_path: Path):
    """If extractor produces no spans (unknown artifact_type), the body
    falls back to head-of-raw truncation. Must not crash."""
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic_type_no_extractor",
        raw_text="line one\nline two\nline three\n",
        creator_agent_id="t", session_id="s",
    )
    row = store.conn.execute(
        "SELECT preview FROM artifacts WHERE artifact_id = ?", (aid,)
    ).fetchone()
    preview = row["preview"]
    assert preview.startswith("[generic_type_no_extractor")
    assert "line one" in preview  # head-of-raw fallback


def test_preview_respects_token_cap(tmp_path: Path):
    """Even with many high-importance spans, preview stays under
    PREVIEW_TOKEN_BUDGET. Demonstrates omit-fits enforcement."""
    from artifactstore.previews import PREVIEW_TOKEN_BUDGET
    from artifactstore.tokens import estimate
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = _seed_pytest(store)
    row = store.conn.execute(
        "SELECT preview FROM artifacts WHERE artifact_id = ?", (aid,)
    ).fetchone()
    assert estimate(row["preview"]) <= PREVIEW_TOKEN_BUDGET + 5  # +slack


# ---------------------------------------------------------------------------
# Refinement 3: auto-derived-from links
# ---------------------------------------------------------------------------

def test_parent_artifact_id_writes_derived_from_link(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    parent = _seed_pytest(store)
    child = store.put_artifact(
        tool_name="git", artifact_type="git_diff",
        raw_text=(FIXTURES / "git_diff_auth_refactor.diff").read_text(),
        creator_agent_id="t", session_id="s",
        parent_artifact_id=parent,
    )
    rels = store.find_related(child, grant_id="__supervisor__")
    assert any(r["dst_artifact_id"] == parent
               and r["relation"] == "derived_from" for r in rels)


def test_no_parent_means_no_link(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = _seed_pytest(store)
    n = store.conn.execute(
        "SELECT COUNT(*) FROM artifact_links WHERE src_artifact_id = ?",
        (aid,),
    ).fetchone()[0]
    assert n == 0


def test_derived_from_idempotent(tmp_path: Path):
    """The INSERT OR IGNORE means re-creating an identical link is a no-op,
    not an error. (Though `put_artifact` allocates a fresh artifact_id each
    time, so this is mostly defensive.)"""
    store = ArtifactStore.init(tmp_path / "store.db")
    parent = _seed_pytest(store)
    # Manually insert the same link twice; second should be ignored.
    child = store.put_artifact(
        tool_name="git", artifact_type="git_diff",
        raw_text="diff --git a/x b/x\n",
        creator_agent_id="t", session_id="s",
        parent_artifact_id=parent,
    )
    store.conn.execute(
        "INSERT OR IGNORE INTO artifact_links("
        "src_artifact_id, dst_artifact_id, relation, confidence) "
        "VALUES (?, ?, ?, ?)",
        (child, parent, "derived_from", 1.0),
    )
    n = store.conn.execute(
        "SELECT COUNT(*) FROM artifact_links "
        "WHERE src_artifact_id = ? AND dst_artifact_id = ? AND relation = ?",
        (child, parent, "derived_from"),
    ).fetchone()[0]
    assert n == 1
