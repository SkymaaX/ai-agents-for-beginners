"""Agent local via l'API développeur d'AnythingLLM (ex. Qwen, le chef).

API utilisée (préfixe /api/v1, en-tête `Authorization: Bearer <clé>`) :
  GET  /auth                          → {"authenticated": true}
  GET  /workspaces                    → {"workspaces": [{slug, name, ...}]}
  POST /workspace/{slug}/chat         → {type, textResponse, sources, error, ...}
  POST /workspace/{slug}/vector-search→ {"results": [{text, metadata, score}]}
  GET  /system                        → {"settings": {LLMProvider, ...}}
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from typing import Any

import httpx

from ..errors import AgentError, AgentTimeout, ConfigError
from ..logs import logger
from .base import Agent, AgentResult, AgentTask, Usage, classify_http

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class AnythingLLMClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 600.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url + "/api/v1",
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
        )

    def request(self, method: str, path: str, **kwargs: Any) -> dict:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise AgentTimeout(f"AnythingLLM n'a pas répondu à temps ({exc.__class__.__name__})") from exc
        except httpx.HTTPError as exc:
            raise AgentError(
                f"AnythingLLM injoignable à {self.base_url} ({exc.__class__.__name__}). Est-il lancé ?",
                retryable=True,
            ) from exc
        if response.status_code == 403:
            raise AgentError("AnythingLLM refuse la clé API (ANYTHINGLLM_API_KEY).", retryable=False)
        if response.status_code >= 400:
            raise classify_http(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise AgentError("Réponse AnythingLLM illisible (JSON attendu).", retryable=True) from exc

    def authenticated(self) -> bool:
        return bool(self.request("GET", "/auth").get("authenticated"))

    def workspaces(self) -> list[dict]:
        return self.request("GET", "/workspaces").get("workspaces", [])

    def system(self) -> dict:
        return self.request("GET", "/system").get("settings", {})

    def chat(self, slug: str, message: str, mode: str = "chat", session_id: str | None = None) -> dict:
        body = {"message": message, "mode": mode, "sessionId": session_id or f"router-{uuid.uuid4().hex}"}
        return self.request("POST", f"/workspace/{slug}/chat", json=body)

    def vector_search(self, slug: str, query: str, top_n: int, score_threshold: float) -> list[dict]:
        body = {"query": query, "topN": top_n, "scoreThreshold": score_threshold}
        return self.request("POST", f"/workspace/{slug}/vector-search", json=body).get("results", [])


def client_from_cfg(cfg, timeout: float | None = None, transport: httpx.BaseTransport | None = None) -> AnythingLLMClient:
    key_env = cfg.get("api_key_env", "ANYTHINGLLM_API_KEY")
    api_key = os.environ.get(key_env, "")
    if not api_key:
        raise ConfigError(f"Clé AnythingLLM absente : renseigne {key_env} dans .env")
    return AnythingLLMClient(cfg.get("base_url", "http://localhost:3001"), api_key, timeout or cfg.timeout, transport)


def strip_thinking(text: str) -> str:
    """Retire le raisonnement <think>…</think> que les modèles Qwen récents ajoutent."""
    return _THINK.sub("", text or "").strip()


class AnythingLLMAgent(Agent):
    type_name = "anythingllm"
    default_capabilities = frozenset({"text"})
    gpu_group = "llm"

    def __init__(self, name, cfg, runtime, transport: httpx.BaseTransport | None = None):
        super().__init__(name, cfg, runtime)
        self._transport = transport
        self._client: AnythingLLMClient | None = None

    @property
    def client(self) -> AnythingLLMClient:
        if self._client is None:
            self._client = client_from_cfg(self.cfg, transport=self._transport)
        return self._client

    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        slug = self.cfg.get("workspace", "chef")
        message = f"{task.system.strip()}\n\n{prompt}" if task.system else prompt
        # Un identifiant de session neuf par appel : aucun historique ne fuit d'un appel à l'autre.
        data = self.client.chat(slug, message, mode=self.cfg.get("mode", "chat"))
        if data.get("type") == "abort" or data.get("error"):
            raise AgentError(f"AnythingLLM a interrompu la réponse : {data.get('error') or 'abort'}", retryable=True)
        text = strip_thinking(data.get("textResponse") or "")
        if not text:
            raise AgentError("AnythingLLM a renvoyé une réponse vide.", retryable=True)
        metrics = data.get("metrics") or {}
        usage = Usage(
            tokens_in=int(metrics.get("prompt_tokens") or 0),
            tokens_out=int(metrics.get("completion_tokens") or 0),
            cost_usd=0.0,
        )
        return AgentResult(text=text, usage=usage, model=self.cfg.model or f"anythingllm:{slug}")

    # ------------------------------------------------------------------------------ VRAM
    def unload(self) -> None:
        """Décharge le modèle local de la VRAM, selon le moteur déclaré dans gpu.llm_backend."""
        gpu = self.runtime.config.gpu
        backend = gpu.llm_backend
        if backend == "ollama":
            with httpx.Client(base_url=gpu.ollama_url.rstrip("/"), timeout=30) as http:
                loaded = http.get("/api/ps").json().get("models", [])
                for model in loaded:
                    name = model.get("name") or model.get("model")
                    http.post("/api/generate", json={"model": name, "keep_alive": 0})
                    logger.info("VRAM : modèle Ollama déchargé (%s)", name)
        elif backend == "lmstudio":
            subprocess.run(["lms", "unload", "--all"], check=False, capture_output=True, timeout=60)
        elif backend == "command" and gpu.unload_command:
            subprocess.run(gpu.unload_command, check=False, capture_output=True, timeout=120)

    def diagnose(self) -> list[str]:
        lines: list[str] = []
        slug = self.cfg.get("workspace", "chef")
        try:
            if not self.client.authenticated():
                return ["✗ clé API refusée"]
            slugs = [w.get("slug") for w in self.client.workspaces()]
            lines.append(f"✓ connecté — workspaces : {', '.join(str(s) for s in slugs) or 'aucun'}")
            lines.append(f"{'✓' if slug in slugs else '✗'} workspace « {slug} » {'trouvé' if slug in slugs else 'INTROUVABLE'}")
            settings = self.client.system()
            provider = settings.get("LLMProvider")
            if provider:
                model = next((v for k, v in settings.items() if "ModelPref" in k and isinstance(v, str) and v), "")
                lines.append(f"  moteur LLM : {provider}{' — ' + model if model else ''}")
        except (AgentError, ConfigError) as exc:
            lines.append(f"✗ {exc}")
        return lines
