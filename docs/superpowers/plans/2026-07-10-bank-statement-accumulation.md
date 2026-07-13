# Minimum-3 Bank Statement Accumulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MatchLender accumulate every bank statement a client sends (keyed by `client_id`, never overwriting) and only run the Gemini lender-match analysis once a job has at least 3 statements, re-running as more arrive.

**Architecture:** `bs.json` becomes a JSON array of per-statement metrics instead of a single object. New pure helpers in `app.py` handle load/append/count and combine-into-one-averaged-dict. A per-client lock plus an in-flight "pending re-run" guard serializes uploads and analysis so there are no overlapping Gemini calls or stale results. The HTTP handlers, `run_analysis`, startup recovery, and status reporting are wired to the new threshold + list model. All new logic lives in `app.py` (this repo is a single-file app).

**Tech Stack:** Python 3.14 standard library only (`http.server`, `threading`, `json`, `pathlib`) plus the existing `google-genai`. Tests are plain `python tests/test_*.py` scripts using `assert` + `print` (no pytest — matches repo convention).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-10-bank-statement-accumulation-design.md`
- MatchLender (`app.py`) only — no change to the OCR repo.
- Built on current file storage (`jobs/<client_id>/`); independent of the pending Neon Postgres migration.
- `MIN_BANK_STATEMENTS = 3` — analysis must not run for a job with fewer than 3 statements.
- Combination: `total_revenue`/`total_deposits`/`avg_daily_balance`/`pos_count` are **averaged** across statements; `nsf_count`/`loan_count` are **summed**.
- Accumulation is keyed by `client_id` / `clientCode` only. Same id → same job (append); different id → separate job.
- One POST = one statement; counting sums an optional per-entry `statement_count` (default 1) so a future batched entry still counts correctly (combination remains average-over-entries — a documented approximation for the batched case, which the current one-at-a-time flow never hits).
- Completed `result.json` responses and the Power Automate/ORBIT webhook payload must stay byte-for-byte identical to today.
- Every new status string is `waiting_for_bank_statements` (plural) — it replaces the old singular `waiting_for_bank_statement`.
- Tests run as `python tests/test_<name>.py` from the repo root; each prints `ALL TESTS PASSED` on success.

---

### Task 1: Statement list storage helpers (`load_statements`, `append_statement`, `count_statements`)

**Files:**
- Modify: `app.py` (add helpers in the "Job directory helpers" section, near `app.py:712`)
- Create: `tests/test_statements.py`

**Interfaces:**
- Produces:
  - `load_statements(job_dir: Path) -> list` — returns the list of statement dicts for a job. Empty list if `bs.json` is absent. A legacy single-object `bs.json` is returned as a 1-element list.
  - `append_statement(job_dir: Path, statement: dict) -> list` — appends `statement` to the job's list, writes `bs.json`, returns the new list.
  - `count_statements(job_dir: Path) -> int` — total statement count for a job (sum of each entry's `statement_count`, default 1 per entry).

- [ ] **Step 1: Write the failing test**

Create `tests/test_statements.py`:

```python
"""
Unit tests for the bank-statement accumulation helpers in app.py.
Pure file helpers — each test uses its own temp directory, no server needed.

Run: python tests/test_statements.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import app


def test_load_statements_empty_when_no_file():
    with tempfile.TemporaryDirectory() as d:
        assert app.load_statements(Path(d)) == []
    print("test_load_statements_empty_when_no_file: PASS")


def test_append_then_load_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        app.append_statement(job, {"summary_metrics": {"total_revenue": 100}})
        app.append_statement(job, {"summary_metrics": {"total_revenue": 200}})
        stmts = app.load_statements(job)
        assert len(stmts) == 2, stmts
        assert stmts[0]["summary_metrics"]["total_revenue"] == 100
        assert stmts[1]["summary_metrics"]["total_revenue"] == 200
    print("test_append_then_load_roundtrip: PASS")


def test_load_legacy_single_object_as_list():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        # Simulate an old-format bs.json (single flat object)
        (job / "bs.json").write_text('{"nsf_count": 1, "total_revenue": 5000}')
        stmts = app.load_statements(job)
        assert isinstance(stmts, list) and len(stmts) == 1, stmts
        assert stmts[0]["total_revenue"] == 5000
    print("test_load_legacy_single_object_as_list: PASS")


def test_count_statements_counts_entries():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        assert app.count_statements(job) == 0
        app.append_statement(job, {"summary_metrics": {}})
        app.append_statement(job, {"summary_metrics": {}})
        assert app.count_statements(job) == 2
    print("test_count_statements_counts_entries: PASS")


def test_count_statements_honors_statement_count_field():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        app.append_statement(job, {"summary_metrics": {}, "statement_count": 3})
        assert app.count_statements(job) == 3
    print("test_count_statements_honors_statement_count_field: PASS")


if __name__ == "__main__":
    test_load_statements_empty_when_no_file()
    test_append_then_load_roundtrip()
    test_load_legacy_single_object_as_list()
    test_count_statements_counts_entries()
    test_count_statements_honors_statement_count_field()
    print("\nALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_statements.py`
Expected: FAIL with `AttributeError: module 'app' has no attribute 'load_statements'`.

- [ ] **Step 3: Add the helpers to `app.py`**

In `app.py`, in the "Job directory helpers" section (just above `def _job_status`, around line 712), add:

```python
def load_statements(job_dir: Path) -> list:
    """Return the job's list of bank-statement dicts. Empty list if none.
    A legacy single-object bs.json is returned as a 1-element list."""
    bs_file = job_dir / "bs.json"
    if not bs_file.exists():
        return []
    data = json.loads(bs_file.read_text())
    if isinstance(data, list):
        return data
    return [data]  # legacy single-object format


def append_statement(job_dir: Path, statement: dict) -> list:
    """Append one bank statement to the job's list and persist it."""
    job_dir.mkdir(parents=True, exist_ok=True)
    statements = load_statements(job_dir)
    statements.append(statement)
    (job_dir / "bs.json").write_text(json.dumps(statements))
    return statements


