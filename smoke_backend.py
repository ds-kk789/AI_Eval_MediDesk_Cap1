"""Backend smoke test with a scripted fake model: no LLM calls, no API key.

Drives the real FastAPI app, ADK runners, tools, RAG index and audit log in
process (TestClient). Only the model is replaced: FakeLlm reads the question
and returns the tool calls a model would make, including the calls a
misbehaving model might make, so the server-side controls are what is tested.

    python smoke_backend.py

Writes reports/backend_smoke_report.json and refreshes the evaluation
dashboard (build_report.py -> reports/evaluation_report.html), where it is the
"Backend smoke" tab. This is a developer check of the backend, not one of the
eval lanes, and the release gate does not read it.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from backend import config

# Keep smoke traffic out of the real audit log.
config.AUDIT_LOG = Path(tempfile.mkdtemp()) / "audit_smoke.jsonl"

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402

import medidesk_agent.agent as agent_module  # noqa: E402


# question substring -> tool calls for the first turn. No match -> a refusal.
PLAN = {
    "cancellation fee": [("retrieve_clinic_policy", {"query": "cancellation fee"})],
    "sneaky": [("retrieve_clinic_policy", {"query": "Daniel Koh diagnosis hypertension"})],
    "P-1003 allergic": [("retrieve_patient_record", {"patient": "P-1003"})],
    "two patients": [
        ("retrieve_patient_record", {"patient": "P-1001"}),
        ("retrieve_patient_record", {"patient": "P-1002"}),
    ],
    "P-9999": [("retrieve_patient_record", {"patient": "P-9999"})],
    "by name": [("retrieve_patient_record", {"patient": "alex  TAN"})],
}


class FakeLlm(BaseLlm):
    model: str = "fake"

    async def generate_content_async(self, llm_request, stream=False):
        contents = llm_request.contents
        question = next(
            p.text for c in contents if c.role == "user" for p in c.parts if p.text
        )
        responses = [
            p.function_response for p in contents[-1].parts if p.function_response
        ]

        if responses:
            # Second turn: echo what the tools returned, as a careless model would.
            text = "ANSWER: " + " || ".join(
                " | ".join(r.response.get("passages", [])) or r.response.get("status", "")
                for r in responses
            )
        else:
            plan = next((v for k, v in PLAN.items() if k in question), [])
            if plan:
                parts = [
                    types.Part(function_call=types.FunctionCall(name=n, args=a))
                    for n, a in plan
                ]
                yield LlmResponse(content=types.Content(role="model", parts=parts))
                return
            text = config.RESTRICTED_REFUSAL_TEXT if "Koh" in question else config.REFUSAL_TEXT

        # gpt-oss style: a reasoning part (thought=True) before the reply.
        thought = types.Part(text="We have the answer. Provide that.", thought=True)
        yield LlmResponse(content=types.Content(role="model", parts=[thought, types.Part(text=text)]))


agent_module.select_model = lambda: FakeLlm()

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app, classify  # noqa: E402


FD = {"X-API-Key": "front-desk-demo-key"}
CL = {"X-API-Key": "clinician-demo-key"}

results = []


def check(area, name, rule, passed, evidence=""):
    results.append(
        {"area": area, "check": name, "rule": rule, "passed": bool(passed), "evidence": str(evidence)[:300]}
    )


def post(client, path, question, headers=None):
    return client.post(path, json={"question": question}, headers=headers or {})


def ids(body):
    return [s["id"] for s in body["sources"]]


def run():
    with TestClient(app) as client:
        r = client.get("/")
        check("Service", "GET / carries the MediDesk marker", "-", "MediDesk" in r.json()["message"], r.json()["message"])

        # --- Access control -------------------------------------------
        r = post(client, "/ask", "hi")
        check("Access", "No API key → 401", "BR-2", r.status_code == 401, r.status_code)
        r = post(client, "/ask", "hi", {"X-API-Key": "not-a-key"})
        check("Access", "Unknown API key → 401", "BR-2", r.status_code == 401, r.status_code)
        r = post(client, "/clinical/ask", "P-1003 allergic", FD)
        check("Access", "Front desk on /clinical/ask → 403", "BR-3", r.status_code == 403, r.status_code)
        r = post(client, "/ask", "", FD)
        check("Access", "Empty question → 422", "-", r.status_code == 422, r.status_code)
        me = client.get("/me", headers=FD).json()
        check("Access", "/me: front desk sees public only, one tool", "BR-1",
              me["visibilities"] == ["public"] and me["tools"] == ["retrieve_clinic_policy"], me)

        # --- Front desk ----------------------------------------------
        r = post(client, "/ask", "What is the cancellation fee?", FD).json()
        check("Front desk", "Policy question answered (Allowed)", "BR-12", r["decision"] == "Allowed" and "20 dollar" in r["answer"], r["answer"][:120])
        check("Front desk", "Policy tool called", "BR-5", r["tools_called"] == ["retrieve_clinic_policy"], r["tools_called"])
        check("Front desk", "Model reasoning (thought parts) stripped from the answer", "-", "Provide that" not in r["answer"], r["answer"][:80])
        check("Front desk", "Top source is the cancellation section", "BR-10", "cancellation" in r["sources"][0]["id"], ids(r))
        check("Front desk", "Every source is PUBLIC", "BR-5", all(s["visibility"] == "public" for s in r["sources"]), ids(r))

        r = post(client, "/ask", "What is Daniel Koh's diagnosis?", FD).json()
        check("Front desk", "Patient question refused, no tool called (Denied)", "BR-6",
              r["decision"] == "Denied" and r["tools_called"] == [] and r["sources"] == [], r["answer"])

        r = post(client, "/ask", "sneaky Daniel Koh", FD).json()
        check("Front desk", "Model searches for a patient: only PUBLIC chunks come back", "BR-5",
              all(s["visibility"] == "public" for s in r["sources"]) and "hypertension" not in r["answer"].lower(), ids(r))

        # --- Clinician -----------------------------------------------
        r = post(client, "/ask", "sneaky Daniel Koh", CL).json()
        check("Clinician", "Policy tool stays PUBLIC even for clinicians", "BR-7",
              all(s["visibility"] == "public" for s in r["sources"]), ids(r))

        r = post(client, "/clinical/ask", "What is P-1003 allergic to?", CL).json()
        check("Clinician", "Lookup by ID returns that one record", "BR-1",
              ids(r) == ["clinical/patient-record-p-1003-01"] and "sulfa" in r["answer"], ids(r))

        r = post(client, "/clinical/ask", "by name", CL).json()
        check("Clinician", "Lookup by full name (case and spacing ignored)", "BR-1",
              ids(r) == ["clinical/patient-record-p-1001-01"], ids(r))

        r = post(client, "/clinical/ask", "two patients", CL).json()
        check("Clinician", "Second patient in one request: whole answer refused", "BR-7",
              r["answer"] == config.BULK_REFUSAL_TEXT and r["decision"] == "Denied" and r["sources"] == [], r["answer"])
        bulk_id = r["request_id"]

        r = post(client, "/clinical/ask", "P-9999 meds", CL).json()
        check("Clinician", "Unknown patient → No match", "BR-10", r["decision"] == "No match" and r["sources"] == [], r["decision"])

        # --- Patient lookup page ----------------------------------------
        r = client.get("/clinical/patients/p1003", headers=CL)
        card = r.json()
        check("Patient lookup", "Clinician gets the structured record", "BR-1",
              r.status_code == 200 and card["allergies"] == "sulfa drugs", {k: card.get(k) for k in ("patient_id", "name", "allergies")})
        check("Patient lookup", "Unknown ID → 404", "-", client.get("/clinical/patients/P-9999", headers=CL).status_code == 404)
        exists = client.get("/clinical/patients/P-1003", headers=FD)
        missing = client.get("/clinical/patients/P-9999", headers=FD)
        check("Patient lookup", "Front desk: same 403 for existing and missing patient", "BR-9",
              exists.status_code == missing.status_code == 403 and exists.json() == missing.json(), exists.json())

        check("Release health", "GET /release/health responds", "-", client.get("/release/health", headers=FD).status_code == 200)

        # --- Audit ------------------------------------------------------
        fd_audit = client.get("/audit", headers=FD).json()["entries"]
        cl_audit = client.get("/audit", headers=CL).json()["entries"]
        check("Audit", "Front desk sees front-desk lines only", "BR-13",
              fd_audit and all(e["role"] == "front_desk" for e in fd_audit), f"{len(fd_audit)} lines")
        check("Audit", "Front-desk lines never reference a clinical chunk", "BR-5",
              not any(c.startswith("clinical/") for e in fd_audit for c in e["chunk_ids"]))
        check("Audit", "Clinician sees all roles, incl. anonymous 401s", "BR-13",
              {e["role"] for e in cl_audit} >= {"front_desk", "clinician", None}, sorted(map(str, {e["role"] for e in cl_audit})))
        bulk_line = next(e for e in cl_audit if e["request_id"] == bulk_id)
        check("Audit", "Refused bulk request still logs what was retrieved", "BR-13",
              bulk_line["chunk_ids"] == ["clinical/patient-record-p-1001-01"], bulk_line["chunk_ids"])


    lines = [json.loads(line) for line in config.AUDIT_LOG.read_text(encoding="utf-8").splitlines()]
    raw = config.AUDIT_LOG.read_text(encoding="utf-8")
    fields = {"timestamp", "request_id", "role", "method", "endpoint", "status_code",
              "decision", "tools_called", "chunk_ids", "latency_ms"}
    check("Audit", f"All {len(lines)} lines have exactly the required fields", "BR-13", all(set(l) == fields for l in lines))
    check("Audit", "No question or answer text in the log", "BR-13",
          not any(t in raw for t in ("cancellation fee", "allergic", "sulfa", "Daniel Koh")))
    check("Audit", "Patient IDs in URLs logged as the route template", "BR-13",
          any(l["endpoint"] == "/clinical/patients/{patient_id}" for l in lines) and "/clinical/patients/P-" not in raw)
    check("Audit", "401 and 422 rejections are logged", "BR-13",
          any(l["status_code"] == 401 and l["decision"] == "Denied" for l in lines)
          and any(l["status_code"] == 422 for l in lines))

    check("Decision", "Curly apostrophe still classified (Referred)", "BR-11",
          classify("I can’t give medical advice or a diagnosis. Please speak to the doctor in charge.", [], []) == "Referred")


# ------------------------------------------------------------
# REPORT
# ------------------------------------------------------------


def traffic_stats():
    """Request and retrieval counts from this run's audit log."""
    lines = [json.loads(line) for line in config.AUDIT_LOG.read_text(encoding="utf-8").splitlines()]
    by_decision, chunks_by_role = {}, {}

    for line in lines:
        by_decision[line["decision"]] = by_decision.get(line["decision"], 0) + 1

        if line["role"]:
            counts = chunks_by_role.setdefault(line["role"], {"public": 0, "clinical": 0})
            for chunk_id in line["chunk_ids"]:
                counts[chunk_id.split("/", 1)[0]] += 1

    return {"requests": len(lines), "by_decision": by_decision, "chunks_by_role": chunks_by_role}


def write_report():
    import build_report

    passed = sum(r["passed"] for r in results)
    rules = sorted({r["rule"] for r in results if r["rule"] != "-"}, key=lambda x: int(x.split("-")[1]))

    report = {
        "lane": "backend_smoke",
        "generated_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "total_cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "rules_covered": rules,
        "traffic": traffic_stats(),
        "detailed_logs": results,
    }
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / "backend_smoke_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Refresh the evaluation dashboard; its Backend smoke tab shows this run.
    return build_report.build(), passed


if __name__ == "__main__":
    run()
    path, passed = write_report()

    for r in results:
        print(("PASS " if r["passed"] else "FAIL ") + f"[{r['area']}] {r['check']}")

    print(f"\n{passed}/{len(results)} passed · {path}")
    sys.exit(0 if passed == len(results) else 1)
