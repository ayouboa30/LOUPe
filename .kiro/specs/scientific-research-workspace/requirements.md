# Document des exigences — Espace de recherche scientifique

## Introduction

Cette spec transforme 3loop en espace de recherche scientifique local-first, centré sur la traçabilité des preuves et spécialisé dans les workflows de machine learning. Le système doit ingérer et organiser des publications et artefacts, piloter des recherches web scientifiques avec des modèles locaux, produire des synthèses vérifiables, analyser des données, conserver un carnet de recherche et exporter des résultats reproductibles.

L’interface de réponse remplace la bulle classique par une **page de carnet de laboratoire au style pixelwise/pixel-art**. Pendant une recherche, cette page montre une animation de progression. Dans la réponse finale, elle conserve une trace de recherche structurée, repliable et consultable au clic. Cette trace expose les requêtes, sources, vérifications et décisions utiles à l’audit, mais jamais le raisonnement interne brut ni les chaînes de pensée privées d’un modèle.

## Objectifs

- Fonctionner localement par défaut, y compris pour la bibliothèque, l’index et l’historique de recherche.
- Préserver une provenance immuable jusqu’à la page, la zone, le fragment et la version de document.
- Orienter la planification des recherches vers le machine learning : modèles, datasets, benchmarks, métriques, ablations, matériel, licences et reproductibilité.
- Rendre chaque affirmation scientifique vérifiable à partir de preuves ancrées.
- Étendre progressivement le produit sans casser les conversations, documents et réglages existants.

## Hors périmètre initial

- Entraînement distribué de grands modèles dans 3loop.
- Affichage du raisonnement interne brut d’un modèle.
- Synchronisation cloud implicite ou publication automatique de données privées.
- Remplacement complet d’un tableur, d’un IDE ou d’un gestionnaire de versions.

## Priorités

- **P0 — Fondations :** stockage, migrations, jobs, sécurité et compatibilité.
- **P1 — Cœur scientifique :** ingestion, provenance, citations, modèle local ML et carnet de réponse.
- **P2 — Découverte :** connecteurs scientifiques et bibliographie.
- **P3 — Synthèse :** comparaison, revue, carnet de connaissances et exports.
- **P4 — Analyse :** veille, graphe, statistiques et expériences.
- **P5 — Avancé :** assistants spécialisés et intégrations externes.

## Glossaire

- **Artefact :** fichier ou ressource scientifique conservée, par exemple PDF, dataset, notebook ou transcription.
- **Claim :** affirmation atomique extraite d’une source ou produite dans une synthèse.
- **Preuve ancrée :** passage lié à une version, une page et, si disponible, une zone ou des offsets.
- **Provenance :** chaîne immuable reliant une sortie à ses sources, transformations, versions et paramètres.
- **Trace de recherche :** journal structuré et résumé des actions auditables ; ce n’est pas une chaîne de pensée.
- **Egress :** transmission de données hors de la machine locale.
- **Page de laboratoire :** conteneur visuel pixelwise utilisé pour les réponses et leur trace.

## Exigences

### Exigence 1 — Persistance locale et évolution du stockage [P0]

**User Story :** En tant que chercheur, je veux que ma bibliothèque et mes travaux persistent localement, afin de reprendre une recherche sans dépendre d’un service distant.

#### Critères d’acceptation

1.1 WHEN 3loop démarre avec un profil neuf, THE SYSTEM SHALL créer une base SQLite versionnée et les répertoires de blobs nécessaires sans accès réseau.

1.2 WHEN une donnée métier est créée ou modifiée, THE SYSTEM SHALL la persister transactionnellement et conserver des dates de création et de modification en UTC.

1.3 WHEN un fichier est ingéré, THE SYSTEM SHALL stocker son contenu dans un espace adressé par hash cryptographique et réutiliser un blob identique au lieu de le dupliquer.

1.4 WHEN le schéma évolue, THE SYSTEM SHALL exécuter des migrations ordonnées, atomiques et rejouables, avec sauvegarde préalable pour toute migration destructive.

