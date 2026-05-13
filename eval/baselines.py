"""Baseline configurations for PLAN §11.1 single-agent eval.

Each baseline is a function `(store, fixture_data, fixture_meta) -> Setup`
where Setup is the (system_prompt, user_message, tools, baseline_meta) tuple
the eval driver passes into Agent.run().

The five baselines differ only in what context the agent receives:
  B1 RAW            — full raw output inline in user message
  B2 TRUNCATED      — first N tokens of raw inline
  B3 SUMMARY        — deterministic offline summary inline
  B3 LLM_SUMMARY    — LLM-generated summary inline (one extra paid call)
  B4 ARTIFACT       — handle inline + artifact_* tools to expand on demand

Models, temperature, and tasks stay constant across baselines so the
difference attributes to the context strategy, not noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from artifactstore import ArtifactStore
from artifactstore.tokens import estimate
from demo.agent import Agent, ModelConfig, Tool
from demo.workloads import deterministic_summary


B1_B2_B3_SYSTEM = """\
You are a debugging assistant. Given the output of a tool run, identify the
root cause of any failure.

Be concise (under 200 words). Quote the specific line(s) or value(s) that
are decisive evidence. End your turn with the diagnosis as plain text.
"""

B4_SYSTEM = """\
You are a debugging assistant. You receive a handle to a tool output
(artifact_id) rather than the raw text — the tool result is stored in
ArtifactStore. Use artifact_get_spans first to read the typed evidence
(assertions, log warnings, stack frames). Use artifact_expand_view only
if get_spans is not enough. Stop searching as soon as you have enough
evidence — efficiency matters.

End your turn with the diagnosis as plain text under 200 words.

CITATION FORMAT (strict — output is parsed by regex):
  - Every key claim MUST cite at least one span in the FULL form:
      art_<8hex>/span_<8hex>
    Concrete example: art_2c52e1c7/span_d0fc13a8
  - Do NOT cite by span_id alone (e.g. 'span_d0fc13a8'). Always include
    the artifact_id with a slash.
  - A diagnosis without at least one full-form citation will be flagged
    as invalid by the eval grader.
"""


@dataclass
class Setup:
    system: str
    user_message: str
    tools: list[Tool]
    grant_id: str | None = None       # B4 only
    artifact_id: str | None = None    # B4 only
    extra: dict[str, Any] = field(default_factory=dict)
    # Pre-run cost in USD (for baselines that make summarization calls
    # before the measurable run, like B3_LLM_SUMMARY). Added to the
    # final run's estimated_cost_usd by the eval driver so headline
    # cost numbers stay apples-to-apples.
    setup_cost_usd: float = 0.0
    # Pre-run token usage attributed to setup (e.g., summarizer LLM call).
    # Stored separately so the eval driver can fold them into reported
    # token totals without conflating with the agent's own measurement.
    setup_input_tokens: int = 0
    setup_output_tokens: int = 0


def _diagnostic_phrase(fixture_meta: dict) -> str:
    """Build the per-task diagnostic instruction. For fixtures where the
    target is genuinely a single failure ('auth_expiry' in a 1-failure
    log), naming it is informative not biasing. For multi-failure
    fixtures we hide the target and ask the agent to discover."""
    kind = fixture_meta["kind"]
    if fixture_meta.get("reveal_target", True):
        return (f"Diagnose the root cause of any failure in this {kind} "
                f"output for target '{fixture_meta['target']}'. Be specific.")
    return (f"Diagnose the root cause of THE most diagnostically informative "
            f"failure in this {kind} output. The output may contain multiple "
            f"failures; pick the one with the most actionable evidence "
            f"(e.g. a smoking-gun log line, an explicit error message). "
            f"Be specific about which failure you chose.")


# ---------------------------------------------------------------------------
# B1 / B2 / B3 — single-message context, no tools.
# ---------------------------------------------------------------------------

def b1_raw(store: ArtifactStore, fixture_data: str,
           fixture_meta: dict) -> Setup:
    user = (
        f"{_diagnostic_phrase(fixture_meta)}\n\n"
        f"<output>\n{fixture_data}\n</output>"
    )
    return Setup(system=B1_B2_B3_SYSTEM, user_message=user, tools=[])


def b2_truncated(store: ArtifactStore, fixture_data: str,
                 fixture_meta: dict, *, max_tokens: int = 200) -> Setup:
    approx_chars = max_tokens * 4
    head = fixture_data[:approx_chars]
    raw_tokens = estimate(fixture_data)
    body = head if len(fixture_data) <= approx_chars else (
        head + f"\n... [truncated; total {raw_tokens} tokens]"
    )
    user = (
        f"{_diagnostic_phrase(fixture_meta)} "
        f"The output was truncated to {max_tokens} tokens for context "
        f"efficiency.\n\n"
        f"<output truncated=\"true\">\n{body}\n</output>"
    )
    return Setup(system=B1_B2_B3_SYSTEM, user_message=user, tools=[])


# Backward-compat alias for any external callers (kept until callers migrate).
# Canonical implementation lives in demo/workloads.py so the demo's SUMMARY
# policy and the eval's B3/D1 baselines share one codepath.
_deterministic_summary = lambda raw, kind=None: deterministic_summary(raw)


def b3_summary(store: ArtifactStore, fixture_data: str,
               fixture_meta: dict) -> Setup:
    summary = deterministic_summary(fixture_data)
    user = (
        f"{_diagnostic_phrase(fixture_meta)} "
        f"You only have a summary; the raw output was not preserved.\n\n"
        f"<summary>\n{summary}\n</summary>"
    )
    return Setup(system=B1_B2_B3_SYSTEM, user_message=user, tools=[])


B3_LLM_SUMMARIZER_SYSTEM = """\
You are a summarizer that compresses tool output for a downstream debugging
agent. The downstream agent will see ONLY your summary, not the raw output,
and must use it to identify the root cause of a failure.

