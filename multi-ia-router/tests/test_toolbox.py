"""Outils complémentaires : catalogue vérifié, installation, démarrage, état, CLI, tableau de bord."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from router.cli import main
from router.dashboard.app import create_app
from router.errors import ConfigError
from router.runhint import CMD
from router.toolbox import Toolbox, load_catalog
from router.toolbox.core import KEY_VAR, OVERRIDE_NAME, render_env

OFFICIAL = {
    "voicestudio": "debpalash/VoiceStudio",
    "openseo": "every-app/open-seo",
    "cap": "CapSoftware/Cap",
    "chatwoot": "chatwoot/chatwoot",
    "caldiy": "calcom/cal.diy",
    "appflowy": "AppFlowy-IO/AppFlowy",
    "open_generative_ai": "Anil-matcha/Open-Generative-AI",
    "openproject": "opf/openproject",
    "openwhispr": "OpenWhispr/openwhispr",
}
# Ports que le routeur et ses outils habituels occupent déjà.
RESERVED_PORTS = {3001: "AnythingLLM", 8765: "tableau de bord", 8188: "ComfyUI", 11434: "Ollama", 1234: "LM Studio", 8080: "SearXNG"}

OPENSEO_ENV = """# Copy to .env. Choose unique storage credentials before the first start.
STORAGE_ACCESS_KEY=openseo
STORAGE_SECRET_KEY=replace-with-a-long-random-secret
BETTER_AUTH_URL=http://localhost:3001
AUTH_MODE=password
PORT=3001
DATAFORSEO_API_KEY=
CRAWLER_ACCESS_SECRET=
"""
CHATWOOT_ENV = """SECRET_KEY_BASE=replace_with_lengthy_secure_hex
REDIS_PASSWORD=
POSTGRES_PASSWORD=
FRONTEND_URL=http://0.0.0.0:3000
RAILS_ENV=development
ENABLE_ACCOUNT_SIGNUP=false
"""
# Reconstitution fidèle du compose officiel de Chatwoot d'après son contenu relevé (ports, anchors, env).
CHATWOOT_COMPOSE = """x-base: &base
  image: chatwoot/chatwoot:latest
  env_file: .env
  volumes:
    - storage_data:/app/storage
services:
  rails:
    <<: *base
    depends_on: [postgres, redis]
    ports: ['127.0.0.1:3000:3000']
    environment:
      - RAILS_ENV=production
    restart: always
  sidekiq:
    <<: *base
    depends_on: [postgres, redis]
    environment:
      - RAILS_ENV=production
    restart: always
  postgres:
    image: pgvector/pgvector:pg16
    restart: always
    ports: ['127.0.0.1:5432:5432']
    volumes: [postgres_data:/var/lib/postgresql/data]
    environment:
      - POSTGRES_DB=chatwoot
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=
  redis:
    image: redis:alpine
    restart: always
    env_file: .env
    command: ["sh", "-c", "redis-server --requirepass \\"$REDIS_PASSWORD\\""]
    ports: ['127.0.0.1:6379:6379']
    volumes: [redis_data:/data]
volumes:
  storage_data:
  postgres_data:
  redis_data:
"""

OPENPROJECT_ENV = """TAG=17-slim
PORT=127.0.0.1:8080
OPENPROJECT_HTTPS=false
OPENPROJECT_HOST__NAME=localhost:8080
COLLABORATIVE_SERVER_SECRET=secret12345
POSTGRES_PASSWORD=p4ssw0rd
"""

REPOS = {
    "https://github.com/opf/openproject-docker-compose.git": {".env.example": OPENPROJECT_ENV, "docker-compose.yml": "services: {}\n"},
    "https://github.com/every-app/open-seo.git": {"deploy/standalone/.env.example": OPENSEO_ENV, "deploy/standalone/compose.yaml": "services: {}\n"},
    "https://github.com/CapSoftware/Cap.git": {"docker-compose.yml": "services: {}\n"},
    "https://github.com/chatwoot/chatwoot.git": {".env.example": CHATWOOT_ENV, "docker-compose.production.yaml": CHATWOOT_COMPOSE},
}


class FakeRunner:
    """Simule git et docker : enregistre chaque commande, crée les fichiers qu'un clonage créerait."""

    def __init__(self):
        self.calls: list[tuple[list[str], Path | None, dict]] = []
        self.containers: set[str] = set()
        self.fail_on: str | None = None

    def run(self, command, *, cwd=None, env=None, timeout=3600):
        self.calls.append((list(command), cwd, dict(env or {})))
        if self.fail_on and self.fail_on in " ".join(command):
            return subprocess.CompletedProcess(command, 1, "", "échec simulé")
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            (target / ".git").mkdir(parents=True)
            for relative, content in REPOS[command[-2]].items():
                (target / relative).parent.mkdir(parents=True, exist_ok=True)
                (target / relative).write_text(content, encoding="utf-8")
        elif command[:2] == ["docker", "create"]:
            self.containers.add(command[command.index("--name") + 1])
        elif command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.containers else 1, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    @property
    def commands(self) -> list[str]:
        return [" ".join(c) for c, _, _ in self.calls]


