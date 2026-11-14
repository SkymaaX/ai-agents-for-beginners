"""Module 2 — pipeline complet avec agents simulés : critique croisée, arbitrage, renvoi,
pannes, reprise, modification ciblée."""

from __future__ import annotations

import json

import pytest

from router.errors import ConfigError
from router.logs import read_jsonl
from router.pipeline import orchestrator
from router.state import Project


def load(config, state) -> Project:
    return Project.load(config.projects_dir / state.id)


def calls(project: Project) -> list[dict]:
    return read_jsonl(project.calls_path)


def test_full_pipeline_end_to_end(make_config):
    config = make_config()
    state = orchestrator.start(config, "Guide du potager sur balcon avec script et illustration")
    project = load(config, state)

    assert state.status == "done"
    assert all(state.stages[s].status == "done" for s in ("intake", "plan", "research", "produce", "review", "assemble", "validate", "deliver"))
    assert state.stages["replan"].status == "skipped"
    for name in ("demande.md", "00_brief.json", "01_plan.json", "02_recherche/recherche.md", "02_recherche/sources.json",
                 "04_livrable/livrable_v1.md", "04_livrable/LIVRABLE.md", "05_validation/verdict_v1.json",
                 "logs/calls.jsonl", "events.jsonl"):
        assert (project.dir / name).exists(), name

    plan = project.load_plan()
    assert {p.type for p in plan.parts} == {"text", "code", "image"}
    for part in plan.parts:
        part_state = state.parts[part.id]
        assert part_state.status == "accepted" and part_state.accepted_version == 2  # 1er tour à revoir, 2e accepté
        assert project.version_path(part.id, 1).exists() and project.version_path(part.id, 2).exists()
    # les fichiers produits arrivent dans le livrable
    assert (project.assets_dir / "p3" / "solution.py").exists()
    assert (project.assets_dir / "p4" / "couverture.png").read_bytes().startswith(b"\x89PNG")
    final = project.read_text(project.final_path)
    assert "assets/p4/couverture.png" in final


def test_nobody_reviews_own_copy(make_config):
    config = make_config()
    project = load(config, orchestrator.start(config, "Demande de test"))
    plan = project.load_plan()
    critiques = [json.loads(p.read_text(encoding="utf-8")) for p in project.dir.glob("03_parties/*/critique_v*.json")]
    assert critiques
    for critique in critiques:
        author, critic = critique["author"], critique["critic"]
        assert author == plan.part(critique["part_id"]).agent
        assert config.family(author) != config.family(critic)
    by_author = {c["author"]: c["critic"] for c in critiques}
    assert by_author["gemini"] == "claude" and by_author["claude"] == "gemini" and by_author["claude_code"] == "gemini"


def test_call_log_is_complete(make_config):
    config = make_config()
    project = load(config, orchestrator.start(config, "Demande de test"))
    records = calls(project)
    assert records
    for record in records:
        for key in ("ts", "agent", "role", "stage", "prompt", "response", "tokens_in", "tokens_out", "duration_s", "attempt", "ok"):
            assert key in record, key
        assert record["prompt"] and record["ok"]
    roles = [r["role"] for r in records]
    assert roles[0] == "chef.reformulation" and roles[1] == "repartiteur.plan"
    assert roles[-1] == "chef.validation" and roles[-2] == "assembleur"
    # recherche faite une seule fois, avant toute production
    research = [i for i, r in enumerate(roles) if r == "chercheur"]
    assert research and max(research) < roles.index("producteur")


def test_arbitration_after_max_rounds(make_config):
    config = make_config(mock_scenario={"critique": "always_revise"})
    project = load(config, orchestrator.start(config, "Demande de test"))
    state = project.state
    assert state.status == "done"
    for part_id, part_state in state.parts.items():
        assert part_state.status == "arbitrated"
        assert part_state.version == 3 and part_state.rounds == 3  # 3 tours, pas un de plus
        assert project.critique_path(part_id, 3).exists() and not project.version_path(part_id, 4).exists()
        arbitration = project.read_json(project.arbitration_path(part_id))
        assert arbitration["arbiter"] == "qwen" and arbitration["chosen_version"] in (1, 2, 3)
        assert part_state.reserves
    assert "## Réserves" in project.read_text(project.final_path)


