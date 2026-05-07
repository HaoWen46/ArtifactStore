"""Workload runner — produces tool outputs to feed into ArtifactStore.

Two modes:
  - replay (default): read a captured fixture from eval/fixtures/, never spawn
    a real process. Deterministic for evaluation.
  - live: actually shell out (pytest, rg, git). Off by default.

Two view policies, exposed by the runner so the same workload can power all
four eval baselines (PLAN §11.1 B1-B4):
  - RAW         → tool returns the full output (B1)
  - TRUNCATED   → tool returns first N tokens (B2)
  - SUMMARY     → LLM-summarize, return summary (B3) — TODO when eval lands
  - ARTIFACT    → put in ArtifactStore, return only the handle (B4) — the demo
"""
from __future__ import annotations

import shlex
import subprocess
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


@dataclass
class WorkloadResult:
    """What the supervisor's tool actually returns under the chosen policy."""
    policy: ViewPolicy
    artifact_type: str
    artifact_id: str | None      # None unless policy == ARTIFACT
    preview: str | None          # None for RAW
    body: str | None             # full / truncated / summary; None for ARTIFACT
    raw_token_count: int


def _load(kind: str, target: str, *, live: bool) -> str:
    if not live:
        key = (kind, target)
        if key not in FIXTURE_INDEX:
            raise FileNotFoundError(
                f"no fixture registered for ({kind}, {target}); "
                f"add one to eval/fixtures/ and FIXTURE_INDEX"
            )
        return (FIXTURES_DIR / FIXTURE_INDEX[key]).read_text()

    # live mode (off-path; only used when --live is passed)
    cmd = {
        "pytest": f"pytest -x {shlex.quote(target)}",
        "grep":   f"rg --no-heading {shlex.quote(target)}",
        "git":    f"git diff {shlex.quote(target)}",
    }.get(kind)
    if cmd is None:
        raise ValueError(f"live mode not supported for kind={kind}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (proc.stdout or "") + (proc.stderr or "")


def run_workload(
    *,
    store: ArtifactStore,
    session_id: str,
    creator_agent_id: str,
    kind: str,
    target: str,
    policy: ViewPolicy = ViewPolicy.ARTIFACT,
    truncate_tokens: int = 200,
    live: bool = False,
) -> WorkloadResult:
    raw = _load(kind, target, live=live)
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
        # TODO(eval): call Claude to summarize raw under truncate_tokens.
        # Stubbed deterministically for now so the runner doesn't break.
        body = f"[B3 summary stub] {artifact_type} of {raw_tokens} tokens; " \
               f"first line: {raw.splitlines()[0] if raw else ''!r}"
        return WorkloadResult(policy, artifact_type, None, None, body, raw_tokens)

    # ViewPolicy.ARTIFACT — the demo path
    artifact_id = store.put_artifact(
        tool_name=kind,
        artifact_type=artifact_type,
        raw_text=raw,
        creator_agent_id=creator_agent_id,
        session_id=session_id,
        metadata={"target": target, "live": live},
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
