"""Public API. Signatures track ArtifactStore_PLAN.md §9."""
from __future__ import annotations
import sqlite3
from pathlib import Path

from artifactstore.db import connect, migrate


class ArtifactStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection = connect(self.db_path)

    @classmethod
    def init(cls, db_path: str | Path) -> "ArtifactStore":
        store = cls(db_path)
        migrate(store.conn)
        return store

    # --- write path ---

    def put_artifact(
        self,
        *,
        tool_name: str,
        artifact_type: str,
        raw_text: str,
        creator_agent_id: str,
        session_id: str,
        metadata: dict | None = None,
    ) -> str:
        raise NotImplementedError

    # --- read path (all gated by grant_id) ---

    def search(
        self,
        query: str,
        *,
        grant_id: str,
        artifact_types: list[str] | None = None,
        limit: int = 5,
        token_budget: int = 1000,
    ) -> list[dict]:
        raise NotImplementedError

    def get_spans(
        self,
        artifact_id: str,
        *,
        grant_id: str,
        span_types: list[str] | None = None,
        token_budget: int = 1000,
    ) -> list[dict]:
        raise NotImplementedError

    def expand_view(
        self,
        artifact_id: str,
        *,
        grant_id: str,
        view: str,
        token_budget: int,
    ) -> str:
        raise NotImplementedError

    def find_related(
        self,
        artifact_id: str,
        *,
        grant_id: str,
        relations: list[str] | None = None,
    ) -> list[dict]:
        raise NotImplementedError

    # --- grants & audit ---

    def create_grant(
        self,
        *,
        subject_agent_id: str,
        issuer_agent_id: str,
        artifact_predicate: dict,
        allowed_ops: list[str],
        allowed_views: list[str],
        max_tokens: int,
        ttl_seconds: int,
    ) -> str:
        raise NotImplementedError

    def audit(self, grant_id: str) -> list[dict]:
        raise NotImplementedError
