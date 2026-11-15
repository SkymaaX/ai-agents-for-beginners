"""installer-skills.sh : commandes exactes, confirmation, reprise sur échec, aucune action sans accord.

Aucun vrai `claude`, `uv` ni `npx` n'est lancé : des faux binaires enregistrent leurs arguments.
Les noms de plugins et de marketplaces attendus viennent des `.claude-plugin/marketplace.json`
officiels de chaque dépôt (relevés à la construction) ; si un dépôt les change, ce test le dira
à celui qui met le script à jour, pas à l'utilisateur.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from shellbox import Sandbox

SCRIPT = ROOT / "installer-skills.sh"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="script shell")

# id du script -> (dépôt de la marketplace, nom de la marketplace, nom du plugin)
PLUGINS = {
    "superpowers": ("obra/superpowers", "superpowers-dev", "superpowers"),
    "ponytail": ("DietrichGebert/ponytail", "ponytail", "ponytail"),
    "ui-ux-pro-max": ("nextlevelbuilder/ui-ux-pro-max-skill", "ui-ux-pro-max-skill", "ui-ux-pro-max"),
    "caveman": ("JuliusBrussee/caveman", "caveman", "caveman"),
    "agent-skills": ("addyosmani/agent-skills", "addy-agent-skills", "agent-skills"),
    "understand-anything": ("Egonex-AI/Understand-Anything", "understand-anything", "understand-anything"),
    "impeccable": ("pbakaus/impeccable", "impeccable", "impeccable"),
}
ARCHIFY = "npx -y skills@latest add https://archify.si --skill archify --agent claude-code --global --copy --yes"

@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path, SCRIPT)


# --- Le fichier lui-même ----------------------------------------------------------------------

@posix_only
def test_script_is_valid_sh_with_unix_line_endings():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n") and b"\r" not in raw


@posix_only
def test_script_is_executable_in_git():
    if shutil.which("git") is None or not (ROOT.parent / ".git").exists():
        pytest.skip("pas un dépôt git")
    out = subprocess.run(["git", "ls-files", "-s", "installer-skills.sh"], cwd=ROOT, capture_output=True, text=True).stdout
    assert out.split()[0] == "100755"


def test_script_never_pipes_to_a_shell_or_uses_privileges():
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("curl", "wget", "| sh", "| bash", "eval ", "rm -", "run sudo", "chmod ", "sudo claude"):
        assert forbidden not in code, forbidden


# --- Simulation et refus : rien ne s'exécute -------------------------------------------------------

@posix_only
def test_simulation_prints_every_command_and_runs_nothing(sandbox):
    result = sandbox.run("--simulation", "--oui")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == []
    for repo, market, plugin in PLUGINS.values():
        assert f"$ claude plugin marketplace add {repo}" in result.stdout
        assert f"$ claude plugin install {plugin}@{market}" in result.stdout
    assert "$ uv tool install graphifyy" in result.stdout and "$ graphify install" in result.stdout
    assert f"$ {ARCHIFY}" in result.stdout


@posix_only
@pytest.mark.parametrize("answers", ["", "n\n" * 12, "\n" * 12, "peut-être\n" * 12])
def test_without_a_yes_nothing_is_installed(sandbox, answers):
    result = sandbox.run(stdin=answers)
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == []
    assert "Installés" not in result.stdout


@posix_only
def test_only_the_confirmed_skill_is_installed(sandbox):
    result = sandbox.run(stdin="o\n")  # oui au premier (Superpowers), puis fin de l'entrée = non
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == ["claude plugin marketplace add obra/superpowers", "claude plugin install superpowers@superpowers-dev"]


# --- Installation réelle (faux binaires) -----------------------------------------------------------

@posix_only
def test_yes_installs_every_skill_with_the_exact_commands(sandbox):
    result = sandbox.run("--oui")
    assert result.returncode == 0, result.stderr
    calls = sandbox.calls()
    for repo, market, plugin in PLUGINS.values():
        assert f"claude plugin marketplace add {repo}" in calls
        assert f"claude plugin install {plugin}@{market}" in calls
        # la marketplace est ajoutée avant l'installation
        assert calls.index(f"claude plugin marketplace add {repo}") < calls.index(f"claude plugin install {plugin}@{market}")
    assert calls.index("uv tool install graphifyy") < calls.index("graphify install")
    assert ARCHIFY in calls
    assert not any("awesome" in call for call in calls), "la liste Awesome n'est jamais installée"
    assert "Awesome Claude Skills" in result.stdout and "Rien n'a été fait" in result.stdout


@posix_only
def test_selection_installs_only_the_named_skills(sandbox):
    result = sandbox.run("caveman", "--oui")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == ["claude plugin marketplace add JuliusBrussee/caveman", "claude plugin install caveman@caveman"]


@posix_only
def test_unknown_skill_is_refused_before_anything_runs(sandbox):
    result = sandbox.run("caveman", "pas-un-skill", "--oui")
    assert result.returncode == 2
    assert "pas-un-skill" in result.stderr and "superpowers" in result.stderr
    assert sandbox.calls() == []


@posix_only
def test_already_installed_plugin_is_left_alone(sandbox):
    (sandbox.state / "plugins").write_text("ux-superpowers@autre\ncaveman@caveman  v1.0\n", encoding="utf-8")
    result = sandbox.run("caveman", "--oui")
    assert result.returncode == 0 and "Déjà installé" in result.stdout
    assert sandbox.calls() == []


@posix_only
def test_a_similar_plugin_name_does_not_count_as_installed(sandbox):
    """`ux-superpowers` n'est pas `superpowers`."""
    (sandbox.state / "plugins").write_text("ux-superpowers@autre\n", encoding="utf-8")
    result = sandbox.run("superpowers", "--oui")
    assert result.returncode == 0
    assert "claude plugin install superpowers@superpowers-dev" in sandbox.calls()


