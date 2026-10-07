"""FastAPI front door. Resolves the role, runs the ADK agent for that role,
writes the audit log.

Ported from AI_Eval_Module_4_3_Steps/backend/main.py, with the FastAPI + ADK
wiring from docs/DISCUSSION_NOTES.md section 8:

    X-API-Key -> resolve_role (401) -> require_clinician (403, clinical routes)
              -> ADK Runner for that role -> tools search with that role
              -> audit line (no question/answer text) -> JSON response

Routes:
    GET  /                          health + app marker, no auth
    GET  /me                        the caller's role and what it may use
    POST /ask                       any valid role
    POST /clinical/ask              clinician only (403 otherwise)
    GET  /clinical/patients/{id}    clinician only; exact record lookup, no LLM
    GET  /audit                     recent audit lines (front desk: own role only)
    GET  /release/health            lane, regression and drift report summaries

MEDIDESK_REPLAY=1 answers the two ask routes from the recorded e2e responses
instead of calling the model (backend/replay.py); everything else is live.

ADK's own `adk api_server` / `adk web` are not used here: they know nothing
about API keys, roles or the audit log.
"""

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from backend import audit, config, replay
from backend.auth import allowed_visibilities, require_clinician, resolve_role
from medidesk_agent.agent import (
    BULK_BLOCKED_KEY,
    RETRIEVED_KEY,
    build_agent,
    rag,
)


logger = logging.getLogger("medidesk")

APP_NAME = "medidesk"

# Requests under these paths get an audit line, including 401/403/422.
AUDITED_PREFIXES = ("/ask", "/clinical/")

# Fixed refusal sentences -> decision. Matched on the normalised answer so a
# trailing space or curly apostrophe doesn't change the outcome.
REFUSALS = (
    (config.RESTRICTED_REFUSAL_TEXT, "Denied"),
    (config.BULK_REFUSAL_TEXT, "Denied"),
    (config.MEDICAL_ADVICE_TEXT, "Referred"),
    (config.REFUSAL_TEXT, "No match"),
)

STATUS_DECISIONS = {401: "Denied", 403: "Denied", 404: "No match", 422: "Invalid"}


# ------------------------------------------------------------
# APP
# ------------------------------------------------------------


@asynccontextmanager
async def lifespan(app):
    # Load (or build) the FAISS index up front so the first question isn't
    # slow. No model is called here; the agent model is chosen on first use.
    rag.load_index()
    yield


