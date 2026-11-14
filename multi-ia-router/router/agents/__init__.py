"""Registre des types d'agents.

Pour brancher une nouvelle IA (ex. OpenAI) :
1. créer `router/agents/openai.py` avec une classe héritant de `Agent` ;
2. l'ajouter à `AGENT_TYPES` ci-dessous ;
3. déclarer un agent `type: openai` dans config.yaml.
Les modules sont importés à la demande : un SDK absent ne bloque que son agent.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from ..config import AgentCfg
from ..errors import ConfigError
from .base import Agent

if TYPE_CHECKING:
    from ..runtime import Runtime

AGENT_TYPES: dict[str, str] = {
    "anythingllm": "router.agents.qwen_anythingllm:AnythingLLMAgent",
    "gemini": "router.agents.gemini:GeminiAgent",
    "claude_api": "router.agents.claude_api:ClaudeApiAgent",
    "claude_cli": "router.agents.claude_cli:ClaudeCliAgent",
    "comfyui": "router.agents.comfyui:ComfyUIAgent",
    "mock": "router.agents.mock:MockAgent",
}


def agent_class(type_name: str) -> type[Agent]:
    target = AGENT_TYPES.get(type_name)
    if target is None:
        raise ConfigError(f"Type d'agent inconnu : « {type_name} ». Types connus : {sorted(AGENT_TYPES)}")
    module_name, _, class_name = target.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


def build_agent(name: str, cfg: AgentCfg, runtime: "Runtime") -> Agent:
    """Construit l'agent `name`. En mode simulation, un agent simulé prend sa place."""
    cls = agent_class(cfg.type)
    if runtime.config.mock and cfg.type != "mock":
        from .mock import MockAgent

        return MockAgent(name, cfg, runtime, stands_for=cls)
    return cls(name, cfg, runtime)
