# Plan d’implémentation — Espace de recherche scientifique

## Règles d’exécution

- Exécuter les tâches dans l’ordre de leurs dépendances, par incréments verticaux utilisables.
- Ne jamais écraser une donnée historique ou un changement de travail existant sans migration explicite.
- Pour chaque tâche : implémenter, valider les critères référencés, puis cocher la case.
- Les tests réseau réels restent opt-in ; les tests standards utilisent des fixtures.
- Une capacité optionnelle indisponible doit se dégrader proprement sans empêcher le cœur local de démarrer.
- `Done` signifie : code intégré, erreur gérée, persistance/provenance vérifiée, API/UI applicable validée et ressource PyInstaller prise en compte.

## Lot vertical initial — Fondation scientifique utilisable

Ce lot est la première livraison immédiate. Il ne requiert aucun nouveau service distant : base/migrations, blobs, modèle scientifique minimal, import PDF paginé, API bibliothèque, jobs/trace persistants et rendu carnet pixelwise.

- [ ] 1. Établir le point de migration et la compatibilité de l’existant
  - Inventorier les chemins de données, routes, conversations, documents et préférences existants.
  - Ajouter un calcul de répertoire de données stable hors du bundle et un indicateur de capacités `/api/v1`.
  - Ne modifier aucune route historique sans adaptateur ou appel vers le nouveau service.
  - **Dépendances :** aucune.
  - **Fin :** démarrage existant inchangé ; chemin de données et stratégie de migration observables.
  - _Requirements: 1.1, 1.6, 19.1, 19.3, 19.4_

- [ ] 2. Créer le noyau SQLite et le moteur de migrations
  - Configurer connexion par thread, `foreign_keys`, WAL, timeout et transactions.
  - Créer `schema_migrations` et la migration initiale.
  - Ajouter sauvegarde et diagnostic de migration ; embarquer les scripts dans PyInstaller.
  - **Dépendances :** 1.
  - **Fin :** base neuve et base déjà migrée démarrent de façon idempotente ; échec simulé ne détruit pas les données.
  - _Requirements: 1.1, 1.2, 1.4, 1.5, 19.4, 19.6_

- [ ] 3. Implémenter le blob store adressé par SHA-256
  - Écriture en streaming via fichier temporaire et renommage atomique.
  - Déduplication, vérification de taille/hash et détection des références avant suppression.
  - **Dépendances :** 1, 2.
  - **Fin :** deux imports identiques partagent un blob ; une divergence de hash est signalée.
  - _Requirements: 1.3, 2.6, 20.2_

- [ ] 4. Créer le schéma scientifique P1 et les repositories
  - Tables `Paper`, `Identifier`, `Author`, `Venue`, `Artifact`, `DocumentVersion`, `Page`, `Chunk`, `Source`, `Transform`, `Claim`, `Citation`, `ProvenanceEdge` et tombstones.
  - Contraintes d’identité, versions immuables et cohérence version/page/chunk.
  - Ajouter FTS5 reconstructible pour titres, résumés et chunks.
  - **Dépendances :** 2, 3.
  - **Fin :** CRUD transactionnel minimal et invariants de citation vérifiés sur base temporaire.
  - _Requirements: 1.2, 2.1–2.4, 3.1–3.5, 4.1–4.5, 20.1–20.4_

- [ ] 5. Ajouter les jobs persistants et événements publics de trace
  - Tables `jobs`, `job_events`, `research_runs`, `message_traces` et mécanisme de lease.
  - États, annulation coopérative, reprise idempotente et récupération après redémarrage.
  - Événements publics séquencés sans prompts secrets ni chaîne de pensée.
  - **Dépendances :** 2.
  - **Fin :** un job interrompu est restauré correctement et ses événements sont rejouables sans doublon.
  - _Requirements: 7.2–7.6, 17.1–17.7_

