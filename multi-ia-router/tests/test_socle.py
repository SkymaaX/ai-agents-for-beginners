"""Module 1 — socle : config, schémas, JSON, état, réduction de contexte."""

from __future__ import annotations

import json

import pytest

from router.config import CAPABILITY_FOR_TYPE, load_config
from router.errors import ConfigError
from router.jsonutil import extract_json, portable_schema
from router.schemas import Critique, Plan, check_plan
from router.state import Project, list_projects, slugify
from router.textutil import fit_blocks, fit_text

from conftest import ROOT


def test_config_loads_and_checks():
    config = load_config(ROOT / "config.yaml", use_local=False)
    assert config.roles.chef == "qwen"
    assert config.critic_name_for("gemini") == "claude"
    assert config.critic_name_for("claude_code") == "gemini"
    assert "comfyui" not in config.enabled_producers()  # désactivé par défaut


def test_nobody_reviews_own_family():
    with pytest.raises(ConfigError, match="propre copie"):
        load_config(ROOT / "config.yaml", use_local=False, overrides={"roles": {"critics": {"claude_code": "claude"}}})


def test_unknown_role_agent_rejected():
    with pytest.raises(ConfigError):
        load_config(ROOT / "config.yaml", use_local=False, overrides={"roles": {"chef": "inconnu"}})


def test_local_override_file(tmp_path):
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text("paths:\n  blender_bin: /opt/blender\nreview:\n  max_rounds: 5\n", encoding="utf-8")
    config = load_config(tmp_path / "config.yaml", root=tmp_path)
    assert config.paths.blender_bin == "/opt/blender"
    assert config.review.max_rounds == 5
    assert config.review.min_score == 4  # le reste n'est pas écrasé


def test_extract_json_tolerates_chatter():
    assert extract_json('Voici :\n```json\n{"a": 1}\n```\nVoilà.') == {"a": 1}
    assert extract_json('{"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}
    with pytest.raises(ValueError):
        extract_json("pas de json ici")


def test_portable_schema_is_flat_and_strict():
    schema = portable_schema(Plan)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["additionalProperties"] is False
    part = schema["properties"]["parts"]["items"]
    assert set(part["required"]) == set(part["properties"])
    assert "title" in part["properties"]  # un champ nommé « title » n'est pas confondu avec le mot-clé
    assert "minimum" not in json.dumps(portable_schema(Critique))
    assert "additionalProperties" not in json.dumps(portable_schema(Plan, strict=False))


def _plan(**part_overrides):
    part = {"id": "p1", "title": "T", "type": "text", "agent": "gemini", "prompt": "…", **part_overrides}
    return Plan.model_validate({"project_title": "X", "parts": [part]})


def test_check_plan_catches_real_problems():
    agents = {"gemini": {"text", "image"}, "claude_code": {"text", "files"}}
    check_plan(_plan(), agents, CAPABILITY_FOR_TYPE)
    with pytest.raises(ValueError, match="indisponible"):
        check_plan(_plan(agent="gpt"), agents, CAPABILITY_FOR_TYPE)
    with pytest.raises(ValueError, match="ne sait pas produire"):
        check_plan(_plan(type="blender"), agents, CAPABILITY_FOR_TYPE)
    with pytest.raises(ValueError, match="dépendance inconnue"):
        check_plan(_plan(depends_on=["p9"]), agents, CAPABILITY_FOR_TYPE)
    cyclic = Plan.model_validate({"project_title": "X", "parts": [
        {"id": "a", "title": "A", "type": "text", "agent": "gemini", "prompt": "…", "depends_on": ["b"]},
        {"id": "b", "title": "B", "type": "text", "agent": "gemini", "prompt": "…", "depends_on": ["a"]},
    ]})
    with pytest.raises(ValueError, match="circulaires"):
        check_plan(cyclic, agents, CAPABILITY_FOR_TYPE)


def test_plan_order_respects_dependencies():
    plan = Plan.model_validate({"project_title": "X", "parts": [
        {"id": "b", "title": "B", "type": "text", "agent": "g", "prompt": "…", "depends_on": ["a"]},
        {"id": "a", "title": "A", "type": "text", "agent": "g", "prompt": "…"},
    ]})
    assert [p.id for p in plan.ordered()] == ["a", "b"]


def test_critique_acceptance_rule():
    good = {"fidelite": 5, "completude": 4, "erreurs": 5, "qualite": 4, "sources": 4}
    assert Critique(scores=good, verdict="accept").accepted(4)
    assert not Critique(scores=good, verdict="revise").accepted(4)
    assert not Critique(scores={**good, "sources": 3}, verdict="accept").accepted(4)
    blocking = [{"critere": "erreurs", "severity": "bloquant", "detail": "d", "correction": "c"}]
    assert not Critique(scores=good, verdict="accept", problems=blocking).accepted(4)
    with pytest.raises(ValueError):
        Critique(scores={**good, "qualite": 9}, verdict="accept")  # note hors grille


def test_project_state_roundtrip(tmp_path):
    project = Project.create(tmp_path, "Écrire un guide : été & hiver !")
    assert "ecrire-un-guide" in project.id
    project.set_stage("intake", "done")
    project.write_json(project.brief_path, {"title": "x"})
    again = Project.load(project.dir)
    assert again.state.stages["intake"].status == "done"
    assert again.state.stages["replan"].status == "skipped"
    assert again.read_json(again.brief_path) == {"title": "x"}
    assert [s.id for s in list_projects(tmp_path)] == [project.id]
    assert not list(project.dir.glob("*.tmp"))  # écriture atomique : pas de fichier temporaire oublié


def test_slugify_handles_accents_and_empty():
    assert slugify("Été à Paris — étude n°2") == "ete-a-paris-etude-n2"
    assert slugify("!!!") == "projet"


def test_fit_text_keeps_headings_and_marks_cuts():
    text = "# A\n" + "x" * 5000 + "\n## B\n" + "y" * 5000 + "\n"
    out = fit_text(text, 2000)
    assert len(out) < 3200 and "# A" in out and "## B" in out and "coupés" in out
    assert fit_text("court", 2000) == "court"
    blocks, cut = fit_blocks([("a", "x" * 9000), ("b", "court")], 3000)
    assert cut and blocks[1][1] == "court"
    assert fit_blocks([("a", "x")], 0) == ([("a", "x")], False)


def test_local_file_can_be_ignored(tmp_path):
    """Les tests chargent la config sans les réglages personnels de la machine."""
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text("mock: true\nreview:\n  max_rounds: 9\n", encoding="utf-8")
    assert load_config(tmp_path / "config.yaml", root=tmp_path).review.max_rounds == 9
    plain = load_config(tmp_path / "config.yaml", root=tmp_path, use_local=False)
    assert plain.review.max_rounds == 3 and plain.mock is False


def test_env_file_is_loaded_only_with_local_settings(tmp_path, monkeypatch):
    import os

    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text("GEMINI_API_KEY=cle-du-fichier-env\n", encoding="utf-8")
    load_config(tmp_path / "config.yaml", root=tmp_path, use_local=False)
    assert "GEMINI_API_KEY" not in os.environ  # une vraie clé ne doit jamais fuir dans les tests
    load_config(tmp_path / "config.yaml", root=tmp_path)
    assert os.environ["GEMINI_API_KEY"] == "cle-du-fichier-env"
    monkeypatch.delenv("GEMINI_API_KEY")
