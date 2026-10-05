"""FastAPI front door. Resolves the role, runs the ADK agent for that role, writes the audit log.

Port from: AI_Eval_Module_4_3_Steps/backend/main.py (plus the FastAPI + ADK wiring in docs/DISCUSSION_NOTES.md)

TODO:
- GET /  -> {'message': 'MediDesk ...'} (conftest checks this marker)
- POST /ask           any valid role
- POST /clinical/ask  clinician only (403 otherwise)
- Build one ADK Runner per role at startup; run the agent per request
- Return answer, tools_called and retrieved chunk ids (the tests assert on these)
"""
