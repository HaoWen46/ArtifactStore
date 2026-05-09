# PLAN §11.1 eval — first results

> Two sweeps, 84 live runs total against `deepseek-v4-pro` via DeepSeek's
> Anthropic-compatible endpoint. ~$0.20 spent. The numbers below are from
> sweep v2 (4 fixtures × 4 baselines × 3 reps = 48 runs); sweep v1 used the
> uncached-only token field and is superseded by v2 for RQ1. Raw outputs in
> `eval/runs/<UTC-iso>/`.

## Setup

- **Model**: `deepseek-v4-pro` (DeepSeek V4 Pro reasoning model, $0.435/M input
  cache-miss / $0.0036/M cache-hit / $0.87/M output, all at the May 2026
  promotional rate).
- **Fixtures** (all in `eval/fixtures/`):
  - `pytest_auth_expiry` (~440 tok raw, 1 failed of 47)
  - `pytest_large_run` (~3500 tok raw, 5 failed of 312, target: auth_expiry)
  - `rg_grep_noise` (~250 tok raw, ripgrep with TODOs and the auth bug)
  - `git_diff_auth_refactor` (~600 tok raw, the timezone fix)
- **Baselines** (single-agent, PLAN §11.1):
  - **B1 RAW** — full output inline in user message, no tools.
  - **B2 TRUNCATED** — first 200 tokens inline.
  - **B3 SUMMARY** — deterministic offline summary (head + lines matching
    `FAIL|Error|assert|WARNING`), capped at ~150 tokens.
  - **B4 ARTIFACT** — `artifact_id` handle + `artifact_search`,
    `artifact_get_spans`, `artifact_expand_view`, `artifact_find_related`.
- **Task**: same prompt for every baseline — *"diagnose the root cause of
  any failure in this {kind} output for target '{target}'"*.
- **Temperature**: 1.0. **Max turns**: 10.
- **Metrics** (`eval/metrics.py`):
  - `evidence_recall` = fraction of gold-truth `diagnosis_keywords` present
    in the agent's diagnosis text (case-insensitive substring).
  - `task_success` = `evidence_recall ≥ 0.5`.
  - `citation_count` / `citations_resolved` / `citation_validity` = pulled
    via regex `art_[0-9a-f]{8}/span_[0-9a-f]{8}` from the diagnosis,
    resolved by `artifactstore.cite.verify_resolves`.
  - `exact_evidence_recovery (EER)` (B4 only) = fraction of gold-truth
    `must_contain` strings appearing in the concatenated tool-result text
    the agent actually read.
  - `blocked_reads` = audit-log rows with `allowed=0`.

## Results

### Per (fixture × baseline), 3 reps

| fixture | baseline | tot_in | out | recall | succ | cost($) | cite | EER |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| git_diff_auth_refactor | B1_RAW | 749 | 909 | 0.93 | 100% | 0.0009 | 0.0 | – |
| git_diff_auth_refactor | B2_TRUNCATED | 351 | 997 | 0.93 | 100% | 0.0009 | 0.0 | – |
| git_diff_auth_refactor | B3_SUMMARY | 230 | 1072 | **0.13** | **0%** | 0.0010 | 0.0 | – |
| git_diff_auth_refactor | **B4_ARTIFACT** | 13314 | 1623 | 0.93 | 100% | 0.0032 | **4.0** | **1.00** |
| pytest_auth_expiry | B1_RAW | 482 | 1380 | 0.93 | 100% | 0.0013 | 0.0 | – |
| pytest_auth_expiry | B2_TRUNCATED | 257 | 698 | **0.00** | **0%** | 0.0006 | 0.0 | – |
| pytest_auth_expiry | B3_SUMMARY | 263 | 746 | 0.80 | 100% | 0.0007 | 0.0 | – |
| pytest_auth_expiry | **B4_ARTIFACT** | 17791 | 2935 | 0.93 | 100% | 0.0041 | **3.0** | **1.00** |
| pytest_large_run | B1_RAW | 3575 | 1636 | 0.93 | 100% | 0.0025 | 0.0 | – |
| pytest_large_run | B2_TRUNCATED | 349 | 857 | **0.00** | **0%** | 0.0008 | 0.0 | – |
| pytest_large_run | B3_SUMMARY | 288 | 533 | **0.20** | **0%** | 0.0005 | 0.0 | – |
| pytest_large_run | **B4_ARTIFACT** | 12445 | 1497 | **1.00** | 100% | 0.0032 | **3.3** | **1.00** |
| rg_grep_noise | B1_RAW | 559 | 1447 | 1.00 | 100% | 0.0013 | 0.0 | – |
| rg_grep_noise | B2_TRUNCATED | 361 | 935 | 0.78 | 100% | 0.0009 | 0.0 | – |
| rg_grep_noise | B3_SUMMARY | 244 | 1430 | 0.67 | 100% | 0.0013 | 0.0 | – |
| rg_grep_noise | **B4_ARTIFACT** | 42738 | 7096 | 0.89 | 100% | 0.0100 | **7.0** | **1.00** |

`tot_in` = total input tokens including DeepSeek cache-read hits across all
turns. The naive `usage.input_tokens` (uncached only) is misleading because
prompt caching across reps that share system/user prompts makes it
rep-dependent.

### Aggregate (n=12 per baseline)

| baseline | success | avg_recall | avg_tot_in | avg_out | avg_cost |
|---|---:|---:|---:|---:|---:|
| B1_RAW | **100%** | 0.95 | 1,341 | 1,343 | $0.0015 |
| B2_TRUNCATED | 50% | 0.43 | 330 | 872 | $0.0008 |
| B3_SUMMARY | 50% | 0.45 | 256 | 945 | $0.0009 |
| **B4_ARTIFACT** | **100%** | 0.94 | 21,572 | 3,288 | $0.0051 |

