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
