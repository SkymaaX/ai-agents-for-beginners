"""Étape 8 — enregistrement du livrable final (+ copie optionnelle dans la vault Obsidian)."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..logs import logger
from ..runtime import Runtime
from ..state import slugify


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    state = project.state
    version = state.validated_version or state.assembly_version
    text = project.read_text(project.assembly_path(version)).rstrip()

    reserves = list(state.reserves)
    for part_id, part_state in state.parts.items():
        if part_state.status != "removed":
            reserves += [f"{part_id} : {r}" for r in part_state.reserves]
    if reserves:
        text += "\n\n---\n\n## Réserves\n\nPoints signalés par le chef de projet lors de la validation :\n\n"
        text += "\n".join(f"- {r}" for r in reserves)
    project.write_text(project.final_path, text + "\n")

    state.deliverable_version = version
    for modification in state.modifications:
        modification.status = "done"
    project.save()

    exported = export_to_vault(rt)
    rt.event("delivered", stage="deliver", version=version, reserves=len(reserves), vault=str(exported or ""))


def export_to_vault(rt: Runtime) -> Path | None:
    """Copie LIVRABLE.md et ses fichiers dans la vault, si `paths.vault_dir` est renseigné."""
    project = rt.project
    assert project is not None
    paths = rt.config.paths
    if not paths.vault_dir or rt.config.mock:
        return None
    vault = Path(paths.vault_dir).expanduser()
    if not vault.is_dir():
        logger.warning("Vault introuvable (%s) : pas de copie du livrable.", vault)
        return None
    name = slugify(project.state.title or project.id, 60)
    target = vault / paths.vault_export_subdir / f"{project.id[:15]}-{name}"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project.final_path, target / f"{name}.md")
    if project.assets_dir.exists():
        shutil.copytree(project.assets_dir, target / "assets", dirs_exist_ok=True)
    return target
