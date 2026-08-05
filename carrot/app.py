"""Carrot AI — FastAPI application.

Consolidated API server exposing chat (streaming + non-streaming), bootstrap,
search, computer-use, terminal, recap, goals, reminders, notes, config,
leaderboard, and speech endpoints.
"""
from fastapi import FastAPI, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import re
import json
import queue
import threading

from carrot.database import get_db, init_db
from carrot import (
    config,
    search,
    conversation as conv_mod,
    ollama_client as ollama_mod,
    computer_use as cpu_mod,
    terminal as term_mod,
    recap as recap_mod,
    goals as goals_mod,
    reminders as rem_mod,
    notes as notes_mod,
    leaderboard as lb_mod,
    bootstrap as bootstrap_mod,
    hub as hub_mod,
    calfeed as calfeed_mod,
    attachments as attach_mod,
    artifacts as artifacts_mod,
    interop as interop_mod,
    deep_research as dr_mod,
    skills as skills_mod,
    mcp_client as mcp_mod,
    widgets as widgets_mod,
    health as health_mod,
    github_oauth as gh_mod,
    files_api,
    vectors as vectors_mod,
    memory as memory_mod,
    summarize as summarize_mod,
    indexer as indexer_mod,
    agent_tools as agent_mod,
    extensions as extensions_mod,
    doc_agent,
    router as router_mod,
    providers as providers_mod,
    security as security_mod,
    proactive as proactive_mod,
    backup as backup_mod,
    policy as policy_mod,
    research as research_mod,
    agent as carrot_agent,
    workspaces as workspaces_mod,
    coder as coder_mod,
    coder_api,
    media as media_mod,
    media_api,
    planner as planner_mod,
    planner_api,
    webhooks as webhooks_mod,
    webhooks_api,
    consensus as consensus_mod,
    consensus_api,
    ambient as ambient_mod,
    ambient_api,
    dualauth,
    gitops as gitops_mod,
    help as help_mod,
)
from carrot.speech import whisper_stt, kokoro_tts
from carrot.recap import DUCKDUCKGO_QUERY

app = FastAPI(title="Carrot AI", version="0.2.0")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

app.mount("/css", StaticFiles(directory=os.path.join(WEB_DIR, "css")), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(WEB_DIR, "js")), name="js")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")
os.makedirs(ASSETS_DIR, exist_ok=True)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
VENDOR_DIR = os.path.join(WEB_DIR, "vendor")
os.makedirs(VENDOR_DIR, exist_ok=True)
app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")


