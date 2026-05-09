"""Grant predicate evaluation + audit logging (PLAN §7.4, §7.5).

Predicate semantics (locked in CLAUDE.md "Design choices"):
  - session_id        artifact.session_id == predicate.session_id
  - artifact_types[]  artifact.artifact_type ∈ predicate.artifact_types
  - sensitivity_max   SENSITIVITY[artifact.label] ≤ SENSITIVITY[predicate.max]
  - path_prefixes[]   applies to artifact_spans.file_path only (filtered at
                      span-read time). Spans with NULL file_path are
                      path-opaque and pass.
Empty / missing predicate keys = no constraint along that axis.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone

from artifactstore.db import new_id


# Numeric ordering for sensitivity_max comparisons. Single source of truth.
SENSITIVITY: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "restricted": 2,
    "secret": 3,
}
DEFAULT_SENSITIVITY = "internal"


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
    """Evaluate a grant predicate against an artifact row dict.

    Path prefixes are NOT checked here — they apply at span-read time against
    artifact_spans.file_path (see span_passes_path_prefix). An artifact-only
    check has no path to compare against.
    """
    if "session_id" in predicate and predicate["session_id"]:
        if artifact.get("session_id") != predicate["session_id"]:
            return False
    types = predicate.get("artifact_types")
    if types:
        if artifact.get("artifact_type") not in types:
            return False
    smax = predicate.get("sensitivity_max")
    if smax:
        artifact_label = artifact.get("sensitivity_label") or DEFAULT_SENSITIVITY
        if SENSITIVITY.get(artifact_label, 99) > SENSITIVITY.get(smax, -1):
            return False
    return True


def span_passes_path_prefix(predicate: dict, span_file_path: str | None) -> bool:
    """Path-prefix check for one span. NULL file_path is path-opaque (passes)."""
    prefixes = predicate.get("path_prefixes")
    if not prefixes:
        return True
    if span_file_path is None:
        return True
    return any(span_file_path.startswith(p) for p in prefixes)


def check(conn: sqlite3.Connection, grant_id: str, artifact_id: str | None,
          op: str, view: str | None) -> dict:
    """Returns the loaded grant if access is permitted, else raises AccessDenied.
    Logs every attempt (allowed or not) to artifact_access_log.

    `artifact_id` may be None for ops that aren't artifact-scoped at the call
    site (e.g. `search`, where the predicate is enforced per-result and the
    audit row records only the call itself).
    """
    try:
        grant = load_grant(conn, grant_id)
    except AccessDenied as e:
        log_access(conn, grant_id=grant_id, subject_agent_id=None,
                   artifact_id=artifact_id, operation=op, view=view,
                   result_token_count=0, allowed=False, denial_reason=str(e))
        raise

    subj = grant["subject_agent_id"]

    def _deny(reason: str) -> None:
        log_access(conn, grant_id=grant_id, subject_agent_id=subj,
                   artifact_id=artifact_id, operation=op, view=view,
                   result_token_count=0, allowed=False, denial_reason=reason)
        raise AccessDenied(reason)

    if op not in grant["allowed_ops"]:
        _deny(f"op '{op}' not in allowed_ops")
    if view is not None and view not in grant["allowed_views"]:
        _deny(f"view '{view}' not in allowed_views")

    expires_at = grant.get("expires_at")
    if expires_at:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            _deny("grant expired")

    if artifact_id is not None:
        art = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if art is None:
            _deny(f"artifact {artifact_id} not found")
        if not predicate_matches(grant["artifact_predicate"], dict(art)):
            _deny("artifact does not match grant predicate")

    return grant


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
