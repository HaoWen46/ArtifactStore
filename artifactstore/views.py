"""Materializers for the five artifact views (PLAN §8).
Each takes (conn, artifact_id, token_budget) and returns a string."""
from __future__ import annotations
import sqlite3

VIEW_NAMES = ("preview", "evidence", "redacted", "raw", "provenance")


def preview(conn: sqlite3.Connection, artifact_id: str, token_budget: int) -> str:
    raise NotImplementedError


def evidence(conn: sqlite3.Connection, artifact_id: str, token_budget: int) -> str:
    raise NotImplementedError


def redacted(conn: sqlite3.Connection, artifact_id: str, token_budget: int) -> str:
    raise NotImplementedError


def raw(conn: sqlite3.Connection, artifact_id: str, token_budget: int) -> str:
    raise NotImplementedError


def provenance(conn: sqlite3.Connection, artifact_id: str, token_budget: int) -> str:
    raise NotImplementedError


VIEWS = {
    "preview": preview,
    "evidence": evidence,
    "redacted": redacted,
    "raw": raw,
    "provenance": provenance,
}
