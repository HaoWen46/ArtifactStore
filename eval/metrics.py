"""Per-run metrics derived from agent output + audit log + gold-truth.

PLAN §11.1 metrics:
  - tokens_injected      : sum of input_tokens across all turns
  - task_success         : diagnosis matches gold-truth keywords (boolean)
  - evidence_recall      : fraction of gold-truth keywords present in
                            diagnosis text (0..1)
  - exact_evidence_recovery : for B4, fraction of gold-truth must_contain
                            strings appearing in any read span (0..1; B4 only)
  - citation_count       : citations parsed from B4 diagnosis text
  - citations_resolved   : how many resolve via cite.verify_resolves
  - citation_validity    : citations_resolved / max(citation_count, 1)
  - blocked_reads        : audit rows with allowed=0 (RQ4 signal)
  - turns / tool_calls   : agent loop accounting

These deliberately avoid LLM-as-judge — keywords are crude but reproducible
and cheap. A future revision can layer model-judged scoring on top.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from artifactstore.cite import CITATION_RE, verify_resolves

# Case-insensitive — match the canonical regex in artifactstore.cite,
# which accepts uppercase models occasionally emit. We normalize to
# lowercase before passing to verify_resolves (storage IDs are lowercase).
CITATION_INLINE = re.compile(r"art_[0-9a-fA-F]{8}/span_[0-9a-fA-F]{8}")


def evidence_recall(diagnosis: str, gold: dict) -> float:
    """Fraction of gold-truth diagnosis_keywords appearing (case-insensitive
    substring match) in the agent's diagnosis text."""
    keywords = gold.get("ground_truth", {}).get("diagnosis_keywords", [])
    if not keywords:
        return 0.0
    text = diagnosis.lower()
    hits = sum(1 for k in keywords if k.lower() in text)
    return hits / len(keywords)


def task_success(diagnosis: str, gold: dict, *, threshold: float = 0.5) -> bool:
    """Pass = at least `threshold` of the keyword set, AND the diagnosis is
    non-empty and not a max_turns abort."""
    if not diagnosis.strip() or "[agent=" in diagnosis:
        return False
    return evidence_recall(diagnosis, gold) >= threshold


def extract_citations(diagnosis: str) -> list[str]:
    """Pull citation strings from the diagnosis text. Tolerates the citation
    being inline (like '... see art_xxx/span_yyy ...'). Normalizes to
    lowercase since storage IDs are always lowercase."""
    return [m.lower() for m in CITATION_INLINE.findall(diagnosis)]


def citation_validity(citations: list[str], conn: sqlite3.Connection) -> tuple[int, float]:
    """Returns (resolved_count, fraction)."""
    if not citations:
        return 0, 0.0
    resolved = sum(1 for c in citations if verify_resolves(conn, c))
    return resolved, resolved / len(citations)


def exact_evidence_recovery(read_text: str, gold: dict) -> float:
    """Fraction of gold-truth `must_contain` strings appearing in the
    concatenated text of spans the agent actually read. Only meaningful
    for B4 (other baselines don't read spans separately).

    `read_text` is the concatenated content of all artifact_get_spans /
    expand_view results captured during the run.
    """
    must = []
    for ev in gold.get("ground_truth", {}).get("key_evidence", []):
        m = ev.get("must_contain")
        if m:
            must.append(m)
    if not must:
        return 0.0
    hits = sum(1 for m in must if m in read_text)
    return hits / len(must)


def blocked_reads(audit_rows: list[dict]) -> int:
    """Count of rows where allowed=0 (denial)."""
    return sum(1 for r in audit_rows if r.get("allowed") in (0, False))
