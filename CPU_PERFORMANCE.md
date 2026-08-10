# Performance CPU : mesures et décisions

Toutes les mesures ci-dessous ont été prises sur la machine cible, avec le
modèle réellement utilisé. Aucune n'est reprise d'un benchmark tiers.

**Banc d'essai** — AMD Ryzen 7 5825U (8 cœurs physiques / 16 logiques),
32 Go DDR4-3200 dual-channel, Qwen2.5-Coder-3B-Instruct **déjà en Q4_K_M**
(1,79 Gio, 4,99 bits/poids), llama-cpp-python.

> **Attention en lisant les §1 à §7.** Ils ont été mesurés sur
> Qwen2.5-Coder-3B via `llama-cpp-python`. L'application sert aujourd'hui
> **Qwen3 1.7B/4B via le serveur Ollama**, qui est un modèle plus petit et un
> chemin de code différent. Le §8 reprend les mesures sur ce chemin-là, et
> **contredit le §2 sur le nombre de threads** : les conclusions d'un banc
> d'essai ne se transportent pas d'un modèle à l'autre.

---

## 1. Où part réellement le temps

En faisant varier la longueur du prompt à génération constante, puis en
ajustant une droite :

| grandeur | mesure |
|---|---|
| Prefill | **70 tok/s** (14,3 ms par token de prompt) |
| Décodage | **22,4 tok/s** (44,6 ms par token généré) |

Le décodage à 22,4 tok/s correspond au plafond de bande passante mémoire
(1,79 Gio de poids à relire par token sur ~40 Go/s effectifs ≈ 22 tok/s).
On ne peut pas le rendre plus rapide : c'est une limite physique. On peut
en revanche générer **moins de tokens**.

**Un token généré coûte 3,1 tokens de prompt.** C'est le rapport qui décide
de tout, et il fait basculer le diagnostic selon la longueur de réponse :

| profil | prefill | décodage | dominante |
|---|---|---|---|
| prompt 1500, réponse 32 | 21,4 s | 1,4 s | prefill (94 %) |
| prompt 1500, réponse 334 *(débat compact réel)* | 21,4 s | 14,9 s | **prefill 59 % / décodage 41 %** |
| prompt 1500, réponse 900 | 21,4 s | 40,2 s | décodage (65 %) |

Une première version de ce document annonçait « prefill = 94 % du temps »
comme un fait général : c'était mesuré avec une génération de 32 tokens,
non représentative. Sur la charge réelle, **les deux comptent**, et il faut
travailler les deux.

## 1 ter. Faut-il quitter llama.cpp ? Analyse roofline

Question légitime : rien n'oblige à passer par llama.cpp puisqu'on a les
poids. La réponse se lit sur un modèle de performance, pas sur une opinion.

### Le décodage est borné par la mémoire, le prefill par le calcul

Intensité opérationnelle (Q4_K_M stocke 4,99 bits/paramètre, soit
0,624 octet ; le décodage fait ~2 FLOP par poids relu) :

| noyau | intensité | régime |
|---|---|---|
| décodage (batch 1) | 3,2 FLOP/octet | **mémoire** |
| prefill (batch 32) | 103 FLOP/octet | **calcul** |
| prefill (batch 512) | 1642 FLOP/octet | **calcul** |

Point d'inflexion mesuré ≈ 14,5 FLOP/octet (SGEMM 315 GFLOP/s ÷ bande
passante). Cela explique quantitativement l'observation empirique du §2 :
le décodage plafonne à 4 threads, le prefill continue de scaler jusqu'à 12.
Ce ne sont pas deux réglages à ajuster au doigt mouillé, ce sont deux
régimes distincts du roofline.

### Combien de marge reste-t-il en décodage ?

llama.cpp obtient **33,8 GB/s** effectifs sur le 3B (18,8 tok/s × 1,80 Go).
Le théorique DDR4-3200 dual-channel est 51,2 GB/s, soit **66 % atteints** —
ce qui est la fourchette normale d'un vrai workload sur une puce mobile
15 W. La marge résiduelle en décodage est donc au mieux de l'ordre de
1,1–1,5×, et seulement en améliorant les motifs d'accès mémoire, ce que
llama.cpp fait déjà bien.

