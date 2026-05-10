"""Type-driven preview registry. Same registry shape as extractors.py.

A preview is a compact handle the model sees instead of raw output. Format:

    [<artifact_type> | <one-line summary> | <raw_token_count> tokens]
    <body lines under PREVIEW_TOKEN_BUDGET tokens>

When extracted spans are passed in, the body inlines the top-importance
spans rather than the head of the raw output. This is a substantive
RQ1 win: for diagnostic artifacts, the assertion line / log warning /
changed_line is far more useful per token than the test-collection header.
The model gets a meaningful peek and can skip the round-trip in many cases.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from artifactstore.tokens import estimate

PREVIEW_TOKEN_BUDGET = 256

PreviewFn = Callable[[str], str]
_REGISTRY: dict[str, PreviewFn] = {}


def register(artifact_type: str):
    def deco(fn: PreviewFn) -> PreviewFn:
        _REGISTRY[artifact_type] = fn
        return fn
    return deco


def _truncate_lines_to_budget(text: str, budget: int) -> str:
    """Omit-fits over lines. If the first line alone overflows (single-line
    content), fall back to char-level truncation with a marker so the
    preview body is never silently empty. See views._truncate_lines for
    the same semantics on the read path."""
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
    marker = f"\n... [truncated, {len(text)} chars total]"
    chars_for_content = max(0, budget * 4 - len(marker))
    if chars_for_content <= 0:
        return ""
    return text[:chars_for_content] + marker


def _spans_inline(spans: list[Any], budget: int) -> str:
    """Format top-importance spans for inline preview body.

    `spans` are Span tuples from extractors.py:
        (span_type, file_path|None, line_start|None, line_end|None, text, importance)
    Sort by importance DESC, format compactly, omit-fits to `budget` tokens.
    Each span gets one line: `<span_type>@<file>:<line> <text>`.
    """
    if budget <= 0 or not spans:
        return ""
    ranked = sorted(spans, key=lambda s: -(s[5] or 0))
    out: list[str] = []
    used = 0
    for span_type, fpath, lstart, _lend, text, _imp in ranked:
        loc = fpath or "-"
        if lstart is not None and fpath:
            loc = f"{fpath}:{lstart}"
        line = f"  · {span_type}@{loc}  {text}"
        cost = estimate(line) + 1
        if used + cost > budget:
            break
        out.append(line)
        used += cost
    return "\n".join(out)


def _default_summary(raw: str) -> str:
    """Fallback when no type-specific preview is registered."""
    first = next((ln for ln in raw.splitlines() if ln.strip()), "")
    return first[:80] if first else "(empty)"


def make_preview(artifact_type: str, raw_text: str, raw_tokens: int,
                 spans: list[Any] | None = None) -> str:
    """Build the preview string. Hard-capped at PREVIEW_TOKEN_BUDGET.

    If `spans` is provided and non-empty, the body inlines the top-importance
    spans (one line each, sorted by importance DESC). This gives the model
    real diagnostic signal in the preview itself — most diagnostic tasks
    can be solved from the preview alone with this layout. Otherwise we
    fall back to the head-of-raw layout.
    """
    fn = _REGISTRY.get(artifact_type, _default_summary)
    summary = fn(raw_text).strip().replace("\n", " ")
    header = f"[{artifact_type} | {summary} | {raw_tokens} tokens]"
    body_budget = PREVIEW_TOKEN_BUDGET - estimate(header) - 1
    if spans:
        body = _spans_inline(spans, body_budget)
        if not body:
            # Spans existed but nothing fit — fall back so the preview is
            # never empty under the budget.
            body = _truncate_lines_to_budget(raw_text, body_budget)
    else:
        body = _truncate_lines_to_budget(raw_text, body_budget)
    return f"{header}\n{body}" if body else header


# --- type-specific summaries ---

@register("pytest_failure")
def _pytest_summary(raw: str) -> str:
    m = re.search(r"(\d+)\s+failed,\s*(\d+)\s+passed", raw)
    if m:
        return f"{m.group(1)} failed, {m.group(2)} passed"
    if "FAILED" in raw or "failed" in raw:
        return "pytest failure"
    return "pytest result"


@register("grep_result")
def _grep_summary(raw: str) -> str:
    n = sum(1 for ln in raw.splitlines() if ln.strip())
    return f"{n} hits"


@register("git_diff")
def _git_summary(raw: str) -> str:
    files = sum(1 for ln in raw.splitlines() if ln.startswith("diff --git"))
    adds = sum(1 for ln in raw.splitlines()
               if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in raw.splitlines()
               if ln.startswith("-") and not ln.startswith("---"))
    return f"{files} files, +{adds} -{dels}"
