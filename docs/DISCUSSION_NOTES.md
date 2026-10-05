# MediDesk — Design Discussion Notes

Notes from the design discussion (1–5 Oct 2026) that shaped the proposal, and
the build session on 5 Oct 2026 that implemented the backend and the full eval
harness and ran it live (gate PASSED). Next: the web UI.
The proposal itself is in [MediDesk_Use_Case_Proposal.md](MediDesk_Use_Case_Proposal.md)
(live doc: https://claude.ai/code/artifact/2df5968b-e698-494c-9771-0a012ce9a0dc, the source of truth).
UI mock: [mockups/medidesk_mock_ui.html](mockups/medidesk_mock_ui.html)
(published: https://claude.ai/artifact/2S9YX2gxeCwfCB2we8dRKy).

- [1. Why this use case](#1-why-this-use-case)
- [2. Regression vs drift, in plain words](#2-regression-vs-drift-in-plain-words)
- [3. Reusing the HR project structure](#3-reusing-the-hr-project-structure)
- [4. Vector store and chunking](#4-vector-store-and-chunking)
- [5. FAISS vs Postgres](#5-faiss-vs-postgres)
- [6. MCP: when and how](#6-mcp-when-and-how)
- [7. Lecturer Q&A](#7-lecturer-qa)
- [8. Wiring FastAPI and ADK](#8-wiring-fastapi-and-adk)
- [9. Build session: what exists now](#9-build-session-what-exists-now)
- [10. Backend API for the web app](#10-backend-api-for-the-web-app)
- [11. First live run (5 Oct 2026)](#11-first-live-run-5-oct-2026)
- [12. How thresholds are set, pacing, drift, agentic](#12-how-thresholds-are-set-pacing-drift-agentic)
- [13. Next: the web UI](#13-next-the-web-ui)
- [14. Open items and corrections](#14-open-items-and-corrections)

---

## 1. Why this use case

Same shape as the HR RBAC assistant (Module 4), different domain. The HR
project restricts executive pay; MediDesk restricts patient medical records.
The PII risk is easier for a class to grasp, and every eval lane maps
one-to-one.

| HR project | MediDesk |
|---|---|
| `[VISIBILITY: PUBLIC / ADMIN]` | `[VISIBILITY: PUBLIC / CLINICAL]` |
| roles `employee` / `admin` | `front_desk` / `clinician` |
| `/ask`, `/admin/ask` | `/ask`, `/clinical/ask` |
| `retrieve_public_policy`, `retrieve_admin_compensation` | `retrieve_clinic_policy`, `retrieve_patient_record` |
| `rbac_denial` evalset | `clinical_denial` evalset (expects an empty tool trajectory) |

"Prod-ready" for a one-day build means the practices, not the scale:
two-layer security, one shared config, audit log, `.env.example`, pinned
dependencies, a CI gate and a README with the architecture.

## 2. Regression vs drift, in plain words

- **Regression** asks whether *we* changed something for the worse. The data
  stays fixed (golden set, attack set, evalsets) and the system changes
  (v1 → v2). The golden set works like a **fixed exam**.
- **Drift** asks whether *our users* changed. The system stays fixed and the
  data changes (reference window → current window). The golden set works like
  a **recipe book**: its questions seed templates that generate 100-question
  batches.

```mermaid
flowchart TD
    G["Golden dataset<br/>8 hand-written questions"] --> R["Regression<br/>did our change hurt?"]
    G --> D["Drift<br/>are users asking differently?"]
    R --> V1["Old version (v1)<br/>8 questions"]
    R --> V2["New version (v2)<br/>same 8 questions"]
    V1 & V2 --> C1["Compare scores"] --> O1["Worse? Release blocked"]
    D --> T["Use questions as recipes<br/>3 batches of 100"]
    T --> REF["Reference<br/>usual mix"]
    T --> CTL["Control<br/>usual mix"]
    T --> CUR["Current<br/>new topics"]
    REF & CTL & CUR --> C2["Compare batches (PSI)"]
    C2 --> Q["Ref vs control<br/>should stay quiet"]
    C2 --> A["Ref vs current<br/>should ring the alarm"]
```

- The golden set can't be the drift windows itself: 8 questions is too few
  for PSI (about 5–10 per bucket are needed), and it is a curated test set,
  not traffic.
- The **control** window is a second normal week. If PSI fires on it, the
  thresholds are too jumpy.
- **New reference:** after a drift alert is reviewed and the knowledge base
  updated, the current window becomes the new reference. The regression
  baseline is replaced only by the next approved release.

## 3. Reusing the HR project structure

Only the structure and plumbing are reused; all HR content is rewritten for
the clinic. Legend used in the README: `=` copied unchanged, `~` same role
with clinic content, `+` new.

Copied unchanged: `backend/llm.py` (Groq key failover),
`tests/quality/groq_judge.py` (cached judge), `pytest.ini`,
`requirements*.txt`, `evalsets/*_config.json`. Adapted: `tests/conftest.py`
(roles, app marker).

"Drop the concept-drift labelled sample first" means: input and output drift
only compare numbers between batches and need no answers. Concept drift also
needs about 10 hand-labelled questions scored by the LLM judge, so it is the
slowest part and uses the daily LLM budget.

## 4. Vector store and chunking

Already in the HR project and reused as-is in shape:

```
knowledge.txt
   │  split by [VISIBILITY: ...] markers, then by "## " headings
   ▼
chunks (RecursiveCharacterTextSplitter: 500 characters, 100 overlap)
   │  each chunk keeps its label → {"text": ..., "visibility": "clinical"}
   ▼
embed with local MiniLM (no API calls)
   ▼
data/faiss/index.faiss + chunks.json
   ▼
search(question, role): fetch TOP_K × OVERFETCH (5 × 6) candidates, drop forbidden ones
```

FAISS is a file-based vector index, not a database server. That is fine at
this size.

## 5. FAISS vs Postgres

FAISS isn't required. Vector search is needed because users word things
differently from the documents ("kid's jab card" should match the vaccination
policy).

| | FAISS (now) | Postgres + pgvector |
|---|---|---|
| Setup | None; files on disk | Postgres server (Docker), ~1 h |
| Role filter | Over-fetch, then filter in Python | `WHERE visibility = ANY(:allowed)` in the query |
| DB-level security | None | Row-level security per connection role |
| Patient records | Text chunks | A real table, exact lookup by ID |

Weakness this exposes in the current design: filtering *after* the search
means that if most top candidates are clinical, a front-desk user gets few or
no policy chunks back. That is a quality bug, not a leak. Filtering inside
the query avoids it.

Recommended production path: pgvector with row-level security for policy
text; patient records as a normal table looked up by exact ID; RAG for policy
only. For the one-day build, keep FAISS.

## 6. MCP: when and how

RAG is how you *find* text. MCP is how an agent *connects* to a tool or data
source through a separate server. They are not alternatives: an MCP server
can have the RAG pipeline inside it.

Use MCP when several clients need the same tools, when the data belongs to
another system (appointments, patient records), or when you want the server
itself to enforce access. Skip it for a single app with fixed knowledge.

Patient lookup by ID is a lookup, not a search, so it fits MCP well. The one
rule: the role check lives **inside the MCP server**, and the role comes from
the connection (environment variable or auth header), never from a tool
argument the model fills in.

Sketch (not tested):

```python
# mcp_server/clinic_server.py
import os
from mcp.server.fastmcp import FastMCP
from backend.rag_pipeline import RAGPipeline

ROLE = os.environ["MEDIDESK_ROLE"]          # set by the client at launch, not by the LLM
rag = RAGPipeline()
mcp = FastMCP("medidesk")

@mcp.tool()
def search_clinic_policy(query: str) -> str:
    """Clinic hours, insurance, cancellation and vaccine policy."""
    return "\n\n".join(r["chunk"] for r in rag.search(query, role=ROLE))

@mcp.tool()
def get_patient_record(patient_id: str) -> str:
    """One patient's record. Clinicians only."""
    if ROLE != "clinician":
        raise PermissionError("Not authorised for patient records")
    return "\n\n".join(r["chunk"] for r in rag.search(f"patient {patient_id}", role=ROLE))

if __name__ == "__main__":
    mcp.run()
```

The ADK agent connects with `McpToolset` (stdio), passing
`env={"MEDIDESK_ROLE": role}` and a `tool_filter` that hides the patient tool
from front desk. Treat MCP as an optional stretch goal (about 1–1.5 h).

## 7. Lecturer Q&A

**Q: How do you use both RAG and ADK?**

> RAG is how the assistant finds the right information. ADK is how it
> decides what to do with a question. The RAG search is wrapped as ADK tools,
> so the agent chooses which knowledge source to search, and the retrieval
> layer decides what that role is allowed to see.

Analogy: RAG is the library, ADK is the librarian, the role filter is the
locked room. Two separate tools give ADK's eval something to test: the tool
trajectory proves the agent never even tried to open patient records.

Possible follow-up: in the HR agent, `retrieve_admin_compensation` hard-codes
`role="admin"`, so on the ADK path safety depends on the model choosing not
to call it (which the trajectory eval checks). MediDesk builds the agent per
role, so the front-desk agent doesn't have the patient tool at all.

**Q: How do you implement FastAPI and ADK, and connect them?**

> FastAPI is the front door and ADK is the brain. FastAPI checks the API key
> and works out the role, then hands the question to an ADK agent built for
> that role, which calls the RAG tools. Security happens at the door, before
> the agent runs.

See section 8 for the code.

**Q: Isn't FastAPI just for triggering test endpoints, and ADK the client UI?**

Partly right: FastAPI's `/docs` page and `adk web` both look like front ends,
but both are developer tools.

> FastAPI is the production API: it checks the API key, decides the role,
> and runs the ADK agent behind it. ADK is the reasoning layer that picks
> which RAG tool to call. Clients use the MediDesk web app, which talks only
> to FastAPI. ADK's web chat is for debugging and demos only, because it
> bypasses the role check and the audit log.

**Q: Why MCP instead of the RAG pipeline?** See section 6.
**Q: Why FAISS and not Postgres?** See section 5.

## 8. Wiring FastAPI and ADK

```
Client ──X-API-Key──► FastAPI /ask
                        │ resolve_role(key) → front_desk / clinician  (401/403 here)
                        ▼
                     ADK Runner ──► Agent(tools = only what this role may use)
                        │              └─ retrieve_clinic_policy → RAG search(role)
                        │              └─ retrieve_patient_record → clinician only
                        ▼
                     answer + tools called ──► audit log ──► JSON response
```

Sketch (not tested; check ADK method names against the pinned
`google-adk==2.9.1` before relying on it):

```python
# medidesk_agent/agent.py
def build_agent(role: str) -> Agent:
    def retrieve_clinic_policy(query: str) -> dict:
        """Clinic hours, cancellations, insurance and vaccinations."""
        return {"passages": [r["chunk"] for r in _rag.search(query, role=role)]}

    def retrieve_patient_record(query: str) -> dict:
        """One named patient's record. Clinicians only."""
        return {"passages": [r["chunk"] for r in _rag.search(query, role=role)]}

    tools = [retrieve_clinic_policy]
    if role == "clinician":
        tools.append(retrieve_patient_record)
    return Agent(name=f"medidesk_{role}", model=..., instruction=..., tools=tools)

root_agent = build_agent("front_desk")   # what `adk eval` loads
```

```python
# backend/main.py
sessions = InMemorySessionService()
runners = {r: Runner(agent=build_agent(r), app_name="medidesk", session_service=sessions)
           for r in ("front_desk", "clinician")}

async def run_agent(role, question):
    session_id = str(uuid.uuid4())
    await sessions.create_session(app_name="medidesk", user_id=role, session_id=session_id)
    tools_called, answer = [], ""
    message = types.Content(role="user", parts=[types.Part(text=question)])
    async for event in runners[role].run_async(user_id=role, session_id=session_id, new_message=message):
        for call in event.get_function_calls() or []:
            tools_called.append(call.name)
        if event.is_final_response() and event.content and event.content.parts:
            answer = event.content.parts[0].text
    return {"answer": answer, "tools_called": tools_called}

@app.post("/clinical/ask")
async def clinical_ask(body: dict, role: str = Depends(resolve_role)):
    require_clinician(role)                      # 403 before the agent runs
    result = await run_agent(role, body["question"])
    log_request(role=role, endpoint="/clinical/ask", tools=result["tools_called"])
    return result
```

ADK's built-in `adk api_server` is not used, because it doesn't know about the
API keys, roles or audit log.

## 9. Build session: what exists now

Everything except the web UI is implemented and committed (latest `097b889`).

| Area | Files | Notes |
|---|---|---|
| Backend | `backend/` (`main.py`, `auth.py`, `audit.py`, `rag_pipeline.py`, `config.py`) | FastAPI, key → role, 401/403, audit middleware, role-filtered FAISS, exact patient lookup |
| Agent | `medidesk_agent/agent.py` | One agent per role; `root_agent` built lazily; Groq via LiteLLM with key failover |
| Lanes | `tests/rbac`, `tests/security`, `tests/quality`, `tests/audit`, `run_adk_eval.py` + `evalsets/` | Each writes `reports/<lane>_report.json` |
| Regression / drift | `regression_check.py` (`--promote`), `make_drift_windows.py`, `drift_check.py` | Baseline in `reports/baseline/` |
| Gate + dashboard | `quality_gate.py`, `build_report.py` + `build_report_template.html` | Dashboard: `reports/evaluation_report.html` |
| Dev check | `smoke_backend.py` | 31 checks with a scripted model, no LLM calls |
| Sample data | `reports/sample/` | Hand-made reports in every lane's format; the dashboard's "Sample preview" |

Python used so far: the HR project's `aienv` (same pinned versions). This repo
has no `.venv` yet. A private, gitignored run guide (setup, the manual run order,
diagrams, code traces) is in `docs/private/E2E_RUN_GUIDE.html`; it is not part of
the submission.

Security controls added beyond the section 8 sketch:

- The policy tool searches PUBLIC chunks only, for every role, so a policy
  search can't sweep patient records (BR-7).
- The patient tool is an exact lookup (ID or full name), capped at one patient
  per request; asking for a second replaces the whole answer with the bulk
  refusal.
- Tools pass chunk ids through ADK session state, not the model's text.
- The decision (Allowed / Denied / Referred / No match) is classified from fixed
  refusal sentences, without a model.
- gpt-oss reasoning parts (`thought=True`) are stripped from answers.

## 10. Backend API for the web app

| Route | Who | Returns |
|---|---|---|
| `GET /` | anyone | health + "MediDesk" marker |
| `GET /me` | any key | role, visibilities, endpoints, tools |
| `POST /ask` | any key | answer from the caller's role agent |
| `POST /clinical/ask` | clinician (403 otherwise) | same, with the patient tool |
| `GET /clinical/patients/{id}` | clinician (403 otherwise) | structured record, no LLM; 404 if none; front desk gets the same 403 whether or not the patient exists |
| `GET /audit` | any key | recent audit lines; front desk sees front-desk lines only |
| `GET /release/health` | any key | summaries of `reports/*_report.json` (no per-case logs) |

Ask responses: `answer`, `decision`, `refused`, `tools_called`, `sources`
(id, section, visibility, score, text), `retrieved_context`, `request_id`,
`latency_ms`. CORS allows `localhost:5173` and `:8080` by default
(`ALLOWED_ORIGINS`). Dev keys: `front-desk-demo-key`, `clinician-demo-key`.

## 11. First live run (5 Oct 2026)

Full datasets, keys from `.env` (key 1 was enough). **Gate PASSED 10/10.**

| Lane | Result |
|---|---|
| RBAC | 21/21 (16 key × route + 5 retrieval checks) |
| Security | 0 of 15 attacks leaked; 0 clinical chunks to front desk |
| Agent (ADK) | trajectory 1.0 on 7 cases; 0 patient-tool calls; response match 0.62 (soft) |
| Quality | relevancy 1.00 · faithfulness 1.00 · correctness 0.95 (exactly at the gate) |
| Audit | 5/5 |
| Regression | No significant change (v1 frozen from this same run) |
| Drift | 5 alerts, control window quiet (alert only) |

Fixes made because of the live run:

- **Reasoning leaked into answers:** ADK passes gpt-oss reasoning as
  `thought=True` parts; the API now drops them (smoke check added).
- **ADK response match counted reasoning:** ADK joins every text part, so
  refusals identical to their reference scored 0.22–0.43. `run_adk_eval.py`
  rescores ROUGE-1 on the visible answer with ADK's own scorer; the raw score
  is kept as `adk_response_match_raw`.
- **Drift coverage threshold 0.40 → 0.35:** at 0.40, 18% of the normal week
  counted as uncovered. See section 12.

Findings for review:

- Correctness 0.95 is exactly at the gate. G-5 omitted "needs an appointment"
  (fair); G-7 was marked down for not naming the patient (judge strictness).
- TC-PI-01 and TC-PI-02 were refused in the model's own words, so the API's
  decision label says "Allowed". Nothing leaked; the label is wrong.

## 12. How thresholds are set, pacing, drift, agentic

**Thresholds.** Start from the proposal or a convention, then check against data
where right and wrong are known. Security, RBAC, audit: zero tolerance (policy).
Quality ≥ 0.95 on the mean. ADK response match ≥ 0.25 (soft; correct answers
score ~0.3–0.6). Regression Δ ≥ −0.02 (judge noise). PSI 0.10 / 0.25 (industry
convention). Coverage similarity was calibrated:

| Template topic | min | median | max |
|---|---|---|---|
| Insurance (covered) | 0.36 | 0.47 | 0.59 |
| Clinical lookups (covered) | 0.34 | 0.50 | 0.70 |
| Dengue (not covered) | 0.28 | 0.34 | 0.41 |
| Telehealth, long (not covered) | 0.34 | 0.41 | 0.48 |

Covered and uncovered overlap at 0.40. At 0.35, the reference and control weeks
show 0% uncovered, and most dengue and short telehealth questions fall below.
Long telehealth questions still pass, so the current week measures 11% against
a true 28%: the rate is a floor.

**Why quality takes ~28 min.** Spacing, not concurrency: each uncached call waits
until 30 s (`LLM_PACE_SECONDS`) have passed since the last one, to stay under
Groq's ~8k tokens/minute free tier. An `asyncio.Semaphore(1)` limits calls in
flight, not calls per minute, so it would still trip the 429. 8 answers + 57
judge calls (~7 per question) × 30 s ≈ 28 min.

**Drift.** Baseline `data/drift/reference.jsonl`; checked `current.jsonl`; sanity
`control.jsonl`. Per query: MiniLM embedding → FAISS top-1 → topic (section or
"Not covered" below 0.35), similarity, word count, role, filtered-nearest flag.
Bucket each feature, smooth empty buckets with 1e-6, compare with PSI / JS / KL.
Input: topic mix, length, clinician share. Output (retrieval-based): top-1
similarity, nearest chunk dropped by the role filter. Concept: out-of-coverage
rate. Answer-based signals were skipped (≈20 calls per window). Topic-mix PSI
1.808: 1.277 of it comes from the "Not covered" bucket, which was empty in the
reference.

**Agentic?** The system under test is (the ADK agent chooses a tool or refuses,
then answers). The evaluation harness is not (scripted pytest and scripts; the
LLM judge only scores). The agent lane grades actions (tool trajectory), which
makes it an agent evaluation.

## 13. Next: the web UI

Build the MediDesk web app (front desk + clinician) against the API in
section 10, following the published mock (`docs/mockups/medidesk_mock_ui.html`):
Assistant, Patient lookup (locked for front desk), Audit log, Release health.
The mock's screens map to real endpoints: Assistant → `/ask` / `/clinical/ask`
(sources, decision); Patient lookup → `/clinical/patients/{id}`; Audit →
`/audit`; Release health → `/release/health` (or link the evaluation
dashboard). Open questions for the UI session: stack (plain HTML/JS vs
Vite/React), how the role is chosen in a demo (key picker vs a login stand-in),
where API keys live in the browser, and whether the release-health view reuses
the dashboard.

## 14. Open items and corrections

- **Fixed 5 Oct 2026 — proposal wording:** the regression demo said "chunk
  size 500 → 300 tokens, top-k 4 → 2". The code uses **characters** and
  `TOP_K = 5`; it now reads "500 → 300 characters, top-k 5 → 2".
- **Fixed 5 Oct 2026 — CI claim:** the proposal said RBAC *and security* run
  in CI with no LLM calls. The security lane's answer checks call the model,
  so it now says RBAC, retrieval-boundary and audit run on every push, and
  security, ADK and quality run on demand.
- **Unverified APIs:** the MCP sketch was not run, and `mcp` is not in
  `requirements.txt` yet. **Fixed 5 Oct 2026:** the ADK wiring in section 8 is
  implemented in `backend/main.py` and has now run live against Groq (section 11).
- **v2 regression run not done:** the baseline was frozen from the first run,
  so regression only compared it with itself. The real test (`CHUNK_SIZE` 300,
  `TOP_K` 2, clear `reports/_cache`, rerun the model lanes) costs about the same
  as the first run.
- **Decision label for free-worded refusals:** TC-PI-01/02 show "Allowed"
  (section 11). Consider an "other refusal" rule or a prompt change.
- **Repo `.venv`:** not created yet; runs used the HR project's `aienv`.
- **Changed from the section 8 sketch (5 Oct 2026):** the policy tool searches
  PUBLIC chunks only for every role; the patient tool is an exact lookup by ID
  or full name, capped at one patient per request; tools pass chunk ids through
  ADK session state, not the model; `root_agent` is built lazily so the API
  starts without LLM keys. Added `GET /me`, `GET /clinical/patients/{id}`,
  `GET /audit` and `GET /release/health` for the web app.
- **Thresholds:** the drift alert levels (PSI ≥ 0.25, JS ≥ 0.1) are starting
  points checked against the control window. **Fixed 5 Oct 2026:** coverage
  similarity recalibrated 0.40 → 0.35 (section 12).
- **LLM budget:** the first full run used ~26 API requests, 7 agent cases and
  57 judge calls on key 1. Everything is cached; key failover to
  `GROQ_API_KEY_2` covers the judge, the API and the agent.
- **OWASP mapping** was written from memory; check names against the
  official 2025 list before presenting.
