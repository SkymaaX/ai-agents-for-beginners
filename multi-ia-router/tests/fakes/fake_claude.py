"""Faux binaire `claude` pour les tests : imite `claude -p --output-format json`.

Le comportement est piloté par des mots-clés dans le prompt reçu sur l'entrée standard.
Les arguments reçus sont recopiés dans le champ `echo_args` pour vérification.
"""

import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


def emit(result="", **extra):
    payload = {
        "type": "result", "subtype": "success", "is_error": False, "result": result, "num_turns": 1,
        "total_cost_usd": 0.0123, "duration_ms": 42,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5, "output_tokens": 7},
        "modelUsage": {"claude-test-model": {}}, "permission_denials": [],
        "echo_args": args, "echo_cwd": os.getcwd(),
    }
    payload.update(extra)
    print(json.dumps(payload))


if "--version" in args:
    print("9.9.9 (Claude Code factice)")
elif "LIMITE" in prompt:
    emit("Claude AI usage limit reached|1760000000", is_error=True, subtype="error_during_execution")
    sys.exit(1)
elif "PANNE" in prompt:
    emit("API Error: 529 overloaded", is_error=True, api_error_status=529)
    sys.exit(1)
elif "LOGIN" in prompt:
    emit("Invalid API key · Please run /login", is_error=True)
    sys.exit(1)
elif "CRASH" in prompt:
    print("segfault", file=sys.stderr)
    sys.exit(3)
elif value("--json-schema"):
    schema = json.loads(value("--json-schema"))
    data = {key: "valeur" for key in schema.get("properties", {})}
    emit(json.dumps(data), structured_output=data)
elif "ECRIS" in prompt:
    Path("sortie.txt").write_text("fichier écrit par le faux claude", encoding="utf-8")
    system = Path(value("--append-system-prompt-file")).read_text(encoding="utf-8") if value("--append-system-prompt-file") else ""
    emit("Fichier sortie.txt écrit.", echo_system=system, permission_denials=[{"tool_name": "Bash"}])
else:
    system = Path(value("--system-prompt-file")).read_text(encoding="utf-8") if value("--system-prompt-file") else ""
    emit(f"réponse à : {prompt[:40]}", echo_system=system, echo_prompt=prompt)
