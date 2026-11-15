"""Agent VoiceStudio testé contre un VRAI serveur MCP (protocole « streamable HTTP ») lancé en local.

Le faux serveur imite le contrat décrit dans la documentation de VoiceStudio : outils
`generate_speech` et `check_health`, réponse JSON avec `wav_base64` ou `audio_url`, clé en
`Authorization: Bearer`. Deux variantes de noms de paramètres vérifient que l'agent lit le schéma
du serveur au lieu de supposer des noms.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from router.agents.base import ROLE_PRODUCE, AgentTask
from router.agents.mock import write_wav
from router.agents.voicestudio import VoiceStudioAgent, build_arguments, find_audio, leaf_exceptions
from router.errors import AgentError, AgentTimeout, ConfigError
from router.runtime import Runtime

KNOBS: dict = {}


def reset_knobs(**changes):
    KNOBS.clear()
    KNOBS.update({"mode": "base64", "key": None, "calls": [], "downloads": [], "wav": b""}, **changes)


def make_app(variant: str, wav: bytes):
    server = MCPServer(f"voicestudio-{variant}")
    health = lambda: json.dumps({"status": "ok", "device": "cpu", "version": "0.5.6"})  # noqa: E731

    def respond(arguments: dict) -> str:
        KNOBS["calls"].append(arguments)
        mode = KNOBS["mode"]
        if mode == "error":
            return json.dumps({"error": "voix inconnue : profil absent"})
        if mode == "url":
            return json.dumps({"audio_url": "/audio/abc123.ogg", "output_path": ""})
        return json.dumps({"wav_base64": base64.b64encode(wav).decode("ascii")})

    if variant == "A":
        @server.tool()
        def generate_speech(text: str, profile_id: str = "", language: str = "", format: str = "wav") -> str:
            """Synthétise la parole."""
            return respond({"text": text, "profile_id": profile_id, "language": language, "format": format})
    else:
        @server.tool()
        def generate_speech(input: str, voice_id: str = "") -> str:  # noqa: A002
            """Synthétise la parole (variante aux noms différents)."""
            return respond({"input": input, "voice_id": voice_id})

    @server.tool()
    def check_health() -> str:
        """État du serveur."""
        return health()

    app = server.streamable_http_app(streamable_http_path="/mcp/")

    async def audio(request: Request) -> Response:
        KNOBS["downloads"].append(request.headers.get("authorization"))
        return Response(b"OggS-fake-audio", media_type="audio/ogg")

    app.add_route("/audio/{name}", audio)

    class Bearer(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            key = KNOBS.get("key")
            if key and request.headers.get("authorization") != f"Bearer {key}":
                return JSONResponse({"error": "clé refusée"}, status_code=401)
            return await call_next(request)

    app.add_middleware(Bearer)
    return app


def serve(app) -> tuple[uvicorn.Server, int]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "le faux serveur MCP n'a pas démarré"
    return server, port


@pytest.fixture(scope="module")
def wav_bytes(tmp_path_factory) -> bytes:
    return write_wav(tmp_path_factory.mktemp("wav") / "v.wav", seconds=0.5).read_bytes()


@pytest.fixture(scope="module")
def servers(wav_bytes):
    started = {variant: serve(make_app(variant, wav_bytes)) for variant in ("A", "B")}
    yield {variant: port for variant, (_, port) in started.items()}
    for server, _ in started.values():
        server.should_exit = True


@pytest.fixture(autouse=True)
def knobs(wav_bytes):
    reset_knobs(wav=wav_bytes)
    yield


@pytest.fixture
def rt(make_config):
    return Runtime(make_config(mock=False))


def agent_for(rt, port: int, **extra) -> VoiceStudioAgent:
    cfg = rt.config.agent_cfg("voix").model_copy(deep=True)
    cfg.enabled = True
    cfg.retries = 0
    cfg.timeout = 30
    cfg.__pydantic_extra__.update({"base_url": f"http://127.0.0.1:{port}", **extra})
    return VoiceStudioAgent("voix", cfg, rt)


def speak(agent: VoiceStudioAgent, tmp_path: Path, text: str = "Bonjour à tous.", filename: str = "narration.wav"):
    return agent.run(AgentTask(role=ROLE_PRODUCE, prompt=text, kind="audio", workdir=tmp_path, meta={"filename": filename}))


# ─────────────────────────────────────────────────────────────────────── appel nominal
def test_generates_wav_through_real_mcp(rt, servers, tmp_path, wav_bytes):
    result = speak(agent_for(rt, servers["A"]), tmp_path)
    assert [f.name for f in result.files] == ["narration.wav"]
    assert (tmp_path / "narration.wav").read_bytes() == wav_bytes and wav_bytes.startswith(b"RIFF")
    assert KNOBS["calls"] == [{"text": "Bonjour à tous.", "profile_id": "", "language": "", "format": "wav"}]
    assert result.usage.cost_usd == 0.0 and result.model == "voicestudio"


def test_reads_the_server_schema_instead_of_assuming_names(rt, servers, tmp_path):
    """Variante B : le texte s'appelle `input`, la voix `voice_id`. Aucun nom n'est codé en dur."""
    speak(agent_for(rt, servers["B"], voice="profil-42"), tmp_path)
    assert KNOBS["calls"] == [{"input": "Bonjour à tous.", "voice_id": "profil-42"}]


def test_voice_language_and_format_are_sent_only_when_supported(rt, servers, tmp_path):
    speak(agent_for(rt, servers["A"], voice="clone-1", language="fr", format="opus"), tmp_path, filename="x.opus")
    assert KNOBS["calls"][0] == {"text": "Bonjour à tous.", "profile_id": "clone-1", "language": "fr", "format": "opus"}


def test_voice_without_matching_parameter_is_a_clear_error():
    schema = {"properties": {"text": {"type": "string"}}, "required": ["text"]}
    with pytest.raises(AgentError, match="aucun paramètre de voix"):
        build_arguments(schema, "salut", voice="clone", language="", fmt="wav")


def test_text_parameter_found_by_requirement_when_name_is_unknown():
    schema = {"properties": {"phrase": {"type": "string"}}, "required": ["phrase"]}
    assert build_arguments(schema, "salut", voice="", language="", fmt="wav") == {"phrase": "salut"}
    with pytest.raises(AgentError, match="paramètre « texte »"):
        build_arguments({"properties": {"n": {"type": "integer"}}}, "salut", voice="", language="", fmt="wav")


# ───────────────────────────────────────────────────────────────── clé et mode fichier
def test_file_mode_downloads_with_the_same_key(rt, servers, tmp_path, monkeypatch):
    reset_knobs(mode="url", key="secret")
    monkeypatch.setenv("OMNIVOICE_API_KEY", "secret")
    result = speak(agent_for(rt, servers["A"]), tmp_path)
    assert result.files[0].name == "narration.ogg"  # l'extension suit le fichier servi
    assert result.files[0].read_bytes() == b"OggS-fake-audio"
    assert KNOBS["downloads"] == ["Bearer secret"]


def test_server_with_key_refuses_a_missing_key(rt, servers, tmp_path, monkeypatch):
    reset_knobs(key="secret")
    monkeypatch.delenv("OMNIVOICE_API_KEY", raising=False)
    with pytest.raises(AgentError, match="refuse la clé") as info:
        speak(agent_for(rt, servers["A"]), tmp_path)
    assert not info.value.retryable  # inutile de réessayer sans la bonne clé
    monkeypatch.setenv("OMNIVOICE_API_KEY", "secret")
    assert speak(agent_for(rt, servers["A"]), tmp_path).files


def test_key_is_never_written_to_the_log(rt, servers, tmp_path, monkeypatch):
    from router.state import Project

    reset_knobs(key="cle-ultra-secrete-123")
    monkeypatch.setenv("OMNIVOICE_API_KEY", "cle-ultra-secrete-123")
    project = Project.create(rt.config.projects_dir, "x")
    rt.project = project
    speak(agent_for(rt, servers["A"]), tmp_path)
    assert "cle-ultra-secrete-123" not in project.calls_path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────── échecs
def test_server_not_running_gives_actionable_message(rt, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    with pytest.raises(AgentError, match="injoignable.*tools start voicestudio") as info:
        speak(agent_for(rt, closed_port), tmp_path)
    assert info.value.retryable


def test_server_error_payload_is_not_retried(rt, servers, tmp_path):
    reset_knobs(mode="error")
    with pytest.raises(AgentError, match="profil absent") as info:
        speak(agent_for(rt, servers["A"]), tmp_path)
    assert not info.value.retryable


def test_missing_tool_lists_what_exists(rt, servers, tmp_path):
    with pytest.raises(AgentError, match=r"outil « synthese ».*check_health.*generate_speech"):
        speak(agent_for(rt, servers["A"], tool="synthese"), tmp_path)


def test_only_audio_tasks_are_accepted(rt, servers, tmp_path):
    with pytest.raises(AgentError, match="ne produit que de l'audio"):
        agent_for(rt, servers["A"]).run(AgentTask(role=ROLE_PRODUCE, prompt="x", kind="text"))


def test_invalid_format_is_a_config_error(rt, servers, tmp_path):
    with pytest.raises(AgentError, match="format « mp3 » invalide"):
        speak(agent_for(rt, servers["A"], format="mp3"), tmp_path)


def test_tiny_timeout_becomes_a_timeout_error(rt, servers, tmp_path, monkeypatch):
    agent = agent_for(rt, servers["A"])
    agent.cfg.timeout = 0.001
    with pytest.raises((AgentTimeout, AgentError)) as info:
        speak(agent, tmp_path)
    assert info.value.retryable


# ───────────────────────────────────────────────────────────────── lecture des réponses
def block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def result(*content, structured=None, is_error=False):
    return SimpleNamespace(content=list(content), structured_content=structured, is_error=is_error)


def test_find_audio_understands_every_documented_shape(wav_bytes):
    b64 = base64.b64encode(wav_bytes).decode()
    assert find_audio(result(block("text", text=json.dumps({"wav_base64": b64}))))[0] == wav_bytes
    assert find_audio(result(block("audio", data=b64, mime_type="audio/wav")))[0] == wav_bytes
    embedded = block("resource", resource=SimpleNamespace(blob=b64, mime_type="audio/wav", text=None))
    assert find_audio(result(embedded))[0] == wav_bytes
    assert find_audio(result(structured={"wav_base64": b64}))[0] == wav_bytes
    data, url, path, suffix = find_audio(result(block("text", text=json.dumps({"audio_url": "/audio/x.opus", "output_path": "/tmp/x.opus"}))))
    assert (data, url, path, suffix) == (None, "/audio/x.opus", "/tmp/x.opus", ".opus")
    assert find_audio(result(block("text", text="pas du json")))[0] is None  # texte libre : pas d'audio, pas d'exception


def test_find_audio_reports_errors():
    with pytest.raises(AgentError, match="refusé"):
        find_audio(result(block("text", text="modèle non chargé"), is_error=True))
    with pytest.raises(AgentError, match="voix absente"):
        find_audio(result(block("text", text=json.dumps({"error": "voix absente"}))))


def test_exception_groups_are_flattened():
    inner = ConnectionError("refus")
    group = BaseExceptionGroup("g", [ExceptionGroup("h", [inner]), ValueError("v")])
    assert leaf_exceptions(group) == [inner, group.exceptions[1]]


# ─────────────────────────────────────────────────────────────────────── diagnostic
def test_diagnose_reports_health_and_tools(rt, servers):
    lines = agent_for(rt, servers["A"]).diagnose()
    assert lines[0].startswith("✓ VoiceStudio joignable") and "2 outils MCP" in lines[0]
    assert any("generate_speech" in line and "présent" in line for line in lines)
    assert any("état : ok" in line and "cpu" in line for line in lines)


def test_diagnose_when_down(rt):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    assert agent_for(rt, closed).diagnose()[0].startswith("✗ VoiceStudio injoignable")
