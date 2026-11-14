Compare le livrable final à ce que le client a demandé. C'est la dernière vérification avant livraison (contrôle {{attempt}}).

Demande du client :

<demande>
{{request}}
</demande>

Critères d'acceptation établis au départ :

{{criteria}}

Parties du projet (pour désigner l'origine d'un manque) :

{{parts}}

Le contexte ci-dessus contient le livrable.{{assets_note}}

Vérifie point par point que chaque élément de la demande et chaque critère d'acceptation est satisfait par le livrable. Ne juge pas le style : seulement ce qui manque, ce qui contredit la demande ou ce qui est manifestement faux.

- ok = true si le livrable peut être remis au client.
- Sinon, liste chaque manque dans gaps : part_id de la partie à corriger (ou vide si le défaut vient de l'assemblage : structure, partie oubliée, incohérence entre parties), description du manque, et consigne de correction concrète.

Rédige en {{language}}.
