# ArtifactStore

> A typed, permission-scoped tool-result store for AI agent harnesses.

Modern tool-using agents produce large intermediate outputs — pytest logs,
ripgrep results, browser snapshots, API JSON, subagent research traces — and
the four canonical handling strategies all fail in distinct ways:

| strategy | failure mode |
|---|---|
| dump raw output into the transcript | doesn't scale; pollutes context |
| truncate to N tokens | strictly loses evidence past the cap |
| LLM-summarize | hallucinates or omits the decisive line |
| hide inside a worker subagent's summary | exact intermediate evidence unrecoverable in the parent |

ArtifactStore reframes a tool result as a **materialized artifact** with type,
preview, evidence spans, raw payload hash, provenance links, and access-control
metadata. Tools return a compact handle; the agent retrieves exact evidence
through scoped queries when needed.

```text
┌─ Supervisor agent ─────────────┐    grant_id only        ┌─ Subagent (scoped) ──┐
│  run_workload, create_grant,   │ ──────────────────────► │  artifact_search,    │
│  delegate, verify_citation,    │                         │  artifact_get_spans, │
│  expand_artifact               │                         │  artifact_expand_view│
└────────────┬───────────────────┘                         │  artifact_find_related│
             │ put_artifact, audit                         │  submit_report       │
             ▼                                             └──────────┬───────────┘
       ┌─────────────────────────  ArtifactStore API  ─────────────────┘
       │  search · get_spans · expand_view · create_grant · audit · find_related
       ▼
   SQLite + FTS5  (artifacts, artifact_spans, artifact_links,
                   artifact_grants, artifact_access_log, artifact_fts)
```

The full system architecture (data model, API contract, evaluation, threat
model) is in [`report/architecture.pdf`](report/architecture.pdf) (10 pages).
The authoritative spec is [`ArtifactStore_PLAN.md`](ArtifactStore_PLAN.md).

---

## Status

- **102 tests, all green.** Unit (predicate matching, sensitivity ordering,
  budget enforcement, citation parsing), integration (extractors against real
  fixtures, all five view materializers), offline e2e (full supervisor↔subagent
  flow via a scripted Anthropic SDK stub — no key needed), and live smoke.
- **Live evaluation done.** 48 runs across 4 fixtures × 4 baselines × 3 reps
  against `deepseek-v4-pro`. ~$0.10. Findings below.
- **Provider-agnostic.** Default `deepseek-v4-pro` via DeepSeek's
  Anthropic-compatible endpoint (~10× cheaper than Anthropic Sonnet 4.5);
  swap to native Anthropic with one env var.

---

## Quickstart

```bash
# Python 3.12 pinned via .python-version. uv handles the rest.
uv sync

# Run the test suite (no API key required)
uv run pytest -q
# 102 passed in <1s
```

### CLI

```bash
uv run artifactstore --help

uv run artifactstore init --db demo.db
uv run artifactstore put eval/fixtures/pytest_auth_expiry.log \
  --tool pytest --type pytest_failure --db demo.db
# -> art_xxxxxxxx

uv run artifactstore grant --agent worker \
  --types pytest_failure --views preview,evidence \
  --ops search,get_spans,expand_view --ttl 30m --db demo.db
# -> grant_xxxxxxxx

uv run artifactstore search "expired" --grant grant_xxxxxxxx --db demo.db
uv run artifactstore expand <art_id> --view evidence --grant grant_xxxxxxxx --db demo.db
uv run artifactstore audit --grant grant_xxxxxxxx --db demo.db
```

### Live demo (provider key required)

```bash
# 1. copy .env.example → .env, fill in your provider key
cp .env.example .env
$EDITOR .env

# 2. verify wiring (no API call, no charge)
uv run python -m demo.runner --check-config

# 3. tiny paid probe (~$0.0001) to confirm tool_use works on the provider
uv run python -m demo.runner --verify-tool-use

# 4. full supervisor↔subagent run (~$0.005 on DeepSeek V4 Pro)
uv run python -m demo.runner --kind pytest --target auth_expiry --verbose
```

