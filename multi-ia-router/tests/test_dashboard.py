"""Module 7 — tableau de bord : API, rendu sûr du Markdown, lancement, reprise, modification."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from router.dashboard.app import create_app, render_markdown
from router.pipeline import orchestrator

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def client(make_config):
    config = make_config(mock_scenario={"critique": "accept"})
    with TestClient(create_app(config), base_url=BASE) as test_client:
        test_client.config = config
        yield test_client


def wait_done(client, project_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(f"/api/projects/{project_id}").json()
        if not detail["running"] and detail["state"]["status"] in ("done", "failed", "paused"):
            return detail
        time.sleep(0.05)
    raise AssertionError("pipeline non terminé")


def test_page_and_static_assets(client):
    page = client.get("/")
    assert page.status_code == 200 and "Routeur multi-IA" in page.text
    assert "http://" not in page.text and "https://" not in page.text  # aucune ressource externe : 100 % local
    for name in ("app.js", "app.css"):
        assert client.get(f"/static/{name}").status_code == 200
    assert client.get("/static/../app.py").status_code == 404


def test_meta_describes_team_and_rules(client):
    meta = client.get("/api/meta").json()
    team = {a["name"]: a for a in meta["team"]}
    assert team["qwen"]["roles"] == ["Chef de projet"]
    assert "Répartiteur" in team["gemini"]["roles"] and "Critique" in team["gemini"]["roles"]
    assert "Assembleur" in team["claude"]["roles"] and "Exécutant" in team["claude_code"]["roles"]
    assert team["comfyui"]["enabled"] is False
    assert set(meta["grid"]) == {"fidelite", "completude", "erreurs", "qualite", "sources"}
    assert meta["review"] == {"max_rounds": 3, "min_score": 4}


def test_launch_from_dashboard_and_read_everything(client):
    assert client.get("/api/projects").json() == []
    created = client.post("/api/projects", json={"request": "Guide du potager sur balcon", "mock": True})
    assert created.status_code == 200
    project_id = created.json()["id"]
    detail = wait_done(client, project_id)

    assert detail["state"]["status"] == "done" and not detail["stale"]
    assert [s["id"] for s in detail["stages"]] == ["intake", "plan", "research", "produce", "review", "assemble", "validate", "deliver"]
    assert all(s["status"] == "done" for s in detail["stages"])
    assert detail["brief"]["objective"] and detail["plan"]["summary"]
    part = detail["parts"][0]
    assert part["critic"] != part["agent"] and part["versions"][0]["critique"]["verdict"] == "accept"
    image_part = next(p for p in detail["parts"] if p["type"] == "image")
    image = image_part["versions"][0]["files"][0]
    assert image["image"] and client.get(image["url"]).content.startswith(b"\x89PNG")
    assert detail["totals"]["calls"] > 5 and "gemini" in detail["totals"]["by_agent"]

    version = client.get(f"/api/projects/{project_id}/parts/{part['id']}/versions/1").json()
    assert "<h3>" in version["html"]
    research = client.get(f"/api/projects/{project_id}/research").json()
    assert research["sources"] and "Dossier de recherche" in research["html"]
    final = client.get(f"/api/projects/{project_id}/deliverable").json()
    assert f"/files/{project_id}/04_livrable/assets/" in final["html"]  # liens relatifs → fichiers du projet
    assert client.get(final["download"]).status_code == 200
    assert client.get(f"/api/projects/{project_id}/deliverable?version=1").status_code == 200
    assert client.get(f"/api/projects/{project_id}/deliverable?version=9").status_code == 404

    calls = client.get(f"/api/projects/{project_id}/calls").json()
    assert "prompt" not in calls[0] and calls[0]["role"] == "chef.reformulation"
    full = client.get(f"/api/projects/{project_id}/calls/0").json()
    assert full["prompt"] and full["response"]
    assert client.get(f"/api/projects/{project_id}/calls/999").status_code == 404
    listing = client.get("/api/projects").json()
    assert listing[0]["id"] == project_id and listing[0]["status"] == "done"


def test_cli_project_is_visible_in_dashboard(client):
    """Un projet lancé hors du tableau de bord s'y lit de la même façon."""
    state = orchestrator.start(client.config, "Projet lancé en ligne de commande")
    detail = client.get(f"/api/projects/{state.id}").json()
    assert detail["state"]["status"] == "done" and detail["final"] and detail["deliverables"][0]["verdict"]["ok"]


def test_revise_from_dashboard(client):
    project_id = client.post("/api/projects", json={"request": "Guide du compost"}).json()["id"]
    wait_done(client, project_id)
    answer = client.post(f"/api/projects/{project_id}/revise", json={"instruction": "Ajoute un glossaire"})
    assert answer.status_code == 200
    detail = wait_done(client, project_id)
    assert detail["state"]["status"] == "done" and len(detail["deliverables"]) == 2
    modification = detail["modifications"][0]
    assert modification["status"] == "done" and modification["plan"]["changes"][0]["part_id"] == "p1"
    assert any(s["id"] == "replan" for s in detail["stages"])
    assert client.post(f"/api/projects/{project_id}/revise", json={"instruction": "  "}).status_code == 400