- [ ] 6. Adapter l’ingestion PDF pour préserver pages et chunks
  - Réutiliser l’extracteur actuel sans aplatir le document comme source d’autorité.
  - Créer Paper/Artifact/DocumentVersion, puis Page/Chunk avec hash, offsets, méthode et Transform.
  - Conserver les succès partiels et exposer une reprise par page.
  - Préparer le point d’extension OCR sans rendre l’OCR obligatoire.
  - **Dépendances :** 3, 4, 5.
  - **Fin :** un PDF connu est importé, chaque chunk résout sa page/version et le blob original reste intact.
  - _Requirements: 2.1–2.5, 3.1–3.6, 20.3, 20.4_

- [ ] 7. Exposer la première API `/api/v1` de bibliothèque et de jobs
  - Ajouter health/capabilities, import, liste/détail Paper, pages/chunks, job, annulation et flux SSE.
  - Valider tailles, types, pagination, erreurs JSON et clés d’idempotence.
  - Maintenir les routes documentaires historiques via adaptateur.
  - **Dépendances :** 4, 5, 6.
  - **Fin :** import asynchrone observable en SSE puis consultation paginée du document.
  - _Requirements: 17.1–17.7, 18.4–18.6, 19.3_

- [ ] 8. Construire la page de carnet de laboratoire pixelwise
  - Remplacer visuellement la bulle assistant par une page sémantique : en-tête, blocs Hypothèse/Observation/Résultat/Avertissement, réponse, citations et pied de page.
  - Utiliser uniquement des assets locaux et préserver sélection, zoom, contraste et navigation clavier.
  - Conserver une apparence correcte si JavaScript ou les animations sont indisponibles.
  - **Dépendances :** 1.
  - **Fin :** chaque réponse assistant est rendue comme carnet pixelwise sans régression de lecture ni copie.
  - _Requirements: 7.1, 7.8, 7.9, 18.1–18.3, 19.4_

- [ ] 9. Intégrer la trace animée, persistante et repliable dans le chat
  - Consommer les événements SSE en place pendant le job.
  - Afficher animation discrète et étapes ; fournir mode `prefers-reduced-motion`.
  - Finaliser le même composant avec `<details>`, résumé, compteurs, avertissements et restauration au rechargement.
  - **Dépendances :** 5, 7, 8.
  - **Fin :** une recherche simulée reste visible dans la réponse finale, se replie au clic et se restaure après redémarrage.
  - _Requirements: 7.1–7.8, 17.2, 17.7, 18.1–18.3_

- [ ] 10. Implémenter le planificateur local spécialisé ML
  - Définir schémas de plan, prompts locaux et validation/réparation bornée.
  - Couvrir tâche, architecture, datasets, benchmarks, métriques, baselines, ablations, matériel, coût, licence, biais et reproductibilité.
  - Produire les requêtes par fournisseur et une synthèse courte du personnage.
  - Garder un profil scientifique général pour les requêtes non ML.
  - **Dépendances :** 5, 9.
  - **Fin :** fixtures ML et non-ML produisent des plans valides ; aucune sortie libre ne déclenche directement un outil.
  - _Requirements: 5.1–5.8, 16.4, 17.7_

- [ ] 11. Résoudre claims, citations et preuves dans les réponses
  - Extraire ou créer des claims atomiques et attribuer `supported`, `conflicting` ou `unverified`.
  - Ancrer les citations à version/page/chunk/offset et ouvrir la source au bon emplacement.
  - Afficher contradictions et incertitude ; interdire les citations inventées.
  - **Dépendances :** 4, 6, 9, 10.
  - **Fin :** chaque claim factuel d’une fixture est sourcé ou explicitement non vérifié ; modification de source ne retargete pas la citation.
  - _Requirements: 4.1–4.7, 7.3, 20.1, 20.4_

- [ ] 12. Valider et livrer le lot vertical initial
  - Vérifier migration, déduplication blob, import PDF paginé, API/SSE, annulation, restauration de trace et accessibilité réduite.
  - Vérifier absence d’egress pour ce lot et compatibilité des routes historiques.
  - Construire le bundle Windows et exécuter un smoke test hors ligne.
  - **Dépendances :** 1–11.
  - **Fin :** critères du lot reproduits sur source et exécutable gelé ; régressions corrigées.
  - _Requirements: 16.1, 19.1–19.6, 20.1–20.6_

