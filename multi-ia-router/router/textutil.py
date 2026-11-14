"""Petits outils texte : réduction de contexte, estimation de tokens."""

from __future__ import annotations

import re

CUT = "\n[… {n} caractères coupés pour tenir dans le contexte …]\n"


def estimate_tokens(text: str) -> int:
    """Estimation grossière (≈ 4 caractères par token) quand l'API ne donne pas le compte."""
    return max(1, len(text) // 4) if text else 0


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    limit = max(limit, 200)
    head = int(limit * 0.7)
    tail = limit - head
    removed = len(text) - limit
    return text[:head].rstrip() + CUT.format(n=removed) + text[-tail:].lstrip()


def fit_text(text: str, max_chars: int) -> str:
    """Réduit `text` à ~max_chars en gardant tous les titres Markdown.

    Chaque section est raccourcie en proportion : le lecteur voit toute la structure,
    avec des coupes signalées. Sert aux modèles locaux à petit contexte.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    sections = re.split(r"(?m)^(?=#{1,6} )", text)
    sections = [s for s in sections if s]
    if len(sections) <= 1:
        return _cut(text, max_chars)
    ratio = max_chars / len(text)
    parts = []
    for section in sections:
        heading, _, body = section.partition("\n")
        budget = max(200, int(len(section) * ratio) - len(heading))
        parts.append(heading + "\n" + _cut(body, budget))
    return "".join(parts)


def fit_blocks(blocks: list[tuple[str, str]], max_chars: int) -> tuple[list[tuple[str, str]], bool]:
    """Répartit un budget de caractères entre blocs (titre, texte). Renvoie (blocs, coupé ?)."""
    total = sum(len(text) for _, text in blocks)
    if max_chars <= 0 or total <= max_chars:
        return blocks, False
    ratio = max_chars / total
    return [(title, fit_text(text, max(400, int(len(text) * ratio)))) for title, text in blocks], True