def test_immediate_accept_keeps_v1(make_config):
    config = make_config(mock_scenario={"critique": "accept"})
    project = load(config, orchestrator.start(config, "Demande de test"))
    assert all(p.accepted_version == 1 and p.rounds == 1 for p in project.state.parts.values())
    assert not [r for r in calls(project) if r["role"] == "auteur.revision"]


def test_validation_return_reworks_only_the_faulty_part(make_config):
    config = make_config(mock_scenario={"critique": "accept", "verdict": "fail_once"})
    project = load(config, orchestrator.start(config, "Demande de test"))
    state = project.state
    assert state.status == "done" and state.validation_returns == 1 and state.assembly_version == 2
    assert project.read_json(project.verdict_path(1))["ok"] is False
    assert project.read_json(project.verdict_path(2))["ok"] is True
    versions = {pid: p.version for pid, p in state.parts.items()}
    assert versions["p1"] == 2 and all(v == 1 for pid, v in versions.items() if pid != "p1")
    assert state.deliverable_version == 2


def test_validation_gives_up_with_reserves(make_config):
    config = make_config(mock_scenario={"critique": "accept", "verdict": "always_fail"})
    project = load(config, orchestrator.start(config, "Demande de test"))
    state = project.state
    assert state.status == "done" and state.validation_returns == config.validation.max_returns
    assert state.reserves and "## Réserves" in project.read_text(project.final_path)


def test_transient_failure_is_retried(make_config):
    config = make_config(mock_scenario={"critique": "accept", "fail": {"role": "assembleur", "times": 1, "kind": "retryable"}})
    project = load(config, orchestrator.start(config, "Demande de test"))
    assert project.state.status == "done"
    attempts = [(r["attempt"], r["ok"]) for r in calls(project) if r["role"] == "assembleur"]
    assert attempts == [(1, False), (2, True)]


def test_fatal_failure_then_resume_skips_done_work(make_config):
    config = make_config(mock_scenario={"critique": "accept", "fail": {"role": "assembleur", "times": 1, "kind": "fatal"}})
    state = orchestrator.start(config, "Demande de test")
    assert state.status == "failed" and state.stages["assemble"].status == "error" and "panne simulée" in state.error
    project = load(config, state)
    before = len(calls(project))

    healthy = make_config(mock_scenario={"critique": "accept"})
    resumed = orchestrator.resume(healthy, state.id)
    assert resumed.status == "done" and resumed.error == ""
    after = calls(load(healthy, resumed))[before:]
    assert [r["role"] for r in after] == ["assembleur", "chef.validation"]  # rien n'est refait en amont


def test_quota_pauses_then_resume(make_config):
    config = make_config(mock_scenario={"fail": {"role": "critique", "times": 1, "kind": "quota"}})
    state = orchestrator.start(config, "Demande de test")
    assert state.status == "paused" and "quota" in state.error.lower()
    resumed = orchestrator.resume(make_config(), state.id)
    assert resumed.status == "done"


def test_invalid_json_is_repaired(make_config):
    config = make_config(mock_scenario={"critique": "accept", "invalid_json": {"role": "repartiteur.plan", "times": 1}})
    project = load(config, orchestrator.start(config, "Demande de test"))
    assert project.state.status == "done"
    plans = [r for r in calls(project) if r["role"] == "repartiteur.plan"]
    assert len(plans) == 2 and "rejetée" in plans[1]["prompt"]


def test_bad_plan_is_sent_back_with_reason(make_config):
    config = make_config(mock_scenario={"critique": "accept", "bad_plan_once": True})
    project = load(config, orchestrator.start(config, "Demande de test"))
    assert project.state.status == "done"
    plans = [r for r in calls(project) if r["role"] == "repartiteur.plan"]
    assert len(plans) == 2 and "agent_inexistant" in plans[1]["prompt"] and "indisponible" in plans[1]["prompt"]


