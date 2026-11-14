Un projet est terminé et le client demande une modification. Le but est de modifier le moins possible : seules les parties réellement concernées repassent en production.

Demande initiale et modifications précédentes :

<demande>
{{request}}
</demande>

Plan actuel du projet :

<plan>
{{plan}}
</plan>

Agents disponibles pour d'éventuelles nouvelles parties (utilise exactement ces noms) :

{{agents}}

Modification demandée maintenant :

<modification>
{{instruction}}
</modification>

Établis le plan de modification.

- changes : pour chaque partie existante à retoucher, la consigne précise de ce qui doit changer. Ne liste pas une partie qui reste valable telle quelle.
- new_parts : uniquement si la modification exige un contenu qu'aucune partie existante ne peut porter. Mêmes règles que pour un plan initial ; choisis des ids qui n'existent pas encore.
- remove_parts : parties devenues inutiles.
- research_queries : uniquement si la modification demande des informations absentes du dossier de recherche existant.
- assembly_instructions : à remplir si la modification touche la structure, l'ordre ou le ton du livrable ; sinon laisse vide.

Si la modification ne concerne que la forme du livrable final, changes peut rester vide et assembly_instructions suffit.

Rédige en {{language}}.
