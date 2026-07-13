"""
Pure unit tests for the bank-statement accumulation logic — no database and
no server needed:
  - app.combine_statements()      (averages/sums N statements into one)
  - storage.count_statements()    (counts entries, honoring statement_count)
  - storage.job_status()          (min-3 gating on a plain row dict)

Run: python tests/test_statements.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import app
import storage


# ---------------------------------------------------------------------------
# combine_statements
# ---------------------------------------------------------------------------
def test_combine_averages_and_sums():
    stmts = [
        {"summary_metrics": {"total_revenue": 30000, "avg_daily_balance": 2000, "nsf_count": 1, "deposit_count": 6, "cash_flow": 1000}},
        {"summary_metrics": {"total_revenue": 40000, "avg_daily_balance": 4000, "nsf_count": 0, "deposit_count": 8, "cash_flow": 2000}},
        {"summary_metrics": {"total_revenue": 50000, "avg_daily_balance": 3000, "nsf_count": 2, "deposit_count": 10, "cash_flow": 3000}},
    ]
    out = app.combine_statements(stmts)
    m = out["summary_metrics"]
    assert out["statement_count"] == 3
    assert m["total_revenue"] == 40000.0, m          # averaged
    assert m["avg_daily_balance"] == 3000.0, m       # averaged
    assert m["deposit_count"] == 8.0, m              # averaged, survives
    assert m["cash_flow"] == 2000.0, m               # averaged, survives
    assert m["nsf_count"] == 3, m                     # summed
    print("test_combine_averages_and_sums: PASS")


def test_combine_handles_flat_and_total_credits_fallback():
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


# ---------------------------------------------------------------------------
# count_statements
# ---------------------------------------------------------------------------
def test_count_none_and_empty():
    assert storage.count_statements(None) == 0
    assert storage.count_statements([]) == 0
    print("test_count_none_and_empty: PASS")


def test_count_counts_list_entries():
    stmts = [{"summary_metrics": {}}, {"summary_metrics": {}}]
    assert storage.count_statements(stmts) == 2
    print("test_count_counts_list_entries: PASS")


def test_count_honors_statement_count_field():
    stmts = [{"summary_metrics": {}, "statement_count": 3}]
    assert storage.count_statements(stmts) == 3
    print("test_count_honors_statement_count_field: PASS")


def test_count_legacy_single_object_is_one():
    # A legacy single-object bs_json counts as one statement.
    assert storage.count_statements({"nsf_count": 1, "total_revenue": 5000}) == 1
    print("test_count_legacy_single_object_is_one: PASS")


# ---------------------------------------------------------------------------
# job_status (min-3 gating) — operates on a plain row dict, no DB
# ---------------------------------------------------------------------------
def _row(app_json=None, bs=None, result=None, error=None):
    return {"app_json": app_json, "bs_json": bs, "result_json": result, "error_json": error}


def test_status_waiting_for_bank_statements_below_threshold():
    row = _row(app_json={"x": 1}, bs=[{"summary_metrics": {}}, {"summary_metrics": {}}])
    assert storage.count_statements(row["bs_json"]) == 2
    assert storage.job_status(row) == "waiting_for_bank_statements"
    print("test_status_waiting_for_bank_statements_below_threshold: PASS")


def test_status_processing_at_threshold():
    row = _row(app_json={"x": 1}, bs=[{"summary_metrics": {}}] * 3)
    assert storage.job_status(row) == "processing"
    print("test_status_processing_at_threshold: PASS")


def test_status_waiting_for_application_when_only_statements():
    row = _row(bs=[{"summary_metrics": {}}] * 3)
    assert storage.job_status(row) == "waiting_for_application"
    print("test_status_waiting_for_application_when_only_statements: PASS")


def test_status_complete_and_error():
    complete = _row(app_json={"x": 1}, bs=[{"summary_metrics": {}}] * 3, result={"status": "complete"})
    assert storage.job_status(complete) == "complete"
    error = _row(app_json={"x": 1}, error={"status": "error"})
    assert storage.job_status(error) == "error"
    print("test_status_complete_and_error: PASS")


def test_status_unknown_when_empty():
    assert storage.job_status(_row()) == "unknown"
    print("test_status_unknown_when_empty: PASS")


if __name__ == "__main__":
    test_combine_averages_and_sums()
    test_combine_handles_flat_and_total_credits_fallback()
    test_combine_missing_field_is_none()
    test_count_none_and_empty()
    test_count_counts_list_entries()
    test_count_honors_statement_count_field()
    test_count_legacy_single_object_is_one()
    test_status_waiting_for_bank_statements_below_threshold()
    test_status_processing_at_threshold()
    test_status_waiting_for_application_when_only_statements()
    test_status_complete_and_error()
    test_status_unknown_when_empty()
    print("\nALL TESTS PASSED")