@posix_only
def test_one_failure_does_not_stop_the_others_and_sets_the_exit_code(sandbox):
    result = sandbox.run("ponytail", "caveman", "--oui", env={"FAKE_FAIL": "ponytail@ponytail"})
    assert result.returncode == 1
    assert "claude plugin install caveman@caveman" in sandbox.calls()
    assert "Échecs : ponytail" in result.stdout and "Installés : caveman" in result.stdout


@posix_only
def test_marketplace_already_added_does_not_block_the_install(sandbox):
    (sandbox.state / "markets").write_text("caveman\n", encoding="utf-8")
    result = sandbox.run("caveman", "--oui", env={"FAKE_FAIL": "marketplace add"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "claude plugin install caveman@caveman" in sandbox.calls()


@posix_only
def test_unknown_marketplace_that_cannot_be_added_is_a_failure(sandbox):
    result = sandbox.run("caveman", "--oui", env={"FAKE_FAIL": "marketplace add"})
    assert result.returncode == 1
    assert not any(call.startswith("claude plugin install") for call in sandbox.calls())


# --- Prérequis manquants ------------------------------------------------------------------------------

@posix_only
def test_missing_claude_is_explained_and_nothing_else_is_tried(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("node", "npx", "uv"))
    result = box.run("caveman", "--oui")
    assert result.returncode == 1 and "Manque : claude" in result.stderr
    assert box.calls() == []


@posix_only
def test_ponytail_needs_node(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude",))
    result = box.run("ponytail", "--oui")
    assert result.returncode == 1 and "Manque : node" in result.stderr
    assert box.calls() == []


@posix_only
def test_graphify_without_uv_or_pipx_stops_with_a_hint_and_installs_nothing(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude",))
    result = box.run("graphify", "--oui")
    assert result.returncode == 1 and "uv ou pipx" in result.stderr
    assert box.calls() == []


@posix_only
def test_graphify_falls_back_to_pipx(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude", "pipx", "graphify"))
    (box.bin / "graphify").unlink()  # pas encore installé
    pipx = box.bin / "pipx"
    pipx.write_text(
        '#!/bin/sh\necho "pipx $*" >> "$FAKE_LOG"\n'
        'printf \'#!/bin/sh\\necho "graphify $*" >> "$FAKE_LOG"\\n\' > "$FAKE_BIN/graphify"\n'
        'chmod +x "$FAKE_BIN/graphify"\n', encoding="utf-8")
    pipx.chmod(0o755)
    result = box.run("graphify", "--oui")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.calls() == ["pipx install graphifyy", "graphify install"]


@posix_only
def test_graphify_already_present_only_registers_the_skill(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude", "graphify"))
    result = box.run("graphify", "--oui")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.calls() == ["graphify install"]


@posix_only
def test_graphify_not_on_path_after_install_tells_how_to_finish(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude", "uv"))
    (box.bin / "uv").write_text('#!/bin/sh\necho "uv $*" >> "$FAKE_LOG"\n', encoding="utf-8")  # n'installe rien
    result = box.run("graphify", "--oui")
    assert result.returncode == 1 and "uv tool update-shell" in result.stderr
    assert box.calls() == ["uv tool install graphifyy"]


@posix_only
def test_archify_needs_npx(tmp_path):
    box = Sandbox(tmp_path, SCRIPT, tools=("claude",))
    result = box.run("archify", "--oui")
    assert result.returncode == 1 and "Manque : npx" in result.stderr
    assert box.calls() == []


# --- Liste ------------------------------------------------------------------------------------------------

@posix_only
def test_list_names_every_skill_and_runs_nothing(sandbox):
    result = sandbox.run("--liste")
    assert result.returncode == 0
    ids = [line.split()[0] for line in result.stdout.splitlines()]
    assert len(ids) == 10 and set(PLUGINS) <= set(ids) and {"graphify", "archify", "awesome-claude-skills"} <= set(ids)
    assert sandbox.calls() == []
    assert "graphifyy" in result.stdout  # le nom exact, pas « graphify »
    assert os.path.exists(SCRIPT)
