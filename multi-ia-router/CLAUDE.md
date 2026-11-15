# Routeur multi-IA — repères pour modifier ce projet

Projet autonome dans `multi-ia-router/` (le reste du dépôt est un cours sans rapport : ne pas y toucher).
Orchestrateur Python d'une équipe d'agents IA : chef (Qwen local), répartiteur (Gemini), producteurs,
critique croisée, assembleur (Claude), exécutant (Claude Code). L'utilisateur est francophone :
messages, prompts, README et commentaires en français ; identifiants de code en anglais.

## Commandes

```bash
python -m pytest                       # toute la suite, sans aucun appel réel (~10 s)
python -m pytest tests/test_pipeline.py -q
python -m router --mock run "demande"  # pipeline complet en simulation
python -m router dashboard             # http://127.0.0.1:8765
python -m router doctor                # détection du PC et des connexions
```

Après toute modification : `python -m pytest` doit rester vert. Une modification de comportement
vient avec son test.

## Où changer quoi

| Besoin | Fichier |
|---|---|
| Qui fait quoi, seuils, chemins, modèles | `config.yaml` (défauts), `config.local.yaml` (PC de l'utilisateur, non versionné) |
| Nouveau réglage | modèle Pydantic dans `router/config.py` + valeur commentée dans `config.yaml` |
| Texte d'un prompt | `router/prompts/<nom>.md` (variables `{{nom}}`, toutes obligatoires) |
| Forme du plan, de la critique, de l'arbitrage, du verdict | `router/schemas.py` (le prompt et la validation suivent) |
| Une étape du pipeline | `router/pipeline/<étape>.py` ; ordre des étapes : `STAGES` dans `router/state.py` |
| Fabrication d'une version de partie (texte, fichiers, image, Blender) | `router/pipeline/parts.py` |
| Boucle de critique et arbitrage | `router/pipeline/review.py` |
| Modification ciblée d'un projet terminé | `router/pipeline/replan.py` + `prepare_revision` dans `orchestrator.py` |
| Nouvel agent | `router/agents/<nom>.py` + entrée dans `AGENT_TYPES` (`router/agents/__init__.py`) |
| Retries, journalisation, réduction de contexte, JSON invalide | `router/agents/base.py` (commun à tous les agents) |
| Recherche web ou vault | `router/research/` |
| Tableau de bord | API : `router/dashboard/app.py` ; page : `router/dashboard/static/` |
| Diagnostic | `router/doctor.py` |
| Outils complémentaires (catalogue, installation, état) | `router/toolbox/catalog.yaml` (faits vérifiés) + `core.py` + `cli.py` |
| Skills Claude Code du top 10 (installation guidée) | `installer-skills.sh` ; noms relevés dans les `marketplace.json` officiels, à garder alignés avec `tests/test_skills_installer.py` |
| Voix de synthèse (VoiceStudio, MCP) | `router/agents/voicestudio.py` ; partie `audio` : `router/pipeline/parts.py` |

## Règles de conception à respecter

- **Interface unique.** Tout agent hérite de `Agent` et n'implémente que `_call`. Ne pas recoder
  retries, délais ou journaux dans un agent. Erreurs : `AgentError(retryable=…)`, `QuotaError`
  (met le projet en pause), `AgentTimeout`, `ConfigError`.
- **Rien en dur.** Rôles, modèles, URL, seuils viennent de la config. Les clés viennent de `.env`
  et ne sont jamais écrites, journalisées ni affichées.
- **Personne ne juge sa propre copie.** `Config.critic_name_for` refuse un critique de la même
  `family` que l'auteur. Ne pas contourner.
- **Tout est repris.** Chaque étape écrit ses fichiers puis l'état (`Project.save`, écriture
  atomique). Une étape doit pouvoir être relancée sans refaire ce qui est déjà sur disque
  (voir `review.py` : une critique existante est relue, pas redemandée).
- **Chemins.** Tous les chemins d'un projet passent par `router/state.py::Project`. Les noms de
  dossiers (`03_parties`, `04_livrable`…) ne sont écrits nulle part ailleurs.
