"""`uv run python -m eval` entrypoint.

By default runs the §11.1 single-agent sweep. Pass `--mode delegation` for
the §11.2 supervisor↔subagent sweep (D1/D2/D3 strategies). The `--mode`
flag is consumed before delegation; everything after is passed through to
the appropriate driver's argparser.
"""
import sys

from eval.driver import main as single_main


def main() -> None:
    args = sys.argv[1:]
    mode = "single"
    # Look for `--mode <x>` and strip it from argv before subdriver sees it.
    if "--mode" in args:
        i = args.index("--mode")
        if i + 1 < len(args):
            mode = args[i + 1]
            del args[i:i + 2]
            sys.argv = [sys.argv[0], *args]
    if mode == "delegation":
        from eval.delegation import main as delegation_main
        delegation_main()
        return
    if mode == "single":
        single_main()
        return
    raise SystemExit(f"unknown --mode: {mode!r} (use 'single' or 'delegation')")


if __name__ == "__main__":
    main()
