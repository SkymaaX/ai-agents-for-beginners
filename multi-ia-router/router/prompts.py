"""Gabarits de prompts.

Les textes vivent dans `router/prompts/*.md` : modifiables sans toucher au code.
Variables au format {{nom}}. Une variable absente lève une erreur (pas de prompt troué).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .errors import ConfigError

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_DEFAULT_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=128)
def _load(directory: str, name: str) -> str:
    for base in (Path(directory), _DEFAULT_DIR):
        path = base / f"{name}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise ConfigError(f"Gabarit de prompt introuvable : {name}.md")


def render(prompts_dir: Path, name: str, **variables: object) -> str:
    template = _load(str(prompts_dir), name)

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            raise ConfigError(f"Prompt {name}.md : variable « {key} » non fournie.")
        return str(variables[key])

    return _VAR.sub(replace, template).strip()


def bullets(items: list[str], empty: str = "(aucun)") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty
