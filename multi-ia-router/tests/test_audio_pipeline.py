"""Partie « audio » dans le pipeline : la voix lit le texte d'une autre partie, relu par un autre agent."""

from __future__ import annotations

import json
import wave

from router.logs import read_jsonl
from router.pipeline import orchestrator
from router.pipeline.parts import speakable
from router.state import Project


def run(make_config, **scenario):
    config = make_config(mock_scenario={"critique": "accept", **scenario}, agents={"voix": {"enabled": True}})
    state = orchestrator.start(config, "Guide du compost avec une narration audio")
    return config, Project.load(config.projects_dir / state.id)


def test_voice_agent_is_off_until_enabled(make_config):
    config = make_config(mock_scenario={"critique": "accept"})
    state = orchestrator.start(config, "Guide du compost")
    plan = Project.load(config.projects_dir / state.id).load_plan()
    assert "audio" not in {p.type for p in plan.parts}  # désactivé par défaut : aucun changement pour l'existant


def test_audio_part_reads_the_text_of_its_dependency(make_config):
    config, project = run(make_config)
    assert project.state.status == "done", project.state.error
    plan = project.load_plan()
    audio = plan.part("p5")
    assert audio.type == "audio" and audio.agent == "voix" and audio.depends_on == ["p1"]

    report = project.read_text(project.version_path("p5", 1))
    assert "Narration générée par voix" in report and "durée" in report and "Texte lu" in report
    assert "[S" not in report.split("Texte lu :")[1]  # renvois de sources retirés du texte lu
    wav_path = project.version_files_dir("p5", 1) / "narration.wav"
    with wave.open(str(wav_path), "rb") as handle:  # vrai fichier WAV lisible
        assert handle.getnframes() > 0 and handle.getframerate() == 16000
    assert (project.assets_dir / "p5" / "narration.wav").exists()
    assert "assets/p5/narration.wav" in project.read_text(project.final_path)


def test_narration_is_reviewed_by_another_family_that_knows_it_cannot_listen(make_config):
    config, project = run(make_config)
    critique = project.read_json(project.critique_path("p5", 1))
    assert critique["author"] == "voix" and critique["critic"] == "claude"
    assert config.family("voix") != config.family(critique["critic"])
    prompt = next(r["prompt"] for r in read_jsonl(project.calls_path) if r["role"] == "critique" and r["part_id"] == "p5")
    assert "tu ne peux pas l'écouter" in prompt and "narration.wav" in prompt


def test_audio_part_is_produced_after_the_text_it_reads(make_config):
    _, project = run(make_config)
    produced = [r["part_id"] for r in read_jsonl(project.calls_path) if r["role"] == "producteur"]
    assert produced.index("p1") < produced.index("p5")


def test_revising_the_script_regenerates_the_narration(make_config):
    """Sans ça, la voix continuerait de lire l'ancien texte après une modification."""
    config, project = run(make_config)
    first_text = project.read_text(project.version_path("p5", 1)).split("Texte lu :")[1]
    revised = orchestrator.revise(config, project.id, "Ajoute une section sur l'arrosage")
    project = Project.load(config.projects_dir / revised.id)
    assert revised.status == "done" and revised.parts["p1"].version == 2
    assert revised.parts["p5"].version == 2 and revised.parts["p5"].accepted_version == 2  # narration refaite
    second = project.read_text(project.version_path("p5", 2))
    assert "Version régénérée" in second and second.split("Texte lu :")[1] != first_text
    plan_event = [e for e in read_jsonl(project.events_path) if e["type"] == "replan"][0]
    assert plan_event["changed"] == ["p1", "p5"]
    assert (project.version_files_dir("p5", 2) / "narration.wav").exists()
    assert revised.parts["p2"].version == 1  # le reste n'est pas touché


def test_audio_part_with_nothing_to_read_is_refused_at_planning_and_at_production(make_config):
    import pytest

    from router.agents.base import AgentError
    from router.config import CAPABILITY_FOR_TYPE
    from router.pipeline.parts import generate
    from router.runtime import Runtime
    from router.schemas import Plan, check_plan

    empty = {"id": "p1", "title": "Voix", "type": "audio", "agent": "voix", "prompt": "   "}
    plan = Plan.model_validate({"project_title": "X", "parts": [empty]})
    with pytest.raises(ValueError, match="ne peut pas être vide"):  # le répartiteur peut corriger son plan
        check_plan(plan, {"voix": {"audio"}}, CAPABILITY_FOR_TYPE)
    ok = Plan.model_validate({"project_title": "X", "parts": [
        {"id": "p0", "title": "Script", "type": "text", "agent": "gemini", "prompt": "écris"},
        {**empty, "depends_on": ["p0"]},
    ]})
    check_plan(ok, {"voix": {"audio"}, "gemini": {"text"}}, CAPABILITY_FOR_TYPE)

    # Garde-fou à la production si un plan incohérent passait quand même (prompt = renvoi de source seul).
    config = make_config(mock_scenario={"critique": "accept"}, agents={"voix": {"enabled": True}})
    project = Project.create(config.projects_dir, "x", mock=True)
    project.write_json(project.brief_path, {"title": "t", "objective": "o"})
    rt = Runtime(config, project)
    part = Plan.model_validate({"project_title": "X", "parts": [{**empty, "prompt": "[S1]"}]}).parts[0]
    project.state.parts["p1"] = __import__("router.state", fromlist=["PartState"]).PartState(author="voix")
    with pytest.raises(AgentError, match="aucun texte à lire") as info:
        generate(rt, Plan.model_validate({"project_title": "X", "parts": [part]}), part, 1)
    assert not info.value.retryable


def test_speakable_cleans_markdown_for_speech():
    text = "### Guide (version 2)\n\nBonjour **à tous**, voici [le guide](http://x.y) [S3] et [S1, S2].\n\n- un\n- deux\n```py\nprint(1)\n```\n![img](a.png)"
    spoken = speakable(text)
    assert spoken == "Bonjour à tous, voici le guide et.\nun\ndeux"
    assert "S3" not in spoken and "print" not in spoken and "http" not in spoken and "#" not in spoken
