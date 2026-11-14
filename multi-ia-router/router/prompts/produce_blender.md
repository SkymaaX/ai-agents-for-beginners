Partie à produire : {{title}}

Consigne du répartiteur :

<consigne>
{{part_prompt}}
</consigne>

Critères de réussite sur lesquels ton travail sera relu :

{{criteria}}

Écris dans le dossier de travail courant un script Python nommé `scene.py` utilisant l'API bpy. Le routeur l'exécutera ensuite lui-même avec `blender --background --python scene.py`, le dossier de travail comme dossier courant. N'essaie pas de lancer Blender toi-même.

Le script doit :
- partir d'une scène vide (`bpy.ops.wm.read_factory_settings(use_empty=True)`) ;
- fonctionner sans interface graphique (pas d'opérateur dépendant d'une vue 3D ouverte) ;
- enregistrer la scène dans `{{blend_name}}` et, si un rendu est demandé, l'image dans `render.png`, avec des chemins relatifs au dossier courant ;
- afficher avec print les étapes importantes, pour le diagnostic.

Quand tu as terminé, réponds par un compte rendu court en {{language}} : ce que construit le script et les choix faits.
