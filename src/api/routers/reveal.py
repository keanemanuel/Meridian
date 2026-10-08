"""The "Are you verified?" gate in front of real applicant names.

`GET /api/workspaces/{workspace_id}/reveal` tells the UI whether *this
workspace* disguises names at all and whether the caller is already
verified; `POST .../reveal` trades the password for a short-lived token the
browser then sends as `X-Reveal-Token`. The disguise itself, and which
workspaces it applies to, lives in `api.privacy`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.privacy import RevealDep, issue_token, masking_enabled, password_matches

router = APIRouter(prefix="/api/workspaces/{workspace_id}/reveal", tags=["reveal"])


@router.get("")
def reveal_status(workspace_id: str, revealed: RevealDep) -> dict[str, Any]:
    return {"masking_enabled": masking_enabled(workspace_id), "revealed": revealed}


class RevealBody(BaseModel):
    password: str


@router.post("")
def reveal(workspace_id: str, body: RevealBody) -> dict[str, Any]:
    if not masking_enabled(workspace_id):
        raise HTTPException(
            status_code=409, detail="Names are not disguised in this workspace."
        )
    if not password_matches(body.password):
        raise HTTPException(status_code=401, detail="Wrong password.")
    token, expires_at = issue_token()
    return {"token": token, "expires_at": expires_at}
