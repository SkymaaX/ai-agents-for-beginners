"""`doctor` : détection tolérante, aucune clé affichée, config locale jamais écrasée."""

from __future__ import annotations

import json
import sys

import pytest
import yaml

from router import doctor
from router.config import load_config

from conftest import ROOT


def test_doctor_runs_without_any_service_and_hides_keys(make_config, monkeypatch, capsys):
    monkeypatch.setenv("ANYTHINGLLM_API_KEY", "secret-xyz-123")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(doctor, "_get", lambda url, timeout=3.0, headers=None: None)  # rien ne répond
    monkeypatch.setattr(doctor, "detect_gpus", lambda: [{"name": "RTX 4070", "vram_gb": 12.0, "reliable": True}])
    assert doctor.run_doctor(make_config(mock=False)) == 0
    out = capsys.readouterr().out
    assert "secret-xyz-123" not in out and "ANYTHINGLLM_API_KEY : présente" in out
    assert "GEMINI_API_KEY : absente" in out and "Renseigner GEMINI_API_KEY" in out
    assert "RTX 4070 — 12.0 Go VRAM" in out and "gpu.exclusive conseillé : true" in out
    assert "personne ne critique sa propre famille" in out


def test_write_local_config_never_overwrites_user_values(tmp_path):
    path = tmp_path / "config.local.yaml"
    path.write_text("paths:\n  blender_bin: /mon/blender\n", encoding="utf-8")
    found = {"paths": {"blender_bin": "/usr/bin/blender", "vault_dir": "/home/moi/vault"}, "gpu": {"llm_backend": "ollama"}}
    written = doctor.write_local_config(path, found)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["paths"] == {"blender_bin": "/mon/blender", "vault_dir": "/home/moi/vault"}
    assert data["gpu"]["llm_backend"] == "ollama"
    assert written == ["paths.vault_dir = /home/moi/vault", "gpu.llm_backend = ollama"]
    assert doctor.write_local_config(path, found) == []  # deuxième passage : plus rien à écrire


def test_written_local_config_is_loaded(tmp_path):
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    doctor.write_local_config(tmp_path / "config.local.yaml", {"paths": {"blender_bin": "/opt/blender"}, "gpu": {"exclusive": False}})
    config = load_config(tmp_path / "config.yaml", root=tmp_path)
    assert config.paths.blender_bin == "/opt/blender" and config.gpu.exclusive is False


def test_obsidian_vaults_read_from_obsidian_json(tmp_path, monkeypatch):
    vault = tmp_path / "MaVault"
    vault.mkdir()
    settings = tmp_path / ".config" / "obsidian"
    settings.mkdir(parents=True)
    (settings / "obsidian.json").write_text(json.dumps({"vaults": {
        "a1": {"path": str(vault), "ts": 1}, "b2": {"path": str(tmp_path / "supprimee"), "ts": 2},
    }}), encoding="utf-8")
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "nulle-part"))
    assert doctor.detect_obsidian_vaults() == [str(vault)]  # la vault disparue du disque est ignorée


def test_gpu_detection_parses_nvidia_smi(monkeypatch):
    monkeypatch.setattr(doctor, "_run", lambda command, timeout=15.0: "NVIDIA GeForce RTX 4090, 24564\nNVIDIA GeForce RTX 3060, 12288")
    assert doctor.detect_gpus() == [
        {"name": "NVIDIA GeForce RTX 4090", "vram_gb": 24.0, "reliable": True},
        {"name": "NVIDIA GeForce RTX 3060", "vram_gb": 12.0, "reliable": True},
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="lanceur shell POSIX")
def test_blender_detection_uses_configured_path(tmp_path):
    from pathlib import Path

    fake = Path(__file__).resolve().parent / "fakes" / "fake_blender.py"
    launcher = tmp_path / "blender"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    path, version = doctor.detect_blender(str(launcher))
    assert path == str(launcher) and version.startswith("Blender 4.2.0")
    assert doctor.detect_blender("blender-qui-n-existe-pas-xyz")[0] in ("", "/usr/bin/blender", "/snap/bin/blender")
