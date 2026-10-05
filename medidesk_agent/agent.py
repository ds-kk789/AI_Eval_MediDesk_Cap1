"""ADK agent, built per role so the front-desk agent never has the patient tool.

Ported from AI_Eval_Module_4_3_Steps/hr_agent/agent.py. In the HR agent both
tools were always attached and the admin tool hard-coded role="admin", so on
the ADK path safety rested on the model choosing not to call it. Here:

- build_agent(role) attaches retrieve_patient_record only for clinicians.
- Each tool searches with the server-side role it was built with, so the
  retrieval filter applies even if a tool were attached by mistake.
- retrieve_clinic_policy is scoped to PUBLIC chunks for every role, so a
  clinician's policy search can't be used to sweep patient records (BR-7).
- retrieve_patient_record is an exact lookup capped at one patient per
  request (config.MAX_PATIENTS_PER_REQUEST). Asking for a second patient
  flags the request, and the API answers with the bulk refusal instead.

Tools write what they retrieved to session state under RETRIEVED_KEY.
backend/main.py reads it back for the response and the audit log, so chunk
ids and visibility labels never have to pass through the model.

`root_agent` (the front-desk agent, what `adk eval` loads) is built on first
access. Importing this module never calls a model, so the API can start and
answer 401/403 without any LLM key.
"""

import os
import threading

from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.genai import types

from backend import config
from backend.rag_pipeline import RAGPipeline


RETRIEVED_KEY = "medidesk_retrieved"
PATIENTS_KEY = "medidesk_patients"
BULK_BLOCKED_KEY = "medidesk_bulk_blocked"

# One pipeline shared by every agent; loads (or builds) the index on first use.
rag = RAGPipeline()


# ------------------------------------------------------------
# TOOLS
# ------------------------------------------------------------


def _remember(tool_context, results):
    """Append retrieved chunk refs to session state (reassigned, so ADK
    records the change as a state delta)."""
    if tool_context is None:
        return

    refs = [
        {"id": r["id"], "visibility": r["visibility"], "score": r["score"]}
        for r in results
    ]
    tool_context.state[RETRIEVED_KEY] = (
        tool_context.state.get(RETRIEVED_KEY, []) + refs
    )


def _refuse_bulk(tool_context):
    """Flag the request; backend/main.py then replaces the whole answer with
    the bulk refusal, so a partial answer about the first patient never
    goes out either."""
    if tool_context is not None:
        tool_context.state[BULK_BLOCKED_KEY] = True

    return {"status": "refused", "reason": config.BULK_REFUSAL_TEXT}


def _make_tools(role):

    def retrieve_clinic_policy(query: str, tool_context: ToolContext) -> dict:
        """Look up Kestrel Family Clinic policy.

        Use for opening hours, cancellations and no-show fees, insurance and
        panel plans, and vaccinations.

        Args:
            query: The question or topic to look up.

        Returns:
            A dict with the matching policy passages.
        """
        results = rag.search(query, role=role, scope={config.PUBLIC})
        _remember(tool_context, results)

        return {
            "status": "ok" if results else "no_match",
            "passages": [r["chunk"] for r in results],
        }

    def retrieve_patient_record(patient: str, tool_context: ToolContext) -> dict:
        """Look up ONE patient's record. Clinicians only.

        Args:
            patient: The patient ID (for example P-1003) or the patient's full
                name. Exactly one patient.

        Returns:
            A dict with the record passages, or a not_found / refused status.
        """
        seen = list(tool_context.state.get(PATIENTS_KEY, [])) if tool_context else []

        try:
            patient_id = rag.resolve_patient(patient)
        except ValueError:
            return _refuse_bulk(tool_context)

        if (
            patient_id
            and patient_id not in seen
            and len(seen) >= config.MAX_PATIENTS_PER_REQUEST
        ):
            return _refuse_bulk(tool_context)

        results = rag.lookup_patient(patient, role=role)

        if not results:
            return {"status": "not_found", "passages": []}

        if tool_context is not None and patient_id not in seen:
            tool_context.state[PATIENTS_KEY] = seen + [patient_id]

        _remember(tool_context, results)

        return {"status": "ok", "passages": [r["chunk"] for r in results]}

    tools = [retrieve_clinic_policy]

    if config.CLINICAL in config.ROLE_VISIBILITY.get(role, set()):
        tools.append(retrieve_patient_record)

    return tools


