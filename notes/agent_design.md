# Agent design notes

Research notes for the demo agent. Authoritative spec: `ArtifactStore_PLAN.md` §20 (added below). Reference code studied: `notes/references/anthropic-quickstarts/agents/` (MIT, ~300 LOC, copy-and-adapt friendly).

## Canonical client-tool agent loop (Anthropic docs)

> Source: `platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works`

```
1. send messages.create(messages=[user], tools=[...])
2. response.stop_reason == "tool_use"  →  execute every tool_use block
3. append assistant content as-is, then append a user message whose
   content[] STARTS with one tool_result per tool_use_id (text after, never before)
4. repeat from 2 until stop_reason in {"end_turn","max_tokens","stop_sequence","refusal"}
```

### Hard rules learned from the docs

- `tool_result` blocks **must immediately follow** their `tool_use` blocks — no intervening messages, and inside the user message, tool_results come **first** in `content[]` (text after). 400 error otherwise.
- One assistant turn can emit **multiple parallel** `tool_use` blocks → execute in parallel, return all results in the next user message.
- Tool failures: return `tool_result` with `is_error: true` and a *useful* message ("Rate limited, retry after 60s"), not just "failed". The model recovers if you tell it how.
- Invalid tool calls (missing required params): same — return is_error with a hint. Claude self-corrects 2–3 times.
- For deterministic schema, use `strict: true` on tool defs.
- Server tools (`web_search`, `code_execution`) run on Anthropic side — do not loop them yourself. Watch `stop_reason: "pause_turn"` if they hit iteration cap.

## Patterns worth copying from anthropic-quickstarts/agents

The reference is MIT-licensed; keep `notes/references/.../LICENSE` if we lift code.

- **Tool dataclass**: `name`, `description`, `input_schema`, async `execute(**input)` → string. `to_dict()` emits the API shape. Clean abstraction; we should adopt it.
- **Parallel `execute_tools`**: `asyncio.gather` over per-call coroutines, each catches its own exception and packs `is_error: True` so the loop never crashes.
- **History truncation**: simple FIFO over (input, output) token pairs with a "history truncated" sentinel. Token-counting via `client.messages.count_tokens` with a fallback. Good enough for the demo.
- **Prompt caching**: drop `cache_control: {type: "ephemeral"}` on the **last** user content's blocks. Cuts cost on multi-turn loops without changing semantics.
- **Verbose printing** by default via `verbose=True`. Cheap, makes recordings/screenshots easy.

## What we will *not* copy

- MCP server connection plumbing (`utils/connections.py`) — out of scope; ArtifactStore is in-process.
- Notebook demo path — we ship a CLI demo so it scripts cleanly into eval runs.
- The async-everywhere style — fine to keep, but the ArtifactStore API is sync; wrap with `asyncio.to_thread` at the tool boundary, don't sprinkle `async` everywhere.

## Pitfalls observed (do not relearn)

1. **Putting text before tool_result** in the user message → 400. Always tool_results first.
2. **Skipping a tool_result** when one tool_use block was emitted but you only handled some → 400 ("tool_use ids without tool_result"). Return a result for *every* `tool_use_id`, even if you stub it.
3. **Free-text "I'll use the X tool"** parsed via regex → that decision should have been a `tool_use` schema. If we find ourselves regexing model output, the structure belongs in the tool.
4. **Treating `pause_turn` as `end_turn`** for server tools → loop exits early. We don't use server tools in the demo, but flag for later.
5. **Hidden state in agent**: subagent must not share memory with supervisor outside the grant + ArtifactStore. The whole *point* of the demo is the grant boundary.

## Demo agent shape (matches PLAN §20)

Two agents, both client-tool style on Anthropic Messages API:

```
Supervisor                                    Subagent
----------                                    --------
tools:                                        tools (gated by grant_id):
  run_pytest(target)                            artifact_search(query, types?, limit?, budget?)
  put_artifact(...)        [internal]           artifact_get_spans(artifact_id, types?, budget?)
  create_grant(...)        [scoped helper]      artifact_expand_view(artifact_id, view, budget?)
  delegate(task, grant)                         artifact_find_related(artifact_id, relations?)
  expand_artifact(view)                         submit_report(text, citations[])
```