@pytest.fixture
def box_factory(make_config, tmp_path, monkeypatch):
    monkeypatch.setattr("router.toolbox.core.shutil.which", lambda name: f"/usr/bin/{name}")

    def build(*, health=None, port_busy=False, **kwargs):
        config = make_config(tools={"dir": str(tmp_path / "tools")})
        config.root = tmp_path / "root"
        config.root.mkdir(exist_ok=True)
        lines: list[str] = []
        runner = kwargs.pop("runner", None) or FakeRunner()
        http = httpx.Client(transport=httpx.MockTransport(health or (lambda request: httpx.Response(503)))) if health is not None else \
            httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        box = Toolbox(config, runner=runner, http=http, say=lines.append, port_check=lambda port: port_busy, **kwargs)
        box.lines = lines
        return box

    return build


# ───────────────────────────────────────────────────────────────────────────── catalogue
def test_catalog_has_the_nine_tools_from_the_two_videos():
    catalog = load_catalog()
    assert set(catalog) == set(OFFICIAL)
    assert {s.replaces for s in catalog.values()} == {
        "ElevenLabs", "Semrush", "Loom", "Intercom", "Calendly", "Notion", "Higgsfield", "Asana", "Wispr Flow",
    }


def test_no_tool_is_listed_twice():
    """La deuxième vidéo reprenait 7 outils de la première sous d'autres noms : aucun doublon."""
    catalog = load_catalog()
    repos = [s.repo.lower() for s in catalog.values()]
    assert len(repos) == len(set(repos))
    replaced = [s.replaces for s in catalog.values()]
    assert len(replaced) == len(set(replaced))  # un seul outil par abonnement remplacé
    names = {s.name.lower() for s in catalog.values()}
    for spec in catalog.values():
        assert not ({a.lower() for a in spec.aka} & names), spec.id  # un alias ne doit pas être un autre outil
    # « OpenHiggsfield » (2e vidéo) est l'ancien nom d'Open Generative AI (1re vidéo)
    assert {"OpenHiggsfield AI", "Open-Higgsfield-AI"} <= set(catalog["open_generative_ai"].aka)


def test_every_repo_is_the_official_owner():
    """Un nom de projet se copie facilement : le propriétaire exact est verrouillé par ce test."""
    for tool_id, spec in load_catalog().items():
        assert spec.repo == f"https://github.com/{OFFICIAL[tool_id]}", tool_id
        if spec.git:
            # Le dépôt d'installation est chez le même propriétaire (parfois un dépôt voisin : OpenProject).
            assert spec.git.split("/")[3] == spec.repo.split("/")[3], tool_id
            assert spec.git.startswith("https://github.com/") and spec.git.endswith(".git"), tool_id
    assert load_catalog()["openproject"].git == "https://github.com/opf/openproject-docker-compose.git"
    assert load_catalog()["openproject"].git_branch == "stable/17"


def test_only_verified_recipes_are_automated():
    automated = {t for t, s in load_catalog().items() if s.automated}
    assert automated == {"voicestudio", "openseo", "cap", "chatwoot", "openproject"}
    manual = {t: s for t, s in load_catalog().items() if not s.automated}
    assert all(s.manual or s.download for s in manual.values()) and all(s.warnings for s in manual.values())


