"""PLAN §11.1 single-agent eval driver.

Runs each (fixture × baseline × rep) combination, captures per-run metrics
and per-run audit logs, writes them to `eval/runs/<UTC-iso>/`.

Output layout (CLAUDE.md "eval/runs/<UTC-iso>/" contract):
  config.json    — fixtures, baselines, reps, model, base_url, git_rev
  result.jsonl   — one row per run; full RunResult
  audit.csv      — denormalized artifact_access_log across all runs
  manifest.json  — totals: runs, tokens in/out, est. cost, elapsed

We never reuse a SQLite file across runs — each run gets its own
`scratch.db` so the audit log is per-run-isolated and can be denormalized
into audit.csv with the right `run_id` foreign key.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

from artifactstore import ArtifactStore
from artifactstore.tokens import estimate
from demo.agent import Agent, ModelConfig
from demo.runner import load_dotenv

from eval.baselines import BASELINES
from eval.metrics import (
    blocked_reads,
    citation_validity,
    evidence_recall,
    exact_evidence_recovery,
    extract_citations,
    task_success,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "eval" / "fixtures"
RUNS_DIR = PROJECT_ROOT / "eval" / "runs"

# Per-run pricing for cost estimation. DeepSeek V4 Pro discounted prices as
# of 2026-05; update if the rate card changes. Source:
# https://api-docs.deepseek.com/quick_start/pricing
PRICE_PER_MTOK = {
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
}


# Map fixture filename -> (kind, target, artifact_type, ext, reveal_target)
# so workloads.py config and eval config don't drift.
#
# `reveal_target` controls whether the eval task prompt names the specific
# target. For single-failure fixtures (auth_expiry, single diff), revealing
# is harmless — there's only one failure to find. For multi-failure
# fixtures (pytest_large_run, 5 failures), revealing biases the agent
# toward the right one and inflates recall across all baselines. Setting
# reveal_target=False makes the agent discover, which is the more honest
# diagnostic test. We document the methodology in the report.
FIXTURE_REGISTRY: dict[str, dict] = {
    "pytest_auth_expiry":     {"kind": "pytest", "target": "auth_expiry",
                                "artifact_type": "pytest_failure",
                                "ext": ".log",
                                "reveal_target": True},
    # ~3500-token pytest log with 5 failures. We DON'T reveal which
    # failure to focus on — the agent has to discover the auth_expiry
    # bug among the others. Without this flag the eval would tell the
    # agent the answer.
    "pytest_large_run":       {"kind": "pytest", "target": "auth_expiry",
                                "artifact_type": "pytest_failure",
                                "ext": ".log",
                                "reveal_target": False},
    # ~10K-token CI log with 6 failures. Same auth_expiry bug as
    # pytest_large_run, buried in heavier startup logs and a longer
    # flaky-history tail. Target hidden — the agent must discover.
    "pytest_ci_run":          {"kind": "pytest", "target": "auth_expiry",
                                "artifact_type": "pytest_failure",
                                "ext": ".log",
                                "reveal_target": False},
    # ~30K-token XL CI log with 8 failures. Same auth_expiry bug, but
    # buried in heavier captured-log tails (~16K of access-log noise),
    # extended progress, stability summary, and flake-correlation
    # matrix. Designed to test the projected B1/B4 cost crossover
    # past the 10K regime. Target hidden.
    "pytest_xl_run":          {"kind": "pytest", "target": "auth_expiry",
                                "artifact_type": "pytest_failure",
                                "ext": ".log",
                                "reveal_target": False},
    "rg_grep_noise":          {"kind": "grep",   "target": "todos",
                                "artifact_type": "grep_result",
                                "ext": ".txt",
                                "reveal_target": True},
    "git_diff_auth_refactor": {"kind": "git",    "target": "auth_diff",
                                "artifact_type": "git_diff",
                                "ext": ".diff",
                                "reveal_target": True},
}


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    run_id: str
    fixture: str
    baseline: str
    rep: int
    model: str
    # Token usage. RQ1 should compare `total_input_tokens`, NOT
    # `input_tokens` — DeepSeek's prompt cache makes the latter wildly
    # rep-dependent (rep 0 pays full, reps 1-2 hit cache). Reporting all
    # three lets the writeup separate "tokens the model saw" from
    # "tokens billed at full rate".
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_input_tokens: int
    output_tokens: int
    # Loop accounting
    turns: int
    tool_calls: int
    stop_reason: str
    # Diagnosis
    diagnosis: str
    diagnosis_chars: int
    # Metrics vs gold-truth
    evidence_recall: float
    task_success: bool
    citation_count: int
    citations_resolved: int
    citation_validity: float
    exact_evidence_recovery: float
    blocked_reads: int
    # Cost
    elapsed_seconds: float
    estimated_cost_usd: float
    # Provenance
    error: str | None = None
    # Pre-run setup tokens (e.g., B3_LLM_SUMMARY's summarizer call). 0 for
    # deterministic baselines. Recorded separately so cost numbers fold the
    # full pipeline while analyses can split agent-context tokens from
    # setup-stage tokens.
    setup_input_tokens: int = 0
    setup_output_tokens: int = 0


# ---------------------------------------------------------------------------
# Per-run execution
# ---------------------------------------------------------------------------

def _capture_read_text(messages: list[dict]) -> str:
    """Concatenate all tool_result contents from an agent's message history.
    Used for exact_evidence_recovery — the model only 'saw' content via these.
    """
    chunks: list[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", [])
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, str):
                    chunks.append(c)
    return "\n".join(chunks)


def _run_one(*, fixture: str, baseline: str, rep: int, model: str,
             max_turns: int, run_dir: Path,
             gold: dict, fixture_data: str, fixture_meta: dict) -> RunResult:
    run_id = f"{fixture}__{baseline}__rep{rep}"
    db_path = run_dir / f"db_{run_id}.sqlite"
    store = ArtifactStore.init(db_path)
    setup_fn = BASELINES[baseline]
    setup = setup_fn(store, fixture_data, fixture_meta)
    agent = Agent(
        name=f"eval_{baseline}",
        system=setup.system,
        tools=setup.tools,
        config=ModelConfig(model=model, max_turns=max_turns),
        verbose=False,
    )
    started = time.time()
    error: str | None = None
    cache_read = cache_create = 0
    try:
        result = agent.run(setup.user_message)
        diagnosis = result.final_text
        stop_reason = result.stop_reason
        in_tok = result.input_tokens
        cache_read = result.cache_read_input_tokens
        cache_create = result.cache_creation_input_tokens
        out_tok = result.output_tokens
        turns = result.turns
        tool_calls = result.tool_calls
    except Exception as e:  # noqa: BLE001
        diagnosis = ""
        stop_reason = "exception"
        in_tok = out_tok = turns = tool_calls = 0
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - started

    citations = extract_citations(diagnosis)
    cit_resolved, cit_valid = citation_validity(citations, store.conn)

    read_text = _capture_read_text(agent.messages) if not error else ""
    eer = (exact_evidence_recovery(read_text, gold)
           if baseline == "B4_ARTIFACT" else 0.0)

    audit_rows = []
    if setup.grant_id is not None:
        audit_rows.extend(store.audit(setup.grant_id))
    audit_rows.extend(store.audit("__supervisor__"))

    # Cost approximation: uncached + cache_creation at full rate (creation
    # is 1.25x in DeepSeek's table but we use 1x for back-of-envelope),
    # cache_read at 0.1x. Real billing follows the provider's pricing page.
    rates = PRICE_PER_MTOK.get(model, {"input": 0.0, "output": 0.0})
    cost = (
        (in_tok + cache_create) * rates["input"]
        + cache_read * rates["input"] * 0.1
        + out_tok * rates["output"]
    ) / 1_000_000

    # Fold any pre-run setup cost (B3_LLM_SUMMARY pays a one-shot
    # summarization call per setup; deterministic baselines pay 0).
    setup_in = setup.setup_input_tokens
    setup_out = setup.setup_output_tokens
    setup_cost = (
        setup_in * rates["input"] + setup_out * rates["output"]
    ) / 1_000_000
    cost += setup_cost
    # The summarizer's tokens were *seen by an LLM* but were not part of the
    # measured agent's context — fold them into the run's total_input_tokens
    # so the cost story stays apples-to-apples but mark them via a separate
    # field so analyses can split them out.

    return RunResult(
        run_id=run_id,
        fixture=fixture,
        baseline=baseline,
        rep=rep,
        model=model,
        input_tokens=in_tok,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_create,
        total_input_tokens=in_tok + cache_read + cache_create,
        output_tokens=out_tok,
        turns=turns,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        diagnosis=diagnosis,
        diagnosis_chars=len(diagnosis),
        evidence_recall=evidence_recall(diagnosis, gold),
        task_success=task_success(diagnosis, gold),
        citation_count=len(citations),
        citations_resolved=cit_resolved,
        citation_validity=cit_valid,
        exact_evidence_recovery=eer,
        blocked_reads=blocked_reads(audit_rows),
        elapsed_seconds=elapsed,
        estimated_cost_usd=cost,
        error=error,
        setup_input_tokens=setup_in,
        setup_output_tokens=setup_out,
    ), audit_rows


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def _git_rev() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        return out
    except Exception:
        return "unknown"


def _load_fixture(name: str) -> tuple[str, dict, dict]:
    meta = FIXTURE_REGISTRY[name]
    data = (FIXTURES_DIR / f"{name}{meta['ext']}").read_text()
    gold_path = FIXTURES_DIR / f"{name}.gold.json"
    gold = json.loads(gold_path.read_text()) if gold_path.is_file() else {}
    return data, gold, meta


def run_eval(*, fixtures: list[str], baselines: list[str], reps: int,
             model: str, max_turns: int = 10,
             output_root: Path = RUNS_DIR) -> Path:
    load_dotenv(override=True)
    started = datetime.now(timezone.utc)
    run_dir = output_root / started.strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    from demo.providers import describe
    p_info = describe(model)
    config = {
        "started_at": started.isoformat(),
        "model": model,
        "provider": p_info["provider"],
        "base_url": p_info["base_url"],
        "key_env": p_info["key_env"],
        "git_rev": _git_rev(),
        "fixtures": fixtures,
        "baselines": baselines,
        "reps": reps,
        "max_turns": max_turns,
        "fixture_registry": {k: FIXTURE_REGISTRY[k] for k in fixtures},
    }
    (run_dir / "config.json").write_text(json.dumps(config, indent=2))

    results: list[RunResult] = []
    audit_csv_path = run_dir / "audit.csv"
    audit_fields = ["run_id", "access_id", "grant_id", "subject_agent_id",
                    "artifact_id", "operation", "view", "timestamp",
                    "result_token_count", "allowed", "denial_reason"]
    audit_writer_state = {"opened": False}

    def write_audit(rows: list[dict], run_id: str):
        mode = "a" if audit_writer_state["opened"] else "w"
        with audit_csv_path.open(mode, newline="") as f:
            w = csv.DictWriter(f, fieldnames=audit_fields)
            if not audit_writer_state["opened"]:
                w.writeheader()
                audit_writer_state["opened"] = True
            for row in rows:
                row_out = {k: row.get(k) for k in audit_fields if k != "run_id"}
                row_out["run_id"] = run_id
                w.writerow(row_out)

    result_jsonl = (run_dir / "result.jsonl").open("a")
    try:
        for fixture in fixtures:
            data, gold, meta = _load_fixture(fixture)
            for baseline, rep in product(baselines, range(reps)):
                print(f"[eval] {fixture} / {baseline} / rep{rep} ...",
                      end="", flush=True)
                rr, audit_rows = _run_one(
                    fixture=fixture, baseline=baseline, rep=rep,
                    model=model, max_turns=max_turns, run_dir=run_dir,
                    gold=gold, fixture_data=data, fixture_meta=meta,
                )
                results.append(rr)
                result_jsonl.write(json.dumps(asdict(rr)) + "\n")
                result_jsonl.flush()
                write_audit(audit_rows, rr.run_id)
                ok = "ok" if rr.task_success and not rr.error else "FAIL"
                # `tot` = total tokens the model saw (uncached + cache hits +
                # cache writes). Use this for RQ1 — `in` alone is warped by
                # caching across reps that share a system/user prompt.
                cache_str = (f" cache={rr.cache_read_input_tokens}r/"
                             f"{rr.cache_creation_input_tokens}w"
                             if (rr.cache_read_input_tokens
                                 or rr.cache_creation_input_tokens) else "")
                print(f" {ok}  in={rr.input_tokens}{cache_str} "
                      f"tot={rr.total_input_tokens} out={rr.output_tokens}"
                      f" turns={rr.turns} cost=${rr.estimated_cost_usd:.4f}"
                      f" recall={rr.evidence_recall:.2f}"
                      f"{' BLOCKED='+str(rr.blocked_reads) if rr.blocked_reads else ''}")
    finally:
        result_jsonl.close()

    finished = datetime.now(timezone.utc)
    manifest = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_seconds": (finished - started).total_seconds(),
        "total_runs": len(results),
        "successful": sum(1 for r in results if r.task_success),
        "failed": sum(1 for r in results if not r.task_success),
        "exceptions": sum(1 for r in results if r.error),
        "total_input_tokens_uncached": sum(r.input_tokens for r in results),
        "total_input_tokens_cache_read": sum(
            r.cache_read_input_tokens for r in results),
        "total_input_tokens_cache_creation": sum(
            r.cache_creation_input_tokens for r in results),
        "total_input_tokens_all": sum(r.total_input_tokens for r in results),
        "total_output_tokens": sum(r.output_tokens for r in results),
        "total_estimated_cost_usd": sum(r.estimated_cost_usd for r in results),
        "by_baseline": {
            b: {
                "runs": (n := sum(1 for r in results if r.baseline == b)),
                "avg_input_tokens_uncached": (
                    sum(r.input_tokens for r in results if r.baseline == b)
                    / max(1, n)
                ),
                "avg_total_input_tokens": (
                    sum(r.total_input_tokens for r in results
                        if r.baseline == b) / max(1, n)
                ),
                "avg_output_tokens": (
                    sum(r.output_tokens for r in results if r.baseline == b)
                    / max(1, n)
                ),
                "avg_evidence_recall": (
                    sum(r.evidence_recall for r in results if r.baseline == b)
                    / max(1, n)
                ),
                "task_success_rate": (
                    sum(1 for r in results
                        if r.baseline == b and r.task_success) / max(1, n)
                ),
                "avg_estimated_cost_usd": (
                    sum(r.estimated_cost_usd for r in results
                        if r.baseline == b) / max(1, n)
                ),
            }
            for b in baselines
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return run_dir


def main() -> None:
    p = argparse.ArgumentParser(description="ArtifactStore PLAN §11.1 eval")
    p.add_argument("--fixtures", nargs="+",
                   default=list(FIXTURE_REGISTRY.keys()),
                   choices=list(FIXTURE_REGISTRY.keys()))
    p.add_argument("--baselines", nargs="+",
                   default=list(BASELINES.keys()),
                   choices=list(BASELINES.keys()))
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--model", default="deepseek-v4-pro")
    p.add_argument("--max-turns", type=int, default=10)
    args = p.parse_args()

    run_dir = run_eval(
        fixtures=args.fixtures, baselines=args.baselines,
        reps=args.reps, model=args.model, max_turns=args.max_turns,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    print(f"\n[eval] wrote {run_dir}")
    print(f"[eval] {manifest['successful']}/{manifest['total_runs']} succeeded "
          f"in {manifest['elapsed_seconds']:.1f}s "
          f"(${manifest['total_estimated_cost_usd']:.4f})")


if __name__ == "__main__":
    main()