The supervisor never forwards its transcript to the subagent. The only handle is `grant_id`. The subagent's `submit_report` ends its loop and returns to the supervisor.

For the eval runs, the same workload is replayed against four configurations (B1–B4 in PLAN §11.1). Same fixture in, same `submit_report` shape out, only the tool surface and context-injection policy change.

## Models to use

The `anthropic` Python SDK is just the HTTP client. With `ANTHROPIC_BASE_URL` it talks to any Anthropic-API-compatible provider — we are not coupled to Anthropic.

- **Default (cheap, good enough)**: `deepseek-v4-pro` via `https://api.deepseek.com/anthropic`. ~$0.44/M in, $0.87/M out — roughly 7× cheaper than Sonnet 4.5. Tool_use blocks behave the same. Set in `demo.agent.DEFAULT_MODEL`.
- **Cheaper still**: `deepseek-v4-flash` for the subagent in eval sweeps ($0.14/$0.28).
- **Anthropic native (alternate)**: unset `ANTHROPIC_BASE_URL`, `--model claude-sonnet-4-5` (or `claude-opus-4-7` for stress runs).
- Keep the model id in **one place** (`ModelConfig`), never hardcoded in agent code, so eval scripts can sweep it.

> ~~Open question: does DeepSeek's `/anthropic` endpoint emit real `tool_use` content blocks?~~ **Confirmed yes** via `python -m demo.runner --verify-tool-use` against `deepseek-v4-pro` (2 turns, 1 tool call, 76 in / 97 out tokens — ~$0.0001/call). The Anthropic Python SDK round-trips identically; no adapter needed. The `--verify-tool-use` flag stays as a pre-eval sanity check.

## Workload tools (the missing half)

Subagent tools *read* from the store. Supervisor tools must *produce* outputs to feed in. Concrete surface in `demo/tools.py::supervisor_tools(...)`:

```text
run_workload(kind, target)       # returns {artifact_id, type, preview, ...}
create_grant(...)                # returns {grant_id, predicate, ...}
delegate(task, grant_id)         # spawns subagent loop, returns {report, audit}
expand_artifact(artifact_id, view)  # supervisor's own citation verifier
```

**Critical invariant:** under the demo's default `ViewPolicy.ARTIFACT`, `run_workload` puts raw output into ArtifactStore and returns ONLY the handle. The supervisor never sees the raw bytes. That's the entire ArtifactStore thesis encoded as a tool boundary — if a future change leaks raw output back here, the experiment is invalid.

### One workload runner, four policies (eval factorization)

Same `run_workload(kind, target)` powers all PLAN §11.1 baselines via a `ViewPolicy`:

```text
RAW         B1   tool returns raw text                    (control)
TRUNCATED   B2   first N tokens of raw                    (cheap baseline)
SUMMARY     B3   LLM-summarize raw, return summary        (smart baseline)
ARTIFACT    B4   put + return handle                      (ArtifactStore)
```

Eval driver: same fixture, same task description, same agent loop, swap the policy. Measure `(input_tokens, output_tokens, turns, tool_calls, evidence_recall, citation_validity, blocked_reads)`. This is what `eval/runs/<ts>/` records.

### Fixtures over live execution

Default mode is **replay**: `_load(kind, target)` reads from `eval/fixtures/` via a small registry. `--live` flag exists for ad-hoc demos but is not used in eval — determinism is required to attribute differences to the policy, not to noisy real outputs.

Capture fixtures once (real `pytest` / `rg` / `git diff` runs against a toy buggy project), check them in, never regenerate during eval.

## Open questions to resolve when we wire it up

- Does `submit_report` deserve `tool_choice: {type: "tool", name: "submit_report"}` to force termination on the last turn? Probably yes once we decide the subagent is "done" — saves a wasted turn.
- Token budget enforcement: hard truncate at `max_tokens` per artifact view, or bias the model with a system-prompt note? Start with hard truncation; revisit.
- Citation correctness check: the subagent reports `citations: ["art_xxx/span_y", ...]`. The supervisor must verify each citation resolves under the grant. Build that verifier alongside the demo, not later.