1.5 IF la base est verrouillée, corrompue ou incompatible, THEN THE SYSTEM SHALL refuser l’écriture risquée, afficher un diagnostic récupérable et préserver les fichiers sources.

1.6 WHEN le poste est hors ligne, THE SYSTEM SHALL permettre au minimum la consultation, la recherche locale, l’annotation, les conversations et les exports ne nécessitant pas de fournisseur distant.

### Exigence 2 — Ingestion, identité, déduplication et versions [P1]

**User Story :** En tant que chercheur, je veux importer des publications depuis plusieurs entrées sans créer de doublons, afin de maintenir une bibliothèque fiable.

#### Critères d’acceptation

2.1 WHEN l’utilisateur fournit un PDF, DOI, URL, dossier ou lot de fichiers, THE SYSTEM SHALL créer ou rattacher un Paper, ses Identifiers, ses Artifacts et une DocumentVersion.

2.2 WHEN des métadonnées DOI, arXiv, PMID, titre/auteurs/année ou hash concordent, THE SYSTEM SHALL proposer ou appliquer une déduplication explicable sans supprimer silencieusement les versions distinctes.

2.3 WHEN une nouvelle version d’un document existant est importée, THE SYSTEM SHALL conserver les versions antérieures, leur origine, leur hash et leur date d’acquisition.

2.4 WHEN les métadonnées sont incomplètes ou contradictoires, THE SYSTEM SHALL afficher les valeurs candidates avec leur provenance et permettre une correction manuelle.

2.5 WHEN un dossier surveillé est rescanné, THE SYSTEM SHALL importer uniquement les contenus nouveaux ou modifiés et produire un bilan d’ingestion.

2.6 WHEN l’utilisateur supprime un élément de bibliothèque, THE SYSTEM SHALL distinguer le retrait logique de la suppression définitive du blob et avertir si d’autres objets le référencent.

### Exigence 3 — Extraction structurée et OCR [P1]

**User Story :** En tant que chercheur, je veux retrouver le contexte exact d’un passage, afin de vérifier les citations et résultats.

#### Critères d’acceptation

3.1 WHEN un document paginé est traité, THE SYSTEM SHALL conserver séparément chaque page, son numéro physique, son libellé logique et son lien vers la version source.

3.2 WHEN du texte est extrait, THE SYSTEM SHALL produire des chunks stables avec offsets, page, ordre, méthode d’extraction et hash du contenu.

3.3 WHEN une page ne contient pas de texte exploitable, THE SYSTEM SHALL pouvoir déclencher l’OCR local, marquer la méthode et conserver le texte OCR séparément du fichier original.

3.4 WHEN des tableaux ou figures sont détectés, THE SYSTEM SHALL conserver leur page, légende, zone, type et lien vers l’image ou les données extraites.

3.5 WHEN une extraction est relancée avec un moteur ou des paramètres différents, THE SYSTEM SHALL créer une nouvelle transformation traçable sans écraser les résultats antérieurs.

3.6 IF l’extraction échoue partiellement, THEN THE SYSTEM SHALL conserver les pages réussies, signaler les pages en erreur et permettre une reprise ciblée.

### Exigence 4 — Claims, preuves et citations vérifiables [P1]

**User Story :** En tant que chercheur, je veux que chaque affirmation importante soit reliée à une preuve, afin d’évaluer rapidement sa fiabilité.

#### Critères d’acceptation

4.1 WHEN le système produit une synthèse factuelle, THE SYSTEM SHALL relier chaque claim important à au moins une Citation ou le marquer explicitement comme non vérifié.

4.2 WHEN une citation provient d’un document local, THE SYSTEM SHALL conserver la DocumentVersion, la page, le chunk, les offsets et un extrait suffisamment court pour la vérification.

4.3 WHEN une citation provient du web, THE SYSTEM SHALL conserver l’URL canonique, le fournisseur, le titre, la date d’accès et, si autorisé, un instantané ou hash du contenu consulté.

