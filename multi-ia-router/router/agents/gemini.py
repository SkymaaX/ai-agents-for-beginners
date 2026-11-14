"""Gemini via l'API Google (SDK officiel `google-genai`).

Rôles : répartiteur (plan JSON strict), producteur (texte, images, vidéo Veo), critique,
chercheur (recherche web ancrée sur Google Search : `meta["grounding"]`).

Les noms de modèles changent souvent : ils sont dans la config (`model`, `image_model`,
`video_model`) et `python -m router doctor` liste ceux que ta clé peut réellement utiliser.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
from pathlib import Path

from ..errors import AgentError, AgentTimeout, ConfigError, QuotaError
from ..jsonutil import portable_schema
from .base import Agent, AgentResult, AgentTask, Usage


def add_citations(text: str, supports: list, sources_count: int) -> str:
    """Insère les renvois [n] là où Gemini indique qu'un passage s'appuie sur la source n.

    Les positions fournies par l'API sont en octets UTF-8 : on insère sur les octets.
    """
    raw = text.encode("utf-8")
    inserts: list[tuple[int, str]] = []
    for support in supports or []:
        segment = getattr(support, "segment", None)
        indices = [i for i in (getattr(support, "grounding_chunk_indices", None) or []) if 0 <= i < sources_count]
        end = getattr(segment, "end_index", None) if segment is not None else None
        if not indices or end is None or not 0 < end <= len(raw):
            continue
        inserts.append((end, " [" + ", ".join(str(i + 1) for i in sorted(set(indices))) + "]"))
    for end, marker in sorted(inserts, key=lambda item: item[0], reverse=True):
        raw = raw[:end] + marker.encode("utf-8") + raw[end:]
    return raw.decode("utf-8", errors="replace")


class GeminiAgent(Agent):
    type_name = "gemini"
    default_capabilities = frozenset({"text", "vision", "web_search", "image", "video"})
    native_json = True

    def __init__(self, name, cfg, runtime):
        super().__init__(name, cfg, runtime)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            key_env = self.cfg.get("api_key_env", "GEMINI_API_KEY")
            api_key = os.environ.get(key_env, "")
            if not api_key:
                raise ConfigError(f"Clé Gemini absente : renseigne {key_env} dans .env")
            self._client = genai.Client(
                api_key=api_key, http_options=types.HttpOptions(timeout=int(self.cfg.timeout * 1000)),
            )
        return self._client

    # ---------------------------------------------------------------------------- appel
    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        from google.genai import errors

        try:
            if task.kind == "image":
                return self._image(task)
            if task.kind == "video":
                return self._video(task)
            return self._text(task, prompt)
        except errors.APIError as exc:
            raise self._translate(exc) from exc
        except (TimeoutError, OSError) as exc:
            name = type(exc).__name__
            if "Timeout" in name:
                raise AgentTimeout(f"API Gemini : délai dépassé ({name})") from exc
            raise AgentError(f"API Gemini injoignable ({name} : {exc})", retryable=True) from exc

    @staticmethod
    def _translate(exc) -> AgentError:
        code = int(getattr(exc, "code", 0) or 0)
        message = str(getattr(exc, "message", "") or exc)
        text = f"API Gemini HTTP {code} : {message[:500]}"
        if code == 429:
            # Limite par minute ou quota du jour : on réessaie, puis pause si ça persiste.
            return AgentError(text, retryable=True, pause=True)
        if code in (401, 403):
            return AgentError(text + " — vérifie GEMINI_API_KEY et l'accès au modèle.", retryable=False)
        if code == 402:
            return QuotaError(text)
        return AgentError(text, retryable=code in (408, 409) or code >= 500)

    def _text(self, task: AgentTask, prompt: str) -> AgentResult:
        from google.genai import types

        grounding = bool(task.meta.get("grounding"))
        config = types.GenerateContentConfig(system_instruction=task.system or None)
        if grounding:
            config.tools = [types.Tool(google_search=types.GoogleSearch())]
        elif task.schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = portable_schema(task.schema, strict=False)
        if self.cfg.get("max_output_tokens"):
            config.max_output_tokens = int(self.cfg.get("max_output_tokens"))
        contents: list = [prompt]
        for path in task.attachments:
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            contents.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=mime))

        model = self.cfg.model
        response = self.client.models.generate_content(model=model, contents=contents, config=config)
        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason:
            raise AgentError(f"Gemini a bloqué la requête ({feedback.block_reason}).", retryable=False)
        text = (response.text or "").strip()
        candidate = response.candidates[0] if response.candidates else None
        if not text:
            reason = getattr(candidate, "finish_reason", None)
            raise AgentError(f"Gemini a renvoyé une réponse vide (fin : {reason}).", retryable=True)

        extra: dict = {}
        if grounding and candidate is not None and candidate.grounding_metadata is not None:
            metadata = candidate.grounding_metadata
            chunks = metadata.grounding_chunks or []
            sources = [
                {"title": (chunk.web.title or chunk.web.domain or chunk.web.uri or ""), "url": chunk.web.uri or ""}
                for chunk in chunks if chunk.web is not None
            ]
            if len(sources) == len(chunks):  # indices alignés : on peut placer les renvois
                text = add_citations(text, metadata.grounding_supports or [], len(sources))
            extra["sources"] = sources
            extra["queries"] = list(metadata.web_search_queries or [])

        data = None
        if task.schema is not None and not grounding:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        return AgentResult(text=text, data=data, usage=self._usage(response), model=response.model_version or model, extra=extra)

    @staticmethod
    def _usage(response) -> Usage:
        meta = response.usage_metadata
        if meta is None:
            return Usage()
        return Usage(
            tokens_in=int(meta.prompt_token_count or 0),
            tokens_out=int(meta.candidates_token_count or 0) + int(meta.thoughts_token_count or 0),
        )

    # ---------------------------------------------------------------------------- image
    def _image(self, task: AgentTask) -> AgentResult:
        from google.genai import types

        model = self.cfg.get("image_model", "")
        if not model:
            raise ConfigError(f"Agent « {self.name} » : `image_model` non renseigné dans la config.")
        workdir = task.workdir or Path(".")
        workdir.mkdir(parents=True, exist_ok=True)
        stem = Path(task.meta.get("filename") or "image").stem
        aspect = self.cfg.get("image_aspect_ratio", "16:9")
        files: list[Path] = []
        note = ""
        usage = Usage()
        if model.startswith("imagen"):
            response = self.client.models.generate_images(
                model=model, prompt=task.prompt,
                config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio=aspect),
            )
            for index, generated in enumerate(response.generated_images or []):
                if generated.image is None or not generated.image.image_bytes:
                    continue
                suffix = mimetypes.guess_extension(generated.image.mime_type or "image/png") or ".png"
                path = workdir / f"{stem}{'' if index == 0 else f'_{index + 1}'}{suffix}"
                path.write_bytes(generated.image.image_bytes)
                files.append(path)
        else:
            response = self.client.models.generate_content(
                model=model, contents=task.prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"], image_config=types.ImageConfig(aspect_ratio=aspect),
                ),
            )
            usage = self._usage(response)
            for part in response.parts or []:
                if part.text:
                    note += part.text
                blob = part.inline_data
                if blob is not None and blob.data:
                    suffix = mimetypes.guess_extension(blob.mime_type or "image/png") or ".png"
                    path = workdir / f"{stem}{'' if not files else f'_{len(files) + 1}'}{suffix}"
                    path.write_bytes(blob.data)
                    files.append(path)
        if not files:
            raise AgentError("Gemini n'a renvoyé aucune image (filtre de sécurité ou modèle inadapté).", retryable=False)
        text = f"Image générée par {model} : {', '.join(p.name for p in files)}.\n\nPrompt utilisé :\n{task.prompt}"
        if note.strip():
            text += f"\n\nCommentaire du modèle : {note.strip()}"
        return AgentResult(text=text, files=files, usage=usage, model=model)

    # ---------------------------------------------------------------------------- vidéo
    def _video(self, task: AgentTask) -> AgentResult:
        from google.genai import types

        model = self.cfg.get("video_model", "")
        if not model:
            raise ConfigError(f"Agent « {self.name} » : `video_model` non renseigné dans la config.")
        workdir = task.workdir or Path(".")
        workdir.mkdir(parents=True, exist_ok=True)
        stem = Path(task.meta.get("filename") or "video").stem
        operation = self.client.models.generate_videos(
            model=model, prompt=task.prompt,
            config=types.GenerateVideosConfig(number_of_videos=1, aspect_ratio=self.cfg.get("video_aspect_ratio", "16:9")),
        )
        deadline = time.monotonic() + float(self.cfg.get("video_timeout", 1200))
        while not operation.done:
            if time.monotonic() > deadline:
                raise AgentTimeout("Veo : génération vidéo trop longue, abandon.")
            time.sleep(float(self.cfg.get("video_poll_seconds", 10)))
            operation = self.client.operations.get(operation)
        if operation.error:
            raise AgentError(f"Veo a échoué : {operation.error}", retryable=False)
        videos = (operation.response.generated_videos if operation.response else None) or []
        files: list[Path] = []
        for index, generated in enumerate(videos):
            if generated.video is None:
                continue
            path = workdir / f"{stem}{'' if index == 0 else f'_{index + 1}'}.mp4"
            self.client.files.download(file=generated.video)
            generated.video.save(str(path))
            files.append(path)
        if not files:
            raise AgentError("Veo n'a renvoyé aucune vidéo (filtre de sécurité ?).", retryable=False)
        text = f"Vidéo générée par {model} : {', '.join(p.name for p in files)}.\n\nPrompt utilisé :\n{task.prompt}"
        return AgentResult(text=text, files=files, model=model)

    # ----------------------------------------------------------------------- diagnostic
    def diagnose(self) -> list[str]:
        try:
            names = sorted({(m.name or "").removeprefix("models/") for m in self.client.models.list()})
        except Exception as exc:  # diagnostic : on rapporte, on ne lève pas
            return [f"✗ API Gemini : {type(exc).__name__} — {str(exc)[:200]}"]
        lines = [f"✓ API Gemini joignable — {len(names)} modèles accessibles"]
        for label, key in (("texte", "model"), ("image", "image_model"), ("vidéo", "video_model")):
            wanted = self.cfg.model if key == "model" else self.cfg.get(key, "")
            if not wanted:
                continue
            ok = wanted in names
            lines.append(f"{'✓' if ok else '✗'} modèle {label} « {wanted} » {'disponible' if ok else 'ABSENT de la liste'}")
        for family in ("gemini", "imagen", "veo"):
            found = [n for n in names if n.startswith(family) and "embedding" not in n and "tts" not in n]
            if found:
                lines.append(f"  {family} : {', '.join(found[-8:])}")
        return lines