A typical run: 7 supervisor turns, 1 delegated subagent submission with 5
formal citations, all citations verified, ~3 unauthorized `view='raw'`
attempts blocked under the narrow grant. The full audit log is queryable
via `artifactstore audit`.

### Evaluation

```bash
# 4 fixtures × 4 baselines × 3 reps = 48 runs (~$0.10 total on DeepSeek)
uv run python -m eval --reps 3
# -> writes eval/runs/<UTC-iso>/{config.json, result.jsonl, audit.csv, manifest.json}
```

---

## Provider configuration

The agent loop uses the `anthropic` Python SDK as its HTTP client. The same
SDK speaks DeepSeek's Anthropic-compatible endpoint when `ANTHROPIC_BASE_URL`
points at it. **`.env` is gitignored**; copy `.env.example` and fill it in.

```bash
# DeepSeek V4 Pro (default, recommended) — get key at platform.deepseek.com
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic

# OR native Anthropic — get key at console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-api03-...
# (omit ANTHROPIC_BASE_URL — SDK uses Anthropic's default)
```

Three pre-flight flags catch every category of provider misconfiguration we've
seen in practice before spending eval budget:

- `--check-config` — print resolved config (model, base_url, key presence).
  No network. No charge.
- `--verify-tool-use` — one paid call (~$0.0001) confirming the provider
  emits Anthropic-format `tool_use` blocks (vs OpenAI-style `function_call`).
- `--verify-tool-choice` — probes whether named/`any` `tool_choice` is honored.
  Reveals provider quirks (DeepSeek's reasoning models reject named
  `tool_choice` with HTTP 400).

---

## Headline evaluation results

48 live runs, `deepseek-v4-pro`, 4 fixtures (pytest small/large, ripgrep,
git-diff), 4 baselines, 3 reps each. Detailed analysis:
[`notes/eval_writeup.md`](notes/eval_writeup.md). Per-baseline aggregates
(n=12):

| baseline | task success | avg evidence recall | avg total tokens (in) | avg cost |
|---|---:|---:|---:|---:|
| **B1** raw injection | 100% | 0.95 | 1,341 | $0.0015 |
| **B2** truncated to 200 tok | 50% | 0.43 | 330 | $0.0008 |
| **B3** offline summary | 50% | 0.45 | 256 | $0.0009 |
| **B4** ArtifactStore | **100%** | **0.94** | 21,572 | $0.0051 |

The headline framing is **not** "B4 saves the most tokens" — it's:

> ArtifactStore (B4) is the only baseline that combines reliable success
> *and* exact-evidence recovery (EER=1.00 on every fixture) *and* formal
> citations (3-7 per run, all resolve via `cite.verify_resolves`) *and*
> per-read audit-log signal. B2 and B3 strictly lose evidence in
> fixture-dependent ways. B1 doesn't scale.

For RQ1 (token efficiency), B4's `tot_in` is higher than B1's because
multi-turn loops accumulate context across turns. The fairer comparison is
**cost-per-success**: at 3.5K-token fixtures, B4 costs $0.0032 vs B1's
$0.0025 — a 28% premium for strict EER. The crossover happens around 5K
tokens; B1's input scales with fixture size while B4's stays bounded by the
model's tool-use intent.

For RQ4 (permission enforcement), the demo runner with a narrow grant
blocks 3 unauthorized `view='raw'` reads organically and logs them with
specific `denial_reason` strings — a non-zero RQ4 signal under realistic
agent behavior.

---

## Layout

