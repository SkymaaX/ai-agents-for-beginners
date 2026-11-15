@echo off
rem Lanceur Windows : installe ce qui manque, diagnostique, puis ouvre le tableau de bord.
rem   demarrer.bat                        tout faire
rem   demarrer.bat --installer-seulement  preparer sans lancer le tableau de bord
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3" "python") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1 && set "PY=%%~C"
  )
)
if not defined PY (
  echo Python 3.12 ou plus est requis : https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe %PY% -m venv .venv
if errorlevel 1 ( echo Creation de l'environnement impossible. & pause & exit /b 1 )
call .venv\Scripts\activate.bat
echo Installation des dependances...
pip install -q -r requirements.txt
if errorlevel 1 ( echo Installation impossible. & pause & exit /b 1 )
if not exist .env (
  copy .env.example .env >nul
  echo Fichier .env cree : ajoute-y tes cles ^(GEMINI_API_KEY, ANYTHINGLLM_API_KEY^).
)

echo Diagnostic de ta machine...
python -m router doctor --write

if "%~1"=="--installer-seulement" (
  echo Pret. Lancer le tableau de bord : python -m router dashboard
  pause
  exit /b 0
)
echo Tableau de bord : http://127.0.0.1:8765  ^(Ctrl+C pour arreter^)
start "" http://127.0.0.1:8765
python -m router dashboard
pause
