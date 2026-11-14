"""File d'attente VRAM : un seul groupe d'agents locaux sur la carte graphique à la fois.

Qwen (groupe "llm") et ComfyUI (groupe "diffusion") partagent la VRAM. En mode exclusif,
quand le groupe change, l'occupant précédent est déchargé avant de laisser passer le suivant.
Les agents distants (Gemini, Claude) n'ont pas de groupe et passent sans attendre.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from ..logs import logger


class GpuArbiter:
    def __init__(self, exclusive: bool = True, settle_seconds: float = 0.0, on_event=None):
        self.exclusive = exclusive
        self.settle_seconds = settle_seconds
        self._on_event = on_event
        self._lock = threading.RLock()
        self._owner: Any = None

    @property
    def owner_group(self) -> str:
        return getattr(self._owner, "gpu_group", "") if self._owner is not None else ""

    @contextmanager
    def use(self, agent: Any) -> Iterator[None]:
        group = getattr(agent, "gpu_group", "")
        if not group:
            yield
            return
        with self._lock:  # un seul travail GPU à la fois : les autres attendent leur tour
            previous = self._owner
            if self.exclusive and previous is not None and previous.gpu_group != group:
                logger.info("VRAM : déchargement de %s avant %s", previous.name, agent.name)
                if self._on_event:
                    self._on_event("gpu_switch", unload=previous.name, load=agent.name)
                try:
                    previous.unload()
                except Exception as exc:  # un déchargement raté ne doit pas tuer le pipeline
                    logger.warning("VRAM : déchargement de %s impossible (%s)", previous.name, exc)
                if self.settle_seconds:
                    time.sleep(self.settle_seconds)
            self._owner = agent
            yield
