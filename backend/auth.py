"""API key -> role resolution and the clinician-only guard.

Authorisation is enforced in two independent places:

1. Here, at the HTTP boundary: a missing or unknown key gets 401 (BR-2), and
   front desk on a clinical route gets 403 (BR-3).
2. In rag_pipeline, which drops chunks the role may not see before they can
   reach the model (BR-5).

The second layer is the one that protects the data. A model asked not to
reveal text it was handed will eventually reveal it; a model that was never
handed the text cannot.

The role is stored on request.state so the audit middleware can log it, and
is never read from the request body (BR-4).
"""

from fastapi import Header, HTTPException, Request

from backend import config


def resolve_role(
    request: Request,
    x_api_key: str = Header(default=None, alias="X-API-Key"),
):
    """FastAPI dependency: map the API key to a role, or reject with 401."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header.")

    role = config.ROLE_KEYS.get(x_api_key)

    if role is None:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    request.state.role = role
    return role


def require_clinician(role: str):
    """Raise 403 unless the resolved role is clinician.

    The message is the same whatever was asked, so a 403 never hints at
    whether a patient exists (BR-9).
    """
    if role != config.CLINICIAN:
        raise HTTPException(
            status_code=403,
            detail="Forbidden. Patient records are restricted to clinicians.",
        )

    return role


def allowed_visibilities(role: str):
    """Chunk visibilities this role may retrieve. Unknown roles fail closed."""
    return config.ROLE_VISIBILITY.get(role, {config.PUBLIC})
