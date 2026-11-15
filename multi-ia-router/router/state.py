"""Dossier projet + état sauvegardé (reprise après arrêt).

Chaque étape écrit ses fichiers puis met à jour state.json de façon atomique : après une
coupure, `resume` saute ce qui est fait et repart de l'appel interrompu.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .schemas import Plan

STAGES = ("intake", "plan", "research", "replan", "produce", "review", "assemble", "validate", "deliver")
STAGE_LABELS = {
    "intake": "Reformulation",
    "plan": "Plan",
    "research": "Recherche",
    "replan": "Plan de modification",
    "produce": "Production",
    "review": "Critique croisée",
    "assemble": "Assemblage",
    "validate": "Validation",
    "deliver": "Livraison",
}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".opus", ".flac", ".m4a"}
TEXT_EXT = {
    ".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".css", ".js",
    ".ts", ".sh", ".bat", ".ps1", ".xml", ".ini", ".cfg", ".log", ".svg", ".tex", ".rst",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].strip("-") or "projet"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class StageState(BaseModel):
    status: Literal["pending", "running", "done", "error", "skipped"] = "pending"
    started: str = ""
    ended: str = ""
    detail: str = ""


class PartState(BaseModel):
    status: Literal["pending", "produced", "to_revise", "accepted", "arbitrated", "removed"] = "pending"
    author: str = ""
    version: int = 0  # dernière version produite
    accepted_version: int = 0  # version retenue pour l'assemblage
    cycle_start: int = 1  # première version du cycle de relecture en cours
    rounds: int = 0  # tours de critique consommés dans le cycle courant
    max_rounds: int = 0  # 0 = valeur de la config
    instruction: str = ""  # consigne en attente (renvoi du chef ou modification)
    reserves: list[str] = Field(default_factory=list)


class Modification(BaseModel):
    n: int
    instruction: str
    ts: str = Field(default_factory=now)
    status: Literal["pending", "done"] = "pending"


class Current(BaseModel):
    """Ce qui tourne en ce moment (pour le tableau de bord)."""

    stage: str = ""
    agent: str = ""
    role: str = ""
    part_id: str = ""


class ProjectState(BaseModel):
    id: str
    title: str = ""
    request: str
    created: str = Field(default_factory=now)
    updated: str = Field(default_factory=now)
    status: Literal["new", "running", "paused", "failed", "done"] = "new"
    mock: bool = False
    stages: dict[str, StageState] = Field(default_factory=dict)
    parts: dict[str, PartState] = Field(default_factory=dict)
    assembly_version: int = 0
    assembly_instruction: str = ""  # correction d'assemblage en attente
    validated_version: int = 0
    validation_returns: int = 0
    deliverable_version: int = 0
    modifications: list[Modification] = Field(default_factory=list)
    reserves: list[str] = Field(default_factory=list)
    error: str = ""
    current: Current = Field(default_factory=Current)


class Project:
    """Accès aux fichiers d'un projet. Tous les chemins passent par ici."""

    def __init__(self, directory: Path, state: ProjectState):
        self.dir = Path(directory)
        self.state = state
        self._lock = threading.RLock()

    # ------------------------------------------------------------- création / chargement
    @classmethod
    def create(cls, projects_dir: Path, request: str, *, mock: bool = False) -> "Project":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{slugify(request)}"
        directory = Path(projects_dir) / base
        suffix = 1
        while directory.exists():
            suffix += 1
            directory = Path(projects_dir) / f"{base}-{suffix}"
        directory.mkdir(parents=True)
        state = ProjectState(id=directory.name, request=request, mock=mock)
        for stage in STAGES:
            state.stages[stage] = StageState(status="skipped" if stage == "replan" else "pending")
        project = cls(directory, state)
        atomic_write(project.dir / "demande.md", request.strip() + "\n")
        project.save()
        return project

    @classmethod
    def load(cls, directory: Path) -> "Project":
        directory = Path(directory)
        path = directory / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"Projet introuvable : {directory}")
        state = ProjectState.model_validate_json(path.read_text(encoding="utf-8"))
        return cls(directory, state)

    def save(self) -> None:
        with self._lock:
            self.state.updated = now()
            atomic_write(self.dir / "state.json", self.state.model_dump_json(indent=2))

    # ------------------------------------------------------------------------- chemins
    @property
    def id(self) -> str:
        return self.state.id

    @property
    def brief_path(self) -> Path:
        return self.dir / "00_brief.json"

    @property
    def plan_path(self) -> Path:
        return self.dir / "01_plan.json"

    @property
    def research_dir(self) -> Path:
        return self.dir / "02_recherche"

    @property
    def research_path(self) -> Path:
        return self.research_dir / "recherche.md"

    @property
    def deliverable_dir(self) -> Path:
        return self.dir / "04_livrable"

    @property
    def assets_dir(self) -> Path:
        return self.deliverable_dir / "assets"

    @property
    def final_path(self) -> Path:
        return self.deliverable_dir / "LIVRABLE.md"

    @property
    def validation_dir(self) -> Path:
        return self.dir / "05_validation"

    @property
    def calls_path(self) -> Path:
        return self.dir / "logs" / "calls.jsonl"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    def part_dir(self, part_id: str) -> Path:
        return self.dir / "03_parties" / part_id

    def work_dir(self, part_id: str) -> Path:
        path = self.part_dir(part_id) / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def version_path(self, part_id: str, version: int) -> Path:
        return self.part_dir(part_id) / f"v{version}.md"

    def version_files_dir(self, part_id: str, version: int) -> Path:
        return self.part_dir(part_id) / f"v{version}_files"

    def critique_path(self, part_id: str, version: int) -> Path:
        return self.part_dir(part_id) / f"critique_v{version}.json"

    def response_path(self, part_id: str, version: int) -> Path:
        return self.part_dir(part_id) / f"reponse_v{version}.md"

    def arbitration_path(self, part_id: str) -> Path:
        return self.part_dir(part_id) / "arbitrage.json"

    def assembly_path(self, version: int) -> Path:
        return self.deliverable_dir / f"livrable_v{version}.md"

    def verdict_path(self, version: int) -> Path:
        return self.validation_dir / f"verdict_v{version}.json"

    def modification_path(self, n: int) -> Path:
        return self.dir / "modifications" / f"modif_{n}.json"

    # ------------------------------------------------------------------ lecture / écriture
    def write_text(self, path: Path, content: str) -> Path:
        atomic_write(path, content)
        return path

    def write_json(self, path: Path, data: Any) -> Path:
        if isinstance(data, BaseModel):
            data = data.model_dump()
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        return path

    def read_text(self, path: Path, default: str = "") -> str:
        return path.read_text(encoding="utf-8") if path.exists() else default

    def read_json(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def load_plan(self) -> Plan:
        return Plan.model_validate(self.read_json(self.plan_path))

    def version_files(self, part_id: str, version: int) -> list[Path]:
        directory = self.version_files_dir(part_id, version)
        if not directory.exists():
            return []
        return sorted(p for p in directory.rglob("*") if p.is_file())

    def full_request(self) -> str:
        """Demande initiale + modifications demandées ensuite (référence de la validation)."""
        text = self.state.request.strip()
        if self.state.modifications:
            lines = [f"{m.n}. {m.instruction}" for m in self.state.modifications]
            text += "\n\nModifications demandées ensuite :\n" + "\n".join(lines)
        return text

    # ----------------------------------------------------------------------------- état
    def set_stage(self, stage: str, status: str, detail: str = "") -> None:
        with self._lock:
            entry = self.state.stages.setdefault(stage, StageState())
            entry.status = status  # type: ignore[assignment]
            if status == "running":
                entry.started = now()
                entry.ended = ""
            elif status in ("done", "error"):
                entry.ended = now()
            entry.detail = detail
            self.save()

    def reset_stages(self, *stages: str) -> None:
        with self._lock:
            for stage in stages:
                self.state.stages[stage] = StageState()
            self.save()


def list_projects(projects_dir: Path) -> list[ProjectState]:
    """États de tous les projets, du plus récent au plus ancien."""
    states: list[ProjectState] = []
    root = Path(projects_dir)
    if not root.exists():
        return states
    for directory in sorted(root.iterdir(), reverse=True):
        path = directory / "state.json"
        if not path.exists():
            continue
        try:
            states.append(ProjectState.model_validate_json(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return states