def test_resume_failed_project_from_dashboard(make_config):
    broken = make_config(mock_scenario={"critique": "accept", "fail": {"role": "assembleur", "times": 1, "kind": "fatal"}})
    state = orchestrator.start(broken, "Projet qui casse à l'assemblage")
    assert state.status == "failed"
    with TestClient(create_app(make_config(mock_scenario={"critique": "accept"})), base_url=BASE) as client:
        assert client.post(f"/api/projects/{state.id}/revise", json={"instruction": "x"}).status_code == 400  # pas terminé
        assert client.post(f"/api/projects/{state.id}/resume").status_code == 200
        assert wait_done(client, state.id)["state"]["status"] == "done"
        assert client.post(f"/api/projects/{state.id}/resume").status_code == 400  # déjà terminé


def test_interrupted_project_is_flagged_resumable(client):
    state = orchestrator.start(client.config, "Projet interrompu")
    path = client.config.projects_dir / state.id / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "running"  # comme après une coupure de courant en plein pipeline
    path.write_text(json.dumps(data), encoding="utf-8")
    detail = client.get(f"/api/projects/{state.id}").json()
    assert detail["stale"] and not detail["running"]
    assert client.get("/api/projects").json()[0]["stale"]


def test_empty_request_and_unknown_project(client):
    assert client.post("/api/projects", json={"request": "   "}).status_code == 400
    assert client.get("/api/projects/inconnu").status_code == 404
    assert client.get("/api/projects/..").status_code == 404


def test_files_cannot_escape_project_folder(client):
    state = orchestrator.start(client.config, "Projet")
    (client.config.projects_dir / "secret.txt").write_text("secret", encoding="utf-8")
    assert client.get(f"/files/{state.id}/demande.md").status_code == 200
    for attempt in ("../secret.txt", "%2e%2e/secret.txt", "04_livrable/../../secret.txt"):
        assert client.get(f"/files/{state.id}/{attempt}").status_code == 404, attempt


def test_event_stream_replays_and_follows(client):
    state = orchestrator.start(client.config, "Projet")
    app = client.app
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/projects/{project_id}/events")

    class OneShot:
        """Requête factice : se déclare déconnectée après la première passe."""

        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    import asyncio

    async def collect(since):
        response = await route.endpoint(project_id=state.id, request=OneShot(), since=since)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(collect(0))
    events = [json.loads(c.split("data: ", 1)[1]) for c in chunks if "data: " in c]
    assert events[0]["type"] == "pipeline_start" and events[-1]["type"] == "pipeline_done"
    assert {"stage_start", "agent_start", "agent_end", "part_update", "verdict", "delivered"} <= {e["type"] for e in events}
    assert chunks[0].startswith("id: 1\n")
    later = asyncio.run(collect(len(events) - 1))
    assert len(later) == 1  # reprise du flux : seulement ce qui manque


def test_markdown_rendering_is_safe():
    html = render_markdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[lien](javascript:alert(1))\n\n**gras**', "/files/p/")
    assert "<script" not in html and "onerror=" not in html.replace("&lt;img src=x onerror=", "")
    assert 'href="javascript' not in html and "<strong>gras</strong>" in html
    html = render_markdown("![rendu](assets/p1/a.png) [doc](assets/p1/b.py) [web](https://ex.org) | a |\n|---|\n| b |", "/files/p/04_livrable/")
    assert 'src="/files/p/04_livrable/assets/p1/a.png"' in html and 'href="/files/p/04_livrable/assets/p1/b.py"' in html
    assert 'href="https://ex.org"' in html and 'rel="noopener noreferrer"' in html
    assert "<table>" in render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")


def test_dashboard_only_answers_local_requests(client):
    assert client.get("/api/meta", headers={"host": "evil.example.com"}).status_code == 403  # DNS rebinding
    assert client.get("/api/meta", headers={"host": "localhost:8765"}).status_code == 200
    forged = client.post("/api/projects", json={"request": "x"}, headers={"origin": "https://evil.example.com"})
    assert forged.status_code == 403 and client.get("/api/projects").json() == []  # aucun projet lancé par un site tiers
    same = client.post("/api/projects", json={"request": "Projet légitime"}, headers={"origin": BASE})
    assert same.status_code == 200
    wait_done(client, same.json()["id"])


def test_dashboard_opened_to_network_skips_host_check(make_config):
    config = make_config(dashboard={"host": "0.0.0.0"})
    with TestClient(create_app(config), base_url="http://192.168.1.20:8765") as client:
        assert client.get("/api/meta").status_code == 200
        assert client.post("/api/projects", json={"request": "x"}, headers={"origin": "https://evil.example.com"}).status_code == 403
