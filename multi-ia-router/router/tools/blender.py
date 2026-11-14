"""Exécution d'un script bpy : `blender --background --python script.py`.

Le routeur lance Blender lui-même (délai, journal, code de retour) : l'agent exécutant écrit
le script, il ne lance rien.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigError

# Blender renvoie souvent 0 même quand le script Python a planté : on lit aussi la sortie.
_ERROR_MARKERS = ("Traceback (most recent call last)", "Error: Python:", "SyntaxError:")


@dataclass
class BlenderRun:
    ok: bool
    returncode: int
    log: str
    duration: float

    def tail(self, chars: int = 3000) -> str:
        return self.log[-chars:] if len(self.log) > chars else self.log


def resolve(binary: str | list[str]) -> list[str]:
    """Commande Blender sous forme de liste. Accepte un chemin, un nom, ou une liste (tests)."""
    if isinstance(binary, (list, tuple)):
        return [str(part) for part in binary]
    found = shutil.which(binary) or (binary if Path(binary).exists() else None)
    if not found:
        raise ConfigError(
            f"Blender introuvable (« {binary} »). Renseigne paths.blender_bin dans config.local.yaml "
            "ou lance `python -m router doctor`."
        )
    return [found]


def run_script(binary: str | list[str], script: Path, workdir: Path, *, timeout: float = 900.0) -> BlenderRun:
    """Lance le script dans Blender, dossier courant = `workdir`."""
    started = time.monotonic()
    if not script.exists():
        return BlenderRun(False, -1, f"Script introuvable : {script.name}", 0.0)
    command = resolve(binary) + ["--background", "--python", str(script.resolve())]
    try:
        proc = subprocess.run(
            command, cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        log = f"{partial}\nDélai dépassé : Blender arrêté après {timeout:.0f} s."
        code = -9
    except OSError as exc:
        log, code = f"Impossible de lancer Blender : {exc}", -1
    failed = code != 0 or any(marker in log for marker in _ERROR_MARKERS)
    return BlenderRun(not failed, code, log.strip(), round(time.monotonic() - started, 2))


def version(binary: str | list[str]) -> str:
    """Première ligne de `blender --version`, ou chaîne vide."""
    try:
        proc = subprocess.run(
            resolve(binary) + ["--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (ConfigError, OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip().splitlines()[0] if proc.stdout else ""
