"""Shared pytest fixtures: the live API under test, and report generation.

The RBAC and security lanes exercise the real HTTP surface, so a uvicorn
process is started once per session and torn down at the end. If a server is
already listening on the configured port it is reused, which keeps the
grader's "run the server, then run the tests" flow working too.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402


REPORTS_DIR = config.REPORTS_DIR
CACHE_DIR = REPORTS_DIR / "_cache"


# ------------------------------------------------------------
# LIVE API
# ------------------------------------------------------------


# A health check that only asks "did something answer on this port" will
# happily grade a completely different application. That is not theoretical:
# the Module-3 e-commerce API listens on this same port, and against it the
# security lane passes while proving nothing. The marker below identifies the
# MediDesk app specifically.
APP_MARKER = "MediDesk"


def _probe():
    """Return (reachable, is_our_app, description)."""
    try:
        response = requests.get(config.BASE_URL + "/", timeout=2.0)
    except requests.RequestException:
        return False, False, "no response"

    if response.status_code != 200:
        return True, False, f"HTTP {response.status_code}"

    try:
        body = response.json()
    except ValueError:
        return True, False, "non-JSON response"

    message = str(body.get("message", ""))

    if body.get("mode") == "replay":
        # Our app, but /ask replays recorded answers (run_web.py): grading
        # it would only re-grade the recording.
        return True, False, "MediDesk in replay mode (python run_web.py)"

    return True, APP_MARKER in message, message or "unknown app"


def _server_is_up(timeout=1.0):
    reachable, is_ours, _ = _probe()
    return reachable and is_ours


@pytest.fixture(scope="session", autouse=True)
def live_api():
    """Ensure the FastAPI app is reachable for the whole session."""
    reachable, is_ours, description = _probe()

    if reachable and is_ours:
        # A MediDesk app is already serving this port; reuse it.
        yield config.BASE_URL
        return

    if reachable and not is_ours:
        pytest.fail(
            f"Port {config.PORT} is occupied by an application the tests can't grade "
            f"({description!r}). Refusing to run: tests against the wrong "
            "app would report a meaningless pass. Stop that process, or set "
            "APP_PORT to a free port, then re-run."
        )

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            config.HOST,
            "--port",
            str(config.PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(config.PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    deadline = time.time() + 180

    while time.time() < deadline:
        if _server_is_up():
            break

        if process.poll() is not None:
            output = process.stdout.read().decode("utf-8", "replace")
            pytest.fail(
                "uvicorn exited before becoming healthy:\n" + output
            )

        time.sleep(1.0)
    else:
        process.terminate()
        pytest.fail(
            f"API did not become healthy at {config.BASE_URL} within 180s"
        )

    yield config.BASE_URL

    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


# ------------------------------------------------------------
# API CLIENT
# ------------------------------------------------------------


def _key_for(role):
    if role == "clinician":
        return os.getenv("CLINICIAN_API_KEY", "clinician-demo-key")
    return os.getenv("FRONT_DESK_API_KEY", "front-desk-demo-key")


@pytest.fixture(scope="session")
def ask():
    """Post a question to /ask as a given role and return the JSON body."""

    # Free-tier keys enforce a per-minute rate limit on top of the daily
    # quota. A burst of parametrised cases fired back-to-back trips it, and
    # the API surfaces that as a 5xx. Retry with a pause rather than
    # reporting a transient limit as a security failure.
    retryable = {429, 500, 502, 503, 504}

    # Minimum spacing between live model calls. Groq's free tier allows
    # 8,000 tokens per MINUTE (x-ratelimit-limit-tokens); one RAG prompt with
    # five chunks plus reasoning output is ~3k, so back-to-back calls trip it
    # and the SDK's retry backoff then looks like a hang. 30s keeps two calls
    # per minute, which fits.
    pace_seconds = float(os.getenv("LLM_PACE_SECONDS", "30"))
    last_call = {"at": 0.0}

    def _ask(question, role="front_desk", path="/ask", attempts=4):
        last = None

        for attempt in range(attempts):
            wait = pace_seconds - (time.time() - last_call["at"])
            if wait > 0:
                time.sleep(wait)
            last_call["at"] = time.time()

            response = requests.post(
                config.BASE_URL + path,
                json={"question": question},
                headers={"X-API-Key": _key_for(role)},
                # gpt-oss can deliberate for minutes on adversarial prompts
                # before refusing; a short timeout misreports that as a
                # transport failure.
                timeout=360,
            )

            if response.status_code not in retryable:
                response.raise_for_status()
                return response.json()

            last = response
            time.sleep(20 * (attempt + 1))

        raise RuntimeError(
            f"{path} still failing after {attempts} attempts: "
            f"HTTP {last.status_code} {last.text[:500]}"
        )

    return _ask


@pytest.fixture(scope="session")
def cached_ask(ask):
    """Same as `ask`, but memoised to disk by test id.

    Keeps a grading run reproducible and avoids re-spending Groq tokens when
    a single lane is re-run. Delete reports/_cache to force fresh calls.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cached_ask(test_id, question, role="front_desk", path="/ask"):
        cache_file = CACHE_DIR / f"{test_id}.json"

        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        payload = ask(question, role=role, path=path)

        cache_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return payload

    return _cached_ask


# ------------------------------------------------------------
# REPORT RECORDING
# ------------------------------------------------------------

# lane name -> list of result dicts
_RESULTS = {}


@pytest.fixture(scope="session")
def record():
    """Record one test's outcome into a lane report."""

    def _record(lane, entry):
        _RESULTS.setdefault(lane, []).append(entry)

    return _record


def pytest_sessionfinish(session, exitstatus):
    """Write one JSON report per lane that recorded anything."""
    if not _RESULTS:
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for lane, entries in _RESULTS.items():
        failures = [
            entry for entry in entries if not entry.get("passed", False)
        ]

        report = {
            "lane": lane,
            "total_cases": len(entries),
            "passed": len(entries) - len(failures),
            "failed": len(failures),
            "failure_rate": (
                round(len(failures) / len(entries), 4) if entries else 0.0
            ),
            "detailed_logs": entries,
        }

        # Lanes whose entries carry a `scores` dict (the quality lane) also
        # get per-metric means, which is what the >= 0.95 gate is applied to.
        metric_names = sorted(
            {
                name
                for entry in entries
                for name in entry.get("scores", {})
            }
        )

        if metric_names:
            means = {}

            for name in metric_names:
                values = [
                    entry["scores"][name]
                    for entry in entries
                    if entry.get("scores", {}).get(name) is not None
                ]

                means[name] = (
                    round(sum(values) / len(values), 4) if values else None
                )

            report["metric_means"] = means

        (REPORTS_DIR / f"{lane}_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
