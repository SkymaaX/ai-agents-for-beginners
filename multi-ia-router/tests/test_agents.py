"""Module 3 — agents réels, testés contre de faux serveurs / faux binaires (aucun appel réseau)."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from router.agents.base import ROLE_CRITIC, ROLE_PRODUCE, AgentTask, ask_json
from router.agents.claude_api import ClaudeApiAgent
from router.agents.claude_cli import ClaudeCliAgent
from router.agents.comfyui import ComfyUIAgent
from router.agents.gemini import GeminiAgent, add_citations
from router.agents.qwen_anythingllm import AnythingLLMAgent, strip_thinking
from router.errors import AgentError, ConfigError, QuotaError
from router.runtime import Runtime
from router.schemas import Brief, Critique
from router.tools.gpu_lock import GpuArbiter

FAKES = Path(__file__).resolve().parent / "fakes"
GOOD_SCORES = {"fidelite": 5, "completude": 5, "erreurs": 5, "qualite": 5, "sources": 5}


@pytest.fixture
def rt(make_config, monkeypatch):
    """Runtime NON simulé : on teste les vraies classes d'agents."""
    monkeypatch.setenv("ANYTHINGLLM_API_KEY", "cle-test")
    monkeypatch.setenv("GEMINI_API_KEY", "cle-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-test")
    return Runtime(make_config(mock=False))


def retries(rt, name: str, count: int):
    cfg = rt.config.agent_cfg(name)
    cfg.retries = count
    return cfg


# ───────────────────────────────────────────────────────────────── AnythingLLM (Qwen)
def anythingllm(rt, handler) -> AnythingLLMAgent:
    return AnythingLLMAgent("qwen", rt.config.agent_cfg("qwen"), rt, transport=httpx.MockTransport(handler))


def test_anythingllm_chat_request_and_response(rt):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "type": "textResponse", "textResponse": "<think>je réfléchis</think>\nBonjour", "error": None,
            "metrics": {"prompt_tokens": 12, "completion_tokens": 3},
        })

    result = anythingllm(rt, handler).run(AgentTask(role="chef.reformulation", system="Tu es chef.", prompt="Dis bonjour"))
    assert result.text == "Bonjour"  # le raisonnement <think> est retiré
    assert (result.usage.tokens_in, result.usage.tokens_out, result.usage.estimated) == (12, 3, False)
    request = seen[0]
    assert request.url.path == "/api/v1/workspace/chef/chat"
    assert request.headers["authorization"] == "Bearer cle-test"
    body = json.loads(request.content)
    assert body["mode"] == "chat" and body["message"].startswith("Tu es chef.") and "Dis bonjour" in body["message"]
    assert body["sessionId"].startswith("router-")


def test_anythingllm_fresh_session_each_call(rt):
    sessions = []

    def handler(request):
        sessions.append(json.loads(request.content)["sessionId"])
        return httpx.Response(200, json={"type": "textResponse", "textResponse": "ok"})

    agent = anythingllm(rt, handler)
    agent.run(AgentTask(role="r", prompt="a"))
    agent.run(AgentTask(role="r", prompt="b"))
    assert len(set(sessions)) == 2  # aucun historique partagé entre deux appels


def test_anythingllm_abort_is_retried_then_succeeds(rt):
    retries(rt, "qwen", 2)
    answers = iter([
        httpx.Response(200, json={"type": "abort", "textResponse": None, "error": "modèle non chargé"}),
        httpx.Response(200, json={"type": "textResponse", "textResponse": "ok"}),
    ])
    result = anythingllm(rt, lambda request: next(answers)).run(AgentTask(role="r", prompt="p"))
    assert result.text == "ok" and result.usage.estimated  # pas de métriques → estimation signalée


def test_anythingllm_bad_key_is_not_retried(rt):
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(403, json={"message": "Invalid API Key"})

    with pytest.raises(AgentError, match="clé API") as info:
        anythingllm(rt, handler).run(AgentTask(role="r", prompt="p"))
    assert not info.value.retryable and len(hits) == 1


def test_anythingllm_unreachable_gives_clear_message(rt):
    retries(rt, "qwen", 0)

    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(AgentError, match="injoignable.*Est-il lancé"):
        anythingllm(rt, handler).run(AgentTask(role="r", prompt="p"))


