"""`python -m router doctor` — détecte ton PC et vérifie chaque connexion.

Relève tout ce qui est détectable : système, carte graphique et VRAM, Blender, Claude Code,
Ollama / LM Studio, AnythingLLM (workspaces), ComfyUI, vaults Obsidian, clés API, modèles Gemini.
Avec `--write`, les valeurs trouvées vont dans config.local.yaml (sans écraser tes réglages).
Aucune clé n'est jamais affichée : seulement « présente » ou « absente ».
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import Config
from .errors import RouterError
from .tools import blender as blender_tool

OK, BAD, INFO = "✓", "✗", "·"


def _run(command: list[str], timeout: float = 15.0) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def _get(url: str, timeout: float = 3.0, headers: dict | None = None) -> Any:
    try:
        response = httpx.get(url, timeout=timeout, headers=headers)
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


# ───────────────────────────────────────────────────────────────────────── détections
def detect_ram_gb() -> float:
    system = platform.system()
    try:
        if system == "Linux":
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 2**20, 1)
        if system == "Darwin":
            return round(int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30, 1)
        if system == "Windows":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in (
                        "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                        "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual",
                    )
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
            return round(status.ullTotalPhys / 2**30, 1)
    except (OSError, ValueError, AttributeError):
        pass
    return 0.0


def detect_gpus() -> list[dict]:
    """Cartes graphiques [{name, vram_gb, reliable}]. nvidia-smi d'abord, sinon ce que l'OS déclare."""
    output = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    gpus: list[dict] = []
    for line in output.splitlines():
        name, _, memory = line.rpartition(",")
        try:
            gpus.append({"name": name.strip(), "vram_gb": round(float(memory) / 1024, 1), "reliable": True})
        except ValueError:
            continue
    if gpus:
        return gpus
    system = platform.system()
    if system == "Windows":
        raw = _run([
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json",
        ])
        try:
            data = json.loads(raw) if raw else []
        except ValueError:
            data = []
        for item in data if isinstance(data, list) else [data]:
            ram = item.get("AdapterRAM") or 0
            # Windows plafonne cette valeur à 4 Go : indicatif seulement.
            gpus.append({"name": item.get("Name", "?"), "vram_gb": round(ram / 2**30, 1), "reliable": False})
    elif system == "Darwin":
        for line in _run(["system_profiler", "SPDisplaysDataType"]).splitlines():
            if "Chipset Model:" in line:
                gpus.append({"name": line.split(":", 1)[1].strip(), "vram_gb": 0.0, "reliable": False})
    return gpus


def detect_blender(configured: str | list[str]) -> tuple[str, str]:
    """(chemin, version) de Blender : config, PATH, puis emplacements habituels."""
    candidates: list[str] = []
    if isinstance(configured, str):
        candidates.append(configured)
        if shutil.which(configured):
            candidates.insert(0, shutil.which(configured) or configured)
    system = platform.system()
    if system == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            if base:
                candidates += sorted(glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe")), reverse=True)
        candidates += glob.glob(r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe")
    elif system == "Darwin":
        candidates += ["/Applications/Blender.app/Contents/MacOS/Blender"]
    else:
        candidates += ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender", "/var/lib/flatpak/exports/bin/org.blender.Blender"]
        candidates += sorted(glob.glob(str(Path.home() / "blender*" / "blender")), reverse=True)
    for candidate in candidates:
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            version = blender_tool.version(candidate)
            if version:
                return candidate, version
    return "", ""


def detect_obsidian_vaults() -> list[str]:
    """Vaults connues d'Obsidian (lues dans son fichier obsidian.json)."""
    home = Path.home()
    locations = [
        Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json",
        home / ".config" / "obsidian" / "obsidian.json",
        home / "Library" / "Application Support" / "obsidian" / "obsidian.json",
        home / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
        home / "snap" / "obsidian" / "current" / ".config" / "obsidian" / "obsidian.json",
    ]
    vaults: list[str] = []
    for location in locations:
        try:
            data = json.loads(location.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in (data.get("vaults") or {}).values():
            path = entry.get("path")
            if path and path not in vaults and Path(path).is_dir():
                vaults.append(path)
    return vaults


def detect_ollama(url: str) -> dict | None:
    tags = _get(url.rstrip("/") + "/api/tags")
    if tags is None:
        return None
    loaded = _get(url.rstrip("/") + "/api/ps") or {}
    return {
        "models": [m.get("name", "") for m in tags.get("models", [])],
        "loaded": [m.get("name", "") for m in loaded.get("models", [])],
    }


def detect_lmstudio(url: str = "http://localhost:1234") -> list[str] | None:
    data = _get(url + "/v1/models")
    if data is None:
        return None
    return [m.get("id", "") for m in data.get("data", [])]


# ───────────────────────────────────────────────────────────────────────────── rapport
def run_doctor(config: Config, write: bool = False) -> int:
    found: dict[str, Any] = {}
    todo: list[str] = []

    def section(title: str) -> None:
        print(f"\n{title}\n" + "─" * len(title))

    def line(mark: str, text: str) -> None:
        print(f"  {mark} {text}")

    section("Machine")
    line(INFO, f"Système : {platform.system()} {platform.release()} ({platform.machine()})")
    python_ok = sys.version_info >= (3, 12)
    line(OK if python_ok else BAD, f"Python {platform.python_version()}{'' if python_ok else ' — 3.12 ou plus requis'}")
    ram = detect_ram_gb()
    line(INFO, f"Processeur : {os.cpu_count()} cœurs — RAM : {ram or '?'} Go")
    gpus = detect_gpus()
    if not gpus:
        line(INFO, "Carte graphique : non détectée (nvidia-smi absent)")
    vram = 0.0
    for gpu in gpus:
        note = "" if gpu["reliable"] else " (valeur indicative déclarée par le système)"
        line(OK, f"Carte graphique : {gpu['name']} — {gpu['vram_gb'] or '?'} Go VRAM{note}")
        if gpu["reliable"]:
            vram = max(vram, gpu["vram_gb"])

    section("Clés API (.env)")
    for name in ("ANYTHINGLLM_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        present = bool(os.environ.get(name))
        optional = name == "ANTHROPIC_API_KEY"
        line(OK if present else (INFO if optional else BAD), f"{name} : {'présente' if present else 'absente'}"
             + (" (utile seulement avec type: claude_api)" if optional and not present else ""))
        if not present and not optional:
            todo.append(f"Renseigner {name} dans .env")

    section("Outils locaux")
    path, version = detect_blender(config.paths.blender_bin)
    if path:
        line(OK, f"Blender : {version} — {path}")
        found.setdefault("paths", {})["blender_bin"] = path
    else:
        line(BAD, "Blender : introuvable (renseigne paths.blender_bin si tu veux des parties Blender)")
    claude = shutil.which("claude")
    if claude:
        line(OK, f"Claude Code : {_run([claude, '--version'], 30) or 'présent'} — {claude}")
    else:
        line(BAD, "Claude Code : introuvable (https://claude.com/claude-code)")
        todo.append("Installer Claude Code et lancer `claude` une fois pour se connecter")
    vaults = detect_obsidian_vaults()
    for vault in vaults:
        line(OK, f"Vault Obsidian : {vault}")
    if not vaults:
        line(INFO, "Vault Obsidian : aucune détectée (renseigne paths.vault_dir à la main)")
    elif len(vaults) == 1:
        found.setdefault("paths", {})["vault_dir"] = vaults[0]

    section("Modèles locaux")
    ollama = detect_ollama(config.gpu.ollama_url)
    lmstudio = detect_lmstudio()
    if ollama is not None:
        models = ollama["models"]
        qwen = [m for m in models if "qwen" in m.lower()]
        line(OK, f"Ollama : {len(models)} modèle(s) — Qwen : {', '.join(qwen) or 'aucun'}")
        if ollama["loaded"]:
            line(INFO, f"en mémoire : {', '.join(ollama['loaded'])}")
        found.setdefault("gpu", {})["llm_backend"] = "ollama"
    else:
        line(INFO, f"Ollama : pas de réponse sur {config.gpu.ollama_url}")
    if lmstudio is not None:
        qwen = [m for m in lmstudio if "qwen" in m.lower()]
        line(OK, f"LM Studio : {len(lmstudio)} modèle(s) — Qwen : {', '.join(qwen) or 'aucun'}")
        if ollama is None:
            found.setdefault("gpu", {})["llm_backend"] = "lmstudio"
    else:
        line(INFO, "LM Studio : pas de réponse sur http://localhost:1234")

    section("Agents configurés")
    from .runtime import Runtime

    real = config.model_copy(update={"mock": False})
    real.root = config.root
    runtime = Runtime(real)
    for name, cfg in config.agents.items():
        if not cfg.enabled:
            print(f"  {name} ({cfg.type}) : désactivé")
            if cfg.type == "comfyui" and _get(str(cfg.get("base_url", "http://127.0.0.1:8188")).rstrip("/") + "/system_stats"):
                line(OK, "ComfyUI répond : tu peux l'activer (agents.comfyui.enabled: true)")
                found.setdefault("agents", {}).setdefault(name, {})["enabled"] = True
            continue
        print(f"  {name} ({cfg.type}{', ' + cfg.model if cfg.model else ''})")
        try:
            lines = runtime.agent(name).diagnose()
        except RouterError as exc:
            lines = [f"{BAD} {exc}"]
        for text in lines:
            print(f"    {text}")
            if text.startswith(BAD):
                todo.append(f"{name} : {text[2:].strip()}")

    section("Recherche")
    research = config.research
    if research.vault.enabled:
        line(INFO, f"Vault : workspace AnythingLLM « {research.vault.workspace} » (via l'agent {research.vault.agent})")
        try:
            from .agents.qwen_anythingllm import client_from_cfg

            slugs = [w.get("slug") for w in client_from_cfg(config.agent_cfg(research.vault.agent), timeout=10).workspaces()]
            good = research.vault.workspace in slugs
            line(OK if good else BAD, f"workspace « {research.vault.workspace} » {'trouvé' if good else 'INTROUVABLE'}")
            if not good:
                todo.append(f"Créer le workspace AnythingLLM « {research.vault.workspace} » et y indexer la vault")
        except RouterError as exc:
            line(BAD, str(exc))
    line(INFO, f"Web : {research.web.provider if research.web.enabled else 'désactivée'}")
    if research.web.enabled and research.web.provider == "searxng":
        good = _get(research.web.searxng_url.rstrip("/") + "/search?q=test&format=json") is not None
        line(OK if good else BAD, f"SearXNG {'répond' if good else 'ne répond pas en JSON'} ({research.web.searxng_url})")

    section("Cohérence de la configuration")
    try:
        config.check()
        line(OK, "Rôles pourvus, personne ne critique sa propre famille")
    except RouterError as exc:
        line(BAD, str(exc))
        todo.append(str(exc))
    if vram:
        exclusive = vram < 24
        found.setdefault("gpu", {})["exclusive"] = exclusive
        line(INFO, f"VRAM {vram} Go → gpu.exclusive conseillé : {str(exclusive).lower()} "
             f"({'Qwen et ComfyUI à tour de rôle' if exclusive else 'les deux peuvent cohabiter'})")

    section("À faire")
    if todo:
        for item in todo:
            line(BAD, item)
    else:
        line(OK, "Rien de bloquant détecté.")
    print("\n  Essai sans aucun appel réel : python -m router --mock run \"ma demande\"")

    if write:
        written = write_local_config(config.root / "config.local.yaml", found)
        section("config.local.yaml")
        if written:
            for key in written:
                line(OK, f"écrit : {key}")
        else:
            line(INFO, "rien à ajouter (déjà renseigné ou rien détecté)")
    elif found:
        print("  Enregistrer les valeurs détectées : python -m router doctor --write")
    return 0


def write_local_config(path: Path, found: dict[str, Any]) -> list[str]:
    """Ajoute les valeurs détectées à config.local.yaml, sans toucher à ce que tu as déjà réglé."""
    existing: dict[str, Any] = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    written: list[str] = []

    def merge(target: dict, source: dict, prefix: str) -> None:
        for key, value in source.items():
            label = f"{prefix}{key}"
            if isinstance(value, dict):
                merge(target.setdefault(key, {}), value, label + ".")
            elif key not in target:
                target[key] = value
                written.append(f"{label} = {value}")

    merge(existing, found, "")
    if written:
        header = "# Réglages propres à ce PC (non versionné). Surcharge config.yaml.\n"
        path.write_text(header + yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return written
