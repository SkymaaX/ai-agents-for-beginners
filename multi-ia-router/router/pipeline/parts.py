"""Fabrication d'une version d'une partie, selon son type.

Utilisé pour la première production (étape 4) comme pour les révisions (étape 5, renvois,
modifications). Écrit toujours `vN.md` (contenu ou compte rendu) et, s'il y a des fichiers,
`vN_files/`.
"""

from __future__ import annotations

import re
import shutil
import wave
from pathlib import Path

from ..agents.base import ROLE_PRODUCE, ROLE_REVISE, AgentResult, AgentTask
from ..errors import AgentError
from ..prompts import bullets
from ..runtime import Runtime
from ..schemas import Critique, Part, Plan
from ..tools import blender
from .common import (
    base_context, best_version, dependency_context, files_context, prompt, split_response, system,
)

FILE_TYPES = ("code", "files", "blender")
MEDIA_TYPES = ("image", "video")


def speakable(text: str) -> str:
    """Texte Markdown → texte à lire à voix haute : sans titres, liens, renvois de sources, code."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"^\s*#{1,6}\s+.*\(version \d+\)\s*$", "", text, count=1, flags=re.MULTILINE)  # titre de version
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s*\[S\d+(?:\s*,\s*S\d+)*\]", "", text)  # renvois [S3], [S1, S2]
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`>]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def spoken_text(rt: Runtime, plan: Plan, part: Part) -> tuple[str, str]:
    """(texte à dire, origine). Une partie audio lit le texte de la première partie dont elle dépend ;
    sans dépendance, c'est son propre prompt qui est lu tel quel."""
    project = rt.project
    assert project is not None
    for dep_id in part.depends_on:
        if project.state.parts[dep_id].status == "removed":
            continue
        version = best_version(project, dep_id)
        if version:
            origin = f"la partie {dep_id} — {plan.part(dep_id).title} (version {version})"
            return speakable(project.read_text(project.version_path(dep_id, version))), origin
    return speakable(part.prompt), "le prompt du répartiteur"


def wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate())
    except (wave.Error, EOFError, OSError):
        return None


def _reason(critique: Critique | None, instruction: str) -> str:
    """Pourquoi la partie est révisée : critique d'un relecteur et/ou consigne directe."""
    blocks: list[str] = []
    if instruction:
        blocks.append(f"Correction demandée :\n\n<correction>\n{instruction}\n</correction>")
    if critique is not None:
        lines = [
            f"- [{p.severity}] ({p.critere}) {p.detail}\n  Correction proposée : {p.correction}"
            for p in critique.problems
        ]
        scores = ", ".join(f"{key} {value}/5" for key, value in critique.scores.model_dump().items())
        blocks.append(
            "Critique du relecteur :\n\n<critique>\n"
            f"Avis : {critique.summary}\nNotes : {scores}\nProblèmes :\n"
            + ("\n".join(lines) or "(aucun problème listé)")
            + "\n</critique>"
        )
    return "\n\n".join(blocks)