4.4 WHEN l’utilisateur ouvre une citation, THE SYSTEM SHALL afficher la source au bon emplacement lorsque cet emplacement est disponible.

4.5 WHEN le contenu source change ou disparaît, THE SYSTEM SHALL préserver la citation historique, signaler l’écart et ne pas réattribuer silencieusement la preuve.

4.6 WHEN plusieurs preuves se contredisent, THE SYSTEM SHALL présenter le désaccord, les dates et les sources au lieu de fusionner les conclusions comme un consensus.

4.7 WHEN une réponse ne dispose pas de preuve suffisante, THE SYSTEM SHALL exprimer l’incertitude et proposer une recherche ou une vérification supplémentaire.

### Exigence 5 — Modèles locaux spécialisés machine learning [P1]

**User Story :** En tant que chercheur en ML, je veux que les modèles locaux comprennent les objets et critères du domaine, afin qu’ils orientent utilement la recherche et les synthèses.

#### Critères d’acceptation

5.1 WHEN une demande concerne le machine learning, THE SYSTEM SHALL construire localement un plan de recherche couvrant selon le besoin : tâche, architecture, dataset, benchmark, métriques, baselines, ablations, matériel, coût, licence, biais et reproductibilité.

5.2 WHEN le modèle local reformule une recherche web, THE SYSTEM SHALL générer des requêtes spécifiques aux fournisseurs choisis et conserver ces requêtes dans la trace de recherche.

5.3 WHEN des résultats ML sont analysés, THE SYSTEM SHALL distinguer publications, modèles, datasets, code, model cards, leaderboards et billets non évalués.

5.4 WHEN des métriques sont comparées, THE SYSTEM SHALL conserver le nom exact de la métrique, le split, le dataset, la direction d’optimisation et les conditions expérimentales connues.

5.5 WHEN des modèles ou datasets sont recommandés, THE SYSTEM SHALL présenter les licences, restrictions, matériel et limites connus, ou indiquer explicitement qu’ils n’ont pas été vérifiés.

5.6 WHEN le planificateur local ne respecte pas un schéma de sortie attendu, THE SYSTEM SHALL valider, réparer ou relancer la sortie dans une limite configurable sans exécuter aveuglément des instructions libres.

5.7 WHEN la recherche est terminée, THE SYSTEM SHALL produire pour le personnage une synthèse scientifique courte, précise et sourcée, adaptée à la page de carnet de laboratoire.

5.8 WHEN la requête n’est pas liée au ML, THE SYSTEM SHALL conserver un mode scientifique général sans forcer artificiellement la terminologie ML.

### Exigence 6 — Recherche web scientifique multi-fournisseurs [P2]

**User Story :** En tant que chercheur, je veux interroger des catalogues scientifiques complémentaires, afin d’obtenir une couverture plus fiable que la recherche web générique.

#### Critères d’acceptation

6.1 WHEN l’utilisateur lance une recherche scientifique, THE SYSTEM SHALL pouvoir interroger les connecteurs activés parmi Crossref, OpenAlex, arXiv, PubMed et Semantic Scholar.

6.2 WHEN la recherche est orientée ML, THE SYSTEM SHALL pouvoir inclure OpenReview, Papers with Code et Hugging Face, sous réserve de disponibilité et des conditions d’utilisation de leurs interfaces.

6.3 WHEN plusieurs fournisseurs retournent le même travail, THE SYSTEM SHALL fusionner les résultats par identifiants et similarité bibliographique tout en conservant la provenance de chaque champ.

6.4 WHEN un fournisseur impose un quota, une authentification ou échoue, THE SYSTEM SHALL respecter le rate limit, appliquer une reprise bornée et afficher une dégradation partielle plutôt que masquer l’échec.

6.5 WHEN des résultats sont classés, THE SYSTEM SHALL rendre visibles les principaux facteurs de classement tels que pertinence, date, type de preuve, citations et disponibilité du texte ou du code.

6.6 WHEN l’utilisateur sélectionne des résultats, THE SYSTEM SHALL pouvoir les ajouter à la bibliothèque avec leurs identifiants, métadonnées et provenance.

