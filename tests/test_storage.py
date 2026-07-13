"""
Integration tests for storage.py — run against a real Postgres database
(no mocking: the thing being verified is SQL/psycopg correctness, including
the atomic append of bank statements into a jsonb array).

Run: python tests/test_storage.py
Requires DATABASE_URL to be set (env var or .streamlit/secrets.toml).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import storage

TEST_CODE = "STORAGE_TEST_job1"


def cleanup():
    storage.delete_job(TEST_CODE)


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


def test_append_bs_accumulates_into_array():
    cleanup()
    storage.append_bs(TEST_CODE, {"summary_metrics": {"total_revenue": 100}})
    storage.append_bs(TEST_CODE, {"summary_metrics": {"total_revenue": 200}})
    storage.append_bs(TEST_CODE, {"summary_metrics": {"total_revenue": 300}})
    job = storage.get_job(TEST_CODE)
    assert isinstance(job["bs_json"], list), f"bs_json must be a list, got {type(job['bs_json'])}"
    assert len(job["bs_json"]) == 3, job["bs_json"]
    assert job["bs_json"][0]["summary_metrics"]["total_revenue"] == 100
    assert job["bs_json"][2]["summary_metrics"]["total_revenue"] == 300
    assert storage.count_statements(job["bs_json"]) == 3
    print("test_append_bs_accumulates_into_array: PASS")


def test_load_statements_roundtrip():
    cleanup()
    assert storage.load_statements(TEST_CODE) == []
    storage.append_bs(TEST_CODE, {"a": 1})
    storage.append_bs(TEST_CODE, {"a": 2})
    stmts = storage.load_statements(TEST_CODE)
    assert [s["a"] for s in stmts] == [1, 2], stmts
    print("test_load_statements_roundtrip: PASS")


def test_append_bs_does_not_clear_app_json():
    cleanup()
    storage.upsert_app(TEST_CODE, {"foo": "bar"})
    storage.append_bs(TEST_CODE, {"baz": "qux"})
    job = storage.get_job(TEST_CODE)
    assert job["app_json"] == {"foo": "bar"}, "append_bs must not touch app_json"
    assert job["bs_json"] == [{"baz": "qux"}]
    print("test_append_bs_does_not_clear_app_json: PASS")


def test_get_job_returns_none_for_missing_code():
    cleanup()
    assert storage.get_job("NO_SUCH_CODE_xyz") is None
    print("test_get_job_returns_none_for_missing_code: PASS")


def test_set_result_and_job_status_complete():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    for _ in range(3):
        storage.append_bs(TEST_CODE, {"b": 2})
    storage.set_result(TEST_CODE, {"qualifying_lenders": []})
    job = storage.get_job(TEST_CODE)
    assert job["result_json"] == {"qualifying_lenders": []}
    assert storage.job_status(job) == "complete"
    print("test_set_result_and_job_status_complete: PASS")


def test_set_error_and_job_status_error():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    storage.append_bs(TEST_CODE, {"b": 2})
    storage.set_error(TEST_CODE, {"error": "boom"})
    job = storage.get_job(TEST_CODE)
    assert job["error_json"] == {"error": "boom"}
    assert storage.job_status(job) == "error"
    print("test_set_error_and_job_status_error: PASS")


def test_job_status_min3_gate():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    assert storage.job_status(storage.get_job(TEST_CODE)) == "waiting_for_bank_statements"
    storage.append_bs(TEST_CODE, {"b": 1})
    storage.append_bs(TEST_CODE, {"b": 2})
    # 2 statements — still waiting, below the min-3 threshold
    assert storage.job_status(storage.get_job(TEST_CODE)) == "waiting_for_bank_statements"
    storage.append_bs(TEST_CODE, {"b": 3})
    # 3 statements + app — now processing
    assert storage.job_status(storage.get_job(TEST_CODE)) == "processing"
    print("test_job_status_min3_gate: PASS")


def test_job_status_waiting_for_application():
    cleanup()
    for _ in range(3):
        storage.append_bs(TEST_CODE, {"b": 2})
    assert storage.job_status(storage.get_job(TEST_CODE)) == "waiting_for_application"
    print("test_job_status_waiting_for_application: PASS")


def test_reupsert_app_clears_result_keeps_statements():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    for _ in range(3):
        storage.append_bs(TEST_CODE, {"b": 2})
    storage.set_result(TEST_CODE, {"qualifying_lenders": []})
    storage.upsert_app(TEST_CODE, {"a": 2})  # resubmission
    job = storage.get_job(TEST_CODE)
    assert job["result_json"] is None, "resubmitting the application must clear result_json"
    # statements are preserved across an application resubmission
    assert storage.count_statements(job["bs_json"]) == 3
    print("test_reupsert_app_clears_result_keeps_statements: PASS")


def test_delete_job_rowcount_semantics():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    assert storage.delete_job(TEST_CODE) is True
    assert storage.delete_job(TEST_CODE) is False, "deleting an already-gone job returns False"
    print("test_delete_job_rowcount_semantics: PASS")


def test_orphaned_jobs_requires_min3():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    storage.append_bs(TEST_CODE, {"b": 1})
    storage.append_bs(TEST_CODE, {"b": 2})
    # 2 statements — NOT orphaned yet (below min-3)
    assert TEST_CODE not in [j["client_code"] for j in storage.orphaned_jobs()]
    storage.append_bs(TEST_CODE, {"b": 3})
    # 3 statements + app, no result — now orphaned
    assert TEST_CODE in [j["client_code"] for j in storage.orphaned_jobs()]
    storage.set_result(TEST_CODE, {"qualifying_lenders": []})
    assert TEST_CODE not in [j["client_code"] for j in storage.orphaned_jobs()]
    print("test_orphaned_jobs_requires_min3: PASS")


def test_list_jobs_includes_code():
    cleanup()
    storage.upsert_app(TEST_CODE, {"a": 1})
    assert TEST_CODE in [j["client_code"] for j in storage.list_jobs()]
    print("test_list_jobs_includes_code: PASS")


if __name__ == "__main__":
    try:
        test_init_db_is_idempotent()
        test_upsert_app_creates_row()
        test_append_bs_accumulates_into_array()
        test_load_statements_roundtrip()
        test_append_bs_does_not_clear_app_json()
        test_get_job_returns_none_for_missing_code()
        test_set_result_and_job_status_complete()
        test_set_error_and_job_status_error()
        test_job_status_min3_gate()
        test_job_status_waiting_for_application()
        test_reupsert_app_clears_result_keeps_statements()
        test_delete_job_rowcount_semantics()
        test_orphaned_jobs_requires_min3()
        test_list_jobs_includes_code()
    finally:
        cleanup()
    print("\nALL TESTS PASSED")