@app.middleware("http")
async def _revalidate_app_assets(request, call_next):
    """Make the app's own JS/CSS revalidate on every load.

    StaticFiles serves far-future-cacheable responses, and the Electron
    renderer honours that — so after an update the shell kept running the
    previous build's JavaScript against the new backend. Fonts and vendor
    bundles are content-addressed and huge, so they stay cacheable.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/js/", "/css/")) or path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif path.startswith(("/assets/fonts/", "/vendor/")):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

app.include_router(files_api.router)
app.include_router(coder_api.router)
app.include_router(media_api.router)
app.include_router(media_api.auth_router)
app.include_router(planner_api.router)
app.include_router(webhooks_api.router)
app.include_router(webhooks_api.public_router)
app.include_router(consensus_api.router)
app.include_router(ambient_api.router)


# ===== Pydantic request models =====

class SearchQuery(BaseModel):
    # None means "the active workspace"; "all" means every workspace.
    workspace: Optional[str] = None
    query: str
    limit: Optional[int] = 20
    hybrid_weight: Optional[float] = 0.5


class ClassifyQueryRequest(BaseModel):
    query: str


class RecapRequest(BaseModel):
    include_web_search: Optional[bool] = True
    model: Optional[str] = None


class VlmScanRequest(BaseModel):
    use_vlm: Optional[bool] = True
    scan_dirs: Optional[list] = None


class ChatAttachment(BaseModel):
    name: Optional[str] = ""
    mime: Optional[str] = ""
    data: str            # base64, with or without a data: URL prefix


class ChatRequest(BaseModel):
    message: str
    attachments: Optional[List[ChatAttachment]] = None
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    stream: Optional[bool] = False
    skill: Optional[str] = None
    task: Optional[str] = None
    provider: Optional[str] = None
    cloud: Optional[bool] = False
    # off | single | multi. Omitted means the saved default.
    search_mode: Optional[str] = None
    # File a newly created conversation into this workspace. The quick-ask
    # panel uses it to ask a question "in" a project without opening the app.
    workspace_id: Optional[str] = None
    # A chat that is answered but not remembered, and deleted afterwards.
    temporary: Optional[bool] = False


class AddMessageRequest(BaseModel):
    role: str
    content: str


class CreateConversationRequest(BaseModel):
    title: str = ""
    metadata: Dict[str, Any] = {}


class ConversationUpdateRequest(BaseModel):
    folder_id: Optional[str] = None
    starred: Optional[bool] = None
    title: Optional[str] = None


class FolderRequest(BaseModel):
    name: str


class CommandRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeout: Optional[int] = 30
    confirm: Optional[bool] = False


class GoalRequest(BaseModel):
    title: str
    description: str = ""
    category: str = ""
    metadata: Dict[str, Any] = {}


class DataPointRequest(BaseModel):
    value: Any
    label: str = ""
    metadata: Dict[str, Any] = {}


class ReminderRequest(BaseModel):
    title: str
    description: str = ""
    due_at: Optional[str] = None
    metadata: Dict[str, Any] = {}


class ReminderCompleteRequest(BaseModel):
    completed: Optional[bool] = True


class NoteRequest(BaseModel):
    title: str
    content: str = ""
    folder: str = ""


class NoteUpdateRequest(BaseModel):
    content: str
    title: Optional[str] = None


class SpeechTranscribeRequest(BaseModel):
    audio_base64: str


class SpeechSpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None


class ModelPullRequest(BaseModel):
    model: str


class ModelSelectRequest(BaseModel):
    model: str


class SkillRequest(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""
    slug: Optional[str] = None


class McpServerRequest(BaseModel):
    name: str
    command: str
    args: List[str] = []
    enabled: bool = True


class MemoryRequest(BaseModel):
    kind: str = "fact"
    subject: str
    content: str
    confidence: Optional[float] = 1.0
    pinned: Optional[bool] = False


class MemoryUpdateRequest(BaseModel):
    kind: Optional[str] = None
    subject: Optional[str] = None
    content: Optional[str] = None
    confidence: Optional[float] = None
    pinned: Optional[bool] = None
    status: Optional[str] = None


class MemoryExtractRequest(BaseModel):
    user_text: str
    assistant_text: Optional[str] = ""
    conversation_id: Optional[str] = None


class IndexDirRequest(BaseModel):
    path: str


class IndexScanRequest(BaseModel):
    force: Optional[bool] = False


class ApprovalRequest(BaseModel):
    decision: str
    remember: Optional[bool] = False
    # Typed back by the user for the handful of actions that move money or
    # destroy something. An allow without the right phrase is treated as a deny.
    confirmation: Optional[str] = ""


class ResearchRequest(BaseModel):
    question: str
    depth: Optional[str] = None
    conversation_id: Optional[str] = None


class AgentRunRequest(BaseModel):
    task: str
    surface: Optional[str] = "browser"
    conversation_id: Optional[str] = None
    max_steps: Optional[int] = None
    max_seconds: Optional[int] = None
    require_plan_approval: Optional[bool] = None


class DomainRequest(BaseModel):
    domain: str


class WorkspaceRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    folder_id: Optional[str] = None
    color: Optional[str] = None
    archived: Optional[bool] = None


class FolderNodeRequest(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None


class WorkspaceItem(BaseModel):
    kind: str
    item_id: str


class WorkspaceItemsRequest(BaseModel):
    items: List[WorkspaceItem] = []


class ActiveWorkspaceRequest(BaseModel):
    workspace_id: Optional[str] = ""


class SecretRequest(BaseModel):
    name: str
    value: str


class RouteRequest(BaseModel):
    task: str
    model: str
    provider: Optional[str] = None
    effort: Optional[str] = None


class DocSendRequest(BaseModel):
    """A note (or a selection from one) being sent to the agent."""

    text: str
    note_id: Optional[str] = None
    title: Optional[str] = None
    conversation_id: Optional[str] = None
    task: Optional[str] = None
    skill: Optional[str] = None
    # An explicit choice from the picker, overriding the note's own @/to.
    destination: Optional[str] = None
    option: Optional[str] = None
    search_mode: Optional[str] = None


class LatexRequest(BaseModel):
    source: str
    out_path: Optional[str] = None


class BibliographyRequest(BaseModel):
    bib: str
    tex: str = ""


class ProviderRequest(BaseModel):
    id: str
    label: str = ""
    kind: str = "openai"
    base_url: str = ""
    models: Optional[List[str]] = None
    env_var: str = ""
    api_key: Optional[str] = None


class ProviderKeyRequest(BaseModel):
    # Required, with no default. It used to default to "", which meant a body
    # naming the field anything else — a client typo — validated fine and
    # quietly cleared a working key while returning 200. Clearing a key is a
    # real operation and should have to be asked for explicitly.
    api_key: str


class ProviderEnabledRequest(BaseModel):
    enabled: bool = True


class TaskRequest(BaseModel):
    id: str
    label: str = ""
    description: str = ""
    local_only: bool = False


class BackupExportRequest(BaseModel):
    path: Optional[str] = None
    include_vectors: Optional[bool] = True


class BackupImportRequest(BaseModel):
    path: str
    safety_copy: Optional[bool] = True


class McpEnableRequest(BaseModel):
    enabled: bool = True


# ===== Startup =====

@app.on_event("startup")
def startup():
    init_db()
    os.makedirs(DB_DIR, exist_ok=True)
    # "Temporary" that survives a crash is not temporary, it is
    # usually-temporary. Sweeping at startup makes the promise unconditional.
    try:
        conv_mod.purge_temporary()
    except Exception:
        pass
    vectors_mod.migrate_legacy_embeddings()
    dr_mod.start_scheduler()
    proactive_mod.start_watcher()
    if config.get_config().get("index_on_startup", False) and indexer_mod.index_dirs():
        indexer_mod.start_scan_async()


@app.middleware("http")
async def require_session_token(request: Request, call_next):
    """Gate the API behind the session token.

    The bind to loopback stops the network, not the machine — any page in the
    browser can reach 127.0.0.1. The token lives in the app's own HTML, which
    the same-origin policy keeps out of reach of other origins.
    """
    if not security_mod.auth_enabled() or security_mod.is_public_path(request.url.path):
        return await call_next(request)

    presented = request.headers.get(security_mod.TOKEN_HEADER) or request.query_params.get(
        security_mod.TOKEN_QUERY_PARAM
    )
    if not security_mod.token_valid(presented):
        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid session token"},
        )
    return await call_next(request)


# ===== Index / static =====

def _asset_fingerprint() -> str:
    """A short hash of every JS/CSS file's size and mtime.

    Appended to asset URLs so an update changes the URL itself. Setting
    cache headers alone cannot fix an *already* cached response — the
    browser will not revalidate until the old entry expires — but a new
    URL is a new cache key, so the new build always wins.
    """
    import hashlib
    digest = hashlib.sha1()
    for folder in ("js", "css"):
        directory = os.path.join(WEB_DIR, folder)
        for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            try:
                stat = os.stat(os.path.join(directory, name))
            except OSError:
                continue
            digest.update(f"{name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
    return digest.hexdigest()[:10]


_ASSET_RE = re.compile(r'(src|href)="(/(?:js|css)/[^"?]+)"')


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as handle:
            html = handle.read()
        version = _asset_fingerprint()
        html = _ASSET_RE.sub(lambda m: f'{m.group(1)}="{m.group(2)}?v={version}"', html)
        return HTMLResponse(security_mod.inject_token(html))
    return HTMLResponse("<h1>Carrot</h1><p>Frontend not found. Run from project root.</p>")


# ===== Health / status =====

def app_version() -> str:
    """The installed version, for the UI and for bug reports.

    The build stamp written by scripts/build_installer.py wins: package
    metadata goes stale in an editable checkout, and a frozen app has no
    pyproject.toml to read.
    """
    try:
        from carrot._build import VERSION, COMMIT
        return f"{VERSION}+{COMMIT}" if COMMIT else VERSION
    except Exception:
        pass
    try:
        import tomllib
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), "rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("carrot")
    except Exception:
        return "unknown"


@app.get("/api/health")
async def health():
    # The build id makes "am I running the update?" answerable at a glance,
    # which guesswork repeatedly got wrong.
    return {"status": "healthy", "version": app_version(),
            "assets": _asset_fingerprint()}


@app.get("/api/status")
async def api_status():
    ollama = ollama_mod.OllamaClient()
    available = ollama.is_available()
    conn = get_db()
    conv_count = conn.execute("SELECT COUNT(*) as c FROM conversations").fetchone()["c"]
    msg_count = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
    goal_count = conn.execute("SELECT COUNT(*) as c FROM goals").fetchone()["c"]
    rem_count = conn.execute("SELECT COUNT(*) as c FROM reminders").fetchone()["c"]
    conn.close()

    default_model = config.get_config().get("ollama_model", bootstrap_mod.DEFAULT_MODEL)
    model_loaded = False
    if available:
        model_loaded = bootstrap_mod.is_model_available(default_model)

    return {
        "status": "ok",
        "ollama_available": available,
        "default_model": default_model,
        "model_loaded": model_loaded,
        "bootstrap_complete": bootstrap_mod.bootstrap_status().get("bootstrap_complete", False),
        "conversations": conv_count,
        "messages": msg_count,
        "goals": goal_count,
        "reminders": rem_count,
    }


# ===== Bootstrap =====

@app.get("/api/bootstrap/status")
async def bootstrap_status():
    return bootstrap_mod.bootstrap_status()


class BootstrapRunRequest(BaseModel):
    # Model chosen on the setup splash; omitted = configured/default model.
    model: str | None = None


@app.post("/api/bootstrap/run")
async def bootstrap_run(req: BootstrapRunRequest | None = None):
    try:
        return bootstrap_mod.run_bootstrap(model=req.model if req else None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bootstrap failed: {e}")


@app.get("/api/bootstrap/stream")
async def bootstrap_stream(model: str = ""):
    """Run bootstrap, streaming install and download progress as SSE.

    The setup splash needs a real progress bar: pulling a model is a
    multi-gigabyte download and a bar that jumps 30% -> 100% tells the
    user nothing. Bootstrap runs on a worker thread and pushes events
    through a queue so the response can stream while it works.
    """
    import queue as _queue
    import threading as _threading

    events: _queue.Queue = _queue.Queue()
    DONE = object()

    def worker():
        try:
            result = bootstrap_mod.run_bootstrap(
                progress_cb=events.put, model=model or None)
            events.put({"type": "done", **result})
        except Exception as e:
            events.put({"type": "done", "error": f"Bootstrap failed: {e}"})
        finally:
            events.put(DONE)

    _threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        while True:
            event = events.get()
            if event is DONE:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ===== Carrot Hub (hardware-aware model recommendations) =====

@app.get("/api/hub")
async def hub_overview(refresh: bool = False):
    """Detected specs, the fit-annotated catalog, and per-role picks."""
    try:
        return hub_mod.hub_overview(refresh=refresh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hub overview failed: {e}")


@app.get("/api/hub/search")
async def hub_search(workload: str = "", sort: str = "trending",
                     image: bool = False, audio: bool = False, video: bool = False,
                     limit: int = 20):
    """Thin-client live search: local specs + workload text, live HF fetch,
    local quant planning and fit filtering, ranked results."""
    modalities = [m for m, on in (("image", image), ("audio", audio), ("video", video)) if on]
    try:
        return hub_mod.live_search(workload=workload, sort=sort,
                                   modalities=modalities, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hub search failed: {e}")


@app.post("/api/hub/refresh")
async def hub_refresh():
    """Force a re-fetch of the daily catalog and the HF trending feed."""
    try:
        os.remove(hub_mod.HF_CACHE_PATH)
    except OSError:
        pass
    hub_mod.fetch_hf_trending(force=True)
    models = hub_mod.refresh_catalog(force=True)
    if models is None:
        return {"refreshed": False, "detail": "Carrot Hub unreachable — using cached/bundled catalog."}
    return {"refreshed": True, "count": len(models)}


@app.post("/api/hub/choose")
async def hub_choose(req: ModelSelectRequest):
    """Make a Hub pick the active model (pull it via /api/models/pull)."""
    config.set_config("ollama_model", req.model)
    installed = bootstrap_mod.is_model_available(req.model)
    return {"active_model": req.model, "installed": installed}


# ===== Models =====

# Curated catalog shown in the model picker for one-click install.
SUGGESTED_MODELS = [
    {"name": "gemma4:e4b", "label": "Gemma 4 E4B", "size_hint": "~4 GB", "blurb": "Default all-rounder"},
    {"name": "llama3.2:3b", "label": "Llama 3.2 3B", "size_hint": "~2 GB", "blurb": "Fast, light"},
    {"name": "qwen2.5-coder:7b", "label": "Qwen 2.5 Coder 7B", "size_hint": "~4.7 GB", "blurb": "Code-focused"},
    {"name": "mistral:7b", "label": "Mistral 7B", "size_hint": "~4.1 GB", "blurb": "General purpose"},
    {"name": "phi4:14b", "label": "Phi 4 14B", "size_hint": "~9.1 GB", "blurb": "Strong reasoning"},
    {"name": "deepseek-r1:8b", "label": "DeepSeek R1 8B", "size_hint": "~4.9 GB", "blurb": "Reasoning"},
]


@app.get("/api/models")
async def list_models():
    client = ollama_mod.OllamaClient()
    installed = client.list_models() if client.is_available() else []
    installed_names = {m["name"] for m in installed}
    cfg = config.get_config()
    active = cfg.get("ollama_model", bootstrap_mod.DEFAULT_MODEL)
    suggested = [
        {**m, "installed": m["name"] in installed_names}
        for m in SUGGESTED_MODELS
    ]

    # Models from configured cloud providers belong in the picker too —
    # a key you already pasted is useless if the UI only offers Ollama.
    #
    # A provider whose model list cannot be fetched (expired key, rate
    # limit, proxy) is still listed, carrying its error: dropping it
    # silently made cloud models look unsupported rather than unreachable,
    # and left no way to pick one by name.
    remote = []
    try:
        status = router_mod.status()
        for provider in status.get("providers", []):
            if not (provider.get("enabled") and provider.get("configured")):
                continue
            if provider.get("id") == "ollama":
                continue
            try:
                listed = providers_mod.list_models(provider["id"])
                names, error = listed.get("models", []), listed.get("error", "")
            except Exception as exc:
                names, error = [], str(exc)
            remote.append({
                "provider": provider["id"],
                "label": provider.get("label", provider["id"]),
                "models": names,
                "error": error,
            })
    except Exception:
        pass

    # Which provider/model currently serves chat (a pinned route wins).
    chat_provider, chat_model, chat_local = "ollama", active, True
    try:
        chat_route = router_mod.route("chat")
        chat_provider, chat_model = chat_route.provider, chat_route.model
        chat_local = chat_route.local
    except Exception:
        pass

    return {
        "installed": installed,
        "active_model": active,
        "default_model": bootstrap_mod.DEFAULT_MODEL,
        "suggested": suggested,
        "remote": remote,
        "chat_provider": chat_provider,
        "chat_model": chat_model,
        "chat_local": chat_local,
    }


@app.post("/api/models/select")
async def select_model(req: ModelSelectRequest):
    config.set_config("ollama_model", req.model)
    return {"active_model": req.model}


@app.post("/api/models/pull")
async def pull_model(req: ModelPullRequest):
    """Pull a model from the Ollama registry, streaming progress as SSE."""
    client = ollama_mod.OllamaClient()
    if not client.is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")

    def event_stream():
        try:
            for update in client.pull_model(req.model):
                payload = {
                    "status": update.get("status", ""),
                    "completed": update.get("completed"),
                    "total": update.get("total"),
                }
                if update.get("error"):
                    payload["error"] = update["error"]
                yield f"data: {json.dumps(payload)}\n\n"
            yield f"data: {json.dumps({'done': True, 'model': req.model})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ===== Storage & cleanup (installed models eat disk fast) =====

@app.get("/api/hub/storage")
async def hub_storage():
    """Disk usage per installed model plus overall disk headroom."""
    client = ollama_mod.OllamaClient()
    models = client.list_models() if client.is_available() else []
    models.sort(key=lambda m: m.get("size", 0), reverse=True)
    import shutil as _shutil
    usage = _shutil.disk_usage(os.path.expanduser("~"))
    active = config.get_config().get("ollama_model", "")
    return {
        "models": [{**m, "active": m["name"] == active} for m in models],
        "models_total_bytes": sum(m.get("size", 0) for m in models),
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
        "active_model": active,
    }


@app.post("/api/models/delete")
async def delete_model(req: ModelSelectRequest):
    """One-click purge of an installed model."""
    active = config.get_config().get("ollama_model", "")
    if req.model == active:
        raise HTTPException(status_code=400,
                            detail="That's the active model — switch to another model first.")
    client = ollama_mod.OllamaClient()
    if not client.is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")
    if not client.delete_model(req.model):
        raise HTTPException(status_code=500, detail=f"Could not delete {req.model}")
    return {"deleted": req.model}


# ===== Interop: Obsidian and VS Code / Cursor =====

class VaultRequest(BaseModel):
    vault_path: str


class NoteIdRequest(BaseModel):
    note_id: str


@app.get("/api/interop/status")
async def interop_status():
    return interop_mod.status()


@app.put("/api/interop/vault")
async def interop_set_vault(req: VaultRequest):
    path = os.path.abspath(os.path.expanduser(req.vault_path.strip())) if req.vault_path.strip() else ""
    if path and not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="That folder doesn't exist — paste your vault's full path.")
    config.set_config("obsidian_vault_path", path)
    return interop_mod.status()


@app.post("/api/interop/obsidian/send")
async def interop_send_note(req: NoteIdRequest):
    try:
        return interop_mod.send_note_to_obsidian(req.note_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/interop/obsidian/import")
async def interop_import_vault():
    try:
        return interop_mod.import_from_obsidian()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===== Calendar (secret iCal URL — no OAuth, no keys) =====

class CalendarConfigRequest(BaseModel):
    ics_url: str | None = None      # empty string disconnects
    enabled: bool | None = None
    agent_aware: bool | None = None


@app.get("/api/calendar/status")
async def calendar_status():
    return calfeed_mod.status()


@app.put("/api/calendar/config")
async def calendar_config(req: CalendarConfigRequest):
    if req.ics_url is not None:
        url = req.ics_url.strip()
        if url and not url.lower().startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="The calendar address must start with https://")
        config.set_config("calendar_ics_url", url)
        if url:
            config.set_config("calendar_enabled", True)
    if req.enabled is not None:
        config.set_config("calendar_enabled", req.enabled)
    if req.agent_aware is not None:
        config.set_config("calendar_agent_aware", req.agent_aware)
    return calfeed_mod.status()


@app.get("/api/calendar/events")
async def calendar_events(days: int = 14):
    if not config.get_config().get("calendar_enabled", False):
        return {"configured": False, "events": []}
    events = calfeed_mod.upcoming_events(days=max(1, min(days, 90)))
    if events is None:
        return {"configured": bool(config.get_config().get("calendar_ics_url")), "events": [],
                "detail": "Calendar not configured or unreachable."}
    return {"configured": True, "events": events}


@app.post("/api/calendar/refresh")
async def calendar_refresh():
    events = calfeed_mod.upcoming_events(days=14, force=True)
    if events is None:
        return {"ok": False, "detail": "Could not fetch the calendar — check the secret address."}
    return {"ok": True, "count": len(events)}


# ===== Chat =====

def _coder_context(conversation_id: str = ""):
    """Mode guidance, the compacted plan, and the workspace's own rules.

    Only emitted when there is something to say: a chat about dinner should not
    carry a plan/act preamble, and an empty rules block is noise in every turn.
    """
    blocks = []
    cfg = config.get_config()
    mode = coder_mod.normalize_mode(cfg.get("coder_mode"))
    if mode == coder_mod.MODE_PLAN:
        blocks.append({"role": "system", "content": coder_mod.MODE_PREAMBLE[mode]})
    else:
        # The brief replaces the planning transcript rather than joining it:
        # by Act time the reading and grepping has served its purpose, and
        # carrying it forward spends the window on transcript.
        brief = coder_mod.snapshot_for(conversation_id)
        if brief:
            blocks.append({
                "role": "system",
                "content": f"{coder_mod.SNAPSHOT_HEADER}\n\n{brief}",
            })
    if cfg.get("agent_tools_enabled", True):
        rules = coder_mod.load_rules(agent_mod.workspace_root())
        if rules:
            blocks.append({"role": "system", "content": rules})
    return blocks


def _prepare_history(conv, message, skill_slug, extra_system=None, mode=None,
                     images=None):
    """Build the model message list for a turn.

    Order matters: the search directive, then skill instructions, then any
    caller-supplied context (a document's cited files, say), then what Carrot
    remembers about the user, then the rolling summary of everything older than
    the recent window, then the recent turns verbatim. Long conversations
    therefore keep their early context instead of falling off a fixed-size slice.
    """
    history = []
    skill = None
    if mode:
        history.append({"role": "system", "content": search_directive(mode)})
    if skill_slug:
        skill = skills_mod.get_skill(skill_slug)
        if skill and skill.get("instructions"):
            history.append({
                "role": "system",
                "content": (
                    f"The user invoked the '{skill['name']}' skill. "
                    f"Follow these instructions:\n\n{skill['instructions']}"
                ),
            })

    if extra_system:
        history.append({"role": "system", "content": extra_system})

    # The coding agent's mode, and whatever instruction files the workspace
    # carries. A repo already set up for Cline, Continue, Goose or Cursor is
    # already set up for Carrot — its rules are read, not re-authored.
    try:
        history.extend(_coder_context(conv.get("id", "") if conv else ""))
    except Exception:
        pass

    # Calendar awareness is an explicit opt-in; when on, the assistant sees
    # the next few days so "what does my week look like" just works.
    try:
        cal_block = calfeed_mod.agent_context()
        if cal_block:
            history.append({"role": "system", "content": cal_block})
    except Exception:
        pass

    if config.get_config().get("memory_enabled", True):
        try:
            # Recall is scoped to the workspace the conversation lives in, not
            # the one that happens to be active — re-opening an old chat should
            # bring back its own context, not today's.
            block = memory_mod.as_prompt_block(memory_mod.recall(
                message,
                workspace_id=workspaces_mod.workspace_of(
                    workspaces_mod.KIND_CONVERSATION, conv.get("id", "")
                ) or "",
            ))
            if block:
                history.append({"role": "system", "content": block})
        except Exception:
            pass

    history += summarize_mod.build_history(conv)
    user_turn = {"role": "user", "content": message}
    if images:
        # Ollama takes base64 images on the message itself.
        user_turn["images"] = images
    history.append(user_turn)
    return history, skill


MAX_TOOL_ROUNDS = 8
# Multi-turn search needs room to search, read, notice a gap, and search again.
# Eight rounds is one pass; a real follow-up loop runs out halfway through.
MAX_TOOL_ROUNDS_MULTI = 16

# What "multi-turn" has to have actually done before an answer is accepted.
# The directive alone does not achieve this: a small on-device model reads
# "do not stop at the first set of results", searches once, and answers from
# the snippets anyway. These gates are checked in code, so the mode means the
# same thing whatever model is behind it.
MULTI_MIN_SEARCHES = 2
MULTI_MIN_READS = 1
# How many times we push back before taking what we are given. Without a cap a
# model that simply cannot use read_url would loop until the round budget ran
# out and the user would get nothing at all.
MAX_GATE_NUDGES = 3

GATE_NUDGE_NO_READ = (
    "You have search results but have not opened any of them. Snippets are not "
    "an answer — they are a list of places an answer might be. Call read_url on "
    "the results most likely to contain the specifics, then answer from what the "
    "pages actually say."
)
GATE_NUDGE_ONE_SEARCH = (
    "That was one search. Before answering, name what you still cannot answer "
    "from what you have, and run another search aimed at exactly that gap — "
    "using the words a source would use, not the words of the question."
)


def _search_gate_gap(searches: int, reads: int) -> Optional[str]:
    """What multi-turn still owes the user, if anything.

    Returned as the nudge to send back to the model; None once the mode has
    done what its name claims.
    """
    if reads < MULTI_MIN_READS:
        return GATE_NUDGE_NO_READ
    if searches < MULTI_MIN_SEARCHES:
        return GATE_NUDGE_ONE_SEARCH
    return None


def _query_drifted(question: str, query: str) -> bool:
    """Did the model search for something other than what was asked?

    The failure this catches is not subtle — a question about the F-15EX
    program coming back with a search for "current American political news".
    A generated query that shares no content word with the question is not a
    rephrasing, it is a different question, and the results cannot answer the
    one that was asked.
    """
    from . import websearch
    asked = websearch._terms(question)
    if not asked or not query.strip():
        return False
    return not (asked & websearch._terms(query))


QUERY_DRIFT_CORRECTION = (
    "That search was not run: the query {query!r} shares no word with what the "
    "user actually asked ({question!r}). Search for the user's question, using "
    "its specific names, models and numbers."
)

# ===== Chat search modes =====
#
# Whether a chat turn may reach the web at all, and how hard it should try.
# Off is a real setting, not a formality: a question about your own notes gets
# worse, not better, when the model decides to search the web first.

SEARCH_OFF = "off"
SEARCH_SINGLE = "single"
SEARCH_MULTI = "multi"

SEARCH_MODES = {
    SEARCH_OFF: {
        "label": "No search",
        "help": "Carrot answers from the conversation, your files and its memory. It never reaches the web.",
        "tools": set(),
    },
    SEARCH_SINGLE: {
        "label": "Search",
        "help": "Carrot may search the web and read a page when the question needs something current.",
        "tools": {"web_search", "read_url"},
    },
    SEARCH_MULTI: {
        "label": "Multi-turn search",
        "help": "Carrot searches, reads, works out what is still missing, and searches again — "
                "and can hand the whole question to Carrot Research.",
        "tools": {"web_search", "read_url", "start_research"},
    },
}

ALL_SEARCH_TOOLS = set().union(*(mode["tools"] for mode in SEARCH_MODES.values()))

# Prepended to both search directives. The date one is not optional advice:
# a model's "now" is its training cutoff, so asked for recent news it will
# search without a year, accept a 2020 satire page as current, and never
# notice. The source note exists because a free search backend returns content
# farms — sites that reword scraped text and match the query wording closely,
# which is exactly what relevance filtering cannot catch.
SEARCH_PREAMBLE = (
    "Before searching for anything time-sensitive — recent, current, latest, "
    "upcoming, 'now', or anything with a year in it — call current_datetime "
    "first. You do not know today's date; your sense of it is your training "
    "cutoff. Once you know it, put the year in the query and reject results "
    "that turn out to be older.\n"
    "Prefer sources with a name behind them: wire services, established "
    "outlets, official and academic sites, project documentation. Results are "
    "already ordered with those first. If the only results are sites you do "
    "not recognise, say that rather than reporting their contents as fact.\n"
)

MULTI_SEARCH_DIRECTIVE = (
    SEARCH_PREAMBLE +
    "Search mode: multi-turn. Do not stop at the first set of results.\n"
    "- Search, read the pages that look most likely to answer the question, then ask "
    "yourself what you still cannot answer, and search again for exactly that.\n"
    "- Two or three focused rounds beat one broad one. Use the words a source would "
    "use, not the words of the question.\n"
    "- Cite the URL for anything you learned from a page, and say plainly when the "
    "sources disagree or when you could not find something.\n"
    "- If the question is big enough to deserve a written report with checked "
    "citations, call start_research instead of doing it by hand."
)

SINGLE_SEARCH_DIRECTIVE = (
    SEARCH_PREAMBLE +
    "Search mode: single-pass. You may search the web and read a page when the "
    "question needs current information or a source you do not already have. "
    "Cite the URL for anything you take from a page. Do not search for things you "
    "already know or that are in the conversation."
)

NO_SEARCH_DIRECTIVE = (
    "Search mode: off. You have no web access this turn. Answer from the "
    "conversation, the user's indexed files, and what you remember about them. "
    "If the answer genuinely needs something from the web, say so rather than "
    "guessing — the user can switch search on."
)


def search_mode(requested: Optional[str] = None) -> str:
    """The mode for this turn: the request's choice, else the saved default."""
    mode = (requested or config.get_config().get("chat_search_mode", SEARCH_SINGLE) or "").lower()
    return mode if mode in SEARCH_MODES else SEARCH_SINGLE


def search_directive(mode: str) -> str:
    return {
        SEARCH_OFF: NO_SEARCH_DIRECTIVE,
        SEARCH_SINGLE: SINGLE_SEARCH_DIRECTIVE,
        SEARCH_MULTI: MULTI_SEARCH_DIRECTIVE,
    }[mode]


def _available_tools(mode: str = SEARCH_SINGLE):
    """Built-in tools, enabled extension packs, and every enabled MCP server.

    The search mode subtracts rather than adds: every non-web tool is always
    offered, and the web ones are filtered to what this mode allows. Removing
    the tool is what makes "off" mean off — an instruction not to search is a
    request, but a tool that is not in the list cannot be called.
    """
    allowed = set(agent_mod.TOOLS) - ALL_SEARCH_TOOLS | SEARCH_MODES[mode]["tools"]
    tools = list(agent_mod.ollama_tools(enabled=sorted(allowed)))
    try:
        tools += extensions_mod.ollama_tools()
    except Exception:
        pass
    try:
        tools += mcp_mod.ollama_tools()
    except Exception:
        pass
    return _apply_coder_mode(tools)


def _apply_coder_mode(tools):
    """In plan mode, take the write tools away rather than asking nicely.

    Cline's plan/act split only means anything if it is enforced by the tool
    list: a model that *can* write eventually will, however the prompt is
    worded. This subtracts, so it also covers pack and MCP tools whose names
    happen to be write tools.
    """
    try:
        mode = coder_mod.normalize_mode(config.get_config().get("coder_mode"))
    except Exception:
        return tools
    if mode == coder_mod.MODE_ACT:
        return tools
    names = [t.get("function", {}).get("name", "") for t in tools]
    keep = set(coder_mod.tools_for_mode(names, mode))
    return [t for t in tools if t.get("function", {}).get("name", "") in keep]


def _run_tool(name, args, conversation_id):
    """Run one tool, forwarding any approval prompt it raises to the stream.

    Built-in mutating tools block on user approval, and the prompt has to reach
    the browser *while* they are blocked. The tool runs on a worker thread and
    pushes events onto a queue this generator drains, so the SSE stream stays
    live for the whole wait.
    """
    # Removing a tool's declaration is the first line of defence, but a small
    # model will still emit a call for a name it saw in training and was never
    # offered. Reject it here, structured, so it reads as a protocol error and
    # gets corrected rather than apologised for.
    try:
        refusal = coder_mod.reject_tool(
            name, config.get_config().get("coder_mode")
        )
    except Exception:
        refusal = None
    if refusal:
        yield {"_tool_result": json.dumps(refusal)}
        return

    events = queue.Queue()
    outcome = {}

    def work():
        try:
            if agent_mod.is_builtin(name):
                outcome["result"] = agent_mod.call(name, args, conversation_id, events.put)
            elif extensions_mod.is_extension_tool(name):
                outcome["result"] = extensions_mod.call(name, args, conversation_id, events.put)
            else:
                outcome["result"] = mcp_mod.call_namespaced_tool(name, args)
        except Exception as exc:
            outcome["result"] = f"error: {exc}"
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True, name="carrot-tool").start()
    while True:
        event = events.get()
        if event is None:
            break
        yield event
    yield {"_tool_result": outcome.get("result", "")}


