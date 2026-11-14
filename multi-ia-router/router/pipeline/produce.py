"""Étape 4 — chaque agent produit la première version de sa partie."""

from __future__ import annotations

from ..runtime import Runtime
from .common import active_parts
from .parts import generate


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    plan = project.load_plan()
    for part in active_parts(project, plan):
        state = project.state.parts[part.id]
        if state.status != "pending":
            continue  # déjà produite (reprise) ou à réviser (traité à l'étape critique)
        version = state.version + 1
        generate(rt, plan, part, version)
        state.version = version
        state.cycle_start = version
        state.author = part.agent
        state.status = "produced"
        project.save()
        rt.event("part_update", stage="produce", part_id=part.id, status="produced", version=version)
