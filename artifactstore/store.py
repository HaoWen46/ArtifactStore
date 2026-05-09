"""Public API. Signatures track ArtifactStore_PLAN.md §9.

Read paths route through grants.check() which is the only place that enforces
allowed_ops / allowed_views / predicate / expiry and writes the audit row.
search() is the exception: per-result predicate filtering means it logs once
per call rather than per row, and silently omits artifacts the predicate
denies. Op-level enforcement (`"search"` ∈ allowed_ops) still goes through
check() with artifact_id=None.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from artifactstore import views
from artifactstore.db import connect, migrate, new_id
from artifactstore.extractors import extract
from artifactstore.grants import (
    AccessDenied,
    DEFAULT_SENSITIVITY,
    check,
    load_grant,
    log_access,
    predicate_matches,
)
from artifactstore.previews import make_preview
from artifactstore.tokens import estimate


class ArtifactStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection = connect(self.db_path)

    @classmethod
    def init(cls, db_path: str | Path) -> "ArtifactStore":
        store = cls(db_path)
        migrate(store.conn)
        return store

    # --- write path -------------------------------------------------------

    def put_artifact(
        self,
        *,
        tool_name: str,
        artifact_type: str,
        raw_text: str,
        creator_agent_id: str,
        session_id: str,
        metadata: dict | None = None,
        sensitivity_label: str = DEFAULT_SENSITIVITY,
        parent_artifact_id: str | None = None,
    ) -> str:
        aid = new_id("art")
        raw_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        raw_tokens = estimate(raw_text)
        preview_text = make_preview(artifact_type, raw_text, raw_tokens)

        self.conn.execute(
            """INSERT INTO artifacts(
                 artifact_id, session_id, parent_artifact_id, creator_agent_id,
                 tool_name, artifact_type, raw_blob, raw_hash, token_count,
                 preview, sensitivity_label, metadata_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (aid, session_id, parent_artifact_id, creator_agent_id,
             tool_name, artifact_type, raw_text, raw_hash, raw_tokens,
             preview_text, sensitivity_label, json.dumps(metadata or {})),
        )

        span_texts: list[str] = []
        for stype, fpath, lstart, lend, text, importance in extract(
            artifact_type, raw_text
        ):
            sid = new_id("span")
            self.conn.execute(
                """INSERT INTO artifact_spans(
                     span_id, artifact_id, span_type, file_path,
                     line_start, line_end, text, token_count, importance
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, aid, stype, fpath, lstart, lend, text,
                 estimate(text), importance),
            )
            span_texts.append(text)

        self.conn.execute(
            """INSERT INTO artifact_fts(
                 artifact_id, artifact_type, preview, span_text, tool_name
               ) VALUES (?, ?, ?, ?, ?)""",
            (aid, artifact_type, preview_text, "\n".join(span_texts), tool_name),
        )
        return aid

    # --- read path (gated by grant_id) ------------------------------------

    def search(
        self,
        query: str,
        *,
        grant_id: str,
        artifact_types: list[str] | None = None,
        limit: int = 5,
        token_budget: int = 1000,
    ) -> list[dict]:
        grant = check(self.conn, grant_id, None, "search", None)
        # FTS5 syntax is permissive enough for plain queries; bm25 ranks better.
        try:
            rows = self.conn.execute(
                "SELECT artifact_id, bm25(artifact_fts) AS score "
                "FROM artifact_fts WHERE artifact_fts MATCH ? "
                "ORDER BY score",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed FTS5 query — fall back to LIKE on preview.
            like = f"%{query}%"
            rows = self.conn.execute(
                "SELECT artifact_id, 0.0 AS score FROM artifact_fts "
                "WHERE preview LIKE ? OR span_text LIKE ?",
                (like, like),
            ).fetchall()

        out: list[dict] = []
        used = 0
        for r in rows:
            aid = r["artifact_id"]
            art = self.conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (aid,)
            ).fetchone()
            if art is None:
                continue
            if artifact_types and art["artifact_type"] not in artifact_types:
                continue
            if not predicate_matches(grant["artifact_predicate"], dict(art)):
                continue
            item = {
                "artifact_id": aid,
                "type": art["artifact_type"],
                "preview": art["preview"],
                "score": r["score"],
            }
            cost = estimate(item["preview"] or "")
            if used + cost > token_budget:
                break
            out.append(item)
            used += cost
            if len(out) >= limit:
                break

        log_access(self.conn, grant_id=grant_id,
                   subject_agent_id=grant["subject_agent_id"],
                   artifact_id=None, operation="search", view=None,
                   result_token_count=used, allowed=True, denial_reason=None)
        return out

    def get_spans(
        self,
        artifact_id: str,
        *,
        grant_id: str,
        span_types: list[str] | None = None,
        token_budget: int = 1000,
    ) -> list[dict]:
        grant = check(self.conn, grant_id, artifact_id, "get_spans", None)
        q = (
            "SELECT span_id, span_type, file_path, line_start, line_end, "
            "       text, token_count, importance "
            "FROM artifact_spans WHERE artifact_id = ?"
        )
        args: list = [artifact_id]
        if span_types:
            q += f" AND span_type IN ({','.join('?' * len(span_types))})"
            args.extend(span_types)
        q += " ORDER BY COALESCE(importance, 0) DESC, span_id"
        rows = self.conn.execute(q, args).fetchall()

        from artifactstore.grants import span_passes_path_prefix
        predicate = grant["artifact_predicate"]
        out: list[dict] = []
        used = 0
        for r in rows:
            if not span_passes_path_prefix(predicate, r["file_path"]):
                continue
            cost = r["token_count"] or estimate(r["text"])
            if used + cost > token_budget:
                break
            out.append({
                "span_id": r["span_id"],
                "type": r["span_type"],
                "file_path": r["file_path"],
                "line_start": r["line_start"],
                "line_end": r["line_end"],
                "text": r["text"],
                "importance": r["importance"],
            })
            used += cost

        log_access(self.conn, grant_id=grant_id,
                   subject_agent_id=grant["subject_agent_id"],
                   artifact_id=artifact_id, operation="get_spans", view=None,
                   result_token_count=used, allowed=True, denial_reason=None)
        return out

    def expand_view(
        self,
        artifact_id: str,
        *,
        grant_id: str,
        view: str,
        token_budget: int = 1500,
    ) -> str:
        if view not in views.VIEW_NAMES:
            raise ValueError(f"unknown view: {view}")
        grant = check(self.conn, grant_id, artifact_id, "expand_view", view)
        rendered = views.VIEWS[view](
            self.conn, artifact_id, token_budget,
            predicate=grant["artifact_predicate"],
        )
        log_access(self.conn, grant_id=grant_id,
                   subject_agent_id=grant["subject_agent_id"],
                   artifact_id=artifact_id, operation="expand_view", view=view,
                   result_token_count=estimate(rendered),
                   allowed=True, denial_reason=None)
        return rendered

    def find_related(
        self,
        artifact_id: str,
        *,
        grant_id: str,
        relations: list[str] | None = None,
    ) -> list[dict]:
        grant = check(self.conn, grant_id, artifact_id, "find_related", None)
        q = ("SELECT dst_artifact_id, relation, confidence "
             "FROM artifact_links WHERE src_artifact_id = ?")
        args: list = [artifact_id]
        if relations:
            q += f" AND relation IN ({','.join('?' * len(relations))})"
            args.extend(relations)
        rows = [dict(r) for r in self.conn.execute(q, args).fetchall()]
        log_access(self.conn, grant_id=grant_id,
                   subject_agent_id=grant["subject_agent_id"],
                   artifact_id=artifact_id, operation="find_related", view=None,
                   result_token_count=0, allowed=True, denial_reason=None)
        return rows

    # --- grants & audit ---------------------------------------------------

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
        gid = new_id("grant")
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(seconds=ttl_seconds)).isoformat()
        self.conn.execute(
            """INSERT INTO artifact_grants(
                 grant_id, subject_agent_id, issuer_agent_id,
                 artifact_predicate, allowed_ops, allowed_views,
                 max_tokens, expires_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (gid, subject_agent_id, issuer_agent_id,
             json.dumps(artifact_predicate),
             json.dumps(allowed_ops),
             json.dumps(allowed_views),
             max_tokens, expires_at),
        )
        return gid

    def audit(self, grant_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT access_id, grant_id, subject_agent_id, artifact_id, "
            "       operation, view, timestamp, result_token_count, allowed, "
            "       denial_reason "
            "FROM artifact_access_log WHERE grant_id = ? ORDER BY timestamp",
            (grant_id,),
        ).fetchall()
        return [dict(r) for r in rows]
