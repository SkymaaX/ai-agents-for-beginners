"""Recherche web : fournisseurs interchangeables (choix dans config : research.web.provider).

- gemini_grounding : Gemini interroge Google Search lui-même (ancrage officiel de l'API) et
  renvoie une synthèse avec ses sources. Aucune page n'est aspirée par le routeur.
- searxng : métamoteur open source auto-hébergé ; le routeur lit ensuite les pages trouvées
  via `Fetcher` (robots.txt, débit limité, cache), puis le chercheur les résume.

Chaque réponse cite ses sources par numéro local [1], [2]… ; le dossier de recherche les
renumérote ensuite en identifiants globaux [S1], [S2]…
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

import httpx

from ..agents.base import ROLE_RESEARCH, AgentTask
from ..errors import AgentError, ConfigError
from ..prompts import render
from ..runtime import Runtime
from .cache import DiskCache, RateLimiter
from .fetcher import Fetcher


@dataclass
class Source:
    title: str
    url: str
    origin: str = "web"
    id: str = ""
    query: str = ""


@dataclass
class WebAnswer:
    query: str
    summary: str
    sources: list[Source] = field(default_factory=list)


class WebProvider(Protocol):
    name: str

    def search(self, query: str, project_title: str) -> WebAnswer: ...


def _language(rt: Runtime) -> str:
    from ..pipeline.common import language

    return language(rt)


class GeminiGroundingProvider:
    name = "gemini_grounding"

    def __init__(self, rt: Runtime):
        self.rt = rt
        self.agent = rt.role("researcher")
        if "web_search" not in self.agent.capabilities and not rt.config.mock:
            raise ConfigError(
                f"L'agent chercheur « {self.agent.name} » ne sait pas faire de recherche web ancrée. "
                "Utilise un agent Gemini ou research.web.provider: searxng."
            )

    def search(self, query: str, project_title: str) -> WebAnswer:
        task = AgentTask(
            role=ROLE_RESEARCH,
            stage="research",
            prompt=render(
                self.rt.config.prompts_dir, "research_grounded",
                project=project_title, query=query, language=_language(self.rt),
            ),
            meta={"grounding": True, "query": query},
        )
        result = self.agent.run(task)
        sources = [Source(title=s.get("title") or s.get("url", ""), url=s.get("url", "")) for s in result.extra.get("sources", [])]
        if not sources and self.rt.config.mock:
            sources = [
                Source("Source simulée A", "https://example.org/a"),
                Source("Source simulée B", "https://example.org/b"),
            ]
        return WebAnswer(query=query, summary=result.text.strip(), sources=sources)


class SearxngProvider:
    name = "searxng"

    def __init__(self, rt: Runtime, fetcher: Fetcher, client: httpx.Client | None = None):
        self.rt = rt
        self.cfg = rt.config.research.web
        self.fetcher = fetcher
        self.client = client or httpx.Client(timeout=self.cfg.timeout, headers={"User-Agent": self.cfg.user_agent})

    def _results(self, query: str) -> list[dict]:
        url = self.cfg.searxng_url.rstrip("/") + "/search"
        try:
            response = self.client.get(url, params={"q": query, "format": "json"})
        except httpx.HTTPError as exc:
            raise AgentError(f"SearXNG injoignable à {self.cfg.searxng_url} ({exc.__class__.__name__})", retryable=True) from exc
        if response.status_code == 403:
            raise AgentError(
                "SearXNG refuse le format JSON : active `json` dans search.formats de son settings.yml.",
                retryable=False,
            )
        if response.status_code >= 400:
            raise AgentError(f"SearXNG : HTTP {response.status_code}", retryable=response.status_code >= 500)
        return response.json().get("results", [])[: self.cfg.max_results]

    def search(self, query: str, project_title: str) -> WebAnswer:
        sources: list[Source] = []
        context: list[tuple[str, str]] = []
        for item in self._results(query):
            url = item.get("url") or ""
            title = item.get("title") or url
            snippet = item.get("content") or ""
            page = self.fetcher.fetch(url) if (self.cfg.fetch_pages and url) else None
            text = page.text if page else snippet
            if not url or not text:
                continue
            sources.append(Source(title=title, url=url))
            context.append((f"[{len(sources)}] {title} — {url}", text))
        if not sources:
            return WebAnswer(query=query, summary="Aucun résultat exploitable pour cette requête.")
        task = AgentTask(
            role=ROLE_RESEARCH,
            stage="research",
            prompt=render(
                self.rt.config.prompts_dir, "research_summary",
                project=project_title, query=query, language=_language(self.rt),
            ),
            context=context,
            meta={"query": query},
        )
        result = self.rt.role("researcher").run(task)
        return WebAnswer(query=query, summary=result.text.strip(), sources=sources)


class CachedProvider:
    """Enveloppe un fournisseur : cache par requête + débit limité entre deux recherches."""

    def __init__(self, inner: WebProvider, cache: DiskCache, limiter: RateLimiter):
        self.inner = inner
        self.name = inner.name
        self.cache = cache
        self.limiter = limiter

    def search(self, query: str, project_title: str) -> WebAnswer:
        key = f"{self.name}|{query.strip().lower()}"
        cached = self.cache.get("search", key)
        if cached is not None:
            return WebAnswer(cached["query"], cached["summary"], [Source(**s) for s in cached["sources"]])
        self.limiter.wait(f"search:{self.name}")
        answer = self.inner.search(query, project_title)
        self.cache.set("search", key, asdict(answer))
        return answer


def build_provider(rt: Runtime) -> WebProvider:
    cfg = rt.config.research.web
    if rt.config.mock:
        return GeminiGroundingProvider(rt)  # l'agent simulé répond, sans cache ni réseau
    cache = DiskCache(rt.config.cache_dir / "web", cfg.cache_ttl_hours)
    limiter = RateLimiter(cfg.rate_limit_seconds)
    if cfg.provider == "gemini_grounding":
        inner: WebProvider = GeminiGroundingProvider(rt)
    elif cfg.provider == "searxng":
        inner = SearxngProvider(rt, Fetcher(cfg, cache, limiter=limiter))
    else:
        raise ConfigError(f"Fournisseur de recherche web inconnu : « {cfg.provider} » (gemini_grounding | searxng)")
    return CachedProvider(inner, cache, limiter)
