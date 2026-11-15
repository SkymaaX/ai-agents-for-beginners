"""Nom de la commande à afficher dans les messages d'aide, selon le système.

Beaucoup de Linux n'ont pas de commande `python` (seulement `python3`) : conseiller
`python -m router …` y mène à une erreur. Le script `routeur` du projet marche partout
sans rien activer : `./routeur` (Linux, macOS) ou `routeur` (Windows).
"""

from __future__ import annotations

import os

CMD = "routeur" if os.name == "nt" else "./routeur"