## P0 complémentaire — Sécurité et robustesse de plateforme

- [ ] 13. Centraliser la politique egress et le récupérateur HTTP anti-SSRF
  - Modes local-only, métadonnées, extraits et fichiers ; consentement par destination.
  - Validation DNS/IP avant connexion et après chaque redirection, limites de temps/taille/type.
  - **Dépendances :** 1, 5.
  - **Fin :** adresses privées/réservées et redirections interdites sont bloquées ; destinations autorisées sont auditées.
  - _Requirements: 16.1, 16.2, 16.5, 16.8, 20.5_

- [ ] 14. Ajouter coffre de secrets et assainissement des contenus
  - Abstraction Credential Manager Windows, fallback non silencieux et migration des secrets hérités.
  - Sanitization HTML/Markdown ; séparation stricte instructions/données PDF, web et OCR.
  - Expurgation des logs.
  - **Dépendances :** 1, 13.
  - **Fin :** aucun secret dans localStorage/log/export ; fixtures d’injection n’exécutent ni HTML ni action outil.
  - _Requirements: 16.3, 16.4, 16.6, 16.7, 19.1, 20.5_

- [ ] 15. Finaliser la migration des données historiques
  - Importer documents aplatis, conversations et préférences ; marquer la provenance limitée.
  - Ajouter sauvegarde, rapport de migration et procédure de restauration.
  - **Dépendances :** 2, 4, 14.
  - **Fin :** migration rejouée deux fois sans doublon ni perte ; données non ancrables correctement étiquetées.
  - _Requirements: 19.1, 19.2, 19.5, 19.6_

## P2 — Découverte scientifique et bibliographie

- [ ] 16. Définir le contrat des connecteurs et les fixtures de fournisseur
  - Interface commune, normalisation, pagination, cache, quotas, backoff et annulation.
  - Jeux de réponses enregistrées ne contenant ni secret ni données privées.
  - **Dépendances :** 10, 13, 14.
  - **Fin :** un faux connecteur couvre succès, pagination, quota et erreur partielle.
  - _Requirements: 6.3–6.7, 16.1–16.8_

- [ ] 17. Implémenter Crossref, OpenAlex et arXiv
  - Recherche, récupération par identifiant et provenance champ par champ.
  - Fusion DOI/arXiv/titre-auteurs sans perte de versions.
  - **Dépendances :** 16.
  - **Fin :** recherche fédérée sur fixtures déduplique et conserve les trois provenances.
  - _Requirements: 2.2–2.4, 6.1, 6.3–6.6_

- [ ] 18. Implémenter PubMed et Semantic Scholar
  - Respecter authentification, quotas et champs disponibles.
  - Rendre l’absence ou l’indisponibilité non bloquante.
  - **Dépendances :** 16.
  - **Fin :** résultats biomédicaux et graphes/citations sont normalisés ou explicitement partiels.
  - _Requirements: 6.1, 6.3–6.6_

- [ ] 19. Implémenter les connecteurs ML spécialisés
  - Vérifier puis intégrer OpenReview, Papers with Code et Hugging Face selon leurs APIs et licences actuelles.
  - Mapper reviews, code, tâches, datasets, modèles, model cards, benchmarks et licences.
  - **Dépendances :** 16.
  - **Fin :** capacités activées dynamiquement ; fournisseur absent ne casse pas la recherche ML.
  - _Requirements: 5.3–5.5, 6.2–6.6_

- [ ] 20. Construire la recherche fédérée et son classement explicable
  - Exécuter le plan local avec budgets, déduplication et collecte partielle.
  - Afficher facteurs de classement et permettre l’ajout à la bibliothèque.
  - Conserver requêtes, réponses, décisions et avertissements dans la trace.
  - **Dépendances :** 17–19.
  - **Fin :** une recherche multi-fournisseurs annulable produit résultats fusionnés et trace complète.
  - _Requirements: 5.2, 6.3–6.7, 7.2–7.6, 17.1–17.7_