app = FastAPI(title="MediDesk Clinic Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith(
        AUDITED_PREFIXES
    ):
        return await call_next(request)

    request.state.request_id = str(uuid.uuid4())
    request.state.role = None
    request.state.audit = {}
    started = time.perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request.state.request_id
        return response
    finally:
        details = request.state.audit
        decision = details.get("decision") or STATUS_DECISIONS.get(
            status_code, "Error" if status_code >= 500 else "Allowed"
        )

        audit.log_request(
            request_id=request.state.request_id,
            role=request.state.role,
            method=request.method,
            endpoint=_route_template(request),
            status_code=status_code,
            decision=decision,
            tools_called=details.get("tools_called", []),
            chunk_ids=details.get("chunk_ids", []),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def _route_template(request):
    """'/clinical/patients/{patient_id}', not the concrete ID: the audit
    line must not carry a patient identifier in its endpoint field."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


# ------------------------------------------------------------
# AGENT RUNNERS
# ------------------------------------------------------------

sessions = InMemorySessionService()
_runners = {}


def _runner(role):
    # Built on first use: building selects (and may probe) the model.
    if role not in _runners:
        _runners[role] = Runner(
            agent=build_agent(role), app_name=APP_NAME, session_service=sessions
        )
    return _runners[role]


async def run_agent(role, question):
    """One fresh session per request; returns answer, tools and chunk refs."""
    if config.REPLAY_MODE:
        return replay.run(role, question)

    runner = _runner(role)
    session_id = str(uuid.uuid4())

    await sessions.create_session(
        app_name=APP_NAME, user_id=role, session_id=session_id
    )

    tools_called, answer = [], ""
    message = types.Content(role="user", parts=[types.Part(text=question)])

    try:
        async for event in runner.run_async(
            user_id=role, session_id=session_id, new_message=message
        ):
            for call in event.get_function_calls() or []:
                tools_called.append(call.name)

            if event.is_final_response() and event.content and event.content.parts:
                # gpt-oss returns its reasoning too; ADK marks those parts
                # thought=True. Only the reply itself goes to the caller.
                answer = "".join(
                    part.text or "" for part in event.content.parts if not part.thought
                )

        session = await sessions.get_session(
            app_name=APP_NAME, user_id=role, session_id=session_id
        )
        state = session.state if session else {}
    finally:
        await sessions.delete_session(
            app_name=APP_NAME, user_id=role, session_id=session_id
        )

    return {
        "answer": answer.strip(),
        "tools_called": tools_called,
        "retrieved": list(state.get(RETRIEVED_KEY, [])),
        "bulk_blocked": bool(state.get(BULK_BLOCKED_KEY)),
    }


# ------------------------------------------------------------
# RESPONSE SHAPING
# ------------------------------------------------------------


def _normalise(text):
    return " ".join(text.replace("’", "'").split()).casefold()


def classify(answer, tools_called, sources):
    """Allowed / Denied / Referred / No match, without asking a model."""
    normalised = _normalise(answer)

    for refusal, decision in REFUSALS:
        if _normalise(refusal) in normalised:
            return decision

    looked_up_patient = "retrieve_patient_record" in tools_called
    found_record = any(s["visibility"] == config.CLINICAL for s in sources)

    if looked_up_patient and not found_record:
        return "No match"

    return "Allowed"


def _sources(retrieved, role):
    """Full chunk details for the response, best score per chunk.

    Re-checks visibility against the caller's role: the tools already filter,
    this keeps the response from ever being the place a leak shows up.
    """
    allowed = allowed_visibilities(role)
    best = {}

    for ref in retrieved:
        record = rag.by_id.get(ref["id"])

        if record is None or record["visibility"] not in allowed:
            continue

        if ref["id"] not in best or ref["score"] > best[ref["id"]]["score"]:
            best[ref["id"]] = {
                "id": record["id"],
                "section": record["section"],
                "visibility": record["visibility"],
                "score": ref["score"],
                "text": record["text"],
            }

    return sorted(best.values(), key=lambda s: s["score"], reverse=True)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


async def _answer(request, role, question):
    started = time.perf_counter()

    try:
        result = await run_agent(role, question)
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - model/provider failures
        logger.exception("Agent run failed")
        raise HTTPException(
            status_code=503,
            detail=f"The assistant is unavailable ({type(error).__name__}). Try again.",
        ) from error

    sources = _sources(result["retrieved"], role)
    answer = result["answer"]

    # The audit records everything that was retrieved, even if the answer
    # below is replaced: it is the record of what reached the model.
    chunk_ids = [s["id"] for s in sources]

    if result["bulk_blocked"]:
        # More than one patient was requested (BR-7). Refuse the whole
        # request rather than answer about the first patient only.
        answer, sources = config.BULK_REFUSAL_TEXT, []

    decision = classify(answer, result["tools_called"], sources)

    request.state.audit = {
        "decision": decision,
        "tools_called": result["tools_called"],
        "chunk_ids": chunk_ids,
    }

    return {
        "request_id": request.state.request_id,
        "role": role,
        "endpoint": request.url.path,
        "question": question,
        "answer": answer,
        "decision": decision,
        "refused": decision != "Allowed",
        "tools_called": result["tools_called"],
        "sources": sources,
        "retrieved_context": [s["text"] for s in sources],
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "replayed": config.REPLAY_MODE,
    }


# ------------------------------------------------------------
# ROUTES
# ------------------------------------------------------------


@app.get("/")
def home():
    # conftest.APP_MARKER checks for "MediDesk" here.
    return {
        "message": "MediDesk Clinic Assistant is running",
        "roles": sorted(config.ROLE_VISIBILITY),
        # conftest refuses to grade a replay server: its answers are recorded.
        "mode": "replay" if config.REPLAY_MODE else "live",
    }


@app.get("/me")
def me(role: str = Depends(resolve_role)):
    clinician = role == config.CLINICIAN

    return {
        "role": role,
        "visibilities": sorted(allowed_visibilities(role)),
        "endpoints": ["/ask", "/clinical/ask"] if clinician else ["/ask"],
        "tools": (
            ["retrieve_clinic_policy", "retrieve_patient_record"]
            if clinician
            else ["retrieve_clinic_policy"]
        ),
        # The web app shows the recorded questions as chips in replay mode.
        "mode": "replay" if config.REPLAY_MODE else "live",
        "replay_questions": replay.questions(role) if config.REPLAY_MODE else [],
    }


@app.post("/ask")
async def ask(
    body: QuestionRequest, request: Request, role: str = Depends(resolve_role)
):
    return await _answer(request, role, body.question)


@app.post("/clinical/ask")
async def clinical_ask(
    body: QuestionRequest, request: Request, role: str = Depends(resolve_role)
):
    require_clinician(role)  # 403 before the agent runs
    return await _answer(request, role, body.question)


@app.get("/clinical/patients/{patient_id}")
def patient_record(
    patient_id: str, request: Request, role: str = Depends(resolve_role)
):
    require_clinician(role)

    try:
        card = rag.patient_card(patient_id, role)
    except ValueError:
        card = None

    if card is None:
        raise HTTPException(status_code=404, detail="No record for that patient ID.")

    request.state.audit = {
        "decision": "Allowed",
        "tools_called": [],
        "chunk_ids": card["chunk_ids"],
    }

    return card


@app.get("/audit")
def audit_log(
    limit: int = Query(default=100, ge=1, le=1000),
    decision: str | None = None,
    role: str = Depends(resolve_role),
):
    # Clinicians see everything; front desk sees only front-desk lines, which
    # by construction never reference a clinical chunk.
    roles = None if role == config.CLINICIAN else {role}

    return {"entries": audit.read_entries(limit=limit, roles=roles, decision=decision)}


@app.get("/release/health")
def release_health(role: str = Depends(resolve_role)):
    """Summaries of whatever eval reports exist. Per-case logs are left out:
    a failing security case can carry the text that leaked."""
    reports = {}

    for path in sorted(config.REPORTS_DIR.glob("*_report.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        name = path.stem.removesuffix("_report")
        reports[name] = {
            key: value
            for key, value in report.items()
            if key not in {"detailed_logs", "cases"}
        }

    return {"reports": reports}
