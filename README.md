# MediDesk: Clinic Patient-Info Assistant

A retrieval-augmented assistant for a fictional clinic. Front-desk staff ask
about clinic policy; clinicians can also look up patient records. Like the HR
project it is modelled on, the interesting part is not the chatbot. It is the
evaluation harness that proves the front desk never sees patient data, and
the gate that blocks a release when that proof fails.

> **Status: backend implemented.** `backend/` and `medidesk_agent/` are
> built (FastAPI, auth, RAG, audit, per-role ADK agent). The web app and the
> eval lanes are still stubs that name the file to port from
> `AI_Eval_Module_4_3_Steps`.

**Problem statement:** How do we let an AI assistant help clinic staff
without it ever leaking patient records, and prove that before it ships?

- Proposal: [docs/MediDesk_Use_Case_Proposal.md](docs/MediDesk_Use_Case_Proposal.md)
  ([live doc](https://claude.ai/code/artifact/2df5968b-e698-494c-9771-0a012ce9a0dc))
- Design discussion and lecturer Q&A: [docs/DISCUSSION_NOTES.md](docs/DISCUSSION_NOTES.md)
- Clickable UI mock: [docs/mockups/medidesk_mock_ui.html](docs/mockups/medidesk_mock_ui.html)
  ([published](https://claude.ai/artifact/2S9YX2gxeCwfCB2we8dRKy))

All patients, names and insurers are synthetic.

---

## Architecture

```
MediDesk web app (front desk, clinicians)
        │  X-API-Key
        ▼
FastAPI  backend/main.py
  /ask            any valid role
  /clinical/ask   clinician only (403 otherwise)
        │  role resolved from the key, never from the chat
        ▼
ADK agent  medidesk_agent/agent.py  (one agent per role)
  retrieve_clinic_policy        all roles
  retrieve_patient_record       clinician agent only
        │
        ▼
RAG  backend/rag_pipeline.py
  knowledge.txt [PUBLIC | CLINICAL] → chunks → MiniLM → FAISS
  search(question, role) drops chunks the role may not see
        │
        ▼
audit log (role, endpoint, tools, chunks, decision; never answer text)

Eval lanes ──► reports/*.json ──► regression_check.py ──► quality_gate.py
drift_check.py runs beside the pipeline: alerts only, never blocks.
```

## API

| Route | Who | What |
|---|---|---|
| `GET /` | anyone | Health check; `message` contains "MediDesk" (conftest checks it) |
| `GET /me` | any valid key | Role, visibilities, endpoints and tools for that key |
| `POST /ask` | any valid key | Runs the caller's role agent |
| `POST /clinical/ask` | clinician (403 otherwise) | Same, with the patient-record tool |
| `GET /clinical/patients/{id}` | clinician (403 otherwise) | Exact record lookup by ID or full name, no LLM; 404 if none |
| `GET /audit` | any valid key | Recent audit lines; front desk sees front-desk lines only |
| `GET /release/health` | any valid key | Summaries of `reports/*_report.json` (no per-case logs) |

`/ask` and `/clinical/ask` return `answer`, `decision` (Allowed, Denied,
Referred, No match), `refused`, `tools_called`, `sources` (id, section,
visibility, score, text), `retrieved_context` and `request_id`.

Enforcement beyond the 401/403:

- The policy tool searches PUBLIC chunks only, for every role, so a policy
  search can't sweep patient records.
- The patient tool is an exact lookup capped at one patient per request. Asking
  for a second patient replaces the whole answer with the bulk refusal (BR-7).
- Tools write retrieved chunk ids to ADK session state, not into the model's
  context; the API reads them back for `sources` and the audit line.
- The model is chosen on the first question, so the API starts and answers
  401/403 without any LLM key (the CI offline lanes rely on this).

## Who uses what

| Part | Used by | Role |
|---|---|---|
| MediDesk web app | Front desk, clinicians | The only client UI |
| FastAPI | Web app, test lanes | Production API: API key → role, 401/403, runs the agent, audit log |
| ADK agent | FastAPI, `adk eval` | Decides which RAG tool to call |
| FastAPI `/docs`, `adk web` | Developers only | Debugging; `adk web` bypasses auth, so never for clients |

## Eval lanes

| Lane | Runner | Gate |
|---|---|---|
| RBAC | `pytest tests/rbac` | 100% correct status codes, 0 restricted retrievals |
| Security | `pytest tests/security` | 0 exploits across 15 attacks |
| Agent | `python run_adk_eval.py` | tool trajectory = 1.0 |
| Quality | `pytest tests/quality` | relevancy, faithfulness, correctness mean ≥ 0.95 |
| Audit | `pytest tests/audit` | required fields on every request |
| Regression | `python regression_check.py` | Δ ≥ −0.02 (quality), 0 flips on security/RBAC/trajectory, regression rate ≤ 10% |
| Drift | `python drift_check.py` | alert at PSI ≥ 0.25 or JS ≥ 0.1; never blocks |

## Folder map

`=` copied unchanged from the HR project · `~` same role, clinic content · `+` new

```
backend/          config ~  auth ~  llm =  rag_pipeline ~  audit +  main ~
medidesk_agent/   agent ~ (per-role tools)
evalsets/         *_config.json =  evalsets TODO
tests/            conftest ~  rbac ~  security ~  quality (groq_judge =)  audit +
data/drift/       reference / control / current windows +
reports/          lane reports, baseline/ +
docs/             proposal, discussion notes, UI mock
```

See [PLAYBOOK.md](PLAYBOOK.md) for run order.
