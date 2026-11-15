"""Commandes `python -m router tools …`."""

from __future__ import annotations

import sys

from ..errors import ConfigError
from .core import Toolbox

STATE_MARK = {"en marche": "●", "installé": "◐", "absent": "○", "manuel": "·"}


def run_tools(config, args) -> int:
    box = Toolbox(config, dry_run=getattr(args, "dry_run", False))
    action = args.tools_command

    if action in ("list", "status"):
        wanted = [args.tool] if getattr(args, "tool", None) else list(box.catalog)
        for tool_id in wanted:
            status = box.status(tool_id)
            spec = box.spec(tool_id)
            mark = STATE_MARK.get(status.state, "·")
            where = f"  {status.url}" if status.url and status.state == "en marche" else ""
            print(f"{mark} {spec.name:<20} remplace {spec.replaces:<26} {status.state}{where}")
            if action == "status" or getattr(args, "tool", None):
                print(f"    {spec.summary}")
                print(f"    Dépôt : {spec.repo}  ({spec.license})  — vérifié sur : {spec.verified}")
                print(f"    Dans le routeur : {' '.join(spec.router.split())}")
                if spec.needs:
                    print(f"    Prérequis : {', '.join(spec.needs)}")
                for warning in spec.warnings:
                    print(f"    ! {warning}")
        if action == "list":
            print("\n● en marche  ◐ installé  ○ absent  · manuel")
            print("Détail : python -m router tools status <outil>   Installer : python -m router tools install <outil|--all>")
        return 0

    if action == "install":
        targets = [t for t in box.catalog if box.spec(t).automated] if args.all else [args.tool]
        if not args.all and not args.tool:
            print("Indique un outil, ou --all. Outils : " + ", ".join(box.catalog), file=sys.stderr)
            return 2
        failures = 0
        for tool_id in targets:
            try:
                box.install(tool_id, gpu=args.gpu)
            except ConfigError as exc:
                failures += 1
                print(f"  ✗ {exc}", file=sys.stderr)
        if args.all:
            print("\nOutils manuels (le routeur n'exécute rien pour eux) :")
            for tool_id in box.catalog:
                if not box.spec(tool_id).automated:
                    print(f"  - {box.spec(tool_id).name} : python -m router tools install {tool_id}")
            print("\nRien n'est démarré. Démarrer un outil : python -m router tools start <outil>")
        return 1 if failures else 0

    if action in ("start", "stop"):
        try:
            getattr(box, action)(args.tool)
        except ConfigError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            return 1
        return 0
    return 2
