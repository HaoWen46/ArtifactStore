"""Grant predicate evaluation + audit logging (PLAN §7.4, §7.5)."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone

from artifactstore.db import new_id


class AccessDenied(Exception):
    pass


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_grant(conn: sqlite3.Connection, grant_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM artifact_grants WHERE grant_id = ?", (grant_id,)
    ).fetchone()
    if row is None:
        raise AccessDenied(f"unknown grant: {grant_id}")
    g = dict(row)
    g["artifact_predicate"] = json.loads(g["artifact_predicate"])
    g["allowed_ops"] = json.loads(g["allowed_ops"])
    g["allowed_views"] = json.loads(g["allowed_views"])
    return g


def predicate_matches(predicate: dict, artifact: dict) -> bool:
    """Predicate JSON shape (PLAN §7.4):
        session_id, artifact_types[], path_prefixes[], sensitivity_max
    Implement when grants land (build step 6)."""
    raise NotImplementedError


def check(conn: sqlite3.Connection, grant_id: str, artifact_id: str,
          op: str, view: str | None) -> dict:
    """Returns the loaded grant if access is permitted, else raises AccessDenied.
    MUST log every attempt (allowed or not) to artifact_access_log."""
    raise NotImplementedError


def log_access(conn: sqlite3.Connection, *, grant_id: str | None,
               subject_agent_id: str | None, artifact_id: str | None,
               operation: str, view: str | None,
               result_token_count: int | None,
               allowed: bool, denial_reason: str | None) -> None:
    conn.execute(
        """INSERT INTO artifact_access_log
           (access_id, grant_id, subject_agent_id, artifact_id, operation,
            view, timestamp, result_token_count, allowed, denial_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id("acc"), grant_id, subject_agent_id, artifact_id, operation,
         view, _now_utc(), result_token_count, allowed, denial_reason),
    )
