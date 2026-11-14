"""Étape 5 — critique croisée.

Pour chaque partie : un agent d'une AUTRE famille que l'auteur note la version sur la grille
fixe et propose des corrections ; l'auteur révise ; on recommence. Au bout de `max_rounds`
tours sans accord, le chef (Qwen) tranche. Personne ne juge sa propre copie : c'est vérifié
par `Config.critic_name_for`, qui refuse un critique de la même famille que l'auteur.
"""

from __future__ import annotations

from ..agents.base import ROLE_ARBITER, ROLE_CRITIC, AgentTask, ask_json
from ..prompts import bullets
from ..runtime import Runtime
from ..schemas import Arbitration, Critique, Part, Plan
from .common import active_parts, base_context, dependency_context, prompt, system
from .parts import generate, review_material


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    plan = project.load_plan()
    for part in active_parts(project, plan):
        review_part(rt, plan, part)


def review_part(rt: Runtime, plan: Plan, part: Part) -> None:
    project = rt.project
    assert project is not None
    cfg = rt.config
    state = project.state.parts[part.id]

    if state.status == "to_revise":
        # Renvoi du chef ou modification demandée : l'auteur corrige, puis relecture.
        version = state.version + 1
        generate(rt, plan, part, version, instruction=state.instruction)
        state.version = version
        state.cycle_start = version
        state.instruction = ""
        state.status = "produced"
        project.save()
        rt.event("part_update", stage="review", part_id=part.id, status="produced", version=version)

    max_rounds = state.max_rounds or cfg.review.max_rounds
    critic = rt.critic_for(part.agent)
    while state.status == "produced":
        version = state.version
        round_no = version - state.cycle_start + 1
        critique = _critique(rt, plan, part, critic.name, version, round_no, max_rounds)
        state.rounds = round_no
        if critique.accepted(cfg.review.min_score):
            state.status = "accepted"
            state.accepted_version = version
            state.reserves = []
            project.save()
            rt.event("part_update", stage="review", part_id=part.id, status="accepted", version=version, round=round_no)
            break
        rt.event(
            "part_update", stage="review", part_id=part.id, status="revise", version=version, round=round_no,
            problems=len(critique.problems),
        )
        if round_no >= max_rounds:
            _arbitrate(rt, part, critic.name, list(range(state.cycle_start, version + 1)))
            break
        generate(rt, plan, part, version + 1, critique=critique)
        state.version = version + 1
        project.save()


def _critique(rt: Runtime, plan: Plan, part: Part, critic_name: str, version: int, round_no: int, max_rounds: int) -> Critique:
    project = rt.project
    assert project is not None
    path = project.critique_path(part.id, version)
    if path.exists():  # reprise : la critique de cette version existe déjà
        return Critique.model_validate(project.read_json(path))
    material, images = review_material(rt, part, version)
    context = base_context(rt) + dependency_context(project, plan, part)
    response = project.read_text(project.response_path(part.id, version))
    response_note = ""
    if response:
        context.append(("Réponse de l'auteur à la critique précédente", response))
        response_note = (
            " L'auteur a répondu à la critique précédente : tiens compte de ses arguments, "
            "et ne répète pas une remarque qu'il a réfutée de façon convaincante."
        )
    context += material
    task = AgentTask(
        role=ROLE_CRITIC,
        stage="review",
        part_id=part.id,
        system=system(rt, "critic"),
        prompt=prompt(
            rt, "critique", version=version, title=part.title, author=part.agent, round=round_no,
            max_rounds=max_rounds, part_prompt=part.prompt,
            criteria=bullets(part.success_criteria, "(aucun critère fourni)"),
            response_note=response_note, min_score=rt.config.review.min_score,
        ),
        context=context,
        attachments=images,
        meta={"round": round_no, "version": version, "author": part.agent},
    )
    critique = ask_json(rt.agent(critic_name), task, Critique, repairs=rt.config.json_repairs)
    project.write_json(path, {
        "part_id": part.id, "version": version, "round": round_no,
        "author": part.agent, "critic": critic_name, **critique.model_dump(),
    })
    return critique


def _arbitrate(rt: Runtime, part: Part, critic_name: str, versions: list[int]) -> None:
    """Le chef choisit la version retenue quand auteur et relecteur ne convergent pas."""
    project = rt.project
    assert project is not None
    state = project.state.parts[part.id]
    context: list[tuple[str, str]] = [("Demande du client", project.full_request())]
    for version in versions:
        context.append((f"Version {version}", project.read_text(project.version_path(part.id, version))))
        critique = project.read_json(project.critique_path(part.id, version))
        if critique:
            problems = "\n".join(
                f"- [{p['severity']}] ({p['critere']}) {p['detail']} → {p['correction']}"
                for p in critique.get("problems", [])
            )
            context.append((
                f"Critique de la version {version}",
                f"Verdict : {critique.get('verdict')}\nNotes : {critique.get('scores')}\n"
                f"Avis : {critique.get('summary', '')}\nProblèmes :\n{problems or '(aucun)'}",
            ))
        response = project.read_text(project.response_path(part.id, version))
        if response:
            context.append((f"Réponse de l'auteur (avec la version {version})", response))

    def valid(decision: Arbitration) -> None:
        if decision.chosen_version not in versions:
            raise ValueError(f"chosen_version doit être l'une de ces versions : {versions}")

    task = AgentTask(
        role=ROLE_ARBITER,
        stage="review",
        part_id=part.id,
        system=system(rt, "chef"),
        prompt=prompt(
            rt, "arbitrate", author=part.agent, critic=critic_name, title=part.title, rounds=len(versions),
            criteria=bullets(part.success_criteria, "(aucun critère fourni)"),
            versions=", ".join(str(v) for v in versions),
        ),
        context=context,
        meta={"versions": versions},
    )
    decision = ask_json(rt.role("chef"), task, Arbitration, repairs=rt.config.json_repairs, validator=valid)
    project.write_json(project.arbitration_path(part.id), {
        "part_id": part.id, "versions": versions, "arbiter": rt.config.roles.chef, **decision.model_dump(),
    })
    state.status = "arbitrated"
    state.accepted_version = decision.chosen_version
    state.reserves = decision.reserves
    project.save()
    rt.event(
        "part_update", stage="review", part_id=part.id, status="arbitrated",
        version=decision.chosen_version, reserves=len(decision.reserves),
    )
