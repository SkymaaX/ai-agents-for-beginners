"""Journaux : un fichier d'appels détaillé et un fil d'événements par projet.

- logs/calls.jsonl : agent, prompt envoyé, réponse, tokens, durée, tentative, erreur.
- events.jsonl     : ce qui se passe, lu en direct par le tableau de bord.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("router")
_lock = threading.Lock()


def setup_logging(log_dir: Path | None = None, verbose: bool = False) -> None:
    """Console + fichier logs/router.log (tous projets confondus)."""
    if logger.handlers:
        return
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "router.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        logger.addHandler(handler)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read_jsonl(path: Path, start: int = 0) -> list[dict[str, Any]]:
    """Lit les lignes à partir de l'index `start` (les lignes incomplètes sont ignorées)."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index < start or not line.endswith("\n"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def stamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")