# How much of each source is kept for the forced-answer digest. Six sources at
# 1200 characters is ~2k tokens, which fits in a 4k-context local model with
# room to write — the full transcript emphatically does not.
EVIDENCE_CHARS = 1200
MAX_EVIDENCE_SOURCES = 6

FORCED_ANSWER_PROMPT = (
    "Answer the question below using only the notes that follow. Write the "
    "answer in plain text now — do not think silently, do not ask to search "
    "again, and do not apologise. If the notes do not answer it, say exactly "
    "what is missing and what they did cover.\n\n"
    "QUESTION: {question}\n\nNOTES:\n{notes}"
)


def _forced_answer(resolved, question, evidence):
    """One last, small ask: answer from a digest rather than the transcript.

    Yields the usual stream events, plus a final ``{"_answer": text}``. The
    prompt is rebuilt from scratch instead of appended to the working history,
    because the history is exactly what made the model run out of room.
    """
    notes = "\n\n".join(
        f"[{item['tool']}] {item['source']}\n{item['text']}"
        for item in evidence[-MAX_EVIDENCE_SOURCES:]
    ) or "(nothing was successfully gathered)"
    messages = [{
        "role": "user",
        "content": FORCED_ANSWER_PROMPT.format(question=question[:500], notes=notes),
    }]
    parts = []
    try:
        for event in router_mod.stream_events(resolved, messages, tools=None):
            if event["type"] == "thinking":
                yield {"thinking": event["text"]}
            elif event["type"] != "tool_calls":
                parts.append(event["text"])
    except Exception as exc:
        # A failed retry must not become an exception the user sees instead of
        # an answer; the deterministic fallback below still runs.
        yield {"thinking": f"(could not re-ask the model: {exc})"}
    yield {"_answer": "".join(parts)}