def test_no_executed_recipe_pipes_a_remote_script():
    forbidden = re.compile(r"curl|wget|\|\s*(ba)?sh|iex|Invoke-Expression|sudo|sysctl|xattr", re.IGNORECASE)
    for tool_id, spec in load_catalog().items():
        executed = [spec.git, spec.image, spec.override, " ".join(" ".join(c) for c in spec.first_start), *spec.env.values()]
        for text in executed:
            assert not forbidden.search(text), (tool_id, text)


def test_ports_never_collide():
    ports = {}
    for tool_id, spec in load_catalog().items():
        if not spec.port:  # outil sans serveur local (application de bureau)
            continue
        assert spec.port not in ports, f"{tool_id} et {ports.get(spec.port)} partagent le port {spec.port}"
        assert spec.port not in RESERVED_PORTS, f"{tool_id} : {spec.port} est pris par {RESERVED_PORTS.get(spec.port)}"
        ports[spec.port] = tool_id
    # Cal.diy est manuel et publie 3000 en dur : aucun outil automatisé ne doit viser ce port.
    assert 3000 not in {s.port for s in load_catalog().values() if s.automated}


def test_every_automated_secret_is_used_and_defined():
    for tool_id, spec in load_catalog().items():
        used = set(re.findall(r"\{(\w+)\}", " ".join(spec.env.values()) + spec.override)) - {"port"}
        assert used <= set(spec.secrets), (tool_id, used - set(spec.secrets))
        assert set(spec.secrets) <= used, (tool_id, "secret défini mais jamais utilisé")


# ─────────────────────────────────────────────────────────────────────────── fichiers .env
def test_render_env_replaces_appends_and_keeps_the_rest():
    template = "# commentaire\nA=1\n\nB=\n#C=3\n"
    out = render_env(template, {"A": "x", "B": "y", "C": "z"})
    assert out == "# commentaire\nA=x\n\nB=y\n#C=3\n\n# Ajouté par le routeur multi-IA\nC=z\n"


# ───────────────────────────────────────────────────────────────────────── installation
def test_openseo_install_end_to_end(box_factory, tmp_path):
    box = box_factory()
    box.install("openseo")
    target = tmp_path / "tools" / "openseo"
    env = (target / "deploy/standalone/.env").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in env.splitlines() if "=" in line and not line.startswith("#"))
    assert values["PORT"] == "3011" and values["BETTER_AUTH_URL"] == "http://localhost:3011"  # évite AnythingLLM sur 3001
    assert values["STORAGE_ACCESS_KEY"].startswith("ops") and values["STORAGE_ACCESS_KEY"] != "openseo"
    assert len(values["STORAGE_SECRET_KEY"]) == 64 and "replace" not in env
    assert values["DATAFORSEO_API_KEY"] == "" and values["AUTH_MODE"] == "password"  # la clé payante reste à toi
    if sys.platform != "win32":
        assert stat.S_IMODE((target / "deploy/standalone/.env").stat().st_mode) == 0o600
    assert box.runner.commands == [
        f"git clone --depth 1 https://github.com/every-app/open-seo.git {target}",
        "docker compose -f deploy/standalone/compose.yaml config --quiet",
    ]
    assert box.runner.calls[1][1] == target
    assert not any(values["STORAGE_SECRET_KEY"] in line for line in box.lines)  # jamais affiché


def test_install_is_idempotent_and_never_rewrites_secrets(box_factory, tmp_path):
    box = box_factory()
    box.install("openseo")
    env_path = tmp_path / "tools/openseo/deploy/standalone/.env"
    first = env_path.read_text(encoding="utf-8")
    box.runner.calls.clear()
    box.install("openseo")
    assert env_path.read_text(encoding="utf-8") == first
    assert not any(c.startswith("git clone") for c in box.runner.commands)
    assert any("déjà cloné" in line for line in box.lines) and any("conservé" in line for line in box.lines)


