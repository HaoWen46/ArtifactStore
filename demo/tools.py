"""Tool surfaces for the demo.

Subagent tools are bound to a grant_id at construction time so the model
never sees a `grant_id` argument — the harness enforces scope, not the LLM.
Every call goes through ArtifactStore.* which logs to artifact_access_log.
"""
from __future__ import annotations

from artifactstore import ArtifactStore
from demo.agent import Tool


# --------- Subagent tools (gated by a single grant) ---------

def subagent_tools(store: ArtifactStore, grant_id: str) -> list[Tool]:
    return [
        Tool(
            name="artifact_search",
            description="Full-text search over artifact previews and span text. "
                        "Returns a list of {artifact_id, type, preview, score}. "
                        "Use this first to locate evidence by keyword.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "artifact_types": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 5},
                    "token_budget": {"type": "integer", "default": 800},
                },
                "required": ["query"],
            },
            fn=lambda **kw: store.search(grant_id=grant_id, **kw),
        ),
        Tool(
            name="artifact_get_spans",
            description="Fetch typed evidence spans (assertion, stack_frame, "
                        "changed_line, error_message, ...) from one artifact.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "span_types": {"type": "array", "items": {"type": "string"}},
                    "token_budget": {"type": "integer", "default": 800},
                },
                "required": ["artifact_id"],
            },
            fn=lambda **kw: store.get_spans(grant_id=grant_id, **kw),
        ),
        Tool(
            name="artifact_expand_view",
            description="Materialize a view of an artifact: 'preview' | 'evidence' "
                        "| 'redacted' | 'raw' | 'provenance'. 'raw' may be denied "
                        "by the grant.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "view": {"type": "string",
                             "enum": ["preview", "evidence", "redacted", "raw", "provenance"]},
                    "token_budget": {"type": "integer", "default": 1500},
                },
                "required": ["artifact_id", "view"],
            },
            fn=lambda **kw: store.expand_view(grant_id=grant_id, **kw),
        ),
        Tool(
            name="artifact_find_related",
            description="Follow provenance/causal links from an artifact "
                        "(caused_by, derived_from, contains_evidence_for, ...).",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "relations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["artifact_id"],
            },
            fn=lambda **kw: store.find_related(grant_id=grant_id, **kw),
        ),
        Tool(
            name="submit_report",
            description="Submit your final diagnosis. Calling this ends your turn. "
                        "Every claim MUST be backed by citations like 'art_xxx/span_y'.",
            input_schema={
                "type": "object",
                "properties": {
                    "diagnosis": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["diagnosis", "citations"],
            },
            fn=lambda **kw: {"submitted": True, **kw},
        ),
    ]


# --------- Supervisor tools (run workloads + delegate) ---------

from dataclasses import asdict
from typing import Callable

from demo.workloads import ViewPolicy, WorkloadResult, run_workload


