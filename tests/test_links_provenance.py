"""Provenance + find_related coverage. The demo doesn't exercise links yet
but the schema and store API support them, and RQ2 (citation correctness)
extensions will lean on links between pytest_failure and git_diff artifacts.
"""
from __future__ import annotations

import json
from pathlib import Path

from artifactstore import ArtifactStore

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def test_find_related_under_supervisor(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    pytest_id = store.put_artifact(
        tool_name="pytest", artifact_type="pytest_failure",
        raw_text=(FIXTURES / "pytest_auth_expiry.log").read_text(),
        creator_agent_id="t", session_id="s",
    )
    diff_id = store.put_artifact(
        tool_name="git", artifact_type="git_diff",
        raw_text=(FIXTURES / "git_diff_auth_refactor.diff").read_text(),
        creator_agent_id="t", session_id="s",
    )
    # The fix in diff_id resolves the failure in pytest_id.
    store.conn.execute(
        "INSERT INTO artifact_links(src_artifact_id, dst_artifact_id, "
        "relation, confidence) VALUES (?, ?, ?, ?)",
        (pytest_id, diff_id, "caused_by", 0.9),
    )

    rels = store.find_related(pytest_id, grant_id="__supervisor__")
    assert any(r["dst_artifact_id"] == diff_id and r["relation"] == "caused_by"
               for r in rels)


def test_provenance_view_includes_links(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    aid = store.put_artifact(
        tool_name="pytest", artifact_type="pytest_failure",
        raw_text=(FIXTURES / "pytest_auth_expiry.log").read_text(),
        creator_agent_id="t", session_id="s",
        metadata={"target": "auth_expiry", "live": False},
    )
    other = store.put_artifact(
        tool_name="git", artifact_type="git_diff",
        raw_text=(FIXTURES / "git_diff_auth_refactor.diff").read_text(),
        creator_agent_id="t", session_id="s",
    )
    store.conn.execute(
        "INSERT INTO artifact_links(src_artifact_id, dst_artifact_id, "
        "relation, confidence) VALUES (?, ?, ?, ?)",
        (aid, other, "caused_by", 1.0),
    )

    out = store.expand_view(aid, grant_id="__supervisor__",
                            view="provenance", token_budget=2000)
    payload = json.loads(out)
    assert payload["artifact_id"] == aid
    assert payload["metadata"]["target"] == "auth_expiry"
    assert any(link["relation"] == "caused_by"
               and link["to"] == other for link in payload["links"])