- **JSON des agents.** Toujours via `ask_json(agent, task, Modèle, validator=…)` : validation
  Pydantic, puis renvoi à l'agent avec l'erreur si invalide.
- **Consigne et contexte séparés.** `AgentTask.prompt` n'est jamais coupé ; `AgentTask.context`
  (documents) est réduit pour les modèles à petit contexte (`max_context_chars`).
- **Simulation.** `MockAgent` (`router/agents/mock.py`) doit savoir répondre à tout nouveau schéma
  ou rôle (méthode `_json_<nomduschéma>`), sinon le mode `--mock` et les tests cassent.
- **Tableau de bord 100 % local.** Aucune ressource externe (pas de CDN). Tout texte venant des
  modèles passe par `esc()` côté JS ; le Markdown est rendu côté serveur avec le HTML brut désactivé.
- **Portabilité Windows.** `pathlib`, `encoding="utf-8"` explicite, commandes en liste, aucun texte
  long en argument de ligne de commande (stdin ou fichier).
- **Exécutant sans commandes par défaut.** `claude_code` n'a que des outils de fichiers ; c'est le
  routeur qui lance Blender (`router/tools/blender.py`). Ne pas ajouter `Bash` par défaut.

- **Outils complémentaires.** Le catalogue ne contient que des faits relevés dans les fichiers officiels
  du projet (README, compose, .env.example) ; ne rien y ajouter de mémoire. Un outil n'est `automatisé`
  que si ses ports, variables et secrets sont vérifiés. Jamais de `curl | sh`, jamais `down`, `rm`, `-v`.
  Les secrets sont générés, jamais affichés, un `.env` existant n'est jamais réécrit.
- **Partie `audio`.** Elle lit le texte de la partie dont elle dépend (`depends_on`) ; si cette partie
  change, `propagate_to_audio` la remet à refaire. Le relecteur ne peut pas écouter : ne pas lui demander.
- **Skills Claude Code.** `installer-skills.sh` ne lance rien sans « o », sans `sudo`, sans `curl | sh`. Les skills ne sont jamais branchés dans les agents `claude_cli` (`--setting-sources project`). Un nom de plugin ne s'écrit pas de mémoire : lire le `marketplace.json` du dépôt.
- **Client MCP.** Il lit le schéma annoncé par le serveur (`tools/list`) pour nommer les paramètres.
  Le SDK `mcp` 2.x masque les codes HTTP : voir `_probe_status` pour distinguer clé refusée et panne.

## Tests

- `tests/conftest.py::make_config` : config de simulation isolée dans un dossier temporaire.
- Les tests chargent la config avec `use_local=False` : ni `config.local.yaml` ni `.env` de la
  machine ne sont lus, et les variables de clés sont retirées de l'environnement.
- `config.mock_scenario` force des situations (critique toujours négative, verdict en échec,
  panne, quota, JSON invalide) : voir l'en-tête de `router/agents/mock.py`.
- `tests/test_voicestudio.py` lance un vrai serveur MCP en local (uvicorn) ; `tests/test_toolbox.py`
  valide la surcharge Chatwoot avec le vrai `docker compose config` (ignoré si Docker est absent).
- Agents réels testés contre de faux serveurs (`httpx.MockTransport`) et de faux binaires
  (`tests/fakes/fake_claude.py`, `tests/fakes/fake_blender.py`). Aucun test ne doit appeler un
  service réel ni dépenser de tokens.

## Limites connues

- AnythingLLM, Gemini, ComfyUI et Blender réels n'ont pas pu être testés à la construction
  (machine sans GPU ni clés) : seulement contre des faux. `claude -p` a été testé en réel.
- Les images Docker des outils complémentaires n'ont jamais été lancées (pas de démon Docker à la construction).
- `installer-skills.sh` n'a jamais lancé les vrais `claude plugin install`, `uv` ou `npx` : testé avec de faux binaires.
- Les noms de modèles Gemini de `config.yaml` sont à confirmer avec `doctor`.
