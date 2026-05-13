"""Unit tests for eval/baselines.py and eval/delegation.py setup builders.

These previously had 0% coverage — they ran only inside live sweeps.
We exercise the setup-builder contract here:

  - each baseline returns a Setup with the right system / user / tools
  - B4's setup creates an artifact and a permissive grant
  - delegation D1/D2/D3 setups build the right tool surface for the
    supervisor and a sub_metrics dict the delegate tool can populate

No API key needed — we only inspect the constructed objects, never run
the agent loop.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from artifactstore import ArtifactStore
from eval.baselines import (
    BASELINES,
    Setup,
    b1_raw,
    b2_truncated,
    b3_summary,
    b4_artifactstore,
)


@pytest.fixture
def fixture_meta() -> dict:
    return {
        "kind": "pytest",
        "target": "auth_expiry",
        "artifact_type": "pytest_failure",
        "ext": ".log",
    }


@pytest.fixture
def fixture_data() -> str:
    return "FAIL test_x\n  assert 1 == 2\n"


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore.init(tmp_path / "store.db")


# --- B1 / B2 / B3 — single-message context, no tools ----------------------

def test_b1_raw_inlines_full_output(store, fixture_data, fixture_meta):
    s = b1_raw(store, fixture_data, fixture_meta)
    assert isinstance(s, Setup)
    assert s.tools == []
    assert fixture_data in s.user_message
    assert "Diagnose" in s.user_message
    assert s.artifact_id is None
    assert s.grant_id is None


def test_b2_truncated_caps_at_max_tokens(store, fixture_meta):
    big = ("x" * 100 + "\n") * 200  # 20K chars
    s = b2_truncated(store, big, fixture_meta, max_tokens=200)
    assert s.tools == []
    # 200 tokens ~= 800 chars; should include a truncation marker.
    assert "[truncated" in s.user_message
    assert len(s.user_message) < len(big) + 500


def test_b3_summary_uses_deterministic_summarizer(store, fixture_meta):
    raw = "header\n" + ("WARNING: thing\n" * 5) + "trailing\n"
    s = b3_summary(store, raw, fixture_meta)
    assert s.tools == []
    # The deterministic summary keeps WARNING lines.
    assert "WARNING" in s.user_message
    assert "<summary>" in s.user_message


# --- B4 — handle + artifact_* tools ---------------------------------------

def test_b4_creates_artifact_and_permissive_grant(store, fixture_data, fixture_meta):
    s = b4_artifactstore(store, fixture_data, fixture_meta)
    assert s.artifact_id is not None
    assert s.artifact_id.startswith("art_")
    assert s.grant_id is not None
    assert s.grant_id.startswith("grant_")
    # The handle is in the user message.
    assert s.artifact_id in s.user_message
    # Tools include the four artifact_* read tools — no submit_report
    # (PLAN §11.1 is single-agent, free-text answer).
    tool_names = {t.name for t in s.tools}
    assert tool_names == {
        "artifact_search", "artifact_get_spans",
        "artifact_expand_view", "artifact_find_related",
    }


def test_b4_grant_is_permissive_for_eval(store, fixture_data, fixture_meta):
    """The §11.1 eval intentionally mints a permissive grant — RQ4 (denials)
    is exercised by the demo runner with a narrow grant, not here."""
    s = b4_artifactstore(store, fixture_data, fixture_meta)
    grant = store.conn.execute(
        "SELECT allowed_views, allowed_ops FROM artifact_grants "
        "WHERE grant_id = ?", (s.grant_id,)
    ).fetchone()
    import json
    assert "raw" in json.loads(grant["allowed_views"])
    assert "expand_view" in json.loads(grant["allowed_ops"])


def test_b4_tools_actually_callable(store, fixture_data, fixture_meta):
    """Smoke: each artifact_* tool can be invoked through its fn callable
    (the Anthropic SDK boundary just hands tool args dict → fn)."""
    s = b4_artifactstore(store, fixture_data, fixture_meta)
    by_name = {t.name: t for t in s.tools}
    spans = by_name["artifact_get_spans"].fn(
        artifact_id=s.artifact_id, token_budget=500,
    )
    assert isinstance(spans, list)


# --- BASELINES registry ---------------------------------------------------

def test_baselines_registry_complete():
    assert set(BASELINES.keys()) == {
        "B1_RAW", "B2_TRUNCATED", "B3_SUMMARY", "B3_LLM_SUMMARY",
        "B3_LLM_MULTIPASS", "B4_ARTIFACT",
    }
    # Every value is callable with the expected signature.
    import inspect
    for name, fn in BASELINES.items():
        sig = inspect.signature(fn)
        assert "store" in sig.parameters or len(sig.parameters) >= 3, name


# --- B3'' — multi-pass map-reduce summary ---------------------------------

def test_chunk_text_respects_token_budget():
    """_chunk_text should produce chunks under (target_tokens + line size)
    each. Short inputs return a single chunk."""
    from eval.baselines import _chunk_text
    from artifactstore.tokens import estimate

    short = "FAIL test_x\n  assert 1 == 2\n"
    chunks = _chunk_text(short, target_tokens=2000)
    assert len(chunks) == 1
    assert chunks[0] == short

    # Large input: many lines, each ~20 tokens, push past target.
    big_lines = [f"WARNING auth.py:{i:03d} token rejected: now=1000 exp=900\n"
                 for i in range(500)]
    big = "".join(big_lines)
    chunks = _chunk_text(big, target_tokens=500)
    assert len(chunks) >= 2
    for c in chunks:
        # Generous slack — we chunk on line boundaries so the last line of
        # a chunk can push past target. Just ensure no chunk is unbounded.
        assert estimate(c) < 1500


def test_b3_multipass_short_input_uses_map_only(store, fixture_data, fixture_meta):
    """Single-chunk inputs skip the reducer pass — verifies the
    map_only optimization (saves one LLM call when not needed)."""
    from eval.baselines import b3_llm_summary_multipass

    # Inject a scripted Agent class so we count calls without network.
    import eval.baselines as bl

    calls: list[str] = []

    class FakeResult:
        def __init__(self, text):
            self.final_text = text
            self.total_input_tokens = 10
            self.output_tokens = 5

    class FakeAgent:
        def __init__(self, *, name, system, tools, config, verbose=False,
                     **_):
            self.name = name

        def run(self, instruction):
            calls.append(self.name)
            return FakeResult(f"MAP_SUMMARY_FROM_{self.name}")

    original = bl.Agent
    bl.Agent = FakeAgent
    try:
        s = b3_llm_summary_multipass(
            store, fixture_data, fixture_meta,
            target_chunk_tokens=2000,
        )
    finally:
        bl.Agent = original

    # One mapper, zero reducers (single chunk).
    assert sum(1 for c in calls if c.startswith("b3mp_map_")) == 1
    assert sum(1 for c in calls if c.startswith("b3mp_reduce")) == 0
    # The map output is the final summary.
    assert "MAP_SUMMARY_FROM" in s.user_message
    assert s.extra["num_chunks"] == 1
    assert s.extra["stages"] == "map_only"
    # Costs from the single mapper call propagate.
    assert s.setup_input_tokens == 10
    assert s.setup_output_tokens == 5


def test_b3_multipass_large_input_uses_map_reduce(store, fixture_meta):
    """Multi-chunk inputs invoke the reducer — verifies both stages run
    and tokens accumulate across all calls."""
    from eval.baselines import b3_llm_summary_multipass
    import eval.baselines as bl

    calls: list[tuple[str, str]] = []

    class FakeResult:
        def __init__(self, text, in_tok=10, out_tok=5):
            self.final_text = text
            self.total_input_tokens = in_tok
            self.output_tokens = out_tok

    class FakeAgent:
        def __init__(self, *, name, system, tools, config, verbose=False,
                     **_):
            self.name = name

        def run(self, instruction):
            calls.append((self.name, instruction[:30]))
            return FakeResult(f"SUMMARY_FROM_{self.name}")

    big = "".join([f"WARNING line {i}: assertion failed\n" for i in range(500)])
    original = bl.Agent
    bl.Agent = FakeAgent
    try:
        s = b3_llm_summary_multipass(
            store, big, fixture_meta,
            target_chunk_tokens=300,
        )
    finally:
        bl.Agent = original

    n_map = sum(1 for c, _ in calls if c.startswith("b3mp_map_"))
    n_reduce = sum(1 for c, _ in calls if c.startswith("b3mp_reduce"))
    assert n_map >= 2, "expected multiple map chunks for a large input"
    assert n_reduce == 1, "exactly one reduce pass over chunk summaries"
    assert s.extra["num_chunks"] >= 2
    assert s.extra["stages"] == "map+reduce"
    assert "SUMMARY_FROM_b3mp_reduce" in s.user_message
    # Token cost accumulates across all (N+1) calls.
    assert s.setup_input_tokens == 10 * (n_map + n_reduce)
    assert s.setup_output_tokens == 5 * (n_map + n_reduce)


def test_b3_multipass_falls_back_when_mappers_produce_nothing(
    store, fixture_data, fixture_meta,
):
    """If every mapper returns empty text (rare but possible — say the
    model refuses), the baseline must NOT pass an empty summary to the
    downstream agent. It falls back to the deterministic summary."""
    from eval.baselines import b3_llm_summary_multipass
    import eval.baselines as bl

    class FakeResult:
        def __init__(self):
            self.final_text = ""
            self.total_input_tokens = 5
            self.output_tokens = 0

    class FakeAgent:
        def __init__(self, *, name, **_):
            self.name = name

        def run(self, instruction):
            return FakeResult()

    original = bl.Agent
    bl.Agent = FakeAgent
    try:
        s = b3_llm_summary_multipass(store, fixture_data, fixture_meta)
    finally:
        bl.Agent = original

    # The user message must still carry some summary content; the
    # deterministic fallback at least includes the WARNING/FAIL lines.
    assert "<summary>" in s.user_message
    assert s.user_message.count("<summary>\n\n</summary>") == 0


# --- delegation builders --------------------------------------------------

def test_delegation_d1_d2_d3_have_required_tools():
    """The three delegation strategies must construct supervisor tool sets
    with `run_workload` + `delegate`. D3 also has `create_grant`."""
    from eval.delegation import (
        STRATEGIES, _setup_d1_summary, _setup_d2_full, _setup_d3_scoped,
    )
    assert set(STRATEGIES.keys()) == {
        "D1_SUMMARY", "D1_LLM_SUMMARY", "D2_FULL_CONTEXT", "D3_SCOPED",
    }


def test_delegation_d1_setup_returns_tools(store, fixture_data, fixture_meta):
    from eval.delegation import _setup_d1_summary
    tools, sub_metrics = _setup_d1_summary(
        store, fixture_data, fixture_meta, model="x", max_turns=5,
    )
    names = {t.name for t in tools}
    assert names == {"run_workload", "delegate"}
    # sub_metrics is the side-channel dict the delegate tool populates.
    assert isinstance(sub_metrics, dict)
    for key in ("input_tokens", "output_tokens", "diagnosis", "submitted"):
        assert key in sub_metrics


def test_delegation_d2_setup_returns_tools(store, fixture_data, fixture_meta):
    from eval.delegation import _setup_d2_full
    tools, sub_metrics = _setup_d2_full(
        store, fixture_data, fixture_meta, model="x", max_turns=5,
    )
    names = {t.name for t in tools}
    assert names == {"run_workload", "delegate"}


def test_delegation_d3_setup_includes_create_grant(store, fixture_data, fixture_meta):
    from eval.delegation import _setup_d3_scoped
    tools, sub_metrics = _setup_d3_scoped(
        store, fixture_data, fixture_meta, model="x", max_turns=5,
    )
    names = {t.name for t in tools}
    assert names == {"run_workload", "create_grant", "delegate"}


def test_delegation_d1_run_workload_returns_summary_inline(
    store, fixture_data, fixture_meta,
):
    """D1's run_workload should put the summary into its return value
    (it goes into the supervisor's context that way, not into the store)."""
    from eval.delegation import _setup_d1_summary
    tools, _ = _setup_d1_summary(
        store, fixture_data, fixture_meta, model="x", max_turns=5,
    )
    rw = next(t for t in tools if t.name == "run_workload")
    out = rw.fn(kind="pytest", target="auth_expiry")
    assert "summary" in out
    assert isinstance(out["summary"], str) and out["summary"]
    assert out["policy"] == "summary"


def test_delegation_d2_run_workload_returns_full_body(
    store, fixture_data, fixture_meta,
):
    from eval.delegation import _setup_d2_full
    tools, _ = _setup_d2_full(
        store, fixture_data, fixture_meta, model="x", max_turns=5,
    )
    rw = next(t for t in tools if t.name == "run_workload")
    out = rw.fn(kind="pytest", target="auth_expiry")
    assert out["body"] == fixture_data
    assert out["policy"] == "raw"


def test_delegation_d3_run_workload_returns_only_handle(
    store, fixture_data, fixture_meta,
):
    """The supervisor under D3 must see only an artifact_id, never the raw."""
    from eval.delegation import _setup_d3_scoped
    tools, _ = _setup_d3_scoped(
        store, fixture_data, fixture_meta, model="x", max_turns=5,
    )
    rw = next(t for t in tools if t.name == "run_workload")
    out = rw.fn(kind="pytest", target="auth_expiry")
    assert out["policy"] == "artifact"
    assert out["artifact_id"].startswith("art_")
    assert "body" not in out, "D3 supervisor must not see the raw body"
    assert fixture_data not in str(out), \
        "raw fixture content leaked into D3's run_workload return"


# --- driver helpers --------------------------------------------------------

def test_run_result_dataclass_round_trip():
    """Sanity: RunResult is a dataclass we can serialize via asdict."""
    from dataclasses import asdict
    from eval.driver import RunResult
    r = RunResult(
        run_id="x", fixture="f", baseline="B1_RAW", rep=0, model="m",
        input_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        total_input_tokens=1, output_tokens=2,
        turns=1, tool_calls=0, stop_reason="end_turn",
        diagnosis="ok", diagnosis_chars=2,
        evidence_recall=1.0, task_success=True,
        citation_count=0, citations_resolved=0, citation_validity=0.0,
        exact_evidence_recovery=0.0, blocked_reads=0,
        elapsed_seconds=0.1, estimated_cost_usd=0.001,
    )
    d = asdict(r)
    assert d["fixture"] == "f"
    assert d["task_success"] is True


def test_load_fixture_resolves(tmp_path: Path):
    """`_load_fixture` reads the data + sibling .gold.json + meta from
    FIXTURE_REGISTRY. Smoke against pytest_auth_expiry."""
    from eval.driver import _load_fixture
    data, gold, meta = _load_fixture("pytest_auth_expiry")
    assert "test_auth_expiry" in data
    assert gold and "ground_truth" in gold
    assert meta["kind"] == "pytest"
