"""installer-connecteurs.sh : commandes exactes, clés jamais affichées, rien sans accord.

Aucun vrai `claude` ni `npx` : de faux binaires enregistrent leurs arguments (voir shellbox.py).
Les commandes attendues viennent des README officiels de chaque projet (relevés à la construction) :
perplexityai/modelcontextprotocol, microsoft/playwright-mcp, firecrawl/firecrawl-mcp-server.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT
from shellbox import Sandbox

SCRIPT = ROOT / "installer-connecteurs.sh"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="script shell")

KEY = "pplx-clé-de-test-0123456789"
PLAYWRIGHT = "claude mcp add --scope user playwright -- npx @playwright/mcp@latest"
FIRECRAWL = "claude mcp add --scope user --transport http firecrawl https://mcp.firecrawl.dev/v2/mcp"
FIRECRAWL_OAUTH = "claude mcp add --scope user --transport http firecrawl https://mcp.firecrawl.dev/v2/mcp-oauth"
PERPLEXITY = f"claude mcp add --scope user perplexity -e PERPLEXITY_API_KEY={KEY} -- npx -y @perplexity-ai/mcp-server"


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path, SCRIPT)


def _adds(box: Sandbox) -> list[str]:
    return [call for call in box.calls() if call.startswith("claude mcp add")]


# --- Le fichier lui-même ---------------------------------------------------------------------

@posix_only
def test_script_is_valid_sh_with_unix_line_endings():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n") and b"\r" not in raw


@posix_only
def test_script_is_executable_in_git():
    if shutil.which("git") is None or not (ROOT.parent / ".git").exists():
        pytest.skip("pas un dépôt git")
    out = subprocess.run(["git", "ls-files", "-s", "installer-connecteurs.sh"], cwd=ROOT, capture_output=True, text=True).stdout
    assert out.split()[0] == "100755"


def test_script_never_executes_a_download_or_privileged_command():
    """`curl | sh` peut apparaître dans un message (Composio), jamais comme commande."""
    code = [line.strip() for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
    for line in code:
        if "curl" in line or "| sh" in line or "| bash" in line:
            assert line.startswith("say "), f"commande de téléchargement exécutable : {line}"
        for forbidden in ("wget", "eval ", "rm -", "run sudo", "sudo claude", "chmod "):
            assert forbidden not in line, line


def test_secret_is_never_written_to_a_file():
    code = SCRIPT.read_text(encoding="utf-8")
    assert ">>" not in code.replace("2>/dev/null", "") and "> .env" not in code and "tee " not in code


# --- Simulation, refus ----------------------------------------------------------------------------

@posix_only
def test_simulation_prints_every_command_masks_the_key_and_runs_nothing(sandbox):
    result = sandbox.run("--simulation", "--oui", "firecrawl-compte", env={"PERPLEXITY_API_KEY": KEY})
    assert sandbox.calls() == []
    out = sandbox.run("--simulation", "--oui", env={"PERPLEXITY_API_KEY": KEY})
    assert out.returncode == 0, out.stderr
    assert sandbox.calls() == []
    assert f"$ {PLAYWRIGHT}" in out.stdout and f"$ {FIRECRAWL}" in out.stdout
    assert "$ claude mcp add --scope user perplexity -e PERPLEXITY_API_KEY=*** -- npx -y @perplexity-ai/mcp-server" in out.stdout
    assert KEY not in out.stdout + out.stderr + result.stdout + result.stderr


@posix_only
@pytest.mark.parametrize("answers", ["", "n\n" * 8, "\n" * 8, "peut-être\n" * 8])
def test_without_a_yes_nothing_is_added(sandbox, answers):
    result = sandbox.run(stdin=answers, env={"PERPLEXITY_API_KEY": KEY})
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == []


@posix_only
def test_only_the_confirmed_connector_is_added(sandbox):
    # ordre par défaut : perplexity (non), firecrawl (oui), playwright (non)
    result = sandbox.run(stdin="n\no\nn\n", env={"PERPLEXITY_API_KEY": KEY})
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == [FIRECRAWL]


# --- Installation (faux binaires) -------------------------------------------------------------------

@posix_only
def test_yes_adds_the_three_installable_connectors_with_exact_commands(sandbox):
    result = sandbox.run("--oui", env={"PERPLEXITY_API_KEY": KEY})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _adds(sandbox) == [PERPLEXITY, FIRECRAWL, PLAYWRIGHT]


@posix_only
def test_the_key_never_appears_in_the_scripts_output(sandbox):
    result = sandbox.run("--oui", env={"PERPLEXITY_API_KEY": KEY})
    assert KEY not in result.stdout and KEY not in result.stderr
    assert "PERPLEXITY_API_KEY=***" in result.stdout


@posix_only
def test_oauth_firecrawl_is_only_added_when_named(sandbox):
    default = sandbox.run("--oui", env={"PERPLEXITY_API_KEY": KEY})
    assert FIRECRAWL_OAUTH not in sandbox.calls() and "mcp-oauth" not in default.stdout
    explicit = sandbox.run("firecrawl-compte", "--oui")
    assert FIRECRAWL_OAUTH in sandbox.calls()
    assert "/mcp" in explicit.stdout  # la connexion se termine dans Claude Code


@posix_only
def test_composio_is_explained_and_never_installed(sandbox):
    result = sandbox.run("composio", "--oui")
    assert result.returncode == 0
    assert sandbox.calls() == []
    assert "n'exécute jamais un « curl | sh »" in result.stdout and "INSTALL.md" in result.stdout
    assert "Ignorés : composio" in result.stdout


@posix_only
def test_already_configured_server_is_left_alone(sandbox):
    (sandbox.state / "mcp").write_text("playwright\n", encoding="utf-8")
    result = sandbox.run("playwright", "--oui")
    assert result.returncode == 0 and "déjà configuré" in result.stdout
    assert sandbox.calls() == []


@posix_only
def test_oauth_firecrawl_refuses_to_add_a_second_firecrawl_entry(sandbox):
    (sandbox.state / "mcp").write_text("firecrawl\n", encoding="utf-8")
    result = sandbox.run("firecrawl-compte", "--oui")
    assert "déjà configuré" in result.stdout and "claude mcp remove firecrawl" in result.stdout
    assert sandbox.calls() == []


@posix_only
def test_one_failure_does_not_stop_the_others_and_sets_the_exit_code(sandbox):
    result = sandbox.run("--oui", env={"PERPLEXITY_API_KEY": KEY, "FAKE_FAIL": "perplexity"})
    assert result.returncode == 1
    assert FIRECRAWL in sandbox.calls() and PLAYWRIGHT in sandbox.calls()
    assert "Échecs : perplexity" in result.stdout
    assert KEY not in result.stdout + result.stderr


@posix_only
def test_unknown_connector_is_refused_before_anything_runs(sandbox):
    result = sandbox.run("playwright", "pas-un-connecteur", "--oui")
    assert result.returncode == 2 and "pas-un-connecteur" in result.stderr and "perplexity" in result.stderr
    assert sandbox.calls() == []


# --- Clé Perplexity -------------------------------------------------------------------------------------

@posix_only
def test_without_a_key_and_without_a_terminal_perplexity_fails_with_a_hint(sandbox):
    result = sandbox.run("perplexity", "--oui")
    assert result.returncode == 1 and "PERPLEXITY_API_KEY" in result.stderr
    assert _adds(sandbox) == []


@posix_only
def test_key_typed_in_a_real_terminal_is_not_echoed_and_reaches_claude(sandbox):
    """Terminal simulé (pty) : l'écho est coupé avant l'invite, puis rétabli."""
    master, slave = os.openpty()
    proc = subprocess.Popen(
        ["/bin/sh", str(SCRIPT), "perplexity", "--oui"], stdin=slave, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=sandbox.env, close_fds=True,
    )
    os.close(slave)
    seen_on_terminal = b""
    out = b""
    try:
        deadline = time.monotonic() + 15
        while b"invisible" not in out and time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                out += chunk
        assert b"invisible" in out, out
        os.write(master, (KEY + "\n").encode("utf-8"))
        proc.wait(timeout=15)
        while select.select([master], [], [], 0.3)[0]:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            seen_on_terminal += data
        out += proc.stdout.read()
        errors = proc.stderr.read()
    finally:
        proc.kill() if proc.poll() is None else None
        os.close(master)
        proc.stdout.close()
        proc.stderr.close()
    assert proc.returncode == 0, out + errors
    assert KEY.encode("utf-8") not in seen_on_terminal, "la clé a été affichée par le terminal"
    assert KEY.encode("utf-8") not in out + errors
    assert _adds(sandbox) == [PERPLEXITY]


# --- Prérequis ----------------------------------------------------------------------------------------------

@posix_only
def test_missing_claude_is_explained_and_nothing_is_tried(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("npx",))
    result = box.run("playwright", "--oui")
    assert result.returncode == 1 and "Manque : claude" in result.stderr
    assert box.calls() == []


@posix_only
@pytest.mark.parametrize("connector", ["playwright", "perplexity"])
def test_connectors_run_through_npx_need_it(tmp_path, connector):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude",))
    result = box.run(connector, "--oui", env={"PERPLEXITY_API_KEY": KEY})
    assert result.returncode == 1 and "Manque : npx" in result.stderr
    assert box.calls() == []


@posix_only
def test_firecrawl_needs_no_node(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude",))
    result = box.run("firecrawl", "--oui")
    assert result.returncode == 0, result.stderr
    assert box.calls() == [FIRECRAWL]


# --- Liste ---------------------------------------------------------------------------------------------------

@posix_only
def test_list_names_every_connector_and_runs_nothing(sandbox):
    result = sandbox.run("--liste")
    ids = [line.split()[0] for line in result.stdout.splitlines()]
    assert result.returncode == 0 and ids == ["perplexity", "firecrawl", "firecrawl-compte", "playwright", "composio"]
    assert sandbox.calls() == []
    assert Path(SCRIPT).exists()
