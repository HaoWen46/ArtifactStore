"""Demo entrypoint: supervisor runs a workload, mints a grant, delegates to a
subagent. Replay-mode by default — uses fixtures from eval/fixtures/.

Wire-up depends on ArtifactStore.put_artifact / create_grant / search /
get_spans / expand_view / audit being implemented (build steps 1-7 in PLAN §13).
"""
from __future__ import annotations

import argparse

from artifactstore import ArtifactStore
from demo.agent import Agent, ModelConfig
from demo.prompts import SUBAGENT_SYSTEM, SUPERVISOR_SYSTEM
from demo.tools import subagent_tools, supervisor_tools
from demo.workloads import ViewPolicy


def _make_run_subagent(store: ArtifactStore, model: str, verbose: bool):
    def run_subagent(task: str, grant_id: str) -> dict:
        sub = Agent(
            name="subagent",
            system=SUBAGENT_SYSTEM,
            tools=subagent_tools(store, grant_id),
            config=ModelConfig(model=model),
            verbose=verbose,
            force_terminator="submit_report",
        )
        r = sub.run(task)
        return {
            "report": r.final_text,
            "stop_reason": r.stop_reason,
            "turns": r.turns,
            "tool_calls": r.tool_calls,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "audit": store.audit(grant_id),
        }
    return run_subagent


def demo(*, db: str, kind: str, target: str, model: str,
         policy: ViewPolicy, verbose: bool) -> None:
    store = ArtifactStore.init(db)

    sup = Agent(
        name="supervisor",
        system=SUPERVISOR_SYSTEM,
        tools=supervisor_tools(
            store,
            session_id="demo",
            issuer_agent_id="supervisor",
            run_subagent=_make_run_subagent(store, model, verbose),
            policy=policy,
        ),
        config=ModelConfig(model=model),
        verbose=verbose,
    )

    task = (
        f"Run the {kind} workload on target '{target}'. Identify the root cause "
        f"of any failure. Delegate the diagnosis to a subagent under the "
        f"narrowest grant possible. Verify every citation in the subagent's "
        f"report by calling expand_artifact. Produce a final answer."
    )
    result = sup.run(task)

    print("\n=== supervisor final ===")
    print(result.final_text)
    print(f"\nturns={result.turns} tool_calls={result.tool_calls} "
          f"in={result.input_tokens} out={result.output_tokens}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="demo.db")
    p.add_argument("--kind", default="pytest")
    p.add_argument("--target", default="auth_expiry")
    p.add_argument("--model", default="claude-sonnet-4-5")
    p.add_argument("--policy",
                   choices=[v.value for v in ViewPolicy],
                   default=ViewPolicy.ARTIFACT.value)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    demo(db=args.db, kind=args.kind, target=args.target,
         model=args.model, policy=ViewPolicy(args.policy),
         verbose=args.verbose)


if __name__ == "__main__":
    main()
