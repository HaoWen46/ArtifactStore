"""Offline supervisor↔subagent end-to-end test.

Drives the same agent loop the live demo uses, but with a scripted Anthropic
client so the test is deterministic and runs without an API key. Verifies:

  - supervisor's run_workload writes an artifact and returns only the handle
    (no raw bytes leak into the supervisor's transcript)
  - supervisor mints a scoped grant
  - delegate() spawns the subagent loop, which uses search → get_spans →
    submit_report against the store
  - submit_report's citations resolve under the grant
  - the supervisor verifies citations via expand_artifact (its own grant)
  - the audit log records every read attempt — allowed and denied — under
    each grant separately

This is the wiring the live `python -m demo.runner` exercises end-to-end.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from artifactstore import ArtifactStore
from artifactstore.cite import verify_resolves
from demo.agent import Agent
from demo.prompts import SUBAGENT_SYSTEM, SUPERVISOR_SYSTEM
from demo.runner import _make_run_subagent
from demo.tools import supervisor_tools
from demo.workloads import ViewPolicy

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


# ---------------------------------------------------------------------------
# Minimal Anthropic SDK stub. Only the attributes our agent loop reads:
#   resp.content[i].type / .text / .id / .name / .input
#   resp.stop_reason
#   resp.usage.input_tokens / .output_tokens
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Usage:
    input_tokens = 100
    output_tokens = 50


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str = "tool_use"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


def _tool_use(name: str, input_: dict, *, idx: int = 0) -> _Block:
    return _Block(type="tool_use", id=f"toolu_{name}_{idx}", name=name,
                  input=input_)


def _text(text: str) -> _Block:
    return _Block(type="text", text=text)


Move = Callable[[list[dict], list[dict]], _Response]


class ScriptedClient:
    """Plays back a fixed sequence of moves, one per messages.create call.

    Each move receives (messages, tools) and returns a _Response. Moves can
    inspect prior tool_results to extract IDs.
    """

    def __init__(self, moves: list[Move]):
        self._moves = list(moves)
        self._idx = 0

    @property
    def messages(self) -> "ScriptedClient":
        return self

    def create(self, **kw: Any) -> _Response:
        if self._idx >= len(self._moves):
            raise RuntimeError(
                f"ScriptedClient out of moves at turn {self._idx}; "
                f"the agent loop went further than the script."
            )
        move = self._moves[self._idx]
        self._idx += 1
        return move(kw["messages"], kw.get("tools", []))


# ---------------------------------------------------------------------------
# Helpers for moves to introspect the conversation.
# ---------------------------------------------------------------------------

def _last_tool_result(messages: list[dict]) -> Any:
    """Return the parsed JSON payload of the most recent tool_result block.
    Tool results are stringified by demo/agent.py via json.dumps for non-str
    returns, so we round-trip through json.loads here.
    """
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", [])
        if not isinstance(content, list):
            continue
        for b in reversed(content):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                raw = b["content"]
                try:
                    return json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    return raw
    return None


def _scan_tool_results(messages: list[dict]) -> list[Any]:
    """All parsed tool_result payloads, oldest → newest."""
    out: list[Any] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", [])
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                try:
                    out.append(json.loads(b["content"]))
                except (TypeError, json.JSONDecodeError):
                    out.append(b["content"])
    return out


# ---------------------------------------------------------------------------
# Subagent script: search → get_spans → submit_report → end_turn
# ---------------------------------------------------------------------------

def sub_search(_messages, _tools):
    return _Response([_tool_use(
        "artifact_search",
        {"query": "expired token", "token_budget": 800},
        idx=0,
    )])


def sub_get_spans(messages, _tools):
    rows = _last_tool_result(messages)  # search returned a list
    assert isinstance(rows, list) and rows, "search returned no hits"
    art_id = rows[0]["artifact_id"]
    return _Response([_tool_use(
        "artifact_get_spans",
        {"artifact_id": art_id,
         "span_types": ["assertion", "log_warning"],
         "token_budget": 800},
        idx=1,
    )])


def sub_submit(messages, _tools):
    """Walk *all* prior tool_results (skipping is_error stubs) so the move
    works whether or not the subagent attempted a denied view in between."""
    spans: list | None = None
    art_id: str | None = None
    for payload in _scan_tool_results(messages):
        if not isinstance(payload, list) or not payload:
            continue
        if not isinstance(payload[0], dict):
            continue
        if "span_id" in payload[0]:
            spans = payload
        elif "artifact_id" in payload[0]:
            art_id = payload[0]["artifact_id"]
    assert spans, "no span list found in prior tool_results"
    assert art_id, "no search result found in prior tool_results"
    cites = [f"{art_id}/{s['span_id']}" for s in spans[:2]]
    return _Response([_tool_use(
        "submit_report",
        {
            "diagnosis": "Token validator compares naive local clock against "
                          "tz-aware UTC. The log line shows now=local exp=UTC.",
            "citations": cites,
            "confidence": 0.92,
        },
        idx=2,
    )])


def sub_finalize(_messages, _tools):
    return _Response(
        [_text("Submitted.")],
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# Supervisor script: run_workload → create_grant → delegate → expand_artifact
#                     (verify) → end_turn
# ---------------------------------------------------------------------------

def sup_run_workload(_messages, _tools):
    return _Response([_tool_use(
        "run_workload",
        {"kind": "pytest", "target": "auth_expiry"},
        idx=0,
    )])


def sup_create_grant(_messages, _tools):
    return _Response([_tool_use(
        "create_grant",
        {
            "subject_agent_id": "diagnostic_worker",
            "artifact_types": ["pytest_failure"],
            "allowed_views": ["preview", "evidence"],
            "allowed_ops": ["search", "get_spans", "expand_view"],
            "max_tokens": 2500,
            "ttl_seconds": 600,
        },
        idx=1,
    )])


def sup_delegate(messages, _tools):
    grant_payload = _last_tool_result(messages)
    assert isinstance(grant_payload, dict)
    return _Response([_tool_use(
        "delegate",
        {"task": "Diagnose why test_auth_expiry fails. Cite spans.",
         "grant_id": grant_payload["grant_id"]},
        idx=2,
    )])


def sup_verify(messages, _tools):
    delegate_result = _last_tool_result(messages)
    assert isinstance(delegate_result, dict)
    cites = delegate_result.get("citations") or []
    assert cites, "subagent returned no citations"
    art_id, _ = cites[0].split("/", 1)
    return _Response([_tool_use(
        "expand_artifact",
        {"artifact_id": art_id, "view": "evidence", "token_budget": 800},
        idx=3,
    )])


def sup_finalize(_messages, _tools):
    return _Response(
        [_text("Root cause: naive local-clock comparison against tz-aware UTC "
                "expiry. Fix: datetime.now(timezone.utc) in app/auth.py.")],
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_demo_supervisor_subagent_e2e(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")

    sup_client = ScriptedClient([
        sup_run_workload, sup_create_grant, sup_delegate,
        sup_verify, sup_finalize,
    ])
    sub_client = ScriptedClient([
        sub_search, sub_get_spans, sub_submit, sub_finalize,
    ])

    run_subagent = _make_run_subagent(store, model="test-model",
                                       verbose=False, client=sub_client)

    sup = Agent(
        name="supervisor",
        system=SUPERVISOR_SYSTEM,
        tools=supervisor_tools(
            store,
            session_id="test_e2e",
            issuer_agent_id="supervisor",
            run_subagent=run_subagent,
            policy=ViewPolicy.ARTIFACT,
        ),
        client=sup_client,
    )

    result = sup.run("Diagnose the failing pytest run.")

    # 1. The supervisor terminates cleanly with a text response.
    assert result.stop_reason == "end_turn"
    assert "datetime.now(timezone.utc)" in result.final_text

    # 2. Exactly one artifact was created — the pytest log.
    arts = store.conn.execute(
        "SELECT artifact_id, artifact_type, raw_blob "
        "FROM artifacts"
    ).fetchall()
    assert len(arts) == 1
    art = arts[0]
    assert art["artifact_type"] == "pytest_failure"
    assert "token expired prematurely" in art["raw_blob"]

    # 3. Exactly one new grant was minted (besides the seeded supervisor).
    grants = store.conn.execute(
        "SELECT grant_id, subject_agent_id, allowed_views FROM artifact_grants "
        "WHERE grant_id != '__supervisor__'"
    ).fetchall()
    assert len(grants) == 1
    g = grants[0]
    assert g["subject_agent_id"] == "diagnostic_worker"
    # 'raw' was deliberately not granted.
    assert "raw" not in g["allowed_views"]

    # 4. The audit log under the worker grant shows search + get_spans.
    audit = store.audit(g["grant_id"])
    ops = {r["operation"] for r in audit}
    assert {"search", "get_spans"} <= ops, audit
    assert all(r["allowed"] in (1, True) for r in audit), \
        "subagent had a denied read it shouldn't have"

    # 5. The supervisor's __supervisor__ grant logs the verification expand_view.
    sup_audit = store.audit("__supervisor__")
    assert any(r["operation"] == "expand_view" and r["view"] == "evidence"
               for r in sup_audit)


def test_demo_citations_resolve(tmp_path: Path):
    """Re-run the same script and verify every citation resolves under the
    seeded supervisor grant. This is the demo's correctness invariant: a
    report with unresolvable citations is invalid (PLAN §20.2)."""
    store = ArtifactStore.init(tmp_path / "store.db")

    captured: dict = {}

    def capturing_run_subagent(task: str, grant_id: str) -> dict:
        run = _make_run_subagent(store, "test-model", False,
                                  client=ScriptedClient([
                                      sub_search, sub_get_spans,
                                      sub_submit, sub_finalize,
                                  ]))
        out = run(task, grant_id)
        captured.update(out)
        return out

    sup = Agent(
        name="supervisor",
        system=SUPERVISOR_SYSTEM,
        tools=supervisor_tools(
            store, session_id="test_cite",
            issuer_agent_id="supervisor",
            run_subagent=capturing_run_subagent,
            policy=ViewPolicy.ARTIFACT,
        ),
        client=ScriptedClient([
            sup_run_workload, sup_create_grant, sup_delegate,
            sup_verify, sup_finalize,
        ]),
    )
    sup.run("Diagnose.")

    cites = captured["citations"]
    assert cites, "subagent submitted no citations"
    for c in cites:
        assert verify_resolves(store.conn, c), \
            f"citation does not resolve: {c}"


def test_supervisor_does_not_see_raw_under_artifact_policy(tmp_path: Path):
    """Under ViewPolicy.ARTIFACT (the demo default), run_workload returns ONLY
    {artifact_id, type, preview, ...} — never the raw log. This is the
    project's central thesis encoded as a tool boundary."""
    from demo.workloads import run_workload

    store = ArtifactStore.init(tmp_path / "store.db")
    result = run_workload(
        store=store, session_id="test_thesis",
        creator_agent_id="supervisor",
        kind="pytest", target="auth_expiry",
        policy=ViewPolicy.ARTIFACT,
    )
    assert result.body is None, \
        "ViewPolicy.ARTIFACT must not return raw bytes to the supervisor"
    assert result.artifact_id and result.artifact_id.startswith("art_")
    assert result.preview and result.preview.startswith("[pytest_failure")


