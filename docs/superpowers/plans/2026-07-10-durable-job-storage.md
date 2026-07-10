# Durable Job Storage (Neon Postgres) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `app.py`'s local-file job storage (`jobs/<clientCode>/*.json`) with a Postgres-backed `storage.py` module, so job data survives Render free-tier restarts (which have no persistent disk and spin down on idle), with zero change to any HTTP endpoint's response shape.

**Architecture:** A new `storage.py` module owns all persistence: one `jobs` table, one row per job, four nullable `JSONB` columns (`app_json`, `bs_json`, `result_json`, `error_json`) mirroring the four files each job used to have. `app.py`'s handlers call `storage.*` functions instead of touching `Path`/file APIs. Each storage function opens and closes its own short-lived connection — no persistent pool.

**Tech Stack:** `psycopg[binary]` (sync Postgres driver) against a Neon (serverless Postgres) database.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-10-durable-job-storage-design.md`
- Provider: Neon Postgres. Connection string via `DATABASE_URL` env var, with a `.streamlit/secrets.toml` fallback (same convention `GEMINI_API_KEY` already uses at `app.py:63-71`).
- Local dev always uses the database — no local-file fallback mode.
- One short-lived Postgres connection per storage operation (no connection pool).
- Every HTTP endpoint's response shape must stay byte-for-byte identical to today — this is a storage swap only, not a behavior change. Downstream consumers (Power Automate webhook, Orbit) must not need to change.
- No migration of jobs currently on the live Render service — fresh start.
- No retention/expiration policy — out of scope (jobs already accumulated forever under file storage; unrelated to durability).
- On any storage operation failure, the affected HTTP request returns `503` with the error message rather than crashing the server (mirrors the existing `try/except` pattern around the Power Automate webhook call at `app.py:695-699`).
- `DATABASE_URL` (Neon connection string) is already present in `.streamlit/secrets.toml` locally — do not print or log its value anywhere.

---

### Task 1: `storage.py` foundation — connect, init_db, upsert_app, upsert_bs, get_job

**Files:**
- Modify: `requirements.txt`
- Create: `storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Produces: `storage.init_db() -> None`, `storage.upsert_app(client_code: str, app_json: dict) -> None`, `storage.upsert_bs(client_code: str, bs_json: dict) -> None`, `storage.get_job(client_code: str) -> dict | None` (dict keys: `client_code`, `app_json`, `bs_json`, `result_json`, `error_json`, `updated_at`)

- [ ] **Step 1: Add the `psycopg[binary]` dependency**

Edit `requirements.txt` to:

```
google-genai>=2.0.0
psycopg[binary]>=3.1
```

- [ ] **Step 2: Install it locally**

Run: `pip install -r requirements.txt`
Expected: installs successfully, no errors.

- [ ] **Step 3: Write the failing test**

Create `tests/test_storage.py`:

