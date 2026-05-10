"""Regression tests for the four critical issues found in the review.

Each test reproduces the original bug, then asserts the fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from artifactstore import ArtifactStore
from artifactstore.cite import BadCitation, CITATION_RE, parse, verify_resolves
from artifactstore.grants import (
    AccessDenied,
    SENSITIVITY,
    effective_sensitivity,
    infer_sensitivity,
)


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore.init(tmp_path / "store.db")


# ---------------------------------------------------------------------------
# W1 — long-line truncation no longer returns empty for sub-line budgets.
# Original bug: a 5000-char single-line artifact returned 0 chars at any
# budget below the full line cost. Fix: char-level fallback when the first
# line exceeds the budget.
# ---------------------------------------------------------------------------

def test_w1_long_single_line_does_not_return_empty(tmp_path: Path):
    store = _store(tmp_path)
    big = "x" * 5000  # one line, no newlines
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text=big,
        creator_agent_id="t", session_id="s",
    )
    for budget in (100, 500, 1000):
        out = store.expand_view(aid, grant_id="__supervisor__",
                                  view="raw", token_budget=budget)
        assert len(out) > 0, \
            f"budget={budget} returned empty — long-line fallback regressed"
        assert "truncated" in out, \
            f"budget={budget} did not include truncation marker: {out[:80]!r}"


def test_w1_short_single_line_unaffected(tmp_path: Path):
    """Sub-budget single line content fits without char-truncation."""
    store = _store(tmp_path)
    short = "small content"
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text=short,
        creator_agent_id="t", session_id="s",
    )
    out = store.expand_view(aid, grant_id="__supervisor__", view="raw",
                              token_budget=1000)
    assert out == short
    assert "truncated" not in out


def test_w1_multiline_uses_line_truncation_first(tmp_path: Path):
    """Normal multiline content takes whole lines; only single-line
    fallback uses char truncation."""
    store = _store(tmp_path)
    raw = "\n".join(f"line {i}" for i in range(20))
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text=raw,
        creator_agent_id="t", session_id="s",
    )
    out = store.expand_view(aid, grant_id="__supervisor__", view="raw",
                              token_budget=20)
    # Lines fit one at a time; we get *some* whole lines, no marker.
    assert "truncated, " not in out
    assert out.startswith("line 0")


# ---------------------------------------------------------------------------
# W2 — producer cannot bypass sensitivity_max by self-labeling 'public'.
# Original bug: `put_artifact(sensitivity_label='public')` won regardless
# of content. Fix: heuristic detection + `effective_sensitivity` bumps up.
# ---------------------------------------------------------------------------

def test_w2_jwt_in_content_bumps_label_up(tmp_path: Path):
    store = _store(tmp_path)
    raw = "log: token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturepart"
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text=raw,
        creator_agent_id="attacker", session_id="s",
        sensitivity_label="public",  # liar
    )
    row = store.conn.execute(
        "SELECT sensitivity_label FROM artifacts WHERE artifact_id = ?",
        (aid,),
    ).fetchone()
    assert row["sensitivity_label"] == "restricted", \
        "JWT-bearing artifact should have been bumped to restricted"


def test_w2_password_pattern_bumps_label_up(tmp_path: Path):
    store = _store(tmp_path)
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic",
        raw_text="config: password=hunter2",
        creator_agent_id="attacker", session_id="s",
        sensitivity_label="public",
    )
    row = store.conn.execute(
        "SELECT sensitivity_label FROM artifacts WHERE artifact_id = ?",
        (aid,),
    ).fetchone()
    assert row["sensitivity_label"] == "restricted"


def test_w2_aws_key_bumps_label_up(tmp_path: Path):
    store = _store(tmp_path)
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic",
        raw_text="env: AKIA1234567890ABCDEF something",
        creator_agent_id="attacker", session_id="s",
        sensitivity_label="public",
    )
    row = store.conn.execute(
        "SELECT sensitivity_label FROM artifacts WHERE artifact_id = ?",
        (aid,),
    ).fetchone()
    assert row["sensitivity_label"] == "restricted"


def test_w2_clean_content_keeps_caller_label(tmp_path: Path):
    """Producer-supplied label is honored when content is clean."""
    store = _store(tmp_path)
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic",
        raw_text="just some normal log lines\nno secrets here",
        creator_agent_id="t", session_id="s",
        sensitivity_label="public",
    )
    row = store.conn.execute(
        "SELECT sensitivity_label FROM artifacts WHERE artifact_id = ?",
        (aid,),
    ).fetchone()
    assert row["sensitivity_label"] == "public"


def test_w2_caller_label_higher_than_inferred_wins(tmp_path: Path):
    """If the caller declares 'secret' but content has no patterns, label
    stays 'secret' (caller is the lower bound, not an upper cap)."""
    store = _store(tmp_path)
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text="boring log",
        creator_agent_id="t", session_id="s", sensitivity_label="secret",
    )
    row = store.conn.execute(
        "SELECT sensitivity_label FROM artifacts WHERE artifact_id = ?",
        (aid,),
    ).fetchone()
    assert row["sensitivity_label"] == "secret"


def test_w2_self_labeled_public_jwt_now_blocks_under_public_grant(tmp_path: Path):
    """End-to-end: the original W2 reproducer, now denied."""
    store = _store(tmp_path)
    sneaky_aid = store.put_artifact(
        tool_name="attacker", artifact_type="generic",
        raw_text="SECRET: api_key=sk-prod-leak-12345",
        creator_agent_id="attacker", session_id="s",
        sensitivity_label="public",  # liar
    )
    # Public-only grant. Pre-fix: the read went through. Post-fix: denied.
    gid = store.create_grant(
        subject_agent_id="w", issuer_agent_id="s",
        artifact_predicate={"session_id": "s", "sensitivity_max": "public"},
        allowed_ops=["expand_view"], allowed_views=["raw"],
        max_tokens=10000, ttl_seconds=600,
    )
    with pytest.raises(AccessDenied, match="predicate"):
        store.expand_view(sneaky_aid, grant_id=gid, view="raw",
                            token_budget=500)


def test_w2_infer_sensitivity_unit():
    # Clean content → 'public' (no minimum requirement above public).
    assert infer_sensitivity("hello world") == "public"
    # JWT shape → 'restricted'.
    assert infer_sensitivity("eyJabc.eyJdef.signature") == "restricted"
    # password=val → 'restricted'.
    assert infer_sensitivity("password: hunter2") == "restricted"
    # api_key=val → 'restricted'.
    assert infer_sensitivity("api_key=sk-1234567890abcdef1234567890") == "restricted"
    # Lowercase 'akia' shouldn't match the AWS pattern (must be uppercase).
    assert infer_sensitivity("akiaxxxx") == "public"


def test_w2_effective_sensitivity_unit():
    # caller higher than inferred → caller wins
    assert effective_sensitivity("secret", "boring") == "secret"
    # caller lower than inferred → inferred wins
    assert effective_sensitivity("public", "password=x") == "restricted"
    # equal → caller wins (no change)
    assert effective_sensitivity("internal", "boring") == "internal"


# ---------------------------------------------------------------------------
# W3 — first overrun read no longer reads past the grant budget.
# Original bug: `max_tokens=50` grant let a single read pull thousands of
# tokens before the next call got denied. Fix: clamp `token_budget` by
# `max_tokens - consumed_tokens` on every read.
# ---------------------------------------------------------------------------

def test_w3_first_read_clamped_to_remaining_budget(tmp_path: Path):
    store = _store(tmp_path)
    # Many-line content so per-line costs are small enough that omit-fits
    # would happily pull thousands of tokens if uncapped.
    raw = "\n".join(f"line {i} {'x' * 40}" for i in range(500))
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text=raw,
        creator_agent_id="t", session_id="s",
    )
    gid = store.create_grant(
        subject_agent_id="w", issuer_agent_id="s",
        artifact_predicate={"session_id": "s"},
        allowed_ops=["expand_view"], allowed_views=["raw"],
        max_tokens=50,  # tiny budget
        ttl_seconds=600,
    )
    out = store.expand_view(aid, grant_id=gid, view="raw",
                              token_budget=10000)
    # Pre-fix: out was ~6000 tokens. Post-fix: bounded by max_tokens=50.
    from artifactstore.tokens import estimate
    out_tokens = estimate(out)
    assert out_tokens <= 60, \
        f"first read returned {out_tokens} tokens; should be ≤ ~50 + slack"


def test_w3_budget_eventually_denies(tmp_path: Path):
    """Once cumulative consumption reaches max_tokens, further reads are
    denied. With the W3 clamp, the first read no longer over-runs — so
    exhaustion may take more than one read for content larger than the
    cap. The eventual denial behavior is what matters.
    """
    store = _store(tmp_path)
    raw = "\n".join(f"line {i}" for i in range(50))
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text=raw,
        creator_agent_id="t", session_id="s",
    )
    gid = store.create_grant(
        subject_agent_id="w", issuer_agent_id="s",
        artifact_predicate={"session_id": "s"},
        allowed_ops=["expand_view"], allowed_views=["raw"],
        max_tokens=20, ttl_seconds=600,
    )
    denied = False
    for _ in range(8):
        try:
            store.expand_view(aid, grant_id=gid, view="raw",
                                token_budget=200)
        except AccessDenied as e:
            assert "budget exhausted" in str(e)
            denied = True
            break
    assert denied, "budget never exhausted in 8 reads — clamp over-restricting"


def test_w3_unlimited_grant_still_serves_full_request(tmp_path: Path):
    """The synthetic __supervisor__ grant has max_tokens=NULL (unlimited)
    and must NOT be clamped. Verifies the W19 fix at the same time:
    consumed_tokens never accrues for unlimited grants."""
    store = _store(tmp_path)
    raw = "\n".join(f"line {i}" for i in range(20))
    aid = store.put_artifact(
        tool_name="x", artifact_type="generic", raw_text=raw,
        creator_agent_id="t", session_id="s",
    )
    out = store.expand_view(aid, grant_id="__supervisor__", view="raw",
                              token_budget=10000)
    assert "line 19" in out, "supervisor should see all 20 lines"
    consumed = store.conn.execute(
        "SELECT consumed_tokens FROM artifact_grants "
        "WHERE grant_id = '__supervisor__'"
    ).fetchone()[0]
    assert (consumed or 0) == 0, \
        "unlimited grant should not accumulate consumed_tokens (W19)"


# ---------------------------------------------------------------------------
# W4 — citation regex now accepts uppercase hex, normalizes on parse.
# ---------------------------------------------------------------------------

def test_w4_uppercase_citation_parses(tmp_path: Path):
    art_id, span_id = parse("art_DEADBEEF/span_CAFE1234")
    assert art_id == "art_deadbeef"
    assert span_id == "span_cafe1234"


def test_w4_mixed_case_citation_parses():
    art_id, span_id = parse("art_DeadBEEF/span_CafE1234")
    # Always normalized to lowercase.
    assert art_id == "art_deadbeef"
    assert span_id == "span_cafe1234"


def test_w4_uppercase_citation_resolves_in_db(tmp_path: Path):
    store = _store(tmp_path)
    aid = store.put_artifact(
        tool_name="x", artifact_type="pytest_failure",
        raw_text="boom", creator_agent_id="t", session_id="s",
    )
    # Get a real span_id (lowercase from token_hex).
    span_id = store.conn.execute(
        "SELECT span_id FROM artifact_spans WHERE artifact_id = ? LIMIT 1",
        (aid,),
    ).fetchone()
    if span_id is None:
        pytest.skip("no spans extracted from 'boom'; need richer fixture")
    span_id = span_id["span_id"]
    # Verify uppercase version of the citation resolves.
    upper = f"{aid}/{span_id}".upper()
    assert verify_resolves(store.conn, upper) is True, \
        "uppercase citation did not resolve after W4 fix"


def test_w4_extract_citations_normalizes_uppercase():
    """Eval metrics' extract_citations should also normalize."""
    from eval.metrics import extract_citations
    text = "see art_DEADBEEF/span_CAFE1234 for details"
    cites = extract_citations(text)
    assert cites == ["art_deadbeef/span_cafe1234"], \
        f"extract_citations did not normalize: {cites}"


def test_w4_regex_still_rejects_invalid():
    with pytest.raises(BadCitation):
        parse("art_xx/span_yy")  # too short
    with pytest.raises(BadCitation):
        parse("art_zzzzzzzz/span_zzzzzzzz")  # not hex
    with pytest.raises(BadCitation):
        parse("xxx_12345678/yyy_12345678")  # wrong prefixes
