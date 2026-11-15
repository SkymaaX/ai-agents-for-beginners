"""Étape 7 — le chef (Qwen) compare le livrable à la demande initiale : OK ou renvoi ciblé."""

from __future__ import annotations

from ..agents.base import ROLE_VALIDATE, AgentTask, ask_json
from ..prompts import bullets
from ..runtime import Runtime
from ..schemas import Verdict
from .common import active_parts, load_brief, prompt, propagate_to_audio, system


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    state = project.state
    cfg = rt.config
    plan = project.load_plan()
    parts = active_parts(project, plan)
    part_ids = [p.id for p in parts]
    brief = load_brief(project)
    version = state.assembly_version
    deliverable = project.read_text(project.assembly_path(version))
    attempt = state.validation_returns + 1
    assets = sorted(p.relative_to(project.deliverable_dir).as_posix() for p in project.assets_dir.rglob("*") if p.is_file()) \
        if project.assets_dir.exists() else []
    assets_note = ""
    if assets:
        assets_note = " Fichiers joints au livrable (non affichés ici) : " + ", ".join(assets) + "."

    def valid(verdict: Verdict) -> None:
        if not verdict.ok and not verdict.gaps:
            raise ValueError("ok=false exige au moins un élément dans gaps (manque + consigne de correction).")

    task = AgentTask(
        role=ROLE_VALIDATE,
        stage="validate",
        system=system(rt, "chef"),
        prompt=prompt(
            rt, "validate", attempt=attempt, request=project.full_request(),
            criteria=bullets(brief.acceptance_criteria, "(aucun critère : se référer à la demande)"),
            parts="\n".join(f"- {p.id} : {p.title} ({p.type}, par {p.agent})" for p in parts),
            assets_note=assets_note,
        ),
        context=[(f"Livrable final (version {version})", deliverable)],
        meta={"attempt": attempt, "part_ids": part_ids},
    )
    verdict = ask_json(rt.role("chef"), task, Verdict, repairs=cfg.json_repairs, validator=valid)
    project.write_json(project.verdict_path(version), {
        "assembly_version": version, "attempt": attempt, "validator": cfg.roles.chef, **verdict.model_dump(),
    })

    if verdict.ok:
        state.validated_version = version
        project.save()
        rt.event("verdict", stage="validate", ok=True, version=version)
        return

    if state.validation_returns >= cfg.validation.max_returns:
        # Limite de renvois atteinte : on livre, avec les manques signalés noir sur blanc.
        state.validated_version = version
        state.reserves = [gap.description for gap in verdict.gaps]
        project.save()
        rt.event("verdict", stage="validate", ok=False, version=version, final=True, gaps=len(verdict.gaps))
        return

    state.validation_returns += 1
    assembly_notes: list[str] = []
    for gap in verdict.gaps:
        if gap.part_id in part_ids:
            part_state = state.parts[gap.part_id]
            note = f"{gap.description} — {gap.instruction}"
            part_state.instruction = (part_state.instruction + "\n" + note).strip()
            part_state.status = "to_revise"
            part_state.max_rounds = cfg.review.rounds_after_return
        else:
            assembly_notes.append(f"{gap.description} — {gap.instruction}")
    propagate_to_audio(project, plan, [g.part_id for g in verdict.gaps if g.part_id in part_ids], cfg.review.rounds_after_return)
    # Les parties corrigées changent : l'assembleur doit toujours savoir pourquoi il repasse.
    state.assembly_instruction = "\n".join(
        assembly_notes or [f"Parties corrigées après contrôle du chef : {verdict.summary}"]
    )
    rt.event("verdict", stage="validate", ok=False, version=version, gaps=len(verdict.gaps), returns=state.validation_returns)
    project.reset_stages("review", "assemble", "validate")  # le pipeline reprend à la critique
