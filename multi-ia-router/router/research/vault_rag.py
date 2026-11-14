"""Recherche dans la vault Obsidian, indexée dans un workspace AnythingLLM (RAG)."""

from __future__ import annotations

from dataclasses import dataclass

from ..agents.qwen_anythingllm import AnythingLLMClient
from ..config import VaultCfg
from ..errors import AgentError


@dataclass
class Excerpt:
    title: str
    text: str
    score: float = 0.0


class VaultSearch:
    def __init__(self, client: AnythingLLMClient, cfg: VaultCfg):
        self.client = client
        self.cfg = cfg

    def search(self, query: str) -> list[Excerpt]:
        """Extraits de notes les plus proches de la question."""
        try:
            results = self.client.vector_search(self.cfg.workspace, query, self.cfg.top_n, self.cfg.score_threshold)
            return [
                Excerpt(
                    title=str((item.get("metadata") or {}).get("title") or "note"),
                    text=str(item.get("text") or "").strip(),
                    score=float(item.get("score") or 0.0),
                )
                for item in results
                if item.get("text")
            ]
        except AgentError as exc:
            if "HTTP 404" not in str(exc):
                raise
        # Ancienne version d'AnythingLLM sans /vector-search : mode « query » (RAG strict).
        data = self.client.chat(self.cfg.workspace, query, mode="query")
        return [
            Excerpt(title=str(source.get("title") or "note"), text=str(source.get("chunk") or source.get("text") or "").strip())
            for source in data.get("sources", [])
            if source.get("chunk") or source.get("text")
        ]


class MockVault:
    """Extraits factices pour le mode simulation."""

    def search(self, query: str) -> list[Excerpt]:
        return [
            Excerpt("Notes de projet.md", f"Extrait simulé de la vault en rapport avec « {query} ».", 0.82),
            Excerpt("Idées en vrac.md", "Deuxième extrait simulé : contraintes et préférences notées par le passé.", 0.71),
        ]
