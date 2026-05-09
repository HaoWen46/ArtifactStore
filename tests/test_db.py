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
