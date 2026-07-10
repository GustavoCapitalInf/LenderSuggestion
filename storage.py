"""
Job storage backed by Postgres (Neon). Replaces the old jobs/<clientCode>/*.json
file layout so job data survives Render free-tier restarts (no persistent
disk, spins down on idle).

Requires a DATABASE_URL environment variable (a standard Postgres
connection string), falling back to .streamlit/secrets.toml the same way
app.py loads GEMINI_API_KEY.
"""

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

BASE_DIR = Path(__file__).parent

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    _secrets = BASE_DIR / ".streamlit" / "secrets.toml"
    if _secrets.exists():
        for _line in _secrets.read_text().splitlines():
            if _line.strip().startswith("DATABASE_URL"):
                DATABASE_URL = _line.split("=", 1)[1].strip().strip('"').strip("'")
                break


def _connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db() -> None:
    """Create the jobs table if it doesn't already exist. Call once at startup."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                client_code TEXT PRIMARY KEY,
                app_json    JSONB,
                bs_json     JSONB,
                result_json JSONB,
                error_json  JSONB,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def upsert_app(client_code: str, app_json: dict) -> None:
    """Insert or update a job's app_json. Clears result_json/error_json so a
    resubmitted application starts fresh (matches prior file-based behavior)."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO jobs (client_code, app_json, result_json, error_json, updated_at)
            VALUES (%s, %s, NULL, NULL, now())
            ON CONFLICT (client_code) DO UPDATE
                SET app_json = EXCLUDED.app_json,
                    result_json = NULL,
                    error_json = NULL,
                    updated_at = now()
        """, (client_code, Json(app_json)))


def upsert_bs(client_code: str, bs_json: dict) -> None:
    """Insert or update a job's bs_json."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO jobs (client_code, bs_json, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (client_code) DO UPDATE
                SET bs_json = EXCLUDED.bs_json,
                    updated_at = now()
        """, (client_code, Json(bs_json)))


def get_job(client_code: str) -> dict | None:
    """Return the job row as a dict, or None if it doesn't exist."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT client_code, app_json, bs_json, result_json, error_json, updated_at "
            "FROM jobs WHERE client_code = %s",
            (client_code,),
        ).fetchone()
        return dict(row) if row else None


def set_result(client_code: str, result_json: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET result_json = %s, updated_at = now() WHERE client_code = %s",
            (Json(result_json), client_code),
        )


def set_error(client_code: str, error_json: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET error_json = %s, updated_at = now() WHERE client_code = %s",
            (Json(error_json), client_code),
        )


def delete_job(client_code: str) -> bool:
    """Delete the job row. Returns True if a row was deleted, False if it didn't exist."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE client_code = %s", (client_code,))
        return cur.rowcount > 0


def list_jobs() -> list[dict]:
    """All jobs, newest updated_at first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT client_code, app_json, bs_json, result_json, error_json, updated_at "
            "FROM jobs ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def orphaned_jobs() -> list[dict]:
    """Jobs with both app_json and bs_json present but no result/error yet
    (used to relaunch analysis on startup after an unclean shutdown)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT client_code, app_json, bs_json, result_json, error_json, updated_at "
            "FROM jobs "
            "WHERE app_json IS NOT NULL AND bs_json IS NOT NULL "
            "AND result_json IS NULL AND error_json IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def job_status(row: dict) -> str:
    """Derive a status string from a job row dict, mirroring the old
    file-existence-based _job_status()."""
    has_app = row.get("app_json") is not None
    has_bs = row.get("bs_json") is not None
    if row.get("result_json") is not None:
        return "complete"
    if row.get("error_json") is not None:
        return "error"
    if has_app and has_bs:
        return "processing"
    if has_bs and not has_app:
        return "waiting_for_application"
    if has_app and not has_bs:
        return "waiting_for_bank_statement"
    return "unknown"
