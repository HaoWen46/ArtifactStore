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
    """Show every read attempt under a grant — allowed and denied."""
    store = ArtifactStore.init(db)
    typer.echo(json.dumps(store.audit(grant), indent=2))


@app.command(name="find-related")
def find_related(
    artifact_id: str,
    grant: str = typer.Option(...),
    relation: list[str] = typer.Option(None, "--relation",
                                        help="filter to specific relation(s); repeatable"),
    db: str = DB_OPT,
) -> None:
    """List provenance/causal links from an artifact (caused_by, derived_from, ...)."""
    store = ArtifactStore.init(db)
    rows = store.find_related(artifact_id, grant_id=grant,
                               relations=relation or None)
    typer.echo(json.dumps(rows, indent=2))


@app.command()
def verify(citation: str, db: str = DB_OPT) -> None:
    """Resolve a citation 'art_<8hex>/span_<8hex>'. Exit 0 = resolves,
    exit 4 = does not resolve, exit 5 = malformed."""
    import sys
    from artifactstore.cite import BadCitation, parse, verify_resolves
    store = ArtifactStore.init(db)
    try:
        art_id, span_id = parse(citation)
    except BadCitation as e:
        typer.secho(f"malformed: {e}", err=True, fg=typer.colors.RED)
        sys.exit(5)
    ok = verify_resolves(store.conn, citation)
    if ok:
        typer.echo(f"resolved: artifact_id={art_id}  span_id={span_id}")
        sys.exit(0)
    typer.secho(f"unresolved: {citation}  (no matching span in store)",
                err=True, fg=typer.colors.YELLOW)
    sys.exit(4)


@app.command()
def show(artifact_id: str, db: str = DB_OPT) -> None:
    """Print artifact metadata + spans + outbound links. Debugging helper —
    bypasses grants (uses the seeded __supervisor__ grant)."""
    store = ArtifactStore.init(db)
    art = store.conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if art is None:
        typer.secho(f"no such artifact: {artifact_id}", err=True,
                    fg=typer.colors.RED)
        raise typer.Exit(code=2)
    spans = store.conn.execute(
        "SELECT span_id, span_type, file_path, line_start, line_end, "
        "       importance, length(text) AS text_chars "
        "FROM artifact_spans WHERE artifact_id = ? "
        "ORDER BY COALESCE(importance, 0) DESC, span_id",
        (artifact_id,),
    ).fetchall()
    links = store.conn.execute(
        "SELECT dst_artifact_id, relation, confidence "
        "FROM artifact_links WHERE src_artifact_id = ?",
        (artifact_id,),
    ).fetchall()
    payload = {
        "artifact_id": art["artifact_id"],
        "session_id": art["session_id"],
        "creator_agent_id": art["creator_agent_id"],
        "tool_name": art["tool_name"],
        "artifact_type": art["artifact_type"],
        "raw_hash": art["raw_hash"],
        "token_count": art["token_count"],
        "sensitivity_label": art["sensitivity_label"],
        "created_at": art["created_at"],
        "preview": art["preview"],
        "metadata": json.loads(art["metadata_json"] or "{}"),
        "span_count": len(spans),
        "spans": [dict(r) for r in spans],
        "outbound_links": [dict(r) for r in links],
    }
    typer.echo(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
