"""Contexte d'exécution : config + projet + agents + journaux, partagé par tout le pipeline."""

from __future__ import annotations

from typing import Any

from .agents import build_agent
from .agents.base import Agent
from .config import Config
from .logs import append_jsonl, logger, stamp
from .state import Project
from .tools.gpu_lock import GpuArbiter


class Runtime:
    def __init__(self, config: Config, project: Project | None = None):
        self.config = config
        self.project = project
        self.gpu = GpuArbiter(config.gpu.exclusive, config.gpu.settle_seconds, on_event=self.event)
        self._agents: dict[str, Agent] = {}
        self.counters: dict[str, int] = {}  # compteurs de l'agent simulé (scénarios de test)

    # --------------------------------------------------------------------------- agents
    def agent(self, name: str) -> Agent:
        if name not in self._agents:
            self._agents[name] = build_agent(name, self.config.agent_cfg(name), self)
        return self._agents[name]

    def role(self, role: str) -> Agent:
        return self.agent(getattr(self.config.roles, role))

    def critic_for(self, author: str) -> Agent:
        return self.agent(self.config.critic_name_for(author))

    def producers(self) -> dict[str, Agent]:
        return {name: self.agent(name) for name in self.config.enabled_producers()}

    # ------------------------------------------------------------------------- journaux
    def event(self, type_: str, **fields: Any) -> None:
        record = {"ts": stamp(), "type": type_, **{k: v for k, v in fields.items() if v not in ("", None)}}
        if self.project is not None:
            append_jsonl(self.project.events_path, record)
        logger.debug("événement %s %s", type_, fields)

    def log_call(self, record: dict[str, Any]) -> None:
        if self.project is not None:
            record = {"project": self.project.id, **record}
            append_jsonl(self.project.calls_path, record)
        status = "ok" if record.get("ok") else f"ERREUR {record.get('error', '')[:120]}"
        logger.info(
            "appel %-12s %-24s %-4s %6.1fs  %s→%s tokens  %s",
            record.get("agent", ""), record.get("role", ""), record.get("part_id", ""),
            record.get("duration_s", 0.0), record.get("tokens_in", 0), record.get("tokens_out", 0), status,
        )

    def set_current(self, **fields: str) -> None:
        if self.project is None:
            return
        current = self.project.state.current
        for key, value in fields.items():
            setattr(current, key, value or "")
        self.project.save()
