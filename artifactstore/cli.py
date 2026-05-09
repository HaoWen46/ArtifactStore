"""CLI surface tracks ArtifactStore_PLAN.md §13."""
from __future__ import annotations

import json
import re
from pathlib import Path

import typer

from artifactstore.grants import AccessDenied
from artifactstore.store import ArtifactStore

# pretty_exceptions_*=False — we want clean one-line errors for AccessDenied,
# not a full rich-format traceback (the demo's RQ4 'denied' surface).
app = typer.Typer(no_args_is_help=True, add_completion=False,
                  pretty_exceptions_show_locals=False,
                  pretty_exceptions_enable=False)


@app.callback()
def _catch(ctx: typer.Context) -> None:
    """No-op root callback (lets AccessDenied propagate to the wrapper below)."""
    pass


def main() -> None:
    """Console-script entrypoint that turns AccessDenied into a clean exit."""
    import sys
    try:
        app()
    except AccessDenied as e:
        typer.secho(f"denied: {e}", err=True, fg=typer.colors.RED)
        sys.exit(3)

DB_OPT = typer.Option("artifactstore.db", "--db", help="SQLite database path")


_TTL_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[smhd])$")


def _parse_ttl(s: str) -> int:
    """'30m' -> 1800. Accept s/m/h/d. Bare integers are seconds."""
    if s.isdigit():
        return int(s)
    m = _TTL_RE.match(s.strip())
    if not m:
        raise typer.BadParameter(f"bad TTL: {s!r}, want 30s/15m/2h/1d")
    n = int(m.group("n"))
    unit = m.group("unit")
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


@app.command()
def init(db: str = DB_OPT) -> None:
    """Create schema."""
    ArtifactStore.init(db)
    typer.echo(f"initialized {db}")


@app.command()
def put(
    file: str,
    tool: str = typer.Option(...),
    artifact_type: str = typer.Option(..., "--type"),
    agent: str = typer.Option("cli"),
    session: str = typer.Option("default"),
    db: str = DB_OPT,
) -> None:
    """Ingest a file as an artifact and print its id."""
    raw = Path(file).read_text()
    store = ArtifactStore.init(db)
    aid = store.put_artifact(
        tool_name=tool, artifact_type=artifact_type, raw_text=raw,
        creator_agent_id=agent, session_id=session,
        metadata={"source_file": file},
    )
    typer.echo(aid)


@app.command()
def search(query: str, grant: str = typer.Option(...), db: str = DB_OPT,
           limit: int = 5, token_budget: int = 1000) -> None:
    store = ArtifactStore.init(db)
    rows = store.search(query, grant_id=grant, limit=limit,
                        token_budget=token_budget)
    typer.echo(json.dumps(rows, indent=2))


@app.command()
def spans(artifact_id: str, grant: str = typer.Option(...), db: str = DB_OPT,
          span_type: list[str] = typer.Option(None, "--type"),
          token_budget: int = 1000) -> None:
    store = ArtifactStore.init(db)
    rows = store.get_spans(artifact_id, grant_id=grant,
                           span_types=span_type or None,
                           token_budget=token_budget)
    typer.echo(json.dumps(rows, indent=2))


@app.command()
def expand(artifact_id: str, view: str = typer.Option(...),
           grant: str = typer.Option(...), db: str = DB_OPT,
           token_budget: int = 2000) -> None:
    store = ArtifactStore.init(db)
    typer.echo(store.expand_view(artifact_id, grant_id=grant, view=view,
                                 token_budget=token_budget))


@app.command()
def grant(agent: str = typer.Option(...),
          types: str = typer.Option("", help="comma-separated artifact_types"),
          views: str = typer.Option("preview,evidence"),
          ops: str = typer.Option("search,get_spans,expand_view"),
          ttl: str = typer.Option("30m"),
          max_tokens: int = 4000,
          session: str = typer.Option("default"),
          issuer: str = typer.Option("cli"),
          db: str = DB_OPT) -> None:
    store = ArtifactStore.init(db)
    artifact_types = [t for t in types.split(",") if t]
    predicate = {"session_id": session}
    if artifact_types:
        predicate["artifact_types"] = artifact_types
    gid = store.create_grant(
        subject_agent_id=agent, issuer_agent_id=issuer,
        artifact_predicate=predicate,
        allowed_ops=[o for o in ops.split(",") if o],
        allowed_views=[v for v in views.split(",") if v],
        max_tokens=max_tokens, ttl_seconds=_parse_ttl(ttl),
    )
    typer.echo(gid)


@app.command()
def audit(grant: str = typer.Option(...), db: str = DB_OPT) -> None:
    store = ArtifactStore.init(db)
    typer.echo(json.dumps(store.audit(grant), indent=2))


if __name__ == "__main__":
    main()
