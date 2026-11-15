"""Voix de synthèse locale via VoiceStudio (alternative gratuite à ElevenLabs).

VoiceStudio expose un serveur MCP en HTTP (« streamable HTTP ») sur http://127.0.0.1:3900/mcp/
avec, entre autres, les outils `generate_speech`, `clone_voice`, `list_voices`, `check_health`.
Le contrat exact des paramètres n'étant pas publié sous forme de schéma, l'agent LIT le schéma
que le serveur annonce (`tools/list`) pour trouver le paramètre « texte », « voix », « langue »
et « format » : il n'invente aucun nom de paramètre.

Réponse de `generate_speech` : JSON dans un bloc texte avec `wav_base64` (mode par défaut) ou
`audio_url` / `output_path` (mode « files »). Les blocs MCP « audio » et « resource » sont
aussi compris.

Authentification : la boucle locale n'exige rien, mais un conteneur Docker voit le routeur comme
une machine distante : la clé OMNIVOICE_API_KEY est alors envoyée en `Authorization: Bearer`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from ..errors import AgentError, AgentTimeout, ConfigError
from .base import Agent, AgentResult, AgentTask, Usage
from ..runhint import CMD

TEXT_PARAMS = ("text", "input", "prompt", "content", "script")
VOICE_PARAMS = ("profile_id", "voice_id", "voice", "speaker")
FORMATS = ("wav", "ogg", "opus")


def leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Aplatit les ExceptionGroup (le client MCP enveloppe ses erreurs dans des groupes de tâches)."""
    if isinstance(exc, BaseExceptionGroup):
        found: list[BaseException] = []
        for sub in exc.exceptions:
            found += leaf_exceptions(sub)
        return found
    return [exc]