def _evidence_answer(question, evidence):
    """Write something true and useful without the model, as a last resort.

    This is not a good answer, and it does not pretend to be. It is the
    difference between "(no response)" — which tells the user nothing and
    wastes everything the turn did — and a list of what was actually found,
    which they can act on.
    """
    if not evidence:
        return (
            "I could not gather anything usable for that. Every source I tried "
            "either refused the request or returned nothing readable. Trying a "
            "narrower question, or sending this to Research, usually gets past it."
        )
    queries = [e["source"] for e in evidence if e["tool"] == "web_search" and e["source"]]
    pages = [e["source"] for e in evidence if e["tool"] == "read_url" and e["source"]]
    lines = [
        f"I gathered the material for this but could not write it up — the model "
        f"ran out of room to answer. Here is what the turn actually collected, "
        f"so it is not wasted:",
        "",
    ]
    if queries:
        lines.append("**Searched:** " + ", ".join(dict.fromkeys(queries))[:400])
    if pages:
        lines.append("**Read:**")
        lines += [f"- {url}" for url in list(dict.fromkeys(pages))[:8]]
    excerpt = next((e["text"] for e in evidence if e["tool"] == "read_url"), "")
    if excerpt:
        lines += ["", "**From the first page read:**", "", excerpt[:700].strip()]
    lines += [
        "",
        "A model with a larger context window, or sending this to Research, will "
        "get you the written answer.",
    ]
    return "\n".join(lines)


def _agentic_chat_events(history, resolved, skill=None, conversation_id=None, mode=SEARCH_SINGLE):
    """Yield SSE dicts for one chat turn, running the tool-calling loop.

    Tool calls are dispatched to built-in tools or MCP by name prefix, surfaced
    to the UI as they happen, and fed back to the model for a bounded number of
    rounds before the final answer streams as `chunk` events. Multi-turn search
    gets a larger round budget because searching, reading and re-searching is
    several rounds on its own.
    """
    if skill:
        yield {"skill": {"slug": skill["slug"], "name": skill["name"]}}
    yield {"route": resolved.as_dict()}
    yield {"search_mode": mode}

    tools = _available_tools(mode)
    rounds = MAX_TOOL_ROUNDS_MULTI if mode == SEARCH_MULTI else MAX_TOOL_ROUNDS
    working = list(history)
    question = next((m["content"] for m in reversed(history) if m.get("role") == "user"), "")

    gated = mode == SEARCH_MULTI
    searches = reads = nudges = 0
    final_text = ""
    stalled = False
    # What the turn actually gathered, kept small on purpose. This is what a
    # forced answer is written from, so it has to fit in a context window that
    # the full transcript already overran.
    evidence = []

    # In gated mode the model's prose is held back until the gates are met,
    # because a premature answer is one we intend to throw away — streaming it
    # first would print an answer to the user and then silently replace it.
    def emit_text(text):
        return [] if gated else [{"chunk": text}]

    for _ in range(rounds):
        content_parts = []
        tool_calls = []
        for event in router_mod.stream_events(resolved, working, tools=tools or None):
            if event["type"] == "thinking":
                yield {"thinking": event["text"]}
            elif event["type"] == "tool_calls":
                tool_calls.extend(event["calls"])
            else:
                content_parts.append(event["text"])
                for out in emit_text(event["text"]):
                    yield out

        content_str = "".join(content_parts)

        if not tool_calls:
            # The model wants to finish. In multi-turn that is only allowed
            # once it has actually searched and read.
            gap = _search_gate_gap(searches, reads) if gated else None
            if gap and nudges < MAX_GATE_NUDGES:
                nudges += 1
                yield {"gate": {"reason": gap, "searches": searches, "reads": reads}}
                working.append({"role": "assistant", "content": content_str})
                working.append({"role": "user", "content": gap})
                continue
            if gap:
                # Out of nudges: the model cannot or will not do it. Keep the
                # answer, but say so rather than passing it off as researched.
                stalled = True
            final_text = content_str
            break

        working.append({"role": "assistant", "content": content_str, "tool_calls": tool_calls})
        for call in tool_calls:
            function = call.get("function", {})
            name = function.get("name", "")
            args = function.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            # Tools are offered to the model namespaced as `carrot__web_search`,
            # so every comparison below has to be against the bare name. Getting
            # this wrong meant the gate counters never incremented: the model
            # searched, read six pages, and was told after each one that it had
            # not searched yet — which is exactly the thrashing users saw, and
            # why the turn always ended stalled with nothing written.
            bare = name.split("__", 1)[-1]

            # A query about something else cannot answer the question asked.
            # Refuse it and say why, instead of spending a round on it.
            if bare == "web_search" and _query_drifted(question, str(args.get("query", ""))):
                correction = QUERY_DRIFT_CORRECTION.format(
                    query=str(args.get("query", "")), question=question[:200])
                yield {"tool": {"name": name, "args": args, "rejected": True}}
                working.append({
                    "role": "tool", "content": correction,
                    "name": name, "tool_call_id": call.get("id", name),
                })
                continue

            yield {"tool": {"name": name, "args": args}}
            result = ""
            for event in _run_tool(name, args, conversation_id):
                if "_tool_result" in event:
                    result = event["_tool_result"]
                else:
                    yield event
            if bare == "web_search":
                searches += 1
            elif bare == "read_url":
                reads += 1
            yield {"tool_result": {"name": name, "result": result[:2000]}}
            if bare in ("web_search", "read_url") and not result.startswith("error:"):
                evidence.append({
                    "tool": bare,
                    "source": str(args.get("url") or args.get("query") or ""),
                    "text": result[:EVIDENCE_CHARS],
                })
            working.append(
                {
                    "role": "tool",
                    "content": result,
                    "name": name,
                    "tool_call_id": call.get("id", name),
                }
            )
    else:
        # Every round went to tool calls and the budget ran out, so the model
        # was never asked to write an answer.
        stalled = True

    # An empty answer is never acceptable, and the first attempt at this fix
    # was not enough. Re-asking with the *same* history fails the same way:
    # by then `working` holds several full web pages, which overruns a small
    # local model's context window, and a model with no room left produces
    # nothing. So the retry is asked with a compact digest instead of the
    # transcript — a few thousand characters that fit anywhere.
    if not final_text.strip():
        final_text = ""
        for event in _forced_answer(resolved, question, evidence):
            if "_answer" in event:
                final_text = event["_answer"]
            else:
                yield event
        # And if even that returns nothing, the answer is written from the
        # evidence here, deterministically. "(no response)" after the user
        # watched ten searches scroll past is the worst outcome in the app,
        # and it must not be reachable.
        if not final_text.strip():
            final_text = _evidence_answer(question, evidence)
        for out in emit_text(final_text):
            yield out
        stalled = True

    if gated:
        # Held back above; send the answer we actually kept.
        if final_text:
            yield {"chunk": final_text}
        # The user escalates to Research by hand — this only offers it, and
        # only when the turn was visibly thin.
        if stalled or reads < MULTI_MIN_READS or searches < MULTI_MIN_SEARCHES:
            yield {"suggest_research": {
                "question": question,
                "reason": "this turn answered from "
                          f"{searches} search(es) and {reads} page(s) read",
            }}

    yield {"_final_text": final_text}


def _post_turn(conversation_id, user_message, assistant_text, message_id):
    """Extract memories and refresh the rolling summary after a turn.

    Both call the model, so they run on a worker thread — the user gets their
    answer without waiting for Carrot's bookkeeping.
    """
    def work():
        # A temporary chat is exempt from all of it. This is the only place
        # that decides, so there is one thing to get right rather than three.
        try:
            if conv_mod.is_temporary(conversation_id):
                return
        except Exception:
            pass
        settings = config.get_config()
        if settings.get("memory_enabled", True):
            try:
                memory_mod.extract_from_turn(
                    user_text=user_message,
                    assistant_text=assistant_text,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    min_confidence=settings.get("memory_min_confidence", 0.6),
                )
            except Exception:
                pass
        if settings.get("summarize_enabled", True):
            try:
                summarize_mod.maybe_summarize(conversation_id)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True, name="carrot-post-turn").start()


def _open_conversation(req):
    """Resolve (or create) the conversation a chat request targets."""
    if req.conversation_id is None:
        temporary = bool(getattr(req, "temporary", False))
        created = conv_mod.create_conversation(
            title=req.message[:80],
            metadata={conv_mod.TEMPORARY_KEY: True} if temporary else None,
        )
        req.conversation_id = created["id"]
        # Only on creation: filing an existing conversation elsewhere because
        # of one message would move it out from under the user. A temporary
        # chat is filed nowhere at all.
        workspace_id = None if temporary else getattr(req, "workspace_id", None)
        if workspace_id:
            try:
                workspaces_mod.file_item(
                    workspaces_mod.KIND_CONVERSATION, created["id"], workspace_id)
            except Exception:
                pass          # a stale workspace id must not lose the message
    conv = conv_mod.get_conversation(req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _resolve_chat_route(req):
    """Pick the model for this turn, failing early if nothing can serve it."""
    resolved = router_mod.route(
        task=getattr(req, "task", None) or router_mod.TASK_CHAT,
        model=req.model,
        provider=getattr(req, "provider", None),
        prefer_cloud=bool(getattr(req, "cloud", False)),
    )
    if resolved.local:
        client = ollama_mod.OllamaClient()
        if not client.is_available():
            raise HTTPException(status_code=503, detail="Ollama is not available")
        # Naming a model Ollama has never pulled used to fail deep inside the
        # stream as an empty answer. Say which model is missing, and where it
        # would have to come from, before a single token is spent.
        _require_installed_model(client, resolved.model)
    return resolved


def _require_installed_model(client, model: str) -> None:
    """Fail early, and legibly, when an on-device model is not installed."""
    if not model:
        return
    try:
        installed = {m.get("name", "") for m in client.list_models()}
    except Exception:
        return  # Listing is best-effort; never block a turn on it.
    if not installed or model in installed:
        return
    # Ollama treats a bare name as ":latest", so match that way round too.
    base = model.split(":", 1)[0]
    if any(name == model or name.split(":", 1)[0] == base for name in installed):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"'{model}' is not installed on this machine. Pull it from the model "
            f"picker, or pick a hosted provider's model in Settings → Providers."
        ),
    )