6.7 WHEN aucun connecteur scientifique n’est disponible, THE SYSTEM SHALL permettre une recherche web générique explicitement étiquetée et soumise aux mêmes règles de provenance.

### Exigence 7 — Page de carnet de laboratoire pixelwise et trace de recherche [P1]

**User Story :** En tant qu’utilisateur, je veux suivre puis revoir la démarche de recherche dans la réponse, afin de comprendre ce qui a été fait sans encombrer le chat.

#### Critères d’acceptation

7.1 WHEN une réponse de recherche démarre, THE SYSTEM SHALL afficher dans le flux du chat une page de carnet de laboratoire pixelwise avec un état animé non bloquant.

7.2 WHEN une étape auditable se produit, THE SYSTEM SHALL ajouter un événement structuré parmi planification, requête, source trouvée, lecture, extraction, vérification, décision, avertissement et achèvement.

7.3 WHEN une source ou une requête apparaît dans la trace, THE SYSTEM SHALL permettre d’en consulter le libellé et le lien ou identifiant sans exposer de prompt secret ni de raisonnement interne brut.

7.4 WHEN la réponse finale est prête, THE SYSTEM SHALL conserver la trace dans la même page de laboratoire et la rendre repliable/dépliable au clic.

7.5 WHEN la trace est repliée, THE SYSTEM SHALL afficher un résumé compact comprenant le statut, la durée, le nombre de requêtes, de sources consultées, de preuves retenues et les avertissements.

7.6 WHEN l’utilisateur rouvre une conversation, THE SYSTEM SHALL restaurer la réponse finale, les citations et la trace dans leur état persistant.

7.7 WHEN `prefers-reduced-motion` est actif ou que l’utilisateur désactive les animations, THE SYSTEM SHALL remplacer les mouvements pixelwise par des changements d’état statiques accessibles.

7.8 WHEN JavaScript ou une animation échoue, THE SYSTEM SHALL laisser le contenu final, les citations et les contrôles de repli utilisables.

7.9 WHEN le personnage présente un message, THE SYSTEM SHALL utiliser la page de carnet plutôt qu’une bulle classique et distinguer visuellement hypothèse, observation, résultat et avertissement.

### Exigence 8 — Bibliothèque, collections et échanges bibliographiques [P2]

**User Story :** En tant que chercheur, je veux organiser et échanger mes références, afin d’intégrer 3loop à mon workflow bibliographique.

#### Critères d’acceptation

8.1 WHEN l’utilisateur consulte la bibliothèque, THE SYSTEM SHALL permettre recherche, tri et filtres par auteurs, année, venue, type, tags, collection, statut de lecture et disponibilité locale.

8.2 WHEN un Paper est ouvert, THE SYSTEM SHALL afficher ses versions, identifiants, auteurs, artefacts, notes, citations et provenance des métadonnées.

8.3 WHEN l’utilisateur importe du BibTeX, RIS ou CSL-JSON, THE SYSTEM SHALL valider les enregistrements, signaler les erreurs et dédupliquer avant insertion.

8.4 WHEN l’utilisateur exporte des références, THE SYSTEM SHALL produire au minimum BibTeX, RIS et CSL-JSON en conservant les champs connus sans inventer de valeurs.

8.5 WHEN Zotero est configuré, THE SYSTEM SHALL importer ou exporter uniquement après action explicite et journaliser les éléments acceptés, ignorés et en erreur.

8.6 WHEN des collections, tags ou statuts sont modifiés en lot, THE SYSTEM SHALL appliquer l’opération de manière transactionnelle ou fournir un bilan détaillé des échecs.

### Exigence 9 — Comparaison multi-articles et revue de littérature [P3]

**User Story :** En tant que chercheur, je veux comparer méthodiquement plusieurs travaux, afin d’identifier consensus, divergences et lacunes.

#### Critères d’acceptation

9.1 WHEN plusieurs Papers sont sélectionnés, THE SYSTEM SHALL générer une matrice sourcée selon des dimensions configurables telles que question, méthode, données, métriques, résultats, limites et reproductibilité.

