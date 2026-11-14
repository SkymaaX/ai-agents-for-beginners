"""Génération d'images locale via ComfyUI (Flux, Stable Diffusion).

API HTTP de ComfyUI : POST /prompt → file d'attente ; GET /history/{id} → résultat ;
GET /view → fichier image ; POST /free → libère la VRAM ; GET /system_stats → état.

Le workflow est un fichier JSON au format API (dans ComfyUI : menu → « Export (API) »).
Jetons remplacés avant envoi : {{prompt}}, {{negative}}, {{checkpoint}} (dans des chaînes)
et "{{seed}}", "{{width}}", "{{height}}", "{{steps}}" (remplacés par des nombres).
Pour Flux : exporte ton propre workflow et pointe `workflow` dessus dans la config.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path

import httpx

from ..errors import AgentError, AgentTimeout, ConfigError
from .base import Agent, AgentResult, AgentTask

DEFAULT_WORKFLOW = Path(__file__).resolve().parent.parent / "workflows" / "sdxl_basic.json"


class ComfyUIAgent(Agent):
    type_name = "comfyui"
    default_capabilities = frozenset({"image"})
    gpu_group = "diffusion"

    def __init__(self, name, cfg, runtime, transport: httpx.BaseTransport | None = None):
        super().__init__(name, cfg, runtime)
        self.base_url = str(cfg.get("base_url", "http://127.0.0.1:8188")).rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(60.0, connect=10.0), transport=transport)

    def _workflow(self, prompt: str) -> dict:
        configured = self.cfg.get("workflow", "")
        path = self.runtime.config.resolve(configured) if configured else DEFAULT_WORKFLOW
        if not path.exists():
            raise ConfigError(f"Workflow ComfyUI introuvable : {path}")
        text = path.read_text(encoding="utf-8")
        numbers = {
            "seed": int(self.cfg.get("seed", 0)) or random.randint(1, 2**31 - 1),
            "width": int(self.cfg.get("width", 1344)),
            "height": int(self.cfg.get("height", 768)),
            "steps": int(self.cfg.get("steps", 28)),
        }
        for key, value in numbers.items():
            text = text.replace(f'"{{{{{key}}}}}"', str(value))
        strings = {
            "prompt": prompt,
            "negative": str(self.cfg.get("negative", "flou, texte, filigrane, basse qualité")),
            "checkpoint": str(self.cfg.get("checkpoint", "")),
        }
        for key, value in strings.items():
            text = text.replace(f"{{{{{key}}}}}", json.dumps(value, ensure_ascii=False)[1:-1])
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Workflow ComfyUI invalide après remplacement des jetons ({path.name}) : {exc}") from exc

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise AgentTimeout("ComfyUI n'a pas répondu à temps") from exc
        except httpx.HTTPError as exc:
            raise AgentError(f"ComfyUI injoignable à {self.base_url} ({exc.__class__.__name__}). Est-il lancé ?", retryable=True) from exc
        if response.status_code >= 400:
            raise AgentError(f"ComfyUI HTTP {response.status_code} : {response.text[:400]}", retryable=response.status_code >= 500)
        return response

    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        workflow = self._workflow(task.prompt)
        queued = self._request("POST", "/prompt", json={"prompt": workflow, "client_id": uuid.uuid4().hex}).json()
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise AgentError(f"ComfyUI a refusé le workflow : {str(queued)[:400]}", retryable=False)

        deadline = time.monotonic() + self.cfg.timeout
        entry: dict | None = None
        while entry is None:
            if time.monotonic() > deadline:
                raise AgentTimeout(f"ComfyUI : image non terminée en {self.cfg.timeout:.0f} s")
            time.sleep(float(self.cfg.get("poll_seconds", 2)))
            history = self._request("GET", f"/history/{prompt_id}").json()
            entry = history.get(prompt_id)
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            raise AgentError(f"ComfyUI : exécution en erreur ({str(status.get('messages'))[:400]})", retryable=False)

        workdir = task.workdir or Path(".")
        workdir.mkdir(parents=True, exist_ok=True)
        stem = Path(task.meta.get("filename") or "image").stem
        files: list[Path] = []
        for output in (entry.get("outputs") or {}).values():
            for image in output.get("images", []):
                params = {"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")}
                content = self._request("GET", "/view", params=params).content
                suffix = Path(image["filename"]).suffix or ".png"
                path = workdir / f"{stem}{'' if not files else f'_{len(files) + 1}'}{suffix}"
                path.write_bytes(content)
                files.append(path)
        if not files:
            raise AgentError("ComfyUI n'a produit aucune image (le workflow a-t-il un nœud SaveImage ?).", retryable=False)
        text = f"Image générée localement par ComfyUI : {', '.join(p.name for p in files)}.\n\nPrompt utilisé :\n{task.prompt}"
        return AgentResult(text=text, files=files, model=str(self.cfg.get("checkpoint") or "comfyui"))

    def unload(self) -> None:
        """Libère la VRAM occupée par ComfyUI avant de rendre la carte à Qwen."""
        self._request("POST", "/free", json={"unload_models": True, "free_memory": True})

    def diagnose(self) -> list[str]:
        try:
            stats = self._request("GET", "/system_stats").json()
        except AgentError as exc:
            return [f"✗ {exc}"]
        lines = [f"✓ ComfyUI joignable ({self.base_url})"]
        for device in stats.get("devices", []):
            total = device.get("vram_total", 0) / 2**30
            free = device.get("vram_free", 0) / 2**30
            lines.append(f"  {device.get('name', 'GPU')} : {total:.1f} Go VRAM, {free:.1f} Go libres")
        return lines
