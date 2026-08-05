"""HTTP surface for the ambient capture policy.

There is no capture endpoint here, because there is no capture yet. This is the
governor, shipped first on purpose: when the capture loop is written, the only
way it will be able to run is by asking this.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from carrot import ambient

router = APIRouter(prefix="/api/ambient", tags=["ambient"])


class PolicyRequest(BaseModel):
    policy: Dict[str, Any]


class ExclusionRequest(BaseModel):
    kind: str          # app | title | url
    value: str


class PauseRequest(BaseModel):
    minutes: Optional[float] = 60


class CheckRequest(BaseModel):
    """A hypothetical moment, for the panel's "would this be captured?" test."""
    app: Optional[str] = ""
    title: Optional[str] = ""
    url: Optional[str] = ""
    private_window: Optional[bool] = False
    secure_input: Optional[bool] = False


@router.get("")
async def state():
    return ambient.status()


@router.put("/policy")
async def set_policy(req: PolicyRequest):
    return {"policy": ambient.set_policy(req.policy)}


@router.post("/exclusions")
async def add_exclusion(req: ExclusionRequest):
    try:
        return {"policy": ambient.add_exclusion(req.kind, req.value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/exclusions/remove")
async def remove_exclusion(req: ExclusionRequest):
    try:
        return {"policy": ambient.remove_exclusion(req.kind, req.value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/pause")
async def pause(req: PauseRequest):
    return {"policy": ambient.pause_for(req.minutes or 60)}


@router.post("/resume")
async def resume():
    return {"policy": ambient.resume()}


@router.post("/check")
async def check(req: CheckRequest):
    """Would this window be captured? The panel's honesty test.

    Being able to try "Chase — Google Chrome" and see it refused, before
    trusting the feature with a real day, is the difference between a promise
    and a demonstration.
    """
    context = {**req.model_dump(), **ambient.probe_resources()}
    return {
        "decision": ambient.should_capture(context).as_dict(),
        "privacy": ambient.check_privacy(context).as_dict(),
        "resources": ambient.check_resources(context).as_dict(),
        "schedule": ambient.check_schedule(context).as_dict(),
    }