def test_anythingllm_json_goes_through_prompt_and_repair(rt):
    """Qwen n'a pas de sortie structurée : schéma dans le prompt, puis correction si invalide."""
    prompts = []
    brief = {"title": "T", "objective": "O"}
    answers = iter(["Voici le brief : {titre manquant", "```json\n" + json.dumps(brief) + "\n```"])

    def handler(request):
        prompts.append(json.loads(request.content)["message"])
        return httpx.Response(200, json={"type": "textResponse", "textResponse": next(answers)})

    result = ask_json(anythingllm(rt, handler), AgentTask(role="chef.reformulation", prompt="Reformule"), Brief)
    assert result.title == "T"
    assert '"acceptance_criteria"' in prompts[0] and "UNIQUEMENT" in prompts[0]
    assert "rejetée" in prompts[1]


def test_missing_key_is_a_config_error(rt, monkeypatch):
    monkeypatch.delenv("ANYTHINGLLM_API_KEY")
    slept = []
    monkeypatch.setattr("router.agents.base.time.sleep", slept.append)
    with pytest.raises(AgentError, match="ANYTHINGLLM_API_KEY") as info:
        AnythingLLMAgent("qwen", retries(rt, "qwen", 3), rt).run(AgentTask(role="r", prompt="p"))
    assert not info.value.retryable and not slept  # config incomplète : aucun nouvel essai


def test_strip_thinking():
    assert strip_thinking("<think>a\nb</think>réponse") == "réponse"
    assert strip_thinking("sans balise") == "sans balise"


# ────────────────────────────────────────────────────────────── Claude Code headless
def claude_cli(rt, name="claude", **extra) -> ClaudeCliAgent:
    cfg = rt.config.agent_cfg(name)
    cfg.__pydantic_extra__["command"] = [sys.executable, str(FAKES / "fake_claude.py")]
    cfg.__pydantic_extra__.update(extra)
    return ClaudeCliAgent(name, cfg, rt)


def test_claude_cli_text_mode_result_and_usage(rt):
    result = claude_cli(rt).run(AgentTask(role=ROLE_CRITIC, system="Tu es relecteur.", prompt="Relis ceci"))
    assert result.text.startswith("réponse à : Relis ceci")
    assert result.usage.tokens_in == 115 and result.usage.tokens_out == 7 and result.usage.cost_usd == 0.0123
    assert result.model == "claude-test-model"


def test_claude_cli_arguments(rt, monkeypatch):
    captured = {}
    import subprocess as real_subprocess

    original = real_subprocess.run

    def spy(command, **kwargs):
        captured["command"], captured["kwargs"] = command, kwargs
        return original(command, **kwargs)

    monkeypatch.setattr("router.agents.claude_cli.subprocess.run", spy)
    claude_cli(rt, effort="high").run(AgentTask(role=ROLE_CRITIC, system="Sys", prompt="Un très long prompt"))
    command = captured["command"]
    assert command[2:5] == ["-p", "--output-format", "json"]
    assert command[command.index("--tools") + 1] == ""  # aucun outil pour un relecteur
    assert command[command.index("--setting-sources") + 1] == "project"  # pas de réglages perso (style, hooks)
    assert "--system-prompt-file" in command and "--append-system-prompt-file" not in command
    assert "--permission-mode" not in command and "--allowedTools" not in command
    assert "Un très long prompt" not in " ".join(command)  # le prompt passe par l'entrée standard
    assert captured["kwargs"]["input"] == "Un très long prompt"


def test_claude_cli_structured_output(rt):
    agent = claude_cli(rt)
    assert agent.native_json
    result = agent.run(AgentTask(role=ROLE_CRITIC, prompt="Évalue", schema=Critique))
    assert set(result.data) == {"scores", "verdict", "summary", "problems"}


def test_claude_cli_cmd_launcher_puts_schema_in_prompt(rt):
    cfg = rt.config.agent_cfg("claude")
    cfg.__pydantic_extra__["command"] = ["C:/Users/x/AppData/Roaming/npm/claude.cmd"]
    agent = ClaudeCliAgent("claude", cfg, rt)
    assert not agent.native_json
    prompt, _ = agent.build_prompt(AgentTask(role=ROLE_CRITIC, prompt="Évalue", schema=Critique))
    assert '"verdict"' in prompt


def test_claude_cli_executor_writes_in_part_folder(rt, tmp_path):
    workdir = tmp_path / "work"
    result = claude_cli(rt, "claude_code").run(AgentTask(
        role=ROLE_PRODUCE, system="Tu produis.", prompt="ECRIS le fichier", kind="files", workdir=workdir,
    ))
    assert (workdir / "sortie.txt").read_text(encoding="utf-8").startswith("fichier écrit")
    assert result.extra["permission_denials"] == 1


