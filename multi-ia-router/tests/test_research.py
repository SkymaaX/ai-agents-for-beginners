"""Module 4 — recherche : robots.txt, débit, cache, vault (RAG), fournisseurs web, dossier."""

from __future__ import annotations

import json

import httpx
import pytest

from router.agents.qwen_anythingllm import AnythingLLMClient
from router.config import VaultCfg, WebCfg
from router.errors import AgentError
from router.research.cache import DiskCache, RateLimiter
from router.research.dossier import build_dossier, remap_citations
from router.research.fetcher import Fetcher, html_to_text
from router.research.vault_rag import VaultSearch
from router.research.web_search import CachedProvider, SearxngProvider, WebAnswer, Source
from router.runtime import Runtime
from router.schemas import ResearchQueries

HTML = """<html><head><title>Le compost</title><style>p{color:red}</style></head>
<body><nav>Menu Accueil</nav><script>alert(1)</script>
<h1>Faire son compost</h1><p>Mélanger matières <b>vertes</b> et brunes.</p>
<footer>Mentions légales</footer></body></html>"""


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def fetcher(tmp_path, handler, clock=None, **cfg):
    web = WebCfg(**cfg)
    clock = clock or FakeClock()
    limiter = RateLimiter(web.rate_limit_seconds, clock=clock.time, sleep=clock.sleep)
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": web.user_agent})
    return Fetcher(web, DiskCache(tmp_path / "cache", web.cache_ttl_hours), client=client, limiter=limiter)


def site(robots: str | int = "", pages=None):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots) if isinstance(robots, int) else httpx.Response(200, text=robots)
        body = (pages or {}).get(request.url.path, HTML)
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    return handler, hits


def test_html_to_text_drops_chrome_and_scripts():
    title, text = html_to_text(HTML)
    assert title == "Le compost"
    assert "Faire son compost" in text and "Mélanger matières vertes et brunes." in text
    assert "alert" not in text and "Menu" not in text and "Mentions" not in text and "color" not in text


def test_robots_disallow_is_respected(tmp_path):
    handler, hits = site("User-agent: *\nDisallow: /prive/\n")
    f = fetcher(tmp_path, handler)
    assert f.fetch("https://ex.org/prive/page") is None
    assert "/prive/page" not in hits  # la page interdite n'est jamais demandée
    page = f.fetch("https://ex.org/public/page")
    assert page is not None and page.title == "Le compost"


def test_robots_rule_for_our_user_agent(tmp_path):
    handler, hits = site("User-agent: multi-ia-router\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert fetcher(tmp_path, handler).fetch("https://ex.org/page") is None
    assert hits == ["/robots.txt"]


def test_missing_robots_allows_but_server_error_blocks(tmp_path):
    handler, _ = site(404)
    assert fetcher(tmp_path / "a", handler).fetch("https://ex.org/page") is not None
    handler, hits = site(503)
    assert fetcher(tmp_path / "b", handler).fetch("https://ex.org/page") is None  # dans le doute, on s'abstient
    assert hits == ["/robots.txt"]


def test_robots_can_be_disabled_explicitly(tmp_path):
    handler, hits = site("User-agent: *\nDisallow: /\n")
    assert fetcher(tmp_path, handler, respect_robots=False).fetch("https://ex.org/page") is not None
    assert "/robots.txt" not in hits


def test_only_http_and_html(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    f = fetcher(tmp_path, handler)
    assert f.fetch("file:///etc/passwd") is None and f.fetch("ftp://ex.org/x") is None
    assert f.fetch("https://ex.org/doc.pdf") is None


def test_rate_limit_per_domain_and_crawl_delay(tmp_path):
    clock = FakeClock()
    handler, _ = site("User-agent: *\nCrawl-delay: 5\n")
    f = fetcher(tmp_path, handler, clock=clock, rate_limit_seconds=2.0)
    f.fetch("https://lent.org/a")
    f.fetch("https://lent.org/b")
    assert clock.sleeps == [5.0, 5.0]  # le Crawl-delay du site (5 s) prime sur notre débit de base (2 s)

    clock2 = FakeClock()
    handler2, _ = site(404)
    f2 = fetcher(tmp_path / "2", handler2, clock=clock2, rate_limit_seconds=2.0)
    f2.fetch("https://a.org/x")
    f2.fetch("https://b.org/x")  # autre domaine : seule l'attente robots→page de chaque site
    assert clock2.sleeps == [2.0, 2.0]


def test_page_cache_avoids_second_download(tmp_path):
    handler, hits = site(404)
    f = fetcher(tmp_path, handler)
    first = f.fetch("https://ex.org/page")
    again = fetcher(tmp_path, handler).fetch("https://ex.org/page")  # nouvelle instance, même cache disque
    assert first == again and hits.count("/page") == 1 and hits.count("/robots.txt") == 1


def test_cache_expires(tmp_path, monkeypatch):
    cache = DiskCache(tmp_path, ttl_hours=1)
    cache.set("ns", "clé", {"v": 1})
    assert cache.get("ns", "clé") == {"v": 1} and cache.get("ns", "autre") is None
    import router.research.cache as module

    real = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: real + 2 * 3600)
    assert cache.get("ns", "clé") is None


def test_page_text_is_bounded(tmp_path):
    handler, _ = site(404, pages={"/long": "<html><body><p>" + "mot " * 20000 + "</p></body></html>"})
    page = fetcher(tmp_path, handler, max_page_chars=1000).fetch("https://ex.org/long")
    assert page is not None and len(page.text) == 1000


# ─────────────────────────────────────────────────────────────────────────── vault
def vault(handler) -> VaultSearch:
    client = AnythingLLMClient("http://localhost:3001", "cle", transport=httpx.MockTransport(handler))
    return VaultSearch(client, VaultCfg(workspace="vault", top_n=3, score_threshold=0.3))


def test_vault_vector_search(tmp_path):
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"id": "1", "text": "Arroser le soir.", "metadata": {"title": "Potager.md"}, "score": 0.81},
            {"id": "2", "text": "", "metadata": {"title": "Vide.md"}, "score": 0.5},
        ]})

    excerpts = vault(handler).search("arrosage")
    assert seen["path"] == "/api/v1/workspace/vault/vector-search"
    assert seen["body"] == {"query": "arrosage", "topN": 3, "scoreThreshold": 0.3}
    assert [(e.title, e.text, e.score) for e in excerpts] == [("Potager.md", "Arroser le soir.", 0.81)]


