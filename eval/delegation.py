"""PLAN §11.2 supervisor/subagent delegation eval.

Three delegation strategies, evaluated end-to-end:

  D1 SUMMARY      — supervisor's run_workload returns only a deterministic
                    offline summary; subagent receives summary inline, no tools.
  D2 FULL_CONTEXT — supervisor's run_workload returns the full raw text;
                    subagent receives raw inline, no tools.
  D3 SCOPED       — supervisor's run_workload returns only an artifact_id;
                    supervisor mints a grant; subagent has artifact_* tools
                    under that grant. (This is the demo runner's flow.)

Why this matters separately from §11.1: §11.1 measures single-agent context
strategies. §11.2 measures *delegation* strategies — what the supervisor
keeps in ITS context vs what the subagent gets. The supervisor's context
cost grows with what run_workload returns, so D2 inflates parent input
linearly with fixture size while D3 stays bounded.

Output: same dir layout as §11.1 (eval/runs/<UTC-iso>/...). DelegationResult
captures parent + subagent token counts separately so the writeup can
report both per-role.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Callable

from artifactstore import ArtifactStore
from artifactstore.tokens import estimate
from demo.agent import Agent, ModelConfig, Tool
from demo.runner import _extract_submit_report, load_dotenv

from eval.baselines import _deterministic_summary, _b4_tools
from eval.driver import (
    FIXTURE_REGISTRY,
    PRICE_PER_MTOK,
    RUNS_DIR,
    _git_rev,
    _load_fixture,
)
from eval.metrics import (
    blocked_reads,
    citation_validity,
    evidence_recall,
    extract_citations,
    task_success,
)


SUPERVISOR_DELEGATION_SYSTEM = """\
You are a delegation supervisor. Your job:
  1. Call run_workload to obtain a tool output.
  2. If you got back an artifact handle (artifact_id only), call
     create_grant for the subagent and then delegate(task, grant_id).
     If you got back a summary or raw text inline, just call
     delegate(task) — no grant needed.
  3. Inspect delegate's return value: it has the subagent's diagnosis.
     If diagnosis is non-empty, accept it.
  4. Produce a final answer (text only, end_turn).

Hard rules:
  - Do NOT analyse the tool output yourself. Your job is to delegate.
  - Bias toward minimal context.
  - One delegate call only. Do not retry.
"""

SUBAGENT_NO_TOOLS_SYSTEM = """\
You are a debugging subagent. You receive a task and a context blob
(either a summary of a tool output, or its full raw text). Read the
context, identify the root cause of any failure, and produce a concise
diagnosis (under 200 words). End your turn.

Quote specific lines from the context as evidence. Do not speculate
beyond what the context contains.
"""

SUBAGENT_ARTIFACT_SYSTEM = """\
You are a debugging subagent with access to ArtifactStore via the
artifact_* tools. You receive a task and an artifact_id (handle).

Procedure:
  1. Call artifact_get_spans(artifact_id) to read typed evidence.
  2. If you need broader context, call artifact_search at most once.
  3. As soon as you have evidence sufficient to diagnose the failure,
     end your turn with the diagnosis as plain text under 200 words.

CITATION FORMAT (strict — output is parsed by regex):
  - Every key claim MUST cite at least one span in the FULL form:
      art_<8hex>/span_<8hex>
    Concrete example: art_2c52e1c7/span_d0fc13a8
  - Do NOT cite by span_id alone (e.g. 'span_d0fc13a8'). Always include
    the artifact_id with a slash.
  - A diagnosis without at least one full-form citation will be flagged
    as invalid by the eval grader.