def test_claude_cli_executor_arguments(rt, tmp_path, monkeypatch):
    captured = {}
    import subprocess as real_subprocess

    original = real_subprocess.run

    def spy(command, **kwargs):
        captured["command"], captured["cwd"] = command, kwargs["cwd"]
        return original(command, **kwargs)

    monkeypatch.setattr("router.agents.claude_cli.subprocess.run", spy)
    workdir = tmp_path / "work"
    claude_cli(rt, "claude_code").run(AgentTask(role=ROLE_PRODUCE, system="S", prompt="ECRIS", kind="files", workdir=workdir))
    command = captured["command"]
    assert captured["cwd"] == str(workdir)
    tools = command[command.index("--tools") + 1].split(",")
    assert "Write" in tools and "Bash" not in tools  # pas de commandes système par défaut
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "--append-system-prompt-file" in command


def test_claude_cli_usage_limit_pauses(rt):
    with pytest.raises(QuotaError, match="limite d'usage"):
        claude_cli(rt).run(AgentTask(role=ROLE_CRITIC, prompt="LIMITE"))


def test_claude_cli_overload_is_retryable_and_login_is_not(rt):
    retries(rt, "claude", 0)
    with pytest.raises(AgentError) as overloaded:
        claude_cli(rt).run(AgentTask(role=ROLE_CRITIC, prompt="PANNE"))
    assert overloaded.value.retryable and not isinstance(overloaded.value, QuotaError)
    with pytest.raises(AgentError, match="te connecter") as login:
        claude_cli(rt).run(AgentTask(role=ROLE_CRITIC, prompt="LOGIN"))
    assert not login.value.retryable
    with pytest.raises(AgentError, match="code 3"):
        claude_cli(rt).run(AgentTask(role=ROLE_CRITIC, prompt="CRASH"))


def test_claude_cli_images_are_passed_as_readable_paths(rt, tmp_path, monkeypatch):
    image = tmp_path / "img" / "rendu.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG")
    captured = {}
    import subprocess as real_subprocess

    original = real_subprocess.run

    def spy(command, **kwargs):
        captured["command"], captured["input"] = command, kwargs["input"]
        return original(command, **kwargs)

    monkeypatch.setattr("router.agents.claude_cli.subprocess.run", spy)
    claude_cli(rt).run(AgentTask(role=ROLE_CRITIC, prompt="Juge l'image", attachments=[image]))
    command = captured["command"]
    assert command[command.index("--tools") + 1] == "Read"
    assert command[command.index("--add-dir") + 1] == str(image.parent.resolve())
    assert str(image.resolve()) in captured["input"]


def test_claude_cli_missing_binary(rt):
    cfg = rt.config.agent_cfg("claude")
    cfg.__pydantic_extra__["command"] = "claude-introuvable-xyz"
    cfg.retries = 0
    with pytest.raises(AgentError, match="introuvable"):
        ClaudeCliAgent("claude", cfg, rt).run(AgentTask(role=ROLE_CRITIC, prompt="p"))


# ──────────────────────────────────────────────────────────────────── Claude API
class FakeAnthropic:
    """Imite client.beta.messages.stream / client.messages.stream du SDK."""

    def __init__(self, message=None, error=None):
        self.calls: list[tuple[str, dict]] = []
        self._message, self._error = message, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: self._stream("beta", kw)))
        self.messages = SimpleNamespace(stream=lambda **kw: self._stream("stable", kw))

    @contextmanager
    def _stream(self, surface, kwargs):
        self.calls.append((surface, kwargs))
        if self._error:
            raise self._error
        yield SimpleNamespace(get_final_message=lambda: self._message)


def claude_message(text, stop_reason="end_turn", model="claude-opus-5-5", **extra):
    usage = SimpleNamespace(input_tokens=1000, cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=500)
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop_reason, usage=usage, model=model, **extra)


def claude_api(rt, fake, **extra) -> ClaudeApiAgent:
    cfg = rt.config.agent_cfg("claude").model_copy(update={"type": "claude_api", "model": "claude-opus-5-5"})
    cfg.__pydantic_extra__.update(extra)
    agent = ClaudeApiAgent("claude", cfg, rt)
    agent._client = fake
    return agent


