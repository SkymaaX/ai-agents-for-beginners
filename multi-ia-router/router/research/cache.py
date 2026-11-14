"""Cache disque (fichiers JSON) avec durée de vie + limiteur de débit par clé."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any


class DiskCache:
    def __init__(self, directory: Path, ttl_hours: float):
        self.directory = Path(directory)
        self.ttl = ttl_hours * 3600

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.directory / namespace / f"{digest}.json"

    def get(self, namespace: str, key: str) -> Any | None:
        path = self._path(namespace, key)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if self.ttl > 0 and time.time() - record.get("ts", 0) > self.ttl:
            return None
        return record.get("value")

    def set(self, namespace: str, key: str, value: Any) -> None:
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "key": key, "value": value}
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


class RateLimiter:
    """Garantit un intervalle minimal entre deux requêtes portant la même clé (ex. un domaine)."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, min_interval: float | None = None) -> float:
        """Attend si nécessaire. Renvoie le temps attendu (secondes)."""
        interval = self.min_interval if min_interval is None else max(self.min_interval, min_interval)
        with self._lock:
            now = self._clock()
            last = self._last.get(key)
            waited = 0.0
            if last is not None and now - last < interval:
                waited = interval - (now - last)
                self._sleep(waited)
            self._last[key] = self._clock()
            return waited
