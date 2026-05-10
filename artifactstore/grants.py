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
import re
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


# Patterns that indicate sensitive content in raw text. If any matches, we
# bump the sensitivity_label up to 'restricted' regardless of what the
# producer said. Defense-in-depth against producers who self-label as
# 'public' to bypass the predicate ceiling.
#
# These are heuristic — a determined attacker can phrase secrets in ways
# the regex doesn't see ("the password is hunter2 in plain prose"). The
# threat model documents this explicitly: callers from outside the harness
# (subagents, external tools) cannot raise their own sensitivity ceiling
# by mislabeling.
_SECRET_PATTERNS = [
    # JWT (three base64-ish segments separated by dots)
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # secret/password/api_key/bearer = value
    re.compile(r"(?i)\b(secret|password|passwd|api[_-]?key|bearer|access[_-]?key)"
               r"\s*[:=]\s*\S+"),
    # OpenAI/Anthropic/etc shape keys
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # AWS access key id
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    # Private key blocks
    re.compile(r"-----BEGIN[A-Z ]+PRIVATE KEY-----"),
]


def infer_sensitivity(raw_text: str) -> str:
    """Heuristic: returns the MINIMUM safe sensitivity label for the given
    raw text. 'restricted' if any secret-pattern matches, else 'public'
    (no positive signal — caller's claim wins). False negatives possible
    (a determined attacker can phrase secrets in prose); this is
    defense-in-depth, not a hard guarantee.
    """
    for pat in _SECRET_PATTERNS:
        if pat.search(raw_text):
            return "restricted"
    return "public"


def effective_sensitivity(claimed_label: str, raw_text: str) -> str:
    """The sensitivity label actually stored.

    Semantics: `final = max(claimed, inferred)` by SENSITIVITY ordering.

    - If the producer claims a high label (e.g. 'secret'), the claim is
      honored — claimed acts as a lower bound.
    - If the producer claims a low label (e.g. 'public') but the
      heuristic finds secrets, the inferred label wins. The producer
      cannot self-label sensitive content down to bypass
      `sensitivity_max` predicates.
    - Clean content + claim of 'public' ⇒ 'public' (claim honored).

    This is enforcement at the storage boundary, not at the read boundary
    (predicate enforcement still applies as before). One regex pass per
    put_artifact.
    """
    inferred = infer_sensitivity(raw_text)
    claimed_score = SENSITIVITY.get(claimed_label, SENSITIVITY[DEFAULT_SENSITIVITY])
    inferred_score = SENSITIVITY.get(inferred, SENSITIVITY[DEFAULT_SENSITIVITY])
    if inferred_score > claimed_score:
        return inferred
    return claimed_label


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

    # Cumulative token budget. max_tokens=NULL on a grant means unlimited
    # (the seeded __supervisor__ grant uses this). Otherwise reads stop the
    # moment consumed_tokens >= max_tokens — a hard quota, not advisory.
    max_tokens = grant.get("max_tokens")
    consumed = grant.get("consumed_tokens") or 0
    if max_tokens is not None and consumed >= max_tokens:
        _deny(f"grant budget exhausted ({consumed}/{max_tokens} tokens)")

    if artifact_id is not None:
        art = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if art is None:
            _deny(f"artifact {artifact_id} not found")
        if not predicate_matches(grant["artifact_predicate"], dict(art)):
            _deny("artifact does not match grant predicate")

    return grant


def account_consumption(conn: sqlite3.Connection, grant_id: str,
                        tokens: int) -> None:
    """Increment a grant's consumed_tokens after a successful read. Called by
    the store API alongside log_access. Skipped for grants with NULL
    max_tokens (unlimited) — there's no quota to debit, and otherwise the
    seeded __supervisor__ grant's counter would drift upward forever.
    """
    if tokens <= 0:
        return
    # Only debit grants with a finite max_tokens.
    conn.execute(
        "UPDATE artifact_grants SET consumed_tokens = "
        "COALESCE(consumed_tokens, 0) + ? "
        "WHERE grant_id = ? AND max_tokens IS NOT NULL",
        (tokens, grant_id),
    )


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
