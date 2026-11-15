"""Tableau de bord web local (FastAPI + page HTML sans dépendance externe).

Lancement : python -m router dashboard   → http://127.0.0.1:8765

Le tableau de bord ne fait que lire le dossier du projet (state.json, events.jsonl, fichiers
des étapes) : un projet lancé en ligne de commande s'y affiche en direct de la même façon.
Les pipelines démarrés d'ici tournent dans un fil d'exécution en arrière-plan.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel

from ..config import Config
from ..errors import ConfigError, RouterError
from ..logs import logger, read_jsonl
from ..pipeline import orchestrator
from ..runtime import Runtime
from ..schemas import GRID_LABELS
from ..state import AUDIO_EXT, IMAGE_EXT, STAGE_LABELS, STAGES, Project, list_projects
from ..toolbox import Toolbox
from ..runhint import CMD

STATIC = Path(__file__).resolve().parent / "static"
ROLE_LABELS = {
    "chef": "Chef de projet",
    "dispatcher": "Répartiteur",
    "assembler": "Assembleur",
    "executor": "Exécutant",
    "researcher": "Chercheur",
}


# ─────────────────────────────────────────────────────────────────────── rendu Markdown
def _markdown(base: str) -> MarkdownIt:
    """Markdown → HTML sûr : le HTML brut est neutralisé, les liens relatifs pointent vers `base`."""
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")

    def absolute(url: str) -> str:
        if not url or url.startswith(("http://", "https://", "mailto:", "#", "/")):
            return url
        return base + url

    def image(tokens, idx, options, env):
        tokens[idx].attrSet("src", absolute(tokens[idx].attrGet("src") or ""))
        tokens[idx].attrSet("loading", "lazy")
        return default_image(tokens, idx, options, env)

    def link_open(tokens, idx, options, env):
        href = tokens[idx].attrGet("href") or ""
        tokens[idx].attrSet("href", absolute(href))
        if href.startswith(("http://", "https://")):
            tokens[idx].attrSet("target", "_blank")
            tokens[idx].attrSet("rel", "noopener noreferrer")
        return md.renderer.renderToken(tokens, idx, options, env)

    default_image = md.renderer.rules["image"]
    md.renderer.rules["image"] = image
    md.renderer.rules["link_open"] = link_open
    return md


def render_markdown(text: str, base: str = "") -> str:
    return _markdown(base).render(text or "")


# ───────────────────────────────────────────────────────────────────── lecture d'un projet
_totals_cache: dict[str, tuple[tuple[int, float], dict]] = {}


def call_totals(project: Project) -> dict[str, Any]:
    """Totaux tokens / durée / coût par agent. Recalculé seulement si le journal a changé."""
    path = project.calls_path
    if not path.exists():
        return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "duration_s": 0.0, "cost_usd": 0.0, "by_agent": {}}
    stat = path.stat()
    key = (stat.st_size, stat.st_mtime)
    cached = _totals_cache.get(project.id)
    if cached and cached[0] == key:
        return cached[1]
    totals: dict[str, Any] = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "duration_s": 0.0, "cost_usd": 0.0, "by_agent": {}}
    for record in read_jsonl(path):
        agent = totals["by_agent"].setdefault(
            record.get("agent", "?"), {"calls": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0, "duration_s": 0.0, "cost_usd": 0.0}
        )
        for bucket in (totals, agent):
            bucket["calls"] += 1
            bucket["tokens_in"] += record.get("tokens_in") or 0
            bucket["tokens_out"] += record.get("tokens_out") or 0
            bucket["duration_s"] = round(bucket["duration_s"] + (record.get("duration_s") or 0.0), 2)
            bucket["cost_usd"] = round(bucket["cost_usd"] + (record.get("cost_usd") or 0.0), 4)
        if not record.get("ok"):
            agent["errors"] += 1
    _totals_cache[project.id] = (key, totals)
    return totals


def file_entry(project: Project, path: Path) -> dict[str, Any]:
    relative = path.relative_to(project.dir).as_posix()
    return {
        "name": path.name, "url": f"/files/{project.id}/{relative}", "size": path.stat().st_size,
        "image": path.suffix.lower() in IMAGE_EXT,
        "audio": path.suffix.lower() in AUDIO_EXT,
    }


def project_detail(config: Config, project: Project, running: bool) -> dict[str, Any]:
    state = project.state
    plan = project.read_json(project.plan_path)
    parts: list[dict[str, Any]] = []
    for part in (plan or {}).get("parts", []):
        part_state = state.parts.get(part["id"])
        if part_state is None:
            continue
        versions = []
        for number in range(1, part_state.version + 1):
            if not project.version_path(part["id"], number).exists():
                continue
            versions.append({
                "n": number,
                "critique": project.read_json(project.critique_path(part["id"], number)),
                "response": bool(project.response_path(part["id"], number).exists()),
                "files": [file_entry(project, f) for f in project.version_files(part["id"], number)],
            })
        try:
            critic = config.critic_name_for(part["agent"])
        except ConfigError:
            critic = "?"
        parts.append({
            **part, "critic": critic, **part_state.model_dump(), "versions": versions,
            "arbitration": project.read_json(project.arbitration_path(part["id"])),
        })

    deliverables = []
    for number in range(1, state.assembly_version + 1):
        if project.assembly_path(number).exists():
            deliverables.append({"version": number, "verdict": project.read_json(project.verdict_path(number))})
    modifications = [
        {**m.model_dump(), "plan": project.read_json(project.modification_path(m.n))} for m in state.modifications
    ]
    return {
        "state": state.model_dump(),
        "running": running,
        "stale": state.status == "running" and not running,  # arrêté brutalement : peut être repris
        "stages": [
            {"id": s, "label": STAGE_LABELS[s], **state.stages[s].model_dump()}
            for s in STAGES if s in state.stages and state.stages[s].status != "skipped"
        ],
        "brief": project.read_json(project.brief_path),
        "plan": {k: v for k, v in (plan or {}).items() if k != "parts"} if plan else None,
        "parts": parts,
        "has_research": project.research_path.exists(),
        "deliverables": deliverables,
        "final": project.final_path.exists(),
        "modifications": modifications,
        "totals": call_totals(project),
    }


# ──────────────────────────────────────────────────────────────────────────── application
class NewProject(BaseModel):
    request: str
    mock: bool = False


class Revision(BaseModel):
    instruction: str


def create_app(config: Config, toolbox: Toolbox | None = None) -> FastAPI:
    app = FastAPI(title="Routeur multi-IA", docs_url=None, redoc_url=None)
    threads: dict[str, threading.Thread] = {}
    open_to_network = config.dashboard.host in ("0.0.0.0", "::")
    local_names = {"127.0.0.1", "localhost", "[::1]", config.dashboard.host}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Le tableau de bord lance des agents : seule ta machine doit pouvoir lui parler.

        - En-tête Host inattendu → refus (attaque par « DNS rebinding » depuis un site web).
        - Requête d'écriture venue d'une autre origine → refus (un site ne peut pas lancer de projet).
        """
        if not config.dashboard.local_only:  # derrière un proxy de confiance : contrôles désactivés exprès
            return await call_next(request)
        host = request.headers.get("host", "")
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if not open_to_network and name not in local_names:
            return JSONResponse({"detail": "Hôte refusé : tableau de bord local uniquement."}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc != host:
            return JSONResponse({"detail": "Origine refusée."}, status_code=403)
        return await call_next(request)
    lock = threading.Lock()

    def is_running(project_id: str) -> bool:
        thread = threads.get(project_id)
        return bool(thread and thread.is_alive())

    def get_project(project_id: str) -> Project:
        directory = config.projects_dir / project_id
        if "/" in project_id or "\\" in project_id or ".." in project_id or not (directory / "state.json").exists():
            raise HTTPException(404, "Projet introuvable")
        return Project.load(directory)

    def launch(project: Project, run_config: Config) -> None:
        """Démarre le pipeline en arrière-plan (un seul fil par projet)."""
        with lock:
            if is_running(project.id):
                raise HTTPException(409, "Ce projet est déjà en cours d'exécution.")

            def work() -> None:
                try:
                    orchestrator.run_pipeline(Runtime(run_config, project))
                except Exception:  # run_pipeline gère déjà ses erreurs ; ceci ne doit jamais tuer le serveur
                    logger.exception("pipeline interrompu (%s)", project.id)

            thread = threading.Thread(target=work, name=f"pipeline-{project.id}", daemon=True)
            threads[project.id] = thread
            thread.start()

    def demo_config() -> Config:
        """Simulation lancée depuis le tableau de bord : ralentie pour que le direct soit visible."""
        clone = config.model_copy(update={"mock": True, "mock_delay": config.mock_delay or 0.7})
        clone.root = config.root
        return clone

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/static/{name}")
    def static(name: str) -> FileResponse:
        path = (STATIC / name).resolve()
        if path.parent != STATIC or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        roles = config.roles
        team = []
        for name, cfg in config.agents.items():
            jobs = [label for key, label in ROLE_LABELS.items() if getattr(roles, key) == name]
            if name in roles.critics.values():
                jobs.append("Critique")
            if name in roles.producers:
                jobs.append("Producteur")
            team.append({
                "name": name, "type": cfg.type, "family": cfg.family or cfg.type, "model": cfg.model,
                "enabled": cfg.enabled, "roles": jobs, "description": cfg.description,
            })
        return {
            "mock": config.mock, "team": team, "grid": GRID_LABELS, "stage_labels": STAGE_LABELS,
            "review": config.review.model_dump(include={"max_rounds", "min_score"}),
            "max_returns": config.validation.max_returns,
        }

    @app.get("/api/tools")
    def tools() -> list[dict[str, Any]]:
        """État des outils complémentaires. Lecture seule : démarrer ou installer se fait en ligne de commande."""
        box = toolbox or Toolbox(config)
        listing = []
        for status in box.statuses():
            spec = box.spec(status.id)
            listing.append({
                **status.as_dict(), "summary": spec.summary, "license": spec.license, "repo": spec.repo,
                "needs": spec.needs, "warnings": spec.warnings, "automated": spec.automated,
                "command": f"{CMD} tools {'start' if spec.automated else 'install'} {spec.id}",
            })
        return listing

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        return [
            {
                "id": s.id, "title": s.title or s.request[:70], "status": s.status, "created": s.created,
                "updated": s.updated, "mock": s.mock, "running": is_running(s.id),
                "stale": s.status == "running" and not is_running(s.id), "modifications": len(s.modifications),
            }
            for s in list_projects(config.projects_dir)
        ]

    @app.post("/api/projects")
    def new_project(body: NewProject) -> dict[str, str]:
        run_config = demo_config() if (body.mock and not config.mock) else config
        try:
            run_config.check()
            project = orchestrator.create_project(run_config, body.request)
        except RouterError as exc:
            raise HTTPException(400, str(exc)) from exc
        launch(project, run_config)
        return {"id": project.id}

    @app.get("/api/projects/{project_id}")
    def detail(project_id: str) -> dict[str, Any]:
        return project_detail(config, get_project(project_id), is_running(project_id))

    @app.post("/api/projects/{project_id}/resume")
    def resume(project_id: str) -> dict[str, str]:
        project = get_project(project_id)
        if project.state.status == "done":
            raise HTTPException(400, "Ce projet est déjà terminé.")
        run_config = demo_config() if (project.state.mock and not config.mock) else config
        launch(project, run_config)
        return {"id": project.id}

    @app.post("/api/projects/{project_id}/revise")
    def revise(project_id: str, body: Revision) -> dict[str, str]:
        project = get_project(project_id)
        if is_running(project_id):
            raise HTTPException(409, "Ce projet est en cours d'exécution.")
        try:
            orchestrator.prepare_revision(project, body.instruction)
        except RouterError as exc:
            raise HTTPException(400, str(exc)) from exc
        run_config = demo_config() if (project.state.mock and not config.mock) else config
        launch(project, run_config)
        return {"id": project.id}

    @app.get("/api/projects/{project_id}/events")
    async def events(project_id: str, request: Request, since: int = 0) -> StreamingResponse:
        """Flux en direct (Server-Sent Events) : relit events.jsonl au fil de l'eau."""
        project = get_project(project_id)

        async def stream():
            index = max(0, since)
            idle = 0.0
            while not await request.is_disconnected():
                records = read_jsonl(project.events_path, start=index)
                for record in records:
                    index += 1
                    yield f"id: {index}\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"
                if records:
                    idle = 0.0
                else:
                    idle += 0.5
                    if idle >= 15:
                        idle = 0.0
                        yield ": actif\n\n"  # empêche les coupures de connexion silencieuses
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/projects/{project_id}/parts/{part_id}/versions/{version}")
    def part_version(project_id: str, part_id: str, version: int) -> dict[str, Any]:
        project = get_project(project_id)
        if part_id not in project.state.parts or not project.version_path(part_id, version).exists():
            raise HTTPException(404, "Version introuvable")
        text = project.read_text(project.version_path(part_id, version))
        response = project.read_text(project.response_path(part_id, version))
        base = f"/files/{project.id}/03_parties/{part_id}/v{version}_files/"
        return {
            "markdown": text, "html": render_markdown(text, base),
            "response_html": render_markdown(response) if response else "",
        }

    @app.get("/api/projects/{project_id}/research")
    def research(project_id: str) -> dict[str, Any]:
        project = get_project(project_id)
        return {
            "html": render_markdown(project.read_text(project.research_path)),
            "sources": project.read_json(project.research_dir / "sources.json", []) or [],
        }

    @app.get("/api/projects/{project_id}/deliverable")
    def deliverable(project_id: str, version: int = 0) -> dict[str, Any]:
        project = get_project(project_id)
        path = project.assembly_path(version) if version else project.final_path
        if not path.exists():
            raise HTTPException(404, "Livrable introuvable")
        text = project.read_text(path)
        return {
            "markdown": text, "html": render_markdown(text, f"/files/{project.id}/04_livrable/"),
            "path": str(path), "download": f"/files/{project.id}/{path.relative_to(project.dir).as_posix()}",
        }

    @app.get("/api/projects/{project_id}/calls")
    def calls(project_id: str) -> list[dict[str, Any]]:
        """Journal des appels, sans les textes longs (voir /calls/{index} pour le détail)."""
        project = get_project(project_id)
        light = ("ts", "stage", "part_id", "role", "agent", "model", "attempt", "duration_s", "tokens_in",
                 "tokens_out", "tokens_estimated", "cost_usd", "ok", "error")
        return [{"index": i, **{k: r.get(k) for k in light}} for i, r in enumerate(read_jsonl(project.calls_path))]

    @app.get("/api/projects/{project_id}/calls/{index}")
    def call(project_id: str, index: int) -> dict[str, Any]:
        records = read_jsonl(get_project(project_id).calls_path)
        if not 0 <= index < len(records):
            raise HTTPException(404, "Appel introuvable")
        return records[index]

    @app.get("/files/{project_id}/{path:path}")
    def files(project_id: str, path: str) -> FileResponse:
        project = get_project(project_id)
        root = project.dir.resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():  # interdit de sortir du dossier du projet
            raise HTTPException(404, "Fichier introuvable")
        return FileResponse(target)

    return app


def serve(config: Config, host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    host = host or config.dashboard.host
    port = port or config.dashboard.port
    print(f"\n  Tableau de bord : http://{host}:{port}   (Ctrl+C pour arrêter)\n")
    uvicorn.run(create_app(config), host=host, port=port, log_level="warning")