9.2 WHEN une cellule de comparaison contient une conclusion, THE SYSTEM SHALL la relier à une ou plusieurs preuves ancrées ou la marquer comme inconnue.

9.3 WHEN une revue de littérature est demandée, THE SYSTEM SHALL conserver la question, les requêtes, fournisseurs, filtres, critères d’inclusion/exclusion et la date d’exécution.

9.4 WHEN un corpus est filtré, THE SYSTEM SHALL permettre d’enregistrer les raisons d’inclusion ou d’exclusion article par article.

9.5 WHEN une synthèse de corpus est produite, THE SYSTEM SHALL distinguer faits rapportés, interprétations, contradictions, lacunes et pistes futures.

9.6 WHEN le corpus ou les critères changent, THE SYSTEM SHALL créer une nouvelle révision de la revue plutôt qu’écraser l’état précédemment cité.

### Exigence 10 — Carnet de connaissances, annotations et glossaire [P3]

**User Story :** En tant que chercheur, je veux capturer mes idées à côté des sources, afin de construire une mémoire de projet exploitable.

#### Critères d’acceptation

10.1 WHEN l’utilisateur crée une note, THE SYSTEM SHALL permettre de la rattacher à un projet, Paper, page, chunk, claim, dataset, analyse ou expérience.

10.2 WHEN l’utilisateur annote un passage ou une zone, THE SYSTEM SHALL conserver la version du document et l’ancrage exact.

10.3 WHEN un document change de version, THE SYSTEM SHALL préserver l’annotation historique et proposer un ré-ancrage sans déplacer silencieusement la référence.

10.4 WHEN des tags ou termes de glossaire sont utilisés, THE SYSTEM SHALL permettre leur recherche, fusion et définition avec sources facultatives.

10.5 WHEN des notes sont exportées, THE SYSTEM SHALL conserver les liens vers les sources et signaler les références qui ne sont pas exportables.

### Exigence 11 — Veille et graphe scientifique [P4]

**User Story :** En tant que chercheur, je veux surveiller un sujet et explorer ses relations, afin de découvrir les évolutions pertinentes.

#### Critères d’acceptation

11.1 WHEN une veille est créée, THE SYSTEM SHALL enregistrer sa requête, ses fournisseurs, filtres, fréquence et état d’activation.

11.2 WHEN une veille s’exécute, THE SYSTEM SHALL signaler uniquement les résultats nouveaux ou modifiés depuis sa dernière exécution réussie.

11.3 WHEN l’application ne dispose pas d’un ordonnanceur de fond fiable, THE SYSTEM SHALL exécuter les veilles dues au prochain démarrage et indiquer leur retard.

11.4 WHEN le graphe est affiché, THE SYSTEM SHALL représenter au minimum les relations de citation, auteurs, sujets, artefacts et appartenance aux collections avec leur provenance.

11.5 WHEN une relation est inférée plutôt qu’explicite, THE SYSTEM SHALL l’étiqueter comme inférée avec sa méthode et son score.

### Exigence 12 — Datasets, statistiques et visualisations reproductibles [P4]

**User Story :** En tant que chercheur, je veux analyser des données dans 3loop, afin de relier résultats quantitatifs, code et publications.

#### Critères d’acceptation

12.1 WHEN un fichier CSV ou Excel est importé, THE SYSTEM SHALL conserver le fichier source, détecter le schéma, présenter un aperçu et demander confirmation pour les conversions ambiguës.

12.2 WHEN une analyse est lancée, THE SYSTEM SHALL enregistrer le dataset et sa version, les colonnes, filtres, paramètres, versions logicielles, code ou recette et seed aléatoire applicable.

12.3 WHEN des statistiques sont calculées, THE SYSTEM SHALL afficher la taille d’échantillon, les données manquantes, la méthode et les hypothèses pertinentes avec le résultat.

12.4 WHEN un graphique est produit, THE SYSTEM SHALL conserver ses données d’entrée, paramètres, légendes, unités et une recette permettant sa régénération.

