"""Cross-model comparison aggregator.

Reads two (or more) `eval/runs/<utc>/` directories — typically one per
model family — and emits a tidy CSV with per-(fixture, baseline, model)
aggregates plus a paired view that puts the two models side-by-side.

The paired view is what reviewers actually want to read: does
ArtifactStore (B4) still dominate when the model family changes from
DeepSeek to Qwen?

Usage:
  uv run python -m eval._compare_models <run_dir_A> <run_dir_B> [...]
  uv run python -m eval._compare_models eval/runs/2026-05-13T11-08-47Z \\
                                       eval/runs/2026-05-13T11-08-56Z
"""
from __future__ import annotations

import argparse
import json
import statistics as stats
from collections import defaultdict
from pathlib import Path


def _mean(xs: list[float]) -> float:
    return stats.mean(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    return stats.stdev(xs) if len(xs) > 1 else 0.0


def _load_runs(run_dir: Path) -> tuple[str, list[dict]]:
    """Return (model_id, rows). Reads config.json for the model id
    (so the caller doesn't have to remember which dir is which)."""
    cfg = json.loads((run_dir / "config.json").read_text())
    model = cfg.get("model", "(unknown)")
    rows: list[dict] = []
    rfile = run_dir / "result.jsonl"
    if rfile.is_file():
        for line in rfile.read_text().splitlines():
            rows.append(json.loads(line))
    return model, rows


def aggregate(run_dirs: list[Path]) -> None:
    # (fixture, baseline, model) -> list[row]
    cells: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    models: list[str] = []
    for d in run_dirs:
        model, rows = _load_runs(d)
        if model not in models:
            models.append(model)
        for r in rows:
            cells[(r["fixture"], r["baseline"], model)].append(r)

    # Per-cell summary table.
    print("# Per-cell summary")
    print("fixture,baseline,model,n,success_rate,"
          "recall_mean,recall_sd,"
          "total_in_mean,total_in_sd,"
          "out_mean,cost_mean")
    keys = sorted(cells.keys())
    for (fx, bl, m) in keys:
        rs = cells[(fx, bl, m)]
        n = len(rs)
        succ = sum(1 for r in rs if r.get("task_success")) / max(1, n)
        recalls = [r["evidence_recall"] for r in rs if "evidence_recall" in r]
        tins = [r.get("total_input_tokens") or r.get("input_tokens", 0)
                for r in rs]
        outs = [r.get("output_tokens", 0) for r in rs]
        costs = [r.get("estimated_cost_usd", 0.0) for r in rs]
        print(f"{fx},{bl},{m},{n},{succ:.2f},"
              f"{_mean(recalls):.3f},{_stdev(recalls):.3f},"
              f"{_mean(tins):.0f},{_stdev(tins):.0f},"
              f"{_mean(outs):.0f},{_mean(costs):.5f}")

    if len(models) < 2:
        return

    # Side-by-side paired view: for each (fixture, baseline), print one
    # row per model, plus a delta. Helps reviewers eyeball whether B4's
    # win is robust across model families.
    print()
    print("# Paired view (model A vs B)")
    a_model, b_model = models[0], models[1]
    print(f"# A = {a_model}, B = {b_model}")
    print(f"fixture,baseline,"
          f"n_A,success_A,recall_A,tin_A,cost_A,"
          f"n_B,success_B,recall_B,tin_B,cost_B,"
          f"d_recall,d_tin")
    pair_keys = sorted(
        {(fx, bl) for (fx, bl, _) in cells.keys()},
    )
    for (fx, bl) in pair_keys:
        rs_a = cells.get((fx, bl, a_model), [])
        rs_b = cells.get((fx, bl, b_model), [])
        if not rs_a and not rs_b:
            continue
        def stats_for(rs):
            n = len(rs)
            if n == 0:
                return 0, 0.0, 0.0, 0.0, 0.0
            succ = sum(1 for r in rs if r.get("task_success")) / n
            rec = _mean([r["evidence_recall"] for r in rs])
            tin = _mean([r.get("total_input_tokens")
                         or r.get("input_tokens", 0) for r in rs])
            cost = _mean([r.get("estimated_cost_usd", 0.0) for r in rs])
            return n, succ, rec, tin, cost
        nA, sA, rA, tA, cA = stats_for(rs_a)
        nB, sB, rB, tB, cB = stats_for(rs_b)
        d_rec = rB - rA
        d_tin = tB - tA
        print(f"{fx},{bl},"
              f"{nA},{sA:.2f},{rA:.3f},{tA:.0f},{cA:.5f},"
              f"{nB},{sB:.2f},{rB:.3f},{tB:.0f},{cB:.5f},"
              f"{d_rec:+.3f},{d_tin:+.0f}")

    # Headline: B4 vs best-of(B1,B2,B3,B3_LLM_SUMMARY,B3_LLM_MULTIPASS)
    # per fixture, per model. The CRITIQUE wants to know if the
    # ArtifactStore win holds across models — this row is the answer.
    print()
    print("# Headline: B4 vs best-non-B4, per model")
    print(f"# (best-non-B4 baseline picked per (fixture, model) by recall mean)")
    print("fixture,model,n,b4_recall,b4_tin,b4_cost,"
          "best_non_b4,best_recall,best_tin,best_cost,"
          "d_recall,d_tin,d_cost")
    fixtures_seen = sorted({fx for (fx, _, _) in cells.keys()})
    NON_B4 = ["B1_RAW", "B2_TRUNCATED", "B3_SUMMARY",
              "B3_LLM_SUMMARY", "B3_LLM_MULTIPASS"]
    for fx in fixtures_seen:
        for m in models:
            b4 = cells.get((fx, "B4_ARTIFACT", m), [])
            if not b4:
                continue
            b4_n = len(b4)
            b4_r = _mean([r["evidence_recall"] for r in b4])
            b4_t = _mean([r.get("total_input_tokens")
                          or r.get("input_tokens", 0) for r in b4])
            b4_c = _mean([r.get("estimated_cost_usd", 0.0) for r in b4])
            # Pick best non-B4 baseline by recall mean.
            best_b = None
            best_r = -1.0
            best_t = best_c = 0.0
            for bl in NON_B4:
                rs = cells.get((fx, bl, m), [])
                if not rs:
                    continue
                rmean = _mean([r["evidence_recall"] for r in rs])
                if rmean > best_r:
                    best_r = rmean
                    best_b = bl
                    best_t = _mean([r.get("total_input_tokens")
                                    or r.get("input_tokens", 0) for r in rs])
                    best_c = _mean([r.get("estimated_cost_usd", 0.0)
                                    for r in rs])
            if best_b is None:
                continue
            print(f"{fx},{m},{b4_n},{b4_r:.3f},{b4_t:.0f},{b4_c:.5f},"
                  f"{best_b},{best_r:.3f},{best_t:.0f},{best_c:.5f},"
                  f"{b4_r-best_r:+.3f},{b4_t-best_t:+.0f},"
                  f"{b4_c-best_c:+.5f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("run_dirs", nargs="+", type=Path)
    args = p.parse_args()
    aggregate(args.run_dirs)


if __name__ == "__main__":
    main()
