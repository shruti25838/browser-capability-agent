"""Tests for catalog/service.py's process-isolated replay runner.

Regression coverage for: sync Playwright crashing when invoked from a
FastAPI/uvicorn request thread because a running asyncio event loop exists
somewhere in the process. _run_replay now runs the actual Playwright call in
a separate OS process specifically to avoid that.

Two fast unit tests exercise _run_replay_worker's result-reporting directly
(sync_playwright/ReplayEngine faked, no real browser, no multiprocessing).
One slower test reproduces the real crash condition end-to-end: a real
asyncio event loop running in a thread, calling _run_replay via
run_in_executor exactly as Starlette's run_in_threadpool does, against a
real (offline, data: URL) page -- proving the fix actually works, not just
that the code compiles.
"""

from __future__ import annotations

import asyncio
import http.server
import multiprocessing
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import catalog.service as service
from agent.artifact import Artifact, Condition, ConditionKind, Locator, LocatorKind, Metadata


def _minimal_artifact(target_url: str) -> Artifact:
    return Artifact(
        metadata=Metadata(goal="trivial", target_url=target_url, created_at=datetime.now(timezone.utc)),
        parameters=[],
        outputs=[],
        steps=[],
        success_condition=Condition(
            kind=ConditionKind.ELEMENT_VISIBLE,
            description="heading visible",
            locator=Locator(kind=LocatorKind.ROLE, role="heading", name="Hi"),
        ),
    )


# -- _run_replay_worker: fast, no real browser --------------------------------


class _FakePage:
    def goto(self, url):
        pass


class _FakeBrowser:
    def new_page(self):
        return _FakePage()

    def close(self):
        pass


class _FakePlaywrightContext:
    def __enter__(self):
        return SimpleNamespace(chromium=SimpleNamespace(launch=lambda headless=True: _FakeBrowser()))

    def __exit__(self, *exc):
        return False


class _FakeReplayEngine:
    def __init__(self, page, guardrail, root_selector=None):
        pass

    def run(self, artifact, params):
        return {"status": "success", "outputs": {"x": "1"}}


def test_run_replay_worker_puts_success_result_on_queue(monkeypatch):
    monkeypatch.setattr(service, "sync_playwright", lambda: _FakePlaywrightContext())
    monkeypatch.setattr(service, "ReplayEngine", _FakeReplayEngine)
    artifact = _minimal_artifact("https://example.com")
    q = multiprocessing.Queue()

    service._run_replay_worker(artifact, {}, q)

    ok, payload = q.get(timeout=5)
    assert ok is True
    assert payload == {"status": "success", "outputs": {"x": "1"}}


def test_run_replay_worker_puts_failure_tuple_on_exception(monkeypatch):
    def boom():
        raise RuntimeError("playwright exploded")

    monkeypatch.setattr(service, "sync_playwright", boom)
    artifact = _minimal_artifact("https://example.com")
    q = multiprocessing.Queue()

    service._run_replay_worker(artifact, {}, q)

    ok, payload = q.get(timeout=5)
    assert ok is False
    assert "playwright exploded" in payload


# -- _run_replay: real regression test, reproduces the actual crash condition --


class _StaticPageHandler(http.server.BaseHTTPRequestHandler):
    _BODY = b"<html><body><h1>Hi</h1></body></html>"

    def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler's naming convention
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self._BODY)))
        self.end_headers()
        self.wfile.write(self._BODY)

    def log_message(self, format, *args):  # noqa: A002 -- silence request logging in test output
        pass


@pytest.fixture
def local_page_url():
    """A tiny loopback-only HTTP server -- gives _run_replay a real hostname
    to guardrail-check (unlike a data: URL, which has none), with zero
    external network access.
    """
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _StaticPageHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()
    thread.join(timeout=5)


def test_run_replay_works_when_invoked_from_a_thread_with_a_running_event_loop(local_page_url):
    """This is exactly the call shape that crashed before the fix: a sync
    callable invoked from a worker thread via run_in_executor while an
    asyncio event loop is alive and running concurrently in that same
    thread/process -- precisely what Starlette's run_in_threadpool does for
    a sync FastAPI endpoint. Uses a real (loopback-only) page -- a real
    Chromium launch, but no live external network or LLM call.
    """
    artifact = _minimal_artifact(local_page_url)
    result_holder: dict = {}

    def run_loop_and_call_replay():
        async def runner():
            loop = asyncio.get_running_loop()
            result_holder["result"] = await loop.run_in_executor(None, service._run_replay, artifact, {})

        asyncio.run(runner())

    thread = threading.Thread(target=run_loop_and_call_replay)
    thread.start()
    thread.join(timeout=60)

    assert "result" in result_holder, "replay call did not complete within the timeout"
    result = result_holder["result"]
    assert result["status"] == "success", result
