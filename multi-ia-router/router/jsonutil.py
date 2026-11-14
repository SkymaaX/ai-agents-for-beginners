"""Outils JSON : extraire du JSON d'une réponse bavarde, produire un schéma portable."""

from __future__ import annotations

import copy
import json
from typing import Any

from pydantic import BaseModel

# Mots-clés de JSON Schema que les sorties structurées des fournisseurs refusent souvent.
_UNSUPPORTED = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "default", "title", "examples",
)


def extract_json(text: str) -> Any:
    """Retrouve le premier objet/tableau JSON valide dans `text`.

    Les modèles locaux entourent souvent le JSON de texte ou de balises ``` : on tolère.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("réponse vide, JSON attendu")
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    raise ValueError("aucun JSON valide trouvé dans la réponse")


def _deref(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
            return _deref(merged, defs)
        return {key: _deref(value, defs) for key, value in node.items() if key != "$defs"}
    if isinstance(node, list):
        return [_deref(item, defs) for item in node]
    return node


def _clean(node: Any, strict: bool, descriptions: bool) -> Any:
    if isinstance(node, list):
        return [_clean(item, strict, descriptions) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED or (key == "description" and not descriptions):
            continue
        if key == "properties" and isinstance(value, dict):
            # les noms de propriétés ne sont pas des mots-clés : on ne les filtre pas
            out[key] = {name: _clean(sub, strict, descriptions) for name, sub in value.items()}
        else:
            out[key] = _clean(value, strict, descriptions)
    if out.get("type") == "object" and "properties" in out:
        out["required"] = list(out["properties"].keys())
        if strict:
            out["additionalProperties"] = False
    return out


def portable_schema(model: type[BaseModel], *, strict: bool = True, descriptions: bool = True) -> dict[str, Any]:
    """Schéma JSON sans $ref ni contraintes exotiques, accepté par Gemini, Claude et `claude -p`.

    strict=False omet `additionalProperties` (Gemini). descriptions=False retire les descriptions
    (schéma passé en argument de ligne de commande). Pydantic revalide ensuite la réponse
    (bornes, valeurs par défaut) : rien n'est perdu.
    """
    raw = model.model_json_schema()
    return _clean(_deref(raw, raw.get("$defs", {})), strict, descriptions)


def schema_text(model: type[BaseModel]) -> str:
    """Schéma lisible à coller dans un prompt (modèles sans sortie structurée native)."""
    return json.dumps(portable_schema(model), ensure_ascii=False, indent=2)


def dumps(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump()
    return json.dumps(value, ensure_ascii=False, indent=2)
