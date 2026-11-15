"""Lanceurs Linux / macOS : syntaxe, droits d'exécution, passage des arguments, messages d'aide."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="lanceurs shell")


@posix_only
@pytest.mark.parametrize("script", ["demarrer.sh", "routeur"])
def test_shell_scripts_are_valid_posix_sh(script):
    assert subprocess.run(["sh", "-n", str(ROOT / script)], capture_output=True).returncode == 0


@posix_only
def test_scripts_are_executable_in_git():
    """Un clone ou un zip doit permettre `./routeur` ; sinon l'utilisateur se heurte à « Permission denied »."""
    if shutil.which("git") is None or not (ROOT.parent / ".git").exists():
        pytest.skip("pas un dépôt git")
    out = subprocess.run(["git", "ls-files", "-s", "demarrer.sh", "routeur"], cwd=ROOT, capture_output=True, text=True).stdout
    modes = {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}
    assert modes == {"demarrer.sh": "100755", "routeur": "100755"}


@posix_only
def test_wrapper_forwards_arguments_to_the_private_environment(tmp_path):
    (tmp_path / ".venv/bin").mkdir(parents=True)
    shim = tmp_path / ".venv/bin/python"
    shim.write_text('#!/bin/sh\necho "python: $@"\n', encoding="utf-8")
    shim.chmod(0o755)
    wrapper = tmp_path / "routeur"
    shutil.copy(ROOT / "routeur", wrapper)
    wrapper.chmod(0o755)
    result = subprocess.run([str(wrapper), "tools", "install", "voicestudio", "--dry-run"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "python: -m router tools install voicestudio --dry-run"
    # appelable depuis un autre dossier
    other = tmp_path / "ailleurs"
    other.mkdir()
    assert subprocess.run([str(wrapper), "doctor"], cwd=other, capture_output=True, text=True).stdout.strip() == "python: -m router doctor"


@posix_only
def test_wrapper_without_environment_explains_what_to_do(tmp_path):
    wrapper = tmp_path / "routeur"
    shutil.copy(ROOT / "routeur", wrapper)
    wrapper.chmod(0o755)
    result = subprocess.run([str(wrapper), "doctor"], capture_output=True, text=True)
    assert result.returncode == 1 and "demarrer.sh --installer-seulement" in result.stderr


@posix_only
def test_demarrer_explains_missing_venv_module(tmp_path):
    """Debian / Ubuntu sans python3-venv : message actionnable au lieu d'une trace."""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    script = tmp_path / "demarrer.sh"
    shutil.copy(ROOT / "demarrer.sh", script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("python3.13", "python3.12", "python3", "python"):  # tous les candidats du script, devant le vrai Python
        fake_python = fake_bin / name
        fake_python.write_text(
            '#!/bin/sh\n'
            'case "$*" in\n'
            '  *"sys.version_info"*) exit 0 ;;\n'
            '  *"-m venv"*) echo "ensurepip is not available" >&2; exit 1 ;;\n'
            'esac\nexit 0\n', encoding="utf-8")
        fake_python.chmod(0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(["sh", str(script), "--installer-seulement"], capture_output=True, text=True, env=env)
    assert result.returncode == 1 and "sudo apt install python3-venv" in result.stderr
    assert not (tmp_path / ".venv").exists()  # pas de dossier à moitié créé