**Trois tentatives de mesurer le pic de bande passante ont échoué**, et
chacune a été détectée par une borne physique plutôt qu'à l'œil :

| méthode | résultat | pourquoi c'est faux |
|---|---|---|
| threads + médiane | 21,8 GB/s | mettrait llama.cpp à **156 % de son propre plafond** ; les réductions numpy ne libèrent pas fiablement le GIL, les « 16 threads » sérialisaient |
| processus + somme des meilleurs | 95,9 GB/s | **187 % du théorique** ; chaque worker atteint son pic à un instant différent, les sommer compte une bande passante jamais simultanée |
| processus + barrière (correct) | 32,1 GB/s | méthodologie juste, mais **sous le plancher de 33,8 GB/s** que llama.cpp atteint |

Le troisième échec est le plus instructif : la méthode est bonne, mais les
noyaux numpy sont de moins bons streamers que les kernels AVX2 de
llama.cpp. **Un instrument ne peut pas borner par le haut ce qu'il ne sait
pas atteindre lui-même.** llama.cpp reste donc la meilleure sonde
disponible sur cette machine, et son propre débit est le plancher.

### Conséquence

Aucun runtime alternatif — ONNX Runtime, CTranslate2, PyTorch, OpenVINO —
ne peut transformer un noyau borné par la mémoire. Le seul levier à facteur
3 reste la **taille du modèle** (§1 bis).

Le **prefill** est la seule phase où d'autres noyaux (tinyBLAS de
llamafile, oneDNN/MKL) peuvent réellement gagner, puisqu'il est borné par
le calcul. Il pèse ~59 % du temps : un 2× hypothétique n'y donnerait
qu'environ 1,4× au total.

## 1 bis. Taille du modèle — le seul levier qui donne un facteur 3

C'est la réponse aux « LLM CPU à des centaines de tokens/s » : ils existent,
sur des modèles plus petits. Mesuré sur cette machine, même quantification
Q4_K_M, même réglages :

| modèle | taille | prefill | décodage |
|---|---|---|---|
| Qwen2.5-Coder **0.5B** | 0,37 Go | **260 tok/s** | **58,5 tok/s** |
| Qwen2.5-Coder **3B** | 1,80 Go | 68,7 tok/s | 18,8 tok/s |

**3,8× en prefill, 3,1× en décodage.** Aucun réglage logiciel n'approche ce
facteur : la vitesse suit 1/taille, comme le prédit la bande passante.

**Appliqué : routage à deux niveaux.** Les rôles de support (contexte,
chercheur) ne raisonnent pas sur la tâche, ils résument — la taille du
modèle n'y achète rien. Ils tournent donc sur le plus petit GGUF installé,
le débat gardant le gros modèle. `server.py::_support_backend` choisit
automatiquement, et ne bascule que si le petit modèle fait moins de 60 % du
gros. Repli sur le modèle principal pour les backends cloud, où cette
asymétrie de coût n'existe pas.

## 2. Nombre de threads — appliqué

Le code utilisait `os.cpu_count()`, soit 16 threads sur 8 cœurs physiques.

| threads | tok/s | écart |
|---|---|---|
| 4 | 61,2 | +10,5 % |
| 6 | 61,3 | +10,6 % |
| **8** (physiques) | **61,3** | **+10,6 %** |
| 12 | 57,9 | +4,5 % |
| 16 (ancien défaut) | 55,4 | référence |

Le débit plafonne dès 4 threads : le **décodage** est limité par la bande
passante mémoire, pas par le calcul. Les threads SMT supplémentaires
n'apportent pas de bande passante et ajoutent de la contention.

Mais le **prefill** est du calcul matriciel, et lui continue de scaler :

| `n_threads_batch` | prefill |
|---|---|
| 4 | 48,1 tok/s |
| 8 | 57,7 tok/s |
| **12** | **68,4 tok/s** |
| 16 | 67,6 tok/s |

Les deux phases veulent donc des réglages **différents** — utiliser une
seule valeur laissait ~18 % de prefill sur la table.
→ `n_threads` = cœurs physiques (décodage), `n_threads_batch` = 1,5× cœurs
physiques (prefill).

## 3. Réutilisation du cache KV — le vrai gisement

llama.cpp réutilise le plus long préfixe de tokens identique à l'appel
précédent. Mesuré avec un préfixe de ~724 tokens :