def supervisor_tools(
    store: ArtifactStore,
    *,
    session_id: str,
    issuer_agent_id: str,
    run_subagent: Callable[[str, str], dict],
    policy: ViewPolicy = ViewPolicy.ARTIFACT,
    live: bool = False,
) -> list[Tool]:
    """Supervisor tool surface.

    `run_subagent(task, grant_id)` is supplied by the runner — the runner owns
    construction of the subagent loop because it knows which model + tool set
    to use. Keeping it as a callback keeps the supervisor harness loop-agnostic.
    """

    def _run_workload(kind: str, target: str) -> dict:
        result: WorkloadResult = run_workload(
            store=store,
            session_id=session_id,
            creator_agent_id=issuer_agent_id,
            kind=kind, target=target,
            policy=policy, live=live,
        )
        # Under ARTIFACT policy the supervisor sees ONLY the handle, never raw.
        # Under RAW/TRUNCATED/SUMMARY it sees the body — that's the eval baseline.
        d = asdict(result); d["policy"] = result.policy.value
        return d

    def _create_grant(subject_agent_id: str, artifact_types: list[str],
                      allowed_views: list[str], allowed_ops: list[str],
                      max_tokens: int = 2500, ttl_seconds: int = 1800,
                      path_prefixes: list[str] | None = None,
                      sensitivity_max: str | None = None) -> dict:
        predicate = {"session_id": session_id, "artifact_types": artifact_types}
        if path_prefixes is not None:
            predicate["path_prefixes"] = path_prefixes
        if sensitivity_max is not None:
            predicate["sensitivity_max"] = sensitivity_max
        grant_id = store.create_grant(
            subject_agent_id=subject_agent_id,
            issuer_agent_id=issuer_agent_id,
            artifact_predicate=predicate,
            allowed_ops=allowed_ops,
            allowed_views=allowed_views,
            max_tokens=max_tokens,
            ttl_seconds=ttl_seconds,
        )
        return {"grant_id": grant_id, "predicate": predicate,
                "allowed_views": allowed_views, "allowed_ops": allowed_ops}

    def _delegate(task: str, grant_id: str) -> dict:
        return run_subagent(task, grant_id)

    def _expand_artifact(artifact_id: str, view: str,
                         token_budget: int = 1500) -> str:
        # Supervisor uses its own implicit grant — eval treats supervisor as
        # trusted for citation verification. (PLAN §20.2: "supervisor verifies
        # every citation".)
        return store.expand_view(artifact_id=artifact_id, grant_id="__supervisor__",
                                 view=view, token_budget=token_budget)

    return [
        Tool(
            name="run_workload",
            description="Run a workload (pytest / grep / git / ...). Under the "
                        "default policy you receive only an artifact handle "
                        "{artifact_id, type, preview}; raw output is stored in "
                        "ArtifactStore. To inspect, mint a grant and delegate, "
                        "or call expand_artifact.",
            input_schema={
                "type": "object",
                "properties": {
                    "kind":   {"type": "string",
                               "enum": ["pytest", "grep", "git", "npm", "docker"]},
                    "target": {"type": "string",
                               "description": "Test path / pattern / commit-ish; "
                                              "fixture lookup key in replay mode."},
                },
                "required": ["kind", "target"],
            },
            fn=_run_workload,
        ),
        Tool(
            name="create_grant",
            description="Mint a scoped grant for a subagent. Be specific: "
                        "narrow artifact_types and avoid 'raw' in allowed_views "
                        "unless required.",
            input_schema={
                "type": "object",
                "properties": {
                    "subject_agent_id": {"type": "string"},
                    "artifact_types":   {"type": "array",
                                         "items": {"type": "string"}},
                    "allowed_views":    {"type": "array",
                                         "items": {"type": "string",
                                                   "enum": ["preview", "evidence",
                                                            "redacted", "raw",
                                                            "provenance"]}},
                    "allowed_ops":      {"type": "array",
                                         "items": {"type": "string",
                                                   "enum": ["search", "get_spans",
                                                            "expand_view",
                                                            "find_related"]}},
                    "max_tokens":       {"type": "integer", "default": 2500},
                    "ttl_seconds":      {"type": "integer", "default": 1800},
                    "path_prefixes":    {"type": "array", "items": {"type": "string"}},
                    "sensitivity_max":  {"type": "string"},
                },
                "required": ["subject_agent_id", "artifact_types",
                             "allowed_views", "allowed_ops"],
            },
            fn=_create_grant,
        ),
        Tool(
            name="delegate",
            description="Hand a focused task to the diagnostic subagent under "
                        "the given grant. The subagent does NOT see your "
                        "transcript. Returns its final report and audit summary.",
            input_schema={
                "type": "object",
                "properties": {
                    "task":     {"type": "string",
                                 "description": "Self-contained problem statement "
                                                "and any artifact handles to inspect."},
                    "grant_id": {"type": "string"},
                },
                "required": ["task", "grant_id"],
            },
            fn=_delegate,
        ),
        Tool(
            name="expand_artifact",
            description="Materialize a view of an artifact. Use this to verify "
                        "the subagent's citations resolve to real evidence. "
                        "Views: preview | evidence | redacted | raw | provenance.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id":  {"type": "string"},
                    "view":         {"type": "string",
                                     "enum": ["preview", "evidence", "redacted",
                                              "raw", "provenance"]},
                    "token_budget": {"type": "integer", "default": 1500},
                },
                "required": ["artifact_id", "view"],
            },
            fn=_expand_artifact,
        ),
    ]
