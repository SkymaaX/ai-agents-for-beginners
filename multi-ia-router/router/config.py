"""Chargement de la configuration.

Ordre : config.yaml (défauts versionnés) ← config.local.yaml (réglages du PC, ignoré par git)
← variables d'environnement (.env). Changer qui fait quoi = modifier le YAML, pas le code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent  # dossier multi-ia-router/

# Capacité exigée d'un agent pour chaque type de partie du plan.
CAPABILITY_FOR_TYPE = {
    "text": "text",
    "code": "files",
    "files": "files",
    "blender": "files",
    "image": "image",
    "video": "video",
}


class AgentCfg(BaseModel):
    """Réglages d'un agent. Les champs propres à un type (base_url, workspace...) sont libres."""

    model_config = ConfigDict(extra="allow")

    type: str
    family: str = ""  # éditeur du modèle : sert à interdire l'auto-critique
    enabled: bool = True
    model: str = ""
    description: str = ""
    timeout: float = 600.0
    retries: int = 3
    backoff: float = 2.0
    max_context_chars: int = 0  # 0 = pas de limite
    capabilities: list[str] | None = None  # None = capacités par défaut du type

    def get(self, key: str, default: Any = None) -> Any:
        extra = self.model_extra or {}
        return extra.get(key, default)


class RolesCfg(BaseModel):
    chef: str = "qwen"
    dispatcher: str = "gemini"
    assembler: str = "claude"
    executor: str = "claude_code"
    researcher: str = "gemini"
    producers: list[str] = Field(default_factory=lambda: ["gemini", "claude", "claude_code"])
    critics: dict[str, str] = Field(default_factory=dict)  # auteur → critique ; "default" en repli


class ReviewCfg(BaseModel):
    max_rounds: int = 3
    min_score: int = 4  # note minimale (0-5) sur chaque critère pour accepter
    rounds_after_return: int = 1  # tours de critique après un renvoi du chef ou une modification
    max_attachments: int = 4
    max_file_chars: int = 20000


class ValidationCfg(BaseModel):
    max_returns: int = 2


class VaultCfg(BaseModel):
    enabled: bool = True
    agent: str = "qwen"  # agent AnythingLLM dont on réutilise l'URL et la clé
    workspace: str = "vault"
    top_n: int = 6
    score_threshold: float = 0.25


class WebCfg(BaseModel):
    enabled: bool = True
    provider: str = "gemini_grounding"  # gemini_grounding | searxng
    searxng_url: str = "http://localhost:8080"
    max_queries: int = 5
    max_results: int = 5
    fetch_pages: bool = True
    max_page_chars: int = 12000
    rate_limit_seconds: float = 2.0
    cache_ttl_hours: float = 168.0
    respect_robots: bool = True
    user_agent: str = "multi-ia-router/1.0 (usage personnel ; respecte robots.txt)"
    timeout: float = 30.0


class ResearchCfg(BaseModel):
    vault: VaultCfg = Field(default_factory=VaultCfg)
    web: WebCfg = Field(default_factory=WebCfg)
    max_chars: int = 30000  # taille max du dossier de recherche injecté dans les prompts


class PathsCfg(BaseModel):
    projects_dir: str = "projects"
    cache_dir: str = "cache"
    prompts_dir: str = ""  # vide = prompts fournis avec le code
    vault_dir: str = ""  # chemin de la vault Obsidian ; vide = pas de copie
    vault_export_subdir: str = "Livrables IA"
    blender_bin: str | list[str] = "blender"  # chemin de blender(.exe)


class GpuCfg(BaseModel):
    exclusive: bool = True  # True = jamais Qwen et ComfyUI en même temps
    llm_backend: str = "ollama"  # ollama | lmstudio | command | none
    ollama_url: str = "http://localhost:11434"
    unload_command: list[str] = Field(default_factory=list)
    settle_seconds: float = 2.0


class BlenderCfg(BaseModel):
    timeout: float = 900.0
    max_fix_attempts: int = 3


class DashboardCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    local_only: bool = True  # false = accepte les requêtes relayées par un proxy de confiance (aperçu distant)


class Config(BaseModel):
    language: str = "fr"
    mock: bool = False
    mock_delay: float = 0.0
    mock_scenario: dict[str, Any] = Field(default_factory=dict)
    json_repairs: int = 2
    paths: PathsCfg = Field(default_factory=PathsCfg)
    roles: RolesCfg = Field(default_factory=RolesCfg)
    agents: dict[str, AgentCfg] = Field(default_factory=dict)
    review: ReviewCfg = Field(default_factory=ReviewCfg)
    validation: ValidationCfg = Field(default_factory=ValidationCfg)
    research: ResearchCfg = Field(default_factory=ResearchCfg)
    gpu: GpuCfg = Field(default_factory=GpuCfg)
    blender: BlenderCfg = Field(default_factory=BlenderCfg)
    dashboard: DashboardCfg = Field(default_factory=DashboardCfg)

    root: Path = Field(default=ROOT, exclude=True)

    # ------------------------------------------------------------------ chemins
    def resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    @property
    def projects_dir(self) -> Path:
        return self.resolve(self.paths.projects_dir)

    @property
    def cache_dir(self) -> Path:
        return self.resolve(self.paths.cache_dir)

    @property
    def prompts_dir(self) -> Path:
        if self.paths.prompts_dir:
            return self.resolve(self.paths.prompts_dir)
        return Path(__file__).resolve().parent / "prompts"

    # ------------------------------------------------------------------- agents
    def agent_cfg(self, name: str) -> AgentCfg:
        if name not in self.agents:
            raise ConfigError(f"Agent inconnu dans la config : « {name} »")
        return self.agents[name]

    def family(self, name: str) -> str:
        cfg = self.agent_cfg(name)
        return cfg.family or cfg.type

    def critic_name_for(self, author: str) -> str:
        """Nom de l'agent qui critique `author`. Personne ne juge sa propre copie."""
        critic = self.roles.critics.get(author) or self.roles.critics.get("default")
        if not critic:
            raise ConfigError(f"Aucun critique configuré pour « {author} » (roles.critics).")
        if self.family(critic) == self.family(author):
            raise ConfigError(
                f"« {critic} » ne peut pas critiquer « {author} » : même famille "
                f"({self.family(author)}). Personne ne juge sa propre copie."
            )
        return critic

    def enabled_producers(self) -> list[str]:
        return [n for n in self.roles.producers if n in self.agents and self.agents[n].enabled]

    def check(self) -> None:
        """Vérifie la cohérence : rôles pourvus, agents actifs, critiques valides."""
        roles = self.roles
        for role in ("chef", "dispatcher", "assembler", "researcher"):
            name = getattr(roles, role)
            cfg = self.agent_cfg(name)
            if not cfg.enabled:
                raise ConfigError(f"Le rôle « {role} » pointe sur l'agent désactivé « {name} ».")
        producers = self.enabled_producers()
        if not producers:
            raise ConfigError("Aucun agent producteur actif (roles.producers).")
        for name in producers:
            critic = self.critic_name_for(name)
            if not self.agent_cfg(critic).enabled:
                raise ConfigError(f"Le critique « {critic} » de « {name} » est désactivé.")


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} doit contenir un dictionnaire YAML.")
    return data


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict | None = None,
    root: Path | None = None,
    check: bool = True,
) -> Config:
    """Charge la config. `overrides` sert aux tests et aux options de ligne de commande."""
    root = Path(root) if root else ROOT
    load_dotenv(root / ".env")
    main = Path(path) if path else root / "config.yaml"
    data = _read_yaml(main)
    data = _deep_merge(data, _read_yaml(main.with_name("config.local.yaml")))
    if overrides:
        data = _deep_merge(data, overrides)
    if os.environ.get("ROUTER_MOCK", "").strip().lower() in ("1", "true", "yes", "oui"):
        data["mock"] = True
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Configuration invalide :\n{exc}") from exc
    config.root = root
    if check:
        config.check()
    return config
