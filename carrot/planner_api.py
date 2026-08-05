"""HTTP surface for semester planning.

The division of labour: the model reads the syllabus photo and knows where
buildings are; ``planner.py`` does the arithmetic. Anything a model gets wrong
here shows up as a schedule you cannot walk, so everything it returns is
validated before it is believed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from carrot import planner
from carrot.config import get_config

router = APIRouter(prefix="/api/planner", tags=["planner"])


class AnswersRequest(BaseModel):
    answers: Dict[str, Any]


class SyllabusRequest(BaseModel):
    # A photo of a schedule, or the text of one — people paste as often as
    # they screenshot.
    image: Optional[str] = None          # data URI or bare base64
    text: Optional[str] = None
    name: Optional[str] = "syllabus"


class UnderstandRequest(BaseModel):
    text: str


class CoursesRequest(BaseModel):
    courses: List[Dict[str, Any]]


class BuildingsRequest(BaseModel):
    school: Optional[str] = ""
    buildings: Dict[str, Any]


class PlanRequest(BaseModel):
    fixed: Optional[List[Dict[str, Any]]] = None


@router.get("/state")
async def state():
    """Everything the tab needs: what is known, what is missing, what is next."""
    stored = planner.profile()
    school = str(planner.answer_value(stored, "school", ""))
    known = planner.campus(school).get("buildings", {}) if school else {}
    missing = planner.missing_intake(stored)
    return {
        "answers": stored.get("answers", {}),
        "courses": stored.get("courses", []),
        "questions": list(planner.INTAKE),
        "missing": missing,
        "ready": planner.intake_complete(stored) and bool(stored.get("courses")),
        "next_question": missing[0] if missing else None,
        "school": school,
        "buildings_known": sorted(place["name"] for place in known.values()),
        "places_needed": planner.places_in(stored.get("courses", []), stored),
        "plan": planner.last_plan(),
    }


@router.put("/answers")
async def save_answers(req: AnswersRequest):
    stored = planner.save_answers(req.answers or {})
    missing = planner.missing_intake(stored)
    return {
        "answers": stored.get("answers", {}),
        "missing": missing,
        "next_question": missing[0] if missing else None,
        "ready": planner.intake_complete(stored),
    }


@router.post("/understand")
async def understand(req: UnderstandRequest):
    """Read a sentence into answers, and report what is still missing.

    This is the difference between a form and a conversation. The user says
    what they know in their own words; Carrot records what it can and comes
    back with the one thing it still needs, rather than fourteen boxes.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="say something first")

    from carrot import router as router_mod

    try:
        resolved = router_mod.route(task="extract")
        reply = "".join(
            event.get("text", "")
            for event in router_mod.stream_events(
                resolved,
                [{"role": "user", "content": planner.UNDERSTAND_PROMPT.format(text=text[:4000])}],
                tools=None)
            if event.get("type") in ("text", "content")
        )
        payload = planner.parse_json_block(reply)
    except planner.PlannerError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"could not read that: {exc}")

    # Only fields the intake actually knows about, and only non-empty ones —
    # a model that answers a question the user did not answer is the failure
    # this whole flow exists to avoid.
    known = {q["id"] for q in planner.INTAKE}
    understood = {
        key: str(value).strip()
        for key, value in (payload.get("answers") or {}).items()
        if key in known and str(value or "").strip()
    }
    stored = planner.save_answers(understood) if understood else planner.profile()
    missing = planner.missing_intake(stored)
    return {
        "understood": understood,
        "answers": stored.get("answers", {}),
        "missing": missing,
        "next_question": missing[0] if missing else None,
        "ready": not missing,
    }


