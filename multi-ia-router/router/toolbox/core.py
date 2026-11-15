"""Installation, démarrage et état des outils complémentaires.

Principes :
- Rien ne s'installe sans commande explicite (`python -m router tools install …`).
- Les outils « manual » ne sont jamais installés par le routeur : il affiche les étapes.
- Aucune commande n'efface de données : `stop` arrête, il ne supprime ni conteneur ni volume.
- Un `.env` existant n'est jamais réécrit (changer un mot de passe de base après coup la casserait).
- Les secrets sont générés ici, écrits dans des fichiers en lecture réservée, jamais affichés.
- Toutes les commandes sont des listes d'arguments (jamais de shell).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
import yaml
from pydantic import BaseModel, Field

from ..errors import ConfigError

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")
OVERRIDE_NAME = "multi-ia.override.yaml"
INIT_MARKER = ".multi-ia-initialise"
KEY_VAR = "OMNIVOICE_API_KEY"  # clé partagée entre le conteneur VoiceStudio et l'agent `voix`


class ToolSpec(BaseModel):
    id: str = ""
    name: str
    replaces: str
    summary: str
    repo: str
    license: str
    kind: Literal["container", "compose", "manual"]
    port: int = 0
    url: str = ""
    health: str = "/"
    health_strict: bool = False  # True : seul un 200 compte comme « en marche »
    git: str = ""
    git_branch: str = ""
    aka: list[str] = Field(default_factory=list)
    compose_file: str = ""
    env_file: str = ""
    env_template: str = ""
    secrets: dict[str, int] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    override: str = ""
    first_start: list[list[str]] = Field(default_factory=list)
    image: str = ""
    container: str = ""
    volume: str = ""
    download: str = ""
    disk: str = ""
    needs: list[str] = Field(default_factory=list)
    verified: str = ""
    router: str = ""
    manual: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def automated(self) -> bool:
        return self.kind != "manual"


def load_catalog(path: Path | None = None) -> dict[str, ToolSpec]:
    data = yaml.safe_load((path or CATALOG_PATH).read_text(encoding="utf-8")) or {}
    return {tool_id: ToolSpec(id=tool_id, **fields) for tool_id, fields in data.items()}


@dataclass
class Status:
    id: str
    name: str
    replaces: str
    kind: str
    state: str  # en marche | installé | absent | manuel
    installed: bool | None
    up: bool
    url: str
    port: int
    router: str = ""
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class Runner:
    """Exécute des commandes. Remplaçable dans les tests."""

    def run(self, command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
            timeout: float = 3600.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            command, cwd=str(cwd) if cwd else None, env={**os.environ, **(env or {})},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def render_env(template: str, values: dict[str, str]) -> str:
    """Remplace les lignes `CLE=…` d'un modèle .env ; ajoute les clés absentes. Le reste est conservé."""
    remaining = dict(values)
    lines: list[str] = []
    for line in template.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    if remaining:
        lines += ["", "# Ajouté par le routeur multi-IA"] + [f"{k}={v}" for k, v in remaining.items()]
    return "\n".join(lines).rstrip() + "\n"


