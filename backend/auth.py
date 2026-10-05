"""API key -> role resolution and the clinician-only guard.

Port from: AI_Eval_Module_4_3_Steps/backend/auth.py

TODO:
- resolve_role(x_api_key): 401 on a missing or unknown key (BR-2)
- require_clinician(role): 403 for front desk on /clinical/ask (BR-3)
- allowed_visibilities(role) from config.ROLE_VISIBILITY
"""