def count_statements(job_dir: Path) -> int:
    """Total number of statements for a job (sums each entry's
    statement_count, defaulting to 1 per entry)."""
    total = 0
    for s in load_statements(job_dir):
        c = s.get("statement_count", 1) if isinstance(s, dict) else 1
        total += c if isinstance(c, int) and c > 0 else 1
    return total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_statements.py`
Expected: all 5 lines print `PASS`, ending with `ALL TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_statements.py
git commit -m "add bank-statement list storage helpers (load/append/count)"
```

---

### Task 2: `combine_statements` — average/sum N statements into one metrics dict

**Files:**
- Modify: `app.py` (add near `extract_bs_metrics`, around `app.py:519`)
- Modify: `tests/test_statements.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `combine_statements(statements: list) -> dict` — returns
  `{"summary_metrics": {...}, "statement_count": N}` where the inner dict has
  averaged `total_revenue`, `total_deposits`, `avg_daily_balance`, `pos_count`
  and summed `nsf_count`, `loan_count`. Each field is `None` if no statement
  provides it. The shape is consumed unchanged by `extract_monthly_rev`,
  `extract_bs_metrics`, and `build_prompt`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_statements.py` (before the `if __name__` block):

```python
def test_combine_averages_and_sums():
    stmts = [
        {"summary_metrics": {"total_revenue": 30000, "avg_daily_balance": 2000, "nsf_count": 1}},
        {"summary_metrics": {"total_revenue": 40000, "avg_daily_balance": 4000, "nsf_count": 0}},
        {"summary_metrics": {"total_revenue": 50000, "avg_daily_balance": 3000, "nsf_count": 2}},
    ]
    out = app.combine_statements(stmts)
    m = out["summary_metrics"]
    assert out["statement_count"] == 3
    assert m["total_revenue"] == 40000.0, m          # averaged
    assert m["avg_daily_balance"] == 3000.0, m       # averaged
    assert m["nsf_count"] == 3, m                     # summed
    print("test_combine_averages_and_sums: PASS")


def test_combine_handles_flat_and_total_credits_fallback():
    # Mix of flat (legacy) and wrapped entries; total_credits used when
    # total_revenue absent.
    stmts = [
        {"total_credits": 10000, "nsf_count": 1},                 # flat
        {"summary_metrics": {"total_revenue": 20000, "nsf_count": 1}},
    ]
    out = app.combine_statements(stmts)
    m = out["summary_metrics"]
    assert m["total_revenue"] == 15000.0, m   # (10000 + 20000) / 2
    assert m["nsf_count"] == 2, m
    print("test_combine_handles_flat_and_total_credits_fallback: PASS")


def test_combine_missing_field_is_none():
    out = app.combine_statements([{"summary_metrics": {"total_revenue": 100}}])
    assert out["summary_metrics"]["pos_count"] is None
    print("test_combine_missing_field_is_none: PASS")