def test_vault_falls_back_to_query_mode_on_old_versions(tmp_path):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/vector-search"):
            return httpx.Response(404, text="Not Found")
        assert json.loads(request.content)["mode"] == "query"
        return httpx.Response(200, json={"textResponse": "…", "sources": [{"title": "Note.md", "chunk": "Extrait."}]})

    excerpts = vault(handler).search("q")
    assert paths == ["/api/v1/workspace/vault/vector-search", "/api/v1/workspace/vault/chat"]
    assert excerpts[0].title == "Note.md" and excerpts[0].text == "Extrait."


# ───────────────────────────────────────────────────────────────── fournisseurs web
def test_searxng_provider_fetches_politely_and_summarises(make_config, tmp_path):
    rt = Runtime(make_config())  # chercheur simulé
    web = rt.config.research.web
    web.rate_limit_seconds = 0
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "localhost":
            assert request.url.params["format"] == "json" and request.url.params["q"] == "compost"
            return httpx.Response(200, json={"results": [
                {"url": "https://ok.org/a", "title": "Guide A", "content": "extrait A"},
                {"url": "https://interdit.org/b", "title": "Guide B", "content": "extrait B"},
            ]})
        if request.url.path == "/robots.txt":
            body = "User-agent: *\nDisallow: /\n" if request.url.host == "interdit.org" else ""
            return httpx.Response(200, text=body)
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SearxngProvider(rt, Fetcher(web, DiskCache(tmp_path, 1), client=client), client=client)
    answer = provider.search("compost", "Projet")
    assert [s.url for s in answer.sources] == ["https://ok.org/a", "https://interdit.org/b"]
    assert "https://interdit.org/b" not in requests  # page interdite : on garde l'extrait du moteur, sans la lire
    assert answer.summary


def test_cached_provider_calls_inner_once(tmp_path):
    class Inner:
        name = "faux"
        calls = 0

        def search(self, query, project_title):
            Inner.calls += 1
            return WebAnswer(query, "résumé [1]", [Source("T", "https://ex.org")])

    clock = FakeClock()
    provider = CachedProvider(Inner(), DiskCache(tmp_path, 1), RateLimiter(3.0, clock=clock.time, sleep=clock.sleep))
    first = provider.search("Compost ", "P")
    second = provider.search("compost", "P")  # même requête à la casse près → cache
    provider.search("autre", "P")
    assert Inner.calls == 2 and first.summary == second.summary and second.sources[0].url == "https://ex.org"
    assert clock.sleeps == [3.0]  # débit limité entre deux vraies recherches


# ─────────────────────────────────────────────────────────────────────────── dossier
def test_remap_citations_to_global_ids():
    assert remap_citations("Fait [1]. Autre [2, 1]. Année [2024]. Liste [3].", ["S4", "S5"]) == \
        "Fait [S4]. Autre [S5, S4]. Année [2024]. Liste [3]."


def test_dossier_numbers_sources_globally(make_config):
    rt = Runtime(make_config())
    markdown, sources = build_dossier(rt, ResearchQueries(vault=["notes"], web=["q1", "q2"]), "Projet")
    assert [s["id"] for s in sources] == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert [s["origin"] for s in sources] == ["vault", "vault", "web", "web", "web", "web"]
    assert "## Vault Obsidian" in markdown and "## Web" in markdown
    assert "[S5]" in markdown and "https://example.org/a" in markdown

    extra, more = build_dossier(rt, ResearchQueries(web=["q3"]), "Projet", heading="Complément", start=len(sources))
    assert [s["id"] for s in more] == ["S7", "S8"] and extra.startswith("# Complément")


def test_dossier_respects_disabled_sources_and_query_cap(make_config):
    rt = Runtime(make_config(research={"vault": {"enabled": False}, "web": {"max_queries": 1}}))
    markdown, sources = build_dossier(rt, ResearchQueries(vault=["notes"], web=["q1", "q2"]), "Projet")
    assert "Vault" not in markdown and "q2" not in markdown and len(sources) == 2
    rt_off = Runtime(make_config(research={"vault": {"enabled": False}, "web": {"enabled": False}}))
    markdown, sources = build_dossier(rt_off, ResearchQueries(vault=["a"], web=["b"]), "Projet")
    assert sources == [] and "Aucune recherche" in markdown


def test_failed_query_is_reported_not_fatal(make_config, monkeypatch):
    rt = Runtime(make_config())

    class Flaky:
        name = "faux"

        def search(self, query, project_title):
            if query == "casse":
                raise AgentError("moteur en panne", retryable=True)
            return WebAnswer(query, "ok [1]", [Source("T", "https://ex.org")])

    monkeypatch.setattr("router.research.dossier.build_provider", lambda runtime: Flaky())
    markdown, sources = build_dossier(rt, ResearchQueries(web=["casse", "marche"]), "Projet")
    assert "Recherche échouée : moteur en panne" in markdown and len(sources) == 1 and "ok [S1]" in markdown