@router.post("/syllabus")
async def read_syllabus(req: SyllabusRequest):
    """Turn a schedule photo or pasted text into structured courses.

    Nothing is saved automatically. Extraction from a photo is the step most
    likely to be subtly wrong — a misread room number produces a plan that
    sends you to the wrong building all term — so the courses come back for
    the user to confirm.
    """
    if not (req.image or req.text):
        raise HTTPException(status_code=400, detail="send an image or some text")

    from carrot import router as router_mod

    messages: List[Dict[str, Any]] = []
    images = []
    if req.image:
        images.append(_bare_base64(req.image))
    content = planner.EXTRACT_PROMPT
    if req.text:
        content = f"{planner.EXTRACT_PROMPT}\n\nSchedule:\n{req.text[:20000]}"
    message: Dict[str, Any] = {"role": "user", "content": content}
    if images:
        message["images"] = images
    messages.append(message)

    try:
        resolved = router_mod.route(task="extract")
        reply = "".join(
            event.get("text", "")
            for event in router_mod.stream_events(resolved, messages, tools=None)
            if event.get("type") in ("text", "content")
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"the model could not read it: {exc}")

    try:
        payload = planner.parse_json_block(reply)
    except planner.PlannerError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    courses = [c for c in payload.get("courses", []) if isinstance(c, dict)]
    if not courses:
        raise HTTPException(
            status_code=422,
            detail="no courses could be read from that. A clearer photo of the "
                   "schedule grid, or pasting the text, usually works.",
        )
    # Validate now rather than at plan time: a bad row should be visible while
    # the user is still looking at the extraction.
    blocks, problems = planner.courses_to_blocks(courses)
    return {
        "courses": courses,
        "meetings": len(blocks),
        "problems": problems,
        "vision": bool(images),
        "model": getattr(resolved, "model", ""),
    }


def _bare_base64(value: str) -> str:
    return value.split(",", 1)[1] if value.startswith("data:") else value


@router.put("/courses")
async def save_courses(req: CoursesRequest):
    blocks, problems = planner.courses_to_blocks(req.courses or [])
    planner.save_courses(req.courses or [])
    return {"courses": req.courses, "meetings": len(blocks), "problems": problems}


@router.post("/campus/lookup")
async def lookup_campus():
    """Ask the model where this campus's buildings are, and cache the answer."""
    stored = planner.profile()
    school = str(planner.answer_value(stored, "school", ""))
    if not school:
        raise HTTPException(status_code=400, detail="name the school first")
    places = planner.places_in(stored.get("courses", []), stored)
    if not places:
        raise HTTPException(status_code=400, detail="no places to look up yet")

    from carrot import router as router_mod

    prompt = planner.CAMPUS_PROMPT.format(
        school=school, places="\n".join(f"- {p}" for p in places))
    try:
        resolved = router_mod.route(task="extract")
        reply = "".join(
            event.get("text", "")
            for event in router_mod.stream_events(
                resolved, [{"role": "user", "content": prompt}], tools=None)
            if event.get("type") in ("text", "content")
        )
        payload = planner.parse_json_block(reply)
    except planner.PlannerError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"lookup failed: {exc}")

    entry = planner.save_buildings(school, payload.get("buildings", {}))
    found = entry.get("buildings", {})
    return {
        "school": school,
        "found": sorted(place["name"] for place in found.values()),
        # Being explicit about what is still unknown matters: those are the
        # walks the planner has to guess at.
        "still_unknown": [p for p in places if not planner.find_building(school, p)],
    }


@router.put("/campus")
async def set_buildings(req: BuildingsRequest):
    stored = planner.profile()
    school = req.school or str(planner.answer_value(stored, "school", ""))
    if not school:
        raise HTTPException(status_code=400, detail="name the school first")
    entry = planner.save_buildings(school, req.buildings or {})
    return {"school": school, "buildings": entry.get("buildings", {})}


@router.get("/travel")
async def travel(origin: str, destination: str):
    stored = planner.profile()
    school = str(planner.answer_value(stored, "school", ""))
    transport = str(planner.answer_value(stored, "transport", "walk"))
    return planner.travel_minutes(school, origin, destination, transport)


@router.post("/plan")
async def build_plan(req: PlanRequest):
    stored = planner.profile()
    missing = planner.missing_intake(stored)
    if missing:
        # Producing a beautiful schedule for a person who does not exist is the
        # failure mode; refusing with the question is the fix.
        raise HTTPException(
            status_code=400,
            detail=f"still need to know: {missing[0]['question']}",
        )
    if not stored.get("courses"):
        raise HTTPException(status_code=400, detail="add your classes first")
    try:
        plan = planner.plan_week(stored, stored["courses"],
                                 {"fixed": req.fixed or []})
    except planner.PlannerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    planner.save_plan(plan)
    return plan


@router.get("/plan")
async def get_plan():
    return planner.last_plan() or {"days": [], "blocks": [], "notes": []}
