"""Grant predicate semantics. These cases exercise the locked behavior in
CLAUDE.md 'Design choices' and grants.py docstring."""
from __future__ import annotations

import pytest

from artifactstore.grants import (
    SENSITIVITY,
    DEFAULT_SENSITIVITY,
    predicate_matches,
    span_passes_path_prefix,
)


# --- predicate_matches ---

def _art(**kw) -> dict:
    base = {
        "session_id": "sess_demo",
        "artifact_type": "pytest_failure",
        "sensitivity_label": "internal",
    }
    base.update(kw)
    return base


def test_empty_predicate_allows_anything():
    assert predicate_matches({}, _art()) is True


def test_session_id_must_match():
    assert predicate_matches({"session_id": "sess_demo"}, _art()) is True
    assert predicate_matches({"session_id": "other"}, _art()) is False


def test_artifact_types_filter():
    p = {"artifact_types": ["pytest_failure", "git_diff"]}
    assert predicate_matches(p, _art(artifact_type="pytest_failure")) is True
    assert predicate_matches(p, _art(artifact_type="grep_result")) is False


def test_sensitivity_max_caps_label():
    p = {"sensitivity_max": "internal"}
    assert predicate_matches(p, _art(sensitivity_label="public")) is True
    assert predicate_matches(p, _art(sensitivity_label="internal")) is True
    assert predicate_matches(p, _art(sensitivity_label="restricted")) is False
    assert predicate_matches(p, _art(sensitivity_label="secret")) is False


def test_default_sensitivity_is_internal():
    """Artifacts without explicit label fall back to DEFAULT_SENSITIVITY."""
    a = _art()
    a["sensitivity_label"] = None
    assert predicate_matches({"sensitivity_max": "internal"}, a) is True
    assert predicate_matches({"sensitivity_max": "public"}, a) is False
    assert DEFAULT_SENSITIVITY == "internal"


def test_sensitivity_ordering():
    assert SENSITIVITY["public"] < SENSITIVITY["internal"]
    assert SENSITIVITY["internal"] < SENSITIVITY["restricted"]
    assert SENSITIVITY["restricted"] < SENSITIVITY["secret"]


# --- span_passes_path_prefix ---

def test_no_prefix_means_no_constraint():
    assert span_passes_path_prefix({}, "anything/at/all.py") is True
    assert span_passes_path_prefix({"path_prefixes": []}, "anything") is True


def test_prefix_match():
    p = {"path_prefixes": ["app/auth", "tests/auth"]}
    assert span_passes_path_prefix(p, "app/auth/tokens.py") is True
    assert span_passes_path_prefix(p, "tests/auth/test_x.py") is True
    assert span_passes_path_prefix(p, "app/billing/invoices.py") is False


def test_null_path_is_opaque_passes():
    """Spans with no file_path are path-opaque and always pass — see
    grants.py docstring."""
    p = {"path_prefixes": ["app/auth"]}
    assert span_passes_path_prefix(p, None) is True


def test_check_allows_supervisor_grant(tmp_path):
    """The seeded __supervisor__ grant must allow every op/view, log success,
    and not raise."""
    from artifactstore import ArtifactStore
    from artifactstore.grants import check
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = store.put_artifact(
        tool_name="x", artifact_type="pytest_failure", raw_text="hi",
        creator_agent_id="t", session_id="s",
    )
    g = check(store.conn, "__supervisor__", aid, "expand_view", "raw")
    assert g["grant_id"] == "__supervisor__"


def test_check_denies_unknown_grant(tmp_path):
    from artifactstore import ArtifactStore
    from artifactstore.grants import AccessDenied, check
    store = ArtifactStore.init(tmp_path / "store.db")
    with pytest.raises(AccessDenied):
        check(store.conn, "grant_does_not_exist", None, "search", None)
    # Failed lookup still writes an audit row (RQ4).
    rows = store.conn.execute(
        "SELECT allowed, denial_reason FROM artifact_access_log"
    ).fetchall()
    assert rows
    assert rows[0]["allowed"] in (0, False)
    assert "unknown grant" in rows[0]["denial_reason"]


def test_check_denies_disallowed_op(tmp_path):
    from artifactstore import ArtifactStore
    from artifactstore.grants import AccessDenied, check
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = store.put_artifact(
        tool_name="x", artifact_type="pytest_failure", raw_text="hi",
        creator_agent_id="t", session_id="s",
    )
    gid = store.create_grant(
        subject_agent_id="w", issuer_agent_id="s",
        artifact_predicate={"session_id": "s"},
        allowed_ops=["search"], allowed_views=["preview"],
        max_tokens=100, ttl_seconds=600,
    )
    with pytest.raises(AccessDenied, match="op 'expand_view'"):
        check(store.conn, gid, aid, "expand_view", "preview")


def test_check_denies_expired_grant(tmp_path):
    from artifactstore import ArtifactStore
    from artifactstore.grants import AccessDenied, check
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = store.put_artifact(
        tool_name="x", artifact_type="pytest_failure", raw_text="hi",
        creator_agent_id="t", session_id="s",
    )
    gid = store.create_grant(
        subject_agent_id="w", issuer_agent_id="s",
        artifact_predicate={"session_id": "s"},
        allowed_ops=["expand_view"], allowed_views=["preview"],
        max_tokens=100, ttl_seconds=-1,  # already expired
    )
    with pytest.raises(AccessDenied, match="expired"):
        check(store.conn, gid, aid, "expand_view", "preview")
