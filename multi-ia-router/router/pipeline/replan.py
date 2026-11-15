"""Modification d'un projet terminé : seules les parties touchées repassent.

Le répartiteur traduit la demande de modification en plan ciblé : parties à retoucher,
à ajouter, à retirer, recherche complémentaire éventuelle. Tout le reste est réutilisé.
"""

from __future__ import annotations

from ..agents.base import ROLE_REPLAN, AgentTask, ask_json
from ..config import CAPABILITY_FOR_TYPE
from ..research.dossier import build_dossier
from ..runtime import Runtime
from ..schemas import Plan, RevisionPlan, check_plan
from ..state import PartState
from .common import active_parts, agents_text, prompt, propagate_to_audio, system


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    state = project.state
    cfg = rt.config
    plan = project.load_plan()
    modification = next((m for m in state.modifications if m.status == "pending"), None)
    if modification is None:
        return
    current = active_parts(project, plan)
    current_ids = [p.id for p in current]
    all_ids = [p.id for p in plan.parts]
    agents, capabilities = agents_text(rt)
    plan_text = "\n".join(
        f"- {p.id} : {p.title} — type {p.type}, agent {p.agent}\n  Critères : {'; '.join(p.success_criteria) or '—'}"
        for p in current
    )
    # La demande de référence exclut la modification en cours, donnée à part.
    previous = [m for m in state.modifications if m.n < modification.n]
    request = state.request.strip()
    if previous:
        request += "\n\nModifications déjà appliquées :\n" + "\n".join(f"{m.n}. {m.instruction}" for m in previous)

    def valid(revision: RevisionPlan) -> None:
        problems: list[str] = []
        for change in revision.changes:
            if change.part_id not in current_ids:
                problems.append(f"changes : partie inconnue « {change.part_id} » (parties : {current_ids}).")
        for part_id in revision.remove_parts:
            if part_id not in current_ids:
                problems.append(f"remove_parts : partie inconnue « {part_id} ».")
        for part in revision.new_parts:
            if part.id in all_ids:
                problems.append(f"new_parts : l'id « {part.id} » existe déjà, choisis-en un autre.")
        if not (revision.changes or revision.new_parts or revision.remove_parts or revision.assembly_instructions.strip()):
            problems.append("Le plan de modification est vide : indique au moins un changement.")
        if problems:
            raise ValueError(" ".join(problems))
        kept = [p for p in plan.parts if p.id not in revision.remove_parts]
        check_plan(Plan(project_title=plan.project_title, parts=kept + revision.new_parts), capabilities, CAPABILITY_FOR_TYPE)

    task = AgentTask(
        role=ROLE_REPLAN,
        stage="replan",
        system=system(rt, "dispatcher"),
        prompt=prompt(rt, "replan", request=request, plan=plan_text, agents=agents, instruction=modification.instruction),
        meta={"part_ids": current_ids, "instruction": modification.instruction},
    )
    revision = ask_json(rt.role("dispatcher"), task, RevisionPlan, repairs=cfg.json_repairs, validator=valid)
    project.write_json(project.modification_path(modification.n), {
        "n": modification.n, "instruction": modification.instruction, **revision.model_dump(),
    })

    for change in revision.changes:
        part_state = state.parts[change.part_id]
        part_state.status = "to_revise"
        part_state.instruction = change.instruction
        part_state.max_rounds = cfg.review.rounds_after_return
    for part_id in revision.remove_parts:
        state.parts[part_id].status = "removed"
    for part in plan.parts:
        part.depends_on = [d for d in part.depends_on if d not in revision.remove_parts]
    for part in revision.new_parts:
        plan.parts.append(part)
        state.parts[part.id] = PartState(author=part.agent)
    if revision.assembly_instructions.strip():
        plan.assembly_instructions = revision.assembly_instructions.strip()
    project.write_json(project.plan_path, plan)
    narrations = propagate_to_audio(project, plan, [c.part_id for c in revision.changes], cfg.review.rounds_after_return)

    # L'assembleur reçoit toujours la demande de modification : c'est lui qui la rend visible.
    state.assembly_instruction = f"Modification demandée par le client : {modification.instruction}"
    project.save()

    queries = revision.research_queries
    if queries.vault or queries.web:
        existing = project.read_json(project.research_dir / "sources.json", []) or []
        markdown, sources = build_dossier(
            rt, queries, state.title or plan.project_title,
            heading=f"Complément de recherche (modification {modification.n})", start=len(existing),
        )
        previous_text = project.read_text(project.research_path).rstrip()
        project.write_text(project.research_path, previous_text + "\n\n" + markdown)
        project.write_json(project.research_dir / "sources.json", existing + sources)

    rt.event(
        "replan", stage="replan", changed=[c.part_id for c in revision.changes] + narrations,
        added=[p.id for p in revision.new_parts], removed=revision.remove_parts,
    )