def pick_param(properties: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in properties:
            return name
    return None


def build_arguments(schema: dict[str, Any], text: str, *, voice: str, language: str, fmt: str) -> dict[str, Any]:
    """Construit les arguments de generate_speech d'après le schéma annoncé par le serveur."""
    properties = (schema or {}).get("properties", {}) or {}
    text_param = pick_param(properties, TEXT_PARAMS)
    if text_param is None:
        required = [n for n in (schema or {}).get("required", []) if (properties.get(n) or {}).get("type") == "string"]
        text_param = required[0] if required else None
    if text_param is None:
        raise AgentError(
            f"VoiceStudio : impossible de repérer le paramètre « texte » de generate_speech (paramètres : {sorted(properties)}).",
            retryable=False,
        )
    arguments: dict[str, Any] = {text_param: text}
    if voice:
        voice_param = pick_param(properties, VOICE_PARAMS)
        if voice_param is None:
            raise AgentError(
                f"VoiceStudio : une voix est configurée (« {voice} ») mais generate_speech n'a aucun paramètre de voix "
                f"(paramètres : {sorted(properties)}).", retryable=False,
            )
        arguments[voice_param] = voice
    if language and "language" in properties:
        arguments["language"] = language
    if fmt and "format" in properties:
        arguments["format"] = fmt
    return arguments


def find_audio(result: Any) -> tuple[bytes | None, str | None, str | None, str]:
    """(octets, url, chemin, extension) trouvés dans un résultat d'outil MCP. Lève AgentError si erreur."""
    texts: list[str] = []
    blobs: list[tuple[bytes, str]] = []
    payloads: list[dict[str, Any]] = []
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        payloads.append(structured)
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "audio":
            blobs.append((base64.b64decode(block.data), getattr(block, "mime_type", "") or ""))
        elif kind == "resource":
            resource = block.resource
            data = getattr(resource, "blob", None)
            if data:
                blobs.append((base64.b64decode(data), getattr(resource, "mime_type", "") or ""))
            elif getattr(resource, "text", None):
                texts.append(resource.text)
        elif kind == "text":
            texts.append(block.text)
    for text in texts:
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(decoded, dict):
            payloads.append(decoded)

    if getattr(result, "is_error", False):
        detail = next((p.get("error") for p in payloads if p.get("error")), None) or (texts[0] if texts else "erreur sans détail")
        raise AgentError(f"VoiceStudio a refusé la synthèse : {str(detail)[:400]}", retryable=False)
    for payload in payloads:
        if payload.get("error"):
            raise AgentError(f"VoiceStudio : {str(payload['error'])[:400]}", retryable=False)

    audio_bytes: bytes | None = None
    suffix = ".wav"
    for payload in payloads:
        if payload.get("wav_base64"):
            audio_bytes = base64.b64decode(payload["wav_base64"])
            break
    if audio_bytes is None and blobs:
        audio_bytes = blobs[0][0]
        suffix = mimetypes.guess_extension(blobs[0][1] or "audio/wav") or ".wav"
    url = next((p["audio_url"] for p in payloads if p.get("audio_url")), None)
    path = next((p["output_path"] for p in payloads if p.get("output_path")), None)
    if url and "." in str(url).rsplit("/", 1)[-1]:
        suffix = "." + str(url).rsplit(".", 1)[-1]
    return audio_bytes, url, path, suffix


class VoiceStudioAgent(Agent):
    type_name = "voicestudio"
    default_capabilities = frozenset({"audio"})

    def __init__(self, name, cfg, runtime):
        super().__init__(name, cfg, runtime)
        # Facultatif : si VoiceStudio tourne en GPU et partage la carte avec Qwen / ComfyUI,
        # `gpu_group: tts` le met dans la file d'attente VRAM.
        self.gpu_group = str(cfg.get("gpu_group", "") or "")

    # ------------------------------------------------------------------------- connexion
    @property
    def endpoint(self) -> str:
        base = str(self.cfg.get("base_url", "http://127.0.0.1:3900")).rstrip("/")
        return base + "/" + str(self.cfg.get("mcp_path", "/mcp/")).lstrip("/")

    def _headers(self) -> dict[str, str]:
        key = os.environ.get(str(self.cfg.get("api_key_env", "OMNIVOICE_API_KEY")), "")
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _session(self, work):
        """Ouvre une session MCP (clé envoyée si elle existe) et exécute `work(session)`."""
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(headers=self._headers(), timeout=float(self.cfg.timeout)) as http:
            async with streamable_http_client(self.endpoint, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return await work(session)

    def _translate(self, exc: BaseException) -> AgentError:
        for leaf in leaf_exceptions(exc):
            name = type(leaf).__name__
            text = str(leaf)
            if isinstance(leaf, AgentError):
                return leaf
            if isinstance(leaf, (asyncio.TimeoutError, TimeoutError)) or "Timeout" in name:
                return AgentTimeout(f"VoiceStudio n'a pas répondu à temps ({name})")
            if "ConnectError" in name or isinstance(leaf, ConnectionError):
                return AgentError(
                    f"VoiceStudio injoignable à {self.endpoint}. Est-il démarré ? "
                    f"{CMD} tools start voicestudio", retryable=True,
                )
            status = getattr(getattr(leaf, "response", None), "status_code", None) or getattr(leaf, "status_code", None)
            if status is None and name == "MCPError":
                # Le client MCP masque le code HTTP : on le relit pour distinguer clé refusée et panne.
                status = self._probe_status()
            if status in (401, 403):
                return AgentError(
                    "VoiceStudio refuse la clé : renseigne OMNIVOICE_API_KEY dans .env "
                    "(la même que celle du conteneur).", retryable=False,
                )
            if status is not None and status >= 500:
                return AgentError(f"VoiceStudio en erreur interne (HTTP {status}).", retryable=True)
        leaf = leaf_exceptions(exc)[0]
        return AgentError(f"VoiceStudio : {type(leaf).__name__} : {str(leaf)[:300]}", retryable=True)

    def _probe_status(self) -> int | None:
        """Code HTTP renvoyé par le serveur à une requête simple (None s'il ne répond pas)."""
        import httpx2

        async def probe() -> int:
            headers = {**self._headers(), "Accept": "application/json, text/event-stream"}
            async with httpx2.AsyncClient(headers=headers, timeout=5.0) as http:
                reply = await http.post(self.endpoint, json={"jsonrpc": "2.0", "id": 0, "method": "ping"})
            return reply.status_code

        try:
            return asyncio.run(probe())
        except Exception:  # noqa: BLE001 — sonde de diagnostic : jamais bloquante
            return None

    # ----------------------------------------------------------------------------- appel
    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        if task.kind != "audio":
            raise AgentError(f"« {self.name} » ne produit que de l'audio (tâche reçue : {task.kind}).", retryable=False)
        try:
            audio, usage_chars = asyncio.run(asyncio.wait_for(self._generate(task, prompt), float(self.cfg.timeout)))
        except AgentError:
            raise
        except BaseException as exc:  # noqa: BLE001 — traduit pour que les retries du routeur décident
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise self._translate(exc) from exc
        path, size = audio
        text = f"Narration générée localement par VoiceStudio : {path.name} ({size} octets)."
        return AgentResult(
            text=text, files=[path], usage=Usage(tokens_in=usage_chars // 4, tokens_out=0, cost_usd=0.0),
            model="voicestudio",
        )

    async def _generate(self, task: AgentTask, text: str) -> tuple[tuple[Path, int], int]:
        tool = str(self.cfg.get("tool", "generate_speech"))
        fmt = str(self.cfg.get("format", "wav")).lower()
        if fmt not in FORMATS:
            raise ConfigError(f"Agent « {self.name} » : format « {fmt} » invalide (wav, ogg ou opus).")

        async def work(session):
            listed = await session.list_tools()
            by_name = {t.name: t for t in listed.tools}
            if tool not in by_name:
                raise AgentError(
                    f"VoiceStudio n'a pas d'outil « {tool} » (outils : {sorted(by_name)}). Serveur MCP désactivé ou trop ancien ?",
                    retryable=False,
                )
            arguments = build_arguments(
                by_name[tool].input_schema or {}, text, voice=str(self.cfg.get("voice", "") or ""),
                language=str(self.cfg.get("language", "") or ""), fmt=fmt,
            )
            return await session.call_tool(tool, arguments)

        result = await self._session(work)
        audio_bytes, url, source_path, suffix = find_audio(result)

        if audio_bytes is None and url:
            audio_bytes = await self._download(url)
        if audio_bytes is None and source_path and Path(source_path).is_file():
            audio_bytes = Path(source_path).read_bytes()
        if not audio_bytes:
            raise AgentError("VoiceStudio n'a renvoyé aucun audio (ni données, ni lien, ni fichier lisible).", retryable=True)

        workdir = task.workdir or Path(".")
        workdir.mkdir(parents=True, exist_ok=True)
        stem = Path(task.meta.get("filename") or "narration").stem
        target = workdir / f"{stem}{suffix}"
        target.write_bytes(audio_bytes)
        return (target, len(audio_bytes)), len(text)

    async def _download(self, url: str) -> bytes:
        import httpx2

        absolute = url if url.startswith("http") else str(self.cfg.get("base_url", "http://127.0.0.1:3900")).rstrip("/") + "/" + url.lstrip("/")
        async with httpx2.AsyncClient(headers=self._headers(), timeout=float(self.cfg.timeout)) as http:
            response = await http.get(absolute)
        if response.status_code >= 400:
            raise AgentError(f"VoiceStudio : téléchargement de l'audio impossible (HTTP {response.status_code}).", retryable=True)
        return response.content

    # ---------------------------------------------------------------------- diagnostic
    def diagnose(self) -> list[str]:
        async def work(session):
            listed = await session.list_tools()
            names = sorted(t.name for t in listed.tools)
            health = await session.call_tool("check_health", {}) if "check_health" in names else None
            return names, health

        try:
            names, health = asyncio.run(asyncio.wait_for(self._session(work), 20))
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return [f"✗ {self._translate(exc)}"]
        lines = [f"✓ VoiceStudio joignable ({self.endpoint}) — {len(names)} outils MCP"]
        wanted = str(self.cfg.get("tool", "generate_speech"))
        lines.append(f"{'✓' if wanted in names else '✗'} outil « {wanted} » {'présent' if wanted in names else 'ABSENT'}")
        if health is not None:
            try:
                text = next((b.text for b in health.content if getattr(b, "type", "") == "text"), "")
                info = json.loads(text) if text else {}
                lines.append(f"  état : {info.get('status', '?')} — appareil : {info.get('device', '?')}")
            except (ValueError, TypeError):
                pass
        return lines
