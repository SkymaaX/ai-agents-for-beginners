"""Étape 2 — le répartiteur (Gemini) découpe la demande en plan JSON strict."""

from __future__ import annotations

from ..agents.base import ROLE_PLAN, AgentTask, ask_json
from ..config import CAPABILITY_FOR_TYPE
from ..runtime import Runtime
from ..schemas import Plan, check_plan
from ..state import PartState
from .common import agents_text, brief_text, load_brief, prompt, system


def run(rt: Runtime) -> None:
    project = rt.project
    assert project is not None
    brief = load_brief(project)
    agents, capabilities = agents_text(rt)
    task = AgentTask(
        role=ROLE_PLAN,
        stage="plan",
        system=system(rt, "dispatcher"),
        prompt=prompt(rt, "plan", request=project.state.request, brief=brief_text(brief), agents=agents),
        meta={
            "request": project.state.request,
            "title": brief.title,
            "producers": {name: sorted(caps) for name, caps in capabilities.items()},
        },
    )
    plan = ask_json(
        rt.role("dispatcher"), task, Plan, repairs=rt.config.json_repairs,
        validator=lambda p: check_plan(p, capabilities, CAPABILITY_FOR_TYPE),
    )
    project.write_json(project.plan_path, plan)
    for part in plan.parts:
        project.state.parts[part.id] = PartState(author=part.agent)
    if not project.state.title:
        project.state.title = plan.project_title
    project.save()
    rt.event("plan", stage="plan", parts=len(plan.parts), agents=sorted({p.agent for p in plan.parts}))

