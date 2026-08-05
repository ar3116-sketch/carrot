"""HTTP surface for multi-model debate.

A debate is slow by construction — N models, three rounds — so the run endpoint
streams its progress rather than holding a request open in silence for a
minute. Watching "propose → critique → synthesise" tick past is also the only
honest way to show what it is spending your time and tokens on.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from carrot import consensus

router = APIRouter(prefix="/api/consensus", tags=["consensus"])


class PanelRequest(BaseModel):
    members: List[Dict[str, Any]]


class SynthesiserRequest(BaseModel):
    provider: Optional[str] = ""
    model: Optional[str] = ""


class DebateRequest(BaseModel):
    question: str
    stream: Optional[bool] = True
    # Panel members can search for their own evidence. Defaults to the app's
    # configured chat search mode, so a debate is no more or less webby than a
    # normal turn unless asked.
    search_mode: Optional[str] = None


@router.get("")
async def state():
    return {
        "panel": consensus.panel(),
        "synthesiser": consensus.synthesiser(),
        "min_members": consensus.MIN_MEMBERS,
        "max_members": consensus.MAX_MEMBERS,
        "ready": len(consensus.panel()) >= consensus.MIN_MEMBERS,
        "history": consensus.history(),
    }


@router.put("/panel")
async def set_panel(req: PanelRequest):
    try:
        return {"panel": consensus.set_panel(req.members)}
    except consensus.ConsensusError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/synthesiser")
async def set_synthesiser(req: SynthesiserRequest):
    return {"synthesiser": consensus.set_synthesiser(req.model_dump())}


def _tooling(search_mode: Optional[str]):
    """The tools panel members may use, and the runner that executes them.

    Imported here rather than at module load because ``carrot.app`` imports
    this module — asking for it at the top would be a circular import.
    """
    from carrot import app as app_mod

    mode = app_mod.search_mode(search_mode)
    tools = app_mod._available_tools(mode)

    def run_tool(name: str, arguments: Dict[str, Any]) -> str:
        result = ""
        for event in app_mod._run_tool(name, arguments, None):
            if "_tool_result" in event:
                result = event["_tool_result"]
        return result

    return tools, run_tool, mode


@router.post("/debate")
async def debate(req: DebateRequest):
    from carrot import router as router_mod

    try:
        consensus.require_panel()
    except consensus.ConsensusError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    tools, run_tool, mode = _tooling(req.search_mode)

    if not req.stream:
        try:
            run = consensus.debate(req.question, router_mod.route, router_mod.stream_events,
                                   tools=tools, run_tool=run_tool)
        except consensus.ConsensusError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        consensus.save_run(run)
        return run

    # Streamed. The debate runs on a worker thread and pushes progress onto a
    # queue this generator drains, so the connection stays alive through a run
    # that can genuinely take a minute.
    events: "queue.Queue" = queue.Queue()
    events.put({"search_mode": mode})

    def work():
        try:
            run = consensus.debate(
                req.question, router_mod.route, router_mod.stream_events,
                on_event=events.put, tools=tools, run_tool=run_tool,
            )
            consensus.save_run(run)
            events.put({"done": run})
        except consensus.ConsensusError as exc:
            events.put({"error": str(exc)})
        except Exception as exc:
            events.put({"error": f"the debate failed: {exc}"})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True, name="carrot-consensus").start()

    def stream():
        while True:
            event = events.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
