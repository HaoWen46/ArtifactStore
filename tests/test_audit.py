"""Access audit log: every read attempt — allowed or denied — must write a
row. The audit log is the RQ4 measurement surface (PLAN §11.3)."""
from __future__ import annotations

from pathlib import Path

from artifactstore import ArtifactStore
from artifactstore.grants import log_access


def test_log_access_writes_row(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    log_access(
        store.conn,
        grant_id="__supervisor__",
        subject_agent_id="supervisor",
        artifact_id=None,
        operation="search",
        view=None,
        result_token_count=0,
        allowed=True,
        denial_reason=None,
    )
    rows = store.conn.execute(
        "SELECT operation, allowed, denial_reason FROM artifact_access_log"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["operation"] == "search"
    assert rows[0]["allowed"] in (1, True)


def test_log_access_records_denial(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    log_access(
        store.conn,
        grant_id="grant_x",
        subject_agent_id="worker",
        artifact_id="art_12345678",
        operation="expand_view",
        view="raw",
        result_token_count=0,
        allowed=False,
        denial_reason="view 'raw' not in allowed_views",
    )
    row = store.conn.execute(
        "SELECT denial_reason, allowed FROM artifact_access_log"
    ).fetchone()
    assert row["allowed"] in (0, False)
    assert "raw" in row["denial_reason"]
