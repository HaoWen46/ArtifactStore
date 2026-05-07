from pathlib import Path

from artifactstore import ArtifactStore


def test_init_creates_schema(tmp_path: Path):
    db = tmp_path / "store.db"
    store = ArtifactStore.init(db)
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    names = {r[0] for r in rows}
    for required in (
        "artifacts", "artifact_spans", "artifact_links",
        "artifact_grants", "artifact_access_log", "artifact_fts",
    ):
        assert required in names, f"missing table: {required}"
