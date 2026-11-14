"""Interface commune à tous les agents.

Ajouter une IA = une classe qui hérite de `Agent` et implémente `_call`, plus une entrée dans
la config. Retries, délais, journalisation et réduction de contexte sont gérés ici, une fois.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import AgentCfg
from ..errors import AgentError, InvalidOutputError, QuotaError, RouterError
from ..jsonutil import extract_json, schema_text
from ..logs import logger, stamp
from ..textutil import estimate_tokens, fit_blocks

if TYPE_CHECKING:
    from ..runtime import Runtime

M = TypeVar("M", bound=BaseModel)

# Rôles joués dans le pipeline (étiquettes pour les journaux et l'agent simulé).
ROLE_INTAKE = "chef.reformulation"
ROLE_PLAN = "repartiteur.plan"
ROLE_REPLAN = "repartiteur.modification"
ROLE_RESEARCH = "chercheur"
ROLE_PRODUCE = "producteur"
ROLE_REVISE = "auteur.revision"
ROLE_CRITIC = "critique"
ROLE_ARBITER = "chef.arbitrage"
ROLE_ASSEMBLE = "assembleur"
ROLE_VALIDATE = "chef.validation"


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    estimated: bool = False


@dataclass
class AgentTask:
    """Une demande faite à un agent.

    `prompt` = la consigne (jamais coupée). `context` = documents joints (réductibles si le
    modèle a un petit contexte). `schema` = modèle Pydantic attendu en sortie JSON.
    """

    role: str
    prompt: str
    system: str = ""
    context: list[tuple[str, str]] = field(default_factory=list)  # (titre, texte)
    schema: type[BaseModel] | None = None
    kind: str = "text"  # text | image | video | files
    attachments: list[Path] = field(default_factory=list)
    workdir: Path | None = None
    stage: str = ""
    part_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    text: str = ""
    data: Any = None  # JSON déjà décodé quand l'agent le fournit
    files: list[Path] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Agent(ABC):
    type_name: ClassVar[str] = ""
    default_capabilities: ClassVar[frozenset[str]] = frozenset({"text"})
    gpu_group: ClassVar[str] = ""  # "llm" ou "diffusion" si l'agent occupe la carte graphique
    native_json: ClassVar[bool] = False  # sortie structurée garantie par l'API

    def __init__(self, name: str, cfg: AgentCfg, runtime: "Runtime"):
        self.name = name
        self.cfg = cfg
        self.runtime = runtime

    # --------------------------------------------------------------------- identité
    @property
    def capabilities(self) -> set[str]:
        if self.cfg.capabilities is not None:
            return set(self.cfg.capabilities)
        return set(self.default_capabilities)

    @property
    def family(self) -> str:
        return self.cfg.family or self.cfg.type

    # ------------------------------------------------------------------- à implémenter
    @abstractmethod
    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        """Un seul appel réel. Lever AgentError (retryable ou non), QuotaError, AgentTimeout."""

    def unload(self) -> None:
        """Libère la VRAM (agents locaux). Sans effet par défaut."""

    def diagnose(self) -> list[str]:
        """Lignes de diagnostic pour `doctor`. Vide par défaut."""
        return []

    # -------------------------------------------------------------------------- prompt
    def build_prompt(self, task: AgentTask) -> tuple[str, bool]:
        """Assemble contexte + consigne (+ schéma JSON si l'API ne le garantit pas)."""
        blocks, cut = fit_blocks(task.context, self.cfg.max_context_chars)
        parts: list[str] = []
        if blocks:
            docs = "\n\n".join(f'<document titre="{title}">\n{text.strip()}\n</document>' for title, text in blocks)
            parts.append(f"<contexte>\n{docs}\n</contexte>")
        if cut:
            parts.append(
                "Note : certains documents du contexte ont été raccourcis (coupes signalées entre crochets). "
                "Ne conclus pas qu'un élément manque s'il peut se trouver dans une zone coupée."
            )
        parts.append(task.prompt.strip())
        if task.schema is not None and not self.native_json:
            parts.append(
                "Réponds UNIQUEMENT par un objet JSON valide, sans texte avant ni après, "
                "conforme à ce schéma JSON :\n" + schema_text(task.schema)
            )
        return "\n\n".join(parts), cut

    # ------------------------------------------------------------------------ exécution
    def run(self, task: AgentTask) -> AgentResult:
        """Appel avec retries, mesure de durée et journalisation complète."""
        prompt, cut = self.build_prompt(task)
        runtime = self.runtime
        retries = max(0, int(self.cfg.retries))
        attempt = 0
        while True:
            attempt += 1
            runtime.event(
                "agent_start", stage=task.stage, agent=self.name, role=task.role,
                part_id=task.part_id, attempt=attempt, context_cut=cut,
            )
            runtime.set_current(stage=task.stage, agent=self.name, role=task.role, part_id=task.part_id)
            started = time.monotonic()
            error: AgentError | None = None
            result: AgentResult | None = None
            try:
                with runtime.gpu.use(self):
                    result = self._call(task, prompt)
            except AgentError as exc:
                error = exc
                error.agent = error.agent or self.name
            except RouterError as exc:  # config incomplète (clé absente, binaire introuvable) : inutile de réessayer
                error = AgentError(str(exc), retryable=False, agent=self.name)
            except Exception as exc:  # erreur imprévue d'un SDK : traitée comme passagère
                error = AgentError(f"{type(exc).__name__}: {exc}", retryable=True, agent=self.name)
            duration = round(time.monotonic() - started, 3)

            if result is not None:
                usage = result.usage
                if not usage.tokens_in and not usage.tokens_out:
                    usage.tokens_in = estimate_tokens(task.system + prompt)
                    usage.tokens_out = estimate_tokens(result.text)
                    usage.estimated = True
            self._log(task, prompt, attempt, duration, result, error)

            if error is None and result is not None:
                runtime.event(
                    "agent_end", stage=task.stage, agent=self.name, role=task.role, part_id=task.part_id,
                    duration=duration, tokens_in=result.usage.tokens_in, tokens_out=result.usage.tokens_out,
                )
                return result

            assert error is not None
            final = (not error.retryable) or attempt > retries
            runtime.event(
                "agent_error", stage=task.stage, agent=self.name, role=task.role, part_id=task.part_id,
                attempt=attempt, error=str(error), final=final,
            )
            if final:
                if error.pause and not isinstance(error, QuotaError):
                    raise QuotaError(f"{error} — essais épuisés, projet mis en pause.", agent=self.name) from error
                raise error
            delay = self.cfg.backoff * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
            logger.warning("%s : tentative %d échouée (%s). Nouvel essai dans %.1fs.", self.name, attempt, error, delay)
            time.sleep(delay)

    def _log(self, task, prompt, attempt, duration, result: AgentResult | None, error: AgentError | None) -> None:
        usage = result.usage if result else Usage()
        self.runtime.log_call({
            "ts": stamp(),
            "stage": task.stage,
            "part_id": task.part_id,
            "role": task.role,
            "agent": self.name,
            "type": self.cfg.type,
            "model": (result.model if result else "") or self.cfg.model,
            "attempt": attempt,
            "duration_s": duration,
            "tokens_in": usage.tokens_in,
            "tokens_out": usage.tokens_out,
            "tokens_estimated": usage.estimated,
            "cost_usd": usage.cost_usd,
            "ok": error is None,
            "error": str(error) if error else "",
            "error_kind": type(error).__name__ if error else "",
            "system": task.system,
            "prompt": prompt,
            "response": result.text if result else "",
            "files": [str(p) for p in result.files] if result else [],
        })


def ask_json(
    agent: Agent,
    task: AgentTask,
    model: type[M],
    *,
    repairs: int = 2,
    validator: Callable[[M], None] | None = None,
) -> M:
    """Demande une sortie JSON conforme à `model`. Invalide → renvoi avec l'erreur, `repairs` fois.

    `validator` ajoute des contrôles métier (ex. agents du plan disponibles) ; il lève ValueError.
    """
    task.schema = model
    base_prompt = task.prompt
    last_error = ""
    for attempt in range(repairs + 1):
        result = agent.run(task)
        try:
            data = result.data if result.data is not None else extract_json(result.text)
            obj = model.model_validate(data)
            if validator is not None:
                validator(obj)
            return obj
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)
            logger.warning("%s : sortie JSON invalide (%s)", agent.name, last_error[:300])
            agent.runtime.event(
                "json_invalid", stage=task.stage, agent=agent.name, part_id=task.part_id,
                attempt=attempt + 1, error=last_error[:500],
            )
            previous = (result.text or "")[:4000]
            task.prompt = (
                f"{base_prompt}\n\n"
                "Ta réponse précédente a été rejetée.\n"
                f"Erreur : {last_error[:1500]}\n"
                f"Réponse rejetée :\n{previous}\n\n"
                "Corrige et renvoie l'objet JSON complet."
            )
    task.prompt = base_prompt
    raise InvalidOutputError(
        f"{agent.name} : JSON toujours invalide après {repairs + 1} essais ({last_error[:300]})", agent=agent.name
    )


def classify_http(status: int, message: str, *, agent: str = "") -> AgentError:
    """Traduit un code HTTP en erreur du routeur (commun aux agents qui parlent HTTP)."""
    text = f"HTTP {status} : {message[:500]}"
    lowered = message.lower()
    if status in (402,) or "quota" in lowered or "billing" in lowered or "credit balance" in lowered:
        return QuotaError(text, agent=agent)
    if status in (408, 409, 425, 429) or status >= 500:
        return AgentError(text, retryable=True, agent=agent)
    return AgentError(text, retryable=False, agent=agent)