| appel | temps | |
|---|---|---|
| à froid | 9,54 s | prefill complet |
| même préfixe, suffixe différent | **1,13 s** | 8,4× |
| texte **ajouté à la fin** | **1,02 s** | 9,4× |
| longueur identique, aucun préfixe commun | 7,41 s | témoin |
| texte **inséré au début** | **9,45 s** | **aucune réutilisation** |

3loop insérait l'historique **au milieu** du prompt : tout ce qui suivait
était invalidé et re-préfillé à chaque cycle.

**Correction appliquée** — les sections du prompt sont désormais ordonnées
du plus stable au plus volatil (`latent.py`) :

1. protocole fixe + schéma JSON (identique à chaque appel)
2. règles de formatage (stables par type de tâche)
3. `TASK` (stable sur tout le run)
4. `SOURCES` (stables après la recherche)
5. `PREVIOUS CYCLES` — **croît par ajout, donc en dernier**
6. numéro de cycle + rappel de la tâche (court, re-préfillé à chaque fois)

`ConversationHistory.render` est append-only : le rendu du cycle N+1
prolonge exactement celui du cycle N (verrouillé par un test).

**Effet mesuré, run 2 cycles complet :**

| | cycle 1 | cycle 2 | total |
|---|---|---|---|
| ancien agencement | 6,28 s | 9,84 s | 16,13 s |
| nouvel agencement | 6,57 s | **5,83 s** | **12,39 s** |

−41 % sur le cycle 2, −23 % au total, et l'écart grandit avec le nombre de
cycles puisque seul le cycle 1 paie le coût à froid.

## 4. Compaction par suppression des voyelles — retirée

L'idée était de réduire le contexte en ne gardant que les consonnes. Mesuré
avec le tokenizer du modèle :

| échantillon | tokens avant | tokens après | caractères |
|---|---|---|---|
| prose FR | 57 | 60 | 213 → 132 |
| prose EN | 28 | **53** | 171 → 114 |
| code | 27 | 26 | 87 → 53 |
| **total** | **152** | **178 (+17 %)** | −38 % |

Le texte est 38 % plus court en caractères mais coûte **17 % de tokens en
plus**. Les fusions BPE sont apprises sur des mots réels : « heuristic »
vaut 1 à 2 tokens, « hrstc » se fragmente en un token par consonne. À
14,3 ms le token de prompt, cela ajoutait ~3,6 s par appel sur un prompt de
1500 tokens — tout en rendant le contexte illisible pour le modèle.

Remplacée par une compaction qui retire de vrais tokens : espaces
normalisés, tournures de remplissage supprimées, et budget qui conserve les
entrées récentes entières plutôt que de dégrader l'ensemble. La fonction
`strip_vowels` reste dans `compact.py`, non utilisée, pour que la mesure
reste reproductible.

## 5. `LlamaRAMCache` — retiré

Il duplique la réutilisation déjà faite par llama.cpp tout en copiant l'état
KV à chaque appel : mesuré 8,8× contre 9,5× sans lui sur le même préfixe.

## 6. flash-attention — activé

+4 % environ (dans le bruit, mais jamais négatif), et c'est un prérequis
pour quantifier le cache KV si le besoin s'en présente. Activé avec repli
silencieux si la version de llama-cpp-python ne l'accepte pas.

## 7. Quantification du modèle — rien à faire

Le modèle servi par Ollama est **déjà en Q4_K_M** (1,79 Gio, 4,99 bpw).
Une version précédente de ce document conseillait de le quantifier depuis
un format 7 Go : c'était faux, l'hypothèse n'avait pas été vérifiée.

Descendre en Q3 ferait gagner ~20 % de bande passante contre une perte de
qualité nette sur du raisonnement technique — mauvais compromis ici, vu que
le décodage ne représente que ~6 % du temps.

---

## Ce qu'il reste à gagner

Par ordre de rendement décroissant :

1. **Réduire les tokens de prompt** — c'est 94 % du temps. Chaque phrase
   retirée du protocole, des règles de formatage ou de l'historique vaut
   14,3 ms par token, à chaque cycle.
2. **Baisser `max_cycles`** — chaque cycle est un appel complet. Le mode
   compact (1 appel au lieu de 6) est déjà le défaut.