def test_claude_api_request_shape(rt):
    critique = {"scores": GOOD_SCORES, "verdict": "accept", "summary": "ok", "problems": []}
    fake = FakeAnthropic(claude_message(json.dumps(critique)))
    result = claude_api(rt, fake).run(AgentTask(role=ROLE_CRITIC, system="Tu es relecteur.", prompt="Évalue", schema=Critique))
    surface, kwargs = fake.calls[0]
    assert surface == "beta" and kwargs["fallbacks"] == "default" and kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["model"] == "claude-opus-5-5" and kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"]["effort"] == "high"
    schema = kwargs["output_config"]["format"]["schema"]
    assert kwargs["output_config"]["format"]["type"] == "json_schema" and schema["additionalProperties"] is False
    assert "temperature" not in kwargs and "budget_tokens" not in json.dumps(kwargs["thinking"])
    assert kwargs["system"] == "Tu es relecteur."
    assert result.data["verdict"] == "accept"
    assert result.usage.tokens_in == 1000 and result.usage.cost_usd == pytest.approx(0.014)  # 1000×4 + 500×20 $/M


def test_claude_api_without_fallbacks_uses_stable_surface(rt):
    fake = FakeAnthropic(claude_message("texte"))
    claude_api(rt, fake, fallbacks=False).run(AgentTask(role=ROLE_PRODUCE, prompt="Rédige"))
    surface, kwargs = fake.calls[0]
    assert surface == "stable" and "fallbacks" not in kwargs and "format" not in kwargs["output_config"]


