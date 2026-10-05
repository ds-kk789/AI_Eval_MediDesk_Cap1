"""Status codes for every API key type × route (BR-1 to BR-3).

Ported from AI_Eval_Module_4_3_Steps/tests/rbac/test_rbac.py, widened to the
full matrix: front desk, clinician, an invalid key and no key, against each
route. 401 and 403 are decided before any model runs, so 13 of the 16 cells
cost nothing. The three 200 cells on /ask and /clinical/ask run the agent
(cached under reports/_cache); they are skipped when no GROQ_API_KEY is set,
so the offline CI job still covers every rejection.

Retrieval containment, the check that actually protects the data, is in
test_retrieval_boundary.py.
"""

import os

import pytest
import requests

from backend import config


LANE = "rbac"

KEYS = {
    "front_desk": os.getenv("FRONT_DESK_API_KEY", "front-desk-demo-key"),
    "clinician": os.getenv("CLINICIAN_API_KEY", "clinician-demo-key"),
    "invalid key": "not-a-real-key",
    "missing key": None,
}

# (method, route template, concrete path)
ROUTES = [
    ("POST", "/ask", "/ask"),
    ("POST", "/clinical/ask", "/clinical/ask"),
    ("GET", "/clinical/patients/{patient_id}", "/clinical/patients/P-1003"),
    ("GET", "/audit", "/audit"),
]

EXPECTED = {
    "front_desk": {"/ask": 200, "/clinical/ask": 403, "/clinical/patients/{patient_id}": 403, "/audit": 200},
    "clinician": {"/ask": 200, "/clinical/ask": 200, "/clinical/patients/{patient_id}": 200, "/audit": 200},
    "invalid key": {route: 401 for _, route, _ in ROUTES},
    "missing key": {route: 401 for _, route, _ in ROUTES},
}

RULE = {200: "BR-1", 401: "BR-2", 403: "BR-3"}

QUESTION = "What are the clinic's opening hours on Saturday?"

MATRIX = [
    (n, key, method, route, path)
    for n, (key, (method, route, path)) in enumerate(
        ((k, r) for k in KEYS for r in ROUTES), start=1
    )
]


def _needs_model(key, route):
    return EXPECTED[key][route] == 200 and route in ("/ask", "/clinical/ask")


@pytest.mark.parametrize(
    "n,key,method,route,path", MATRIX, ids=[f"{k}-{r}" for _, k, _, r, _ in MATRIX]
)
def test_status_code(n, key, method, route, path, cached_ask, record):
    expected = EXPECTED[key][route]

    if _needs_model(key, route):
        if not os.getenv("GROQ_API_KEY"):
            pytest.skip("Needs a model call and no GROQ_API_KEY is set.")
        # cached_ask raises on any non-2xx, so returning means HTTP 200.
        body = cached_ask(f"RBAC-{key}-{route.strip('/').replace('/', '-')}", QUESTION, role=key, path=path)
        actual = 200 if body.get("answer") else 500
    else:
        headers = {"X-API-Key": KEYS[key]} if KEYS[key] else {}
        if method == "POST":
            response = requests.post(config.BASE_URL + path, json={"question": QUESTION}, headers=headers, timeout=60)
        else:
            response = requests.get(config.BASE_URL + path, headers=headers, timeout=60)
        actual = response.status_code

    record(
        LANE,
        {
            "id": f"RBAC-{n:02d}",
            "kind": "status",
            "key": key,
            "method": method,
            "endpoint": route,
            "title": f"{key} → {method} {route}",
            "expected_status": expected,
            "actual_status": actual,
            "passed": actual == expected,
            "rules": RULE[expected],
        },
    )

    assert actual == expected, f"{key} on {method} {route}: expected {expected}, got {actual}"
