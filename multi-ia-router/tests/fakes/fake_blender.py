"""Faux binaire Blender pour les tests : `blender --background --python script.py`.

Lit le script : s'il contient `ERREUR_BPY`, simule une trace Python (avec code retour 0,
comme le vrai Blender) ; sinon crée scene.blend et render.png dans le dossier courant.
"""

import sys
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("Blender 4.2.0 (factice)")
    sys.exit(0)
assert args[0] == "--background" and args[1] == "--python", args
script = Path(args[2]).read_text(encoding="utf-8")
print("Blender 4.2.0 (factice)")
if "ERREUR_BPY" in script:
    print('Traceback (most recent call last):\n  File "scene.py", line 3\nAttributeError: objet inconnu')
    sys.exit(0)
if "BOUCLE" in script:
    import time

    time.sleep(30)
Path("scene.blend").write_bytes(b"BLENDER")
Path("render.png").write_bytes(b"\x89PNG\r\n\x1a\n")
print("Saved: scene.blend")
