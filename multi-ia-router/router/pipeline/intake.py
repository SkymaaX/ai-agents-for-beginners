"""Étape 1 — le chef (Qwen) reformule la demande en brief."""

from __future__ import annotations

from ..agents.base import ROLE_INTAKE, AgentTask, ask_json
from ..runtime import Runtime
from ..schemas import Brief
from .common import prompt, system


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    request = project.state.request
    task = AgentTask(
        role=ROLE_INTAKE,
        stage="intake",
        system=system(rt, "chef"),
        prompt=prompt(rt, "intake", request=request),
        meta={"request": request},
    )
    brief = ask_json(rt.role("chef"), task, Brief, repairs=rt.config.json_repairs)
    project.write_json(project.brief_path, brief)
    project.state.title = brief.title
    project.save()
    rt.event("brief", stage="intake", title=brief.title)
