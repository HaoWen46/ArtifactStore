"""Workload runner — produces tool outputs to feed into ArtifactStore.

Replay-mode only: reads captured fixtures from `eval/fixtures/` keyed by
(kind, target). PLAN §20.6 mandates fixture replay for the eval to be
deterministic; live shell-out is intentionally NOT supported here. If
you need ad-hoc live runs, capture the output once into a fixture file,
register it in `FIXTURE_INDEX`, then replay.

Four view policies, one runner — same `run_workload` powers all four
PLAN §11.1 baselines via `ViewPolicy`:
  - RAW         → tool returns the full output (B1)
  - TRUNCATED   → tool returns first N tokens (B2)
  - SUMMARY     → deterministic offline summary (B3)
  - ARTIFACT    → put in ArtifactStore, return only the handle (B4 / demo)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from artifactstore import ArtifactStore
from artifactstore.tokens import estimate

FIXTURES_DIR = Path(__file__).parent.parent / "eval" / "fixtures"


class ViewPolicy(str, Enum):
    RAW = "raw"
    TRUNCATED = "truncated"
    SUMMARY = "summary"
    ARTIFACT = "artifact"


# (kind, target) -> fixture filename. Extend as eval workloads land (PLAN §20.6).
FIXTURE_INDEX: dict[tuple[str, str], str] = {
    ("pytest", "auth_expiry"): "pytest_auth_expiry.log",
    ("pytest", "demo"):        "pytest_auth_expiry.log",
    ("pytest", "large"):       "pytest_large_run.log",
    ("grep",   "todos"):       "rg_grep_noise.txt",
    ("git",    "auth_diff"):   "git_diff_auth_refactor.diff",
}

KIND_TO_ARTIFACT_TYPE = {
    "pytest": "pytest_failure",
    "grep":   "grep_result",
    "git":    "git_diff",
    "npm":    "npm_test",
    "docker": "docker_log",
}


# Fail signals the deterministic summarizer keeps. Tuned for diagnostic
# tasks — pytest failures, log warnings, error tracebacks. Same regex
# used by eval/baselines.py B3 (single-agent) and eval/delegation.py D1
# (delegation), so the eval comparison is consistent across modes.
_FAILURE_LINE = re.compile(r"FAIL|Error|assert|WARNING|Traceback",
                           re.IGNORECASE)


def deterministic_summary(raw: str, *, head_lines: int = 5,
                          fail_lines_max: int = 20,
                          token_cap: int = 150) -> str:
    """Produce a deterministic, LLM-free summary of a raw tool output.

    Heuristic: top-of-file (first `head_lines`) + any line matching the
    failure-signal regex (capped at `fail_lines_max`) + a `...` separator
    between sections + a hard token cap.

    For pytest-shaped output this preserves the failing-test summary line
    and the assertion line. For grep/diff it just gives a shape sketch.

    No LLM call — the cost is zero and the output is reproducible.
    """
    lines = raw.splitlines()
    head = lines[:head_lines]
    failures = [ln for ln in lines if _FAILURE_LINE.search(ln)][:fail_lines_max]
    summary = "\n".join(head + ["..."] + failures)
    cap_chars = token_cap * 4
    if len(summary) > cap_chars:
        summary = summary[:cap_chars] + "\n... [summary truncated]"
    return summary


@dataclass
class WorkloadResult:
    """What the supervisor's tool actually returns under the chosen policy."""
    policy: ViewPolicy
    artifact_type: str
    artifact_id: str | None      # None unless policy == ARTIFACT
    preview: str | None          # None for RAW / TRUNCATED / SUMMARY
    body: str | None             # full / truncated / summary; None for ARTIFACT
    raw_token_count: int


def _load(kind: str, target: str) -> str:
    """Replay-mode fixture loader. Live execution is intentionally not
    supported — see module docstring."""
    key = (kind, target)
    if key not in FIXTURE_INDEX:
        raise FileNotFoundError(
            f"no fixture registered for ({kind}, {target}); "
            f"add one to eval/fixtures/ and FIXTURE_INDEX"
        )
    return (FIXTURES_DIR / FIXTURE_INDEX[key]).read_text()


def run_workload(
    *,
    store: ArtifactStore,
    session_id: str,
    creator_agent_id: str,
    kind: str,
    target: str,
    policy: ViewPolicy = ViewPolicy.ARTIFACT,
    truncate_tokens: int = 200,
) -> WorkloadResult:
    raw = _load(kind, target)
    artifact_type = KIND_TO_ARTIFACT_TYPE.get(kind, f"{kind}_output")
    raw_tokens = estimate(raw)

    if policy is ViewPolicy.RAW:
        return WorkloadResult(policy, artifact_type, None, None, raw, raw_tokens)

    if policy is ViewPolicy.TRUNCATED:
        approx_chars = truncate_tokens * 4
        body = raw[:approx_chars]
        if len(raw) > approx_chars:
            body += f"\n... [truncated, {raw_tokens} total tokens]"
        return WorkloadResult(policy, artifact_type, None, None, body, raw_tokens)

    if policy is ViewPolicy.SUMMARY:
        body = deterministic_summary(raw)
        return WorkloadResult(policy, artifact_type, None, None, body, raw_tokens)

    # ViewPolicy.ARTIFACT — the demo path
    artifact_id = store.put_artifact(
        tool_name=kind,
        artifact_type=artifact_type,
        raw_text=raw,
        creator_agent_id=creator_agent_id,
        session_id=session_id,
        metadata={"target": target},
    )
    # Read back the preview the store generated. put_artifact owns preview
    # extraction so the supervisor never sees raw.
    row = store.conn.execute(
        "SELECT preview, token_count FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    return WorkloadResult(
        policy=policy,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        preview=row["preview"] if row else "",
        body=None,
        raw_token_count=row["token_count"] if row else raw_tokens,
    )
