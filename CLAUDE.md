# ArtifactStore

Research prototype (DBMS course). Authoritative spec: `ArtifactStore_PLAN.md` — read it before changing the data model, API surface, or eval design.

> **One-liner:** scoped evidence substrate for tool-using AI agents — typed, indexed, permission-scoped artifacts replacing transcript dumping; supervisors and subagents recover exact tool-result evidence under token + access constraints.

## Stack

- Python 3.12 (pinned via `.python-version`), managed with **uv** (`uv sync`, `uv run pytest`, `uv add <pkg>`). Never call bare `python`/`pip`.
- SQLite (stdlib) + FTS5 — chosen over DuckDB: FTS5 is built-in, single-file, no extra dep, fine for prototype scale.
- Typer (or argparse) for CLI
- pytest for tests
- Token estimator: `tiktoken` if available, else `len(text)//4` fallback. Wrap behind one helper.

## Layout

```
artifactstore/        # the contribution
  schema.sql          # DDL straight from PLAN §7
  db.py               # connect(), migrate(), new_id()
  store.py            # ArtifactStore class — public API (PLAN §9)
  extractors.py       # type → span extractors (registry by artifact_type)
  views.py            # preview / evidence / redacted / raw / provenance
  grants.py           # predicate matching, op/view checks, audit logging
  cite.py             # citation parse + verify (art_xxx/span_y)
  cli.py              # init / put / search / spans / expand / grant / audit
demo/                 # the test bench (PLAN §20)
  agent.py            # Tool, ModelConfig, Agent.run() — Claude tool-use loop
  tools.py            # subagent_tools(store, grant_id) — grant bound at construction
  prompts.py          # SUPERVISOR_SYSTEM, SUBAGENT_SYSTEM
  runner.py           # python -m demo.runner --fixture <log>
tests/
notes/
  agent_design.md     # research notes — patterns, pitfalls, citations
  references/         # MIT-licensed reference repos (read-only, do not edit)
eval/                 # fixtures + RQ1-4 driver (PLAN §11, §20.6)
```

## Design choices (locked)

These nail down the open questions in PLAN §7–§9 and §20. Do not relitigate without a reason.

- **Raw storage = BLOB.** A single `raw_blob` column on `artifacts` (added alongside `raw_uri`/`raw_hash`). Single source of truth, atomic with metadata, no orphan files. `raw_uri` stays nullable for a future file-backed escape hatch (`file://...`).
- **`raw_hash` = sha256(raw_text utf-8), hex.** Computed in `put_artifact`; never recomputed on read.
- **Preview = ≤256 tokens.** One header line `[<artifact_type> | <one-line summary> | <raw_token_count> tokens]` produced by a type-driven preview registry (mirrors `extractors.py`), then content lines until budget hits. Hard cap, not advisory.
- **Citation format = `art_<8hex>/span_<8hex>`.** One implementation in `artifactstore/cite.py` (`parse`, `verify`). Both supervisor harness and eval driver call into it — no regex sprinkled in agent code.
- **Sensitivity ordering**: `public(0) < internal(1) < restricted(2) < secret(3)`. Default artifact label = `internal`. `sensitivity_max` in a grant predicate = numeric ≤. Lives in `grants.py` as one dict.
- **`path_prefixes` semantics (prototype)**: applied **only to `artifact_spans.file_path`**. A span whose `file_path` is None is path-opaque and always passes the prefix check; a span with a path passes only if it starts with one of the prefixes. No artifact-level `path` column.
- **Token-budget enforcement = omit-fits.** Iterate spans by `importance DESC, span_id`; include whole-or-skip until next span would overflow. Predictable, easy to test. Same rule for `search`, `get_spans`, `expand_view(evidence)`.
- **FTS5 lifecycle**: one row inserted per `put_artifact` (`span_text = "\n".join(spans)`); artifacts are **immutable**, no update path in v1.
- **Synthetic `__supervisor__` grant** is seeded by `migrate()` (idempotent INSERT in `schema.sql`). Allows audit-log FKs to resolve and keeps `expand_artifact` from special-casing in app code.
- **Subagent termination**: `force_terminator="submit_report"` after 8 turns (already in `demo/agent.py`). Endorsed by PLAN §20.3.
- **API key safety**: `Agent.__init__` fails loudly if the provider key for the resolved model (`DEEPSEEK_API_KEY` for `deepseek-*`, `QWEN_API_KEY` for `qwen*`) is missing **and** no client is injected. No cross-provider fallback. Tests inject a stub client.
- **Gold-truth fixtures**: each `eval/fixtures/<name>.<ext>` ships with a sibling `<name>.gold.json` listing the expected evidence — that's how RQ2 evidence-recall stops being a vibe.
- **`eval/runs/<UTC-iso>/`** layout: `config.json` (baseline + model + fixture), `result.jsonl` (one row per task), `audit.csv` (dump of `artifact_access_log`), `manifest.json` (git rev + timing). Driver writes; nothing else touches.

