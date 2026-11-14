Demande initiale du client :

<demande>
{{request}}
</demande>

Brief établi par le chef de projet :

<brief>
{{brief}}
</brief>

Agents disponibles (utilise exactement ces noms) :

{{agents}}

Types de partie et capacité requise chez l'agent :
- text : contenu rédigé (capacité text)
- code, files : fichiers écrits sur disque — scripts, données, arborescences (capacité files)
- blender : scène 3D produite par un script Python bpy exécuté par Blender en arrière-plan (capacité files)
- image : image générée (capacité image)
- video : vidéo générée (capacité video)

Découpe la demande en parties et établis le plan.

- Chaque point de la demande et chaque livrable du brief doit être couvert par au moins une partie.
- Le prompt de chaque partie doit être autonome : l'agent ne voit que ce prompt, la demande, le brief, le dossier de recherche commun et le résultat des parties listées dans depends_on. Donne-lui l'objectif, le périmètre exact, le format attendu et le niveau de détail.
- Pour une partie image ou video, le prompt est envoyé tel quel au modèle génératif : décris la scène, le style, le cadrage.
- Les critères de réussite servent de grille au relecteur : formule-les de façon vérifiable.
- N'utilise depends_on que si une partie a réellement besoin du résultat d'une autre.
- La recherche est faite une seule fois avant la production et partagée à toutes les parties : liste dans research_queries les questions à poser à la base de notes personnelle du client (vault) et les requêtes web utiles à l'ensemble du projet. Laisse les listes vides si la recherche n'apporte rien.
- Ne crée pas plus de parties que nécessaire : une demande simple peut tenir en une ou deux parties.

Rédige en {{language}}.
