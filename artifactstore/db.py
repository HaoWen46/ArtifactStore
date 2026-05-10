import sqlite3
import secrets
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# Columns added to tables AFTER the initial schema. SQLite's
# `CREATE TABLE IF NOT EXISTS` is a no-op when the table already exists, so
# pre-existing DBs (created from older commits) won't pick up new columns
# from schema.sql alone. We backfill these via ALTER TABLE on every migrate()
# call. ALTER TABLE ... ADD COLUMN is idempotent under our PRAGMA-table_info
# guard. Cheap to run on every connect.
#
# Format: (table, column_name, column_definition).
# Order matters only insofar as columns can reference earlier ones (none here).
_BACKFILL_COLUMNS: list[tuple[str, str, str]] = [
    # artifacts table — columns landed during PLAN §13 step 1+2 refinements
    ("artifacts", "raw_blob", "TEXT"),
    ("artifacts", "metadata_json", "TEXT"),
    # `sensitivity_label` had a default added — adding a NOT NULL column to
    # an existing table requires a default. The default makes it idempotent.
    ("artifacts", "sensitivity_label", "TEXT DEFAULT 'internal'"),
    # artifact_grants table — cumulative budget refinement
    ("artifact_grants", "consumed_tokens", "INTEGER DEFAULT 0"),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Run the full schema migration. Idempotent — safe to call on every
    connect to ensure pre-existing DBs pick up new columns."""
    conn.executescript(SCHEMA_PATH.read_text())
    _backfill_columns(conn)


def _backfill_columns(conn: sqlite3.Connection) -> None:
    """Add columns to existing tables that schema.sql cannot add via
    `CREATE TABLE IF NOT EXISTS` (which is a no-op when the table exists).

    SQLite supports `ALTER TABLE ... ADD COLUMN` but NOT `IF NOT EXISTS`
    on the column name (as of 3.46), so we check `PRAGMA table_info`
    first. This is critical for users who created their .db on an older
    commit — running new code against an old DB would otherwise crash on
    the first INSERT/UPDATE referencing a missing column.
    """
    for table, col, decl in _BACKFILL_COLUMNS:
        # PRAGMA table_info returns rows of (cid, name, type, notnull, dflt_value, pk)
        existing = {r[1] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            # Table doesn't exist yet — schema.sql already created it via
            # CREATE TABLE IF NOT EXISTS, so this branch shouldn't fire.
            # If it does, something's wrong with the migration order.
            continue
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"
