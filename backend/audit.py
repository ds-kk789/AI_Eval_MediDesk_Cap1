"""One JSON line per request: who asked, what was retrieved, allowed or denied.

Written by the audit middleware in backend/main.py for every request to an
audited route, including the 401 and 403 rejections that never reach a
handler (BR-13). Question and answer text are never stored: the log must be
safe to hand to an auditor who is not cleared for patient data.

Line shape:

    {"timestamp": "2026-10-05T09:14:03.120Z", "request_id": "…",
     "role": "front_desk", "method": "POST", "endpoint": "/ask",
     "status_code": 200, "decision": "Allowed",
     "tools_called": ["retrieve_clinic_policy"],
     "chunk_ids": ["public/opening-hours-01"], "latency_ms": 812}
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from backend import config


REQUIRED_FIELDS = (
    "timestamp",
    "request_id",
    "role",
    "method",
    "endpoint",
    "status_code",
    "decision",
    "tools_called",
    "chunk_ids",
    "latency_ms",
)

_lock = threading.Lock()


def log_request(
    *,
    request_id,
    role,
    method,
    endpoint,
    status_code,
    decision,
    tools_called=(),
    chunk_ids=(),
    latency_ms=0,
    log_path=None,
):
    """Append one audit line and return it."""
    entry = {
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "request_id": request_id,
        "role": role,
        "method": method,
        "endpoint": endpoint,
        "status_code": status_code,
        "decision": decision,
        "tools_called": list(tools_called),
        "chunk_ids": list(chunk_ids),
        "latency_ms": int(latency_ms),
    }

    path = Path(log_path or config.AUDIT_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(entry, ensure_ascii=False)

    with _lock, path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    return entry


def read_entries(limit=100, roles=None, decision=None, log_path=None):
    """Most recent entries first, optionally filtered by role and decision."""
    path = Path(log_path or config.AUDIT_LOG)

    if not path.exists():
        return []

    with _lock:
        lines = path.read_text(encoding="utf-8").splitlines()

    entries = []

    for line in reversed(lines):
        if not line.strip():
            continue

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if roles is not None and entry.get("role") not in roles:
            continue

        if decision and entry.get("decision") != decision:
            continue

        entries.append(entry)

        if len(entries) >= limit:
            break

    return entries
