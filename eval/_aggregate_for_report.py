"""One-off aggregator that prints the per-fixture × per-baseline numbers
the report tables and plots need. Reads from `eval/runs/`; takes a list of
single-agent and delegation run directories as args.

Usage:
  uv run python eval/_aggregate_for_report.py \\
      --single <run_dir1> <run_dir2> ... \\
      --delegation <run_dir1> <run_dir2> ...

Prints CSV-formatted aggregates to stdout. Reviewers can rerun against any
sweep to verify the report's headline numbers.
"""
from __future__ import annotations
import argparse
import json
import statistics as stats
from collections import defaultdict
from pathlib import Path


def _avg(rows: list[dict], key: str) -> float:
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    return stats.mean(vals) if vals else 0.0


def aggregate_single(run_dirs: list[Path]) -> None:
    """Aggregate single-agent runs across one or more directories. Each
    directory is a self-contained sweep; we merge by (fixture, baseline)."""
    rows_by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run_dir in run_dirs:
        rfile = run_dir / "result.jsonl"
        if not rfile.is_file():
            continue
        for line in rfile.read_text().splitlines():
            r = json.loads(line)
            rows_by_cell[(r["fixture"], r["baseline"])].append(r)

    print("# Single-agent aggregate")
    print("fixture,baseline,n,success_rate,avg_recall,avg_total_input,avg_output,avg_cost_usd,avg_setup_in,avg_setup_out")
    for (fx, bl) in sorted(rows_by_cell.keys()):
        rs = rows_by_cell[(fx, bl)]
        n = len(rs)
        succ = sum(1 for r in rs if r.get("task_success")) / max(1, n)
        recall = _avg(rs, "evidence_recall")
        ti = _avg(rs, "total_input_tokens")
        if not ti:
            ti = _avg(rs, "input_tokens")
        out = _avg(rs, "output_tokens")
        cost = _avg(rs, "estimated_cost_usd")
        sin = _avg(rs, "setup_input_tokens")
        sout = _avg(rs, "setup_output_tokens")
        print(f"{fx},{bl},{n},{succ:.2f},{recall:.2f},"
              f"{ti:.0f},{out:.0f},{cost:.5f},{sin:.0f},{sout:.0f}")


def aggregate_delegation(run_dirs: list[Path]) -> None:
    rows_by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run_dir in run_dirs:
        rfile = run_dir / "result.jsonl"
        if not rfile.is_file():
            continue
        for line in rfile.read_text().splitlines():
            r = json.loads(line)
            rows_by_cell[(r["fixture"], r["strategy"])].append(r)

    print("# Delegation aggregate")
    print("fixture,strategy,n,success_rate,avg_recall,avg_par_in,avg_sub_in,avg_total_in,avg_total_out,avg_cost_usd,avg_setup_in,avg_setup_out")
    for (fx, st) in sorted(rows_by_cell.keys()):
        rs = rows_by_cell[(fx, st)]
        n = len(rs)
        succ = sum(1 for r in rs if r.get("task_success")) / max(1, n)
        recall = _avg(rs, "evidence_recall")
        pti = _avg(rs, "parent_total_input_tokens") or _avg(rs, "parent_input_tokens")
        sti = _avg(rs, "sub_total_input_tokens") or _avg(rs, "sub_input_tokens")
        tti = _avg(rs, "total_input_tokens")
        tto = _avg(rs, "total_output_tokens")
        cost = _avg(rs, "estimated_cost_usd")
        sin = _avg(rs, "setup_input_tokens")
        sout = _avg(rs, "setup_output_tokens")
        print(f"{fx},{st},{n},{succ:.2f},{recall:.2f},"
              f"{pti:.0f},{sti:.0f},{tti:.0f},{tto:.0f},"
              f"{cost:.5f},{sin:.0f},{sout:.0f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--single", nargs="*", type=Path, default=[])
    p.add_argument("--delegation", nargs="*", type=Path, default=[])
    args = p.parse_args()
    if args.single:
        aggregate_single(args.single)
    if args.delegation:
        if args.single:
            print()
        aggregate_delegation(args.delegation)


if __name__ == "__main__":
    main()