## Build order (PLAN §13)

1. `artifacts` table + raw storage (file or BLOB; pick one and stick with it)
2. Preview extraction (first N tokens, plus type-specific summary if cheap)
3. Span extractors for **pytest_failure**, **grep_result**, **git_diff** first — these are eval workloads
4. FTS5 search over preview + span text
5. Views: preview / evidence / redacted / raw / provenance
6. Grant checker (predicate + allowed_ops + allowed_views + max_tokens + expiry)
7. Access audit log — log every read attempt, allowed or not
8. Supervisor/subagent simulation harness
9. Eval scripts for RQ1–RQ4

Keep each step shippable end-to-end before moving on.

## Invariants — do not violate

- **No raw output by default.** Tools/agents see preview + handle. Raw view requires explicit grant.
- **Every artifact has:** type, preview, raw_hash, creator, session.
- **Every access goes through grant check** — even from same agent. The audit log is the evaluation signal for RQ4.
- **Token budgets are enforced**, not advisory. `search` / `get_spans` / `expand_view` truncate to budget.
- **Span extraction is type-driven**, registered per `artifact_type`. New types = new extractor; do not branch inside a god function.
- **Grant predicate is JSON** stored in column, evaluated in Python — fine for prototype, do not over-engineer to SQL.

## Out of scope (PLAN §17)

Skill/tool selection, general agent memory, KV-cache, A2A protocol, multi-agent scheduling, global eviction. If a change drifts here, push back.

## Demo agent (PLAN §20)

- Stack: Anthropic Messages API shape + a ~150-LOC client-side tool-use loop. **Not** the Claude Agent SDK, not MCP, not a planner.
- Two agents only: supervisor → subagent. No recursion.
- `grant_id` is bound at tool-construction time; the model never sees it. Harness enforces scope, not the LLM.
- After the subagent submits, the supervisor verifies every citation by `expand_artifact` — unresolvable citation = report rejected.
- Fixtures (real pytest/npm/rg/git logs) live in `eval/fixtures/`, replayed deterministically. Don't run live `pytest` from the demo.
- Reference: `notes/agent_design.md` for canonical loop, hard rules, and pitfalls (tool_result ordering, parallel tool_use, etc.).

### Provider configuration

The agent loop uses the `anthropic` Python SDK as a transport against any Anthropic-Messages-API-compatible endpoint. Two providers are supported simultaneously; the model-name prefix picks credentials.

| Prefix | Provider | Key env | URL env | Default URL |
| --- | --- | --- | --- | --- |
| `deepseek-*` | DeepSeek V4 ([docs](https://api-docs.deepseek.com/guides/anthropic_api)) | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/anthropic` |
| `qwen*` | Alibaba Model Studio ([docs](https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages)) | `QWEN_API_KEY` | `QWEN_BASE_URL` | `https://dashscope-intl.aliyuncs.com/apps/anthropic` |

Each provider reads only its own env vars — no cross-provider fallback. Copy `.env.example` to `.env`, fill in whichever keys you have. Both can coexist; the runner picks credentials from `--model`.

```bash
# Sample sweep across providers (one .env, two API keys filled in):
uv run python -m eval.driver --model deepseek-v4-pro --reps 5
uv run python -m eval.driver --model qwen3.6-plus    --reps 5
```

`demo.agent.DEFAULT_MODEL = "deepseek-v4-pro"`. Cheaper iteration: `--model deepseek-v4-flash` or `--model qwen-flash`. Headline-model recommendation for Qwen: `qwen3.6-plus`.

Tests don't touch this — they inject a `ScriptedClient` stub directly. No keys, no network.

## Eval targets (PLAN §14)

- 30–60% fewer prompt tokens vs raw injection
- Higher exact-evidence recall than truncation/summary baselines
- Near-zero unauthorized reads under explicit grants

Comparison baselines (always run together): B1 raw, B2 truncated, B3 summary-only, B4 ArtifactStore. For supervisor/subagent: D1 summary-only, D2 full-context, D3 scoped ArtifactStore.

## Conventions

- Artifact IDs: `art_<8hex>`; spans: `span_<8hex>`; grants: `grant_<short>`. Use a single `new_id(prefix)` helper.
- Timestamps: store ISO-8601 UTC; never local.
- Tests must use a tmp SQLite file, not in-memory, so FTS5 + transactions match production paths.
- No emojis, no docstring novels, no comments restating code. Comment only non-obvious *why*.
