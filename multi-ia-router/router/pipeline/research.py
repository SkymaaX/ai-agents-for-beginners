"""Étape 3 — recherche commune, faite une seule fois et partagée à toutes les parties."""

from __future__ import annotations

from ..research.dossier import build_dossier
from ..runtime import Runtime


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    plan = project.load_plan()
    markdown, sources = build_dossier(rt, plan.research_queries, project.state.title or plan.project_title)
    project.write_text(project.research_path, markdown)
    project.write_json(project.research_dir / "sources.json", sources)
    rt.event("research", stage="research", sources=len(sources))
