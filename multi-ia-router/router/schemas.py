"""Schémas JSON échangés entre agents (validés par Pydantic).

Ces modèles sont LE contrat : plan du répartiteur, grille de critique, arbitrage, verdict.
Modifier un champ ici le modifie partout (prompt, validation, tableau de bord).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

PartType = Literal["text", "code", "files", "blender", "image", "video", "audio"]
Severity = Literal["bloquant", "majeur", "mineur"]

GRID = ("fidelite", "completude", "erreurs", "qualite", "sources")
GRID_LABELS = {
    "fidelite": "Fidélité à la demande",
    "completude": "Complétude",
    "erreurs": "Absence d'erreurs",
    "qualite": "Qualité",
    "sources": "Sources",
}


# --------------------------------------------------------------------------- brief (Qwen)
class Brief(BaseModel):
    """Reformulation de la demande par le chef."""

    title: str = Field(description="Titre court du projet")
    objective: str = Field(description="Objectif reformulé, sans rien ajouter à la demande")
    deliverables: list[str] = Field(default_factory=list, description="Livrables attendus")
    constraints: list[str] = Field(default_factory=list, description="Contraintes explicites")
    acceptance_criteria: list[str] = Field(
        default_factory=list, description="Critères vérifiables pour valider le livrable final"
    )
    open_questions: list[str] = Field(
        default_factory=list, description="Ambiguïtés ; hypothèse retenue pour chacune"
    )


# ------------------------------------------------------------------------- plan (Gemini)
class ExpectedOutput(BaseModel):
    format: str = Field(default="md", description="Extension attendue : md, py, png, mp4, blend...")
    filename: str = Field(default="", description="Nom de fichier souhaité, vide si texte simple")


class Part(BaseModel):
    id: str = Field(description="Identifiant court et unique : p1, p2...")
    title: str
    type: PartType = Field(description="text, code, files, blender, image, video ou audio")
    agent: str = Field(description="Nom exact d'un agent disponible")
    depends_on: list[str] = Field(default_factory=list, description="Ids des parties requises avant")
    prompt: str = Field(description="Prompt optimisé, autonome, pour l'agent assigné")
    success_criteria: list[str] = Field(default_factory=list, description="Critères de réussite vérifiables")
    expected_output: ExpectedOutput = Field(default_factory=ExpectedOutput)

    @field_validator("id")
    @classmethod
    def _clean_id(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(c.isalnum() or c in "-_" for c in value):
            raise ValueError("id de partie invalide (lettres, chiffres, - et _ uniquement)")
        return value


class ResearchQueries(BaseModel):
    vault: list[str] = Field(default_factory=list, description="Questions à poser à la vault Obsidian")
    web: list[str] = Field(default_factory=list, description="Requêtes de recherche web")


class Plan(BaseModel):
    """Découpage de la demande par le répartiteur."""

    project_title: str
    summary: str = Field(default="", description="Stratégie de découpage en 2-3 phrases")
    research_queries: ResearchQueries = Field(default_factory=ResearchQueries)
    parts: list[Part]
    assembly_instructions: str = Field(default="", description="Comment assembler les parties")

    def part(self, part_id: str) -> Part:
        for part in self.parts:
            if part.id == part_id:
                return part
        raise KeyError(part_id)

    def ordered(self) -> list[Part]:
        """Parties triées pour que chaque dépendance passe avant (tri topologique)."""
        done: list[Part] = []
        seen: set[str] = set()
        pending = list(self.parts)
        while pending:
            ready = [p for p in pending if all(d in seen for d in p.depends_on)]
            if not ready:
                raise ValueError("Dépendances circulaires dans le plan")
            for part in ready:
                done.append(part)
                seen.add(part.id)
                pending.remove(part)
        return done


def check_plan(plan: Plan, agents: dict[str, set[str]], capability_for_type: dict[str, str]) -> None:
    """Contrôles que le schéma seul ne garantit pas. Lève ValueError avec un message exploitable.

    `agents` : nom d'agent disponible → capacités.
    """
    problems: list[str] = []
    if not plan.parts:
        problems.append("Le plan ne contient aucune partie.")
    ids = [p.id for p in plan.parts]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"Id de partie en double : {dup}.")
    for part in plan.parts:
        if part.agent not in agents:
            problems.append(
                f"Partie {part.id} : agent « {part.agent} » indisponible. Agents possibles : {sorted(agents)}."
            )
        else:
            need = capability_for_type[part.type]
            if need not in agents[part.agent]:
                able = sorted(n for n, caps in agents.items() if need in caps)
                problems.append(
                    f"Partie {part.id} : l'agent « {part.agent} » ne sait pas produire le type "
                    f"« {part.type} ». Agents capables : {able or 'aucun — change le type'}."
                )
        if part.type == "audio" and not part.depends_on and not part.prompt.strip():
            problems.append(
                f"Partie {part.id} (audio) : sans dépendance, son prompt est le texte lu tel quel : il ne peut pas être vide. "
                "Fais-la dépendre d'une partie `text` qui rédige le script de narration."
            )
        for dep in part.depends_on:
            if dep not in ids:
                problems.append(f"Partie {part.id} : dépendance inconnue « {dep} ».")
            if dep == part.id:
                problems.append(f"Partie {part.id} : dépend d'elle-même.")
    if not problems:
        try:
            plan.ordered()
        except ValueError as exc:
            problems.append(str(exc) + ".")
    if problems:
        raise ValueError(" ".join(problems))


# ------------------------------------------------------------------------------ critique
class Scores(BaseModel):
    """Grille fixe, notes de 0 (inacceptable) à 5 (excellent)."""

    fidelite: int = Field(ge=0, le=5, description="Fidélité à la demande initiale")
    completude: int = Field(ge=0, le=5, description="Tout ce qui est demandé est présent")
    erreurs: int = Field(ge=0, le=5, description="5 = aucune erreur factuelle, logique ou technique")
    qualite: int = Field(ge=0, le=5, description="Clarté, structure, niveau de finition")
    sources: int = Field(ge=0, le=5, description="Affirmations sourcées, liens valides ; 5 si sans objet")


class Problem(BaseModel):
    critere: Literal["fidelite", "completude", "erreurs", "qualite", "sources"]
    severity: Severity
    detail: str = Field(description="Ce qui ne va pas, avec l'endroit précis")
    correction: str = Field(description="Correction concrète à appliquer")


class Critique(BaseModel):
    scores: Scores
    verdict: Literal["accept", "revise"]
    summary: str = Field(default="", description="Avis global en 1-3 phrases")
    problems: list[Problem] = Field(default_factory=list)

    def accepted(self, min_score: int) -> bool:
        """Accepté si le critique le dit, sans problème bloquant, et toutes les notes au seuil."""
        if self.verdict != "accept":
            return False
        if any(p.severity == "bloquant" for p in self.problems):
            return False
        return all(getattr(self.scores, key) >= min_score for key in GRID)


class Arbitration(BaseModel):
    """Décision du chef quand auteur et critique ne convergent pas."""

    chosen_version: int = Field(description="Numéro de la version retenue")
    rationale: str = Field(description="Pourquoi cette version")
    reserves: list[str] = Field(default_factory=list, description="Défauts restants assumés")


# ---------------------------------------------------------------------- validation finale
class Gap(BaseModel):
    part_id: str = Field(default="", description="Partie fautive ; vide si le défaut vient de l'assemblage")
    description: str
    instruction: str = Field(description="Correction demandée, concrète")


class Verdict(BaseModel):
    """Comparaison livrable / demande initiale par le chef."""

    ok: bool
    summary: str = ""
    gaps: list[Gap] = Field(default_factory=list)


# --------------------------------------------------------- modification d'un projet existant
class PartChange(BaseModel):
    part_id: str
    instruction: str = Field(description="Ce qu'il faut changer dans cette partie")


class RevisionPlan(BaseModel):
    """Traduction d'une demande de modification : seules les parties touchées repassent."""

    summary: str = ""
    changes: list[PartChange] = Field(default_factory=list, description="Parties existantes à modifier")
    new_parts: list[Part] = Field(default_factory=list, description="Parties à ajouter")
    remove_parts: list[str] = Field(default_factory=list, description="Ids des parties à retirer")
    research_queries: ResearchQueries = Field(default_factory=ResearchQueries)
    assembly_instructions: str = Field(default="", description="Consignes d'assemblage mises à jour ; vide = inchangé")
