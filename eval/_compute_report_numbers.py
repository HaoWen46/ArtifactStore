"""Aggregate committed result.jsonl files into the numbers used in the
architecture report's §8 tables and plots. Outputs a Markdown table for
direct paste into the Typst source.

Critique-driven additions vs the prior `_aggregate_for_report.py`:
- Wilson 95% CIs on success rates (small-n statistical honesty).
- Per-fixture latency mean + range (cost accounting per CRITIQUE §5).
- Combines multiple run dirs by baseline+fixture; doesn't silently
  shadow.

Usage:
    uv run python eval/_compute_report_numbers.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "eval" / "runs"
FIXTURE_ORDER = [
    ("rg_grep_noise", 407),
    ("pytest_auth_expiry", 444),
    ("git_diff_auth_refactor", 577),
    ("pytest_large_run", 3480),
    ("pytest_ci_run", 9609),
]
SINGLE_BASELINES = ["B1_RAW", "B2_TRUNCATED", "B3_SUMMARY", "B3_LLM_SUMMARY", "B4_ARTIFACT"]
DELEG_STRATEGIES = ["D1_SUMMARY", "D1_LLM_SUMMARY", "D2_FULL_CONTEXT", "D3_SCOPED"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval for binomial proportion. Returns (p, lo, hi)."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    den = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / den
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den
    return p, max(0.0, cen - half), min(1.0, cen + half)


def load_single() -> list[dict]:
    """Load all single-agent rows. Excludes the target-leak control sweep
    (which uses reveal_target=False so headline numbers don't conflate)."""
    rows: list[dict] = []
    for d in sorted(RUNS_DIR.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith("delegation_"):
            continue
        # Distinguish the target-leak control: pytest_large_run sweep with
        # reveal_target=False is a separate scientific question, not the
        # headline sweep.
        cfg_path = d / "config.json"
        if not cfg_path.exists():
            continue
        cfg = json.loads(cfg_path.read_text())
        is_target_leak = (
            list(cfg.get("fixtures", [])) == ["pytest_large_run"]
            and cfg.get("fixture_registry", {}).get("pytest_large_run", {}).get("reveal_target") is False
        )
        if is_target_leak:
            continue
        result_path = d / "result.jsonl"
        if not result_path.exists():
            continue
        for line in result_path.open():
            r = json.loads(line)
            r["_run_dir"] = d.name
            rows.append(r)
    return rows


def load_deleg() -> list[dict]:
    rows: list[dict] = []
    for d in sorted(RUNS_DIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("delegation_"):
            continue
        result_path = d / "result.jsonl"
        if not result_path.exists():
            continue
        for line in result_path.open():
            r = json.loads(line)
            r["_run_dir"] = d.name
            rows.append(r)
    return rows


def cell_stats(rows: list[dict], success_field: str = "task_success") -> dict:
    """Aggregate cell-level stats with Wilson CI."""
    n = len(rows)
    k = sum(1 for r in rows if r.get(success_field))
    p, lo, hi = wilson(k, n)
    recalls = [r.get("evidence_recall", 0) for r in rows]
    costs = [r.get("estimated_cost_usd", 0) for r in rows]
    secs = [r.get("elapsed_seconds", 0) for r in rows]
    return {
        "n": n,
        "succ_k": k,
        "succ_p": p,
        "succ_lo": lo,
        "succ_hi": hi,
        "avg_recall": sum(recalls) / n if n else 0,
        "avg_cost": sum(costs) / n if n else 0,
        "avg_sec": sum(secs) / n if n else 0,
        "med_sec": statistics.median(secs) if n else 0,
        "min_sec": min(secs) if n else 0,
        "max_sec": max(secs) if n else 0,
    }


def main():
    single = load_single()
    deleg = load_deleg()
    print(f"loaded {len(single)} single-agent rows, {len(deleg)} delegation rows", file=sys.stderr)

    # --- Single-agent table ---
    print("\n=== §8.1 single-agent aggregate (by baseline, all fixtures) ===")
    print(f"{'Baseline':18s}  {'n':>3s}  {'succ':>8s}  {'95% CI':>14s}  {'recall':>6s}  {'cost':>8s}  {'lat(s)':>7s}")
    by_b = defaultdict(list)
    for r in single:
        by_b[r["baseline"]].append(r)
    for b in SINGLE_BASELINES:
        rs = by_b.get(b, [])
        if not rs:
            print(f"{b:18s}  ---  no data")
            continue
        s = cell_stats(rs)
        print(f"{b:18s}  {s['n']:>3d}  {s['succ_k']}/{s['n']:<5} ({s['succ_p']:.2f})  [{s['succ_lo']:.2f},{s['succ_hi']:.2f}]  {s['avg_recall']:>6.2f}  ${s['avg_cost']:>6.4f}  {s['avg_sec']:>7.1f}")

    # --- Per-fixture single-agent ---
    print("\n=== Per-fixture single-agent (highest-leverage data) ===")
    by_bf = defaultdict(list)
    for r in single:
        by_bf[(r["baseline"], r["fixture"])].append(r)
    for fix, raw in FIXTURE_ORDER:
        print(f"\n  Fixture: {fix} ({raw} raw tokens)")
        print(f"  {'Baseline':18s}  {'n':>2s}  {'succ':>7s}  {'recall':>6s}  {'tot_in':>7s}  {'cost':>8s}  {'lat(s)':>7s}")
        for b in SINGLE_BASELINES:
            rs = by_bf.get((b, fix), [])
            if not rs:
                continue
            s = cell_stats(rs)
            tot_in = sum(r.get("total_input_tokens", 0) for r in rs) / s["n"]
            print(f"  {b:18s}  {s['n']:>2d}  {s['succ_k']}/{s['n']:<3} ({s['succ_p']:.2f})  {s['avg_recall']:>6.2f}  {tot_in:>7.0f}  ${s['avg_cost']:>6.4f}  {s['avg_sec']:>7.1f}")

    # --- Delegation table ---
    print("\n\n=== §8.4 delegation aggregate (by strategy, all fixtures) ===")
    print(f"{'Strategy':18s}  {'n':>3s}  {'succ':>8s}  {'95% CI':>14s}  {'recall':>6s}  {'par_in':>7s}  {'sub_in':>7s}  {'cost':>8s}")
    by_s = defaultdict(list)
    for r in deleg:
        by_s[r["strategy"]].append(r)
    for s_name in DELEG_STRATEGIES:
        rs = by_s.get(s_name, [])
        if not rs:
            print(f"{s_name:18s}  ---  no data")
            continue
        stats = cell_stats(rs)
        par_in = sum(r.get("parent_total_input_tokens", 0) for r in rs) / stats["n"]
        sub_in = sum(r.get("sub_total_input_tokens", 0) for r in rs) / stats["n"]
        print(f"{s_name:18s}  {stats['n']:>3d}  {stats['succ_k']}/{stats['n']:<5} ({stats['succ_p']:.2f})  [{stats['succ_lo']:.2f},{stats['succ_hi']:.2f}]  {stats['avg_recall']:>6.2f}  {par_in:>7.0f}  {sub_in:>7.0f}  ${stats['avg_cost']:>6.4f}")

    # --- Per-fixture parent input for delegation ---
    print("\n=== Per-fixture parent input (delegation) ===")
    by_sf = defaultdict(list)
    for r in deleg:
        by_sf[(r["strategy"], r["fixture"])].append(r)
    for fix, raw in FIXTURE_ORDER:
        print(f"\n  Fixture: {fix} ({raw} raw tokens)")
        print(f"  {'Strategy':18s}  {'n':>2s}  {'par_in':>7s}  {'sub_in':>7s}  {'succ':>7s}")
        for s_name in DELEG_STRATEGIES:
            rs = by_sf.get((s_name, fix), [])
            if not rs:
                continue
            stats = cell_stats(rs)
            par_in = sum(r.get("parent_total_input_tokens", 0) for r in rs) / stats["n"]
            sub_in = sum(r.get("sub_total_input_tokens", 0) for r in rs) / stats["n"]
            print(f"  {s_name:18s}  {stats['n']:>2d}  {par_in:>7.0f}  {sub_in:>7.0f}  {stats['succ_k']}/{stats['n']:<3} ({stats['succ_p']:.2f})")


if __name__ == "__main__":
    main()