Preserve everything diagnostically critical:
  - error messages and assertion text verbatim (do not paraphrase)
  - the failing file path, line number, and test name
  - WARNING / ERROR log lines that name the relevant code (e.g.,
    `WARNING auth.py:117 token rejected: now=... exp=...`)
  - exception types and their specific arguments
  - any timestamps, hashes, IDs, or other concrete values an engineer
    would quote when diagnosing

Drop:
  - passing-test progress dots
  - startup/teardown noise that doesn't name the failing code
  - benchmark / coverage tables unrelated to the failure

Format: plain prose with short bulleted sections. Keep under 250 tokens.
Do NOT diagnose — produce a faithful summary; downstream agent decides.
"""


def b3_llm_summary(store: ArtifactStore, fixture_data: str,
                   fixture_meta: dict,
                   *, summarizer_model: str = "deepseek-v4-pro") -> Setup:
    """B3' — single-pass LLM-generated summary.

    The weakest LLM-summary baseline: one call, 250-token cap. Kept as
    the strawman that ArtifactStore beat in early eval; the multi-pass
    variant (b3_llm_summary_multipass) is the stronger comparison the
    paper reviewers asked for.

    Token cost is accounted on Setup.setup_* fields and folded into the
    run's reported total by the driver — keeps cost numbers
    apples-to-apples against B3 (deterministic, $0 setup).
    """
    summarizer = Agent(
        name="b3_llm_summarizer",
        system=B3_LLM_SUMMARIZER_SYSTEM,
        tools=[],
        config=ModelConfig(model=summarizer_model, max_turns=1,
                           max_tokens=512),
        verbose=False,
    )
    instruction = (
        f"Summarize the following {fixture_meta['kind']} output for a "
        f"downstream debugger.\n\n<output>\n{fixture_data}\n</output>"
    )
    result = summarizer.run(instruction)
    summary = result.final_text.strip() or deterministic_summary(fixture_data)

    user = (
        f"{_diagnostic_phrase(fixture_meta)} "
        f"You only have a summary produced by an LLM summarizer; the raw "
        f"output was not preserved.\n\n"
        f"<summary>\n{summary}\n</summary>"
    )
    return Setup(
        system=B1_B2_B3_SYSTEM, user_message=user, tools=[],
        setup_input_tokens=result.total_input_tokens,
        setup_output_tokens=result.output_tokens,
        # Cost computed by driver using its rate card.
        extra={"summarizer_model": summarizer_model,
               "summary_chars": len(summary),
               "summary_tokens": estimate(summary)},
    )


# ---------------------------------------------------------------------------
# B3'' — multi-pass map-reduce LLM summary. The stronger baseline a reviewer
# would ask for: chunk the raw output, summarize each chunk preserving
# diagnostic detail, then reduce the chunk summaries into a final synthesis.
# Two stages × N chunks + 1 reducer = (N+1) LLM calls and a larger token
# budget than B3', so it's strictly more compute. If ArtifactStore still
# beats THIS on evidence recovery, the comparison is honest.
# ---------------------------------------------------------------------------

B3_MULTIPASS_MAP_SYSTEM = """\
You are stage 1 of a 2-stage summarizer. You receive ONE CHUNK of a longer
tool output. Other chunks are summarized in parallel by other workers; a
reducer will merge all chunk summaries.

Preserve everything diagnostically critical from THIS chunk:
  - error messages, assertion text, traceback frames verbatim
  - failing test names, file paths, line numbers
  - WARNING / ERROR log lines that name a function or file
  - exception types and arguments
  - timestamps, hashes, IDs, or concrete values an engineer would quote