def test_supervisor_sees_raw_under_raw_policy(tmp_path: Path):
    """Sanity check the B1 baseline: under RAW the body comes through."""
    from demo.workloads import run_workload

    store = ArtifactStore.init(tmp_path / "store.db")
    result = run_workload(
        store=store, session_id="test_b1",
        creator_agent_id="supervisor",
        kind="pytest", target="auth_expiry",
        policy=ViewPolicy.RAW,
    )
    assert result.body and "token expired prematurely" in result.body
    assert result.artifact_id is None


# ---------------------------------------------------------------------------
# Delegate fail-loud: when a subagent runs out of turns without calling
# submit_report, the run_subagent adapter must report submitted=False with a
# useful error. The supervisor's prompt then tells it not to compensate.
# ---------------------------------------------------------------------------

def sub_loop_forever(_messages, _tools):
    """A subagent move that always calls artifact_search — never submits.
    Combined with a low max_turns, exercises the delegate-fail-loud path."""
    return _Response([_tool_use(
        "artifact_search",
        {"query": "anything", "token_budget": 100},
    )])


def test_delegate_reports_no_submit(tmp_path: Path):
    from demo.agent import ModelConfig
    from demo.runner import _make_run_subagent

    store = ArtifactStore.init(tmp_path / "store.db")
    # Pre-populate so search returns SOMETHING (else the harness throws on
    # the first tool call before we hit max_turns).
    store.put_artifact(
        tool_name="pytest", artifact_type="pytest_failure",
        raw_text="boom", creator_agent_id="t", session_id="test_loop",
    )
    grant_id = store.create_grant(
        subject_agent_id="loopy", issuer_agent_id="supervisor",
        artifact_predicate={"session_id": "test_loop"},
        allowed_ops=["search", "get_spans", "expand_view"],
        allowed_views=["preview"],
        max_tokens=500, ttl_seconds=600,
    )
    # 6 looping moves; max_turns=5 means we never reach submit_report.
    sub_client = ScriptedClient([sub_loop_forever] * 8)
    # Patch ModelConfig default by passing client directly to _make_run_subagent.
    # We can't override max_turns through that helper today — but the helper's
    # Agent uses ModelConfig(model=...), so the default max_turns=10 applies.
    # Drop max_turns by monkeypatching ModelConfig if needed; here we just
    # provide enough loops to exceed default 10.
    sub_client = ScriptedClient([sub_loop_forever] * 12)
    run = _make_run_subagent(store, "test", verbose=False, client=sub_client)
    out = run("loop forever", grant_id)
    assert out["submitted"] is False
    assert out["error"] and "did not call submit_report" in out["error"]
    assert out["citations"] == []
    assert out["stop_reason"] == "max_turns"
    # Audit log still recorded the search calls.
    assert any(r["operation"] == "search" for r in out["audit"])