- [ ] 21. Étendre l’interface de bibliothèque
  - Recherche FTS, pagination, tris/filtres, fiche Paper, versions, artefacts, provenance, collections et opérations en lot.
  - **Dépendances :** 4, 7, 20.
  - **Fin :** bibliothèque volumineuse navigable sans charger tous les contenus.
  - _Requirements: 8.1, 8.2, 8.6, 18.4, 18.5_

- [ ] 22. Ajouter BibTeX, RIS, CSL-JSON et Zotero
  - Imports validés/dédupliqués ; exports fidèles ; Zotero sous consentement explicite.
  - **Dépendances :** 14, 21.
  - **Fin :** aller-retour des fixtures conserve identifiants et champs ; erreurs sont rapportées par entrée.
  - _Requirements: 8.3–8.5, 16.2, 16.3_

## P3 — Synthèse, carnet de connaissances et rédaction

- [ ] 23. Implémenter la comparaison multi-articles sourcée
  - Matrice configurable et cellules reliées à des preuves.
  - Détection des métriques/protocoles incompatibles et contradictions.
  - **Dépendances :** 11, 20, 21.
  - **Fin :** chaque cellule factuelle résout une citation ou affiche `inconnu`.
  - _Requirements: 4.6, 9.1, 9.2, 9.5_

- [ ] 24. Implémenter les revues de littérature versionnées
  - Question, stratégie, filtres, screening, motifs d’inclusion/exclusion et révisions.
  - Synthèse séparant faits, interprétations, contradictions et lacunes.
  - **Dépendances :** 20, 23.
  - **Fin :** une revue peut être reproduite depuis sa révision sans écraser la précédente.
  - _Requirements: 9.3–9.6_

- [ ] 25. Ajouter notes, annotations, tags et glossaire
  - Ancrages multi-entités, sélection de passage/zone, ré-ancrage proposé lors d’une nouvelle version.
  - Recherche et fusion contrôlée des termes/tags.
  - **Dépendances :** 4, 21.
  - **Fin :** annotations historiques restent sur leur version et exports conservent les liens.
  - _Requirements: 10.1–10.5_

- [ ] 26. Ajouter l’export Markdown puis DOCX/LaTeX/Overleaf
  - Représentation intermédiaire commune avec claims, citations et bibliographie.
  - Markdown natif ; renderers optionnels et paquet Overleaf local.
  - **Dépendances :** 11, 22–25.
  - **Fin :** export régénérable avec hash/options/sources ; absence d’un renderer ne perd pas le contenu.
  - _Requirements: 15.1–15.3, 15.6_

## P4 — Veille, graphe, données et expériences

- [ ] 27. Implémenter veilles et exécutions différées
  - Requêtes, fournisseurs, filtres, fréquence et détection des nouveautés.
  - Rattrapage au démarrage si aucun scheduler permanent.
  - **Dépendances :** 5, 20.
  - **Fin :** deux exécutions successives ne notifient que les changements.
  - _Requirements: 11.1–11.3_

- [ ] 28. Construire le graphe scientifique provenance-aware
  - Citations, auteurs, sujets, artefacts et collections ; relations explicites ou inférées étiquetées.
  - Chargement progressif et filtres.
  - **Dépendances :** 20, 21, 27.
  - **Fin :** chaque arête résout sa provenance ou sa méthode/score d’inférence.
  - _Requirements: 11.4, 11.5, 18.4_

- [ ] 29. Importer CSV/Excel comme datasets versionnés
  - Aperçu, détection de schéma, ambiguïtés, fichiers sources et versions.
  - **Dépendances :** 3, 4, 5.
  - **Fin :** conversions ambiguës demandent confirmation ; source et schéma restent traçables.
  - _Requirements: 12.1, 12.2_

