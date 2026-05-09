"""Citation parsing + verification (PLAN §20.2).

Subagent reports cite evidence as 'art_<8hex>/span_<8hex>'. The supervisor
verifies each citation by re-resolving it through the store under its own
grant. Anything ill-formed or unresolvable invalidates the report.

Single source of truth — both the demo harness and eval driver call into here.
"""
from __future__ import annotations
import re
import sqlite3

CITATION_RE = re.compile(r"^art_[0-9a-f]{8}/span_[0-9a-f]{8}$")


class BadCitation(ValueError):
    pass


def parse(citation: str) -> tuple[str, str]:
    """('art_xxxxxxxx', 'span_yyyyyyyy') or BadCitation."""
    s = citation.strip()
    if not CITATION_RE.match(s):
        raise BadCitation(f"malformed citation: {citation!r}")
    art, span = s.split("/", 1)
    return art, span


def verify_resolves(conn: sqlite3.Connection, citation: str) -> bool:
    """Does this citation resolve to a real (artifact_id, span_id) pair?

    Note: this is the *existence* check. Permission/grant enforcement is done
    at the call site by routing the lookup through ArtifactStore.expand_view /
    get_spans under the verifier's grant — usually '__supervisor__'.
    """
    try:
        art_id, span_id = parse(citation)
    except BadCitation:
        return False
    row = conn.execute(
        "SELECT 1 FROM artifact_spans WHERE artifact_id = ? AND span_id = ?",
        (art_id, span_id),
    ).fetchone()
    return row is not None


def verify_all(conn: sqlite3.Connection, citations: list[str]) -> dict[str, bool]:
    """Bulk variant. Returns {citation: resolved?} preserving order."""
    return {c: verify_resolves(conn, c) for c in citations}
