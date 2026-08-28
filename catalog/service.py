"""Agent-facing capability catalog: a thin HTTP surface over saved artifacts.

interface.ai's product is AI agents invoking capabilities on demand -- this
module exposes every artifact discovery has already produced as one of
those callable capabilities, with a typed contract an agent's function-
calling layer can ingest directly, and one endpoint that actually runs it.

Deliberately NOT new execution logic: GET /capabilities and
GET /capabilities/{name} only read Artifact.parameters/outputs (never
`steps` -- that's the whole point, an agent decides what to call without
reading the raw step list) and reshape them into a JSON-Schema-shaped
{"type": "object", "properties": {...}, "required": [...]} contract, the
same convention agent/tools.py already uses for its own LLM tool schemas.
POST /capabilities/{name}/invoke does nothing but validate the request
shape and hand off to ReplayEngine -- the exact same GuardrailChecker,
Artifact schema, and replay control flow replay.py's CLI uses, reused as-is,
not reimplemented. The HTTP surface never exposes a way to loosen a
guardrail tier: `allow_confirmed_actions` is never taken from the request,
so invoking a capability is never a way around what --allow-confirmed-actions
gates on the CLI.

Every invoke also appends a run-log record (see catalog/run_log.py) as a
side effect, for the eval dashboard (dashboard/app.py) to read -- purely
additive: a run-log write failure is swallowed, never surfaced as an
invoke error, and the response returned to the caller is unchanged.
"""

from __future__ import annotations

import glob
import multiprocessing
import os
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, HTTPException
from playwright.sync_api import sync_playwright

from agent.artifact import Artifact
from agent.discovery_agent import DEFAULT_ALLOWED_ACTIONS
from agent.guardrails import GuardrailChecker
from agent.redaction import redact_mapping
from replay.replay_engine import ReplayEngine

from .run_log import DEFAULT_RUN_LOG_PATH, append_run

DEFAULT_ARTIFACTS_DIR = "artifacts"


# -- artifact loading / contract shaping --------------------------------------


def _capability_name(artifact_path: str) -> str:
    """The catalog's capability name: the artifact's own filename stem.

    ArtifactWriter already derives that filename from a slugified goal plus
    a unique epoch timestamp (see agent/artifact_writer.py), so it's already
    a clean, URL-safe, and -- critically -- unique identifier: several saved
    artifacts commonly share the same goal (repeat discovery runs), which a
    bare slugified-goal name would collide on.
    """
    return os.path.splitext(os.path.basename(artifact_path))[0]


def _load_artifacts(artifacts_dir: str) -> dict[str, Artifact]:
    """Scan `artifacts_dir` fresh on every call -- new discovery runs add new
    files, and there's no other cache anywhere else in this project to keep
    in sync, so re-reading the directory is the correct source of truth.
    A single malformed artifact file is skipped, not fatal to the catalog.
    """
    artifacts: dict[str, Artifact] = {}
    for path in sorted(glob.glob(os.path.join(artifacts_dir, "*.json"))):
        try:
            artifacts[_capability_name(path)] = Artifact.from_json_file(path)
        except Exception:  # noqa: BLE001 -- one bad file must not break the whole catalog
            continue
    return artifacts


def _build_contract(name: str, artifact: Artifact) -> dict[str, Any]:
    """Shape one Artifact into an agent-consumable capability contract.

    The parameters/outputs sections deliberately mirror the JSON-Schema
    {"type": "object", "properties": {...}, "required": [...]} convention
    agent/tools.py already uses for Gemini tool declarations -- the same
    shape an LLM function-calling layer expects, not a bespoke format.
    Every declared parameter is required: replay's {{param}} substitution
    has no notion of an optional/defaulted input (see
    replay/replay_engine.py's resolve_placeholders).
    """
    properties = {
        p.name: {"type": p.type, "description": p.description, "example_value": p.example_value}
        for p in artifact.parameters
    }
    output_properties = {o.name: {"type": o.type, "description": o.description} for o in artifact.outputs}

    return {
        "name": name,
        "description": artifact.metadata.goal,
        "target_url": artifact.metadata.target_url,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": [p.name for p in artifact.parameters],
        },
        "outputs": {
            "type": "object",
            "properties": output_properties,
        },
    }


# -- the replay/browser seam tests mock ---------------------------------------


REPLAY_SUBPROCESS_TIMEOUT_S = 180


def _run_replay_worker(artifact: Artifact, params: dict[str, Any], result_queue: "multiprocessing.Queue") -> None:
    """Runs in a separate OS process (see _run_replay below) -- same
    GuardrailChecker construction, same DEFAULT_ALLOWED_ACTIONS, same
    artifact.scope_selector default as replay.py's CLI. Nothing in the
    request body can influence `allow_confirmed_actions`: it is never read
    from `params`, so a capability tagged requires_confirmation or blocked
    is refused here exactly as it would be replaying from the CLI without
    --allow-confirmed-actions.

    Reports back over result_queue instead of returning/raising directly --
    a raised exception doesn't reliably cross a process boundary, so any
    failure here becomes a (False, message) tuple instead.
    """
    try:
        target_host = urlparse(artifact.metadata.target_url).hostname
        guardrail = GuardrailChecker(allowed_domains=[target_host], allowed_actions=DEFAULT_ALLOWED_ACTIONS)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(artifact.metadata.target_url)
                engine = ReplayEngine(page=page, guardrail=guardrail, root_selector=artifact.scope_selector)
                result = engine.run(artifact, params)
            finally:
                browser.close()
        result_queue.put((True, result))
    except Exception as e:  # noqa: BLE001 -- must always report back, never leave the parent hanging
        result_queue.put((False, f"{type(e).__name__}: {e}"))


