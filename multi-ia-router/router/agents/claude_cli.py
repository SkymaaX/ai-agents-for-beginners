"""Claude via Claude Code en mode headless : `claude -p --output-format json`.

Deux usages, selon la tâche :
- rédaction / critique / assemblage : aucun outil (ou `Read` pour regarder une image) ;
- exécutant technique (tâche de type fichiers) : outils de fichiers dans le dossier de la partie.

Avantage : passe par l'abonnement Claude (pas de facturation au token). Limite : quota
d'abonnement par fenêtre de temps → en cas de plafond atteint, le projet se met en pause.

Le prompt passe par l'entrée standard et le prompt système par un fichier : aucun texte long
sur la ligne de commande (limite de longueur et problèmes de guillemets sous Windows).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import AgentError, AgentTimeout, ConfigError, QuotaError
from ..jsonutil import portable_schema
from .base import Agent, AgentResult, AgentTask, Usage

_LIMIT = re.compile(r"usage limit|limit reached|rate.?limit|quota|credit balance|out of credits|overage", re.IGNORECASE)
_AUTH = re.compile(r"invalid api key|/login|not logged in|authentication|unauthorized|oauth", re.IGNORECASE)


class ClaudeCliAgent(Agent):
    type_name = "claude_cli"
    default_capabilities = frozenset({"text", "vision", "files"})

    def __init__(self, name, cfg, runtime):
        super().__init__(name, cfg, runtime)
        self._command: list[str] | None = None

    # ------------------------------------------------------------------------- commande
    def command(self) -> list[str]:
        if self._command is None:
            raw = self.cfg.get("command", "claude")
            if isinstance(raw, (list, tuple)):
                self._command = [str(part) for part in raw]
            else:
                found = shutil.which(str(raw))
                if not found:
                    raise ConfigError(
                        f"Claude Code introuvable (« {raw} »). Installe-le puis lance `claude` une fois pour te connecter."
                    )
                self._command = [found]
        return self._command

    @property
    def native_json(self) -> bool:  # type: ignore[override]
        """`--json-schema` garantit le JSON… sauf via un lanceur .cmd/.bat (Windows, installation npm),
        où un schéma en argument casserait les guillemets : le schéma part alors dans le prompt."""
        try:
            return not self.command()[0].lower().endswith((".cmd", ".bat"))
        except ConfigError:
            return True

    # ---------------------------------------------------------------------------- appel
    def _call(self, task: AgentTask, prompt: str) -> AgentResult:
        cfg = self.cfg
        executing = task.kind == "files"
        if executing:
            tools = list(cfg.get("tools", ["Read", "Write", "Edit", "Glob", "Grep"]))
            if not tools:
                raise ConfigError(f"L'agent « {self.name} » n'a aucun outil : il ne peut pas produire de fichiers.")
        else:
            tools = ["Read"] if task.attachments else []

        scratch = Path(tempfile.mkdtemp(prefix="router-claude-"))
        try:
            workdir = task.workdir if executing and task.workdir else scratch
            workdir.mkdir(parents=True, exist_ok=True)
            args = ["-p", "--output-format", "json", "--no-session-persistence", "--tools", ",".join(tools)]
            # Par défaut on n'hérite pas des réglages personnels (plugins de style, hooks…) :
            # ils déformeraient les livrables.
            sources = cfg.get("setting_sources", "project")
            if sources is not None:
                args += ["--setting-sources", str(sources)]
            if cfg.model:
                args += ["--model", cfg.model]
            if cfg.get("effort"):
                args += ["--effort", str(cfg.get("effort"))]
            if cfg.get("max_budget_usd"):
                args += ["--max-budget-usd", str(cfg.get("max_budget_usd"))]
            if tools:
                args += ["--allowedTools", ",".join(tools)]
            if executing:
                args += ["--permission-mode", str(cfg.get("permission_mode", "acceptEdits"))]
            if task.system:
                system_file = scratch / "system.md"
                system_file.write_text(task.system, encoding="utf-8")
                # Exécutant : on garde le prompt système de Claude Code (il sait manier ses outils).
                args += ["--append-system-prompt-file" if executing else "--system-prompt-file", str(system_file)]
            if task.schema is not None and self.native_json:
                args += ["--json-schema", json.dumps(portable_schema(task.schema, descriptions=False), ensure_ascii=True)]
            if task.attachments:
                for directory in sorted({str(p.resolve().parent) for p in task.attachments}):
                    args += ["--add-dir", directory]
                listing = "\n".join(f"- {p.resolve()}" for p in task.attachments)
                prompt += f"\n\nImages à examiner avant de répondre (ouvre-les avec l'outil Read) :\n{listing}"

            try:
                proc = subprocess.run(
                    self.command() + args, input=prompt, cwd=str(workdir), capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=cfg.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise AgentTimeout(f"Claude Code n'a pas terminé en {cfg.timeout:.0f} s") from exc
            except OSError as exc:
                raise AgentError(f"Impossible de lancer Claude Code : {exc}", retryable=False) from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        return self._parse(proc, task)

    def _parse(self, proc: subprocess.CompletedProcess, task: AgentTask) -> AgentResult:
        stdout = (proc.stdout or "").strip()
        data: dict = {}
        if stdout:
            try:
                data = json.loads(stdout.splitlines()[-1])
            except json.JSONDecodeError:
                try:
                    data = json.loads(stdout)
                except json.JSONDecodeError:
                    data = {}
        if not isinstance(data, dict):
            data = {}
        message = str(data.get("result") or "") or (proc.stderr or "").strip() or stdout[:500]
        failed = proc.returncode != 0 or bool(data.get("is_error")) or not data
        if failed:
            status = data.get("api_error_status")
            detail = f"Claude Code a échoué ({data.get('subtype') or f'code {proc.returncode}'}) : {message[:600]}"
            if status == 429 or _LIMIT.search(message):
                raise QuotaError(detail + " — quota ou limite d'usage atteint.")
            if status in (401, 403) or _AUTH.search(message):
                raise AgentError(detail + " — lance `claude` une fois pour te connecter.", retryable=False)
            if data.get("subtype") == "error_max_budget_usd":
                raise AgentError(detail, retryable=False)
            raise AgentError(detail, retryable=True)

        usage_raw = data.get("usage") or {}
        usage = Usage(
            tokens_in=int(usage_raw.get("input_tokens") or 0)
            + int(usage_raw.get("cache_creation_input_tokens") or 0)
            + int(usage_raw.get("cache_read_input_tokens") or 0),
            tokens_out=int(usage_raw.get("output_tokens") or 0),
            cost_usd=data.get("total_cost_usd"),
        )
        structured = data.get("structured_output") if task.schema is not None else None
        text = str(data.get("result") or "")
        if structured is not None and not text:
            text = json.dumps(structured, ensure_ascii=False)
        if not text and structured is None:
            raise AgentError("Claude Code a renvoyé une réponse vide.", retryable=True)
        denials = data.get("permission_denials") or []
        if denials:
            self.runtime.event(
                "permission_denied", stage=task.stage, agent=self.name, part_id=task.part_id,
                tools=sorted({str(d.get("tool_name", "?")) for d in denials if isinstance(d, dict)}),
            )
        model = next(iter(data.get("modelUsage") or {}), "") or self.cfg.model
        return AgentResult(
            text=text, data=structured, usage=usage, model=model,
            extra={"num_turns": data.get("num_turns"), "permission_denials": len(denials)},
        )

    def diagnose(self) -> list[str]:
        try:
            proc = subprocess.run(
                self.command() + ["--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
        except (ConfigError, OSError, subprocess.TimeoutExpired) as exc:
            return [f"✗ {exc}"]
        version = (proc.stdout or proc.stderr or "").strip()
        lines = [f"✓ {version or 'Claude Code présent'} ({self.command()[0]})"]
        if not self.native_json:
            lines.append("  lanceur .cmd détecté : schéma JSON transmis dans le prompt (plus robuste sous Windows)")
        return lines
