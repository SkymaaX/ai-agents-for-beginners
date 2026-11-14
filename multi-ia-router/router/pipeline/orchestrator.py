"""Orchestrateur : enchaîne les étapes, sauvegarde l'état, permet reprise et modification.

Machine à états simple : on exécute la première étape non terminée, jusqu'à ce qu'il n'en
reste plus. Une étape peut en remettre d'autres « à faire » (renvoi du chef) : la boucle les
reprend naturellement. Tout arrêt (panne, quota, Ctrl+C) laisse un état cohérent sur disque.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from ..config import Config
from ..errors import ConfigError, QuotaError, RouterError
from ..logs import logger
from ..runtime import Runtime
from ..state import STAGES, Modification, Project, ProjectState, StageState
from . import assemble, deliver, intake, plan, produce, replan, research, review, validate

HANDLERS = {
    "intake": intake.run,
    "plan": plan.run,
    "research": research.run,
    "replan": replan.run,
    "produce": produce.run,
    "review": review.run,
    "assemble": assemble.run,
    "validate": validate.run,
    "deliver": deliver.run,
}


def run_pipeline(rt: Runtime) -> ProjectState:
    """Exécute (ou reprend) le pipeline du projet porté par `rt`."""
    project = rt.project
    assert project is not None
    state = project.state
    state.status = "running"
    state.error = ""
    project.save()
    rt.event("pipeline_start", project=project.id, mock=rt.config.mock)
    stage = ""
    try:
        rt.config.check()
        while True:
            stage = next((s for s in STAGES if state.stages[s].status not in ("done", "skipped")), "")
            if not stage:
                break
            project.set_stage(stage, "running")
            rt.set_current(stage=stage, agent="", role="", part_id="")
            rt.event("stage_start", stage=stage)
            logger.info("── étape : %s", stage)
            HANDLERS[stage](rt)
            if state.stages[stage].status == "running":  # l'étape peut s'être remise elle-même « à faire »
                project.set_stage(stage, "done")
            rt.event("stage_end", stage=stage)
        state.status = "done"
        rt.event("pipeline_done", project=project.id)
    except QuotaError as exc:
        _stop(rt, stage, "paused", f"En pause — {exc}", "pipeline_paused")
    except KeyboardInterrupt:
        _stop(rt, stage, "paused", "Interrompu par l'utilisateur.", "pipeline_paused")
        raise
    except RouterError as exc:
        _stop(rt, stage, "failed", str(exc), "pipeline_failed")
    except Exception as exc:  # bug ou cas imprévu : état sauvegardé, trace dans le journal
        logger.error("erreur inattendue :\n%s", traceback.format_exc())
        _stop(rt, stage, "failed", f"Erreur inattendue : {type(exc).__name__} : {exc}", "pipeline_failed")
    finally:
        rt.set_current(stage="", agent="", role="", part_id="")
    return state


def _stop(rt: Runtime, stage: str, status: str, message: str, event: str) -> None:
    project = rt.project
    assert project is not None
    project.state.status = status  # type: ignore[assignment]
    project.state.error = message
    if stage:
        project.set_stage(stage, "error", message[:500])
    project.save()
    rt.event(event, stage=stage, error=message)
    logger.error("pipeline %s à l'étape « %s » : %s", "en pause" if status == "paused" else "arrêté", stage, message)


# ------------------------------------------------------------------- points d'entrée publics
def config_for(config: Config, project: Project) -> Config:
    """Un projet créé en simulation reste en simulation (reprise, modification)."""
    if project.state.mock and not config.mock:
        clone = config.model_copy(update={"mock": True})
        clone.root = config.root
        return clone
    return config


def find_project(config: Config, project_id: str) -> Project:
    """Charge un projet par identifiant exact, ou par début/fragment d'identifiant s'il est unique."""
    root = config.projects_dir
    exact = root / project_id
    if (exact / "state.json").exists():
        return Project.load(exact)
    matches = [d for d in sorted(root.glob("*")) if project_id in d.name and (d / "state.json").exists()] if root.exists() else []
    if len(matches) == 1:
        return Project.load(matches[0])
    if not matches:
        raise ConfigError(f"Projet introuvable : « {project_id} » (dans {root})")
    raise ConfigError(f"« {project_id} » correspond à plusieurs projets : {[m.name for m in matches]}")


def create_project(config: Config, request: str) -> Project:
    if not request.strip():
        raise ConfigError("La demande est vide.")
    return Project.create(config.projects_dir, request.strip(), mock=config.mock)


def start(config: Config, request: str) -> ProjectState:
    project = create_project(config, request)
    return run_pipeline(Runtime(config, project))


def resume(config: Config, project_id: str) -> ProjectState:
    project = find_project(config, project_id)
    if project.state.status == "done":
        return project.state
    return run_pipeline(Runtime(config_for(config, project), project))


def prepare_revision(project: Project, instruction: str) -> None:
    """Enregistre une demande de modification et remet « à faire » les seules étapes concernées."""
    state = project.state
    if not instruction.strip():
        raise ConfigError("La modification demandée est vide.")
    if state.status != "done":
        raise ConfigError(
            f"Le projet n'est pas terminé (état : {state.status}). Termine-le d'abord avec `resume`."
        )
    state.modifications.append(Modification(n=len(state.modifications) + 1, instruction=instruction.strip()))
    for stage in ("replan", "produce", "review", "assemble", "validate", "deliver"):
        state.stages[stage] = StageState()
    state.validation_returns = 0
    state.reserves = []
    state.status = "new"
    project.save()


def revise(config: Config, project_id: str, instruction: str) -> ProjectState:
    project = find_project(config, project_id)
    prepare_revision(project, instruction)
    return run_pipeline(Runtime(config_for(config, project), project))


def project_dir(config: Config, project_id: str) -> Path:
    return find_project(config, project_id).dir