12.5 WHEN un test statistique est suggéré, THE SYSTEM SHALL le présenter comme aide technique et non comme conseil professionnel, et signaler les hypothèses non vérifiées.

12.6 WHEN une analyse est relancée avec les mêmes entrées et versions, THE SYSTEM SHALL permettre de comparer les résultats et d’identifier les causes connues d’écart.

### Exigence 13 — Expériences ML et reproductibilité [P4]

**User Story :** En tant que chercheur ML, je veux suivre mes expériences et les relier aux articles, afin de comparer les résultats de façon reproductible.

#### Critères d’acceptation

13.1 WHEN une expérience est créée, THE SYSTEM SHALL enregistrer objectif, hypothèse, code ou commit, environnement, dataset, modèle, paramètres, seed, matériel et métriques disponibles.

13.2 WHEN une exécution est importée ou saisie, THE SYSTEM SHALL conserver ses artefacts, logs et statut sans écraser les exécutions antérieures.

13.3 WHEN plusieurs exécutions sont comparées, THE SYSTEM SHALL aligner les métriques compatibles et signaler les différences de dataset, split, matériel ou protocole.

13.4 WHEN un résultat est cité dans une note ou réponse, THE SYSTEM SHALL lier la citation à l’AnalysisRun ou l’Experiment exact et à ses artefacts.

13.5 WHEN une fiche de reproductibilité est générée, THE SYSTEM SHALL distinguer informations confirmées, absentes et supposées.

### Exigence 14 — Assistants scientifiques avancés [P5]

**User Story :** En tant que chercheur, je veux des outils spécialisés sur mes contenus, afin d’accélérer les tâches répétitives sans perdre la provenance.

#### Critères d’acceptation

14.1 WHEN une traduction est demandée, THE SYSTEM SHALL conserver la langue source, le texte source, le modèle utilisé et les ancrages de citation.

14.2 WHEN des flashcards sont générées, THE SYSTEM SHALL relier chaque réponse à une source et permettre leur correction avant export.

14.3 WHEN un audio ou une vidéo est transcrit, THE SYSTEM SHALL conserver le média source, les timestamps, la langue, le moteur et les segments corrigés.

14.4 WHEN deux versions de document sont comparées, THE SYSTEM SHALL distinguer changements textuels, structurels et métadonnées avec liens vers les emplacements concernés.

14.5 WHEN le mode reviewer est utilisé, THE SYSTEM SHALL séparer résumé, forces, faiblesses, questions, vérifications et suggestions, chacune sourcée lorsque factuelle.

14.6 WHEN un assistant spécialisé manque de preuve, THE SYSTEM SHALL refuser d’inventer une citation et marquer clairement la limite.

### Exigence 15 — Exports et intégrations [P3/P5]

**User Story :** En tant que chercheur, je veux exporter mes travaux et les relier à mes outils, afin de poursuivre la rédaction et la reproductibilité ailleurs.

#### Critères d’acceptation

15.1 WHEN une réponse, revue ou note est exportée, THE SYSTEM SHALL produire du Markdown avec citations et bibliographie résolubles.

15.2 WHEN les dépendances nécessaires sont disponibles, THE SYSTEM SHALL pouvoir produire DOCX et LaTeX ; sinon il devra proposer le format source disponible sans perdre le contenu.

15.3 WHEN un export Overleaf est demandé, THE SYSTEM SHALL générer un paquet LaTeX local avant toute transmission externe explicite.

15.4 WHEN Jupyter est intégré, THE SYSTEM SHALL importer ou générer un notebook avec cellules, données référencées et métadonnées de provenance sans exécuter automatiquement du code non fiable.

15.5 WHEN GitHub ou OSF est configuré, THE SYSTEM SHALL afficher les données et fichiers qui quitteront la machine et demander confirmation avant chaque première destination ou changement de périmètre.

15.6 WHEN un export est régénéré, THE SYSTEM SHALL conserver son format, ses paramètres, son hash et les objets sources utilisés.