"""


# ---------------------------------------------------------------------------
# DelegationResult — same shape as RunResult but with parent/subagent split.
# ---------------------------------------------------------------------------

@dataclass
class DelegationResult:
    run_id: str
    fixture: str
    strategy: str  # D1_SUMMARY, D2_FULL_CONTEXT, D3_SCOPED
    rep: int
    model: str
    # Parent (supervisor) accounting
    parent_input_tokens: int
    parent_cache_read_input_tokens: int
    parent_cache_creation_input_tokens: int
    parent_total_input_tokens: int
    parent_output_tokens: int
    parent_turns: int
    parent_tool_calls: int
    parent_stop_reason: str
    # Subagent accounting
    sub_input_tokens: int
    sub_cache_read_input_tokens: int
    sub_cache_creation_input_tokens: int
    sub_total_input_tokens: int
    sub_output_tokens: int
    sub_turns: int
    sub_tool_calls: int
    sub_stop_reason: str
    # Combined (for cost / RQ1 framing)
    total_input_tokens: int
    total_output_tokens: int
    # Outcomes
    diagnosis: str
    diagnosis_chars: int
    evidence_recall: float
    task_success: bool
    citation_count: int
    citations_resolved: int
    citation_validity: float
    blocked_reads: int
    # Cost
    elapsed_seconds: float
    estimated_cost_usd: float
    error: str | None = None


# ---------------------------------------------------------------------------
# Per-strategy setup. Each returns a (supervisor_tools, run_subagent_metrics)
# pair where run_subagent_metrics is a dict the supervisor's delegate tool
# populates as a side effect — that's how we capture subagent stats from
# inside the supervisor's loop.
# ---------------------------------------------------------------------------

def _empty_sub_metrics() -> dict:
    return {
        "input_tokens": 0, "cache_read": 0, "cache_creation": 0,
        "total_input": 0, "output_tokens": 0,
        "turns": 0, "tool_calls": 0, "stop_reason": "",
        "diagnosis": "", "submitted": False, "blocked_reads": 0,
    }


def _setup_d1_summary(store: ArtifactStore, fixture_data: str,
                      fixture_meta: dict, model: str,
                      max_turns: int) -> tuple[list[Tool], dict]:
    summary = _deterministic_summary(fixture_data, fixture_meta["kind"])
    sub_metrics = _empty_sub_metrics()

    def _run_workload_d1(kind: str, target: str) -> dict:
        return {
            "policy": "summary",
            "summary": summary,
            "artifact_type": fixture_meta["artifact_type"],
            "raw_token_count": estimate(fixture_data),
        }

    def _delegate_d1(task: str) -> dict:
        sub = Agent(
            name="sub_d1", system=SUBAGENT_NO_TOOLS_SYSTEM, tools=[],
            config=ModelConfig(model=model, max_turns=max_turns),
        )
        user = (f"<task>\n{task}\n</task>\n"
                f"<summary>\n{summary}\n</summary>")
        r = sub.run(user)
        sub_metrics.update({
            "input_tokens": r.input_tokens,
            "cache_read": r.cache_read_input_tokens,
            "cache_creation": r.cache_creation_input_tokens,
            "total_input": r.total_input_tokens,
            "output_tokens": r.output_tokens,
            "turns": r.turns, "tool_calls": r.tool_calls,
            "stop_reason": r.stop_reason,
            "diagnosis": r.final_text,
            "submitted": bool(r.final_text.strip()),
            "blocked_reads": 0,
        })
        return {"diagnosis": r.final_text,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens}

    tools = [
        Tool(name="run_workload",
             description="Run a workload and get back a deterministic SUMMARY "
                         "of its output (lossy). The summary is inlined into "
                         "your context.",
             input_schema={"type": "object",
                           "properties": {"kind": {"type": "string"},
                                          "target": {"type": "string"}},
                           "required": ["kind", "target"]},
             fn=_run_workload_d1),
        Tool(name="delegate",
             description="Spawn the subagent with the summary in your context "
                         "and a focused task. Returns the subagent's diagnosis.",
             input_schema={"type": "object",
                           "properties": {"task": {"type": "string"}},
                           "required": ["task"]},
             fn=_delegate_d1),
    ]
    return tools, sub_metrics


def _setup_d2_full(store: ArtifactStore, fixture_data: str,
                   fixture_meta: dict, model: str,
                   max_turns: int) -> tuple[list[Tool], dict]:
    sub_metrics = _empty_sub_metrics()

    def _run_workload_d2(kind: str, target: str) -> dict:
        return {
            "policy": "raw",
            "body": fixture_data,
            "artifact_type": fixture_meta["artifact_type"],
            "raw_token_count": estimate(fixture_data),
        }

    def _delegate_d2(task: str) -> dict:
        sub = Agent(
            name="sub_d2", system=SUBAGENT_NO_TOOLS_SYSTEM, tools=[],
            config=ModelConfig(model=model, max_turns=max_turns),
        )
        user = (f"<task>\n{task}\n</task>\n"
                f"<output>\n{fixture_data}\n</output>")
        r = sub.run(user)
        sub_metrics.update({
            "input_tokens": r.input_tokens,
            "cache_read": r.cache_read_input_tokens,
            "cache_creation": r.cache_creation_input_tokens,
            "total_input": r.total_input_tokens,
            "output_tokens": r.output_tokens,
            "turns": r.turns, "tool_calls": r.tool_calls,
            "stop_reason": r.stop_reason,
            "diagnosis": r.final_text,
            "submitted": bool(r.final_text.strip()),
            "blocked_reads": 0,
        })
        return {"diagnosis": r.final_text,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens}

    tools = [
        Tool(name="run_workload",
             description="Run a workload and get back its FULL raw output. "
                         "The raw text is inlined into your context.",
             input_schema={"type": "object",
                           "properties": {"kind": {"type": "string"},
                                          "target": {"type": "string"}},
                           "required": ["kind", "target"]},
             fn=_run_workload_d2),
        Tool(name="delegate",
             description="Spawn the subagent with the raw output in your "
                         "context and a focused task. Returns subagent's "
                         "diagnosis.",
             input_schema={"type": "object",
                           "properties": {"task": {"type": "string"}},
                           "required": ["task"]},
             fn=_delegate_d2),
    ]
    return tools, sub_metrics


def _setup_d3_scoped(store: ArtifactStore, fixture_data: str,
                     fixture_meta: dict, model: str,
                     max_turns: int) -> tuple[list[Tool], dict]:
    sub_metrics = _empty_sub_metrics()
    artifact_id_box: dict = {"id": None}
    grant_id_box: dict = {"id": None}

    def _run_workload_d3(kind: str, target: str) -> dict:
        aid = store.put_artifact(
            tool_name=fixture_meta["kind"],
            artifact_type=fixture_meta["artifact_type"],
            raw_text=fixture_data,
            creator_agent_id="eval_supervisor",
            session_id="eval_d3",
            metadata={"target": fixture_meta["target"]},
        )
        artifact_id_box["id"] = aid
        row = store.conn.execute(
            "SELECT preview, token_count FROM artifacts WHERE artifact_id = ?",
            (aid,),
        ).fetchone()
        return {
            "policy": "artifact",
            "artifact_id": aid,
            "artifact_type": fixture_meta["artifact_type"],
            "preview": row["preview"],
            "raw_token_count": row["token_count"],
        }

    def _create_grant(subject_agent_id: str,
                      artifact_types: list[str],
                      allowed_views: list[str],
                      allowed_ops: list[str],
                      max_tokens: int = 4000,
                      ttl_seconds: int = 600) -> dict:
        gid = store.create_grant(
            subject_agent_id=subject_agent_id,
            issuer_agent_id="eval_supervisor",
            artifact_predicate={"session_id": "eval_d3",
                                "artifact_types": artifact_types},
            allowed_ops=allowed_ops,
            allowed_views=allowed_views,
            max_tokens=max_tokens, ttl_seconds=ttl_seconds,
        )
        grant_id_box["id"] = gid
        return {"grant_id": gid, "allowed_views": allowed_views,
                "allowed_ops": allowed_ops}

    def _delegate_d3(task: str, grant_id: str) -> dict:
        sub = Agent(
            name="sub_d3", system=SUBAGENT_ARTIFACT_SYSTEM,
            tools=_b4_tools(store, grant_id),
            config=ModelConfig(model=model, max_turns=max_turns),
        )
        artifact_id = artifact_id_box["id"]
        user = (f"<task>\n{task}\n</task>\n"
                f"<artifact_id>{artifact_id}</artifact_id>")
        r = sub.run(user)
        # Count denials accrued under THIS grant.
        audit = store.audit(grant_id)
        denials = sum(1 for a in audit if a["allowed"] in (0, False))
        sub_metrics.update({
            "input_tokens": r.input_tokens,
            "cache_read": r.cache_read_input_tokens,
            "cache_creation": r.cache_creation_input_tokens,
            "total_input": r.total_input_tokens,
            "output_tokens": r.output_tokens,
            "turns": r.turns, "tool_calls": r.tool_calls,
            "stop_reason": r.stop_reason,
            "diagnosis": r.final_text,
            "submitted": bool(r.final_text.strip()),
            "blocked_reads": denials,
        })
        return {"diagnosis": r.final_text,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens}

    tools = [
        Tool(name="run_workload",
             description="Run a workload. You will receive ONLY an artifact "
                         "handle ({artifact_id, type, preview}); raw bytes "
                         "stay in ArtifactStore. To inspect, mint a grant and "
                         "delegate.",
             input_schema={"type": "object",
                           "properties": {"kind": {"type": "string"},
                                          "target": {"type": "string"}},
                           "required": ["kind", "target"]},
             fn=_run_workload_d3),
        Tool(name="create_grant",
             description="Mint a scoped grant for the subagent. Be specific.",
             input_schema={"type": "object",
                           "properties": {
                               "subject_agent_id": {"type": "string"},
                               "artifact_types": {"type": "array",
                                                  "items": {"type": "string"}},
                               "allowed_views": {"type": "array",
                                                 "items": {"type": "string",
                                                           "enum": ["preview", "evidence", "redacted", "raw", "provenance"]}},
                               "allowed_ops": {"type": "array",
                                               "items": {"type": "string",
                                                         "enum": ["search", "get_spans", "expand_view", "find_related"]}},
                               "max_tokens": {"type": "integer", "default": 4000},
                               "ttl_seconds": {"type": "integer", "default": 600},
                           },
                           "required": ["subject_agent_id", "artifact_types",
                                        "allowed_views", "allowed_ops"]},
             fn=_create_grant),
        Tool(name="delegate",
             description="Spawn the subagent with the artifact handle and "
                         "the given grant. Subagent will use artifact_* tools.",
             input_schema={"type": "object",
                           "properties": {"task": {"type": "string"},
                                          "grant_id": {"type": "string"}},
                           "required": ["task", "grant_id"]},
             fn=_delegate_d3),
    ]
    return tools, sub_metrics


STRATEGIES: dict[str, Callable[..., tuple[list[Tool], dict]]] = {
    "D1_SUMMARY": _setup_d1_summary,
    "D2_FULL_CONTEXT": _setup_d2_full,
    "D3_SCOPED": _setup_d3_scoped,
}


# ---------------------------------------------------------------------------
# Per-run execution
# ---------------------------------------------------------------------------

def _run_one(*, fixture: str, strategy: str, rep: int, model: str,
             max_turns: int, run_dir: Path,
             gold: dict, fixture_data: str, fixture_meta: dict
             ) -> DelegationResult:
    run_id = f"{fixture}__{strategy}__rep{rep}"
    db_path = run_dir / f"db_{run_id}.sqlite"
    store = ArtifactStore.init(db_path)
    setup_fn = STRATEGIES[strategy]
    sup_tools, sub_metrics = setup_fn(
        store, fixture_data, fixture_meta, model, max_turns,
    )
    sup = Agent(
        name="sup",
        system=SUPERVISOR_DELEGATION_SYSTEM,
        tools=sup_tools,
        config=ModelConfig(model=model, max_turns=max_turns),
    )
    user = (f"Diagnose the root cause of any failure in the "
            f"{fixture_meta['kind']} workload for target "
            f"'{fixture_meta['target']}'. Delegate the diagnosis to a "
            f"subagent and produce a final answer.")

    started = time.time()
    error: str | None = None
    parent_in = parent_out = parent_cr = parent_cc = 0
    parent_tot = parent_turns = parent_calls = 0
    parent_stop = "exception"
    try:
        pr = sup.run(user)
        parent_in = pr.input_tokens
        parent_cr = pr.cache_read_input_tokens
        parent_cc = pr.cache_creation_input_tokens
        parent_tot = pr.total_input_tokens
        parent_out = pr.output_tokens
        parent_turns = pr.turns
        parent_calls = pr.tool_calls
        parent_stop = pr.stop_reason
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - started

    # The diagnosis is the SUBAGENT's output (it's what we evaluate).
    diagnosis = sub_metrics.get("diagnosis", "") or ""

    citations = extract_citations(diagnosis)
    cit_resolved, cit_valid = citation_validity(citations, store.conn)

    rates = PRICE_PER_MTOK.get(model, {"input": 0.0, "output": 0.0})
    cost = (
        (parent_in + sub_metrics["input_tokens"]
         + parent_cc + sub_metrics["cache_creation"]) * rates["input"]
        + (parent_cr + sub_metrics["cache_read"]) * rates["input"] * 0.1
        + (parent_out + sub_metrics["output_tokens"]) * rates["output"]
    ) / 1_000_000

    return DelegationResult(
        run_id=run_id,
        fixture=fixture,
        strategy=strategy,
        rep=rep,
        model=model,
        parent_input_tokens=parent_in,
        parent_cache_read_input_tokens=parent_cr,
        parent_cache_creation_input_tokens=parent_cc,
        parent_total_input_tokens=parent_tot,
        parent_output_tokens=parent_out,
        parent_turns=parent_turns,
        parent_tool_calls=parent_calls,
        parent_stop_reason=parent_stop,
        sub_input_tokens=sub_metrics["input_tokens"],
        sub_cache_read_input_tokens=sub_metrics["cache_read"],
        sub_cache_creation_input_tokens=sub_metrics["cache_creation"],
        sub_total_input_tokens=sub_metrics["total_input"],
        sub_output_tokens=sub_metrics["output_tokens"],
        sub_turns=sub_metrics["turns"],
        sub_tool_calls=sub_metrics["tool_calls"],
        sub_stop_reason=sub_metrics["stop_reason"],
        total_input_tokens=parent_tot + sub_metrics["total_input"],
        total_output_tokens=parent_out + sub_metrics["output_tokens"],
        diagnosis=diagnosis,
        diagnosis_chars=len(diagnosis),
        evidence_recall=evidence_recall(diagnosis, gold),
        task_success=task_success(diagnosis, gold),
        citation_count=len(citations),
        citations_resolved=cit_resolved,
        citation_validity=cit_valid,
        blocked_reads=sub_metrics["blocked_reads"],
        elapsed_seconds=elapsed,
        estimated_cost_usd=cost,
        error=error,
    )


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def run_eval(*, fixtures: list[str], strategies: list[str], reps: int,
             model: str, max_turns: int = 10,
             output_root: Path = RUNS_DIR) -> Path:
    load_dotenv(override=True)
    started = datetime.now(timezone.utc)
    run_dir = output_root / f"delegation_{started.strftime('%Y-%m-%dT%H-%M-%SZ')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "mode": "delegation",
        "started_at": started.isoformat(),
        "model": model,
        "base_url": os.environ.get("ANTHROPIC_BASE_URL") or "(SDK default)",
        "git_rev": _git_rev(),
        "fixtures": fixtures,
        "strategies": strategies,
        "reps": reps,
        "max_turns": max_turns,
        "fixture_registry": {k: FIXTURE_REGISTRY[k] for k in fixtures},
    }
    (run_dir / "config.json").write_text(json.dumps(config, indent=2))

    results: list[DelegationResult] = []
    result_jsonl = (run_dir / "result.jsonl").open("a")
    try:
        for fixture in fixtures:
            data, gold, meta = _load_fixture(fixture)
            for strategy, rep in product(strategies, range(reps)):
                print(f"[deleg] {fixture} / {strategy} / rep{rep} ...",
                      end="", flush=True)
                rr = _run_one(
                    fixture=fixture, strategy=strategy, rep=rep,
                    model=model, max_turns=max_turns, run_dir=run_dir,
                    gold=gold, fixture_data=data, fixture_meta=meta,
                )
                results.append(rr)
                result_jsonl.write(json.dumps(asdict(rr)) + "\n")
                result_jsonl.flush()
                ok = "ok" if rr.task_success and not rr.error else "FAIL"
                print(f" {ok}  parent={rr.parent_total_input_tokens}/"
                      f"{rr.parent_output_tokens} "
                      f"sub={rr.sub_total_input_tokens}/"
                      f"{rr.sub_output_tokens} "
                      f"total_in={rr.total_input_tokens} "
                      f"cost=${rr.estimated_cost_usd:.4f} "
                      f"recall={rr.evidence_recall:.2f} "
                      f"cite={rr.citation_count}/{rr.citations_resolved}"
                      f"{' BLOCKED='+str(rr.blocked_reads) if rr.blocked_reads else ''}")
    finally:
        result_jsonl.close()

    finished = datetime.now(timezone.utc)
    manifest = {
        "mode": "delegation",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_seconds": (finished - started).total_seconds(),
        "total_runs": len(results),
        "successful": sum(1 for r in results if r.task_success),
        "failed": sum(1 for r in results if not r.task_success),
        "exceptions": sum(1 for r in results if r.error),
        "total_parent_input_tokens": sum(r.parent_total_input_tokens for r in results),
        "total_sub_input_tokens": sum(r.sub_total_input_tokens for r in results),
        "total_output_tokens": sum(r.total_output_tokens for r in results),
        "total_estimated_cost_usd": sum(r.estimated_cost_usd for r in results),
        "by_strategy": {
            s: {
                "runs": (n := sum(1 for r in results if r.strategy == s)),
                "avg_parent_total_input": (
                    sum(r.parent_total_input_tokens for r in results
                        if r.strategy == s) / max(1, n)
                ),
                "avg_sub_total_input": (
                    sum(r.sub_total_input_tokens for r in results
                        if r.strategy == s) / max(1, n)
                ),
                "avg_total_input": (
                    sum(r.total_input_tokens for r in results
                        if r.strategy == s) / max(1, n)
                ),
                "avg_total_output": (
                    sum(r.total_output_tokens for r in results
                        if r.strategy == s) / max(1, n)
                ),
                "avg_evidence_recall": (
                    sum(r.evidence_recall for r in results if r.strategy == s)
                    / max(1, n)
                ),
                "task_success_rate": (
                    sum(1 for r in results
                        if r.strategy == s and r.task_success) / max(1, n)
                ),
                "avg_citations": (
                    sum(r.citation_count for r in results if r.strategy == s)
                    / max(1, n)
                ),
                "avg_citation_validity": (
                    sum(r.citation_validity for r in results if r.strategy == s)
                    / max(1, n)
                ),
                "avg_estimated_cost_usd": (
                    sum(r.estimated_cost_usd for r in results if r.strategy == s)
                    / max(1, n)
                ),
            }
            for s in strategies
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return run_dir


def main() -> None:
    p = argparse.ArgumentParser(description="ArtifactStore PLAN §11.2 eval")
    p.add_argument("--fixtures", nargs="+",
                   default=list(FIXTURE_REGISTRY.keys()),
                   choices=list(FIXTURE_REGISTRY.keys()))
    p.add_argument("--strategies", nargs="+",
                   default=list(STRATEGIES.keys()),
                   choices=list(STRATEGIES.keys()))
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--model", default="deepseek-v4-pro")
    p.add_argument("--max-turns", type=int, default=10)
    args = p.parse_args()

    run_dir = run_eval(
        fixtures=args.fixtures, strategies=args.strategies,
        reps=args.reps, model=args.model, max_turns=args.max_turns,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    print(f"\n[deleg] wrote {run_dir}")
    print(f"[deleg] {manifest['successful']}/{manifest['total_runs']} succeeded "
          f"in {manifest['elapsed_seconds']:.1f}s "
          f"(${manifest['total_estimated_cost_usd']:.4f})")


if __name__ == "__main__":
    main()