def _chat_stream_response(req, conv, history, skill, resolved, prelude=None, mode=SEARCH_SINGLE):
    """Shared SSE body for the chat and doc-send endpoints.

    ``prelude`` is emitted as the first event, which is how a doc send reports
    its resolved citations before any tokens arrive.
    """
    async def stream():
        final_text = ""
        if prelude:
            yield f"data: {json.dumps({'document': prelude})}\n\n"
        for event in _agentic_chat_events(history, resolved, skill, req.conversation_id, mode):
            if "_final_text" in event:
                final_text = event["_final_text"]
                continue
            yield f"data: {json.dumps(event)}\n\n"
        stored = conv_mod.add_message(req.conversation_id, "assistant", final_text)
        _post_turn(
            req.conversation_id, req.message, final_text,
            stored.get("id") if isinstance(stored, dict) else None,
        )
        yield f"data: {json.dumps({'done': True, 'conversation_id': req.conversation_id})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")



def _apply_attachments(req, resolved):
    """Turn a request's attachments into (images, extra_system, note).

    Documents become prompt text and work with any model. Images only go
    through when the resolved model can actually see — otherwise this
    raises, because a model that silently ignores your screenshot and
    answers anyway is worse than a clear error.
    """
    raw = [a.model_dump() if hasattr(a, "model_dump") else dict(a)
           for a in (req.attachments or [])]
    if not raw:
        return None, None, ""
    try:
        images, documents = attach_mod.process(raw)
    except attach_mod.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if images:
        model = getattr(resolved, "model", "") or ""
        if getattr(resolved, "local", True):
            can_see = ollama_mod.OllamaClient().supports_vision(model)
        else:
            can_see = attach_mod.model_supports_vision(model)
        if not can_see:
            raise HTTPException(
                status_code=400,
                detail=(f"{model} cannot read images. Pick a vision model — "
                        "the Model Hub's 'image: Y' filter lists the ones that fit "
                        "your machine — or attach a PDF or text file instead."),
            )
    return images or None, attach_mod.documents_prompt(documents) or None, \
        attach_mod.describe(images, documents)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    conv = _open_conversation(req)
    resolved = _resolve_chat_route(req)
    mode = search_mode(req.search_mode)
    images, docs_system, note = _apply_attachments(req, resolved)
    history, skill = _prepare_history(conv, req.message, req.skill,
                                      extra_system=docs_system, mode=mode, images=images)
    conv_mod.add_message(req.conversation_id, "user",
                         f"{req.message}\n\n[{note}]" if note else req.message)

    if req.stream:
        return _chat_stream_response(req, conv, history, skill, resolved, mode=mode)

    # The non-streaming path used to call the model once, directly, with no
    # tools at all — so the quick-ask overlay, which is the only caller, could
    # not search, could not read a page, and had none of the never-answer-with-
    # nothing guarantees the streaming path has. It runs the same loop now and
    # collects the result, so the two doors into chat behave the same.
    parts, tools_used, route_info = [], [], resolved.as_dict()
    final = ""
    for event in _agentic_chat_events(history, resolved, skill, req.conversation_id, mode):
        if "_final_text" in event:
            final = event["_final_text"]
        elif "chunk" in event:
            parts.append(event["chunk"])
        elif "tool" in event:
            tools_used.append(event["tool"].get("name", ""))
    response = final or "".join(parts)
    stored = conv_mod.add_message(req.conversation_id, "assistant", response)
    _post_turn(
        req.conversation_id, req.message, response,
        stored.get("id") if isinstance(stored, dict) else None,
    )
    return {
        "conversation_id": req.conversation_id,
        "response": response,
        "route": route_info,
        "tools": tools_used,
        "search_mode": mode,
    }