def _run_replay(artifact: Artifact, params: dict[str, Any]) -> dict[str, Any]:
    """The real runner: drives ReplayEngine over a real Playwright browser.

    Runs _run_replay_worker in a separate OS process (multiprocessing,
    "spawn" -- the only start method Windows supports, and the safest
    cross-platform choice) rather than calling sync Playwright directly in
    this thread. FastAPI runs a sync endpoint like invoke_capability in a
    worker thread of the *same process* that's running uvicorn's own asyncio
    event loop, and Playwright's sync API refuses to operate in a process
    that has any running event loop at all -- a fresh child process's main
    thread has none, so this sidesteps that restriction entirely instead of
    working around it in-process.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = ctx.Queue()
    process = ctx.Process(target=_run_replay_worker, args=(artifact, params, result_queue))
    process.start()
    process.join(REPLAY_SUBPROCESS_TIMEOUT_S)

    if process.is_alive():
        process.terminate()
        process.join()
        return {
            "status": "hard_failure",
            "step": 0,
            "expected": f"replay completes within {REPLAY_SUBPROCESS_TIMEOUT_S}s",
            "observed": "replay subprocess was still running and was terminated",
            "error": "replay subprocess timed out",
        }

    if result_queue.empty():
        return {
            "status": "hard_failure",
            "step": 0,
            "expected": "replay subprocess reports a result",
            "observed": f"subprocess exited with code {process.exitcode} before reporting a result",
            "error": "replay subprocess crashed before reporting a result",
        }

    ok, payload = result_queue.get()
    if not ok:
        return {
            "status": "hard_failure",
            "step": 0,
            "expected": "replay completes without an unhandled exception",
            "observed": "exception in replay subprocess",
            "error": payload,
        }
    return payload


ReplayRunner = Callable[[Artifact, dict[str, Any]], dict[str, Any]]


def get_replay_runner() -> ReplayRunner:
    """FastAPI dependency seam: tests override this (app.dependency_overrides)
    to swap in a fake that returns a canned result, so /invoke is testable
    with no live browser -- exactly the "mock the replay/browser layer"
    the brief asks for, without touching ReplayEngine/GuardrailChecker
    themselves.
    """
    return _run_replay


# -- app -----------------------------------------------------------------------


def create_app(artifacts_dir: str = DEFAULT_ARTIFACTS_DIR, run_log_path: str = DEFAULT_RUN_LOG_PATH) -> FastAPI:
    app = FastAPI(
        title="interface.ai capability catalog",
        description="Agent-facing catalog of saved replay artifacts, callable by name.",
    )

    @app.get("/capabilities")
    def list_capabilities() -> dict[str, Any]:
        artifacts = _load_artifacts(artifacts_dir)
        return {"capabilities": [_build_contract(name, artifact) for name, artifact in sorted(artifacts.items())]}

    @app.get("/capabilities/{name}")
    def get_capability(name: str) -> dict[str, Any]:
        artifact = _load_artifacts(artifacts_dir).get(name)
        if artifact is None:
            raise HTTPException(status_code=404, detail=f"no capability named {name!r}")
        return _build_contract(name, artifact)

    @app.post("/capabilities/{name}/invoke")
    def invoke_capability(
        name: str,
        params: dict[str, Any] = Body(default_factory=dict),
        replay_runner: ReplayRunner = Depends(get_replay_runner),
    ) -> dict[str, Any]:
        artifact = _load_artifacts(artifacts_dir).get(name)
        if artifact is None:
            raise HTTPException(status_code=404, detail=f"no capability named {name!r}")

        # Fail fast on a missing required parameter -- before ever launching a browser.
        # ReplayEngine would itself turn this into a hard_failure once it hit the first
        # unresolved {{param}} (see resolve_placeholders' ParameterError), but there's no
        # reason to pay for a Playwright launch just to discover that.
        missing = [p.name for p in artifact.parameters if p.name not in params]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"missing required parameter(s): {', '.join(missing)}",
            )

        result = replay_runner(artifact, params)

        # Side effect only -- never allowed to change the response or fail the request.
        # A full disk or a bad run_log_path must not turn a successful invoke into an error.
        try:
            append_run(run_log_path, name, params, result)
        except OSError:
            pass

        # Same redaction contract as replay.py's own console print: never redact `outputs`,
        # the artifact's own declared, deliberately-returned data.
        return redact_mapping(result, protected_keys=frozenset({"outputs"}))

    return app


app = create_app(
    os.environ.get("CATALOG_ARTIFACTS_DIR", DEFAULT_ARTIFACTS_DIR),
    os.environ.get("CATALOG_RUN_LOG_PATH", DEFAULT_RUN_LOG_PATH),
)
