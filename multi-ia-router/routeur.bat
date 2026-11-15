@echo off
rem Lance le routeur avec son propre environnement Python (sans activer quoi que ce soit).
rem   routeur tools list      routeur doctor      routeur dashboard
set "HERE=%~dp0"
if not exist "%HERE%.venv\Scripts\python.exe" (
  echo L'environnement n'est pas installe. Lance d'abord : demarrer.bat --installer-seulement
  exit /b 1
)
"%HERE%.venv\Scripts\python.exe" -m router %*