def test_json_never_valid_fails_cleanly(make_config):
    config = make_config(mock_scenario={"invalid_json": {"role": "chef.reformulation", "times": 99}})
    state = orchestrator.start(config, "Demande de test")
    assert state.status == "failed" and "JSON" in state.error and state.stages["intake"].status == "error"


def test_revise_touches_only_affected_parts(make_config):
    config = make_config(mock_scenario={"critique": "accept"})
    state = orchestrator.start(config, "Demande de test")
    project = load(config, state)
    before = len(calls(project))
    first_research = project.read_text(project.research_path)

    revised = orchestrator.revise(config, state.id, "Ajoute une section sur l'arrosage")
    project = load(config, revised)
    assert revised.status == "done" and revised.modifications[0].status == "done"
    assert revised.stages["replan"].status == "done"
    assert revised.parts["p1"].version == 2 and revised.parts["p1"].accepted_version == 2
    assert all(p.version == 1 for pid, p in revised.parts.items() if pid != "p1")  # le reste est réutilisé tel quel
    assert revised.assembly_version == 2 and revised.deliverable_version == 2
    assert project.modification_path(1).exists() and project.assembly_path(1).exists()
    assert project.read_text(project.research_path) == first_research  # pas de nouvelle recherche

    roles = [r["role"] for r in calls(project)[before:]]
    assert roles == ["repartiteur.modification", "auteur.revision", "critique", "assembleur", "chef.validation"]
    validation_prompt = calls(project)[-1]["prompt"]
    assert "arrosage" in validation_prompt  # le chef valide contre demande + modification


def test_revise_requires_finished_project(make_config):
    config = make_config(mock_scenario={"fail": {"role": "assembleur", "times": 1, "kind": "fatal"}})
    state = orchestrator.start(config, "Demande de test")
    with pytest.raises(ConfigError, match="pas terminé"):
        orchestrator.revise(config, state.id, "Change le ton")


def test_mock_project_stays_mock_on_resume(make_config, monkeypatch):
    config = make_config(mock_scenario={"fail": {"role": "assembleur", "times": 1, "kind": "fatal"}})
    state = orchestrator.start(config, "Demande de test")
    real = make_config(mock=False)
    resumed = orchestrator.resume(real, state.id)  # sans ça, des appels réels partiraient
    assert resumed.status == "done"
    assert all(r["model"].startswith("simulation:") for r in calls(load(real, resumed)) if r["ok"])


def test_find_project_by_fragment_title_or_latest(make_config):
    config = make_config(mock_scenario={"critique": "accept"})
    first = orchestrator.start(config, "Recette de tarte aux pommes")
    long_request = "Prépare un guide pratique et très complet pour installer chez soi un potager sur balcon"
    second = orchestrator.start(config, long_request)
    assert "potager" not in second.id  # l'identifiant est tronqué : le mot n'y figure pas
    assert orchestrator.find_project(config, "tarte").id == first.id        # fragment de l'identifiant
    assert orchestrator.find_project(config, "Potager").id == second.id     # mot de la demande, casse ignorée
    assert orchestrator.find_project(config, second.id).id == second.id     # identifiant exact
    assert orchestrator.find_project(config, "dernier").id == second.id     # le plus récent
    with pytest.raises(ConfigError, match="introuvable"):
        orchestrator.find_project(config, "introuvable")
    with pytest.raises(ConfigError, match="plusieurs projets"):
        orchestrator.find_project(config, "2")  # ambigu : présent dans les deux identifiants


def test_small_context_agent_gets_reduced_documents(make_config):
    config = make_config(mock_scenario={"critique": "accept"}, agents={"qwen": {"max_context_chars": 600}})
    project = load(config, orchestrator.start(config, "Demande de test " + "très détaillée " * 20))
    validation = [r for r in calls(project) if r["role"] == "chef.validation"][0]
    assert "raccourcis" in validation["prompt"]  # le modèle est prévenu que le contexte est coupé
    events = read_jsonl(project.events_path)
    assert any(e.get("context_cut") for e in events if e["type"] == "agent_start")