### Exigence 16 — Confidentialité, secrets et sécurité [P0]

**User Story :** En tant que chercheur, je veux contrôler les données qui quittent mon poste, afin de protéger les travaux sensibles et mes identifiants.

#### Critères d’acceptation

16.1 WHEN aucune fonctionnalité distante n’est activée, THE SYSTEM SHALL n’effectuer aucun egress de contenu utilisateur.

16.2 WHEN une action doit transmettre une requête, un extrait ou un fichier, THE SYSTEM SHALL identifier la destination et la catégorie de données avant transmission selon une politique de consentement configurable.

16.3 WHEN un secret fournisseur est enregistré, THE SYSTEM SHALL utiliser le gestionnaire de secrets du système d’exploitation ou un stockage chiffré de remplacement et ne jamais le placer dans `localStorage`, les logs ou les exports.

16.4 WHEN du contenu PDF, web, OCR ou issu d’un connecteur contient des instructions, THE SYSTEM SHALL le traiter comme donnée non fiable et l’empêcher de modifier les règles système, les permissions ou la liste d’outils.

16.5 WHEN une URL distante est récupérée, THE SYSTEM SHALL autoriser uniquement HTTP(S), résoudre et bloquer les destinations locales, privées, link-local et autres cibles interdites, y compris après redirection.

16.6 WHEN du HTML ou Markdown externe est affiché, THE SYSTEM SHALL le neutraliser contre l’exécution de scripts, les URL dangereuses et l’injection dans l’interface.

16.7 WHEN un journal est écrit, THE SYSTEM SHALL expurger secrets et données sensibles configurées tout en conservant les identifiants techniques nécessaires au diagnostic.

16.8 WHEN un connecteur ou modèle cloud est activé, THE SYSTEM SHALL le signaler visuellement et permettre sa désactivation immédiate.

### Exigence 17 — Jobs persistants, API versionnée et observabilité [P0]

**User Story :** En tant qu’utilisateur, je veux que les opérations longues soient suivies et récupérables, afin de ne pas perdre mon travail lors d’une interruption.

#### Critères d’acceptation

17.1 WHEN une ingestion, extraction, recherche, analyse ou export long démarre, THE SYSTEM SHALL créer un Job persistant avec type, statut, progression, paramètres, dates et propriétaire logique.

17.2 WHEN un Job progresse, THE SYSTEM SHALL publier des événements structurés via SSE et permettre de reconstruire son état depuis la base.

17.3 WHEN l’utilisateur annule un Job, THE SYSTEM SHALL arrêter au prochain point sûr, conserver les résultats déjà validés et marquer clairement l’état final.

17.4 WHEN un Job échoue de manière récupérable, THE SYSTEM SHALL permettre une reprise ou un rejeu idempotent sans dupliquer les objets validés.

17.5 WHEN l’application redémarre après une interruption, THE SYSTEM SHALL détecter les Jobs restés actifs et les marquer interrompus ou les reprendre selon leur politique.

17.6 WHEN une nouvelle route métier est ajoutée, THE SYSTEM SHALL l’exposer sous `/api/v1` avec erreurs JSON stables, validation d’entrée et limites de taille explicites.

17.7 WHEN un événement de trace est enregistré, THE SYSTEM SHALL associer Job, conversation, message, horodatage, type, résumé public et références d’artefacts sans stocker de chaîne de pensée privée.

### Exigence 18 — Accessibilité, performance et expérience locale [P0/P1]

**User Story :** En tant qu’utilisateur, je veux une interface lisible et réactive, afin de travailler longtemps avec des corpus importants.

#### Critères d’acceptation

18.1 WHEN la page de laboratoire est utilisée au clavier, THE SYSTEM SHALL permettre d’ouvrir, replier, parcourir et copier trace et citations avec un focus visible.

18.2 WHEN un lecteur d’écran est actif, THE SYSTEM SHALL annoncer les changements de statut importants sans lire chaque frame d’animation.