def test_claude_api_refusal_and_truncation(rt):
    refused = claude_message("", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(AgentError, match="décliné.*cyber") as info:
        claude_api(rt, FakeAnthropic(refused)).run(AgentTask(role=ROLE_PRODUCE, prompt="p"))
    assert not info.value.retryable
    with pytest.raises(AgentError, match="tronquée"):
        claude_api(rt, FakeAnthropic(claude_message("début…", stop_reason="max_tokens"))).run(AgentTask(role=ROLE_PRODUCE, prompt="p"))


def test_claude_api_error_mapping(rt):
    import anthropic

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def status_error(cls, status, message):
        return cls(message, response=httpx.Response(status, request=request), body={"error": {"message": message}})

    retries(rt, "claude", 0)
    rate_limited = status_error(anthropic.RateLimitError, 429, "rate limited")
    with pytest.raises(QuotaError, match="pause"):  # limite de débit persistante → pause, pas échec
        claude_api(rt, FakeAnthropic(error=rate_limited)).run(AgentTask(role=ROLE_PRODUCE, prompt="p"))
    with pytest.raises(AgentError, match="clé refusée") as auth:
        claude_api(rt, FakeAnthropic(error=status_error(anthropic.AuthenticationError, 401, "bad key"))).run(AgentTask(role="r", prompt="p"))
    assert not auth.value.retryable
    with pytest.raises(AgentError) as server:
        claude_api(rt, FakeAnthropic(error=status_error(anthropic.InternalServerError, 500, "oups"))).run(AgentTask(role="r", prompt="p"))
    assert server.value.retryable
    with pytest.raises(AgentError) as timeout:
        claude_api(rt, FakeAnthropic(error=anthropic.APITimeoutError(request=request))).run(AgentTask(role="r", prompt="p"))
    assert "délai" in str(timeout.value)


def test_claude_api_image_attachment(rt, tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG")
    fake = FakeAnthropic(claude_message("vu"))
    claude_api(rt, fake).run(AgentTask(role=ROLE_CRITIC, prompt="Juge", attachments=[image]))
    content = fake.calls[0][1]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "Juge"}


# ───────────────────────────────────────────────────────────────────────── Gemini
def gemini_response(text, *, grounding=None, parts=None, model_version="gemini-test"):
    candidate = SimpleNamespace(finish_reason="STOP", grounding_metadata=grounding)
    usage = SimpleNamespace(prompt_token_count=200, candidates_token_count=50, thoughts_token_count=30)
    return SimpleNamespace(text=text, candidates=[candidate], prompt_feedback=None, usage_metadata=usage,
                           model_version=model_version, parts=parts)


class FakeGemini:
    def __init__(self, response=None, error=None):
        self.calls: list[dict] = []
        self._response, self._error = response, error
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


def gemini(rt, fake) -> GeminiAgent:
    agent = GeminiAgent("gemini", rt.config.agent_cfg("gemini"), rt)
    agent._client = fake
    return agent


def test_gemini_json_mode(rt):
    brief = {"title": "T", "objective": "O"}
    fake = FakeGemini(gemini_response(json.dumps(brief)))
    result = gemini(rt, fake).run(AgentTask(role="repartiteur.plan", system="Tu répartis.", prompt="Planifie", schema=Brief))
    config = fake.calls[0]["config"]
    assert fake.calls[0]["model"] == rt.config.agent_cfg("gemini").model
    assert config.response_mime_type == "application/json" and config.system_instruction == "Tu répartis."
    assert "additionalProperties" not in json.dumps(config.response_json_schema)
    assert not config.tools
    assert result.data == brief and (result.usage.tokens_in, result.usage.tokens_out) == (200, 80)


def test_gemini_grounded_search_returns_sources_and_citations(rt):
    text = "Le compost mûrit en six mois. Il faut l'aérer."
    first = len("Le compost mûrit en six mois.".encode("utf-8"))
    grounding = SimpleNamespace(
        grounding_chunks=[
            SimpleNamespace(web=SimpleNamespace(title="ademe.fr", domain="ademe.fr", uri="https://ex/1")),
            SimpleNamespace(web=SimpleNamespace(title="jardin.fr", domain="jardin.fr", uri="https://ex/2")),
        ],
        grounding_supports=[
            SimpleNamespace(segment=SimpleNamespace(end_index=first), grounding_chunk_indices=[0]),
            SimpleNamespace(segment=SimpleNamespace(end_index=len(text.encode("utf-8"))), grounding_chunk_indices=[1, 0]),
        ],
        web_search_queries=["compost durée"],
    )
    fake = FakeGemini(gemini_response(text, grounding=grounding))
    result = gemini(rt, fake).run(AgentTask(role="chercheur", prompt="Cherche", meta={"grounding": True}, schema=None))
    config = fake.calls[0]["config"]
    assert config.tools and config.tools[0].google_search is not None and config.response_mime_type is None
    assert result.text == "Le compost mûrit en six mois. [1] Il faut l'aérer. [1, 2]"
    assert result.extra["sources"] == [{"title": "ademe.fr", "url": "https://ex/1"}, {"title": "jardin.fr", "url": "https://ex/2"}]


def test_add_citations_uses_byte_offsets():
    text = "Été à Noël."  # accents : 1 caractère ≠ 1 octet
    support = SimpleNamespace(segment=SimpleNamespace(end_index=len(text.encode("utf-8"))), grounding_chunk_indices=[0])
    assert add_citations(text, [support], 1) == "Été à Noël. [1]"
    out_of_range = SimpleNamespace(segment=SimpleNamespace(end_index=3), grounding_chunk_indices=[7])
    assert add_citations(text, [out_of_range], 1) == text


def test_gemini_image_saved_to_part_folder(rt, tmp_path):
    parts = [SimpleNamespace(text="Voici.", inline_data=None),
             SimpleNamespace(text=None, inline_data=SimpleNamespace(data=b"\x89PNG-data", mime_type="image/png"))]
    fake = FakeGemini(gemini_response(None, parts=parts))
    result = gemini(rt, fake).run(AgentTask(role=ROLE_PRODUCE, prompt="Un potager", kind="image", workdir=tmp_path, meta={"filename": "couverture.png"}))
    assert [p.name for p in result.files] == ["couverture.png"]
    assert (tmp_path / "couverture.png").read_bytes() == b"\x89PNG-data"
    assert fake.calls[0]["model"] == rt.config.agent_cfg("gemini").get("image_model")
    assert fake.calls[0]["config"].response_modalities == ["TEXT", "IMAGE"]


def test_gemini_rate_limit_retries_then_pauses(rt):
    from google.genai import errors

    retries(rt, "gemini", 1)
    fake = FakeGemini(error=errors.ClientError(429, {"error": {"message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}}))
    with pytest.raises(QuotaError):
        gemini(rt, fake).run(AgentTask(role=ROLE_PRODUCE, prompt="p"))
    assert len(fake.calls) == 2  # un nouvel essai avant la mise en pause


def test_gemini_bad_request_is_not_retried(rt):
    from google.genai import errors

    fake = FakeGemini(error=errors.ClientError(400, {"error": {"message": "model not found"}}))
    with pytest.raises(AgentError, match="HTTP 400") as info:
        gemini(rt, fake).run(AgentTask(role=ROLE_PRODUCE, prompt="p"))
    assert not info.value.retryable and len(fake.calls) == 1


def test_gemini_video_requires_configured_model(rt, tmp_path):
    cfg = rt.config.agent_cfg("gemini")
    cfg.__pydantic_extra__["video_model"] = ""
    agent = GeminiAgent("gemini", cfg, rt)
    agent._client = FakeGemini()
    with pytest.raises(AgentError, match="video_model"):
        agent.run(AgentTask(role=ROLE_PRODUCE, prompt="p", kind="video", workdir=tmp_path))


# ──────────────────────────────────────────────────────────────── ComfyUI + VRAM
def comfy(rt, handler) -> ComfyUIAgent:
    cfg = rt.config.agent_cfg("comfyui")
    cfg.__pydantic_extra__["poll_seconds"] = 0
    return ComfyUIAgent("comfyui", cfg, rt, transport=httpx.MockTransport(handler))


def test_comfyui_generates_and_downloads_image(rt, tmp_path):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/prompt":
            workflow = json.loads(request.content)["prompt"]
            assert workflow["6"]["inputs"]["text"] == 'Un "potager" urbain'  # guillemets échappés proprement
            assert workflow["5"]["inputs"]["width"] == 1344 and isinstance(workflow["3"]["inputs"]["seed"], int)
            assert workflow["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
            return httpx.Response(200, json={"prompt_id": "abc"})
        if request.url.path == "/history/abc":
            return httpx.Response(200, json={"abc": {"status": {"status_str": "success"}, "outputs": {"9": {"images": [{"filename": "routeur_1.png", "subfolder": "", "type": "output"}]}}}})
        if request.url.path == "/view":
            return httpx.Response(200, content=b"\x89PNG-comfy")
        return httpx.Response(404)

    result = comfy(rt, handler).run(AgentTask(role=ROLE_PRODUCE, prompt='Un "potager" urbain', kind="image", workdir=tmp_path, meta={"filename": "illu.png"}))
    assert (tmp_path / "illu.png").read_bytes() == b"\x89PNG-comfy" and result.files[0].name == "illu.png"
    assert seen[0] == ("POST", "/prompt")


def test_comfyui_unload_frees_vram(rt):
    bodies = []

    def handler(request):
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    comfy(rt, handler).unload()
    assert bodies == [("/free", {"unload_models": True, "free_memory": True})]


def test_gpu_arbiter_unloads_previous_group_only_on_switch():
    log: list[str] = []

    class Local:
        def __init__(self, name, group):
            self.name, self.gpu_group = name, group

        def unload(self):
            log.append(f"unload {self.name}")

    qwen, comfyui, remote = Local("qwen", "llm"), Local("comfyui", "diffusion"), Local("gemini", "")
    arbiter = GpuArbiter(exclusive=True)
    for agent in (qwen, qwen, remote, comfyui, comfyui, remote, qwen):
        with arbiter.use(agent):
            log.append(f"run {agent.name}")
    assert log == ["run qwen", "run qwen", "run gemini", "unload qwen", "run comfyui", "run comfyui",
                   "run gemini", "unload comfyui", "run qwen"]

    log.clear()
    shared = GpuArbiter(exclusive=False)  # assez de VRAM : les deux cohabitent
    for agent in (qwen, comfyui, qwen):
        with shared.use(agent):
            pass
    assert log == []


def test_gpu_arbiter_survives_failed_unload():
    class Broken:
        name, gpu_group = "qwen", "llm"

        def unload(self):
            raise RuntimeError("moteur éteint")

    class Other:
        name, gpu_group = "comfyui", "diffusion"

        def unload(self):
            pass

    arbiter = GpuArbiter(exclusive=True)
    with arbiter.use(Broken()):
        pass
    with arbiter.use(Other()):
        pass  # ne lève pas
    assert arbiter.owner_group == "diffusion"


def test_ollama_unload_sends_keep_alive_zero(rt, monkeypatch):
    calls = []

    class FakeHttp:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path):
            return SimpleNamespace(json=lambda: {"models": [{"name": "qwen2.5:14b"}]})

        def post(self, path, json):
            calls.append((path, json))

    monkeypatch.setattr("router.agents.qwen_anythingllm.httpx.Client", FakeHttp)
    AnythingLLMAgent("qwen", rt.config.agent_cfg("qwen"), rt, transport=httpx.MockTransport(lambda r: httpx.Response(200))).unload()
    assert calls == [("/api/generate", {"model": "qwen2.5:14b", "keep_alive": 0})]


def test_unknown_agent_type_is_rejected(make_config):
    with pytest.raises(ConfigError, match="Type d'agent inconnu"):
        Runtime(make_config(mock=False, agents={"gemini": {"type": "inexistant"}})).agent("gemini")