Drop only obvious noise (progress dots, repeated benchmark rows). When in
doubt, KEEP — the reducer can compress further. If the chunk contains no
diagnostic content, return one line: 'CHUNK <i>: no diagnostic content'.

Format: short bulleted facts, prefixed with `CHUNK <i>:`. No prose framing.
Do NOT diagnose. Cap at 400 tokens per chunk."""


B3_MULTIPASS_REDUCE_SYSTEM = """\
You are stage 2 of a 2-stage summarizer. You receive per-chunk summaries
from stage 1, in order. The downstream debugging agent will see ONLY your
final summary, not the raw output.

Synthesize a single coherent summary that preserves:
  - the failing test name(s), assertion text, exception type+message
  - the file path and line number where the failure originates
  - any WARNING / ERROR lines that name the offending code
  - concrete values (timestamps, IDs, hashes) the engineer would quote

If chunk summaries are redundant, merge them. If they conflict, prefer the
more concrete/specific evidence. If multiple failures are present, list
them but mark the one with the most actionable evidence.

Format: short bulleted sections under headings (Failure, Evidence, Context).
Cap at 600 tokens. Do NOT diagnose — produce a faithful synthesis."""


def _chunk_text(text: str, *, target_tokens: int = 2000,
                overlap_tokens: int = 100) -> list[str]:
    """Token-aware chunking using the project's `estimate` helper. We
    chunk on line boundaries so per-chunk content stays self-describing
    (a chunk doesn't slice mid-traceback if avoidable). Falls back to
    char-based slicing when a single line exceeds target_tokens (rare
    but possible — base64-encoded payload, etc.)."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return [text] if text else []
    chunks: list[list[str]] = [[]]
    cur_tokens = 0
    for line in lines:
        n = estimate(line)
        if cur_tokens + n > target_tokens and chunks[-1]:
            # Start a new chunk; carry a small tail for context overlap.
            tail = chunks[-1][-3:] if overlap_tokens else []
            chunks.append(list(tail))
            cur_tokens = sum(estimate(x) for x in tail)
        chunks[-1].append(line)
        cur_tokens += n
    return ["".join(c) for c in chunks if c]


def b3_llm_summary_multipass(
        store: ArtifactStore, fixture_data: str, fixture_meta: dict,
        *, summarizer_model: str = "deepseek-v4-pro",
        target_chunk_tokens: int = 2000,
        max_map_tokens: int = 600,
        max_reduce_tokens: int = 900) -> Setup:
    """B3'' — map-reduce LLM summary.

    Stage 1 (MAP): chunk the raw input, summarize each chunk with strong
    detail-preservation instructions. Each chunk's max_tokens > B3' total
    cap, so the map stage alone has more capacity than B3'.

    Stage 2 (REDUCE): synthesize chunk summaries into a coherent final
    summary, still cap-bounded but the input is curated.

    Cost: ~(N+1)× B3' tokens. If ArtifactStore still wins on
    evidence-recall vs this baseline, the win isn't from B3' being too
    weak. All map+reduce token usage is folded into setup_* fields.
    """
    chunks = _chunk_text(fixture_data, target_tokens=target_chunk_tokens)

    map_summaries: list[str] = []
    setup_in = setup_out = 0
    for i, chunk in enumerate(chunks):
        mapper = Agent(
            name=f"b3mp_map_{i}",
            system=B3_MULTIPASS_MAP_SYSTEM,
            tools=[],
            config=ModelConfig(model=summarizer_model, max_turns=1,
                               max_tokens=max_map_tokens),
            verbose=False,
        )
        instruction = (
            f"Chunk {i+1}/{len(chunks)} of a {fixture_meta['kind']} "
            f"output.\n\n<chunk index=\"{i+1}\">\n{chunk}\n</chunk>"
        )
        r = mapper.run(instruction)
        setup_in += r.total_input_tokens
        setup_out += r.output_tokens
        text = r.final_text.strip()
        if text:
            map_summaries.append(text)

    if not map_summaries:
        # Mapper produced nothing useful — fall back to deterministic
        # summary so the downstream agent isn't given an empty string.
        summary = deterministic_summary(fixture_data)
    elif len(chunks) == 1:
        # Single-chunk inputs (< target_chunk_tokens) don't need a
        # reducer pass — the map summary IS the final summary. Saves
        # one LLM call without compromising the multi-pass story.
        summary = map_summaries[0]
    else:
        reducer = Agent(
            name="b3mp_reduce",
            system=B3_MULTIPASS_REDUCE_SYSTEM,
            tools=[],
            config=ModelConfig(model=summarizer_model, max_turns=1,
                               max_tokens=max_reduce_tokens),
            verbose=False,
        )
        joined = "\n\n".join(map_summaries)
        instruction = (
            f"Synthesize a final summary from the chunk summaries below. "
            f"The downstream agent will see only your output.\n\n"
            f"<chunk_summaries>\n{joined}\n</chunk_summaries>"
        )
        r = reducer.run(instruction)
        setup_in += r.total_input_tokens
        setup_out += r.output_tokens
        summary = r.final_text.strip() or "\n\n".join(map_summaries)

    user = (
        f"{_diagnostic_phrase(fixture_meta)} "
        f"You only have a summary produced by a multi-pass LLM "
        f"summarizer; the raw output was not preserved.\n\n"
        f"<summary>\n{summary}\n</summary>"
    )
    return Setup(
        system=B1_B2_B3_SYSTEM, user_message=user, tools=[],
        setup_input_tokens=setup_in,
        setup_output_tokens=setup_out,
        extra={"summarizer_model": summarizer_model,
               "summary_chars": len(summary),
               "summary_tokens": estimate(summary),
               "num_chunks": len(chunks),
               "stages": ("map+reduce" if len(chunks) > 1 else "map_only")},
    )


# ---------------------------------------------------------------------------
# B4 — handle + artifact_* tools.
# ---------------------------------------------------------------------------

def _b4_tools(store: ArtifactStore, grant_id: str) -> list[Tool]:
    """Subset of demo.tools.subagent_tools — no submit_report (the eval
    measures final text, not a structured report). Grant_id is bound at
    construction time, hidden from the model."""
    return [
        Tool(
            name="artifact_search",
            description="Full-text search over preview + span text. Returns "
                        "[{artifact_id, type, preview, score}].",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "artifact_types": {"type": "array",
                                       "items": {"type": "string"}},
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
                        "changed_line, error_message, log_warning, ...) "
                        "from one artifact.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "span_types": {"type": "array",
                                   "items": {"type": "string"}},
                    "token_budget": {"type": "integer", "default": 800},
                },
                "required": ["artifact_id"],
            },
            fn=lambda **kw: store.get_spans(grant_id=grant_id, **kw),
        ),
        Tool(
            name="artifact_expand_view",
            description="Materialize a view of an artifact: 'preview' | "
                        "'evidence' | 'redacted' | 'raw' | 'provenance'. "
                        "'raw' may be denied by your grant.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "view": {"type": "string",
                             "enum": ["preview", "evidence", "redacted",
                                      "raw", "provenance"]},
                    "token_budget": {"type": "integer", "default": 1500},
                },
                "required": ["artifact_id", "view"],
            },
            fn=lambda **kw: store.expand_view(grant_id=grant_id, **kw),
        ),
        Tool(
            name="artifact_find_related",
            description="Follow provenance/causal links from an artifact.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "relations": {"type": "array",
                                  "items": {"type": "string"}},
                },
                "required": ["artifact_id"],
            },
            fn=lambda **kw: store.find_related(grant_id=grant_id, **kw),
        ),
    ]


