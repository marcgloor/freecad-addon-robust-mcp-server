"""Tests for the XML-RPC client bridge connection-reuse safety.

The client now keeps a single ServerProxy alive across calls (HTTP/1.1
keep-alive on the server side) and serializes access to it with an asyncio
lock, because the default executor may run blocking RPCs on multiple threads
and a shared keep-alive socket must not be used concurrently.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge


def test_bridge_has_call_lock_and_defaults():
    bridge = XmlRpcBridge()
    assert isinstance(bridge._call_lock, asyncio.Lock)
    assert bridge._host == "localhost"
    assert bridge._port == 9875


class _FakeProxy:
    """Proxy that records the maximum number of concurrent execute() calls."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def execute(self, code: str) -> dict:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)  # hold the "socket" so overlap would be detected
        self.calls += 1
        self.active -= 1
        return {"success": True, "result": code, "stdout": "", "stderr": ""}


@pytest.mark.asyncio
async def test_concurrent_calls_are_serialized():
    bridge = XmlRpcBridge()
    fake = _FakeProxy()
    bridge._proxy = fake  # type: ignore[assignment]

    results = await asyncio.gather(
        *[bridge.execute_python(f"c{i}") for i in range(10)]
    )

    assert fake.calls == 10
    assert all(r.success for r in results)
    # The lock must prevent two threads from touching the shared proxy at once.
    assert fake.max_active == 1, f"socket used concurrently: {fake.max_active}"
