"""Module 5 — exécutant : lancement de Blender et boucle de correction du script."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from router.agents.base import AgentResult
from router.agents.mock import MockAgent
from router.errors import ConfigError
from router.logs import read_jsonl
from router.pipeline import orchestrator
from router.state import Project
from router.tools import blender

FAKES = Path(__file__).resolve().parent / "fakes"
FAKE_BLENDER = [sys.executable, str(FAKES / "fake_blender.py")]


def test_blender_runs_script_in_workdir(tmp_path):
    script = tmp_path / "scene.py"
    script.write_text("import bpy\n", encoding="utf-8")
    run = blender.run_script(FAKE_BLENDER, script, tmp_path, timeout=30)
    assert run.ok and run.returncode == 0 and "Saved: scene.blend" in run.log
    assert (tmp_path / "scene.blend").exists() and (tmp_path / "render.png").exists()


def test_blender_python_error_detected_despite_exit_code_zero(tmp_path):
    script = tmp_path / "scene.py"
    script.write_text("import bpy\nERREUR_BPY\n", encoding="utf-8")
    run = blender.run_script(FAKE_BLENDER, script, tmp_path, timeout=30)
    assert not run.ok and run.returncode == 0 and "AttributeError" in run.tail()


def test_blender_timeout_and_missing_script(tmp_path):
    script = tmp_path / "scene.py"
    script.write_text("BOUCLE\n", encoding="utf-8")
    run = blender.run_script(FAKE_BLENDER, script, tmp_path, timeout=1)
    assert not run.ok and "Délai dépassé" in run.log
    assert not blender.run_script(FAKE_BLENDER, tmp_path / "absent.py", tmp_path).ok


def test_blender_binary_resolution():
    assert blender.resolve(FAKE_BLENDER) == FAKE_BLENDER
    with pytest.raises(ConfigError, match="Blender introuvable"):
        blender.resolve("blender-qui-n-existe-pas")
    assert blender.version(FAKE_BLENDER).startswith("Blender 4.2.0")
    assert blender.version("blender-qui-n-existe-pas") == ""


BLENDER_PLAN = {
    "project_title": "Scène 3D",
    "summary": "Une seule partie Blender.",
    "research_queries": {"vault": [], "web": []},
    "parts": [{
        "id": "p1", "title": "Scène cube", "type": "blender", "agent": "claude_code", "depends_on": [],
        "prompt": "Crée une scène avec un cube.", "success_criteria": ["scene.blend produit"],
        "expected_output": {"format": "blend", "filename": "scene.blend"},
    }],
    "assembly_instructions": "",
}


def test_blender_part_fix_loop_end_to_end(make_config, monkeypatch):
    """Script cassé → Blender plante → l'erreur repart à l'auteur → script corrigé → succès."""
    config = make_config(
        mock_scenario={"critique": "accept", "real_blender": True},
        paths={"blender_bin": FAKE_BLENDER},
    )
    monkeypatch.setattr(MockAgent, "_json_plan", lambda self, task: BLENDER_PLAN)
    fixes: list[str] = []

    def files(self, task):
        script = task.workdir / "scene.py"
        if task.meta.get("blender_fix"):
            fixes.append(task.prompt)
            script.write_text("import bpy\n# corrigé\n", encoding="utf-8")
            return "Corrigé : attribut inexistant remplacé.", [script]
        script.write_text("import bpy\nERREUR_BPY\n", encoding="utf-8")
        return "Script écrit.", [script]

    monkeypatch.setattr(MockAgent, "_files", files)
    state = orchestrator.start(config, "Fais une scène 3D avec un cube")
    project = Project.load(config.projects_dir / state.id)

    assert state.status == "done", state.error
    assert len(fixes) == 1 and "AttributeError" in fixes[0]  # l'auteur reçoit la vraie sortie de Blender
    report = project.read_text(project.version_path("p1", 1))
    assert "Exécution réussie après 2 tentative(s)" in report
    files_dir = project.version_files_dir("p1", 1)
    assert (files_dir / "scene.blend").exists() and (files_dir / "render.png").exists() and (files_dir / "scene.py").exists()
    assert (project.assets_dir / "p1" / "scene.blend").exists()
    assert not (project.assets_dir / "p1" / "blender.log").exists()  # le journal interne ne part pas au client
    events = [e for e in read_jsonl(project.events_path) if e["type"] == "blender_result"]
    assert [e["ok"] for e in events] == [False, True]
    # le relecteur voit le rendu : image jointe à la critique
    critic_call = [r for r in read_jsonl(project.calls_path) if r["role"] == "critique"][0]
    assert critic_call["agent"] == "gemini" and "scene.py" in critic_call["prompt"]


def test_blender_part_reports_failure_when_never_fixed(make_config, monkeypatch):
    config = make_config(
        mock_scenario={"critique": "accept", "real_blender": True},
        paths={"blender_bin": FAKE_BLENDER},
        blender={"max_fix_attempts": 2},
    )
    monkeypatch.setattr(MockAgent, "_json_plan", lambda self, task: BLENDER_PLAN)

    def files(self, task):
        script = task.workdir / "scene.py"
        script.write_text("import bpy\nERREUR_BPY\n", encoding="utf-8")
        return "Script écrit.", [script]

    monkeypatch.setattr(MockAgent, "_files", files)
    state = orchestrator.start(config, "Scène 3D")
    project = Project.load(config.projects_dir / state.id)
    report = project.read_text(project.version_path("p1", 1))
    assert "EN ÉCHEC après 2 tentative(s)" in report and "AttributeError" in report  # l'échec est écrit noir sur blanc


def test_missing_blender_fails_with_actionable_message(make_config, monkeypatch):
    config = make_config(
        mock_scenario={"critique": "accept", "real_blender": True},
        paths={"blender_bin": "blender-qui-n-existe-pas"},
    )
    monkeypatch.setattr(MockAgent, "_json_plan", lambda self, task: BLENDER_PLAN)
    state = orchestrator.start(config, "Scène 3D")
    assert state.status == "failed" and "Blender introuvable" in state.error and "doctor" in state.error
