"""Briques partagées par les étapes : contexte commun, lecture des versions, fichiers joints."""

from __future__ import annotations

from pathlib import Path

from ..prompts import bullets, render
from ..runtime import Runtime
from ..schemas import Brief, Part, Plan
from ..state import IMAGE_EXT, TEXT_EXT, Project

LANGUAGES = {"fr": "français", "en": "anglais", "es": "espagnol", "de": "allemand", "it": "italien"}
RESPONSE_MARK = "<<<REPONSE_CRITIQUE>>>"


def language(rt: Runtime) -> str:
    return LANGUAGES.get(rt.config.language, rt.config.language)


def prompt(rt: Runtime, name: str, **variables: object) -> str:
    return render(rt.config.prompts_dir, name, language=language(rt), **variables)


def system(rt: Runtime, name: str) -> str:
    return render(rt.config.prompts_dir, f"system_{name}")


def load_brief(project: Project) -> Brief:
    return Brief.model_validate(project.read_json(project.brief_path))


def brief_text(brief: Brief) -> str:
    return (
        f"Titre : {brief.title}\n\nObjectif : {brief.objective}\n\n"
        f"Livrables attendus :\n{bullets(brief.deliverables)}\n\n"
        f"Contraintes :\n{bullets(brief.constraints)}\n\n"
        f"Critères d'acceptation :\n{bullets(brief.acceptance_criteria)}\n\n"
        f"Questions ouvertes et hypothèses :\n{bullets(brief.open_questions)}"
    )


def research_text(rt: Runtime) -> str:
    project = rt.project
    assert project is not None
    text = project.read_text(project.research_path)
    limit = rt.config.research.max_chars
    if limit and len(text) > limit:
        from ..textutil import fit_text

        text = fit_text(text, limit)
    return text


def base_context(rt: Runtime) -> list[tuple[str, str]]:
    """Contexte commun à toutes les parties : demande, brief, dossier de recherche."""
    project = rt.project
    assert project is not None
    blocks = [
        ("Demande du client", project.full_request()),
        ("Brief du chef de projet", brief_text(load_brief(project))),
    ]
    research = research_text(rt)
    if research.strip():
        blocks.append(("Dossier de recherche commun", research))
    return blocks


def agents_text(rt: Runtime) -> tuple[str, dict[str, set[str]]]:
    """Description des agents producteurs pour le répartiteur + leurs capacités."""
    lines: list[str] = []
    capabilities: dict[str, set[str]] = {}
    for name, agent in rt.producers().items():
        caps = agent.capabilities
        capabilities[name] = caps
        description = agent.cfg.description or agent.cfg.type
        lines.append(f"- {name} — capacités : {', '.join(sorted(caps))}. {description}")
    return "\n".join(lines), capabilities


def split_response(text: str) -> tuple[str, str]:
    """Sépare la version révisée de la réponse de l'auteur à la critique."""
    if RESPONSE_MARK in text:
        content, _, response = text.partition(RESPONSE_MARK)
        return content.strip(), response.strip()
    return text.strip(), ""


def active_parts(project: Project, plan: Plan) -> list[Part]:
    """Parties du plan dans l'ordre des dépendances, hors parties retirées."""
    return [p for p in plan.ordered() if project.state.parts[p.id].status != "removed"]


def best_version(project: Project, part_id: str) -> int:
    state = project.state.parts[part_id]
    return state.accepted_version or state.version


def files_context(rt: Runtime, part_id: str, version: int) -> tuple[list[tuple[str, str]], list[Path]]:
    """Fichiers d'une version : contenu des fichiers texte (borné) + images à joindre."""
    project = rt.project
    assert project is not None
    review = rt.config.review
    files = project.version_files(part_id, version)
    if not files:
        return [], []
    root = project.version_files_dir(part_id, version)
    listing = "\n".join(f"- {p.relative_to(root).as_posix()} ({p.stat().st_size} octets)" for p in files)
    blocks = [(f"Fichiers de la version {version}", listing)]
    budget = review.max_file_chars
    for path in files:
        if path.suffix.lower() not in TEXT_EXT or budget <= 0:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(content) > budget:
            content = content[:budget] + f"\n[… fichier coupé, {len(content) - budget} caractères restants …]"
        budget -= len(content)
        blocks.append((f"Fichier {path.relative_to(root).as_posix()}", content))
    images = [p for p in files if p.suffix.lower() in IMAGE_EXT][: review.max_attachments]
    return blocks, images


def dependency_context(project: Project, plan: Plan, part: Part) -> list[tuple[str, str]]:
    blocks = []
    for dep_id in part.depends_on:
        if project.state.parts[dep_id].status == "removed":
            continue
        version = best_version(project, dep_id)
        if version:
            dep = plan.part(dep_id)
            text = project.read_text(project.version_path(dep_id, version))
            blocks.append((f"Partie dont celle-ci dépend : {dep.title} ({dep_id}, version {version})", text))
    return blocks
