"""Claude via l'API Anthropic (SDK officiel `anthropic`).

Avantages : sortie JSON garantie par schéma, tokens exacts, contrôle fin (modèle, effort).
Limite : facturé au token, à part de l'abonnement Claude.

Authentification : ANTHROPIC_API_KEY dans .env (ou un profil `ant auth login`).
"""

from __future__ import annotations

import base64
import json
import mimetypes

from ..errors import AgentError, AgentTimeout, QuotaError
from ..jsonutil import portable_schema
from .base import Agent, AgentResult, AgentTask, Usage

# Tarifs indicatifs en $ par million de tokens (entrée, sortie), relevés en novembre 2026.
# Sert uniquement à estimer le coût dans les journaux ; surchargeable par `price_in` / `price_out`.
PRICES = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeApiAgent(Agent):
    type_name = "claude_api"
    default_capabilities = frozenset({"text", "vision"})
    native_json = True

    def __init__(self, name, cfg, runtime):
        super().__init__(name, cfg, runtime)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic

            # Les nouveaux essais sont gérés par le routeur (journalisés un par un) : pas de double retry.
            self._client = anthropic.Anthropic(timeout=self.cfg.timeout, max_retries=0)
        return self._client

    @property
    def model(self) -> str:
        return self.cfg.model or "claude-opus-5-5"

    def _content(self, task: AgentTask, prompt: str) -> list[dict]:
        blocks: list[dict] = []
        for path in task.attachments:
            media_type = mimetypes.guess_type(path.name)[0] or "image/png"
            data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        blocks.append({"type": "text", "text": prompt})
        return blocks

    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        import anthropic

        cfg = self.cfg
        output_config: dict = {"effort": cfg.get("effort", "high")}
        if task.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": portable_schema(task.schema)}
        kwargs: dict = {
            "model": self.model,
            "max_tokens": int(cfg.get("max_tokens", 64000)),
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            "messages": [{"role": "user", "content": self._content(task, prompt)}],
        }
        if task.system:
            kwargs["system"] = task.system
        try:
            # Flux continu : évite les coupures HTTP sur les réponses longues.
            if cfg.get("fallbacks", True):
                # Si le modèle décline une requête, l'API la rejoue sur un modèle de repli.
                stream_cm = self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                stream_cm = self.client.messages.stream(**kwargs)
            with stream_cm as stream:
                message = stream.get_final_message()
        except anthropic.APITimeoutError as exc:
            raise AgentTimeout(f"API Anthropic : délai dépassé ({cfg.timeout:.0f} s)") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError(f"API Anthropic injoignable : {exc}", retryable=True) from exc
        except anthropic.RateLimitError as exc:
            raise AgentError(f"API Anthropic : limite de débit ({exc.message})", retryable=True, pause=True) from exc
        except anthropic.AuthenticationError as exc:
            raise AgentError("API Anthropic : clé refusée (ANTHROPIC_API_KEY).", retryable=False) from exc
        except anthropic.NotFoundError as exc:
            raise AgentError(f"API Anthropic : modèle « {self.model} » introuvable ou non autorisé.", retryable=False) from exc
        except anthropic.APIStatusError as exc:
            text = f"API Anthropic HTTP {exc.status_code} : {exc.message}"
            if exc.status_code == 402 or "credit balance" in str(exc.message).lower():
                raise QuotaError(text) from exc
            raise AgentError(text, retryable=exc.status_code >= 500) from exc

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "non précisée"
            raise AgentError(f"Claude a décliné la requête (catégorie : {category}).", retryable=False)
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if message.stop_reason == "max_tokens":
            raise AgentError("Réponse de Claude tronquée : augmente `max_tokens` pour cet agent.", retryable=False)
        if not text:
            raise AgentError("Claude a renvoyé une réponse vide.", retryable=True)

        raw = message.usage
        tokens_in = (raw.input_tokens or 0) + (raw.cache_creation_input_tokens or 0) + (raw.cache_read_input_tokens or 0)
        tokens_out = raw.output_tokens or 0
        served = message.model or self.model
        price_in, price_out = PRICES.get(served, PRICES.get(self.model, (0.0, 0.0)))
        price_in = float(cfg.get("price_in", price_in))
        price_out = float(cfg.get("price_out", price_out))
        cost = round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6) if (price_in or price_out) else None

        data = None
        if task.schema is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None  # ask_json retentera l'extraction puis demandera une correction
        return AgentResult(text=text, data=data, usage=Usage(tokens_in, tokens_out, cost), model=served)

    def diagnose(self) -> list[str]:
        try:
            info = self.client.models.retrieve(self.model)
            return [f"✓ API Anthropic joignable — modèle {info.id}"]
        except Exception as exc:  # diagnostic : on rapporte, on ne lève pas
            return [f"✗ API Anthropic : {type(exc).__name__} — {str(exc)[:200]}"]
