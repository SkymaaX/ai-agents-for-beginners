"""Erreurs du routeur. Une seule hiérarchie, pour décider : réessayer, mettre en pause, ou échouer."""

from __future__ import annotations


class RouterError(Exception):
    """Base de toutes les erreurs du routeur."""


class ConfigError(RouterError):
    """Configuration invalide (rôle inconnu, clé manquante, critique = auteur...)."""


class AgentError(RouterError):
    """Échec d'un appel à un agent.

    retryable=True  : erreur passagère (réseau, 5xx, surcharge) → nouvel essai.
    retryable=False : inutile de réessayer tel quel (requête invalide, refus...).
    pause=True      : si les essais sont épuisés, mettre le projet en pause (limite de débit)
                      plutôt qu'en échec.
    """

    def __init__(self, message: str, *, retryable: bool = True, agent: str = "", pause: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.agent = agent
        self.pause = pause


class AgentTimeout(AgentError):
    """L'agent n'a pas répondu dans le délai configuré."""

    def __init__(self, message: str, *, agent: str = ""):
        super().__init__(message, retryable=True, agent=agent)


class QuotaError(AgentError):
    """Quota ou crédit épuisé : le projet passe en pause, reprise plus tard avec `resume`."""

    def __init__(self, message: str, *, agent: str = ""):
        super().__init__(message, retryable=False, agent=agent)


class InvalidOutputError(AgentError):
    """Sortie inexploitable (JSON invalide même après demandes de correction)."""

    def __init__(self, message: str, *, agent: str = ""):
        super().__init__(message, retryable=False, agent=agent)


class PipelinePaused(RouterError):
    """Le pipeline s'arrête proprement ; l'état est sauvegardé pour une reprise."""