```python
"""
Integration tests for storage.py — run against a real Postgres database
(no mocking: the thing being verified is SQL/psycopg correctness).

Run: python tests/test_storage.py
Requires DATABASE_URL to be set (env var or .streamlit/secrets.toml).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import storage

TEST_CODE = "STORAGE_TEST_job1"


def cleanup():
    # storage.delete_job() doesn't exist until Task 2 — use a raw query here
    # so this test file is self-contained within Task 1.
    with storage._connect() as conn:
        conn.execute("DELETE FROM jobs WHERE client_code = %s", (TEST_CODE,))


def test_init_db_is_idempotent():
    storage.init_db()
    storage.init_db()  # must not raise on second call
    print("test_init_db_is_idempotent: PASS")


def test_upsert_app_creates_row():
    cleanup()
    storage.upsert_app(TEST_CODE, {"foo": "bar"})
    job = storage.get_job(TEST_CODE)
    assert job is not None, "expected a row to exist after upsert_app"
    assert job["app_json"] == {"foo": "bar"}
    assert job["bs_json"] is None
    assert job["result_json"] is None
    assert job["error_json"] is None
    print("test_upsert_app_creates_row: PASS")


def test_upsert_bs_does_not_clear_app_json():
    cleanup()
    storage.upsert_app(TEST_CODE, {"foo": "bar"})
    storage.upsert_bs(TEST_CODE, {"baz": "qux"})
    job = storage.get_job(TEST_CODE)
    assert job["app_json"] == {"foo": "bar"}, "upsert_bs must not touch app_json"
    assert job["bs_json"] == {"baz": "qux"}
    print("test_upsert_bs_does_not_clear_app_json: PASS")


def test_get_job_returns_none_for_missing_code():
    cleanup()
    assert storage.get_job("NO_SUCH_CODE_xyz") is None
    print("test_get_job_returns_none_for_missing_code: PASS")


if __name__ == "__main__":
    try:
        test_init_db_is_idempotent()
        test_upsert_app_creates_row()
        test_upsert_bs_does_not_clear_app_json()
        test_get_job_returns_none_for_missing_code()
    finally:
        cleanup()
    print("\nALL TESTS PASSED")
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python tests/test_storage.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'storage'` (the module doesn't exist yet).

- [ ] **Step 5: Write `storage.py`**

Create `storage.py`:

```python
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
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python tests/test_storage.py`
Expected:
```
test_init_db_is_idempotent: PASS
test_upsert_app_creates_row: PASS
test_upsert_bs_does_not_clear_app_json: PASS
test_get_job_returns_none_for_missing_code: PASS

ALL TESTS PASSED
```

- [ ] **Step 7: Commit**

```bash
git add requirements.txt storage.py tests/test_storage.py
git commit -m "add Postgres-backed storage.py foundation (init_db, upsert_app, upsert_bs, get_job)"
```

---

### Task 2: `storage.py` — set_result, set_error, delete_job, list_jobs, orphaned_jobs, job_status

**Files:**
- Modify: `storage.py`
- Modify: `tests/test_storage.py`

**Interfaces:**
- Consumes: everything from Task 1
- Produces: `storage.set_result(client_code: str, result_json: dict) -> None`, `storage.set_error(client_code: str, error_json: dict) -> None`, `storage.delete_job(client_code: str) -> bool`, `storage.list_jobs() -> list[dict]`, `storage.orphaned_jobs() -> list[dict]`, `storage.job_status(row: dict) -> str` (returns one of `"complete"`, `"error"`, `"processing"`, `"waiting_for_application"`, `"waiting_for_bank_statement"`, `"unknown"`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py` (insert before the `if __name__ == "__main__":` block):

```python
def test_set_result_and_job_status_complete():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    storage.upsert_bs(TEST_CODE, {"b": 2})
    storage.set_result(TEST_CODE, {"qualifying_lenders": []})
    job = storage.get_job(TEST_CODE)
    assert job["result_json"] == {"qualifying_lenders": []}
    assert storage.job_status(job) == "complete"
    print("test_set_result_and_job_status_complete: PASS")


def test_set_error_and_job_status_error():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    storage.upsert_bs(TEST_CODE, {"b": 2})
    storage.set_error(TEST_CODE, {"error": "boom"})
    job = storage.get_job(TEST_CODE)
    assert job["error_json"] == {"error": "boom"}
    assert storage.job_status(job) == "error"
    print("test_set_error_and_job_status_error: PASS")


def test_job_status_transitions():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    assert storage.job_status(storage.get_job(TEST_CODE)) == "waiting_for_bank_statement"
    storage.upsert_bs(TEST_CODE, {"b": 2})
    assert storage.job_status(storage.get_job(TEST_CODE)) == "processing"
    print("test_job_status_transitions: PASS")


def test_reupsert_app_clears_result():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    storage.upsert_bs(TEST_CODE, {"b": 2})
    storage.set_result(TEST_CODE, {"qualifying_lenders": []})
    storage.upsert_app(TEST_CODE, {"a": 2})  # resubmission
    job = storage.get_job(TEST_CODE)
    assert job["result_json"] is None, "resubmitting the application must clear result_json"
    print("test_reupsert_app_clears_result: PASS")


def test_delete_job_rowcount_semantics():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    assert storage.delete_job(TEST_CODE) is True
    assert storage.delete_job(TEST_CODE) is False, "deleting an already-gone job returns False"
    print("test_delete_job_rowcount_semantics: PASS")


def test_list_jobs_and_orphaned_jobs():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    storage.upsert_bs(TEST_CODE, {"b": 2})

    all_codes = [j["client_code"] for j in storage.list_jobs()]
    assert TEST_CODE in all_codes

    orphaned_codes = [j["client_code"] for j in storage.orphaned_jobs()]
    assert TEST_CODE in orphaned_codes, "job with app+bs but no result/error is orphaned"

    storage.set_result(TEST_CODE, {"qualifying_lenders": []})
    orphaned_codes = [j["client_code"] for j in storage.orphaned_jobs()]
    assert TEST_CODE not in orphaned_codes, "job with a result is no longer orphaned"
    print("test_list_jobs_and_orphaned_jobs: PASS")
```

Add the new test calls to the `if __name__ == "__main__":` block, so it reads:

```python
if __name__ == "__main__":
    try:
        test_init_db_is_idempotent()
        test_upsert_app_creates_row()
        test_upsert_bs_does_not_clear_app_json()
        test_get_job_returns_none_for_missing_code()
        test_set_result_and_job_status_complete()
        test_set_error_and_job_status_error()
        test_job_status_transitions()
        test_reupsert_app_clears_result()
        test_delete_job_rowcount_semantics()
        test_list_jobs_and_orphaned_jobs()
    finally:
        cleanup()
    print("\nALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_storage.py`
Expected: FAIL with `AttributeError: module 'storage' has no attribute 'set_result'`.

- [ ] **Step 3: Add the remaining functions to `storage.py`**

Append to `storage.py` (after `get_job`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_storage.py`
Expected: all 10 test lines print `PASS`, ending with `ALL TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add storage.py tests/test_storage.py
git commit -m "add set_result/set_error/delete_job/list_jobs/orphaned_jobs/job_status to storage.py"
```

---

### Task 3: Wire `POST /application` and `POST /bank-statement` to storage

**Files:**
- Modify: `app.py:817-887` (the `do_POST` method)
- Modify: `tests/test_storage.py` is not touched here — this task is tested via a live HTTP smoke check (Step 4), since `do_POST` is a method on `_Handler`, not a standalone unit.

**Interfaces:**
- Consumes: `storage.upsert_app`, `storage.upsert_bs`, `storage.get_job` (Task 1)

- [ ] **Step 1: Add the `storage` import to `app.py`**

In `app.py`, after the line `from google import genai` (around line 42), add:

```python
import storage
```

- [ ] **Step 2: Replace the body of `do_POST`**

In `app.py`, replace the entire `do_POST` method (currently lines 817-887) with:

```python
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return

        path = self.path.rstrip("/")

        try:
            if path == "/application":
                if not GEMINI_API_KEY:
                    self._send(500, {"error": "GEMINI_API_KEY not configured"})
                    return
                client_id = (data.pop("clientCode", None) or data.pop("client_id", None)) if isinstance(data, dict) else None
                if not client_id:
                    self._send(400, {"error": "client_id is required"})
                    return

                storage.upsert_app(client_id, data)
                print(f"[{client_id}] application received")

                job = storage.get_job(client_id)
                if job["bs_json"] is not None:
                    print(f"[{client_id}] bank statement already present — launching analysis")
                    threading.Thread(
                        target=run_analysis,
                        args=(client_id, data, job["bs_json"]),
                        daemon=True,
                    ).start()
                    self._send(200, {"clientCode": client_id, "status": "processing",
                                     "poll": f"GET /job/{client_id}"})
                else:
                    self._send(200, {"clientCode": client_id, "status": "received"})

            elif path == "/bank-statement":
                if not GEMINI_API_KEY:
                    self._send(500, {"error": "GEMINI_API_KEY not configured"})
                    return
                client_id = (data.pop("clientCode", None) or data.pop("client_id", None)) if isinstance(data, dict) else None
                if not client_id:
                    self._send(400, {"error": "client_id is required"})
                    return

                storage.upsert_bs(client_id, data)
                print(f"[{client_id}] bank statement received")

                job = storage.get_job(client_id)
                if job["app_json"] is not None:
                    print(f"[{client_id}] application already present — launching analysis")
                    threading.Thread(
                        target=run_analysis,
                        args=(client_id, job["app_json"], data),
                        daemon=True,
                    ).start()
                    self._send(200, {"clientCode": client_id, "status": "processing",
                                     "poll": f"GET /job/{client_id}"})
                else:
                    self._send(200, {"clientCode": client_id, "status": "waiting_for_application"})

            else:
                self._send(404, {"error": "unknown endpoint"})
        except Exception as exc:
            self._send(503, {"error": f"storage unavailable: {exc}"})
```

- [ ] **Step 3: Start the server locally**

Run (in the background): `python app.py --port 8503`
Expected output includes: `Capital Infusion MCA Backend running on port 8503` (it will also print orphaned-job recovery output, ignore for now — that's Task 6).

- [ ] **Step 4: Smoke-test the two endpoints against the running server**

Run:

```bash
curl -s -X POST http://localhost:8503/application -H "Content-Type: application/json" -d '{"clientCode":"PLAN_TEST_1","Business_Legal_Name":"Test Co"}'
curl -s http://localhost:8503/job/PLAN_TEST_1
```

Expected: first call returns `{"clientCode": "PLAN_TEST_1", "status": "received"}`; second call returns `{"clientCode": "PLAN_TEST_1", "status": "waiting_for_bank_statement"}`.

Then run:

```bash
python -c "
import sys; sys.path.insert(0, '.')
import storage
print(storage.get_job('PLAN_TEST_1'))
storage.delete_job('PLAN_TEST_1')
"
```

Expected: prints a dict with `app_json` containing `{"Business_Legal_Name": "Test Co"}` and `bs_json` as `None`; the `delete_job` call cleans up the test row.

- [ ] **Step 5: Stop the local server**

Stop the `python app.py` process started in Step 3.

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "wire POST /application and POST /bank-statement to storage.py"
```

---

### Task 4: Wire `run_analysis` to storage

**Files:**
- Modify: `app.py:639-709` (the `run_analysis` function)
- Create: `tests/test_run_analysis.py`

**Interfaces:**
- Consumes: `storage.set_result`, `storage.set_error` (Task 2)

- [ ] **Step 1: Write the failing test**

Create `tests/test_run_analysis.py`:

```python
"""
Tests that run_analysis() writes results/errors via storage.py instead of
files. The Gemini call and the Power Automate webhook are stubbed — this
test verifies the storage wiring, not Gemini's output quality (already
covered by manual testing) or the webhook (already covered by prior
production debugging).

Run: python tests/test_run_analysis.py
Requires DATABASE_URL to be set (env var or .streamlit/secrets.toml).
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import app
import storage

TEST_CODE = "RUN_ANALYSIS_TEST_job1"


def cleanup():
    storage.delete_job(TEST_CODE)


def test_run_analysis_writes_result_via_storage():
    cleanup()
    fake_response = MagicMock()
    fake_response.text = json.dumps({
        "lender_evaluations": [],
        "qualifying_lenders": [],
        "concerns": [],
        "no_qualifying_lenders": True,
        "closest_match_if_none": None,
    })
    with patch.object(app, "genai") as mock_genai, patch.object(app, "_post_webhook") as mock_webhook:
        mock_genai.Client.return_value.models.generate_content.return_value = fake_response
        mock_webhook.return_value = 200
        app.run_analysis(TEST_CODE, {"Business_Legal_Name": "Test Co"}, {"total_revenue": 1000})

    job = storage.get_job(TEST_CODE)
    assert job is not None, "run_analysis must create/update a row via storage"
    assert job["result_json"] is not None
    assert job["result_json"]["clientCode"] == TEST_CODE
    assert job["result_json"]["status"] == "complete"
    assert job["error_json"] is None
    print("test_run_analysis_writes_result_via_storage: PASS")


def test_run_analysis_writes_error_via_storage_on_failure():
    cleanup()
    with patch.object(app, "genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.side_effect = RuntimeError("boom")
        app.run_analysis(TEST_CODE, {"Business_Legal_Name": "Test Co"}, {"total_revenue": 1000})

    job = storage.get_job(TEST_CODE)
    assert job is not None
    assert job["error_json"] is not None
    assert "boom" in job["error_json"]["error"]
    assert job["result_json"] is None
    print("test_run_analysis_writes_error_via_storage_on_failure: PASS")


if __name__ == "__main__":
    try:
        test_run_analysis_writes_result_via_storage()
        test_run_analysis_writes_error_via_storage_on_failure()
    finally:
        cleanup()
    print("\nALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_run_analysis.py`
Expected: FAIL — the test's `storage.get_job(TEST_CODE)` after `run_analysis` returns a row with `result_json is None` (or the test errors, since `run_analysis` currently tries to build a `Path` job dir and write files, which does not populate storage at all). Either way, the assertions on `job["result_json"]`/`job["error_json"]` fail.

- [ ] **Step 3: Modify `run_analysis`**

In `app.py`, replace the `run_analysis` function (currently lines 639-709) with:

```python
def run_analysis(job_id: str, app_raw: dict, bs_raw: dict):
    """Runs in a background thread. Writes result/error via storage.py."""
    try:
        app = parse_ocr_app_json(app_raw)
        monthly_rev = extract_monthly_rev(bs_raw)
        if monthly_rev is not None:
            app["monthly_revenue"] = monthly_rev

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=build_prompt(app, bs_raw),
            config={"response_mime_type": "application/json", "temperature": 0, "seed": 42},
        )

        gemini_json = json.loads(response.text)

        # Fill in any missing full_name from the lender pool
        for entry in gemini_json.get("qualifying_lenders", []):
            if not entry.get("full_name"):
                entry["full_name"] = LENDERS.get(entry.get("code", ""), {}).get("full_name", entry.get("code", ""))
        for entry in gemini_json.get("lender_evaluations", []):
            if not entry.get("full_name"):
                entry["full_name"] = LENDERS.get(entry.get("code", ""), {}).get("full_name", entry.get("code", ""))

        result = {
            "clientCode": job_id,
            "status": "complete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "application_data": _map_orbit_fields(app_raw),
            "applicant": {
                "industry": app.get("industry", "Not specified"),
                "state": app.get("state", "Not provided"),
                "zip": app.get("zip", "Not provided"),
                "monthly_revenue": app.get("monthly_revenue", 0),
                "fico": app.get("fico"),
                "tib_years": app.get("tib"),
                "ownership_pct": app.get("ownership"),
                "stacking_positions": app.get("stacking_positions", 0),
                "loan_position": app.get("stacking_positions", 0) + 1,
            },
            "bank_statement_metrics": extract_bs_metrics(bs_raw),
            **gemini_json,
        }

        storage.set_result(job_id, result)
        print(f"[job {job_id[:8]}] complete — {len(gemini_json.get('qualifying_lenders', []))} qualifying lenders")

        try:
            status_code = _post_webhook(result)
            print(f"[job {job_id[:8]}] webhook delivered → HTTP {status_code}")
        except Exception as wb_exc:
            print(f"[job {job_id[:8]}] webhook failed: {wb_exc}")

    except Exception as exc:
        error = {
            "clientCode": job_id,
            "status": "error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        storage.set_error(job_id, error)
        print(f"[job {job_id[:8]}] ERROR: {exc}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_run_analysis.py`
Expected:
```
test_run_analysis_writes_result_via_storage: PASS
test_run_analysis_writes_error_via_storage_on_failure: PASS

ALL TESTS PASSED
```

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_run_analysis.py
git commit -m "wire run_analysis to storage.set_result/set_error"
```

---

### Task 5: Wire `GET /job/<id>`, `GET /jobs`, `GET /queue`, `DELETE /job/<id>` to storage

**Files:**
- Modify: `app.py:715-739` (`_job_status`, `_queue_counts`)
- Modify: `app.py:765-816` (`do_GET`, `do_DELETE`)

**Interfaces:**
- Consumes: `storage.get_job`, `storage.list_jobs`, `storage.delete_job`, `storage.job_status` (Tasks 1-2)

- [ ] **Step 1: Replace `_job_status` and `_queue_counts`**

In `app.py`, replace the `_job_status` function and the `_queue_counts` function (currently lines 715-739) with just:

```python
def _queue_counts() -> dict:
    counts = {"waiting_for_bank_statement": 0, "processing": 0, "complete": 0, "error": 0}
    for job in storage.list_jobs():
        s = storage.job_status(job)
        if s in counts:
            counts[s] += 1
    return counts
```

(`_job_status` is removed entirely — its logic now lives in `storage.job_status`.)

- [ ] **Step 2: Replace `do_GET`**

In `app.py`, replace the `do_GET` method (currently lines 765-806) with:

```python
    def do_GET(self):
        path = self.path.rstrip("/")

        try:
            if path == "/health":
                self._send(200, {"status": "ok", "port": API_PORT, "lenders": len(LENDERS)})

            elif path == "/queue":
                self._send(200, _queue_counts())

            elif path.startswith("/job/"):
                client_id = path[len("/job/"):]
                job = storage.get_job(client_id)
                if job is None:
                    self._send(404, {"error": f"no job found for client_id '{client_id}'"})
                    return
                status = storage.job_status(job)
                if status == "complete":
                    self._send(200, job["result_json"])
                elif status == "error":
                    self._send(200, job["error_json"])
                else:
                    self._send(200, {"clientCode": client_id, "status": status})

            elif path == "/jobs":
                jobs = []
                for job in storage.list_jobs():
                    status = storage.job_status(job)
                    entry = {"clientCode": job["client_code"], "status": status}
                    if status == "complete":
                        result = job["result_json"]
                        entry["qualifying_lenders"] = len(result.get("qualifying_lenders", []))
                        entry["timestamp"] = result.get("timestamp")
                        entry["industry"] = result.get("applicant", {}).get("industry")
                    jobs.append(entry)
                self._send(200, {"total": len(jobs), "jobs": jobs})

            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(503, {"error": f"storage unavailable: {exc}"})
```

- [ ] **Step 3: Replace `do_DELETE`**

In `app.py`, replace the `do_DELETE` method (currently lines 802-816, now shifted slightly after Step 2's edit — locate by its `def do_DELETE(self):` signature) with:

```python
    def do_DELETE(self):
        path = self.path.rstrip("/")
        if path.startswith("/job/"):
            client_id = path[len("/job/"):]
            try:
                deleted = storage.delete_job(client_id)
            except Exception as exc:
                self._send(503, {"error": f"storage unavailable: {exc}"})
                return
            if not deleted:
                self._send(404, {"error": f"no job found for client_id '{client_id}'"})
                return
            print(f"[{client_id}] job deleted")
            self._send(200, {"clientCode": client_id, "deleted": True})
        else:
            self._send(404, {"error": "not found"})
```

- [ ] **Step 4: Start the server locally**

Run (in the background): `python app.py --port 8503`

- [ ] **Step 5: Smoke-test the full lifecycle**

```bash
curl -s -X POST http://localhost:8503/application -H "Content-Type: application/json" -d '{"clientCode":"PLAN_TEST_2","Business_Legal_Name":"Test Co"}'
curl -s http://localhost:8503/job/PLAN_TEST_2
curl -s http://localhost:8503/jobs
curl -s http://localhost:8503/queue
curl -s -X DELETE http://localhost:8503/job/PLAN_TEST_2
curl -s http://localhost:8503/job/PLAN_TEST_2
```

Expected:
- `/job/PLAN_TEST_2` (before delete): `{"clientCode": "PLAN_TEST_2", "status": "waiting_for_bank_statement"}`
- `/jobs`: includes an entry with `"clientCode": "PLAN_TEST_2", "status": "waiting_for_bank_statement"`
- `/queue`: JSON counts object (numbers will vary depending on what else is in the table)
- `DELETE`: `{"clientCode": "PLAN_TEST_2", "deleted": true}`
- `/job/PLAN_TEST_2` (after delete): `404` with `{"error": "no job found for client_id 'PLAN_TEST_2'"}`

- [ ] **Step 6: Stop the local server**

Stop the `python app.py` process started in Step 4.

- [ ] **Step 7: Commit**

```bash
git add app.py
git commit -m "wire GET /job, GET /jobs, GET /queue, DELETE /job to storage"
```

---

### Task 6: Wire startup recovery to storage, remove all file-based storage code

**Files:**
- Modify: `app.py:44-49` (`BASE_DIR`/`JOBS_DIR` config block)
- Modify: `app.py` (`_recover_orphaned_jobs` function, and the `if __name__ == "__main__":` entry point)

**Interfaces:**
- Consumes: `storage.init_db`, `storage.orphaned_jobs` (Tasks 1-2)

- [ ] **Step 1: Remove `JOBS_DIR`**

In `app.py`, change:

```python
BASE_DIR = Path(__file__).parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", BASE_DIR / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
```

to:

```python
BASE_DIR = Path(__file__).parent
```

(`BASE_DIR` is kept — it's still used for the `.streamlit/secrets.toml` fallback reads in both `app.py` and `storage.py`.)

- [ ] **Step 2: Replace `_recover_orphaned_jobs`**

Replace the function body with:

```python
def _recover_orphaned_jobs():
    """On startup, re-run any jobs that have both documents but no result yet."""
    recovered = 0
    for job in storage.orphaned_jobs():
        client_id = job["client_code"]
        print(f"[{client_id}] recovering orphaned job — relaunching analysis")
        threading.Thread(
            target=run_analysis,
            args=(client_id, job["app_json"], job["bs_json"]),
            daemon=True,
        ).start()
        recovered += 1
    if recovered:
        print(f"==> Recovered {recovered} orphaned job(s)")
```

- [ ] **Step 3: Call `storage.init_db()` at startup**

In the `if __name__ == "__main__":` block, change:

```python
if __name__ == "__main__":
    if not GEMINI_API_KEY:
        print("WARNING: GEMINI_API_KEY not set. Requests will fail until it is configured.")

    _recover_orphaned_jobs()
```

to:

```python
if __name__ == "__main__":
    if not GEMINI_API_KEY:
        print("WARNING: GEMINI_API_KEY not set. Requests will fail until it is configured.")

    storage.init_db()
    _recover_orphaned_jobs()
```

- [ ] **Step 4: Confirm no remaining references to `JOBS_DIR`**

Run: `grep -n "JOBS_DIR" app.py`
Expected: no output (no matches).

- [ ] **Step 5: Full startup smoke test**

Run: `python app.py --port 8503` (foreground, briefly)
Expected output includes `Capital Infusion MCA Backend running on port 8503` and no traceback. Stop it with Ctrl+C after confirming it starts cleanly.

- [ ] **Step 6: Re-run both test suites to confirm nothing regressed**

Run: `python tests/test_storage.py && python tests/test_run_analysis.py`
Expected: both print `ALL TESTS PASSED`.

- [ ] **Step 7: Commit**

```bash
git add app.py
git commit -m "wire startup recovery to storage, remove file-based JOBS_DIR entirely"
```

---

### Task 7: Update docs and do a full manual durability smoke test

**Files:**
- Modify: `app.py:1-31` (module docstring)

**Interfaces:**
- None (documentation + manual verification only)

- [ ] **Step 1: Add a Database section to the module docstring**

In `app.py`, in the module docstring (top of file), after the existing `API key` section, add:

```
Database
--------
Requires a Postgres connection string in the DATABASE_URL environment
variable, or place it in .streamlit/secrets.toml:
    DATABASE_URL = "postgresql://user:pass@host/dbname?sslmode=require"
A free Neon (https://neon.tech) database works well for this.
```

- [ ] **Step 2: Commit the docstring change**

```bash
git add app.py
git commit -m "document DATABASE_URL requirement in app.py docstring"
```

- [ ] **Step 3: Manual end-to-end durability check (not automatable — requires a real process restart)**

This step proves the actual goal of this plan: job data must survive the server process dying and restarting, not just pass unit tests.

1. Run: `python app.py --port 8503` in one terminal.
2. In another terminal, submit an application:
   ```bash
   curl -s -X POST http://localhost:8503/application -H "Content-Type: application/json" -d '{"clientCode":"DURABILITY_TEST","Business_Legal_Name":"Test Co"}'
   ```
3. Confirm it's stored: `curl -s http://localhost:8503/job/DURABILITY_TEST` → expect `"status": "waiting_for_bank_statement"`.
4. **Kill the `python app.py` process** (Ctrl+C or stop the background process) — this simulates a Render restart.
5. Start it again: `python app.py --port 8503`.
6. Confirm the job is still there: `curl -s http://localhost:8503/job/DURABILITY_TEST` → expect the same `"status": "waiting_for_bank_statement"` response as step 3.
7. Clean up: `curl -s -X DELETE http://localhost:8503/job/DURABILITY_TEST`.

If step 6 returns the job (rather than a 404), durability is confirmed — job data now survives process restarts, which is the entire goal of this plan.

- [ ] **Step 4: Deploy — set `DATABASE_URL` on Render**

In the Render dashboard, on the `lendersuggestion` service → **Environment**, add `DATABASE_URL` with the same Neon connection string from `.streamlit/secrets.toml`. Redeploy. Confirm via the Render logs that the app starts without a traceback (look for `Capital Infusion MCA Backend running on port ...`).
