Assemble le livrable final du projet « {{project_title}} » à partir des parties validées fournies dans le contexte.

Consignes d'assemblage du répartiteur :

<consignes>
{{assembly_instructions}}
</consignes>

Fichiers produits par les agents, à intégrer au livrable par des liens ou des images Markdown en utilisant exactement ces chemins relatifs :

{{assets}}

{{correction}}

Produis un document Markdown unique en {{language}} :
- un titre, une courte introduction qui rappelle l'objectif, puis le contenu organisé de façon logique pour le lecteur ;
- harmonise le ton, les niveaux de titres et les transitions ; fusionne les redites ;
- conserve toute l'information des parties et leurs références de sources [S…] ; n'ajoute aucun fait qui n'y figure pas ;
- insère chaque image à l'endroit pertinent avec la syntaxe `![description](chemin)` et cite les autres fichiers avec `[nom](chemin)` ;
- termine par une section « Sources » qui reprend, depuis le dossier de recherche, uniquement les sources réellement citées, avec leur lien.

Rends uniquement le document, sans commentaire autour.