```
artifactstore/        ← the contribution (~1.7 kLOC)
  schema.sql          ← DDL for 6 tables (PLAN §7)
  store.py            ← public API (PLAN §9): put/search/get_spans/expand_view/find_related/create_grant/audit
  extractors.py       ← type→span registry (pytest_failure, grep_result, git_diff)
  previews.py         ← type→preview registry; spans-inline preview body
  views.py            ← preview / evidence / redacted / raw / provenance
  grants.py           ← predicate eval, op/view checks, expiry, cumulative budget, audit log
  cite.py             ← citation parse + DB resolution (art_xxx/span_yyy)
  cli.py              ← typer CLI (`uv run artifactstore ...`)
demo/                 ← the test bench (PLAN §20)
  agent.py            ← ~150-LOC client-side tool-use loop, provider-agnostic
  tools.py            ← supervisor + subagent tool surfaces
  prompts.py          ← system prompts (verbose-budget-aware)
  workloads.py        ← run_workload + ViewPolicy (RAW/TRUNCATED/SUMMARY/ARTIFACT)
  runner.py           ← demo entrypoint + .env loader + --check-config / --verify-* probes
eval/
  fixtures/           ← captured pytest/grep/git-diff outputs + .gold.json truth files
  driver.py           ← PLAN §11.1 sweep across 4 baselines × N fixtures × M reps
  baselines.py        ← B1/B2/B3/B4 setup builders
  metrics.py          ← evidence_recall, citation_validity, exact_evidence_recovery, blocked_reads
  runs/               ← gitignored output: config.json, result.jsonl, audit.csv, manifest.json
report/
  architecture.typ    ← Typst source for the architecture report
  architecture.pdf    ← rendered (10 pages)
notes/
  agent_design.md     ← research notes — canonical loop, hard rules, pitfalls
  eval_writeup.md     ← detailed eval analysis with per-fixture breakdown
tests/                ← 102 tests; pytest config in pyproject.toml
ArtifactStore_PLAN.md ← authoritative spec (read this before changing the data model or API)
CLAUDE.md             ← agent-instruction file with locked design choices
```

---

## Stack

- **Python 3.12** pinned via `.python-version`, managed with [`uv`](https://github.com/astral-sh/uv)
- **SQLite (stdlib) + FTS5** for storage; chosen over DuckDB because FTS5 is
  built-in and the prototype scale doesn't need columnar
- **Anthropic Messages API shape** as the agent transport — provider-agnostic
  via `ANTHROPIC_BASE_URL`
- **Typer** for the CLI
- **pytest** for tests; **Typst** for the architecture report
- Optional: `tiktoken` for accurate token counting (falls back to `len/4`)

---

## Contributing / extending

The data model is small but principled. Read [`ArtifactStore_PLAN.md`](ArtifactStore_PLAN.md)
§7 (data model) and §17 (out-of-scope) before changing the schema or API surface.
[`CLAUDE.md`](CLAUDE.md) has the locked design choices (raw=BLOB, sha256,
≤256-tok preview, citation regex, sensitivity ordering, omit-fits budget,
cumulative grant budget, etc.).

To add support for a new artifact type:

1. Register an extractor in [`artifactstore/extractors.py`](artifactstore/extractors.py)
   that yields `(span_type, file_path, line_start, line_end, text, importance)`
   tuples.
2. (Optional) Register a type-specific preview summary in
   [`artifactstore/previews.py`](artifactstore/previews.py).
3. Add a fixture in `eval/fixtures/<name>.<ext>` plus a sibling
   `<name>.gold.json` declaring the truth-set keywords / must-contain spans.
4. Add the fixture to `FIXTURE_REGISTRY` in [`eval/driver.py`](eval/driver.py).

Tests live alongside in `tests/`. For a new artifact type:
`tests/test_extractors.py::test_<type>_finds_<thing>` against the captured
fixture is the canonical entry point.

---

## License & attribution

Research prototype for a DBMS course. The agent loop in `demo/agent.py` is
adapted from Anthropic's MIT-licensed
[anthropic-quickstarts/agents](https://github.com/anthropics/anthropic-quickstarts).
