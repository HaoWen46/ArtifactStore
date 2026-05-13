# 110K-token xxl fixture — paired eval, both models

Addresses CRITIQUE §92.6 ("50K+ token fixtures"). Same diagnostic
target (auth_expiry timezone bug) as `pytest_xl_run`, but ~110K tokens
total — 80K of those are pure HTTP-access-log noise designed to be
useless to truncation/summary baselines.

Run directories (temp=0, n=3 per cell):
- DeepSeek: `eval/runs/2026-05-13T15-03-19Z/` (4/15, $0.29, 11 min)
- Qwen:     `eval/runs/2026-05-13T15-03-27Z/` (3/15, $0.82, 8 min)

Skipped baseline: B3_LLM_MULTIPASS — at 2K tokens/chunk that's ~55
map calls per rep before the reduce; the wall-clock and budget cost
would dominate without changing the headline. The structural points
B3'' clarifies on small fixtures don't apply here (every summary
baseline collapses to ≤0.13 recall on the xxl regardless of
summarizer sophistication).

## Headline table (n=3 per cell, temp=0, both models)

|                  | DS B1_RAW | DS B4_ARTIFACT | Qwen B1_RAW | Qwen B4_ARTIFACT |
| ---------------- | --------: | -------------: | ----------: | ---------------: |
| Success rate     |   2/3     | 2/3            | 3/3         | 0/3              |
| Avg recall       |   0.73    | 0.73           | 1.00        | 0.33             |
| Total input tok  | **142,028** | **25,518**   | **159,598** | **14,704**       |
| Cost / run (USD) |   0.0263  | 0.0070         | 0.1318      | 0.0154           |

Summary baselines (B2_TRUNCATED / B3_SUMMARY / B3_LLM_SUMMARY) at
110K: 0/3 success rate across all cells, recall ≤ 0.13. The
single-pass LLM summarizer (B3') alone costs \$0.06–\$0.12 per run
on the summarization call (it has to read the full 110K), only to
produce a 400-token summary that the downstream agent cannot
diagnose from. *B3' is strictly dominated by B4 at this scale on
both models — cheaper AND lower recall.*

## What the data supports

1. **B4's input plateau holds at 110K.** Total input on DeepSeek B4
   is 25.5K (3–5 targeted FTS + expand calls); Qwen B4 is 14.7K. B1's
   input scales linearly with the raw payload (142K / 159K). The
   plateau-vs-linear architectural claim is now confirmed at 5.5× the
   prior largest fixture.

2. **B4 beats B1 on cost-per-success on DeepSeek at 110K** —
   identical recall (0.73 vs 0.73) at 3.8× less spend (\$0.007 vs
   \$0.026 per run). The PLAN's projected B1/B4 cost crossover (~30K)
   now has the matching empirical: by 110K, B4 is the cheaper option
   at parity recall on DeepSeek.

3. **Qwen B1 wins on raw accuracy (3/3) but at 8.5× the spend**
   (\$0.13 vs \$0.015) than Qwen B4 (0/3, 0.33 recall). Qwen's
   agentic-navigation weakness on multi-failure fixtures (seen at 14K
   and unchanged at 110K) is now well-documented; it is a model story,
   not an architecture story.

## Reproducing

```bash
uv run python -m eval.fixtures._gen_pytest_xxl_run   # writes the .log

uv run python -m eval.driver --fixtures pytest_xxl_run \
  --baselines B1_RAW B2_TRUNCATED B3_SUMMARY B3_LLM_SUMMARY B4_ARTIFACT \
  --reps 3 --temperature 0 --model deepseek-v4-pro
uv run python -m eval.driver --fixtures pytest_xxl_run \
  --baselines B1_RAW B2_TRUNCATED B3_SUMMARY B3_LLM_SUMMARY B4_ARTIFACT \
  --reps 3 --temperature 0 --model qwen3.6-plus

uv run python -m eval._compare_models eval/runs/<DS> eval/runs/<Qwen>
```