# ------------------------------------------------------------
# MODEL SELECTION (Gemini if in quota, else Groq via LiteLLM)
# ------------------------------------------------------------

_model = None
_model_lock = threading.Lock()


def _gemini_is_usable():
    """Probe the Gemini key once. Quota exhaustion is the expected failure."""
    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        return False

    try:
        from google import genai

        genai.Client(api_key=api_key).models.generate_content(
            model=config.GEMINI_MODEL, contents="ping"
        )
        return True
    except Exception as error:  # noqa: BLE001 - any failure means fall back
        print(
            f"[medidesk_agent] Gemini unavailable ({type(error).__name__}), "
            f"falling back to {config.ADK_GROQ_MODEL}"
        )
        return False


def _groq_model():
    """LiteLLM-backed Groq model with two Groq-specific fixes.

    1. gpt-oss returns `reasoning_content` on assistant messages. On the second
       turn of a tool-calling conversation LiteLLM echoes it back and Groq
       rejects it (400: property 'reasoning_content' is unsupported), so it is
       stripped on the way out.
    2. Key failover. LiteLLM sends whatever key it was given, so on its own a
       daily-quota 429 on GROQ_API_KEY would fail every request until restart.
       Each call instead uses the shared pool's active key, and a daily-quota
       429 rotates the pool to GROQ_API_KEY_2 and retries once.
    """
    from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
    from litellm.exceptions import RateLimitError

    # Imported here: backend.llm raises at import when no Groq key is set.
    from backend.llm import _daily_quota_exhausted, groq

    def _strip_reasoning(messages):
        if not isinstance(messages, list):
            return messages

        return [
            {k: v for k, v in m.items() if k != "reasoning_content"}
            if isinstance(m, dict)
            else m
            for m in messages
        ]

    class GroqCompatibleClient(LiteLLMClient):

        async def acompletion(self, model, messages, tools, **kwargs):
            messages = _strip_reasoning(messages)
            while True:
                kwargs["api_key"] = groq.active_key
                try:
                    return await super().acompletion(
                        model=model, messages=messages, tools=tools, **kwargs
                    )
                except RateLimitError as error:
                    if _daily_quota_exhausted(error) and groq.rotate("daily quota"):
                        continue
                    raise

        def completion(self, model, messages, tools, **kwargs):
            messages = _strip_reasoning(messages)
            while True:
                kwargs["api_key"] = groq.active_key
                try:
                    return super().completion(
                        model=model, messages=messages, tools=tools, **kwargs
                    )
                except RateLimitError as error:
                    if _daily_quota_exhausted(error) and groq.rotate("daily quota"):
                        continue
                    raise

    # Start on whichever key still has daily budget (one ~10-token probe per
    # exhausted key); later exhaustion is handled per call above.
    api_key = groq.probe_key()
    print(f"[medidesk_agent] Groq via LiteLLM using {groq.active_label}")

    return LiteLlm(
        model=config.ADK_GROQ_MODEL,
        api_key=api_key,
        llm_client=GroqCompatibleClient(),
    )


def select_model():
    """Chosen once per process and shared by every role's agent."""
    global _model

    with _model_lock:
        if _model is None:
            if _gemini_is_usable():
                print(f"[medidesk_agent] Using Gemini model: {config.GEMINI_MODEL}")
                _model = config.GEMINI_MODEL
            else:
                _model = _groq_model()

    return _model


# ------------------------------------------------------------
# AGENTS
# ------------------------------------------------------------


def build_agent(role, model=None):
    """The agent for one role. `model` overrides selection (tests use a fake)."""
    if role not in config.ROLE_INSTRUCTIONS:
        raise ValueError(f"Unknown role: {role!r}")

    return Agent(
        name=f"medidesk_{role}",
        model=model or select_model(),
        description=(
            "Answers Kestrel Family Clinic staff questions from the clinic's "
            "documents, scoped to the caller's role."
        ),
        instruction=config.ROLE_INSTRUCTIONS[role],
        tools=_make_tools(role),
        # Same inputs, same answers: regression runs compare systems, not luck.
        generate_content_config=types.GenerateContentConfig(temperature=0),
    )


_root_agent = None


def __getattr__(name):
    # PEP 562: `adk eval` / `adk web` read `root_agent`; build it then.
    global _root_agent

    if name == "root_agent":
        if _root_agent is None:
            _root_agent = build_agent(config.FRONT_DESK)
        return _root_agent

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
