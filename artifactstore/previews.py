"""Type-driven preview registry. Same registry shape as extractors.py.

A preview is a compact handle the model sees instead of raw output. Format:

    [<artifact_type> | <one-line summary> | <raw_token_count> tokens]
    <body lines under PREVIEW_TOKEN_BUDGET tokens, type-driven>

The summary function is type-specific; the body is the type-specific summary
text or the head of the raw output truncated by tokens.
"""
from __future__ import annotations

import re
from collections.abc import Callable

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
    """Omit-fits over lines: include whole-or-skip until next line would overflow."""
    if budget <= 0:
        return ""
    out: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = estimate(line) + 1  # +1 for the newline
        if used + cost > budget:
            break
        out.append(line)
        used += cost
    return "\n".join(out)


def _default_summary(raw: str) -> str:
    """Fallback when no type-specific preview is registered."""
    first = next((ln for ln in raw.splitlines() if ln.strip()), "")
    return first[:80] if first else "(empty)"


def make_preview(artifact_type: str, raw_text: str, raw_tokens: int) -> str:
    """Build the preview string. Hard-capped at PREVIEW_TOKEN_BUDGET."""
    fn = _REGISTRY.get(artifact_type, _default_summary)
    summary = fn(raw_text).strip().replace("\n", " ")
    header = f"[{artifact_type} | {summary} | {raw_tokens} tokens]"
    body_budget = PREVIEW_TOKEN_BUDGET - estimate(header) - 1
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
