"""Cleanup helper for `eval/runs/`.

Each sweep writes a directory with config.json, result.jsonl, audit.csv,
manifest.json, plus one db_<run_id>.sqlite per (fixture × baseline × rep).
Over many sweeps these accumulate. This helper keeps the most recent N
runs and deletes the rest.

  uv run python -m eval.cleanup            # keep last 3, dry-run
  uv run python -m eval.cleanup --keep 5   # keep last 5, dry-run
  uv run python -m eval.cleanup --apply    # actually delete

Run dirs are sorted lexicographically by name. We use UTC ISO timestamps
in the directory names (driver-enforced), so lexical sort == chronological
sort.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent / "runs"


def list_run_dirs(runs_dir: Path = RUNS_DIR) -> list[Path]:
    """Return all run directories under `runs_dir`, sorted oldest-first.
    Excludes hidden files (e.g. .gitkeep)."""
    if not runs_dir.is_dir():
        return []
    return sorted(
        d for d in runs_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def plan_cleanup(runs_dir: Path, keep: int) -> tuple[list[Path], list[Path]]:
    """Returns (to_keep, to_delete). Keep is the most recent `keep` dirs."""
    dirs = list_run_dirs(runs_dir)
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")
    if len(dirs) <= keep:
        return dirs, []
    return dirs[-keep:], dirs[:-keep]


def main() -> None:
    p = argparse.ArgumentParser(description="Prune old eval/runs/ directories")
    p.add_argument("--keep", type=int, default=3,
                   help="Keep the most recent N run dirs (default 3)")
    p.add_argument("--apply", action="store_true",
                   help="Actually delete (default: dry-run)")
    p.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    args = p.parse_args()

    keep, delete = plan_cleanup(args.runs_dir, args.keep)
    if not delete:
        print(f"[cleanup] {len(keep)} run dir(s) present; "
              f"≤ keep={args.keep}; nothing to do.")
        return

    total_bytes = 0
    print(f"[cleanup] keeping {len(keep)}: {[d.name for d in keep]}")
    print(f"[cleanup] {'would delete' if not args.apply else 'deleting'} "
          f"{len(delete)}:")
    for d in delete:
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        total_bytes += size
        print(f"  - {d.name} ({size / 1024 / 1024:.1f} MB)")
        if args.apply:
            shutil.rmtree(d)
    print(f"[cleanup] {'freed' if args.apply else 'would free'} "
          f"{total_bytes / 1024 / 1024:.1f} MB total. "
          f"{'(re-run with --apply to actually delete)' if not args.apply else ''}")


if __name__ == "__main__":
    main()
