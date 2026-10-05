"""ADK agent, built per role so the front-desk agent never has the patient tool.

Port from: AI_Eval_Module_4_3_Steps/hr_agent/agent.py

TODO:
- build_agent(role): tools = [retrieve_clinic_policy] (+ retrieve_patient_record for clinician)
- Each tool calls _rag.search(query, role=role) with the server-side role
- root_agent = build_agent('front_desk')  # what `adk eval` loads
- Model selection: Gemini if in quota, else Groq via LiteLLM (copy select_model)
"""
