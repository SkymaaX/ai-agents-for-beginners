"""Outils communs des tests : config de simulation isolée dans un dossier temporaire."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router.config import load_config  # noqa: E402


@pytest.fixture
def make_config(tmp_path):
    """Fabrique une config en simulation, projets et cache dans un dossier temporaire."""

    def build(**overrides):
        base = {
            "mock": True,
            "paths": {"projects_dir": str(tmp_path / "projects"), "cache_dir": str(tmp_path / "cache")},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = {**base[key], **value}
            else:
                base[key] = value
        return load_config(ROOT / "config.yaml", overrides=base)

    return build


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Les attentes entre deux essais ne doivent pas ralentir les tests."""
    monkeypatch.setattr("router.agents.base.time.sleep", lambda seconds: None)
