"""Backend smoke test with a scripted fake model: no LLM calls, no API key.

Drives the real FastAPI app, ADK runners, tools, RAG index and audit log in
process (TestClient). Only the model is replaced: FakeLlm reads the question
and returns the tool calls a model would make, including the calls a
misbehaving model might make, so the server-side controls are what is tested.

    python smoke_backend.py

Writes reports/backend_smoke_report.json and .html. This is a developer
check of the backend, not one of the eval lanes, and it is not read by
quality_gate.py.
"""

import html
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

        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


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

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MediDesk Backend Smoke</title>
<style>
:root {{ --bg:#f7f7f5; --panel:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e4e4df;
        --ok:#1f7a4d; --ok-bg:#e3f3ea; --bad:#b42318; --bad-bg:#fde8e6; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#141413; --panel:#1e1e1c; --text:#ecebe6; --muted:#a3a29b; --line:#33332f;
  --ok:#5cc28d; --ok-bg:#183326; --bad:#f2877d; --bad-bg:#3a1c19; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:32px 16px 48px; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
.sub {{ color:var(--muted); margin:0 0 24px; }}
.summary {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:24px; }}
.tile {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 18px; min-width:150px; }}
.tile .v {{ font-size:28px; font-weight:600; font-variant-numeric:tabular-nums; }}
.tile .k {{ color:var(--muted); font-size:13px; }}
.wrap {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); font-weight:600; }}
tr:last-child td {{ border-bottom:0; }}
td.area {{ font-weight:600; white-space:nowrap; }}
td.rule, td.ev {{ color:var(--muted); font-size:13px; }}
td.ev {{ font-family:ui-monospace, Consolas, monospace; font-size:12px; word-break:break-word; max-width:320px; }}
.pill {{ display:inline-block; padding:1px 9px; border-radius:99px; font-size:12px; font-weight:600; }}
.pass {{ color:var(--ok); background:var(--ok-bg); }} .fail {{ color:var(--bad); background:var(--bad-bg); }}
.note {{ color:var(--muted); font-size:13px; margin-top:16px; }}
</style></head><body><main>
<h1>Backend smoke test</h1>
<p class="sub">{verdict} · {when} · scripted fake model, no LLM calls</p>
<div class="summary">
  <div class="tile"><div class="v">{passed} / {total}</div><div class="k">checks passed</div></div>
  <div class="tile"><div class="v">{rules}</div><div class="k">business rules covered</div></div>
  <div class="tile"><div class="v">{chunks}</div><div class="k">chunks in the index ({public} public, {clinical} clinical)</div></div>
</div>
<div class="wrap"><table>
<thead><tr><th>Area</th><th>Check</th><th>Rule</th><th>Result</th><th>Evidence</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p class="note">The real API, ADK runners, tools, FAISS index and audit log run in process; only the model is
replaced. The fake model also makes calls a misbehaving model might make (searching for a patient from the front
desk, requesting two patients), so these checks test the server-side controls, not model behaviour. The live Groq
path is covered by the security, quality and ADK lanes.</p>
</main></body></html>
"""


def write_report():
    from medidesk_agent.agent import rag

    passed = sum(r["passed"] for r in results)
    rules = sorted({r["rule"] for r in results if r["rule"] != "-"}, key=lambda x: int(x.split("-")[1]))
    visibility = [r["visibility"] for r in rag.records]
    when = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    report = {
        "lane": "backend_smoke",
        "generated_at": when,
        "total_cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "rules_covered": rules,
        "detailed_logs": results,
    }
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / "backend_smoke_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    rows = []
    previous = None
    for r in results:
        area = r["area"] if r["area"] != previous else ""
        previous = r["area"]
        pill = '<span class="pill pass">Pass</span>' if r["passed"] else '<span class="pill fail">Fail</span>'
        rows.append(
            f'<tr><td class="area">{html.escape(area)}</td><td>{html.escape(r["check"])}</td>'
            f'<td class="rule">{html.escape(r["rule"])}</td><td>{pill}</td>'
            f'<td class="ev">{html.escape(r["evidence"])}</td></tr>'
        )

    page = HTML.format(
        verdict="All checks passed" if passed == len(results) else f"{len(results) - passed} failed",
        when=when,
        passed=passed,
        total=len(results),
        rules=len(rules),
        chunks=len(visibility),
        public=visibility.count(config.PUBLIC),
        clinical=visibility.count(config.CLINICAL),
        rows="\n".join(rows),
    )
    path = config.REPORTS_DIR / "backend_smoke_report.html"
    path.write_text(page, encoding="utf-8")
    return path, passed


if __name__ == "__main__":
    run()
    path, passed = write_report()

    for r in results:
        print(("PASS " if r["passed"] else "FAIL ") + f"[{r['area']}] {r['check']}")

    print(f"\n{passed}/{len(results)} passed · {path}")
    sys.exit(0 if passed == len(results) else 1)