```

Add these three calls to the `if __name__ == "__main__":` block, after the Task 1 calls and before the final `print`:

```python
    test_combine_averages_and_sums()
    test_combine_handles_flat_and_total_credits_fallback()
    test_combine_missing_field_is_none()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_statements.py`
Expected: FAIL with `AttributeError: module 'app' has no attribute 'combine_statements'`.

- [ ] **Step 3: Add `combine_statements` to `app.py`**

In `app.py`, immediately after `extract_bs_metrics` (around line 519), add:

```python
def combine_statements(statements: list) -> dict:
    """Average per-month metrics (revenue, deposits, balance, pos) and sum
    count metrics (nsf, loan) across N statements, returning a single
    summary_metrics-shaped dict that the extract_* / build_prompt functions
    consume unchanged."""
    metrics = [s.get("summary_metrics", s) if isinstance(s, dict) else {} for s in statements]

    def _nums(key):
        return [m[key] for m in metrics if isinstance(m.get(key), (int, float))]

    def _avg(key):
        vals = _nums(key)
        return round(sum(vals) / len(vals), 2) if vals else None

    def _sum(key):
        vals = _nums(key)
        return sum(vals) if vals else None

    def _avg_revenue():
        vals = []
        for m in metrics:
            v = m.get("total_revenue")
            if not isinstance(v, (int, float)):
                v = m.get("total_credits")
            if isinstance(v, (int, float)):
                vals.append(v)
        return round(sum(vals) / len(vals), 2) if vals else None

    combined = {
        "total_revenue":     _avg_revenue(),
        "total_deposits":    _avg("total_deposits"),
        "avg_daily_balance": _avg("avg_daily_balance"),
        "pos_count":         _avg("pos_count"),
        "nsf_count":         _sum("nsf_count"),
        "loan_count":        _sum("loan_count"),
    }
    return {"summary_metrics": combined, "statement_count": len(statements)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_statements.py`
Expected: all 8 lines print `PASS`, ending with `ALL TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_statements.py
git commit -m "add combine_statements: average/sum N statements into one metrics dict"
```

---

### Task 3: `MIN_BANK_STATEMENTS` constant + count-aware `_job_status` and `_queue_counts`

**Files:**
- Modify: `app.py:47-49` (config block — add the constant)
- Modify: `app.py:715-739` (`_job_status`, `_queue_counts`)
- Modify: `tests/test_statements.py`

**Interfaces:**
- Consumes: `count_statements` (Task 1).
- Produces: `_job_status(job_dir: Path) -> str` returning one of `"complete"`,
  `"error"`, `"processing"`, `"waiting_for_application"`,
  `"waiting_for_bank_statements"`, `"unknown"`. `processing` requires
  `app.json` present AND `count_statements(job_dir) >= MIN_BANK_STATEMENTS`.
  Module constant `MIN_BANK_STATEMENTS = 3`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_statements.py` (before the `if __name__` block):

```python
import json as _json


def _write(job, name, obj):
    (job / name).write_text(_json.dumps(obj))


def test_status_waiting_for_bank_statements_below_threshold():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        _write(job, "app.json", {"x": 1})
        app.append_statement(job, {"summary_metrics": {}})
        app.append_statement(job, {"summary_metrics": {}})
        assert app.count_statements(job) == 2
        assert app._job_status(job) == "waiting_for_bank_statements"
    print("test_status_waiting_for_bank_statements_below_threshold: PASS")


def test_status_processing_at_threshold():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        _write(job, "app.json", {"x": 1})
        for _ in range(3):
            app.append_statement(job, {"summary_metrics": {}})
        assert app._job_status(job) == "processing"
    print("test_status_processing_at_threshold: PASS")


def test_status_waiting_for_application_when_only_statements():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        for _ in range(3):
            app.append_statement(job, {"summary_metrics": {}})
        assert app._job_status(job) == "waiting_for_application"
    print("test_status_waiting_for_application_when_only_statements: PASS")


def test_status_complete_and_error():
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        _write(job, "app.json", {"x": 1})
        for _ in range(3):
            app.append_statement(job, {"summary_metrics": {}})
        _write(job, "result.json", {"status": "complete"})
        assert app._job_status(job) == "complete"
    with tempfile.TemporaryDirectory() as d:
        job = Path(d)
        _write(job, "app.json", {"x": 1})
        _write(job, "error.json", {"status": "error"})
        assert app._job_status(job) == "error"
    print("test_status_complete_and_error: PASS")
```

Add these four calls to the `if __name__ == "__main__":` block (after the Task 2 calls):

```python
    test_status_waiting_for_bank_statements_below_threshold()
    test_status_processing_at_threshold()
    test_status_waiting_for_application_when_only_statements()
    test_status_complete_and_error()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_statements.py`
Expected: FAIL — `_job_status` still uses the old singular `waiting_for_bank_statement` and file-existence logic, so `test_status_waiting_for_bank_statements_below_threshold` fails (a job with 2 statements returns `"processing"` under the old code).

- [ ] **Step 3: Add the constant**

In `app.py`, in the config block (after `JOBS_DIR.mkdir(...)`, around line 49), add:

```python
MIN_BANK_STATEMENTS = 3
```

- [ ] **Step 4: Replace `_job_status` and `_queue_counts`**

In `app.py`, replace the `_job_status` function (currently `app.py:715-728`) with:

```python
def _job_status(job_dir: Path) -> str:
    has_app = (job_dir / "app.json").exists()
    n = count_statements(job_dir)
    if (job_dir / "result.json").exists():
        return "complete"
    if (job_dir / "error.json").exists():
        return "error"
    if has_app and n >= MIN_BANK_STATEMENTS:
        return "processing"
    if n > 0 and not has_app:
        return "waiting_for_application"
    if has_app:
        return "waiting_for_bank_statements"
    return "unknown"
```

Then replace the `_queue_counts` function (currently `app.py:732-739`) with:

```python
def _queue_counts() -> dict:
    counts = {"waiting_for_bank_statements": 0, "processing": 0, "complete": 0, "error": 0}
    for job_dir in JOBS_DIR.iterdir():
        if job_dir.is_dir():
            s = _job_status(job_dir)
            if s in counts:
                counts[s] += 1
    return counts
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python tests/test_statements.py`
Expected: all 12 lines print `PASS`, ending with `ALL TESTS PASSED`.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_statements.py
git commit -m "gate status on MIN_BANK_STATEMENTS; rename waiting status to plural"
```

---

### Task 4: `run_analysis` takes a statement list + combines + prompt notes month count

**Files:**
- Modify: `app.py:646-709` (`run_analysis`)
- Modify: `app.py:525-552` (`build_prompt` — surface statement count)
- Create: `tests/test_analysis_flow.py`

**Interfaces:**
- Consumes: `combine_statements` (Task 2).
- Produces: `run_analysis(job_id: str, app_raw: dict, statements: list) -> None`
  — combines the statement list internally, writes `result.json`/`error.json`
  via the existing file writes, fires the webhook. Signature changes from
  `(job_id, app_raw, bs_raw)` to `(job_id, app_raw, statements)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_analysis_flow.py`:

```python
"""
Tests that run_analysis() accepts a LIST of statements, combines them, and
writes result/error. Gemini and the webhook are stubbed — this verifies the
list+combine wiring, not Gemini output quality or the webhook.

Run: python tests/test_analysis_flow.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import app

TEST_CODE = "ANALYSIS_FLOW_TEST_job1"


def _job_dir():
    return app.JOBS_DIR / TEST_CODE


def cleanup():
    d = _job_dir()
    if d.exists():
        shutil.rmtree(d)


def test_run_analysis_accepts_statement_list_and_writes_result():
    cleanup()
    _job_dir().mkdir(parents=True, exist_ok=True)
    fake = MagicMock()
    fake.text = json.dumps({
        "lender_evaluations": [], "qualifying_lenders": [],
        "concerns": [], "no_qualifying_lenders": True, "closest_match_if_none": None,
    })
    statements = [
        {"summary_metrics": {"total_revenue": 30000, "nsf_count": 1}},
        {"summary_metrics": {"total_revenue": 40000, "nsf_count": 0}},
        {"summary_metrics": {"total_revenue": 50000, "nsf_count": 2}},
    ]
    with patch.object(app, "genai") as mock_genai, patch.object(app, "_post_webhook") as mock_wh:
        mock_genai.Client.return_value.models.generate_content.return_value = fake
        mock_wh.return_value = 200
        app.run_analysis(TEST_CODE, {"Business_Legal_Name": "Test Co"}, statements)

    result = json.loads((_job_dir() / "result.json").read_text())
    assert result["status"] == "complete"
    # Combined monthly revenue is the AVERAGE (40000), not the sum (120000)
    assert result["bank_statement_metrics"]["total_revenue"] == 40000.0, result["bank_statement_metrics"]
    cleanup()
    print("test_run_analysis_accepts_statement_list_and_writes_result: PASS")


def test_run_analysis_writes_error_on_failure():
    cleanup()
    _job_dir().mkdir(parents=True, exist_ok=True)
    with patch.object(app, "genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.side_effect = RuntimeError("boom")
        app.run_analysis(TEST_CODE, {"Business_Legal_Name": "Test Co"},
                         [{"summary_metrics": {"total_revenue": 1000}}])
    error = json.loads((_job_dir() / "error.json").read_text())
    assert "boom" in error["error"]
    cleanup()
    print("test_run_analysis_writes_error_on_failure: PASS")


if __name__ == "__main__":
    try:
        test_run_analysis_accepts_statement_list_and_writes_result()
        test_run_analysis_writes_error_on_failure()
    finally:
        cleanup()
    print("\nALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_analysis_flow.py`
Expected: FAIL — `run_analysis` currently treats its third arg as a single `bs_raw` dict and calls `extract_monthly_rev(bs_raw)` on the list, so it errors or writes a wrong `total_revenue` (the combine step doesn't exist in the flow yet).

- [ ] **Step 3: Modify `run_analysis`**

In `app.py`, replace the `run_analysis` function (currently `app.py:646-709`) with:

```python
def run_analysis(job_id: str, app_raw: dict, statements: list):
    """Runs in a background thread. Combines the accumulated statements,
    then writes result.json or error.json."""
    job_dir = JOBS_DIR / job_id
    try:
        bs_combined = combine_statements(statements)

        app = parse_ocr_app_json(app_raw)
        monthly_rev = extract_monthly_rev(bs_combined)
        if monthly_rev is not None:
            app["monthly_revenue"] = monthly_rev

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=build_prompt(app, bs_combined),
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
            "bank_statement_metrics": extract_bs_metrics(bs_combined),
            **gemini_json,
        }

        (job_dir / "result.json").write_text(json.dumps(result, indent=2))
        print(f"[job {job_id[:8]}] complete — {len(gemini_json.get('qualifying_lenders', []))} qualifying lenders ({bs_combined['statement_count']} statements)")

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
        (job_dir / "error.json").write_text(json.dumps(error, indent=2))
        print(f"[job {job_id[:8]}] ERROR: {exc}")
```

- [ ] **Step 4: Surface the statement count in the prompt**

In `app.py`, in `build_prompt` (around `app.py:551`), replace this block:

```python
=== BANK STATEMENT DATA ===
{json.dumps(bs, indent=2)}
```

with:

```python
=== BANK STATEMENT DATA (averaged across {bs.get("statement_count", 1)} monthly statements) ===
{json.dumps(bs, indent=2)}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python tests/test_analysis_flow.py`
Expected:
```
test_run_analysis_accepts_statement_list_and_writes_result: PASS
test_run_analysis_writes_error_on_failure: PASS

ALL TESTS PASSED
```

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_analysis_flow.py
git commit -m "run_analysis takes a statement list and combines before analysis"
```

---

### Task 5: Per-client lock + in-flight re-run guard (`_maybe_launch_analysis`)

**Files:**
- Modify: `app.py` (add a concurrency section after `run_analysis`, around `app.py:710`)
- Modify: `tests/test_analysis_flow.py`

**Interfaces:**
- Consumes: `run_analysis` (Task 4), `load_statements` (Task 1).
- Produces:
  - `_client_lock(client_id: str) -> threading.Lock` — returns a stable per-client lock.
  - `_maybe_launch_analysis(client_id: str) -> None` — call while holding the
    client lock. If no analysis is running for the client, starts one in a
    daemon thread; if one is running, sets a pending flag so it re-runs once
    when it finishes. Each run reloads `app.json` + statements so it always
    reflects the latest accumulated set.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_analysis_flow.py` (before the `if __name__` block):

```python
import threading
import time


def test_maybe_launch_runs_once_and_reruns_when_pending():
    cleanup()
    job = _job_dir()
    job.mkdir(parents=True, exist_ok=True)
    (job / "app.json").write_text(json.dumps({"Business_Legal_Name": "Test Co"}))
    for _ in range(3):
        app.append_statement(job, {"summary_metrics": {"total_revenue": 10000}})

    calls = []
    started = threading.Event()
    release = threading.Event()

    def fake_run_analysis(client_id, app_raw, statements):
        calls.append(len(statements))
        started.set()
        release.wait(timeout=5)  # hold the "analysis" open so we can queue a rerun

    with patch.object(app, "run_analysis", side_effect=fake_run_analysis):
        with app._client_lock(TEST_CODE):
            app._maybe_launch_analysis(TEST_CODE)   # launches run #1
        started.wait(timeout=5)
        # A 4th statement arrives while run #1 is still "in progress"
        app.append_statement(job, {"summary_metrics": {"total_revenue": 10000}})
        with app._client_lock(TEST_CODE):
            app._maybe_launch_analysis(TEST_CODE)   # should set pending, NOT start a 2nd thread
        release.set()
        time.sleep(0.5)  # let run #1 finish and the pending rerun fire

    assert len(calls) == 2, f"expected exactly 2 runs (initial + 1 rerun), got {len(calls)}"
    assert calls[0] == 3 and calls[1] == 4, calls  # rerun sees the 4th statement
    cleanup()
    print("test_maybe_launch_runs_once_and_reruns_when_pending: PASS")
```

Add this call inside the `if __name__ == "__main__":` `try` block (after the Task 4 calls):

```python
        test_maybe_launch_runs_once_and_reruns_when_pending()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_analysis_flow.py`
Expected: FAIL with `AttributeError: module 'app' has no attribute '_client_lock'`.

- [ ] **Step 3: Add the concurrency helpers to `app.py`**

In `app.py`, immediately after `run_analysis` (around line 710), add:

```python
# ---------------------------------------------------------------------------
# Per-client analysis serialization
# ---------------------------------------------------------------------------
_registry_lock = threading.Lock()
_client_locks: dict[str, threading.Lock] = {}
_analysis_state: dict[str, dict] = {}  # client_id -> {"running": bool, "pending": bool}


def _client_lock(client_id: str) -> threading.Lock:
    """Return a stable lock for a client_id (created on first use)."""
    with _registry_lock:
        lock = _client_locks.get(client_id)
        if lock is None:
            lock = threading.Lock()
            _client_locks[client_id] = lock
        return lock


def _maybe_launch_analysis(client_id: str) -> None:
    """Launch analysis for a client unless one is already running (in which
    case flag a single re-run). MUST be called while holding _client_lock(client_id)."""
    state = _analysis_state.setdefault(client_id, {"running": False, "pending": False})
    if state["running"]:
        state["pending"] = True
        return
    state["running"] = True
    threading.Thread(target=_analysis_loop, args=(client_id,), daemon=True).start()


def _analysis_loop(client_id: str) -> None:
    """Run analysis, re-running once for each statement that arrived while a
    previous run was in progress. Always reloads the latest app + statements."""
    while True:
        job_dir = JOBS_DIR / client_id
        app_raw = json.loads((job_dir / "app.json").read_text())
        statements = load_statements(job_dir)
        run_analysis(client_id, app_raw, statements)

        with _client_lock(client_id):
            state = _analysis_state[client_id]
            if state["pending"]:
                state["pending"] = False
                continue
            state["running"] = False
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_analysis_flow.py`
Expected: all three tests print `PASS`, ending with `ALL TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_analysis_flow.py
git commit -m "add per-client lock and in-flight re-run guard for analysis"
```

---

### Task 6: Wire `POST /application` and `POST /bank-statement` to accumulation

**Files:**
- Modify: `app.py:824-894` (the `do_POST` method)

**Interfaces:**
- Consumes: `append_statement`, `count_statements` (Task 1), `MIN_BANK_STATEMENTS`
  (Task 3), `_client_lock`, `_maybe_launch_analysis` (Task 5).

- [ ] **Step 1: Replace the body of `do_POST`**

In `app.py`, replace the entire `do_POST` method (currently `app.py:824-894`) with:

```python
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return

        path = self.path.rstrip("/")

        if path == "/application":
            if not GEMINI_API_KEY:
                self._send(500, {"error": "GEMINI_API_KEY not configured"})
                return
            client_id = (data.pop("clientCode", None) or data.pop("client_id", None)) if isinstance(data, dict) else None
            if not client_id:
                self._send(400, {"error": "client_id is required"})
                return

            with _client_lock(client_id):
                job_dir = JOBS_DIR / client_id
                job_dir.mkdir(parents=True, exist_ok=True)
                (job_dir / "app.json").write_text(json.dumps(data))
                # Clear any previous result so a re-submission starts fresh
                for stale in ("result.json", "error.json"):
                    (job_dir / stale).unlink(missing_ok=True)
                print(f"[{client_id}] application received")

                n = count_statements(job_dir)
                if n >= MIN_BANK_STATEMENTS:
                    print(f"[{client_id}] {n} statements present — launching analysis")
                    _maybe_launch_analysis(client_id)
                    self._send(200, {"clientCode": client_id, "status": "processing",
                                     "poll": f"GET /job/{client_id}"})
                else:
                    self._send(200, {"clientCode": client_id, "status": "received",
                                     "received": n, "required": MIN_BANK_STATEMENTS})

        elif path == "/bank-statement":
            if not GEMINI_API_KEY:
                self._send(500, {"error": "GEMINI_API_KEY not configured"})
                return
            client_id = (data.pop("clientCode", None) or data.pop("client_id", None)) if isinstance(data, dict) else None
            if not client_id:
                self._send(400, {"error": "client_id is required"})
                return

            with _client_lock(client_id):
                job_dir = JOBS_DIR / client_id
                append_statement(job_dir, data)
                n = count_statements(job_dir)
                print(f"[{client_id}] bank statement received ({n} total)")

                has_app = (job_dir / "app.json").exists()
                if has_app and n >= MIN_BANK_STATEMENTS:
                    print(f"[{client_id}] {n} statements present — launching analysis")
                    _maybe_launch_analysis(client_id)
                    self._send(200, {"clientCode": client_id, "status": "processing",
                                     "poll": f"GET /job/{client_id}"})
                elif not has_app:
                    self._send(200, {"clientCode": client_id, "status": "waiting_for_application",
                                     "received": n, "required": MIN_BANK_STATEMENTS})
                else:
                    self._send(200, {"clientCode": client_id, "status": "waiting_for_bank_statements",
                                     "received": n, "required": MIN_BANK_STATEMENTS})

        else:
            self._send(404, {"error": "unknown endpoint"})
```

- [ ] **Step 2: Start the server locally (isolated JOBS_DIR)**

Run (in the background), using a throwaway jobs dir so real data is untouched:

```bash
JOBS_DIR=/tmp/ci_accum_jobs python app.py --port 8599
```

Expected output includes: `Capital Infusion MCA Backend running on port 8599`.
(If `GEMINI_API_KEY` is unset it prints a warning — fine for the threshold smoke test below, which never reaches 3+app to trigger Gemini.)

- [ ] **Step 3: Smoke-test the threshold gating**

Run:

```bash
curl -s -X POST http://localhost:8599/application -H "Content-Type: application/json" -d '{"clientCode":"ACCUM_1","Business_Legal_Name":"Test Co"}'
curl -s -X POST http://localhost:8599/bank-statement -H "Content-Type: application/json" -d '{"client_id":"ACCUM_1","summary_metrics":{"total_revenue":30000,"nsf_count":1}}'
curl -s http://localhost:8599/job/ACCUM_1
curl -s -X POST http://localhost:8599/bank-statement -H "Content-Type: application/json" -d '{"client_id":"ACCUM_1","summary_metrics":{"total_revenue":40000,"nsf_count":0}}'
curl -s http://localhost:8599/job/ACCUM_1
```

Expected:
- After the 1st statement, `/job/ACCUM_1` → `{"clientCode": "ACCUM_1", "status": "waiting_for_bank_statements", "received": 1, "required": 3}`.
- After the 2nd statement, `/job/ACCUM_1` → same shape with `"received": 2`.
- No `result.json` is produced (analysis has NOT run — under threshold).

- [ ] **Step 4: Verify statements accumulated (not overwritten)**

Run:

```bash
python -c "
import json
d = json.load(open('/tmp/ci_accum_jobs/ACCUM_1/bs.json'))
print('type:', type(d).__name__, 'count:', len(d))
print('revenues:', [s['summary_metrics']['total_revenue'] for s in d])
"
```

Expected: `type: list count: 2` and `revenues: [30000, 40000]` — both statements are present, the first was not overwritten.

- [ ] **Step 5: Stop the server and clean up**

Stop the `python app.py` process from Step 2, then: `rm -rf /tmp/ci_accum_jobs`.

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "wire POST handlers to accumulate statements and gate on min-3"
```

---

### Task 7: Wire `GET /job/<id>` waiting response, `/jobs`, and startup recovery

**Files:**
- Modify: `app.py:765-807` (`do_GET`)
- Modify: `app.py:900-922` (`_recover_orphaned_jobs`)

**Interfaces:**
- Consumes: `_job_status` (Task 3), `count_statements` (Task 1),
  `MIN_BANK_STATEMENTS` (Task 3), `load_statements` (Task 1),
  `_client_lock`/`_maybe_launch_analysis` (Task 5).

- [ ] **Step 1: Update the `/job/<id>` waiting branch in `do_GET`**

In `app.py`, in `do_GET`, replace the `/job/` branch (currently `app.py:774-786`) with:

```python
        elif path.startswith("/job/"):
            client_id = path[len("/job/"):]
            job_dir = JOBS_DIR / client_id
            if not job_dir.is_dir():
                self._send(404, {"error": f"no job found for client_id '{client_id}'"})
                return
            status = _job_status(job_dir)
            if status == "complete":
                self._send(200, json.loads((job_dir / "result.json").read_text()))
            elif status == "error":
                self._send(200, json.loads((job_dir / "error.json").read_text()))
            else:
                self._send(200, {"clientCode": client_id, "status": status,
                                 "received": count_statements(job_dir),
                                 "required": MIN_BANK_STATEMENTS})
```

- [ ] **Step 2: Replace `_recover_orphaned_jobs`**

In `app.py`, replace `_recover_orphaned_jobs` (currently `app.py:900-922`) with:

```python
def _recover_orphaned_jobs():
    """On startup, re-run any jobs that have an application and at least
    MIN_BANK_STATEMENTS statements but no result yet."""
    recovered = 0
    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        has_app = (job_dir / "app.json").exists()
        n = count_statements(job_dir)
        has_result = (job_dir / "result.json").exists()
        has_error = (job_dir / "error.json").exists()
        if has_app and n >= MIN_BANK_STATEMENTS and not has_result and not has_error:
            client_id = job_dir.name
            print(f"[{client_id}] recovering orphaned job ({n} statements) — relaunching analysis")
            with _client_lock(client_id):
                _maybe_launch_analysis(client_id)
            recovered += 1
    if recovered:
        print(f"==> Recovered {recovered} orphaned job(s)")
```

- [ ] **Step 3: Re-run the unit suites to confirm no regression**

Run: `python tests/test_statements.py && python tests/test_analysis_flow.py`
Expected: both print `ALL TESTS PASSED`.

- [ ] **Step 4: Full end-to-end smoke test (with a real analysis at the 3rd statement)**

This requires `GEMINI_API_KEY` to be set. Start the server against a throwaway jobs dir:

```bash
JOBS_DIR=/tmp/ci_e2e_jobs python app.py --port 8599
```

Then run:

```bash
curl -s -X POST http://localhost:8599/application -H "Content-Type: application/json" -d '{"clientCode":"E2E_1","Business_Legal_Name":"Test Co","business_description":"restaurant"}'
curl -s -X POST http://localhost:8599/bank-statement -H "Content-Type: application/json" -d '{"client_id":"E2E_1","summary_metrics":{"total_revenue":30000,"nsf_count":1,"avg_daily_balance":2000}}'
curl -s -X POST http://localhost:8599/bank-statement -H "Content-Type: application/json" -d '{"client_id":"E2E_1","summary_metrics":{"total_revenue":40000,"nsf_count":0,"avg_daily_balance":4000}}'
curl -s http://localhost:8599/job/E2E_1   # expect waiting_for_bank_statements, received 2
curl -s -X POST http://localhost:8599/bank-statement -H "Content-Type: application/json" -d '{"client_id":"E2E_1","summary_metrics":{"total_revenue":50000,"nsf_count":2,"avg_daily_balance":3000}}'
sleep 8
curl -s http://localhost:8599/job/E2E_1   # expect a complete result
```

Expected:
- The `received: 2` poll shows `status: waiting_for_bank_statements` (no analysis yet).
- After the 3rd statement + `sleep`, `/job/E2E_1` returns a full result with `"status": "complete"`, and `bank_statement_metrics.total_revenue` is `40000.0` (the AVERAGE of 30k/40k/50k, proving combination — not `120000`).

- [ ] **Step 5: Verify restart recovery still reflects all statements**

With the server from Step 4 stopped, add a 4th statement directly to the file, then restart and confirm re-analysis over 4:

```bash
python -c "
import json, pathlib
p = pathlib.Path('/tmp/ci_e2e_jobs/E2E_1/bs.json')
d = json.load(open(p)); d.append({'summary_metrics':{'total_revenue':60000,'nsf_count':0,'avg_daily_balance':3500}})
json.dump(d, open(p,'w'))
# remove result so it is 'orphaned' and eligible for recovery
pathlib.Path('/tmp/ci_e2e_jobs/E2E_1/result.json').unlink(missing_ok=True)
print('statements now:', len(d))
"
JOBS_DIR=/tmp/ci_e2e_jobs python app.py --port 8599 &
sleep 8
curl -s http://localhost:8599/job/E2E_1
```

Expected: startup logs `recovering orphaned job (4 statements)`, and `/job/E2E_1` returns `status: complete` with `bank_statement_metrics.total_revenue == 45000.0` (average of 30/40/50/60k).

- [ ] **Step 6: Stop the server and clean up**

Stop the `python app.py` process, then: `rm -rf /tmp/ci_e2e_jobs`.

- [ ] **Step 7: Update the module docstring**

In `app.py`, in the top module docstring, update the `POST /bank-statement` and status-values lines (currently `app.py:11-18`) to:

```
POST /bank-statement       Body: bank-statement OCR JSON (include "client_id"/"clientCode").
                           Each POST is ONE statement; statements accumulate per client.
                           Analysis runs only once a client has >= 3 statements AND an
                           application; later statements fold in and re-run.
                           Returns: {"clientCode": "...", "status": "...", "received": N, "required": 3}

GET  /job/<id>             Returns: {"clientCode": "...", "status": "...", "result": {...}}
                           status values: waiting_for_application | waiting_for_bank_statements
                                          | processing | complete | error
```

- [ ] **Step 8: Commit**

```bash
git add app.py
git commit -m "wire GET /job waiting response and startup recovery to min-3 accumulation; update docstring"
```