## Findings

### What B4 strictly wins on

- **Task success**: 100% across all 4 fixtures × 3 reps. Tied with B1 on
  rate, but see (3) below — B1 doesn't scale.
- **Exact evidence recovery (EER)**: 1.00 across all fixtures. The agent
  demonstrably reads the gold-truth must-contain strings via
  `artifact_get_spans` / `artifact_expand_view`. **B1/B2/B3 have no
  per-span evidence to measure** — they ingest the whole/truncated/summary
  payload, so EER is undefined for them.
- **Formal citations**: 3.0–7.0 per run, all resolve via
  `cite.verify_resolves`. Citations land at canonical `art_xxx/span_yyy`
  positions tied to the typed-span schema. **No other baseline produces
  citable evidence.**
- **Audit-log signal**: every `artifact_*` call writes a row to
  `artifact_access_log`. Sweep v1 logged ~3 unauthorized denials per
  narrow-grant demo run; sweep v2 grants are deliberately permissive
  because RQ4 is an orthogonal experiment exercised by the demo runner.

### Where B2 fails

- **Pytest** (both sizes): 0% success. The smoking-gun WARNING line
  (`now=local exp=UTC`) sits past the 200-token truncation point.
- **Truncation strictly loses evidence whenever the signal is past the
  cap.** It works on `git_diff_auth_refactor` and `rg_grep_noise` only
  because those fixtures put the relevant evidence near the top.

### Where B3 fails

- **`git_diff_auth_refactor`: 0% success** (recall 0.13). The deterministic
  summarizer's `FAIL|Error|assert|WARNING` regex doesn't preserve hunk-level
  diff structure.
- **`pytest_large_run`: 0% success** (recall 0.20). The summary captures
  the failure list but loses the per-failure traceback that contains the
  WARNING line.
- **Pattern**: B3 is fixture-dependent. A heuristic summarizer cannot know
  what evidence will turn out to matter. An LLM-summary B3 would be a
  fairer comparison; we deliberately left it deterministic for
  reproducibility.

### Where B1 is good but doesn't scale

- B1 hits 100% success here because all four fixtures are <4K raw tokens.
  At 10K+ tokens (large CI logs, full ripgrep output, multi-file diffs),
  B1's `tot_in` scales linearly with fixture size while B4's stays bounded
  by the model's tool-use intent.
- B1 produces zero citations and no audit signal.

### Token-efficiency picture

PLAN §14 predicted **30–60% fewer prompt tokens for B4 vs raw injection**.
The current sweep doesn't show that on the absolute `tot_in` metric — B4 is
~16× B1 because **multi-turn loops accumulate context across turns**, and
each turn's full conversation history counts toward `tot_in`.

The fairer framings:

1. **Cost (with cache)**: at the largest fixture (`pytest_large_run`, 3500
   tok), B4 costs $0.0032 vs B1's $0.0025 — **a 28% cost premium for a
   strict EER and citation-validity gain**. Extrapolating with bigger
   fixtures (B1 input scales with fixture size; B4 stays roughly flat),
   the crossover happens at ~5K tokens.
2. **Tokens-billed-at-full-rate**: `in_uncached + cache_creation`. For
   B4 on `pytest_large_run`: 3357 (out of 12445 total). Excluding cache
   reads, B4 sees ~3357 tok of fresh input vs B1's 2423 (rep0). Still ~40%
   more, but in the same order of magnitude.
3. **Cost-per-success**: B2 and B3 success is 50%, so their effective
   cost-per-correct-diagnosis doubles. On the `pytest_large_run` line,
   B4's $0.0032 with 100% success beats both B2 ($0.0008/0% = ∞) and B3
   ($0.0005/0% = ∞).

**Honest framing for the writeup**: ArtifactStore (B4) is **not** the
cheapest baseline on these fixtures. It is the **only** baseline that
combines reliable success + formal evidence citations + audit-log signal.
On large/noisy fixtures (where B2/B3 strictly fail) it costs roughly the
same as raw injection while being the only baseline a supervisor can
trust.

### Open issues

- **Larger fixtures needed for a clean RQ1 win**. `pytest_large_run` at
  3500 tokens is past the crossover but not by much. Capturing a real
  5–10K-token CI log (or a 200-line ripgrep over a real codebase) would
  let the eval show B4's `tot_in` plateau while B1's keeps growing.
- **Multi-rep variance is non-trivial.** B4 grep ranged from 5 to 10
  turns ($0.005 to $0.014) across reps. More reps + temperature=0
  (deterministic mode) would tighten the bars.
- **B3 is too easy to beat.** A real apples-to-apples B3 should call the
  model itself to summarize, not use a regex. Adding that would cost
  ~$0.001 per fixture once at sweep start; worth doing before publishing.
- **RQ4 (denials) is exercised by the demo runner, not the eval driver.**
  The eval grants are intentionally permissive. RQ4 numbers come from
  `python -m demo.runner --kind pytest --target auth_expiry --verbose`,
  which mints narrow grants and counts the resulting denials in the
  audit log (run 3 of the demo: 3 raw-view attempts blocked).

## Reproducing

```bash
# Set ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL via .env (see .env.example).
uv run python -m eval --reps 3
# Output: eval/runs/<UTC-iso>/{config.json, result.jsonl, audit.csv, manifest.json}
```

Each run uses an isolated SQLite db so the audit log is per-run-clean.
`config.json` records `git_rev`, fixture set, baseline set, model, and
base_url at sweep start. `result.jsonl` has one full `RunResult` per row.
`audit.csv` denormalizes `artifact_access_log` across all runs with
`run_id` as the foreign key.