def write_private(path: Path, content: str) -> None:
    """Écrit un fichier de secrets lisible par le seul utilisateur (sans effet sous Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_env_value(path: Path, name: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^\s*{re.escape(name)}\s*=\s*(.*)$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return ""


class Toolbox:
    def __init__(self, config, *, runner: Runner | None = None, http: httpx.Client | None = None,
                 dry_run: bool = False, say: Callable[[str], None] = print,
                 port_check: Callable[[int], bool] = port_in_use):
        self.config = config
        self.catalog = load_catalog()
        self.runner = runner or Runner()
        self.http = http
        self.dry_run = dry_run
        self.say = say
        self.port_check = port_check

    # --------------------------------------------------------------------- accès
    def spec(self, tool_id: str) -> ToolSpec:
        if tool_id not in self.catalog:
            raise ConfigError(f"Outil inconnu : « {tool_id} ». Outils : {', '.join(self.catalog)}")
        return self.catalog[tool_id]

    @property
    def base_dir(self) -> Path:
        configured = self.config.tools.dir
        return Path(configured).expanduser() if configured else Path.home() / ".multi-ia-router" / "tools"

    def tool_dir(self, spec: ToolSpec) -> Path:
        return self.base_dir / spec.id

    def port(self, spec: ToolSpec) -> int:
        return int(self.config.tools.ports.get(spec.id, spec.port))

    def url(self, spec: ToolSpec) -> str:
        return spec.url.format(port=self.port(spec)) if spec.url else ""

    @property
    def router_env(self) -> Path:
        return self.config.root / ".env"

    # -------------------------------------------------------------------- commandes
    def _exec(self, command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
              timeout: float = 3600.0, shown: list[str] | None = None) -> subprocess.CompletedProcess | None:
        """Lance une commande (ou la montre seulement en mode simulation). Lève ConfigError si elle échoue."""
        printable = shown or command
        self.say("  $ " + " ".join(printable))
        if self.dry_run:
            return None
        try:
            proc = self.runner.run(command, cwd=cwd, env=env, timeout=timeout)
        except FileNotFoundError as exc:
            raise ConfigError(f"Commande introuvable : {command[0]}. Installe-la d'abord ({exc}).") from exc
        except subprocess.TimeoutExpired as exc:
            raise ConfigError(f"Délai dépassé ({timeout:.0f} s) : {' '.join(printable)}") from exc
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-1200:]
            hint = ""
            if "permission denied" in tail.lower() and "docker.sock" in tail.lower():
                hint = ("\nLinux : ton compte n'a pas le droit d'utiliser Docker. "
                        "Ajoute-le au groupe docker : sudo usermod -aG docker $USER, "
                        "puis ferme et rouvre ta session (ou lance : newgrp docker).")
            elif "cannot connect to the docker daemon" in tail.lower() or "is the docker daemon running" in tail.lower():
                hint = "\nLe service Docker n'est pas démarré. Linux : sudo systemctl start docker (Windows/macOS : lance Docker Desktop)."
            raise ConfigError(f"Échec de `{' '.join(printable)}` (code {proc.returncode}) :\n{tail}{hint}")
        return proc

    def _need(self, binary: str) -> None:
        if not self.dry_run and shutil.which(binary) is None:
            raise ConfigError(f"« {binary} » est introuvable dans le PATH.")

    def _compose_args(self, spec: ToolSpec) -> list[str]:
        args = ["docker", "compose", "-f", spec.compose_file]
        if spec.override:
            args += ["-f", OVERRIDE_NAME]
        return args

    # ----------------------------------------------------------------------- état
    def _health(self, spec: ToolSpec) -> bool:
        if not spec.url:
            return False
        target = self.url(spec).rstrip("/") + spec.health
        client = self.http or httpx.Client(timeout=1.5)
        try:
            response = client.get(target)
        except httpx.HTTPError:
            return False
        finally:
            if self.http is None:
                client.close()
        return response.status_code == 200 if spec.health_strict else response.status_code < 500

    def _installed(self, spec: ToolSpec) -> bool | None:
        if spec.kind == "manual":
            return None
        if spec.kind == "compose":
            return (self.tool_dir(spec) / spec.env_file).exists()
        try:
            proc = self.runner.run(["docker", "inspect", "--format", "{{.Id}}", spec.container], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def status(self, tool_id: str) -> Status:
        spec = self.spec(tool_id)
        up = self._health(spec)
        installed = self._installed(spec) if not up else (True if spec.automated else None)
        if spec.kind == "manual":
            state = "en marche" if up else "manuel"
        else:
            state = "en marche" if up else ("installé" if installed else "absent")
        return Status(
            id=spec.id, name=spec.name, replaces=spec.replaces, kind=spec.kind, state=state,
            installed=installed, up=up, url=self.url(spec), port=self.port(spec), router=spec.router,
        )

    def statuses(self) -> list[Status]:
        return [self.status(tool_id) for tool_id in self.catalog]

    # ------------------------------------------------------------------ installation
    def _values(self, spec: ToolSpec) -> dict[str, str]:
        """Secrets générés (une fois par installation) + port."""
        values = {name: secrets.token_hex(length // 2 + length % 2)[:length] for name, length in spec.secrets.items()}
        values["port"] = str(self.port(spec))
        return values

    @staticmethod
    def _fill(text: str, values: dict[str, str]) -> str:
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        return text

    def install(self, tool_id: str, *, gpu: bool = False) -> None:
        spec = self.spec(tool_id)
        self.say(f"\n── {spec.name} (remplace {spec.replaces})")
        for warning in spec.warnings:
            self.say(f"  ! {warning}")
        if spec.kind == "manual":
            self._print_manual(spec)
            return
        self._need("docker")
        if spec.kind == "container":
            self._install_container(spec, gpu)
        else:
            self._install_compose(spec)
        self.say(f"  ✓ prêt. Démarrer : python -m router tools start {spec.id}")

    def _print_manual(self, spec: ToolSpec) -> None:
        self.say("  Installation manuelle (le routeur n'exécute rien pour cet outil) :")
        if spec.download:
            self.say(f"  - Téléchargement : {spec.download}")
        for step in spec.manual:
            self.say(f"  - {step}")
        self.say(f"  Dépôt officiel : {spec.repo}")

    def _install_container(self, spec: ToolSpec, gpu: bool) -> None:
        key = read_env_value(self.router_env, KEY_VAR)
        generated = not key
        if generated:
            key = secrets.token_urlsafe(32)
            self.say(f"  Clé d'accès générée : elle sera écrite dans {self.router_env.name} ({KEY_VAR}), sans être affichée.")
        if not self.dry_run:
            exists = self.runner.run(["docker", "inspect", "--format", "{{.Id}}", spec.container], timeout=30).returncode == 0
            if exists:
                self.say(f"  Le conteneur « {spec.container} » existe déjà : conservé tel quel.")
                return
        port = self.port(spec)
        command = ["docker", "create", "--name", spec.container, "-p", f"127.0.0.1:{port}:{spec.port}",
                   "-e", KEY_VAR, "-e", "OMNIVOICE_DATA_DIR=/app/omnivoice_data",
                   "-e", "OMNIVOICE_ANALYTICS_DISABLED=1", "-v", f"{spec.volume}:/app/omnivoice_data"]
        if gpu:
            command += ["--gpus", "all"]
        command += [spec.image]
        self._exec(command, env={KEY_VAR: key}, timeout=3600)  # télécharge l'image si besoin
        if generated and not self.dry_run:
            self._remember_key(key)

    def _remember_key(self, key: str) -> None:
        current = self.router_env.read_text(encoding="utf-8") if self.router_env.exists() else ""
        if re.search(rf"^\s*{KEY_VAR}\s*=", current, re.MULTILINE):
            return
        write_private(self.router_env, current.rstrip("\n") + ("\n" if current.strip() else "") + f"{KEY_VAR}={key}\n")

    def _install_compose(self, spec: ToolSpec) -> None:
        target = self.tool_dir(spec)
        self._need("git")
        if (target / ".git").exists():
            self.say(f"  Dépôt déjà cloné : {target}")
        else:
            self.say(f"  Dépôt officiel : {spec.git}")
            if not self.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
            branch = ["--branch", spec.git_branch] if spec.git_branch else []
            self._exec(["git", "clone", "--depth", "1", *branch, spec.git, str(target)], timeout=900)

        values = self._values(spec)
        env_path = target / spec.env_file
        if env_path.exists():
            self.say(f"  {spec.env_file} existe déjà : conservé (mots de passe inchangés).")
        else:
            template = ""
            template_path = target / spec.env_template if spec.env_template else None
            if template_path is not None and (template_path.exists() or self.dry_run):
                template = template_path.read_text(encoding="utf-8") if template_path.exists() else ""
            rendered = render_env(template, {k: self._fill(v, values) for k, v in spec.env.items()})
            self.say(f"  Écriture de {env_path} (secrets aléatoires, accès réservé à ton compte).")
            if not self.dry_run:
                write_private(env_path, rendered)
        if spec.override:
            override_path = target / OVERRIDE_NAME
            if override_path.exists():
                self.say(f"  {OVERRIDE_NAME} existe déjà : conservé.")
            else:
                self.say(f"  Écriture de {override_path} (ports et mot de passe de la base).")
                if not self.dry_run:
                    write_private(override_path, self._fill(spec.override, values))
        # Validation avant tout démarrage : fusion des fichiers compose et variables.
        self._exec(self._compose_args(spec) + ["config", "--quiet"], cwd=target, timeout=120)

    # ------------------------------------------------------------------- démarrer
    def start(self, tool_id: str) -> None:
        spec = self.spec(tool_id)
        if spec.kind == "manual":
            raise ConfigError(f"{spec.name} est un outil manuel : lance-le toi-même (voir `tools install {spec.id}`).")
        self._need("docker")
        if self._health(spec):
            self.say(f"  {spec.name} répond déjà sur {self.url(spec)}.")
            return
        if not self.dry_run and self._installed(spec) is not True:
            raise ConfigError(f"{spec.name} n'est pas installé : python -m router tools install {spec.id}")
        port = self.port(spec)
        if self.port_check(port) and not self.dry_run:
            raise ConfigError(
                f"Le port {port} est déjà occupé par un autre programme (pas {spec.name}). "
                f"Libère-le, ou change `tools.ports.{spec.id}` dans config.local.yaml puis réinstalle."
            )
        if spec.kind == "container":
            self._exec(["docker", "start", spec.container], timeout=300)
        else:
            target = self.tool_dir(spec)
            marker = target / INIT_MARKER
            if spec.first_start and not marker.exists():
                self.say("  Premier démarrage : initialisation.")
                for extra in spec.first_start:
                    self._exec(self._compose_args(spec) + extra, cwd=target, timeout=3600)
                if not self.dry_run:
                    marker.write_text("ok\n", encoding="utf-8")
            self._exec(self._compose_args(spec) + ["up", "-d"], cwd=target, timeout=3600)
        self.say(f"  ✓ {spec.name} démarre : {self.url(spec)} (les images et modèles peuvent prendre plusieurs minutes au premier lancement).")

    # --------------------------------------------------------------------- arrêter
    def stop(self, tool_id: str) -> None:
        spec = self.spec(tool_id)
        if spec.kind == "manual":
            raise ConfigError(f"{spec.name} est un outil manuel : ferme-le toi-même.")
        self._need("docker")
        if spec.kind == "container":
            self._exec(["docker", "stop", spec.container], timeout=300)
        else:
            # `stop` conserve conteneurs et volumes : aucune donnée n'est supprimée.
            self._exec(self._compose_args(spec) + ["stop"], cwd=self.tool_dir(spec), timeout=600)
        self.say(f"  ✓ {spec.name} arrêté (données conservées).")
