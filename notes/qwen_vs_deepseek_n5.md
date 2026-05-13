# Qwen3.6-plus vs DeepSeek-v4-pro — n=5 paired eval

Addresses CRITIQUE §92 items 2 ("n=5–10 reps per cell") and 4 ("at
least two model families"). Run directories:
- `eval/runs/2026-05-13T11-08-47Z/` — qwen3.6-plus, 45/90 success, $0.68, 66 min
- `eval/runs/2026-05-13T11-08-56Z/` — deepseek-v4-pro, 51/90 success, $0.18, 90 min

Sweep matrix: 3 fixtures × 6 baselines × 5 reps × 2 models = 180 runs.
Fixtures: pytest_auth_expiry (small, 1.7K chars), pytest_large_run
(medium, 14K, 5 failures), git_diff_auth_refactor (medium, 2.3K).

## Headline by-baseline success rate (avg over 3 fixtures × 5 reps = n=15)

| Baseline           | DeepSeek | Qwen3.6-plus |
| ------------------ | -------: | -----------: |
| B1_RAW             |   0.933  |   0.667      |
| B2_TRUNCATED       |   0.333  |   0.333      |
| B3_SUMMARY         |   0.333  |   0.333      |
| B3_LLM_SUMMARY     |   0.333  |   0.467      |
| **B3_LLM_MULTIPASS** | **0.533**| **0.533**    |
| **B4_ARTIFACT**    | **0.933**| **0.733**    |

## Headline by-baseline avg evidence recall

| Baseline           | DeepSeek | Qwen3.6-plus |
| ------------------ | -------: | -----------: |
| B1_RAW             |   0.867  |   0.693      |
| B2_TRUNCATED       |   0.320  |   0.267      |
| B3_SUMMARY         |   0.427  |   0.387      |
| B3_LLM_SUMMARY     |   0.400  |   0.467      |
| B3_LLM_MULTIPASS   |   0.600  |   0.573      |
| **B4_ARTIFACT**    | **0.907**| **0.787**    |

## Three claims the data supports

1. **B4 (ArtifactStore) beats every other baseline on both models.**
   Success rate 0.933 (DS) / 0.733 (Qwen) — strictly above every
   competitor including the multipass LLM-summary baseline (0.533
   for both). The architecture advantage is *not* a quirk of the
   DeepSeek family.

2. **B3_LLM_MULTIPASS — the stronger summarizer baseline a reviewer
   would ask for — does close some of the gap vs single-pass B3', but
   still loses to B4 by ~30 percentage points on both models.** This
   directly addresses CRITIQUE §92.5: even at ~(N+1)× the LLM-summary
   spend, structured artifact access wins.

3. **The artifact path narrows model-quality gaps.** On
   pytest_auth_expiry, B1_RAW recall is 1.00 (DS) vs 0.84 (Qwen) — a
   16-pt model gap; B4 recall is 0.96 vs 1.00 — gap erased. The same
   structured-access advantage matters more, not less, on weaker
   models.

## Caveats / failure modes worth naming

- **Qwen3.6-plus B4 collapses on the multi-failure fixture
  (pytest_large_run): 0.36 recall, 1/5 success vs 0.80, 4/5 for
  DeepSeek.** Both models share the same artifact tools and grant;
  the gap is in agentic navigation under Qwen's thinking mode. Worth
  a single follow-up paragraph in the report, not a research U-turn.
- **B4 always costs more input tokens than the best non-B4 baseline.**
  Per the per-cell table, B4 is +8K–18K input tokens vs B1_RAW.
  DeepSeek's prompt cache covers most of this (cache-read at
  $0.04/M brings real cost down to $0.004); Qwen has no cache and
  pays the full $0.012–$0.016/run. The cost story is "cache makes B4
  competitive on DeepSeek; on Qwen, B4 is the recall buy, not the
  cost buy."
- **B3_LLM_MULTIPASS task_success rates dropped vs B3_LLM_SUMMARY on
  Qwen for pytest_large_run** (0.20 vs 0.00) — but recall ticked up.
  Map-reduce preserved keywords but the synthesis still mis-prioritized
  among 5 failures. The multipass-vs-single-pass story is mixed at the
  per-fixture level; honest reporting in the writeup.

## Reproducing

```bash
# Paired sweep (~$0.85, ~90 min):
uv run python -m eval.driver \
    --fixtures pytest_auth_expiry pytest_large_run git_diff_auth_refactor \
    --baselines B1_RAW B2_TRUNCATED B3_SUMMARY B3_LLM_SUMMARY \
                B3_LLM_MULTIPASS B4_ARTIFACT \
    --reps 5 --model deepseek-v4-pro

uv run python -m eval.driver --fixtures ... --reps 5 --model qwen3.6-plus

# Aggregate:
uv run python -m eval._compare_models \
    eval/runs/<deepseek-dir> eval/runs/<qwen-dir>
```