# ---------------------------------------------------------------------------
# Adversarial path: subagent attempts a view its grant doesn't allow. The
# harness must (a) NOT propagate the AccessDenied as a Python exception into
# the model loop — instead pack is_error into the tool_result — and (b) write
# the denial to the audit log. This is the RQ4 measurement scenario.
# ---------------------------------------------------------------------------

def sub_attempt_raw(messages, _tools):
    """After get_spans, try to expand the raw view (which the grant forbids)."""
    rows = _last_tool_result(messages)
    # The previous tool_result was the spans list; pull artifact_id earlier.
    art_id = None
    for payload in _scan_tool_results(messages):
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            if "artifact_id" in payload[0]:
                art_id = payload[0]["artifact_id"]
                break
    assert art_id is not None
    return _Response([_tool_use(
        "artifact_expand_view",
        {"artifact_id": art_id, "view": "raw", "token_budget": 1500},
        idx=10,
    )])


def test_subagent_raw_denied_logged(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")

    sub_client = ScriptedClient([
        sub_search, sub_get_spans, sub_attempt_raw, sub_submit, sub_finalize,
    ])
    run_subagent = _make_run_subagent(store, "test-model", False,
                                       client=sub_client)

    sup_client = ScriptedClient([
        sup_run_workload, sup_create_grant, sup_delegate,
        sup_verify, sup_finalize,
    ])
    sup = Agent(
        name="supervisor",
        system=SUPERVISOR_SYSTEM,
        tools=supervisor_tools(
            store, session_id="test_deny",
            issuer_agent_id="supervisor",
            run_subagent=run_subagent,
            policy=ViewPolicy.ARTIFACT,
        ),
        client=sup_client,
    )
    result = sup.run("Diagnose.")
    assert result.stop_reason == "end_turn"

    # Find the worker grant (the non-supervisor one).
    grant_row = store.conn.execute(
        "SELECT grant_id FROM artifact_grants "
        "WHERE grant_id != '__supervisor__' LIMIT 1"
    ).fetchone()
    audit = store.audit(grant_row["grant_id"])

    # The raw attempt must show up as allowed=0 with a useful denial reason.
    denials = [r for r in audit if r["allowed"] in (0, False)]
    assert denials, "no denials logged despite raw-view attempt"
    assert any(r["view"] == "raw" and "raw" in (r["denial_reason"] or "")
               for r in denials)

    # And the subagent still completed: a submit_report eventually went
    # through (the model in our script "recovers" from the denial).
    grants = store.conn.execute(
        "SELECT grant_id FROM artifact_grants WHERE grant_id != '__supervisor__'"
    ).fetchall()
    # Exactly one worker grant (no leakage of grants per attempt).
    assert len(grants) == 1
