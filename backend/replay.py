"""Replay mode: answer from the responses recorded by the e2e run.

With MEDIDESK_REPLAY=1 the API never calls a model. run_agent() looks the
question up here instead, keyed by (role, question), and returns the same
shape the ADK runner would: answer, tools called and chunk refs. Everything
after that is the live code path: backend/main.py re-checks each chunk's
visibility against the caller's role, classifies the decision and writes the
audit line, so a replayed answer can't show a chunk the caller may not see.

Source: reports/_cache/*.json, written by the cached_ask fixture in
tests/conftest.py. RBAC-* entries are skipped: they were recorded before the
fix that strips gpt-oss reasoning from answers, so they would replay a bug.

A question that wasn't recorded gets a 503 rather than a made-up answer.
"""

import json

from fastapi import HTTPException

from backend import config


# test-id prefix -> chip style in the web app
KINDS = {"QUAL-": "golden", "SEC-": "attack"}
SKIPPED_PREFIXES = ("RBAC-",)

_entries = None


def _key(role, question):
    return role, " ".join(question.replace("’", "'").split()).casefold()


def _load():
    global _entries

    if _entries is not None:
        return _entries

    _entries = {}

    for path in sorted(config.REPLAY_DIR.glob("*.json")):
        if path.name.startswith(SKIPPED_PREFIXES):
            continue

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if not payload.get("role") or not payload.get("question"):
            continue

        kind = next(
            (k for prefix, k in KINDS.items() if path.name.startswith(prefix)),
            "other",
        )
        _entries[_key(payload["role"], payload["question"])] = {**payload, "kind": kind}

    return _entries


def questions(role):
    """Recorded questions for this role only, for the web app's chips."""
    return [
        {"question": entry["question"], "kind": entry["kind"]}
        for (entry_role, _), entry in _load().items()
        if entry_role == role
    ]


def run(role, question):
    """Same return shape as backend.main.run_agent, without a model call."""
    entry = _load().get(_key(role, question))

    if entry is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Replay mode: this question wasn't in the recorded run for your "
                "role. Pick a suggested question, or restart the API without "
                "MEDIDESK_REPLAY to ask the live model."
            ),
        )

    return {
        "answer": entry["answer"],
        "tools_called": list(entry.get("tools_called", [])),
        "retrieved": [
            {"id": s["id"], "score": s["score"]} for s in entry.get("sources", [])
        ],
        "bulk_blocked": False,
    }
