"""Tests for the event-driven queue processing in the MCP bridge server.

These cover the performance-oriented rewrite of the queue drain mechanism:
the GUI thread is now woken immediately when a request is enqueued (instead
of waiting up to QUEUE_POLL_INTERVAL_MS), and the headless loop blocks on the
queue instead of sleep-polling. The tests exercise the headless path because
it runs without a Qt event loop or a live FreeCAD instance.

The server module imports cleanly with FREECAD_AVAILABLE == False, so these
run in plain CI without FreeCAD.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest

# Load the bridge server module directly from the addon tree. It is not part
# of the installed `freecad_mcp` package, so we load it by path.
_SERVER_PATH = (
    Path(__file__).resolve().parents[3]
    / "freecad"
    / "RobustMCPBridge"
    / "freecad_mcp_bridge"
    / "server.py"
)
_spec = importlib.util.spec_from_file_location("_mcp_bridge_server", _SERVER_PATH)
assert _spec and _spec.loader
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


@pytest.fixture
def plugin():
    """A plugin instance with no servers started."""
    return server.FreecadMCPPlugin()


@pytest.fixture
def headless_loop(plugin):
    """Start the headless queue-processor loop and stop it after the test."""
    plugin._running = True
    t = threading.Thread(target=plugin._run_queue_processor_loop, daemon=True)
    t.start()
    try:
        yield plugin
    finally:
        plugin._running = False
        t.join(timeout=2.0)


class TestExecuteCodeSync:
    def test_basic_result_and_stdout(self, plugin):
        result = plugin._execute_code_sync('print("hi"); _result_ = 6 * 7')
        assert result["success"] is True
        assert result["result"] == 42
        assert "hi" in result["stdout"]

    def test_error_path_is_structured_not_raised(self, plugin):
        result = plugin._execute_code_sync('raise ValueError("boom")')
        assert result["success"] is False
        assert result["error_type"] == "ValueError"
        assert "boom" in result["error_message"]


class TestRunRequest:
    def test_sets_completion_and_records_on_success(self, plugin):
        before = plugin.request_count
        req = server.ExecutionRequest("_result_ = 1 + 1")
        plugin._run_request(req)
        assert req.completed.is_set()
        assert req.result["result"] == 2
        assert plugin.request_count == before + 1

    def test_never_raises_and_records_on_error(self, plugin):
        before = plugin.request_count
        req = server.ExecutionRequest('raise RuntimeError("x")')
        plugin._run_request(req)  # must not raise
        assert req.completed.is_set()
        assert req.result["success"] is False
        assert plugin.request_count == before + 1


class TestEventDrivenQueue:
    def test_round_trip_far_below_old_poll_floor(self, headless_loop):
        plugin = headless_loop
        start = time.perf_counter()
        result = plugin._execute_via_queue("_result_ = 123", timeout_ms=5000)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert result["success"] is True
        assert result["result"] == 123
        # The old design averaged ~25 ms (0-50 ms poll). Event-driven wake-up
        # is sub-millisecond; 30 ms is a very generous CI ceiling.
        assert elapsed_ms < 30, f"round trip too slow: {elapsed_ms:.1f} ms"

    def test_burst_all_processed_in_order(self, headless_loop):
        plugin = headless_loop
        results = [
            plugin._execute_via_queue(f"_result_ = {i}", timeout_ms=5000)["result"]
            for i in range(50)
        ]
        assert results == list(range(50))

    def test_timeout_returns_structured_error(self, plugin):
        # No processor running: the request is never drained, so it times out.
        plugin._running = False
        result = plugin._execute_via_queue("_result_ = 1", timeout_ms=50)
        assert result["success"] is False
        assert result["error_type"] == "TimeoutError"


class TestConstants:
    def test_poll_interval_is_now_a_safety_net(self):
        # The fixed-poll interval is no longer the hot path; it should be a
        # large safety-net value rather than the old 50 ms.
        assert server.QUEUE_POLL_INTERVAL_MS >= 100