18.3 WHEN le style pixelwise est affiché, THE SYSTEM SHALL conserver des contrastes suffisants, des zones cliquables utilisables et un texte redimensionnable.

18.4 WHEN une bibliothèque contient un grand nombre d’objets, THE SYSTEM SHALL paginer ou virtualiser les listes et ne pas charger tous les blobs ou contenus en mémoire.

18.5 WHEN un index local est incomplet, THE SYSTEM SHALL afficher son état et retourner des résultats partiels identifiés plutôt que bloquer toute recherche.

18.6 WHEN une opération intensive s’exécute, THE SYSTEM SHALL maintenir la navigation et l’annulation disponibles.

### Exigence 19 — Compatibilité, migration et packaging Windows [P0/P5]

**User Story :** En tant qu’utilisateur existant, je veux mettre à jour 3loop sans perdre mes conversations ou réglages, afin d’adopter la suite scientifique en confiance.

#### Critères d’acceptation

19.1 WHEN une installation existante est mise à jour, THE SYSTEM SHALL détecter les conversations, documents et préférences hérités et proposer ou exécuter une migration idempotente documentée.

19.2 WHEN des données héritées ne disposent pas de provenance par page, THE SYSTEM SHALL les marquer comme héritées à provenance limitée au lieu d’inventer des ancrages.

19.3 WHEN les nouvelles API sont introduites, THE SYSTEM SHALL préserver les routes existantes nécessaires au frontend durant une période de compatibilité ou fournir un adaptateur.

19.4 WHEN l’application Windows est empaquetée, THE SYSTEM SHALL inclure les migrations, assets pixelwise, schémas et ressources locales nécessaires au démarrage hors ligne.

19.5 WHEN des dépendances optionnelles manquent dans le bundle, THE SYSTEM SHALL désactiver proprement les fonctions correspondantes et expliquer comment les activer sans empêcher le cœur local de démarrer.

19.6 WHEN une mise à niveau échoue, THE SYSTEM SHALL conserver une sauvegarde restaurable et fournir un message d’erreur exploitable.

### Exigence 20 — Qualité, intégrité et vérification [P0–P5]

**User Story :** En tant que mainteneur, je veux vérifier les invariants scientifiques et techniques, afin d’éviter les régressions silencieuses.

#### Critères d’acceptation

20.1 WHEN un objet cité est modifié ou supprimé, THE SYSTEM SHALL empêcher les références pendantes ou conserver un tombstone résoluble.

20.2 WHEN un blob ou un instantané est relu, THE SYSTEM SHALL pouvoir vérifier son hash et signaler toute divergence.

20.3 WHEN une transformation scientifique est exécutée, THE SYSTEM SHALL enregistrer sa version, ses paramètres et ses entrées afin qu’elle soit auditable.

20.4 WHEN des tests de provenance utilisent une source paginée connue, THE SYSTEM SHALL vérifier que la citation résout la bonne version, page et plage de texte.

20.5 WHEN des tests de sécurité soumettent des URL privées, redirections interdites, HTML actif ou instructions injectées dans une source, THE SYSTEM SHALL bloquer ou neutraliser ces entrées sans exécuter leurs instructions.

20.6 WHEN une version distribuable est construite, THE SYSTEM SHALL réussir les tests ciblés, un démarrage hors ligne et un smoke test des migrations, de la bibliothèque et d’une réponse avec trace repliable.

## Matrice de livraison synthétique

| Lot | Capacités principales |
|---|---|
| P0 | SQLite, migrations, blobs, sécurité, jobs, `/api/v1`, compatibilité |
| P1 | Paper/Page/Chunk/Claim/Citation/Provenance, ingestion PDF/OCR, modèle local ML, page de laboratoire |
| P2 | Connecteurs scientifiques, recherche fédérée, bibliothèque et bibliographie |
| P3 | Comparaison, revue de littérature, carnet, annotations et exports |
| P4 | Veille, graphe, datasets, statistiques, graphiques et expériences |
| P5 | Traduction, flashcards, transcription, diff, reviewer et intégrations |
