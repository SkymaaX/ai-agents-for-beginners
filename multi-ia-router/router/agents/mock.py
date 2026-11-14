"""Agent simulé : réponses plausibles sans aucun appel réseau.

Sert aux tests et au mode démonstration (`--mock`). Il prend la place de n'importe quel agent
et respecte les mêmes schémas. `config.mock_scenario` force des situations :

  critique: default | accept | always_revise      (default = 1er tour à revoir, puis accepté)
  verdict:  ok | fail_once | always_fail
  fail:         {role: "...", times: 1, kind: retryable | fatal | quota}
  invalid_json: {role: "...", times: 1}
  bad_plan_once: true                              (plan avec agent inconnu, puis corrigé)
"""

from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path
from typing import Any

from ..errors import AgentError, QuotaError
from .base import (
    ROLE_ASSEMBLE, ROLE_PRODUCE, ROLE_RESEARCH, ROLE_REVISE, Agent, AgentResult, AgentTask, Usage,
)


def write_png(path: Path, width: int = 320, height: int = 200, seed: int = 0) -> Path:
    """Écrit un PNG dégradé sans bibliothèque externe (image factice mais valide)."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes((
                (x * 255 // max(1, width - 1) + seed * 40) % 256,
                (y * 255 // max(1, height - 1)) % 256,
                (160 + seed * 30) % 256,
            ))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


class MockAgent(Agent):
    type_name = "mock"
    default_capabilities = frozenset({"text", "vision", "image", "video", "files"})
    native_json = True

    def __init__(self, name, cfg, runtime, stands_for: type[Agent] | None = None):
        super().__init__(name, cfg, runtime)
        self._stands_for = stands_for

    @property
    def capabilities(self) -> set[str]:
        if self.cfg.capabilities is not None:
            return set(self.cfg.capabilities)
        if self._stands_for is not None:
            return set(self._stands_for.default_capabilities)
        return set(self.default_capabilities)

    # ------------------------------------------------------------------------ scénarios
    def _scenario(self) -> dict[str, Any]:
        return self.runtime.config.mock_scenario or {}

    def _count(self, key: str) -> int:
        counters = self.runtime.counters
        counters[key] = counters.get(key, 0) + 1
        return counters[key]

    def _maybe_fail(self, task: AgentTask) -> None:
        spec = self._scenario().get("fail")
        if not spec or spec.get("role") != task.role:
            return
        if spec.get("agent") and spec["agent"] != self.name:
            return
        if self._count(f"fail:{task.role}") > int(spec.get("times", 1)):
            return
        kind = spec.get("kind", "retryable")
        if kind == "quota":
            raise QuotaError("quota simulé épuisé", agent=self.name)
        raise AgentError(f"panne simulée ({kind})", retryable=(kind == "retryable"), agent=self.name)

    def _invalid_json(self, task: AgentTask) -> bool:
        spec = self._scenario().get("invalid_json")
        if not spec or spec.get("role") != task.role:
            return False
        return self._count(f"invalid:{task.role}") <= int(spec.get("times", 1))

    # ---------------------------------------------------------------------------- appel
    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        delay = self.runtime.config.mock_delay
        if delay:
            time.sleep(delay)
        self._maybe_fail(task)
        usage = Usage(tokens_in=max(1, len(task.system + prompt) // 4), tokens_out=0)
        model = f"simulation:{self.name}"

        if task.schema is not None:
            if self._invalid_json(task):
                text = "Voici ma réponse : {pas du json"
                usage.tokens_out = len(text) // 4
                return AgentResult(text=text, usage=usage, model=model)
            builder = getattr(self, f"_json_{task.schema.__name__.lower()}", None)
            if builder is None:
                raise AgentError(f"agent simulé : schéma {task.schema.__name__} non géré", retryable=False)
            data = builder(task)
            text = _dumps(data)
            usage.tokens_out = len(text) // 4
            return AgentResult(text=text, data=data, usage=usage, model=model)

        files: list[Path] = []
        if task.kind == "image":
            text, files = self._image(task)
        elif task.kind == "video":
            text, files = self._video(task)
        elif task.kind == "files":
            text, files = self._files(task)
        else:
            text = self._text(task)
        usage.tokens_out = max(1, len(text) // 4)
        return AgentResult(text=text, files=files, usage=usage, model=model)

    # ------------------------------------------------------------------- sorties JSON
    def _json_brief(self, task: AgentTask) -> dict:
        request = task.meta.get("request", "").strip()
        first = request.splitlines()[0] if request else "Projet"
        return {
            "title": first[:60],
            "objective": f"Répondre précisément à la demande : {request[:300]}",
            "deliverables": ["Un document de synthèse structuré", "Les fichiers produits par les agents"],
            "constraints": ["Rester fidèle à la demande initiale", "Citer les sources utilisées"],
            "acceptance_criteria": [
                "Chaque point de la demande est traité",
                "Le livrable est un document unique et cohérent",
            ],
            "open_questions": [],
        }

    def _json_plan(self, task: AgentTask) -> dict:
        producers: dict[str, list[str]] = task.meta.get("producers", {})
        scenario = self._scenario()

        def pick(capability: str, prefer: str = "") -> str | None:
            names = [n for n, caps in producers.items() if capability in caps]
            if prefer in names:
                return prefer
            return names[0] if names else None

        text_agents = [n for n, caps in producers.items() if "text" in caps and "files" not in caps]
        text_agents = text_agents or [n for n, caps in producers.items() if "text" in caps]
        title = (task.meta.get("title") or "Projet")[:60]
        parts: list[dict] = []
        if text_agents:
            first = text_agents[0]
            second = text_agents[1] if len(text_agents) > 1 else first
            parts.append({
                "id": "p1", "title": "Analyse et contexte", "type": "text", "agent": first, "depends_on": [],
                "prompt": f"Rédige l'analyse et le contexte pour : {title}. Appuie-toi sur le dossier de recherche.",
                "success_criteria": ["Contexte clair", "Sources citées avec liens"],
                "expected_output": {"format": "md", "filename": ""},
            })
            parts.append({
                "id": "p2", "title": "Recommandations détaillées", "type": "text", "agent": second,
                "depends_on": ["p1"],
                "prompt": f"À partir de l'analyse, rédige des recommandations concrètes pour : {title}.",
                "success_criteria": ["Recommandations actionnables", "Cohérence avec l'analyse"],
                "expected_output": {"format": "md", "filename": ""},
            })
        executor = pick("files")
        if executor:
            parts.append({
                "id": "p3", "title": "Script d'automatisation", "type": "code", "agent": executor,
                "depends_on": [],
                "prompt": "Écris un script Python commenté qui illustre la solution, avec un README court.",
                "success_criteria": ["Le script s'exécute sans erreur", "README présent"],
                "expected_output": {"format": "py", "filename": "solution.py"},
            })
        painter = pick("image")
        if painter:
            parts.append({
                "id": "p4", "title": "Illustration de couverture", "type": "image", "agent": painter,
                "depends_on": [],
                "prompt": "Illustration sobre et moderne représentant le sujet du projet, format paysage.",
                "success_criteria": ["Image lisible", "En rapport avec le sujet"],
                "expected_output": {"format": "png", "filename": "couverture.png"},
            })
        if scenario.get("bad_plan_once") and self._count("bad_plan") == 1 and parts:
            parts[0] = {**parts[0], "agent": "agent_inexistant"}
        return {
            "project_title": title,
            "summary": "Découpage en analyse, recommandations, script et illustration.",
            "research_queries": {
                "vault": [f"Notes existantes sur : {title}"],
                "web": [f"{title} état de l'art", f"{title} bonnes pratiques"],
            },
            "parts": parts,
            "assembly_instructions": "Introduction, puis chaque partie dans l'ordre, puis conclusion et sources.",
        }

    def _json_critique(self, task: AgentTask) -> dict:
        mode = self._scenario().get("critique", "default")
        round_no = int(task.meta.get("round", 1))
        accept = mode == "accept" or (mode == "default" and round_no >= 2)
        if accept:
            return {
                "scores": {"fidelite": 5, "completude": 4, "erreurs": 5, "qualite": 4, "sources": 4},
                "verdict": "accept",
                "summary": "Version conforme à la demande. Rien de bloquant.",
                "problems": [],
            }
        return {
            "scores": {"fidelite": 4, "completude": 3, "erreurs": 4, "qualite": 3, "sources": 2},
            "verdict": "revise",
            "summary": "Bonne base, mais incomplète et insuffisamment sourcée.",
            "problems": [
                {
                    "critere": "completude", "severity": "majeur",
                    "detail": "Le deuxième critère de réussite n'est pas traité.",
                    "correction": "Ajouter une section dédiée avec un exemple concret.",
                },
                {
                    "critere": "sources", "severity": "mineur",
                    "detail": "Deux affirmations chiffrées sans source.",
                    "correction": "Citer la source avec son lien, ou retirer le chiffre.",
                },
            ],
        }

    def _json_arbitration(self, task: AgentTask) -> dict:
        versions = task.meta.get("versions") or [1]
        return {
            "chosen_version": max(versions),
            "rationale": "La dernière version corrige le plus de points ; les défauts restants sont mineurs.",
            "reserves": ["Sources encore partielles sur un point secondaire."],
        }

    def _json_verdict(self, task: AgentTask) -> dict:
        mode = self._scenario().get("verdict", "ok")
        attempt = int(task.meta.get("attempt", 1))
        if mode == "always_fail" or (mode == "fail_once" and attempt == 1):
            part_ids = task.meta.get("part_ids") or []
            return {
                "ok": False,
                "summary": "Un point de la demande n'est pas assez développé.",
                "gaps": [{
                    "part_id": part_ids[0] if part_ids else "",
                    "description": "Le livrable ne détaille pas assez la mise en œuvre.",
                    "instruction": "Ajouter les étapes de mise en œuvre, numérotées.",
                }],
            }
        return {"ok": True, "summary": "Le livrable couvre tous les points de la demande.", "gaps": []}

    def _json_revisionplan(self, task: AgentTask) -> dict:
        part_ids: list[str] = task.meta.get("part_ids") or []
        instruction = task.meta.get("instruction", "")
        changes = [{"part_id": part_ids[0], "instruction": instruction}] if part_ids else []
        return {
            "summary": f"Modification ciblée de {len(changes)} partie(s).",
            "changes": changes,
            "new_parts": [],
            "remove_parts": [],
            "research_queries": {"vault": [], "web": []},
            "assembly_instructions": "",
        }

    # -------------------------------------------------------------------- sorties texte
    def _text(self, task: AgentTask) -> str:
        meta = task.meta
        title = meta.get("title", "Partie")
        version = meta.get("version", 1)
        if task.role == ROLE_RESEARCH:
            query = meta.get("query", "")
            return (
                f"Synthèse simulée pour « {query} ».\n\n"
                "- Point clé 1 : pratique largement documentée [1].\n"
                "- Point clé 2 : limites connues à surveiller [2].\n"
            )
        if task.role == ROLE_ASSEMBLE:
            sections = "\n\n".join(f"## {name}\n\n{body.strip()}" for name, body in meta.get("parts", []))
            assets = "\n".join(
                f"![{Path(a).stem}]({a})" if Path(a).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                else f"- [{Path(a).name}]({a})"
                for a in meta.get("assets", [])
            )
            note = f"\n\n> Correction d'assemblage appliquée : {meta['instruction']}" if meta.get("instruction") else ""
            return (
                f"# {meta.get('project_title', 'Livrable')}\n\n"
                f"*Livrable assemblé (simulation), version {meta.get('assembly_version', 1)}.*{note}\n\n"
                f"## Introduction\n\nCe document répond à la demande initiale.\n\n{sections}\n\n"
                f"## Fichiers produits\n\n{assets or 'Aucun.'}\n\n## Conclusion\n\nTous les points sont traités.\n"
            )
        if task.role == ROLE_REVISE:
            return (
                f"### {title} (version {version})\n\n"
                f"Texte révisé par {self.name}. Consigne appliquée : {meta.get('instruction', 'critique')[:200]}\n\n"
                "Section ajoutée avec un exemple concret, sources citées [1].\n\n"
                "<<<REPONSE_CRITIQUE>>>\n- Complétude : corrigé, section ajoutée.\n- Sources : corrigé.\n"
            )
        return (
            f"### {title} (version {version})\n\n"
            f"Contenu simulé produit par {self.name}.\n\n"
            "- Élément A, appuyé sur le dossier de recherche [1].\n- Élément B, à approfondir.\n"
        )

    def _image(self, task: AgentTask) -> tuple[str, list[Path]]:
        workdir = task.workdir or Path(".")
        name = task.meta.get("filename") or "image.png"
        path = write_png(workdir / name, seed=int(task.meta.get("version", 1)))
        return f"Image simulée générée par {self.name} : {name}\nPrompt : {task.prompt[:200]}", [path]

    def _video(self, task: AgentTask) -> tuple[str, list[Path]]:
        workdir = task.workdir or Path(".")
        name = task.meta.get("filename") or "video.mp4"
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42simulation")
        return f"Vidéo simulée générée par {self.name} : {name}", [path]

    def _files(self, task: AgentTask) -> tuple[str, list[Path]]:
        workdir = task.workdir or Path(".")
        workdir.mkdir(parents=True, exist_ok=True)
        version = task.meta.get("version", 1)
        if task.meta.get("part_type") == "blender":
            script = workdir / "scene.py"
            script.write_text(
                "import bpy\n\n"
                "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
                "bpy.ops.mesh.primitive_cube_add(size=2)\n"
                f"# version {version}\n"
                "bpy.ops.wm.save_as_mainfile(filepath='scene.blend')\n",
                encoding="utf-8",
            )
            return f"Script Blender écrit par {self.name} : scene.py (version {version}).", [script]
        name = task.meta.get("filename") or "solution.py"
        script = workdir / name
        script.write_text(
            f'"""Script simulé, version {version}."""\n\n\ndef main():\n    print("ok")\n\n\n'
            'if __name__ == "__main__":\n    main()\n',
            encoding="utf-8",
        )
        readme = workdir / "README.md"
        readme.write_text(f"# {task.meta.get('title', 'Script')}\n\nLancer : `python {name}`\n", encoding="utf-8")
        return f"Fichiers écrits par {self.name} : {name}, README.md (version {version}).", [script, readme]


def _dumps(data: Any) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)
