"""Construction du dossier de recherche (Markdown + liste de sources numérotées [S1]…)."""

from __future__ import annotations

import re
from dataclasses import asdict

from ..agents.qwen_anythingllm import client_from_cfg
from ..errors import AgentError, ConfigError, QuotaError
from ..logs import logger
from ..runtime import Runtime
from ..schemas import ResearchQueries
from .vault_rag import MockVault, VaultSearch
from .web_search import Source, build_provider

_LOCAL_REF = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def remap_citations(text: str, ids: list[str]) -> str:
    """Remplace les renvois locaux [1], [2, 3] par les identifiants globaux [S4], [S5, S6]."""

    def replace(match: re.Match) -> str:
        numbers = [int(n) for n in re.split(r"\s*,\s*", match.group(1))]
        if not all(1 <= n <= len(ids) for n in numbers):
            return match.group(0)  # ce n'est pas un renvoi vers nos sources
        return "[" + ", ".join(ids[n - 1] for n in numbers) + "]"

    return _LOCAL_REF.sub(replace, text)


def build_dossier(
    rt: Runtime, queries: ResearchQueries, project_title: str, *, heading: str = "Dossier de recherche", start: int = 0,
) -> tuple[str, list[dict]]:
    """Exécute les requêtes. Renvoie (markdown, sources). `start` = nombre de sources déjà numérotées.

    Une requête qui échoue est notée dans le dossier et n'arrête pas le projet ;
    seul un quota épuisé remonte (mise en pause).
    """
    cfg = rt.config.research
    sources: list[Source] = []
    lines: list[str] = [f"# {heading}", ""]

    def next_id() -> str:
        return f"S{start + len(sources) + 1}"

    vault_queries = queries.vault if cfg.vault.enabled else []
    if vault_queries:
        lines += ["## Vault Obsidian (notes personnelles)", ""]
        try:
            if rt.config.mock:
                vault = MockVault()
            else:
                agent_cfg = rt.config.agent_cfg(cfg.vault.agent)
                vault = VaultSearch(client_from_cfg(agent_cfg, timeout=120), cfg.vault)
        except ConfigError as exc:
            vault = None
            lines += [f"Recherche dans la vault impossible : {exc}", ""]
            rt.event("research_error", stage="research", source="vault", error=str(exc))
        for query in vault_queries if vault else []:
            rt.event("research_query", stage="research", source="vault", query=query)
            lines += [f"### Question : {query}", ""]
            try:
                excerpts = vault.search(query)
            except QuotaError:
                raise
            except AgentError as exc:
                logger.warning("recherche vault échouée (%s) : %s", query, exc)
                rt.event("research_error", stage="research", source="vault", query=query, error=str(exc))
                lines += [f"Recherche échouée : {exc}", ""]
                continue
            if not excerpts:
                lines += ["Aucune note pertinente trouvée.", ""]
            for excerpt in excerpts:
                source = Source(title=excerpt.title, url="", origin="vault", id=next_id(), query=query)
                sources.append(source)
                quoted = "\n".join("> " + line for line in excerpt.text.splitlines() if line.strip())
                lines += [f"**[{source.id}] {excerpt.title}** (pertinence {excerpt.score:.2f})", "", quoted, ""]

    web_queries = queries.web[: cfg.web.max_queries] if cfg.web.enabled else []
    if web_queries:
        lines += ["## Web", ""]
        provider = build_provider(rt)
        for query in web_queries:
            rt.event("research_query", stage="research", source="web", query=query, provider=provider.name)
            lines += [f"### Requête : {query}", ""]
            try:
                answer = provider.search(query, project_title)
            except QuotaError:
                raise
            except AgentError as exc:
                logger.warning("recherche web échouée (%s) : %s", query, exc)
                rt.event("research_error", stage="research", source="web", query=query, error=str(exc))
                lines += [f"Recherche échouée : {exc}", ""]
                continue
            ids: list[str] = []
            for found in answer.sources:
                found.id, found.query = next_id(), query
                sources.append(found)
                ids.append(found.id)
            lines += [remap_citations(answer.summary, ids), ""]
            if answer.sources:
                lines += ["Sources :", ""] + [f"- [{s.id}] {s.title} — {s.url}" for s in answer.sources] + [""]

    if not vault_queries and not web_queries:
        lines += ["Aucune recherche n'a été jugée nécessaire pour ce projet (ou la recherche est désactivée).", ""]
    return "\n".join(lines).rstrip() + "\n", [asdict(s) for s in sources]