@app.get("/api/chat/search-modes")
async def list_search_modes():
    """The three search postures and which one is currently the default."""
    return {
        "modes": [
            {"id": name, "label": spec["label"], "help": spec["help"],
             "tools": sorted(spec["tools"])}
            for name, spec in SEARCH_MODES.items()
        ],
        "current": search_mode(),
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Dedicated SSE streaming endpoint."""
    conv = _open_conversation(req)
    resolved = _resolve_chat_route(req)
    mode = search_mode(req.search_mode)
    images, docs_system, note = _apply_attachments(req, resolved)
    history, skill = _prepare_history(conv, req.message, req.skill,
                                      extra_system=docs_system, mode=mode, images=images)
    conv_mod.add_message(req.conversation_id, "user",
                         f"{req.message}\n\n[{note}]" if note else req.message)
    return _chat_stream_response(req, conv, history, skill, resolved, mode=mode)


# ===== Conversations =====

@app.get("/api/conversations")
async def list_conversations(limit: int = 50, workspace: Optional[str] = None):
    """Recent conversations, scoped to the active workspace unless told otherwise.

    Returns a bare list rather than an envelope — three callers already expect
    that shape, and the UI knows the active workspace without being told again.
    """
    return conv_mod.list_conversations(
        limit=limit, workspace_id=workspaces_mod.resolve_scope(workspace)
    )


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = conv_mod.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@app.post("/api/conversations")
async def create_conversation(req: CreateConversationRequest = CreateConversationRequest()):
    return conv_mod.create_conversation(title=req.title, metadata=req.metadata)


@app.post("/api/conversations/{conv_id}/messages")
async def add_message(conv_id: str, req: AddMessageRequest):
    try:
        msg = conv_mod.add_message(conv_id, req.role, req.content)
        return msg
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, req: ConversationUpdateRequest):
    result = conv_mod.update_conversation_meta(
        conv_id, folder_id=req.folder_id, starred=req.starred, title=req.title
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return result


@app.post("/api/conversations/temporary/purge")
async def purge_temporary_conversations():
    """Delete every temporary chat now, without waiting for a restart."""
    return {"deleted": conv_mod.purge_temporary()}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    if not conv_mod.delete_conversation(conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"status": "deleted"}


# ===== Chat folders =====

@app.get("/api/chat-folders")
async def list_chat_folders():
    return conv_mod.list_folders()


@app.post("/api/chat-folders")
async def create_chat_folder(req: FolderRequest):
    return conv_mod.create_folder(req.name.strip() or "Untitled")


@app.put("/api/chat-folders/{folder_id}")
async def rename_chat_folder(folder_id: str, req: FolderRequest):
    if not conv_mod.rename_folder(folder_id, req.name.strip() or "Untitled"):
        raise HTTPException(status_code=404, detail="Folder not found")
    return {"id": folder_id, "name": req.name.strip()}


@app.delete("/api/chat-folders/{folder_id}")
async def delete_chat_folder(folder_id: str):
    conv_mod.delete_folder(folder_id)
    return {"status": "deleted"}


# ===== Search =====

@app.get("/api/search")
async def search_get(q: str, limit: int = 20, hybrid_weight: float = 0.5,
                     workspace: Optional[str] = None):
    """Search conversations, scoped to the active workspace unless told otherwise.

    ``workspace`` omitted means "whatever is active" — that default is what
    makes a workspace behave like a mode rather than a filter you re-apply on
    every screen. Pass ``workspace=all`` to search everything regardless.
    """
    scope = workspaces_mod.resolve_scope(workspace)
    result = search.search_conversations(
        q, limit=limit, hybrid_weight=hybrid_weight, workspace_id=scope
    )
    return {**result, "workspace_id": scope}


@app.post("/api/search")
async def search_post(req: SearchQuery):
    scope = workspaces_mod.resolve_scope(req.workspace)
    result = search.search_conversations(
        req.query, limit=req.limit, hybrid_weight=req.hybrid_weight, workspace_id=scope
    )
    return {**result, "workspace_id": scope}


@app.post("/api/search/classify")
async def classify_query(req: ClassifyQueryRequest):
    return search.classify_query(req.query)


# ===== Computer use =====

@app.get("/api/assignments")
async def get_assignments():
    results = cpu_mod.find_assignments()
    return {"count": len(results), "assignments": results}


@app.get("/api/computer_use/scan")
async def scan_computer():
    # `index_computer_use` never existed; this endpoint has been raising
    # AttributeError since it was written. The scan itself is computer_use_scan.
    count = cpu_mod.computer_use_scan()
    return {"indexed": count}


@app.post("/api/computer_use/vlm_scan")
async def vlm_scan(req: VlmScanRequest = VlmScanRequest()):
    count = cpu_mod.computer_use_scan(scan_dirs=req.scan_dirs, use_vlm=req.use_vlm)
    return {"indexed": count, "use_vlm": req.use_vlm}


@app.post("/api/computer_use/read")
async def read_file(req: dict):
    path = req.get("path", "")
    max_chars = req.get("max_chars", 5000)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return cpu_mod.read_file(path, max_chars=max_chars)


@app.post("/api/computer_use/analyze_html")
async def analyze_html(req: dict):
    path = req.get("path", "")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return cpu_mod.analyze_html_with_vlm(path)


@app.post("/api/computer_use/screenshot")
async def take_screenshot(req: dict = {}):
    window_title = req.get("window_title")
    return cpu_mod.take_screenshot(window_title=window_title)


@app.post("/api/computer_use/analyze_screenshot")
async def analyze_screenshot(req: dict):
    image_path = req.get("path", "")
    if not image_path or not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return cpu_mod.analyze_screenshot_with_vlm(image_path)


# ===== Terminal =====

@app.post("/api/terminal/execute")
async def execute_command(req: CommandRequest):
    """Run a shell command, screening destructive ones for confirmation first.

    A 428 means "this needs a second look" — the client re-sends with
    confirm=true once the user has agreed.
    """
    verdict = security_mod.check_command(req.command, confirmed=bool(req.confirm))
    if not verdict["allowed"]:
        raise HTTPException(
            status_code=428,
            detail={
                "message": verdict["message"],
                "reasons": verdict["reasons"],
                "command": req.command,
                "needs_confirmation": True,
            },
        )
    try:
        cwd = security_mod.resolve_cwd(req.cwd)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return term_mod.execute_command(req.command, cwd=cwd, timeout=req.timeout)


@app.post("/api/terminal/check")
async def check_terminal_command(req: CommandRequest):
    """Screen a command without running it, so the UI can warn as the user types."""
    return security_mod.classify_command(req.command)


@app.get("/api/terminal/history")
async def terminal_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT value FROM config WHERE key LIKE 'terminal_cmd:%' ORDER BY rowid DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [json.loads(r["value"]) for r in rows]


# ===== Recap =====

@app.get("/api/recap")
async def get_recaps(limit: int = 7):
    return recap_mod.get_recaps(limit=limit)


@app.post("/api/recap/run")
async def run_recap(req: RecapRequest = RecapRequest()):
    return recap_mod.run_recap(include_web_search=req.include_web_search, model=req.model)


@app.post("/api/recap/run/stream")
async def run_recap_stream(req: RecapRequest = RecapRequest()):
    """Run the deep-research pipeline, streaming every step (analysis, searches,
    page reads, model thinking, summary tokens) as SSE."""
    def event_stream():
        try:
            for ev in dr_mod.run_deep_research_stream(
                model=req.model, include_web_search=req.include_web_search
            ):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/recap/briefing/today")
async def briefing_today():
    briefing = dr_mod.get_today_briefing()
    if not briefing:
        return {"available": False}
    return {"available": True, **briefing}


@app.get("/api/recap/web-search")
async def run_web_search(query: str = DUCKDUCKGO_QUERY, max_results: int = 5):
    articles = recap_mod.fetch_live_tech_articles(max_results=max_results)
    return {"count": len(articles), "articles": articles}


# ===== Goals =====

@app.get("/api/goals")
async def list_goals(category: str = None):
    return goals_mod.list_goals(category=category)


@app.post("/api/goals")
async def create_goal(req: GoalRequest):
    return goals_mod.create_goal(req.title, req.description, req.category, req.metadata)


@app.post("/api/goals/{goal_id}/data")
async def add_goal_data(goal_id: str, req: DataPointRequest):
    return goals_mod.add_data_point(goal_id, req.value, req.label, req.metadata)


@app.get("/api/goals/{goal_id}/history")
async def goal_history(goal_id: str, start: str = None, end: str = None):
    return goals_mod.get_goal_history(goal_id, start=start, end=end)


# ===== Reminders =====

@app.get("/api/reminders")
async def list_reminders(completed: str = None):
    c = None
    if completed == "true":
        c = True
    elif completed == "false":
        c = False
    return rem_mod.list_reminders(completed=c)


@app.get("/api/reminders/overdue")
async def overdue_reminders():
    return rem_mod.get_overdue_reminders()


@app.get("/api/reminders/today")
async def today_reminders():
    return rem_mod.get_reminders_today()


@app.post("/api/reminders")
async def create_reminder(req: ReminderRequest):
    return rem_mod.create_reminder(req.title, req.description, req.due_at, req.metadata)


@app.post("/api/reminders/{reminder_id}/complete")
async def complete_reminder(reminder_id: str, req: ReminderCompleteRequest = ReminderCompleteRequest()):
    rem_mod.complete_reminder(reminder_id, completed=req.completed)
    return {"status": "completed" if req.completed else "uncompleted"}


# ===== Notes =====

@app.get("/api/notes")
async def list_notes(folder: str = None):
    return notes_mod.list_notes(folder=folder)


@app.get("/api/notes/{note_id}")
async def get_note(note_id: str):
    note = notes_mod.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.post("/api/notes")
async def create_note(req: NoteRequest):
    return notes_mod.create_note(req.title, req.content, req.folder or None)


@app.put("/api/notes/{note_id}")
async def update_note(note_id: str, req: NoteUpdateRequest):
    result = notes_mod.update_note(note_id, req.content, title=req.title)
    if result is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return result


@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    ok = notes_mod.delete_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"status": "deleted"}


# ===== Config =====

@app.get("/api/config")
async def get_app_config():
    """The full config with secrets redacted.

    Secrets are reported as a boolean so the UI can show "configured" without
    the value ever leaving the process.
    """
    return config.redact(config.get_config())


@app.put("/api/config/{key}")
async def set_config_value(key: str, value: Any = Body(...)):
    """Set one config key.

    Secrets are refused here. They have dedicated endpoints that validate the
    name and never read the value back, and leaving a second write path open
    would mean a value could be stored in a shape the vault does not expect.
    """
    if key in config.SECRET_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"'{key}' holds secrets and cannot be set here — use its own endpoint",
        )
    config.set_config(key, value)
    return {"key": key, "value": value}


# ===== Artifacts =====
# Charts, diagrams and images the assistant made. The content is model-authored
# markup, so the API hands back a *document* to drop into a sandboxed iframe
# rather than a fragment to inline — see carrot/artifacts.py.

@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    artifact = artifacts_mod.get(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {**artifact, "document": artifacts_mod.html_document(artifact)}


@app.get("/api/conversations/{conversation_id}/artifacts")
async def list_conversation_artifacts(conversation_id: str):
    items = artifacts_mod.for_conversation(conversation_id)
    # The list view does not need the payloads, and a conversation with fifty
    # charts in it would be megabytes of JSON.
    return {"artifacts": [{k: v for k, v in a.items() if k != "content"} for a in items]}


@app.delete("/api/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    if not artifacts_mod.delete(artifact_id):
        raise HTTPException(status_code=404, detail="artifact not found")
    return {"status": "deleted"}


class ArtifactRequest(BaseModel):
    kind: str
    content: str = ""
    title: str = ""
    path: str = ""
    conversation_id: str = ""


@app.post("/api/artifacts")
async def create_artifact(req: ArtifactRequest):
    """Used by the UI to re-render an artifact in the current theme, and by
    tests. The model reaches artifacts through the show_artifact tool."""
    try:
        artifact = artifacts_mod.create(
            req.kind, req.content, title=req.title, path=req.path,
            conversation_id=req.conversation_id)
    except artifacts_mod.ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**artifact, "document": artifacts_mod.html_document(artifact)}


# ===== Widgets =====

@app.get("/api/widgets/catalog")
async def widget_catalog():
    return {"catalog": widgets_mod.list_catalog()}


@app.get("/api/widgets")
async def list_widgets(slot: str = None):
    return {"widgets": widgets_mod.list_widgets(slot=slot)}


@app.post("/api/widgets")
async def add_widget(req: dict):
    wtype = req.get("type", "")
    try:
        return widgets_mod.add_widget(wtype, slot=req.get("slot"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ----- GitHub contribution widget (device flow) -----

@app.post("/api/widgets/github/auth")
async def github_auth_start():
    try:
        return gh_mod.start_auth()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub auth start failed: {e}")


@app.get("/api/widgets/github/status")
async def github_auth_status():
    return gh_mod.auth_status()


@app.get("/api/widgets/github/grid")
async def github_grid(refresh: bool = False):
    if refresh:
        gh_mod.refresh_grid()
    grid = gh_mod.get_grid()
    if grid is None:
        return {"available": False}
    return {"available": True, **grid}


@app.delete("/api/widgets/github")
async def github_disconnect():
    gh_mod.disconnect()
    return {"status": "disconnected"}


# ----- Generic widget instance ops (defined after specific paths) -----

@app.put("/api/widgets/{widget_id}")
async def update_widget(widget_id: str, req: dict):
    cfg = req.get("config", req)
    widget = widgets_mod.update_widget(widget_id, cfg)
    if widget is None:
        raise HTTPException(status_code=404, detail="Widget not found")
    return widget


@app.delete("/api/widgets/{widget_id}")
async def remove_widget(widget_id: str):
    if not widgets_mod.remove_widget(widget_id):
        raise HTTPException(status_code=404, detail="Widget not found")
    return {"status": "deleted"}


# ===== Apple Health sync (inbound from iOS Shortcuts) =====

@app.post("/api/health/sync")
async def health_sync(req: dict):
    try:
        return health_mod.sync_metrics(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/health/today")
async def health_today(date: str = None):
    return {"date": date, "metrics": health_mod.today_metrics(date)}


@app.get("/api/health/range")
async def health_range(days: int = 90):
    return {"days": days, "metrics": health_mod.range_metrics(days)}


# ===== Skills =====

@app.get("/api/skills")
async def get_skills():
    return skills_mod.list_skills()


@app.get("/api/skills/{slug}")
async def get_skill(slug: str):
    skill = skills_mod.get_skill(slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@app.post("/api/skills")
async def save_skill(req: SkillRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Skill name is required")
    return skills_mod.save_skill(req.name, req.description, req.instructions, slug=req.slug)


@app.delete("/api/skills/{slug}")
async def delete_skill(slug: str):
    if not skills_mod.delete_skill(slug):
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"status": "deleted"}


# ===== MCP servers =====

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    cfg = mcp_mod.load_mcp_config()
    return {"servers": cfg.get("servers", {})}


@app.get("/api/mcp/tools")
async def list_mcp_tools():
    """Discover tools for every configured server (spawns each briefly)."""
    return mcp_mod.discover_all_tools()


@app.post("/api/mcp/servers")
async def add_mcp_server(req: McpServerRequest):
    if not req.name.strip() or not req.command.strip():
        raise HTTPException(status_code=400, detail="Server name and command are required")
    return mcp_mod.add_server(req.name, req.command, req.args, req.enabled)


@app.post("/api/mcp/servers/{name}/enable")
async def enable_mcp_server(name: str, req: McpEnableRequest = McpEnableRequest()):
    if not mcp_mod.set_server_enabled(name, req.enabled):
        raise HTTPException(status_code=404, detail="Server not found")
    return {"name": name, "enabled": req.enabled}


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    if not mcp_mod.remove_server(name):
        raise HTTPException(status_code=404, detail="Server not found")
    return {"status": "deleted"}


# ===== Leaderboard =====

@app.get("/api/leaderboard")
async def get_leaderboard(
    os_name: str = None,
    ram_gb_min: float = None,
    gpu: str = None,
    model: str = None,
    limit: int = 50,
):
    filters = {}
    if os_name:
        filters["os"] = os_name
    if ram_gb_min:
        filters["ram_gb_min"] = ram_gb_min
    if gpu:
        filters["gpu"] = gpu
    if model:
        filters["model"] = model
    return lb_mod.get_leaderboard(filters=filters, limit=limit)


@app.get("/api/leaderboard/stats")
async def get_leaderboard_stats():
    return lb_mod.get_leaderboard_stats()


@app.get("/api/leaderboard/recommendations")
async def get_model_recommendations(ram_gb: float = None, gpu: str = None):
    return lb_mod.get_model_recommendations(user_ram_gb=ram_gb, user_gpu=gpu)


@app.post("/api/leaderboard/submit")
async def submit_leaderboard(req: dict):
    anon_id = req.get("anonymous_id")
    return lb_mod.submit_leaderboard_entry(anon_id=anon_id)


@app.put("/api/leaderboard/model")
async def set_leaderboard_model(anon_id: str, model: str, task_type: str = "general"):
    lb_mod.update_leaderboard_model(anon_id, model, task_type)
    return {"status": "updated", "anonymous_id": anon_id}


@app.get("/api/leaderboard/my-profile")
async def get_my_profile(anon_id: str):
    entries = lb_mod.get_leaderboard(filters={"anon_id": anon_id}, limit=1)
    if entries:
        return entries[0]
    raise HTTPException(status_code=404, detail="Profile not found")


# ===== Speech =====

@app.post("/api/speech/transcribe")
async def speech_transcribe(req: SpeechTranscribeRequest):
    """Transcribe base64-encoded audio using whisper.cpp STT."""
    try:
        result = whisper_stt.transcribe_base64(req.audio_base64)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")


@app.post("/api/speech/speak")
async def speech_speak(req: SpeechSpeakRequest):
    """Synthesize speech via Kokoro TTS and return base64 audio."""
    try:
        result = kokoro_tts.synthesize_base64(req.text, voice_style=req.voice or "us_rabbit")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {e}")


@app.get("/api/speech/voices")
async def speech_voices():
    """List available TTS voices."""
    return {"voices": kokoro_tts.list_voices()}


# ===== Memory =====

@app.get("/api/memory")
async def list_memories(kind: str = None, status: str = "active", subject: str = None, limit: int = 200):
    return {
        "memories": memory_mod.list_memories(
            kind=kind, status=status or None, subject=subject, limit=limit
        ),
        "stats": memory_mod.stats(),
    }


@app.get("/api/memory/search")
async def search_memories(q: str, limit: int = 10, include_superseded: bool = False):
    return {"results": memory_mod.search(q, limit=limit, include_superseded=include_superseded)}


@app.get("/api/memory/history/{subject}")
async def memory_history(subject: str, kind: str = None):
    """Every version of a belief, oldest first."""
    return {"subject": subject, "versions": memory_mod.history(subject, kind=kind)}


@app.post("/api/memory")
async def create_memory(req: MemoryRequest):
    return memory_mod.create(
        kind=req.kind, subject=req.subject, content=req.content,
        confidence=req.confidence, pinned=bool(req.pinned),
    )


@app.put("/api/memory/{memory_id}")
async def update_memory(memory_id: str, req: MemoryUpdateRequest):
    updated = memory_mod.update(memory_id, **req.model_dump(exclude_none=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return updated


@app.post("/api/memory/{memory_id}/reject")
async def reject_memory(memory_id: str):
    """Mark a memory wrong. Its subject is excluded from future extraction."""
    rejected = memory_mod.reject(memory_id)
    if rejected is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return rejected


@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: str):
    if not memory_mod.delete(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True}


@app.post("/api/memory/extract")
async def extract_memory(req: MemoryExtractRequest):
    """Run extraction over a turn on demand."""
    return {
        "extracted": memory_mod.extract_from_turn(
            user_text=req.user_text,
            assistant_text=req.assistant_text or "",
            conversation_id=req.conversation_id,
        )
    }


# ===== Conversation summaries =====

@app.get("/api/conversations/{conv_id}/summary")
async def conversation_summary(conv_id: str):
    return summarize_mod.get_summary(conv_id) or {"conversation_id": conv_id, "summary": ""}


@app.post("/api/conversations/{conv_id}/summarize")
async def summarize_conversation(conv_id: str):
    return summarize_mod.maybe_summarize(conv_id) or {"conversation_id": conv_id, "summary": ""}


# ===== Vectors =====

@app.get("/api/vectors/stats")
async def vector_stats():
    return vectors_mod.stats()


@app.post("/api/vectors/backfill")
async def vector_backfill(namespace: str = None, limit: int = 200):
    """Embed anything written while the embedding model was unavailable."""
    if namespace:
        return vectors_mod.backfill(namespace, limit=limit)
    return vectors_mod.backfill_all(limit_per_namespace=limit)


# ===== Document index =====

@app.get("/api/index/status")
async def index_status():
    return {**indexer_mod.scan_state(), "stats": indexer_mod.stats()}


@app.get("/api/index/documents")
async def index_documents(limit: int = 100, offset: int = 0):
    return {"documents": indexer_mod.list_documents(limit=limit, offset=offset)}


@app.post("/api/index/dirs")
async def add_index_dir(req: IndexDirRequest):
    try:
        return {"dirs": indexer_mod.add_index_dir(req.path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/index/dirs")
async def remove_index_dir(path: str):
    return {"dirs": indexer_mod.remove_index_dir(path)}


@app.post("/api/index/scan")
async def start_index_scan(req: IndexScanRequest = IndexScanRequest()):
    if not indexer_mod.index_dirs():
        raise HTTPException(status_code=400, detail="No directories are configured for indexing")
    return indexer_mod.start_scan_async(force=bool(req.force))


@app.get("/api/index/search")
async def search_index(q: str, limit: int = 10, hybrid_weight: float = 0.5,
                       workspace: Optional[str] = None):
    return indexer_mod.search_documents(
        q, limit=limit, hybrid_weight=hybrid_weight,
        workspace_id=workspaces_mod.resolve_scope(workspace),
    )


@app.get("/api/search/all")
async def search_everything(q: str, limit: int = 10, workspace: Optional[str] = None):
    """One query across conversations, documents and memory, in one scope."""
    scope = workspaces_mod.resolve_scope(workspace)
    conversations = search.search_conversations(q, limit=limit, workspace_id=scope)
    return {
        "query": q,
        "workspace_id": scope,
        "conversations": conversations["results"],
        "documents": indexer_mod.search_documents(q, limit=limit, workspace_id=scope)["results"],
        "memories": memory_mod.search(q, limit=limit, workspace_id=scope),
    }


# ===== Agent tools =====

@app.get("/api/agent/tools")
async def list_agent_tools():
    return {
        "tools": [
            {"name": name, "mutating": spec["mutating"], "risk": spec["risk"],
             "description": spec["description"]}
            for name, spec in agent_mod.TOOLS.items()
        ],
        "enabled": config.get_config().get("agent_tools_enabled", True),
        "require_approval": config.get_config().get("agent_require_approval", True),
    }


@app.get("/api/agent/approvals")
async def list_approvals():
    return {"pending": agent_mod.pending_approvals()}


@app.post("/api/agent/approvals/{approval_id}")
async def resolve_approval(approval_id: str, req: ApprovalRequest):
    """Answer a pending prompt.

    ``remember`` is honoured only for prompts the policy marked rememberable,
    and an allow on a prompt carrying a confirmation phrase becomes a deny
    unless the phrase was typed back — both enforced in ``agent_tools``, not
    here, so every caller gets the same treatment.
    """
    if not agent_mod.resolve_approval(
        approval_id, req.decision,
        remember=bool(req.remember),
        confirmation=req.confirmation or "",
    ):
        raise HTTPException(status_code=404, detail="No such pending approval")
    return {"resolved": True, "decision": req.decision}


@app.get("/api/agent/journal")
async def agent_journal(limit: int = 50):
    """Agent file edits, newest first, each with its diff."""
    return {"entries": agent_mod.list_journal(limit=limit)}


@app.post("/api/agent/journal/{entry_id}/revert")
async def revert_journal(entry_id: str):
    result = agent_mod.revert_journal_entry(entry_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ===== Workspaces and folders =====

@app.get("/api/workspaces")
async def workspace_tree(include_archived: bool = False):
    """Folders, workspaces and which one is active — the sidebar's whole state."""
    return workspaces_mod.tree(include_archived=include_archived)


@app.get("/api/workspaces/status")
async def workspace_status():
    return workspaces_mod.status()


@app.post("/api/workspaces/active")
async def set_active_workspace(req: ActiveWorkspaceRequest):
    """Switch context. An empty id means all workspaces."""
    try:
        return {"active": workspaces_mod.set_active_workspace(req.workspace_id or "")}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/workspaces")
async def create_workspace(req: WorkspaceRequest):
    try:
        return workspaces_mod.create_workspace(
            name=req.name, description=req.description or "",
            folder_id=req.folder_id or None, color=req.color or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str):
    workspace = workspaces_mod.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="No such workspace")
    return workspace


