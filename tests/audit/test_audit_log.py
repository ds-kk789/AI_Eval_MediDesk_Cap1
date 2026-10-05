"""Every request writes the required audit fields and no answer text (BR-13).

Drives the live API with requests that are decided before any model runs
(401, 403, the clinician record page), then reads the audit lines back by
the X-Request-ID each response carries. No model calls.

The question sent contains a unique marker; finding that marker anywhere in
the log would mean question text was stored.
"""

import json
import uuid

import pytest
import requests

from backend import audit, config


LANE = "audit"

FD = {"X-API-Key": "front-desk-demo-key"}
CL = {"X-API-Key": "clinician-demo-key"}


@pytest.fixture(scope="module")
def traffic():
    marker = f"audit-marker-{uuid.uuid4().hex[:8]}"
    calls = [
        ("POST", "/ask", None, 401),
        ("POST", "/ask", {"X-API-Key": "not-a-real-key"}, 401),
        ("POST", "/clinical/ask", FD, 403),
        ("GET", "/clinical/patients/P-1003", FD, 403),
        ("GET", "/clinical/patients/P-1003", CL, 200),
        ("GET", "/clinical/patients/P-9999", CL, 404),
    ]
    sent = []

    for method, path, headers, expected in calls:
        if method == "POST":
            response = requests.post(config.BASE_URL + path, json={"question": f"{marker} what is P-1003 allergic to"}, headers=headers or {}, timeout=60)
        else:
            response = requests.get(config.BASE_URL + path, headers=headers or {}, timeout=60)
        assert response.status_code == expected, (path, response.status_code)
        sent.append({"path": path, "status": response.status_code, "request_id": response.headers.get("X-Request-ID")})

    lines = [json.loads(line) for line in config.AUDIT_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {}
    for line in lines:
        by_id.setdefault(line["request_id"], []).append(line)

    raw = config.AUDIT_LOG.read_text(encoding="utf-8")
    return {"marker": marker, "sent": sent, "by_id": by_id, "raw": raw}


def _check(record, n, title, passed, evidence):
    record(LANE, {"id": f"AUD-{n:02d}", "title": title, "passed": bool(passed), "evidence": str(evidence)[:300], "rules": "BR-13"})
    assert passed, f"{title}: {evidence}"


def test_one_line_per_request(traffic, record):
    counts = {s["request_id"]: len(traffic["by_id"].get(s["request_id"], [])) for s in traffic["sent"]}
    _check(record, 1, "Every audited request writes exactly one line", all(c == 1 for c in counts.values()), counts)


def test_required_fields(traffic, record):
    lines = [traffic["by_id"][s["request_id"]][0] for s in traffic["sent"] if s["request_id"] in traffic["by_id"]]
    missing = [sorted(set(audit.REQUIRED_FIELDS) - set(line)) for line in lines]
    _check(record, 2, "Required fields present on every line", lines and not any(missing), missing)


def test_no_question_or_answer_text(traffic, record):
    leaked = [t for t in (traffic["marker"], "allergic", "sulfa") if t in traffic["raw"]]
    _check(record, 3, "No question or answer text in the log", not leaked, leaked or "clean")


def test_rejections_logged_as_denied(traffic, record):
    rejected = [s for s in traffic["sent"] if s["status"] in (401, 403)]
    decisions = [traffic["by_id"][s["request_id"]][0]["decision"] for s in rejected]
    _check(record, 4, "401 and 403 rejections are logged as Denied", all(d == "Denied" for d in decisions), decisions)


def test_patient_ids_not_in_endpoint(traffic, record):
    endpoints = sorted({line[0]["endpoint"] for line in traffic["by_id"].values() if line})
    bad = [e for e in endpoints if "P-" in e.upper().replace("{PATIENT_ID}", "")]
    _check(record, 5, "Patient IDs never appear in the endpoint field", not bad, bad or endpoints[:6])
