# Durable job storage (Neon Postgres) — design

## Problem

`app.py` currently stores every job (`app.json`, `bs.json`, `result.json`,
`error.json`) as files under `jobs/<clientCode>/` on local disk. The app is
deployed to Render's **free tier**, which has no persistent disk option and
spins the service down after inactivity, wiping the ephemeral filesystem on
every restart — not just on redeploys. Job data (including in-flight jobs)
can be lost at any idle period.

## Goal

Make job storage durable across restarts/redeploys without requiring a paid
Render plan, by moving storage to a free-tier hosted Postgres database
(Neon), while keeping every existing HTTP endpoint's behavior and response
shape identical.

## Decisions

- **Provider:** Neon (serverless Postgres, generous always-free tier, no
  credit card required, auto-sleeps/wakes). Connection string supplied via a
  `DATABASE_URL` env var, set on Render and used locally too.
- **Scope:** Fresh cutover — existing jobs currently on the live Render
  service's ephemeral disk are **not** migrated. The database starts empty.
- **Local dev:** Always uses the database (no local-file fallback mode).
  Running `app.py` locally requires `DATABASE_URL` to be set.
- **Driver:** `psycopg[binary]` (sync driver — matches the app's existing
  synchronous, thread-per-job model; no async rewrite).
- **Connection strategy:** One short-lived connection per DB operation, no
  persistent pool. Simplest safe option for this app's low, bursty traffic
  across background analysis threads.
- **Table creation:** App creates the table itself on startup
  (`CREATE TABLE IF NOT EXISTS`) — no separate manual migration step.

## Schema

```sql
CREATE TABLE IF NOT EXISTS jobs (
    client_code TEXT PRIMARY KEY,
    app_json    JSONB,
    bs_json     JSONB,
    result_json JSONB,
    error_json  JSONB,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

One row per job. Mirrors the current four-file-per-job layout as four
nullable JSONB columns. Status is derived from column nullability exactly
the way `_job_status()` derives it today from file existence:

- `result_json IS NOT NULL` → `complete`
- `error_json IS NOT NULL` → `error`
- `app_json IS NOT NULL AND bs_json IS NOT NULL` → `processing`
- `bs_json IS NOT NULL AND app_json IS NULL` → `waiting_for_application`
- `app_json IS NOT NULL AND bs_json IS NULL` → `waiting_for_bank_statement`

## Storage helper functions

Replace all direct `job_dir / "*.json"` file access with:

- `_db_upsert_app(client_code, data)` — insert/update `app_json`; clear
  `result_json`/`error_json` to NULL (same "resubmission starts fresh"
  behavior as today's `do_POST /application`)
- `_db_upsert_bs(client_code, data)` — insert/update `bs_json`
- `_db_get_job(client_code)` — fetch a row as a dict, or `None`
- `_db_set_result(client_code, result)` — write `result_json`
- `_db_set_error(client_code, error)` — write `error_json`
- `_db_delete_job(client_code)` — delete the row
- `_db_list_jobs()` — all rows, sorted by `updated_at` desc (for `GET /jobs`)
- `_db_orphaned_jobs()` — rows where `app_json` and `bs_json` are present but
  `result_json` and `error_json` are both NULL (startup recovery)

Each function owns opening/closing its own connection.

## Endpoint mapping

Every endpoint keeps its exact current logic and response shape — only the
storage calls change:

| Endpoint | Change |
|---|---|
| `POST /application` | `_db_upsert_app`; check `_db_get_job(...)["bs_json"]` to decide whether to trigger analysis (same as today's `(job_dir / "bs.json").exists()` check) |
| `POST /bank-statement` | `_db_upsert_bs`; same pairing check against `app_json` |
| `run_analysis()` | Unchanged Gemini/parsing logic; final write becomes `_db_set_result` / `_db_set_error` instead of writing files |
| `GET /job/<id>` | `_db_get_job` instead of directory read; same status derivation and response shape |
| `GET /jobs` | `_db_list_jobs` instead of directory iteration; same summary fields (`qualifying_lenders` count, `timestamp`, `industry`) pulled from `result_json` |
| `GET /queue` | Same counts, computed from `_db_list_jobs` (or a single aggregate query) |
| `DELETE /job/<id>` | `_db_delete_job` |
| `_recover_orphaned_jobs()` (startup) | `_db_orphaned_jobs()` instead of scanning `JOBS_DIR` |

`JOBS_DIR`/`BASE_DIR`-as-storage and the local `jobs/` folder become unused
and are removed from the code (the physical folder is git-ignored and can be
left on disk harmlessly).

## Error handling

If a DB operation fails (transient network issue, Neon briefly
unreachable while waking from idle, etc.), the request returns `503` with
the error message rather than crashing the server or corrupting state —
consistent with the existing `try/except` pattern already used around the
Power Automate webhook call.

## Deployment

1. Create a Neon project/database (user action — outside this codebase).
2. Add `DATABASE_URL` to Render's environment variables for the service.
3. Add `DATABASE_URL` to local `.env`/secrets for local development.
4. Deploy. Table is created automatically on first startup.

## Out of scope

- Migrating jobs currently on the live Render service's ephemeral disk
  (explicitly declined — fresh start).
- Job retention/expiration policy (jobs still accumulate forever; this was
  already true with file storage and is unrelated to durability).
- Connection pooling (not needed at this traffic volume; can be revisited if
  it becomes a bottleneck).