@app.get("/api/workspaces/{workspace_id}/contents")
async def workspace_contents(workspace_id: str, limit: int = 50):
    """What is filed here, resolved to titles."""
    contents = workspaces_mod.contents(workspace_id, limit=limit)
    if not contents:
        raise HTTPException(status_code=404, detail="No such workspace")
    return contents


@app.patch("/api/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, req: WorkspaceRequest):
    try:
        workspace = workspaces_mod.update_workspace(
            workspace_id, name=req.name, description=req.description,
            folder_id=req.folder_id, color=req.color, archived=req.archived,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if workspace is None:
        raise HTTPException(status_code=404, detail="No such workspace")
    return workspace


@app.delete("/api/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str):
    """Delete the grouping. Its chats, memories and files are unfiled, not deleted."""
    if not workspaces_mod.delete_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="No such workspace")
    return {"deleted": True}


@app.post("/api/workspaces/{workspace_id}/items")
async def file_workspace_items(workspace_id: str, req: WorkspaceItemsRequest):
    """File items here. An empty workspace id in the path is not accepted; use DELETE."""
    if workspaces_mod.get_workspace(workspace_id) is None:
        raise HTTPException(status_code=404, detail="No such workspace")
    filed = workspaces_mod.move_items(workspace_id, [i.model_dump() for i in req.items])
    return {"filed": filed, "counts": workspaces_mod.item_counts(workspace_id)}


@app.delete("/api/workspaces/items/{kind}/{item_id}")
async def unfile_workspace_item(kind: str, item_id: str):
    return {"unfiled": workspaces_mod.unfile_item(kind, item_id)}


@app.post("/api/folders")
async def create_folder(req: FolderNodeRequest):
    try:
        return workspaces_mod.create_folder(req.name, req.parent_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/folders/{folder_id}")
async def update_folder(folder_id: str, req: FolderNodeRequest):
    try:
        folder = workspaces_mod.update_folder(folder_id, name=req.name, parent_id=req.parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if folder is None:
        raise HTTPException(status_code=404, detail="No such folder")
    return folder


@app.delete("/api/folders/{folder_id}")
async def delete_workspace_folder(folder_id: str):
    """Delete a folder. Its workspaces move to the top level rather than vanishing."""
    if not workspaces_mod.delete_folder(folder_id):
        raise HTTPException(status_code=404, detail="No such folder")
    return {"deleted": True}


# ===== Help and tutorial =====

@app.get("/api/help")
async def help_index(q: str = ""):
    """Every help topic, or the ones matching a query."""
    return {
        "topics": help_mod.search_topics(q) if q else help_mod.topics(),
        "sections": help_mod.SECTIONS,
        "query": q,
    }


@app.get("/api/help/tutorial")
async def help_tutorial():
    """Getting-started steps, each checked against the live install."""
    return help_mod.tutorial()


@app.get("/api/help/{topic_id}")
async def help_topic(topic_id: str):
    topic = help_mod.get_topic(topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="No such help topic")
    return topic


# ===== Carrot Research =====

def _sse(generator):
    """Wrap a trace generator as an SSE response.

    Every research and agent event goes out the same way, so the UI has one
    parser for both and a new event type never needs a transport change.
    """
    async def stream():
        for event in generator:
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/research/depths")
async def research_depths():
    """Which depths the current research route can sustain."""
    return research_mod.available_depths()


@app.get("/api/research")
async def list_research_runs(limit: int = 30):
    return {"runs": research_mod.list_runs(limit=limit)}


@app.get("/api/research/{run_id}")
async def get_research_run(run_id: str):
    run = research_mod.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such research run")
    return run


@app.delete("/api/research/{run_id}")
async def delete_research_run(run_id: str):
    if not research_mod.delete_run(run_id):
        raise HTTPException(status_code=404, detail="No such research run")
    return {"deleted": True}


@app.post("/api/research/run")
async def start_research(req: ResearchRequest):
    """Run the research pipeline, streaming its trace."""
    if not (req.question or "").strip():
        raise HTTPException(status_code=400, detail="A research question is required")
    depth = req.depth or config.get_config().get("research_default_depth", research_mod.DEFAULT_DEPTH)
    return _sse(research_mod.run_research_stream(
        req.question, depth=depth, conversation_id=req.conversation_id,
    ))


@app.post("/api/research/{run_id}/cancel")
async def cancel_research(run_id: str):
    return {"cancelled": policy_mod.cancel_run(run_id)}


# ===== Carrot Agent =====

@app.get("/api/agent/status")
async def agent_status():
    """What the agent can reach right now, and under which limits."""
    return carrot_agent.status()


@app.get("/api/agent/runs")
async def list_agent_runs(limit: int = 30):
    return {"runs": carrot_agent.list_runs(limit=limit)}


@app.get("/api/agent/runs/{run_id}")
async def get_agent_run(run_id: str):
    """A run with its full audit trail: every action, decision, and result."""
    run = carrot_agent.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such agent run")
    return run


@app.post("/api/agent/run")
async def start_agent_run(req: AgentRunRequest):
    if not (req.task or "").strip():
        raise HTTPException(status_code=400, detail="A task is required")
    overrides = {}
    if req.max_steps:
        overrides["max_steps"] = int(req.max_steps)
    if req.max_seconds:
        overrides["max_seconds"] = int(req.max_seconds)
    return _sse(carrot_agent.run_agent_stream(
        req.task,
        surface=req.surface or carrot_agent.SURFACE_BROWSER,
        conversation_id=req.conversation_id,
        budget_overrides=overrides,
        require_plan_approval=req.require_plan_approval,
    ))


@app.post("/api/agent/runs/{run_id}/stop")
async def stop_agent_run(run_id: str):
    """The kill switch. Takes effect before the run's next action."""
    return {"stopped": policy_mod.cancel_run(run_id)}


# ===== Agent policy: what Carrot is allowed to touch =====

@app.get("/api/policy")
async def get_policy():
    return policy_mod.status()


@app.post("/api/policy/domains")
async def add_allowed_domain(req: DomainRequest):
    try:
        return {"allowed_domains": policy_mod.allow_domain(req.domain)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/policy/domains/{domain}")
async def remove_allowed_domain(domain: str):
    return {"allowed_domains": policy_mod.revoke_domain(domain)}


@app.get("/api/policy/secrets")
async def list_secrets():
    """Names only. There is no endpoint that returns a stored value."""
    return {"secrets": policy_mod.secret_names()}


@app.post("/api/policy/secrets")
async def store_secret(req: SecretRequest):
    try:
        return {"secrets": policy_mod.set_secret(req.name, req.value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/policy/secrets/{name}")
async def remove_secret(name: str):
    return {"secrets": policy_mod.delete_secret(name)}


@app.get("/api/policy/check-url")
async def check_url(url: str):
    """What the kernel would say about a URL — used by the UI to explain a block."""
    return policy_mod.check_url(url).as_dict()


# ===== Model routing =====

@app.get("/api/router/status")
async def router_status():
    return router_mod.status()


@app.put("/api/router/route")
async def set_router_route(req: RouteRequest):
    """Pin a task to a provider and model."""
    try:
        routes = router_mod.set_route(req.task, req.model, provider=req.provider, effort=req.effort)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"routes": routes, "route": router_mod.route(req.task).as_dict()}


@app.delete("/api/router/route/{task}")
async def clear_router_route(task: str):
    """Drop an assignment so the task falls back to the automatic rules."""
    return {"routes": router_mod.clear_route(task), "route": router_mod.route(task).as_dict()}


@app.get("/api/router/recommendation")
async def router_recommendation():
    """A local model sized to this machine's memory."""
    return router_mod.recommend_local_model()


# ===== Extension packs =====

@app.get("/api/extensions")
async def list_extensions():
    return {"extensions": extensions_mod.list_packs()}


@app.get("/api/extensions/{pack_id}")
async def get_extension(pack_id: str):
    """One pack in full: tools, skills, probed capabilities and settings."""
    try:
        return extensions_mod.require_pack(pack_id).as_dict(deep=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.put("/api/extensions/{pack_id}/enabled")
async def set_extension_enabled(pack_id: str, req: ProviderEnabledRequest):
    """Turn a pack on or off. Enabling installs its skills; disabling removes them."""
    try:
        return extensions_mod.set_enabled(pack_id, req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.put("/api/extensions/{pack_id}/settings/{key}")
async def set_extension_setting(pack_id: str, key: str, value: Any = Body(...)):
    try:
        return {"settings": extensions_mod.set_pack_setting(pack_id, key, value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ===== LaTeX workbench =====

@app.post("/api/latex/analyze")
async def latex_analyze(req: LatexRequest):
    """Validation, outline and math blocks — everything that needs no TeX engine."""
    from carrot.packs.academia import latex as latex_mod

    return {
        **latex_mod.validate(req.source),
        "outline": latex_mod.outline(req.source),
        "math": latex_mod.math_blocks(req.source),
        "engine": latex_mod.available_engine(),
    }


@app.post("/api/latex/compile")
async def latex_compile(req: LatexRequest):
    from carrot.packs.academia import latex as latex_mod

    try:
        return latex_mod.compile_document(req.source, req.out_path or "document.pdf")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/latex/bibliography")
async def latex_bibliography(req: BibliographyRequest):
    from carrot.packs.academia import bibliography as bib_mod

    return bib_mod.check(req.bib, req.tex)


# ===== Doc to agent =====

@app.get("/api/doc/destinations")
async def doc_destinations():
    """The places a note can be sent, for the picker beside the Send button."""
    return {
        "destinations": [
            {"id": name, **{k: v for k, v in spec.items()}}
            for name, spec in doc_agent.DESTINATIONS.items()
        ]
    }


@app.get("/api/doc/candidates")
async def doc_candidates(kind: str = "file", q: str = "", provider: str = "", limit: int = 40):
    """Completions for the editor's '@' menu.

    ``model`` needs a provider first — the list is whatever that provider
    reports for the key on file, not a hardcoded set.
    """
    if kind == "to":
        return {"kind": "to", "candidates": doc_agent.destination_candidates(q)}
    if kind == "provider":
        return {"kind": "provider", "candidates": doc_agent.provider_candidates(q)}
    if kind == "model":
        if not provider:
            return {"kind": "provider", "candidates": doc_agent.provider_candidates(q)}
        return {"kind": "model", "provider": provider,
                "candidates": doc_agent.model_candidates(provider, q)}
    return {"kind": "file", "candidates": doc_agent.file_candidates(q, limit=limit)}


@app.post("/api/doc/parse")
async def doc_parse(req: DocSendRequest):
    """Resolve a document's references without sending it.

    This is what the editor shows under the note: which files will be attached,
    which model will serve it, and what could not be resolved.
    """
    resolved = doc_agent.resolve(req.text, task=req.task or router_mod.TASK_CHAT)
    return resolved.as_dict()


@app.post("/api/doc/send")
async def doc_send(req: DocSendRequest):
    """Send a note to wherever it says it goes.

    Three destinations, one document format. Chat reuses the ordinary turn: the
    note's text becomes the user message, its cited files a system context
    block, and memory, tools, summaries and approvals behave exactly as they do
    in chat. Research and Agent take the same note and hand it to their own
    pipeline — the citations follow it either way, as evidence or as background.

    An explicit ``destination`` on the request wins over the note's own
    ``@/to``: the picker in the UI is an override, not a second source of truth.
    """
    if not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="Nothing to send")

    resolved = doc_agent.resolve(req.text, task=req.task or router_mod.TASK_CHAT)
    destination = (req.destination or resolved.destination or doc_agent.DESTINATION_CHAT).lower()
    if destination not in doc_agent.DESTINATIONS:
        raise HTTPException(status_code=400, detail=f"Unknown destination '{destination}'")
    option = req.option or (
        resolved.option if destination == resolved.destination
        else doc_agent.DESTINATIONS[destination]["default"]
    )

    if destination == doc_agent.DESTINATION_RESEARCH:
        # The note is the question. Its citations become numbered evidence, so
        # a claim drawn from a paper the user supplied is verified against that
        # paper's text rather than taken on trust.
        return _sse(research_mod.run_research_stream(
            resolved.prompt,
            depth=option or research_mod.DEFAULT_DEPTH,
            conversation_id=req.conversation_id,
            seed_sources=resolved.seed_sources(),
        ))

    if destination == doc_agent.DESTINATION_AGENT:
        # The note is the task. Citations are background rather than evidence —
        # the agent's evidence is the page in front of it.
        task_text = resolved.prompt
        if resolved.context:
            task_text += "\n\n" + resolved.context
        return _sse(carrot_agent.run_agent_stream(
            task_text,
            surface=option or carrot_agent.SURFACE_BROWSER,
            conversation_id=req.conversation_id,
        ))

    chat_req = ChatRequest(
        message=resolved.prompt,
        conversation_id=req.conversation_id,
        task=req.task,
        skill=req.skill,
        stream=True,
    )
    if chat_req.conversation_id is None:
        title = (req.title or resolved.prompt[:80]).strip() or "Untitled note"
        chat_req.conversation_id = conv_mod.create_conversation(title=title)["id"]
    conv = conv_mod.get_conversation(chat_req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    route = resolved.route
    if route is None:
        route = _resolve_chat_route(chat_req)
    elif route.local and not ollama_mod.OllamaClient().is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")

    mode = search_mode(req.search_mode)
    history, skill = _prepare_history(
        conv, resolved.prompt, chat_req.skill,
        extra_system=resolved.context or None, mode=mode,
    )
    conv_mod.add_message(chat_req.conversation_id, "user", resolved.prompt)
    return _chat_stream_response(
        chat_req, conv, history, skill, route, prelude=resolved.as_dict(), mode=mode
    )


# ===== Providers (BYOK) =====

@app.get("/api/router/providers")
async def list_providers():
    """Every configured provider. Keys are reported as booleans, never values."""
    return {
        "providers": providers_mod.list_providers(),
        "presets": providers_mod.PRESETS,
        "kinds": list(providers_mod.KINDS),
    }


@app.post("/api/router/providers")
async def upsert_provider(req: ProviderRequest):
    """Add or update a provider. An ``api_key`` in the body is stored separately."""
    try:
        provider = providers_mod.upsert_provider(
            req.id, label=req.label, kind=req.kind, base_url=req.base_url,
            models=req.models, env_var=req.env_var,
        )
        if req.api_key is not None and req.api_key.strip():
            providers_mod.set_api_key(provider["id"], req.api_key.strip())
            provider = providers_mod.require_provider(provider["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return provider


@app.delete("/api/router/providers/{provider_id}")
async def delete_provider(provider_id: str):
    try:
        if not providers_mod.delete_provider(provider_id):
            raise HTTPException(status_code=404, detail="Provider not found")
    except providers_mod.ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": provider_id}


@app.put("/api/router/providers/{provider_id}/key")
async def set_provider_key(provider_id: str, req: ProviderKeyRequest):
    """Store or clear a provider's key. An empty string forgets it."""
    try:
        providers_mod.require_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    providers_mod.set_api_key(provider_id, req.api_key.strip())
    return providers_mod.require_provider(provider_id)


@app.put("/api/router/providers/{provider_id}/enabled")
async def set_provider_enabled(provider_id: str, req: ProviderEnabledRequest):
    try:
        return providers_mod.set_enabled(provider_id, req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/router/providers/{provider_id}/models")
async def provider_models(provider_id: str):
    """Ask the provider what it serves — Carrot never hardcodes model names."""
    try:
        return providers_mod.list_models(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/router/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    """Probe a provider so a bad key surfaces here rather than mid-chat."""
    try:
        return providers_mod.test_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ===== Tasks =====

@app.get("/api/router/tasks")
async def list_tasks():
    return {"tasks": router_mod.tasks(), "assignments": router_mod.assignments()}


@app.post("/api/router/tasks")
async def create_task(req: TaskRequest):
    """Define a custom routing target, callable as ``task=<id>``."""
    try:
        return router_mod.add_task(
            req.id, label=req.label, description=req.description, local_only=req.local_only
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/router/tasks/{task_id}")
async def delete_task(task_id: str):
    try:
        if not router_mod.delete_task(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": task_id}


# ===== Notifications =====

@app.get("/api/notifications")
async def list_notifications(unread_only: bool = False, limit: int = 50):
    return {
        "notifications": proactive_mod.list_notifications(unread_only=unread_only, limit=limit),
        "unread": proactive_mod.unread_count(),
    }


@app.post("/api/notifications/check")
async def run_notification_checks():
    return proactive_mod.run_checks()


@app.post("/api/notifications/read-all")
async def read_all_notifications():
    return {"marked": proactive_mod.mark_all_read()}


@app.post("/api/notifications/{notification_id}/read")
async def read_notification(notification_id: str):
    if not proactive_mod.mark_read(notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"read": True}


@app.delete("/api/notifications/{notification_id}")
async def dismiss_notification(notification_id: str):
    if not proactive_mod.dismiss(notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"dismissed": True}


@app.get("/api/notifications/stream")
async def stream_notifications():
    """SSE feed of notifications as they are raised."""
    def stream():
        for notification in proactive_mod.stream_new():
            yield f"data: {json.dumps(notification)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ===== Security =====

@app.get("/api/security/status")
async def security_status():
    return security_mod.status()


@app.post("/api/security/rotate-token")
async def rotate_session_token():
    """Invalidate every existing client. The caller must reload to get the new token."""
    return {"token": security_mod.rotate_token()}


# ===== Backup =====

@app.get("/api/backup")
async def list_backups():
    return {"backups": backup_mod.list_backups()}


@app.post("/api/backup/export")
async def export_backup(req: BackupExportRequest = BackupExportRequest()):
    try:
        return backup_mod.export_archive(
            destination=req.path, include_vectors=bool(req.include_vectors)
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")


@app.post("/api/backup/inspect")
async def inspect_backup(req: BackupImportRequest):
    return backup_mod.inspect_archive(req.path)


@app.post("/api/backup/import")
async def import_backup(req: BackupImportRequest):
    """Replace this instance's data with an archive. A safety copy is taken first."""
    result = backup_mod.import_archive(req.path, safety_copy=bool(req.safety_copy))
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Import failed"))
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8181)
