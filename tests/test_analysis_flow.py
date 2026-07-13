"""
Tests for the per-client analysis serialization in app.py: _maybe_launch_analysis
runs analysis once, re-runs exactly once for statements that arrived mid-run,
and always clears the running flag even if analysis raises.

storage is fully mocked here (get_job / load_statements / run_analysis), so
these tests need no database.

Run: python tests/test_analysis_flow.py
"""
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import app

TEST_CODE = "ANALYSIS_FLOW_TEST_job1"


def _reset_state():
    app._analysis_state.pop(TEST_CODE, None)


def test_maybe_launch_runs_once_and_reruns_when_pending():
    """A statement that arrives while a run is in progress triggers exactly one
    rerun — not a second concurrent thread."""
    _reset_state()
    calls = []
    started = threading.Event()
    release = threading.Event()

    # storage.load_statements grows from 3 -> 4 between the two runs
    statement_counts = iter([
        [{"summary_metrics": {}}] * 3,
        [{"summary_metrics": {}}] * 4,
    ])

    def fake_load_statements(client_id):
        return next(statement_counts)

    def fake_run_analysis(client_id, app_raw, statements):
        calls.append(len(statements))
        started.set()
        release.wait(timeout=5)  # hold run #1 open so we can queue a rerun

    with patch.object(app.storage, "get_job", return_value={"app_json": {"x": 1}}), \
         patch.object(app.storage, "load_statements", side_effect=fake_load_statements), \
         patch.object(app, "run_analysis", side_effect=fake_run_analysis):
        with app._client_lock(TEST_CODE):
            app._maybe_launch_analysis(TEST_CODE)   # launches run #1
        started.wait(timeout=5)
        # A 4th statement arrives while run #1 is still "in progress"
        with app._client_lock(TEST_CODE):
            app._maybe_launch_analysis(TEST_CODE)   # should set pending, NOT start a 2nd thread
        release.set()
        time.sleep(0.5)  # let run #1 finish and the pending rerun fire

    assert len(calls) == 2, f"expected exactly 2 runs (initial + 1 rerun), got {len(calls)}"
    assert calls[0] == 3 and calls[1] == 4, calls  # rerun sees the 4th statement
    print("test_maybe_launch_runs_once_and_reruns_when_pending: PASS")


def test_analysis_loop_clears_running_on_exception():
    """If run_analysis raises, the loop must still reset running=False so the
    client is not permanently wedged."""
    _reset_state()
    done = threading.Event()

    def boom(client_id, app_raw, statements):
        try:
            raise RuntimeError("kaboom")
        finally:
            done.set()

    with patch.object(app.storage, "get_job", return_value={"app_json": {"x": 1}}), \
         patch.object(app.storage, "load_statements", return_value=[{"summary_metrics": {}}] * 3), \
         patch.object(app, "run_analysis", side_effect=boom):
        with app._client_lock(TEST_CODE):
            app._maybe_launch_analysis(TEST_CODE)
        done.wait(timeout=5)
        time.sleep(0.3)  # let the loop's state-transition run

    assert app._analysis_state[TEST_CODE]["running"] is False, app._analysis_state[TEST_CODE]
    assert app._analysis_state[TEST_CODE]["pending"] is False, app._analysis_state[TEST_CODE]
    print("test_analysis_loop_clears_running_on_exception: PASS")


if __name__ == "__main__":
    test_maybe_launch_runs_once_and_reruns_when_pending()
    test_analysis_loop_clears_running_on_exception()
    print("\nALL TESTS PASSED")