def test_cap_replaces_every_known_default_password(box_factory, tmp_path):
    box = box_factory()
    box.install("cap")
    env = (tmp_path / "tools/cap/.env").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in env.splitlines() if "=" in line and not line.startswith("#"))
    defaults = ["cap-local-pwd-123", "cap-root-pwd-789", "cap-minio-pwd-456", "0123456789abcdef", "abcdef0123456789", "fedcba9876543210"]
    assert not any(d in env for d in defaults)
    assert values["CAP_PORT"] == "3020" and values["CAP_URL"] == "http://localhost:3020"
    for name in ("DATABASE_ENCRYPTION_KEY", "NEXTAUTH_SECRET", "MEDIA_SERVER_WEBHOOK_SECRET"):
        assert re.fullmatch(r"[0-9a-f]{64}", values[name]), name
    assert len({values[n] for n in ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "MINIO_ROOT_PASSWORD")}) == 3


def test_chatwoot_install_writes_override_with_same_password(box_factory, tmp_path):
    box = box_factory()
    box.install("chatwoot")
    target = tmp_path / "tools/chatwoot"
    env = dict(line.split("=", 1) for line in (target / ".env").read_text(encoding="utf-8").splitlines() if "=" in line)
    override = (target / OVERRIDE_NAME).read_text(encoding="utf-8")
    assert env["POSTGRES_PASSWORD"] and env["POSTGRES_PASSWORD"] in override  # même mot de passe côté base et côté Rails
    assert env["FRONTEND_URL"] == "http://localhost:3030" and len(env["SECRET_KEY_BASE"]) == 128
    assert "127.0.0.1:3030:3000" in override and "!reset" in override
    assert box.runner.commands[-1] == "docker compose -f docker-compose.production.yaml -f multi-ia.override.yaml config --quiet"


@pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
                    reason="docker compose absent")
