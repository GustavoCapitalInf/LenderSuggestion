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