3. **Modèle plus petit pour le débat aussi** — c'est le seul levier qui
   donne un facteur 3 (voir §1 bis). Arbitrage produit, pas optimisation :
   à toi de juger si le 0.5B ou un 1.5B tient la qualité sur tes tâches.

## Multiplier les agents sur un petit modèle — testé, ne fonctionne pas

Hypothèse : puisque le 0.5B va 3× plus vite, compenser sa faiblesse en
faisant voter plusieurs agents. Testé sur 12 problèmes à réponse
numérique vérifiable, vote majoritaire sur N échantillons :

| condition | score | temps |
|---|---|---|
| **3B × 1** | **11/12 (92 %)** | 141 s |
| 0.5B × 1 | 0/12 (0 %) | 52 s |
| 0.5B × 3 | 4/12 (33 %) | 129 s |
| 0.5B × 5 | 1/12 (8 %) | 229 s |
| 0.5B × 9 | 2/12 (17 %) | 1180 s |

À budget de temps égal (129 s contre 141 s), le 0.5B × 3 plafonne à 33 %
contre 92 %. Et pousser plus loin **dégrade** : ×9 coûte 8× le temps du 3B
pour 17 %.

La cause est visible dans les sorties brutes : sur « calcule 17 × 23 » le
0.5B recopie le gabarit de réponse en boucle sans jamais substituer un
nombre ; sur « 15 % de 240 » il répond 120 puis répète cinq fois la même
question. Ce sont des effondrements dégénératifs, pas des erreurs de
calcul.

C'est la distinction qui décide : **le vote majoritaire corrige
l'inconsistance, pas l'incapacité.** Il fait émerger une bonne réponse
présente mais mal échantillonnée. Quand aucune bonne réponse n'existe dans
la distribution, il agrège du bruit — et les agents se confortent
mutuellement dans l'erreur.

### Reprise statistique — une conclusion à corriger

