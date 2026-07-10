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


def test_job_status_waiting_for_application():
    cleanup()
    storage.upsert_bs(TEST_CODE, {"b": 2})
    assert storage.job_status(storage.get_job(TEST_CODE)) == "waiting_for_application"
    print("test_job_status_waiting_for_application: PASS")


def test_job_status_unknown():
    assert storage.job_status({}) == "unknown"
    print("test_job_status_unknown: PASS")


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


if __name__ == "__main__":
    try:
        test_init_db_is_idempotent()
        test_upsert_app_creates_row()
        test_upsert_bs_does_not_clear_app_json()
        test_get_job_returns_none_for_missing_code()
        test_set_result_and_job_status_complete()
        test_set_error_and_job_status_error()
        test_job_status_transitions()
        test_job_status_waiting_for_application()
        test_job_status_unknown()
        test_reupsert_app_clears_result()
        test_delete_job_rowcount_semantics()
        test_list_jobs_and_orphaned_jobs()
    finally:
        cleanup()
    print("\nALL TESTS PASSED")
