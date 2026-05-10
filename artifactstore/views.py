"""Materializers for the five artifact views (PLAN §8).

Each view: (conn, artifact_id, token_budget, *, predicate=None) -> str.
The optional `predicate` lets the caller apply grant-side path_prefix filters
to span-level views (evidence). Other views ignore it.

Token budget is enforced via omit-fits over the source unit (lines for raw /
preview / redacted, spans for evidence). Whole-or-skip semantics.
"""
from __future__ import annotations

import json
import re
import sqlite3

from artifactstore.grants import span_passes_path_prefix
from artifactstore.tokens import estimate

VIEW_NAMES = ("preview", "evidence", "redacted", "raw", "provenance")


class ArtifactNotFound(KeyError):
    pass


def _truncate_lines(text: str, budget: int) -> str:
    """Take whole lines until budget would overflow. If the first line
    alone overflows (single-line content like a minified file or a JSON
    dump), fall back to character-level truncation with a '... [N chars
    truncated]' marker — otherwise the caller would get an empty string
    for any content with no useful line breaks under tight budgets.
    """
    if budget <= 0 or not text:
        return ""
    out: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = estimate(line) + 1
        if used + cost > budget:
            break
        out.append(line)
        used += cost
    if out:
        return "\n".join(out)
    # Nothing fit at line granularity. Char-level fallback so we never
    # silently return empty when content exists. ~4 chars/token; leave
    # room for the truncation marker.
    marker = f"\n... [truncated, {len(text)} chars total]"
    chars_for_content = max(0, budget * 4 - len(marker))
    if chars_for_content <= 0:
        return ""
    return text[:chars_for_content] + marker


def _load_artifact(conn: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if row is None:
        raise ArtifactNotFound(artifact_id)
    return row


def preview(conn: sqlite3.Connection, artifact_id: str, token_budget: int,
            *, predicate: dict | None = None) -> str:
    art = _load_artifact(conn, artifact_id)
    return _truncate_lines(art["preview"] or "", token_budget)


def evidence(conn: sqlite3.Connection, artifact_id: str, token_budget: int,
             *, predicate: dict | None = None) -> str:
    """Spans ordered by importance DESC; whole-span omit-fits.
    Each rendered span carries its span_id so subagents can cite back."""
    _load_artifact(conn, artifact_id)
    rows = conn.execute(
        "SELECT span_id, span_type, file_path, line_start, line_end, text, "
        "       importance "
        "FROM artifact_spans WHERE artifact_id = ? "
        "ORDER BY COALESCE(importance, 0) DESC, span_id",
        (artifact_id,),
    ).fetchall()
    out: list[str] = []
    used = 0
    for r in rows:
        if predicate and not span_passes_path_prefix(predicate, r["file_path"]):
            continue
        loc = r["file_path"] or ""
        if r["line_start"] is not None:
            loc = f"{loc}:{r['line_start']}" if loc else f":{r['line_start']}"
        rendered = (
            f"<{r['span_id']} type={r['span_type']} "
            f"loc={loc or '-'} importance={r['importance']:.2f}>\n"
            f"{r['text']}\n"
        )
        cost = estimate(rendered)
        if used + cost > token_budget:
            break
        out.append(rendered)
        used += cost
    return "\n".join(out)


# Lightweight redactor: strip JWT-shaped tokens, common secret-style key=val.
# Not a security boundary (PLAN §17 is explicit) — just enough to make the
# redacted view meaningfully different from raw for the demo and RQ4 tests.
_REDACT_PATTERNS = [
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
     "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(secret|password|passwd|api[_-]?key|token)\s*[:=]\s*\S+"),
     r"\1=[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_KEY]"),
]


def redacted(conn: sqlite3.Connection, artifact_id: str, token_budget: int,
             *, predicate: dict | None = None) -> str:
    art = _load_artifact(conn, artifact_id)
    text = art["raw_blob"] or ""
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return _truncate_lines(text, token_budget)


def raw(conn: sqlite3.Connection, artifact_id: str, token_budget: int,
        *, predicate: dict | None = None) -> str:
    art = _load_artifact(conn, artifact_id)
    return _truncate_lines(art["raw_blob"] or "", token_budget)


def provenance(conn: sqlite3.Connection, artifact_id: str, token_budget: int,
               *, predicate: dict | None = None) -> str:
    art = _load_artifact(conn, artifact_id)
    n_spans = conn.execute(
        "SELECT COUNT(*) FROM artifact_spans WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()[0]
    parents = conn.execute(
        "SELECT dst_artifact_id, relation FROM artifact_links "
        "WHERE src_artifact_id = ?",
        (artifact_id,),
    ).fetchall()
    payload = {
        "artifact_id": art["artifact_id"],
        "session_id": art["session_id"],
        "creator_agent_id": art["creator_agent_id"],
        "tool_name": art["tool_name"],
        "artifact_type": art["artifact_type"],
        "raw_hash": art["raw_hash"],
        "token_count": art["token_count"],
        "sensitivity_label": art["sensitivity_label"],
        "created_at": art["created_at"],
        "span_count": n_spans,
        "links": [{"to": r["dst_artifact_id"], "relation": r["relation"]}
                  for r in parents],
        "metadata": json.loads(art["metadata_json"] or "{}"),
    }
    return _truncate_lines(json.dumps(payload, indent=2), token_budget)


VIEWS = {
    "preview": preview,
    "evidence": evidence,
    "redacted": redacted,
    "raw": raw,
    "provenance": provenance,
}
