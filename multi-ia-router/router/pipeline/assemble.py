"""Étape 6 — l'assembleur (Claude) réunit les parties validées en un livrable unique."""

from __future__ import annotations

import shutil

from ..agents.base import ROLE_ASSEMBLE, AgentTask
from ..runtime import Runtime
from ..state import IMAGE_EXT
from .common import active_parts, base_context, best_version, prompt, system

# Fichiers internes du dossier de travail, sans intérêt pour le client.
_SKIP = {"blender.log"}


def collect_assets(rt: Runtime) -> list[str]:
    """Copie les fichiers des versions retenues dans 04_livrable/assets/<partie>/. Renvoie les chemins relatifs."""
    project = rt.project
    assert project is not None
    plan = project.load_plan()
    if project.assets_dir.exists():
        shutil.rmtree(project.assets_dir)
    assets: list[str] = []
    for part in active_parts(project, plan):
        version = best_version(project, part.id)
        root = project.version_files_dir(part.id, version)
        for path in project.version_files(part.id, version):
            if path.name in _SKIP:
                continue
            relative = path.relative_to(root)
            target = project.assets_dir / part.id / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            assets.append(f"assets/{part.id}/{relative.as_posix()}")
    return assets


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    state = project.state
    plan = project.load_plan()
    assets = collect_assets(rt)

    context = base_context(rt)
    parts_meta: list[tuple[str, str]] = []
    for part in active_parts(project, plan):
        version = best_version(project, part.id)
        text = project.read_text(project.version_path(part.id, version))
        reserves = state.parts[part.id].reserves
        if reserves:
            text += "\n\nRéserves du chef de projet sur cette partie : " + " ; ".join(reserves)
        context.append((f"Partie validée {part.id} — {part.title} ({part.type}, par {part.agent})", text))
        parts_meta.append((part.title, text))

    assets_text = "\n".join(
        f"- {a} ({'image' if any(a.lower().endswith(e) for e in IMAGE_EXT) else 'fichier'})" for a in assets
    ) or "(aucun fichier)"
    correction = ""
    if state.assembly_instruction:
        correction = (
            "Correction à appliquer par rapport à l'assemblage précédent :\n\n"
            f"<correction>\n{state.assembly_instruction}\n</correction>"
        )
        previous = project.read_text(project.assembly_path(state.assembly_version))
        if previous:
            context.append((f"Assemblage précédent (version {state.assembly_version})", previous))

    version = state.assembly_version + 1
    task = AgentTask(
        role=ROLE_ASSEMBLE,
        stage="assemble",
        system=system(rt, "assembler"),
        prompt=prompt(
            rt, "assemble", project_title=state.title or plan.project_title,
            assembly_instructions=plan.assembly_instructions or "(aucune consigne particulière)",
            assets=assets_text, correction=correction,
        ),
        context=context,
        meta={
            "project_title": state.title or plan.project_title, "parts": parts_meta, "assets": assets,
            "assembly_version": version, "instruction": state.assembly_instruction,
        },
    )
    result = rt.role("assembler").run(task)
    project.write_text(project.assembly_path(version), result.text.strip() + "\n")
    state.assembly_version = version
    state.assembly_instruction = ""
    project.save()
    rt.event("assembly", stage="assemble", version=version, assets=len(assets))