def test_chatwoot_override_is_accepted_by_real_docker_compose(box_factory, tmp_path):
    """`docker compose config` fonctionne sans démon : on valide la fusion pour de vrai."""
    box = box_factory()
    spec = box.spec("chatwoot")
    values = {"pg": "motdepassebase", "port": "3030", "redis": "r", "secret_key": "s"}
    (tmp_path / "compose.yaml").write_text(CHATWOOT_COMPOSE, encoding="utf-8")
    (tmp_path / ".env").write_text("REDIS_PASSWORD=r\n", encoding="utf-8")
    (tmp_path / OVERRIDE_NAME).write_text(box._fill(spec.override, values), encoding="utf-8")
    proc = subprocess.run(["docker", "compose", "-f", "compose.yaml", "-f", OVERRIDE_NAME, "config", "--format", "json"],
                          cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    services = json.loads(proc.stdout)["services"]
    assert [(p["host_ip"], p["published"], p["target"]) for p in services["rails"]["ports"]] == [("127.0.0.1", "3030", 3000)]
    assert "ports" not in services["postgres"] and "ports" not in services["redis"]  # plus de conflit avec ta base locale
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == "motdepassebase"
    assert services["postgres"]["environment"]["POSTGRES_DB"] == "chatwoot"  # le reste est conservé


def test_openproject_install_binds_locally_and_avoids_sudo(box_factory, tmp_path):
    box = box_factory()
    box.install("openproject")
    target = tmp_path / "tools" / "openproject"
    assert box.runner.commands == [
        f"git clone --depth 1 --branch stable/17 https://github.com/opf/openproject-docker-compose.git {target}",
        "docker compose -f docker-compose.yml config --quiet",
    ]
    env = dict(line.split("=", 1) for line in (target / ".env").read_text(encoding="utf-8").splitlines() if "=" in line)
    assert env["PORT"] == "127.0.0.1:8088" and env["OPENPROJECT_HOST__NAME"] == "localhost:8088"  # l'hôte doit suivre le port
    assert env["OPENPROJECT_HTTPS"] == "false" and env["OPDATA"] == "opdata"  # volume nommé : pas de dossier système
    assert re.fullmatch(r"[0-9a-f]{64}", env["SECRET_KEY_BASE"]) and re.fullmatch(r"[0-9a-f]{32}", env["COLLABORATIVE_SERVER_SECRET"])
    assert env["COLLABORATIVE_SERVER_SECRET"] != "secret12345"  # le secret d'exemple du projet est remplacé
    assert env["POSTGRES_PASSWORD"] == "p4ssw0rd"  # laissé tel quel : DATABASE_URL en dépend
    assert not any("sudo" in c or "/var/openproject" in c for c in box.runner.commands)
    assert box.url(box.spec("openproject")) == "http://localhost:8088"  # même hôte que OPENPROJECT_HOST__NAME


def test_voicestudio_install_creates_a_local_only_container_and_keeps_the_key_private(box_factory, tmp_path):
    box = box_factory()
    box.install("voicestudio")
    command, _, env = box.runner.calls[-1]
    assert command[:3] == ["docker", "create", "--name"] and command[-1] == "ghcr.io/debpalash/voicestudio:stable"
    assert "127.0.0.1:3900:3900" in command  # jamais exposé au réseau
    assert "--gpus" not in command and "--restart" not in command
    assert KEY_VAR in command and not any(a.startswith(f"{KEY_VAR}=") for a in command)  # la clé ne passe pas par la ligne de commande
    key = env[KEY_VAR]
    assert len(key) >= 32 and not any(key in line for line in box.lines)
    router_env = (box.config.root / ".env").read_text(encoding="utf-8")
    assert f"{KEY_VAR}={key}" in router_env  # l'agent `voix` relit la même clé
    if sys.platform != "win32":
        assert stat.S_IMODE((box.config.root / ".env").stat().st_mode) == 0o600

    box2 = box_factory()
    box2.install("voicestudio")  # nouveau conteneur, clé déjà connue du routeur : réutilisée
    assert box2.runner.calls[-1][2][KEY_VAR] != "" and (box.config.root / ".env").read_text(encoding="utf-8").count(KEY_VAR) == 1


def test_voicestudio_gpu_flag_and_existing_container(box_factory):
    box = box_factory()
    box.install("voicestudio", gpu=True)
    assert "--gpus all" in " ".join(box.runner.calls[-1][0])
    box.runner.calls.clear()
    box.install("voicestudio")
    assert not any(c.startswith("docker create") for c in box.runner.commands)  # déjà là : conservé


def test_manual_tools_never_run_anything(box_factory):
    runner = FakeRunner()
    box = box_factory(runner=runner)
    for tool_id in ("caldiy", "appflowy", "open_generative_ai", "openwhispr"):
        box.install(tool_id)
    assert runner.calls == []
    text = "\n".join(box.lines)
    assert "Installation manuelle" in text and "Muapi.ai" in text and "archivé" in text and "usage personnel" in text
    assert "ta propre clé" in text and "brew install --cask openwhispr" in text
    for method in (box.start, box.stop):
        with pytest.raises(ConfigError, match="outil manuel"):
            method("caldiy")


def test_dry_run_touches_nothing(box_factory, tmp_path):
    box = box_factory(dry_run=True)
    box.install("chatwoot")
    box.install("voicestudio")
    assert box.runner.calls == [] and not (tmp_path / "tools").exists() and not (box.config.root / ".env").exists()
    assert any("git clone" in line for line in box.lines) and any("docker create" in line for line in box.lines)


def test_failed_command_stops_with_the_reason(box_factory):
    runner = FakeRunner()
    runner.fail_on = "config --quiet"
    with pytest.raises(ConfigError, match="échec simulé"):
        box_factory(runner=runner).install("openseo")


def test_linux_docker_permission_and_daemon_errors_get_an_actionable_hint(box_factory):
    class Failing(FakeRunner):
        def __init__(self, stderr):
            super().__init__()
            self.stderr = stderr

        def run(self, command, **kwargs):
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(command, 1, "", "")
            return subprocess.CompletedProcess(command, 1, "", self.stderr)

    denied = Failing("permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock")
    with pytest.raises(ConfigError, match=r"usermod -aG docker \$USER"):
        box_factory(runner=denied).install("voicestudio")
    down = Failing("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?")
    with pytest.raises(ConfigError, match="systemctl start docker"):
        box_factory(runner=down).install("voicestudio")


def test_missing_docker_is_explained(box_factory, monkeypatch):
    box = box_factory()
    monkeypatch.setattr("router.toolbox.core.shutil.which", lambda name: None)
    with pytest.raises(ConfigError, match="« docker » est introuvable"):
        box.install("voicestudio")


def test_unknown_tool_lists_the_known_ones(box_factory):
    with pytest.raises(ConfigError, match="Outils : voicestudio"):
        box_factory().install("inconnu")


# ──────────────────────────────────────────────────────────────── démarrer et arrêter
def test_start_refuses_when_not_installed_or_port_busy(box_factory):
    with pytest.raises(ConfigError, match="n'est pas installé"):
        box_factory().start("cap")
    box = box_factory()
    box.install("cap")
    busy = box_factory(runner=box.runner, port_busy=True)
    busy.config.tools.dir = box.config.tools.dir
    with pytest.raises(ConfigError, match="port 3020 est déjà occupé"):
        busy.start("cap")


def test_start_runs_initialisation_once(box_factory, tmp_path):
    box = box_factory()
    box.install("chatwoot")
    box.runner.calls.clear()
    box.start("chatwoot")
    prepare = "docker compose -f docker-compose.production.yaml -f multi-ia.override.yaml run --rm rails bundle exec rails db:chatwoot_prepare"
    up = "docker compose -f docker-compose.production.yaml -f multi-ia.override.yaml up -d"
    assert box.runner.commands == [prepare, up]
    box.runner.calls.clear()
    box.start("chatwoot")
    assert box.runner.commands == [up]  # l'initialisation de la base n'est faite qu'une fois


def test_start_is_a_no_op_when_already_running(box_factory):
    box = box_factory(health=lambda request: httpx.Response(200, json={"status": "ok"}))
    box.start("voicestudio")
    assert box.runner.calls == [] and any("répond déjà" in line for line in box.lines)


def test_nothing_in_the_lifecycle_deletes_data(box_factory):
    """Vérifié sur la structure des commandes : seuls ces verbes existent, aucun ne supprime."""
    box = box_factory()
    for tool_id in ("voicestudio", "openseo", "cap", "chatwoot", "openproject"):
        box.install(tool_id)
        box.start(tool_id)
        box.stop(tool_id)
    allowed_docker = {"create", "start", "stop", "inspect", "compose"}
    allowed_compose = {"config", "up", "stop", "run"}
    seen_compose = set()
    for command, _, _ in box.runner.calls:
        if command[0] == "git":
            assert command[:3] == ["git", "clone", "--depth"]
            continue
        assert command[0] == "docker" and command[1] in allowed_docker, command
        if command[1] == "compose":
            verbs = [a for a in command[2:] if a in allowed_compose | {"down", "rm", "kill", "pull", "build", "restart"}]
            assert verbs and verbs[0] in allowed_compose, command
            seen_compose.add(verbs[0])
            assert "-v" not in command and "--volumes" not in command, command
    assert {"config", "up", "stop"} <= seen_compose
    joined = "\n".join(box.runner.commands)
    assert "docker stop multi-ia-voicestudio" in joined and "docker compose -f deploy/standalone/compose.yaml stop" in joined


# ───────────────────────────────────────────────────────────────────────────── état
def test_status_states(box_factory):
    box = box_factory()
    assert box.status("voicestudio").state == "absent"
    box.runner.containers.add("multi-ia-voicestudio")
    assert box.status("voicestudio").state == "installé"
    assert box.status("caldiy").state == "manuel" and box.status("caldiy").installed is None

    def health(request):
        return httpx.Response(200 if request.url.port == 3900 else 503)

    running = box_factory(health=health)
    assert running.status("voicestudio").state == "en marche"
    assert running.status("voicestudio").url == "http://127.0.0.1:3900"
    assert running.status("chatwoot").state == "absent"


def test_voicestudio_needs_a_strict_200_but_others_accept_any_non_error(box_factory):
    box = box_factory(health=lambda request: httpx.Response(404))
    assert box.status("voicestudio").up is False  # /health absent ≠ serveur prêt
    assert box.status("cap").up is True  # une réponse HTTP (même 404) prouve que l'application répond
    assert box_factory(health=lambda request: httpx.Response(503)).status("cap").up is False


def test_configured_port_overrides_the_catalog(make_config, tmp_path):
    config = make_config(tools={"ports": {"cap": 4040}})
    box = Toolbox(config, say=lambda line: None)
    assert box.port(box.spec("cap")) == 4040 and box.url(box.spec("cap")) == "http://127.0.0.1:4040"


# ──────────────────────────────────────────────────────────────── ligne de commande
@pytest.fixture
def cli(monkeypatch, tmp_path):
    """`main` sur une config neuve : ni config.local.yaml, ni .env de la machine, ni vrai Docker."""
    from conftest import ROOT

    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr("router.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("router.toolbox.core.Runner.run", lambda self, command, **kw: subprocess.CompletedProcess(command, 1, "", ""))
    monkeypatch.setattr("router.toolbox.core.Toolbox._health", lambda self, spec: False)
    monkeypatch.setattr("router.cli.setup_logging", lambda *a, **k: None)
    return lambda *args: main(["--config", str(tmp_path / "config.yaml"), *args])


def test_cli_list_and_status(cli, capsys):
    assert cli("tools", "list") == 0
    out = capsys.readouterr().out
    for name in ("VoiceStudio", "OpenSEO", "Cap", "Chatwoot", "Cal.diy", "AppFlowy", "Open Generative AI", "OpenProject", "OpenWhispr"):
        assert name in out
    assert "ElevenLabs" in out and "● en marche" in out
    assert cli("tools", "status", "openseo") == 0
    detail = capsys.readouterr().out
    assert "DataForSEO" in detail and "https://github.com/every-app/open-seo" in detail and "Non branché" in detail


def test_cli_install_all_dry_run_lists_manual_tools(cli, capsys, monkeypatch):
    monkeypatch.setattr("router.toolbox.core.shutil.which", lambda name: f"/usr/bin/{name}")
    assert cli("tools", "install", "--all", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Rien n'est démarré" in out and f"Cal.diy : {CMD} tools install caldiy" in out
    commands = [line for line in out.splitlines() if line.startswith("  $ ")]
    assert commands and all(c.split()[1] in ("git", "docker") for c in commands)  # seules commandes lancées
    assert not any(re.search(r"curl|wget|\| *sh|iex|sudo", c) for c in commands)


def test_cli_errors_are_readable(cli, capsys):
    assert cli("tools", "install") == 2
    assert cli("tools", "start", "inconnu") == 1
    assert "Outil inconnu" in capsys.readouterr().err


# ───────────────────────────────────────────────────────────────── tableau de bord
def test_dashboard_lists_tools(make_config):
    config = make_config()
    runner = FakeRunner()
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200 if request.url.port == 3900 else 503)))
    box = Toolbox(config, runner=runner, http=http, say=lambda line: None)
    with TestClient(create_app(config, toolbox=box), base_url="http://127.0.0.1:8765") as client:
        tools = client.get("/api/tools").json()
        by_id = {t["id"]: t for t in tools}
        assert set(by_id) == set(OFFICIAL)
        assert by_id["voicestudio"]["state"] == "en marche" and by_id["voicestudio"]["up"] and by_id["voicestudio"]["url"] == "http://127.0.0.1:3900"
        assert by_id["openseo"]["state"] == "absent" and by_id["openseo"]["command"] == f"{CMD} tools start openseo"
        assert by_id["caldiy"]["command"] == f"{CMD} tools install caldiy" and by_id["caldiy"]["automated"] is False
        assert "Branché" in by_id["voicestudio"]["router"] and by_id["openseo"]["warnings"]
        page = client.get("/").text
        assert 'id="tools"' in page and "Outils complémentaires" in page


def test_catalog_yaml_is_valid_utf8_yaml():
    raw = Path(__file__).resolve().parent.parent / "router" / "toolbox" / "catalog.yaml"
    assert len(yaml.safe_load(raw.read_text(encoding="utf-8"))) == 9


def test_help_messages_never_suggest_a_bare_python_command(box_factory, capsys):
    """Sous Linux, `python` n'existe souvent pas : chaque conseil affiché doit passer par `routeur`."""
    box = box_factory()
    box.install("voicestudio")
    box.install("caldiy")
    with pytest.raises(ConfigError) as info:
        box_factory().start("cap")
    shown = "\n".join(box.lines) + str(info.value)
    assert "python -m router" not in shown and CMD in shown
    assert CMD == ("routeur" if sys.platform == "win32" else "./routeur")
