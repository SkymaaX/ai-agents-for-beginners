"""Bac à sable pour tester les scripts shell d'installation sans rien installer.

Le PATH ne contient que des faux binaires (qui enregistrent leurs arguments dans un journal) et
quelques outils système. Utilisé par test_skills_installer.py et test_connectors_installer.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FAKE_CLAUDE = """#!/bin/sh
echo "claude $*" >> "$FAKE_LOG"
case "$1 $2 $3" in
  "plugin list "*) cat "$FAKE_STATE/plugins" 2>/dev/null; exit 0 ;;
  "plugin marketplace list") cat "$FAKE_STATE/markets" 2>/dev/null; exit 0 ;;
esac
case "$1 $2" in
  "mcp get") grep -qx "$3" "$FAKE_STATE/mcp" 2>/dev/null && exit 0; exit 1 ;;
esac
if [ -n "${FAKE_FAIL:-}" ]; then
  case "$*" in *"$FAKE_FAIL"*) echo "erreur simulée" >&2; exit 1 ;; esac
fi
exit 0
"""
FAKE_GRAPHIFY = '#!/bin/sh\necho "graphify $*" >> "$FAKE_LOG"\n'
FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$FAKE_LOG"
if [ "$1 $2" = "tool install" ]; then
  printf '#!/bin/sh\\necho "graphify $*" >> "$FAKE_LOG"\\n' > "$FAKE_BIN/graphify"
  chmod +x "$FAKE_BIN/graphify"
fi
"""
FAKE_RECORDER = '#!/bin/sh\necho "{name} $*" >> "$FAKE_LOG"\n'

# Appels qui ne changent rien : ils ne comptent pas dans `calls()`.
READ_ONLY_PREFIXES = ("claude plugin list", "claude plugin marketplace list", "claude mcp get")


class Sandbox:
    """PATH réduit à des faux binaires, plus quelques outils système (grep, cat, sed, chmod, stty, python3)."""

    def __init__(self, tmp: Path, script: Path, *, tools: tuple[str, ...] = ("claude", "node", "npx", "uv")):
        self.script = script
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.state = tmp / "state"
        self.state.mkdir()
        self.log = tmp / "calls.log"
        for real in ("grep", "python3", "cat", "sed", "chmod", "stty"):
            found = shutil.which(real)
            assert found, f"{real} introuvable pour le test"
            (self.bin / real).symlink_to(found)
        scripts = {"claude": FAKE_CLAUDE, "uv": FAKE_UV, "graphify": FAKE_GRAPHIFY}
        for name in tools:
            path = self.bin / name
            path.write_text(scripts.get(name, FAKE_RECORDER.format(name=name)), encoding="utf-8")
            path.chmod(0o755)
        self.env = {
            "PATH": str(self.bin), "HOME": str(tmp), "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.state), "FAKE_BIN": str(self.bin),
        }

    def run(self, *args: str, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(self.script), *args], input=stdin, capture_output=True, text=True,
            env={**self.env, **(env or {})}, timeout=60,
        )

    def calls(self) -> list[str]:
        """Appels qui changent quelque chose (les lectures ne comptent pas)."""
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if not line.startswith(READ_ONLY_PREFIXES)]

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""