Les scores ci-dessus sont des estimations ponctuelles sur 12 tirages
binaires. Intervalles de Wilson à 95 % (valides à k=0, contrairement à
l'approximation normale) et tests exacts de Fisher :

| condition | score | IC 95 % |
|---|---|---|
| 3B × 1 | 92 % | [65 %, 99 %] |
| 1.5B × 1 | 42 % | [19 %, 68 %] |
| 0.5B × 1 | 0 % | [0 %, 24 %] |
| 0.5B × 3 | 33 % | [14 %, 61 %] |
| 0.5B × 9 | 17 % | [5 %, 45 %] |

**Ce qui tient :** l'effet de la taille du modèle. 3B vs 1.5B p = 0,027 ;
3B vs 0.5B p < 0,001 ; 1.5B vs 0.5B p = 0,037. Les trois sont significatifs.

**Ce qui ne tient pas :** *aucun* contraste sur le vote majoritaire n'est
significatif. 1.5B ×1 vs ×3 : p = 1,000. 0.5B ×1 vs ×3 : p = 0,093. 0.5B ×3
vs ×9 : p = 0,640. Détecter un écart de 42 % à 33 % demanderait **453
tâches par bras**, pas 12 — l'expérience était sous-dimensionnée d'un
facteur ~38.

J'avais écrit plus haut « multiplier les agents ne fonctionne pas » en
m'appuyant sur la série 33 %/8 %/17 %. C'est une surinterprétation : cette
série est du bruit, et l'expérience n'avait pas la puissance de mesurer
l'effet du vote, ni dans un sens ni dans l'autre.

**Ce qui reste solide sur le 0.5B** n'est pas son score mais le *mécanisme*
observé dans les sorties brutes : recopie du gabarit de réponse en boucle,
répétition de la même question cinq fois. Une observation qualitative
directe d'effondrement dégénératif, indépendante de la taille
d'échantillon. À noter cependant qu'elle confond deux choses — incapacité
de raisonnement et non-respect du format de sortie.

**Conception d'éval future** : 74 tâches par bras pour détecter une perte
de 17 points avec 80 % de puissance. Les 12 tâches actuelles ne suffisaient
(n = 13 requis) que pour le contraste 3B vs 1.5B, et de justesse.

## iGPU à mémoire partagée — validation du roofline

Le Ryzen 5000U embarque un Radeon Vega sur **le même contrôleur mémoire**
que le CPU. Cette mémoire unifiée supprime la raison habituelle pour
laquelle l'offload iGPU ne paie pas : il n'y a pas de copie hôte→device,
les poids sont déjà là où le GPU les lit.

Le roofline prédit un effet **dissymétrique** : l'iGPU ajoute des FLOPS
mais pas de bande passante, donc il devrait aider le prefill (borné calcul)
et presque pas le décodage (borné mémoire). A/B contrôlé, même runtime,
même prompt, même modèle, iGPU basculé :

| phase | CPU | iGPU Vulkan | gain |
|---|---|---|---|
| prefill (~100–1600 FLOP/octet) | 86,8 tok/s | **114,5 tok/s** | **+32 %** |
| décodage (3,2 FLOP/octet) | 13,8 tok/s | 15,0 tok/s | +9 % |

**Le rapport des gains est de 3,6× en faveur de la phase bornée calcul** —
la prédiction est vérifiée quantitativement, pas seulement qualitativement.

### Quand cela vaut-il le coup ?

Comparé au chemin CPU utilisé par défaut (llama-cpp-python : 68,7 tok/s
prefill, 18,8 tok/s décodage), l'iGPU est plus rapide en prefill mais
**plus lent en décodage**. Le point d'équilibre :

> `tokens_de_prompt / tokens_generes > 2,3`

Le profil mesuré de 3loop (~1500 prompt, ~334 générés, ratio 4,5) le
satisfait largement. Et **réduire la génération rend l'iGPU plus
intéressant, pas moins** — les deux optimisations se renforcent.

### Piège de configuration

Ollama embarque le backend Vulkan mais **écarte les GPU intégrés par
défaut**, en journalisant `dropping integrated GPU` et en basculant sur le
CPU sans que rien ne l'indique dans l'interface. Il faut lancer le serveur
avec `OLLAMA_VULKAN=1` **et** `OLLAMA_IGPU_ENABLE=1`. Une fois activé,
l'iGPU dispose de 15,9 Gio via la mémoire unifiée — bien au-delà des
512 Mo qu'annonce Windows.

### Vérification sur des démonstrations mathématiques

Le modèle de coût prédisait un **résultat nul** sur ce profil : une preuve
est générative (prompt court, réponse longue), donc très en dessous du
seuil. Six démonstrations classiques (irrationalité de √2, Pythagore,
infinité des premiers, somme des n premiers entiers, dérivée de sin,
non-dénombrabilité de ℝ), notées sur une grille d'étapes attendues :

| | score | débit normalisé | ratio prompt/généré |
|---|---|---|---|
| CPU | 63 % | **14,44 tok/s** | 0,10 |
| iGPU | 63 % | 13,89 tok/s (**−3,8 %**) | 0,10 |

**Prédiction confirmée** : aucun gain, et même une légère perte. Un résultat
nul annoncé à l'avance valide le modèle plus solidement qu'un gain, parce
qu'il ne pouvait pas être obtenu par hasard.

La qualité est identique (63 % des deux côtés, même écart-type de 22) — ce
qui est attendu puisque c'est le même modèle et les mêmes poids. À noter :
les scores **par problème** varient jusqu'à 50 points entre les deux bras.
C'est du bruit d'échantillonnage, pas un effet du matériel ; avec n=6, seule
la moyenne est lisible, et la grille détecte une étape manquante, pas un
raisonnement subtilement faux.

### Routage automatique

Puisque le bon choix dépend de la forme de la requête et que la longueur du
prompt est connue avant l'appel, `should_use_igpu(prompt_chars, max_tokens)`
tranche automatiquement. La génération est estimée à 37 % de `max_tokens`
(mesuré : ~334 tokens produits pour un plafond de 900) — utiliser le plafond
brut surestimerait la génération et n'enverrait jamais rien à l'iGPU.

| profil | routage |
|---|---|
| démonstration mathématique | CPU |
| débat compact 3loop (~1500 tokens de contexte) | **iGPU** |
| capture OCR longue | **iGPU** |
| question courte | CPU |

### Implémentation

Ces variables ne sont lues qu'**au démarrage** du serveur : impossible
d'activer l'iGPU sur une instance déjà lancée. Plutôt que de redémarrer le
service Ollama du système — que d'autres outils utilisent peut-être —
3loop lance **son propre serveur** sur un port privé (11719), sans fenêtre,
au premier usage du backend `igpu`. Mesuré : 3,1 s de démarrage, modèle
chargé à **100 % GPU**.

`three_loop/igpu.py` expose `ensure_server()` (retourne `None` si Ollama est
absent ou refuse de démarrer, pour que l'appelant retombe sur le CPU sans
échouer) et `probe()` (état remonté par `/api/config`). Le backend apparaît
dans l'interface sous « iGPU local (Vulkan) ».

## Architecture séquentielle — ce que le modèle de coût autorise

Modèle de coût mesuré sur le 3B : **53 ms par token décodé, 14,6 ms par
token de prompt**. Un token généré coûte 3,6 tokens lus. Toute
réorganisation doit donc viser le décodage.

### Cascade brouillon → vérification : falsifiée

Idée : le 0.5B rédige, le 3B se contente de *lire* le brouillon et
d'émettre un verdict court. On échange du décodage contre du prefill, ce
que le modèle de coût favorise.

Premier résultat, spectaculaire : **34,1 s → 14,5 s, soit −58 %**. Le 3B
lisait 1006 tokens et n'en générait que 3 (« OK »).

Contre-épreuve indispensable : *le vérificateur vérifie-t-il ?* Six
brouillons, trois corrects et trois avec une erreur plantée.

| formulation | détection | comportement observé |
|---|---|---|
| « réponds exactement OK / ERREUR » | 3/6 | rejette **6/6**, commente le format : « Par exemple, si la correction est… » |
| « commence par VRAI ou FAUX » | 4/6 | répète la consigne puis répond **à une autre question** de la série |

Le hasard est à 3/6, et avec n=6 l'intervalle est [22 %, 96 %]. **Un 3B
n'est pas un vérificateur fiable.** Les −58 % venaient d'un tampon
automatique, pas d'une vérification : l'architecture aurait livré du
0.5B (mesuré 0/12) au prix d'une latence supplémentaire.

### Ce qui survit à la mesure

| principe | gain mesuré | statut |
|---|---|---|
| Contexte unique en append-only | 9,4× sur le prefill partagé | déjà appliqué (§3) |
| Ne générer que ce qui est affiché | **−21 %** (34,1 s → 27,0 s) | à faire |
| Cascade avec vérification | — | **falsifiée** |
| Modèle plus petit | 3× | qualité insuffisante (§1 bis) |

**Génération paresseuse des champs de débat — appliqué.** `heuristic`,
`critique` et les trois `rationale` représentent 65 % des tokens générés et
ne sont jamais affichés tant que le panneau latéral reste fermé. Le schéma
JSON demandé les omet désormais (`lazy_debate_fields`, actif par défaut).

Mesuré sur le chemin de production, même tâche, même modèle :

| mode | temps | longueur de la réponse |
|---|---|---|
| complet | 27,3 s | 439 caractères |
| **paresseux** | **19,5 s (−29 %)** | **827 caractères** |

**Le gain porte sur les deux axes à la fois** : 29 % plus rapide *et* une
réponse presque deux fois plus longue, parce que le budget de tokens va
entièrement au champ visible au lieu d'être partagé avec des champs que
personne ne lit. Les votes restent demandés — ils décident s'il faut un
cycle de plus — mais sans leur justification en prose, qui n'entre pas dans
cette décision.

`parse_latent_debate` traite l'absence de ces champs comme normale et non
comme un échec de parsing : les compter comme une erreur marquerait les
votes non résolus et déclencherait un cycle supplémentaire, ce qui coûterait
plus cher que ce que l'optimisation fait gagner.

## Pistes testées et écartées

**Décodage spéculatif (0.5B brouillonne, 3B vérifie).** Mesuré **4,3× plus
lent** : 144 s contre 33,9 s pour 128 tokens, sorties identiques. La cause
n'est pas la méthode mais le binding — `llama-cpp-python` exige
`logits_all=True` sur cette voie, ce qui matérialise les logits (vocabulaire
de 151k) à chaque position. Le surcoût dépasse de loin le gain. La méthode
reste valable dans le serveur C++ natif de llama.cpp ; elle est
inutilisable via ce binding.

**Décodage spéculatif par recopie du prompt** (`LlamaPromptLookupDecoding`,
sans second modèle) : +7 % seulement (13,7 → 14,7 tok/s). Le modèle
reformule au lieu de recopier, donc peu de n-grammes devinés sont acceptés.

**Quantifier plus bas (Q3).** ~20 % de bande passante gagnée contre une
perte nette sur du raisonnement technique. Mauvais échange comparé au
changement de taille de modèle, qui donne 3× pour une perte comparable.

## 8. Le chemin réellement utilisé : Qwen3 via Ollama

Les sections précédentes réglaient `llama-cpp-python`. Or l'interface
sélectionne **Ollama** dès qu'un profil Qwen3 est installé, et ce backend
avait gardé des réglages qui n'avaient jamais été mesurés sur lui.

**Banc d'essai** — même machine, `qwen3:1.7b-flash` (Q4_K_M, 1,36 Go), API
Ollama, prompt ~1420 tokens, 330 tokens générés, médianes de 5 tirages
entrelacés (round-robin, pour qu'une dérive machine touche toutes les
configurations également).

### Nombre de threads — le §2 ne se transporte pas

| threads | prefill | décodage | **total** |
|---|---|---|---|
| 8 | 11,82 s | 18,26 s | 30,41 s |
| **10** | 11,05 s | **17,56 s** | **28,75 s** |
| 12 | 10,08 s | 19,30 s | 29,29 s |
| 14 | 9,53 s | 21,59 s | 31,12 s |
| 16 *(ancien défaut)* | **9,09 s** | 22,35 s | 31,37 s |

Les deux phases vont en sens **opposé**, comme le prédit le roofline : le
prefill est borné calcul et continue de gagner avec les threads, le décodage
est borné bande passante et se dégrade dès que les jumeaux SMT se disputent
la même mémoire.

Le §2 concluait « le décodage plafonne dès 4 threads ». **C'est faux sur ce
modèle** : il continue de s'améliorer jusqu'à ~10-12. Le 1.7B est deux fois
plus petit que le 3B, donc moins étranglé par la bande passante et plus
sensible au calcul. Une conclusion de perf est attachée à son modèle.

**Ce qui est robuste** : la pénalité de décodage à 16 threads, mesurée deux
fois indépendamment (+27 % et +29 % contre la bande 10-12). **Ce qui ne
l'est pas** : l'écart entre 8, 10 et 12, dont les totaux se recouvrent
(dispersion ~4 s par configuration). `_inference_threads()` vise donc le
milieu de cette bande (1,25 × cœurs physiques, borné aux cœurs logiques)
sans prétendre que 10 soit exactement l'optimum. Le point qui compte est de
**ne pas utiliser tous les cœurs logiques**, ce que faisait `os.cpu_count()`.

Réglable par `LOUPE_NUM_THREADS`, l'équilibre dépendant du rapport
prompt/génération et des CPU sans SMT.

### Taille de la fenêtre de contexte — aucun effet sur la vitesse

| `num_ctx` | total |
|---|---|
| 2048 | 27,74 s |
| 4096 | 28,09 s |
| 8192 | 28,42 s |

Écarts dans le bruit : la fenêtre ne coûte que de la **mémoire**
(~112 Ko de cache KV par token sur Qwen3 1.7B, soit ~0,9 Go à 8192), pas du
temps. Elle est donc choisie sur la RAM disponible, **une fois par
processus** — la faire varier par requête forcerait Ollama à réallouer son
cache KV, donc à recharger le modèle entre deux requêtes.

### Dépassement de contexte — silencieux et destructeur

Un prompt d'environ 4300 tokens envoyé avec une fenêtre de 2048 :

| | |
|---|---|
| tokens réellement vus par le modèle | **1026** |
| marqueur placé au tout début, retrouvé ? | **non** |
| réponse | inventée avec assurance |

Ollama **tronque par l'avant**, sans erreur ni avertissement. Or l'agencement
du §3 place en tête ce qui est le plus stable : le protocole et le schéma
JSON. La troncature retire donc exactement les instructions qui rendent la
réponse analysable. `LlamaCppBackend` refusait déjà ce cas avec une erreur
claire ; `OllamaBackend` le fait désormais aussi, avec une estimation
volontairement pessimiste (3,0 caractères/token contre ~3,95 mesuré).

### Un appel de modèle entier qui ne servait à rien

L'agent de contexte distille la réponse du cycle avant de l'ajouter à
l'historique. Mais cet historique n'est relu que par le **cycle suivant**
(`history.render`, en tête de l'itération d'après) et par personne après la
boucle. Au dernier cycle autorisé, cet appel produisait donc un résultat
immédiatement jeté.

Ce n'est pas un cas marginal : depuis que les cycles suivent le niveau de
réflexion, **1 cycle est le défaut** des profils Flash — l'appel gaspillé
représentait alors la moitié des appels du run.

### Cycles : le choix de l'utilisateur était ignoré

`auto_route` valait `True` par défaut et **écrasait** le `max_cycles` envoyé
par l'interface : le sélecteur de cycles de la barre de message ne pouvait
rien changer, et un prompt long et technique déclenchait silencieusement
3 cycles complets. Un `max_cycles` explicite fait désormais autorité ;
l'heuristique ne sert plus qu'aux appelants qui n'expriment aucun choix
(CLI, compagnon de bureau).

### Vérification bout-en-bout — et ce qu'elle ne prouve pas

Les mesures ci-dessus isolent une phase. Reste à voir ce qu'un utilisateur
attend réellement. Le vrai `ThreeLoopPipeline`, 1 cycle, profil Flash,
médianes de 3 tirages :

| configuration | question courte | tâche de code |
|---|---|---|
| avant (16 threads, agent contexte au dernier cycle) | 8,92 s | 21,19 s |
| threads seuls (10 threads) | 8,85 s *(−0,7 %)* | 22,85 s *(**+7,9 %**)* |
| **après (10 threads + agent contexte sauté)** | **3,78 s (−57,6 %)** | **16,66 s (−21,3 %)** |

**Le gain réel vient de l'appel supprimé, pas des threads.** Sur la tâche de
code, le changement de threads seul mesure même *plus lent*.

Il ne faut pas en conclure que le réglage des threads est mauvais : il faut
conclure que **ce banc d'essai ne peut pas trancher cette question-là**. La
dispersion à configuration constante y est de 20,5 à 28,5 s, soit ±20 %,
alors que l'effet cherché vaut ~5 %. La cause est structurelle : en bout-en-bout
le modèle décide lui-même de la longueur de sa réponse, donc le nombre de
tokens générés — et donc le temps — change à chaque tirage. Le banc isolé
fixait `num_predict=330` et obtenait exactement 330 tokens à chaque fois,
ce qui est précisément pourquoi il pouvait mesurer 5 %.

Le réglage des threads est donc conservé sur la foi des mesures par phase
(pénalité de décodage reproduite deux fois à +27 % et +29 %, effet physique
attendu), **pas** sur la foi d'un gain bout-en-bout, qui n'est pas
démontré. `LOUPE_NUM_THREADS` permet de revenir en arrière sans rebuild.

Le gain de l'appel supprimé, lui, est net dans les deux formes de tâche : il
retire un aller-retour complet sur les deux que faisait un run à 1 cycle,
d'où −58 % quand la réponse est courte et −21 % quand elle est longue (le
débat pèse alors davantage face à la distillation).

### Routage à deux niveaux étendu à Ollama

Le §1 bis fait tourner les rôles de support sur le plus petit modèle
installé — mais uniquement pour `LlamaCppBackend`, donc **jamais sur le
chemin par défaut**. Un utilisateur en profil « Élevé » / « Très élevé »
(Qwen3 4B) fait désormais produire ses résumés de contexte et de recherche
par le profil 1.7B Flash. Sans effet pour qui est déjà en 1.7B : mieux vaut
un seul modèle résident que deux.

## Reproduire les mesures

Les scripts de mesure sont volontairement courts et autonomes ; ils sont
décrits dans ce document et peuvent être réécrits en quelques lignes avec
`llama_cpp.Llama` + `time.perf_counter()`. Les grandeurs à surveiller sont
le débit de prefill (ms/token de prompt) et le temps du cycle 2 par rapport
au cycle 1, qui révèle immédiatement une perte de réutilisation du cache.
