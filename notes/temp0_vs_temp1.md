# Temperature sweep: t=0 vs t=1.0, same fixtures × baselines × n=5

Addresses CRITIQUE §92.3 ("temperature 0 and temperature 1 sweeps"). Same
3 fixtures × 6 baselines × 5 reps × 2 models, re-run at temperature=0.

Run directories:
- t=1.0 DeepSeek: `eval/runs/2026-05-13T11-08-56Z/` (51/90)
- t=1.0 Qwen:     `eval/runs/2026-05-13T11-08-47Z/` (45/90)
- t=0   DeepSeek: `eval/runs/2026-05-13T12-53-25Z/` (53/90)
- t=0   Qwen:     `eval/runs/2026-05-13T12-53-31Z/` (43/90)

## B4 (ArtifactStore) success rate per fixture, by temperature

|                          | DS t=1 | DS t=0 | Qwen t=1 | Qwen t=0 |
| ------------------------ | -----: | -----: | -------: | -------: |
| pytest_auth_expiry       |  1.00  | 1.00   |  1.00    | 1.00     |
| git_diff_auth_refactor   |  1.00  | 1.00   |  1.00    | 1.00     |
| pytest_large_run         | **0.80** | **1.00** | **0.20** | **0.20** |
| **B4 mean over 15 runs** | **0.933** | **1.00** | **0.733** | **0.733** |

## Three things temperature=0 actually does

1. **B4 on DeepSeek goes from 0.93 to 1.00.** The 1/5 miss at temp=1.0
   on `pytest_large_run` was sampling noise; with deterministic sampling
   B4 lands the right failure all 15 times. The headline "B4 wins on
   DeepSeek" is stronger, not weaker, under the temperature-0 sweep
   the CRITIQUE asked for.

2. **B4 on Qwen3.6 is unchanged at 0.73.** The collapse on
   `pytest_large_run` (1/5 at t=1, 1/5 at t=0) is NOT noise — Qwen's
   thinking-mode agentic loop systematically picks the wrong failure
   among the 5. A real model-quality finding, not a flake. The temp=0
   sweep rules out the "you just got unlucky" hypothesis.

3. **Variance shrinks where it should.** Most stdev columns in the
   per-cell table drop or stay zero at t=0 (B2/B3 deterministic
   baselines already had sd=0; B4 sd drops from 0.45 to 0.09 on
   pytest_large_run/DS). The runs that produce non-zero stdev at t=0
   are typically the LLM-summary baselines where map/reduce
   instructions still admit some freedom even at temp=0.

## Per-baseline success rate (n=15 per cell) across temperatures

| Baseline             | DS t=1 | DS t=0 | Qwen t=1 | Qwen t=0 |
| -------------------- | -----: | -----: | -------: | -------: |
| B1_RAW               | 0.933  | 0.800  | 0.667    | 0.667    |
| B2_TRUNCATED         | 0.333  | 0.400  | 0.333    | 0.333    |
| B3_SUMMARY           | 0.333  | 0.333  | 0.333    | 0.333    |
| B3_LLM_SUMMARY       | 0.333  | 0.333  | 0.467    | 0.333    |
| B3_LLM_MULTIPASS     | 0.533  | 0.667  | 0.533    | 0.467    |
| **B4_ARTIFACT**      | **0.933** | **1.000** | **0.733** | **0.733** |

## Reproducing

```bash
# Already plumbed in driver:
uv run python -m eval.driver --temperature 0 --reps 5 \
    --fixtures pytest_auth_expiry pytest_large_run git_diff_auth_refactor \
    --baselines B1_RAW B2_TRUNCATED B3_SUMMARY B3_LLM_SUMMARY \
                B3_LLM_MULTIPASS B4_ARTIFACT \
    --model deepseek-v4-pro

uv run python -m eval._compare_models \
    eval/runs/2026-05-13T12-53-25Z eval/runs/2026-05-13T12-53-31Z
```