def _snapshot(workdir: Path, target: Path) -> list[Path]:
    """Copie le dossier de travail dans vN_files/ pour figer cette version."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(workdir, target)
    return sorted(p for p in target.rglob("*") if p.is_file())


def generate(
    rt: Runtime,
    plan: Plan,
    part: Part,
    version: int,
    *,
    critique: Critique | None = None,
    instruction: str = "",
) -> None:
    """Produit la version `version` de `part`. Révision si une critique ou une consigne est donnée."""
    project = rt.project
    assert project is not None
    revising = version > 1 and (critique is not None or bool(instruction))
    agent = rt.agent(part.agent)
    stage = "review" if revising else "produce"
    role = ROLE_REVISE if revising else ROLE_PRODUCE
    criteria = bullets(part.success_criteria, "(aucun critère fourni)")
    reason = _reason(critique, instruction)
    meta = {
        "title": part.title, "version": version, "part_type": part.type,
        "filename": part.expected_output.filename, "instruction": instruction or (critique.summary if critique else ""),
    }
    context = base_context(rt) + dependency_context(project, plan, part)
    if revising:
        previous = project.read_text(project.version_path(part.id, version - 1))
        context.append((f"Ta version actuelle (version {version - 1})", previous))

    response = ""
    if part.type == "audio":
        # Une voix de synthèse lit un texte : elle ne peut pas « corriger » une critique.
        # Si le texte doit changer, c'est la partie dont elle dépend qui est révisée.
        spoken, origin = spoken_text(rt, plan, part)
        if not spoken:
            raise AgentError(
                f"Partie audio {part.id} : aucun texte à lire (prompt vide et aucune partie dont elle dépende).",
                retryable=False, agent=part.agent,
            )
        files_dir = project.version_files_dir(part.id, version)
        if files_dir.exists():
            shutil.rmtree(files_dir)
        files_dir.mkdir(parents=True)
        filename = part.expected_output.filename or "narration.wav"
        result = agent.run(AgentTask(
            role=role, stage=stage, part_id=part.id, prompt=spoken, kind="audio", workdir=files_dir,
            meta={**meta, "filename": filename},
        ))
        produced = result.files[0] if result.files else None
        length = wav_duration(produced) if produced else None
        report = [f"Narration générée par {agent.name} à partir de {origin}."]
        if produced:
            report.append(f"Fichier audio : {produced.name} ({produced.stat().st_size} octets"
                          + (f", durée {length:.1f} s" if length else "") + ").")
        report.append("Le relecteur ne peut pas écouter le fichier : il juge le texte lu et la présence du fichier.")
        if revising:
            report.append("Version régénérée : la voix ne réécrit pas le texte ; pour le modifier, il faut réviser la partie dont celle-ci dépend.")
        content = "\n\n".join(report) + f"\n\nTexte lu :\n\n{spoken}"

    elif part.type in MEDIA_TYPES:
        # Un modèle d'image lit un prompt, pas une critique : on ne garde que les corrections.
        text = part.prompt.strip()
        fixes = [instruction] if instruction else []
        if critique is not None:
            fixes += [p.correction for p in critique.problems]
        if fixes:
            text += "\n\nCorrections par rapport à la version précédente : " + " ; ".join(f.strip() for f in fixes if f.strip())
        files_dir = project.version_files_dir(part.id, version)
        if files_dir.exists():
            shutil.rmtree(files_dir)
        files_dir.mkdir(parents=True)
        result = agent.run(AgentTask(
            role=role, stage=stage, part_id=part.id, prompt=text, kind=part.type, workdir=files_dir, meta=meta,
        ))
        content = result.text or f"{part.type} générée."

    elif part.type in FILE_TYPES:
        workdir = project.work_dir(part.id)
        if revising:
            runner_note = (
                "Le routeur relancera Blender sur `scene.py` ; ne le lance pas toi-même."
                if part.type == "blender" else "Vérifie ce que tu peux vérifier avant de conclure."
            )
            text = prompt(
                rt, "revise_files", title=part.title, version=version - 1, part_prompt=part.prompt,
                criteria=criteria, reason=reason, runner_note=runner_note,
            )
        elif part.type == "blender":
            text = prompt(
                rt, "produce_blender", title=part.title, part_prompt=part.prompt, criteria=criteria,
                blend_name=part.expected_output.filename or "scene.blend",
            )
        else:
            text = prompt(
                rt, "produce_files", title=part.title, part_prompt=part.prompt, criteria=criteria,
                filename=part.expected_output.filename or "(au choix)",
            )
        task = AgentTask(
            role=role, stage=stage, part_id=part.id, system=system(rt, "producer"), prompt=text,
            context=context, kind="files", workdir=workdir, meta=meta,
        )
        result = agent.run(task)
        content, response = split_response(result.text)
        if part.type == "blender":
            content += "\n\n" + _run_blender(rt, part, agent, workdir, stage, meta)
        _snapshot(workdir, project.version_files_dir(part.id, version))

    else:  # texte
        if revising:
            text = prompt(
                rt, "revise", title=part.title, version=version - 1, part_prompt=part.prompt,
                criteria=criteria, reason=reason,
            )
        else:
            text = prompt(rt, "produce", title=part.title, part_prompt=part.prompt, criteria=criteria)
        result = agent.run(AgentTask(
            role=role, stage=stage, part_id=part.id, system=system(rt, "producer"), prompt=text,
            context=context, meta=meta,
        ))
        content, response = split_response(result.text)

    project.write_text(project.version_path(part.id, version), content.strip() + "\n")
    if response:
        project.write_text(project.response_path(part.id, version), response + "\n")
    rt.event("part_version", stage=stage, part_id=part.id, version=version, agent=part.agent, revised=revising)


def _run_blender(rt: Runtime, part: Part, agent, workdir: Path, stage: str, meta: dict) -> str:
    """Exécute scene.py dans Blender ; en cas d'échec, renvoie l'erreur à l'auteur pour correction."""
    cfg = rt.config
    attempts = max(1, cfg.blender.max_fix_attempts)
    if cfg.mock and not cfg.mock_scenario.get("real_blender"):
        # Simulation : pas de Blender sur la machine, on fabrique une sortie factice.
        (workdir / "scene.blend").write_bytes(b"BLENDER-simulation")
        return "## Exécution Blender\n\nExécution simulée (mode simulation) : `scene.blend` factice créé."
    run = None
    for attempt in range(1, attempts + 1):
        rt.event("blender_run", stage=stage, part_id=part.id, attempt=attempt)
        run = blender.run_script(cfg.paths.blender_bin, workdir / "scene.py", workdir, timeout=cfg.blender.timeout)
        (workdir / "blender.log").write_text(run.log, encoding="utf-8")
        rt.event("blender_result", stage=stage, part_id=part.id, attempt=attempt, ok=run.ok, duration=run.duration)
        if run.ok or attempt == attempts:
            break
        fix = prompt(rt, "blender_fix", title=part.title, attempt=attempt, log=run.tail(4000))
        agent.run(AgentTask(
            role=ROLE_REVISE, stage=stage, part_id=part.id, system=system(rt, "producer"), prompt=fix,
            kind="files", workdir=workdir, meta={**meta, "blender_fix": attempt},
        ))
    assert run is not None
    status = "réussie" if run.ok else "EN ÉCHEC"
    produced = ", ".join(sorted(p.name for p in workdir.iterdir() if p.is_file())) or "aucun"
    return (
        f"## Exécution Blender\n\nExécution {status} après {attempt} tentative(s) "
        f"({run.duration:.1f} s). Fichiers présents : {produced}.\n\n"
        f"Fin du journal Blender :\n\n```\n{run.tail(1500)}\n```"
    )


def review_material(rt: Runtime, part: Part, version: int) -> tuple[list[tuple[str, str]], list[Path]]:
    """Ce que le relecteur reçoit pour juger une version : texte, fichiers, images."""
    project = rt.project
    assert project is not None
    content = project.read_text(project.version_path(part.id, version))
    blocks = [(f"Version {version} à relire — {part.title}", content)]
    file_blocks, images = files_context(rt, part.id, version)
    return blocks + file_blocks, images


__all__ = ["generate", "review_material", "AgentResult"]