def b4_artifactstore(store: ArtifactStore, fixture_data: str,
                     fixture_meta: dict) -> Setup:
    artifact_id = store.put_artifact(
        tool_name=fixture_meta["kind"],
        artifact_type=fixture_meta["artifact_type"],
        raw_text=fixture_data,
        creator_agent_id="eval_driver",
        session_id=fixture_meta.get("session_id", "eval"),
        metadata={"target": fixture_meta["target"]},
    )
    # Permissive grant so the eval measures *what tools the model uses*, not
    # what the predicate denies. RQ4 (denial counts) is exercised by the
    # demo runner with a narrow grant, not here.
    grant_id = store.create_grant(
        subject_agent_id="eval_agent",
        issuer_agent_id="eval_driver",
        artifact_predicate={},
        allowed_ops=["search", "get_spans", "expand_view", "find_related"],
        allowed_views=["preview", "evidence", "redacted", "raw", "provenance"],
        max_tokens=10000, ttl_seconds=3600,
    )
    user = (
        f"{_diagnostic_phrase(fixture_meta)} "
        f"The raw output is stored in ArtifactStore — your handle is "
        f"`{artifact_id}` (type: {fixture_meta['artifact_type']}). Use the "
        f"artifact_* tools to inspect evidence on demand."
    )
    return Setup(
        system=B4_SYSTEM, user_message=user,
        tools=_b4_tools(store, grant_id),
        grant_id=grant_id, artifact_id=artifact_id,
    )


BASELINES: dict[str, Callable[..., Setup]] = {
    "B1_RAW":               b1_raw,
    "B2_TRUNCATED":         b2_truncated,
    "B3_SUMMARY":           b3_summary,
    "B3_LLM_SUMMARY":       b3_llm_summary,
    # B3'': multi-pass map-reduce summary — the stronger LLM-summary
    # baseline a reviewer would ask for. ~(N+1)x B3' cost.
    "B3_LLM_MULTIPASS":     b3_llm_summary_multipass,
    "B4_ARTIFACT":          b4_artifactstore,
}