- [ ] 30. Ajouter statistiques et graphiques reproductibles
  - Recettes, paramètres, seed, environnement, hypothèses, données manquantes et unités.
  - Graphiques régénérables ; exécution de code isolée et uniquement consentie.
  - **Dépendances :** 14, 29.
  - **Fin :** une analyse rejouée compare résultats et causes d’écart connues.
  - _Requirements: 12.2–12.6, 16.4_

- [ ] 31. Ajouter expériences ML et fiches de reproductibilité
  - Hypothèse, code/commit, environnement, dataset, modèle, paramètres, matériel, métriques, logs et artefacts.
  - Comparaison de runs avec incompatibilités explicites.
  - **Dépendances :** 23, 29, 30.
  - **Fin :** un résultat cité résout le run et ses artefacts exacts.
  - _Requirements: 13.1–13.5_

## P5 — Assistants spécialisés et intégrations

- [ ] 32. Ajouter traduction sourcée et flashcards vérifiables
  - Préserver langue, modèle, source/ancrages et validation utilisateur.
  - **Dépendances :** 11, 25.
  - **Fin :** chaque carte et passage traduit revient à sa source.
  - _Requirements: 14.1, 14.2, 14.6_

- [ ] 33. Ajouter transcription horodatée et diff de versions
  - Média, segments, langue, moteur, corrections ; diff texte/structure/métadonnées.
  - **Dépendances :** 3, 4, 5.
  - **Fin :** segment ou changement ouvre le bon timestamp/emplacement.
  - _Requirements: 14.3, 14.4_

- [ ] 34. Ajouter le mode reviewer sourcé
  - Résumé, forces, faiblesses, questions, vérifications et suggestions séparés.
  - **Dépendances :** 11, 23.
  - **Fin :** assertions factuelles sourcées ; absence de preuve visible.
  - _Requirements: 14.5, 14.6_

- [ ] 35. Intégrer Jupyter sans exécution implicite
  - Import/génération de notebook, cellules, références de données et provenance.
  - **Dépendances :** 14, 29, 30.
  - **Fin :** notebook exporté reproductible et aucune cellule non fiable exécutée automatiquement.
  - _Requirements: 15.4, 16.4_

- [ ] 36. Intégrer GitHub et OSF avec aperçu d’egress
  - Coffre de secrets, sélection des fichiers/données, confirmation et journal de transfert.
  - **Dépendances :** 13, 14, 26, 31.
  - **Fin :** aucun transfert avant consentement ; résultat et échecs partiels auditables.
  - _Requirements: 15.5, 16.1–16.3, 16.8_

## Validation finale et distribution

- [ ] 37. Exécuter la validation transversale de provenance et sécurité
  - Tester intégrité de blob, citations immuables, tombstones, transformations, SSRF, injection, HTML actif et secrets.
  - **Dépendances :** 13–36 applicables.
  - **Fin :** aucun cas critique ouvert ; limites documentées pour les capacités optionnelles.
  - _Requirements: 20.1–20.5_

- [ ] 38. Valider accessibilité, performance et récupération
  - Clavier, lecteur d’écran, reduced motion, corpus volumineux, index partiel, annulation et reprise.
  - **Dépendances :** 9, 21, 28, 30.
  - **Fin :** parcours principaux utilisables et opérations longues non bloquantes.
  - _Requirements: 18.1–18.6_

- [ ] 39. Construire et tester la distribution Windows complète
  - Inclure migrations, schémas et assets ; exclure données utilisateur et secrets.
  - Smoke tests offline : migration, import, bibliothèque, recherche locale, citation, trace repliable et export Markdown.
  - **Dépendances :** 37, 38.
  - **Fin :** exécutable gelé démarre sur profil neuf et migré ; hash et rapport de validation enregistrés.
  - _Requirements: 19.4–19.6, 20.6_

- [ ] 40. Vérifier la couverture de la spec et clôturer la livraison
  - Relire les 20 exigences, associer preuves de validation et signaler explicitement toute capacité non livrée.
  - **Dépendances :** 39.
  - **Fin :** matrice exigences → tâches → validations complète, sans critère déclaré satisfait sans preuve.
  - _Requirements: 1.1–20.6_
