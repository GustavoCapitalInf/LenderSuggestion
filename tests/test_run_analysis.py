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
    # Create the job row first (as would happen via upsert_app/append_bs in production)
    storage.upsert_app(TEST_CODE, {"Business_Legal_Name": "Test Co"})
    statements = [
        {"summary_metrics": {"total_revenue": 30000, "nsf_count": 1}},
        {"summary_metrics": {"total_revenue": 40000, "nsf_count": 0}},
        {"summary_metrics": {"total_revenue": 50000, "nsf_count": 2}},
    ]
    with patch.object(app, "genai") as mock_genai, patch.object(app, "_post_webhook") as mock_webhook:
        mock_genai.Client.return_value.models.generate_content.return_value = fake_response
        mock_webhook.return_value = 200
        app.run_analysis(TEST_CODE, {"Business_Legal_Name": "Test Co"}, statements)

    job = storage.get_job(TEST_CODE)
    assert job is not None, "run_analysis must create/update a row via storage"
    assert job["result_json"] is not None
    assert job["result_json"]["clientCode"] == TEST_CODE
    assert job["result_json"]["status"] == "complete"
    # Combined revenue is the AVERAGE across the 3 statements (40000), not the sum
    assert job["result_json"]["bank_statement_metrics"]["total_revenue"] == 40000.0, \
        job["result_json"]["bank_statement_metrics"]
    assert job["error_json"] is None
    print("test_run_analysis_writes_result_via_storage: PASS")


def test_run_analysis_writes_error_via_storage_on_failure():
    cleanup()
    # Create the job row first (as would happen via upsert_app/append_bs in production)
    storage.upsert_app(TEST_CODE, {"Business_Legal_Name": "Test Co"})
    with patch.object(app, "genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.side_effect = RuntimeError("boom")
        app.run_analysis(TEST_CODE, {"Business_Legal_Name": "Test Co"},
                         [{"summary_metrics": {"total_revenue": 1000}}])

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
