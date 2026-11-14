"""Ligne de commande : python -m router <commande>.

  doctor                      détecte ton PC et vérifie les connexions
  run "ma demande"            lance un projet
  resume <projet>             reprend un projet arrêté
  revise <projet> "consigne"  modifie un projet terminé sans tout refaire
  list / status <projet>      historique et détail
  dashboard                   tableau de bord web local
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .errors import RouterError
from .logs import setup_logging
from .state import STAGE_LABELS, STAGES, list_projects

STATUS_LABELS = {"new": "nouveau", "running": "en cours", "paused": "en pause", "failed": "échec", "done": "terminé"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m router", description="Routeur multi-IA")
    parser.add_argument("--config", help="chemin d'un autre config.yaml")
    parser.add_argument("--mock", action="store_true", help="simulation : aucun appel réel")
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="détecte le PC et vérifie les connexions")
    doctor.add_argument("--write", action="store_true", help="écrit les valeurs détectées dans config.local.yaml")

    run = sub.add_parser("run", help="lance un projet")
    run.add_argument("request", nargs="?", help="la demande (entre guillemets)")
    run.add_argument("-f", "--file", help="lire la demande depuis un fichier texte")

    resume = sub.add_parser("resume", help="reprend un projet arrêté")
    resume.add_argument("project", help="identifiant du projet (ou un fragment unique)")

    revise = sub.add_parser("revise", help="modifie un projet terminé")
    revise.add_argument("project")
    revise.add_argument("instruction", help="la modification demandée (entre guillemets)")

    sub.add_parser("list", help="liste les projets")
    status = sub.add_parser("status", help="détail d'un projet")
    status.add_argument("project")

    dashboard = sub.add_parser("dashboard", help="tableau de bord web local")
    dashboard.add_argument("--port", type=int)
    dashboard.add_argument("--host")
    return parser


def _print_state(state, directory: Path | None = None) -> None:
    print(f"\nProjet  : {state.id}")
    print(f"Titre   : {state.title or '—'}")
    print(f"État    : {STATUS_LABELS.get(state.status, state.status)}{' (simulation)' if state.mock else ''}")
    for stage in STAGES:
        entry = state.stages.get(stage)
        if entry is None or entry.status == "skipped":
            continue
        mark = {"done": "✓", "running": "…", "error": "✗", "pending": "·"}.get(entry.status, "·")
        print(f"  {mark} {STAGE_LABELS[stage]}")
    for part_id, part in state.parts.items():
        print(f"  partie {part_id:<6} {part.status:<10} v{part.accepted_version or part.version}  par {part.author}")
    if state.error:
        print(f"Erreur  : {state.error}")
    if state.reserves:
        print("Réserves : " + " ; ".join(state.reserves))
    if directory is not None:
        final = directory / "04_livrable" / "LIVRABLE.md"
        if state.status == "done" and final.exists():
            print(f"Livrable : {final}")
        print(f"Dossier  : {directory}")
    if state.status in ("paused", "failed"):
        print(f"Reprendre : python -m router resume {state.id}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # consoles Windows : accents et symboles sans plantage
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        overrides = {"mock": True} if args.mock else None
        # `doctor` doit pouvoir tourner même si la config est encore incomplète.
        config = load_config(args.config, overrides=overrides, check=args.command not in ("doctor", "list", "status"))
        setup_logging(config.root / "logs", verbose=args.verbose)

        if args.command == "doctor":
            from .doctor import run_doctor

            return run_doctor(config, write=args.write)

        if args.command == "dashboard":
            from .dashboard.app import serve

            serve(config, host=args.host, port=args.port)
            return 0

        from .pipeline import orchestrator

        if args.command == "run":
            request = Path(args.file).read_text(encoding="utf-8") if args.file else (args.request or "")
            if not request.strip():
                print("Demande vide. Exemple : python -m router run \"Rédige un guide sur…\"", file=sys.stderr)
                return 2
            state = orchestrator.start(config, request)
            _print_state(state, config.projects_dir / state.id)
            return 0 if state.status == "done" else 1

        if args.command == "resume":
            state = orchestrator.resume(config, args.project)
            _print_state(state, config.projects_dir / state.id)
            return 0 if state.status == "done" else 1

        if args.command == "revise":
            state = orchestrator.revise(config, args.project, args.instruction)
            _print_state(state, config.projects_dir / state.id)
            return 0 if state.status == "done" else 1

        if args.command == "list":
            states = list_projects(config.projects_dir)
            if not states:
                print("Aucun projet pour l'instant.")
            for state in states:
                label = STATUS_LABELS.get(state.status, state.status)
                print(f"{state.id:<60} {label:<10} {state.title}")
            return 0

        if args.command == "status":
            project = orchestrator.find_project(config, args.project)
            _print_state(project.state, project.dir)
            return 0
    except RouterError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompu. L'état est sauvegardé : `python -m router resume <projet>` pour reprendre.", file=sys.stderr)
        return 130
    return 0
