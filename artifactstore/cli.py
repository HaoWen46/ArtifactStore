"""CLI surface tracks ArtifactStore_PLAN.md §13."""
from __future__ import annotations
import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)

DB_OPT = typer.Option("artifactstore.db", "--db", help="SQLite database path")


@app.command()
def init(db: str = DB_OPT) -> None:
    """Create schema."""
    from artifactstore.store import ArtifactStore
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
    raise typer.Exit(code=2)  # NotImplemented


@app.command()
def search(query: str, grant: str = typer.Option(...), db: str = DB_OPT,
           limit: int = 5, token_budget: int = 1000) -> None:
    raise typer.Exit(code=2)


@app.command()
def spans(artifact_id: str, grant: str = typer.Option(...), db: str = DB_OPT,
          span_type: list[str] = typer.Option(None, "--type"),
          token_budget: int = 1000) -> None:
    raise typer.Exit(code=2)


@app.command()
def expand(artifact_id: str, view: str = typer.Option(...),
           grant: str = typer.Option(...), db: str = DB_OPT,
           token_budget: int = 2000) -> None:
    raise typer.Exit(code=2)


@app.command()
def grant(agent: str = typer.Option(...),
          types: str = typer.Option("", help="comma-separated artifact_types"),
          views: str = typer.Option("preview,evidence"),
          ops: str = typer.Option("search,get_preview,get_spans"),
          ttl: str = typer.Option("30m"),
          max_tokens: int = 4000,
          db: str = DB_OPT) -> None:
    raise typer.Exit(code=2)


@app.command()
def audit(grant: str = typer.Option(...), db: str = DB_OPT) -> None:
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
