"""The "Are you verified?" gate in front of real applicant names.

`GET /api/reveal` tells the UI whether this deployment disguises names at all
and whether the caller is already verified; `POST /api/reveal` trades the
password for a short-lived token the browser then sends as `X-Reveal-Token`.
The disguise itself lives in `api.privacy`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.privacy import RevealDep, issue_token, masking_enabled, password_matches

router = APIRouter(prefix="/api/reveal", tags=["reveal"])


@router.get("")
def reveal_status(revealed: RevealDep) -> dict[str, Any]:
    return {"masking_enabled": masking_enabled(), "revealed": revealed}


class RevealBody(BaseModel):
    password: str


@router.post("")
def reveal(body: RevealBody) -> dict[str, Any]:
    if not masking_enabled():
        raise HTTPException(
            status_code=409, detail="Names are not disguised on this deployment."
        )
    if not password_matches(body.password):
        raise HTTPException(status_code=401, detail="Wrong password.")
    token, expires_at = issue_token()
    return {"token": token, "expires_at": expires_at}
