"""DB layer: migration is idempotent, new_id is well-shaped, supervisor grant
is seeded by migrate()."""
from __future__ import annotations

import re
from pathlib import Path

from artifactstore import ArtifactStore
from artifactstore.db import new_id


def test_new_id_shape():
    nid = new_id("art")
    assert re.fullmatch(r"art_[0-9a-f]{8}", nid), nid


def test_migrate_is_idempotent(tmp_path: Path):
    db = tmp_path / "store.db"
    s1 = ArtifactStore.init(db)
    s1.conn.close()
    # Re-running migrate on the same file must not error.
    s2 = ArtifactStore.init(db)
    rows = s2.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "artifacts" in names


def test_supervisor_grant_seeded(tmp_path: Path):
    """The synthetic __supervisor__ grant must exist after migrate (CLAUDE.md
    'Design choices'). Otherwise audit-log FKs and supervisor citation
    verification break."""
    store = ArtifactStore.init(tmp_path / "store.db")
    row = store.conn.execute(
        "SELECT grant_id, allowed_views FROM artifact_grants "
        "WHERE grant_id = '__supervisor__'"
    ).fetchone()
    assert row is not None, "__supervisor__ grant was not seeded by migrate()"
    assert "raw" in row["allowed_views"]


# ---------------------------------------------------------------------------
# Schema-migration regression: backfilling columns on a pre-existing DB
# created from an older schema.
# ---------------------------------------------------------------------------

def test_migrate_backfills_consumed_tokens_on_old_db(tmp_path: Path):
    """Simulate a DB created before the cumulative-budget refinement.
    Migrate should ADD COLUMN consumed_tokens without erroring; subsequent
    code paths that reference it must work."""
    import sqlite3
    db = tmp_path / "old.db"
    # Build the OLD artifact_grants schema (without consumed_tokens) by hand.
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute(
        "CREATE TABLE artifact_grants ("
        "  grant_id TEXT PRIMARY KEY, subject_agent_id TEXT NOT NULL, "
        "  issuer_agent_id TEXT NOT NULL, artifact_predicate TEXT NOT NULL, "
        "  allowed_ops TEXT NOT NULL, allowed_views TEXT NOT NULL, "
        "  max_tokens INTEGER, expires_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO artifact_grants VALUES "
        "('g1', 's', 's', '{}', '[]', '[]', 1000, NULL)"
    )
    conn.close()

    # Open with the modern code — migrate() must add consumed_tokens.
    store = ArtifactStore.init(db)
    cols = {r[1] for r in store.conn.execute(
        "PRAGMA table_info(artifact_grants)").fetchall()}
    assert "consumed_tokens" in cols, "ALTER TABLE backfill did not run"

    # Existing row defaults to 0 (or NULL, which our code treats as 0).
    row = store.conn.execute(
        "SELECT consumed_tokens FROM artifact_grants WHERE grant_id = 'g1'"
    ).fetchone()
    assert (row["consumed_tokens"] or 0) == 0


def test_migrate_backfills_artifact_columns_on_old_db(tmp_path: Path):
    """Same idea for the artifacts table — raw_blob, metadata_json, and
    sensitivity_label were added after the initial schema."""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute(
        "CREATE TABLE artifacts ("
        "  artifact_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "  parent_artifact_id TEXT, creator_agent_id TEXT, tool_name TEXT, "
        "  artifact_type TEXT NOT NULL, raw_uri TEXT, raw_hash TEXT NOT NULL, "
        "  token_count INTEGER, preview TEXT, "
        "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.close()

    store = ArtifactStore.init(db)
    cols = {r[1] for r in store.conn.execute(
        "PRAGMA table_info(artifacts)").fetchall()}
    for required in ("raw_blob", "metadata_json", "sensitivity_label"):
        assert required in cols, f"missing backfilled column: {required}"


def test_migrate_is_idempotent_after_backfill(tmp_path: Path):
    """Running migrate() twice on a backfilled DB must not error or
    duplicate columns."""
    db = tmp_path / "store.db"
    s1 = ArtifactStore.init(db)
    cols_before = {r[1] for r in s1.conn.execute(
        "PRAGMA table_info(artifact_grants)").fetchall()}
    s1.conn.close()
    s2 = ArtifactStore.init(db)  # second migrate
    cols_after = {r[1] for r in s2.conn.execute(
        "PRAGMA table_info(artifact_grants)").fetchall()}
    assert cols_before == cols_after
