# LOUPe beta 0.1.2

Application de bureau expérimentale de débat multi-agents. Cette version est une
**bêta publique** : elle peut contenir des bugs, ralentissements ou changements
incompatibles dans les prochaines versions. Aucun mot de passe Gmail n’est
requis ni inclus dans le programme.

Le moteur Python asynchrone de LOUPe fait débattre plusieurs rôles spécialisés :

1. **Heuristique** — propose une esquisse, une preuve ou un algorithme.
2. **Critique** — cherche les hypothèses cachées, les erreurs, les cas limites.
3. **Rédacteur** — intègre les corrections et produit la réponse finale.

Les trois votent ensuite indépendamment. Une majorité de deux arrête la
boucle ; sinon l'historique est réinjecté dans le cycle suivant.

Deux rôles de support ne votent pas : **Contexte** distille ce qui est
transmis d'un cycle à l'autre, **Chercheur** résume les résultats web avant
qu'ils n'atteignent le débat.

## Les trois compagnons

La bêta garde trois identités visuelles : le chercheur général **LOUPe**, le
compagnon mathématique **MATh** et le compagnon code **CODy**.

| LOUPe — chercheur | MATh — maths | CODy — code |
|---|---|---|
| ![LOUPe chercheur](web/assets/pixel_researcher_strip.png) | ![MATh](web/assets/pixel_pixelbit_strip.png) | ![CODy](web/assets/pixel_cody_strip.png) |

## Portabilité

Le moteur (`three_loop/`) est du Python asyncio pur — aucune dépendance
plateforme obligatoire. L’interface (`web/`) tourne dans
[pywebview](https://pywebview.flowrl.com/) quand un backend GTK/Qt/WebKit est
installé, et retombe sur le navigateur système. Le serveur HTTP/SSE, Ollama,
les GGUF via `llama-cpp-python`, les CLI, les PDF et les fournisseurs cloud
restent les mêmes sous Windows et Linux.

Les fonctions natives sont séparées par plateforme :

| composant | Windows | Linux/macOS |
|---|---|---|
| fenêtre principale | pywebview + WebView2 | pywebview + GTK/Qt, ou navigateur de secours |
| `native_widget.py`, bulles, menu flottant | mascotte Win32 complète | désactivés proprement ; la fenêtre principale reste utilisable |
| OCR d’images/PDF image | WinRT OCR | Tesseract optionnel sous Linux/macOS |
| suivi oculaire | OpenCV/MediaPipe | OpenCV/MediaPipe avec backend caméra générique |
| dictée du widget | WinRT Speech Recognition | message explicite ; port Whisper/Vosk à prévoir |
| sélection de région/notifications natives | Win32 | non disponibles dans cette version |

Le support Linux est donc fonctionnel pour l’application principale et les
modèles locaux, mais il ne prétend pas encore offrir la parité du compagnon
flottant Windows. Les modules Win32 restent isolés et ne sont jamais importés
par le chemin Linux.

## Installation Linux

Sur Debian/Ubuntu, installe les bibliothèques de fenêtre et, si nécessaire,
Tesseract :

```bash
sudo apt install python3-venv python3-tk libgtk-3-0 tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[desktop,desktop-linux,web,llama]"
python desktop_app.py
```

`pytesseract` est uniquement le pont Python : le binaire système
`tesseract-ocr` et les langues choisies restent nécessaires. Le GGUF est
chargé directement depuis le disque avec mmap, exactement comme sous Windows.
Si pywebview ne trouve pas GTK/Qt, LOUPe ouvre automatiquement l’interface
sur `http://127.0.0.1:<port>` dans le navigateur par défaut.

Pour produire un bundle PyInstaller Linux :

```bash
bash build_linux.sh
dist/3loop/3loop
```

Le script ne télécharge aucun modèle et ne tente pas d’installer de paquet
système. Le widget flottant Win32, la capture de région et la dictée restent
désactivés dans ce bundle ; l’OCR Tesseract fonctionne si ses paquets système
sont présents.

## Installation Windows (LOUPe beta 0.1.2)

Télécharge `Setup_LOUPe_beta_0.1.2.exe` depuis
[www.ayoubouladali.com/projects.html#loupe-beta](https://www.ayoubouladali.com/projects.html#loupe-beta)
(ou directement la [dernière release GitHub](https://github.com/ayouboa30/LOUPe/releases/latest))
et lance-le. L’assistant
permet de refuser séparément WebView2, Node.js, Codex, OpenCode, Claude Code,
Ollama, les profils Qwen3 et un éventuel modèle GGUF. Les modèles ne sont
jamais embarqués dans l’EXE : ils sont téléchargés uniquement si l’utilisateur
les sélectionne, dans la limite d’espace disque indiquée.

Par défaut, l’installeur prépare Ollama et les deux poids Qwen3 nécessaires aux
quatre niveaux de réflexion de l’interface :

| niveau | profil | usage |
|---|---|---|
| **Flash lite** | `qwen3:1.7b-flash` | Qwen3 1.7B, réponse rapide sans raisonnement long |
| **Flash** | `qwen3:1.7b` | Qwen3 1.7B, compromis vitesse/raisonnement |
| **Élevé** | `qwen3:4b-flash` | Qwen3 4B, réponse directe |
| **Très élevé** | `qwen3:4b` | Qwen3 4B, raisonnement approfondi |

Les profils `*-flash` sont des Modelfiles locaux : ils réutilisent les poids
1.7B et 4B déjà téléchargés et n’occupent pas quatre fois l’espace. Les
Modelfiles sont inclus dans l’installeur. Les modèles Qwen3 officiels sont
publiés sous Apache-2.0 dans leurs dépôts [GGUF Qwen3 1.7B](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF), [4B](https://huggingface.co/Qwen/Qwen3-4B-GGUF), [8B](https://huggingface.co/Qwen/Qwen3-8B-GGUF), [14B](https://huggingface.co/Qwen/Qwen3-14B-GGUF) et [32B](https://huggingface.co/Qwen/Qwen3-32B-GGUF).

L’option GGUF utilise directement `llama.cpp` via `llama-cpp-python`. Le fichier
quantifié reste dans `{app}\\models` et est chargé avec mmap depuis le disque ;
ce mécanisme évite une copie inutile des poids, mais ne supprime pas les
besoins de RAM, de VRAM ou de cache KV. Le catalogue propose Qwen3 1.7B Q8,
4B Q4_K_M, 8B Q4_K_M, 14B Q4_K_M et 32B Q4_K_M. Les 14B et 32B sont marqués
GPU/RAM élevée : un fichier sur disque n’est pas une garantie qu’un ordinateur
8 Go RAM pourra l’exécuter.

Une connexion Internet est nécessaire pour les composants et modèles choisis.
Les identifiants Gmail ne sont jamais inclus dans l’installateur. Chaque
personne configure son propre OAuth Google dans LOUPe, avec le scope strictement
limité à `gmail.readonly`. Le suivi Python des yeux est annoncé dans l’interface
comme **bientôt disponible** et reste désactivé dans cette bêta.

## Application de bureau (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Produit `dist\3loop\3loop.exe` (mode `--onedir` : le `--onefile` ré-extrayait
~350 Mo à chaque lancement). L'app ouvre une fenêtre WebView2 et une
mascotte flottante.

Fermer la fenêtre principale (❌) la **masque** — l'app et la mascotte
restent actives, un clic sur la mascotte la rouvre. Pour quitter
réellement : **clic droit sur la mascotte → « Fermer la mascotte »**, seule
action qui arrête le processus.

Au survol, deux actions : micro (dictée hors ligne) et loupe (capture
d'écran + OCR, avec une animation de balayage pendant le traitement).

## Backends

| backend | usage |
|---|---|
| **OpenCode** | délègue à un modèle frontière via la CLI OpenCode installée |
| **iGPU local** | Vulkan sur le GPU intégré (voir CPU_PERFORMANCE.md) |
| **GGUF local** | llama-cpp-python, poids chargés directement |
| **Ollama** | serveur Ollama classique |
| Groq / NVIDIA | API cloud gratuites |
| Démo | réponses simulées, aucune installation |

## Installation en bibliothèque

Le cœur n'a aucune dépendance obligatoire :

```bash
python -m pip install -e ".[dev]"
```

Extras : `desktop` (fenêtre principale multiplateforme), `desktop-windows`
(compagnon Win32, OCR et dictée WinRT), `desktop-linux` (pont Tesseract), `web`
(recherche DDG), `llama` (llama-cpp-python), `litellm`, `airllm`.

## Utilisation en ligne de commande

```bash
python -m three_loop "Implementer une recherche binaire en Python" --cycles 4
```

## Lecture Gmail (lecture seule)

L’interface contient un panneau **Lecture Gmail**. Il utilise uniquement le
scope OAuth `gmail.readonly` : LOUPe lit les emails des dernières 24 heures,
hors spam et Promotions, puis affiche l’expéditeur, un résumé en français et
une classification (`publicité`, `travail` ou `autre`). Aucun email n’est
envoyé, supprimé ou modifié.

Pour activer la connexion :

1. Dans le panneau **Lecture Gmail**, clique sur **Obtenir mes identifiants
   OAuth**. Cela ouvre Google Cloud Console dans le navigateur.
2. Crée ou sélectionne un projet, puis va dans **API et services → Bibliothèque**
   et active **Gmail API**.
3. Configure l’**écran de consentement OAuth**. Pour un usage personnel,
   ajoute ton adresse Gmail comme utilisateur test si Google le demande.
4. Va dans **API et services → Identifiants → Créer des identifiants → ID client
   OAuth**, choisis **Application de bureau**, puis crée le client.
5. Copie le **Client ID** et le **Client secret** dans 3loop et clique sur
   **Enregistrer et connecter**. Google ouvrira alors sa propre page de
   connexion pour ton adresse Gmail.

3loop ne demande jamais ton mot de passe Gmail. Les identifiants OAuth sont
envoyés uniquement au serveur local et conservés dans
`~/.3loop/gmail_client.json`. Le token est conservé localement dans
`~/.3loop/gmail_token.json` ; aucun de ces secrets ne passe par le frontend.
Sans modèle disponible ou sans clé cloud configurée, la lecture reste possible
avec un résumé et une classification heuristiques.

## Exemple Python

```python
import asyncio
from three_loop import DemoBackend, PipelineConfig, ThreeLoopPipeline

async def main() -> None:
    pipeline = ThreeLoopPipeline(
        DemoBackend(),
        config=PipelineConfig(max_cycles=3, compact_debate=True),
    )
    result = await pipeline.run("Prouver que la somme de deux pairs est paire")
    print(result.final_solution)

asyncio.run(main())
```

`pipeline.stream(...)` émet les événements au fil de l'eau plutôt que
d'attendre le résultat final.

**`compact_debate`** (actif par défaut) produit les trois contributions et
les trois votes en **un seul appel** au lieu de six. C'est un partage de
contexte et de cache KV, pas un transfert de vecteurs cachés entre rôles —
ce dernier exigerait un modèle entraîné pour les accepter.

**`lazy_debate_fields`** (actif par défaut) ne génère pas les champs qui
n'alimentent que le panneau latéral : 65 % des tokens produits, jamais lus.
Mesuré −29 % de latence *et* une réponse deux fois plus longue, le budget
allant entièrement au texte visible.

## Température bayésienne

`TemperatureOptimizer` maintient un prior Beta par identité :

```text
T = 0.2 + Beta(alpha, beta) * (0.7 - 0.2)
```

Après chaque cycle, la récompense `R` met à jour `alpha += lr * R` et
`beta += lr * (1 - R)`. La récompense combine le ratio de votes positifs, la
validation structurelle et la vitesse d'obtention du consensus. Un
`external_validator` ou un `reward_function` ajoute un signal métier.

## Recherche triangulée

Avec `research=True`, chaque rôle formule sa requête sans voir celles des
autres. `triangulate_sources` ne conserve que les liens vus par au moins
deux agents, ou un représentant d'un domaine commun. Les erreurs d'un
fournisseur restent isolées dans `WebResearchResult.errors`.

## Performance

[CPU_PERFORMANCE.md](CPU_PERFORMANCE.md) contient les mesures qui motivent
les choix d'implémentation : analyse roofline de la machine, réutilisation
du cache KV (9,4×), threads séparés pour prefill et décodage, offload iGPU,
et les pistes testées **puis écartées** — décodage spéculatif, cascade
brouillon/vérification, compaction par suppression de voyelles.

## Structure

```text
three_loop/
  agents.py            # les trois identités, parseurs de votes
  backend.py           # backends partagés (llama.cpp, Ollama, cloud, demo)
  opencode_backend.py  # délégation à la CLI OpenCode
  igpu.py              # détection, serveur Vulkan géré, routage CPU/iGPU
  latent.py            # débat compact en un appel, parsing tolérant
  prompting.py         # préfixe partagé (réutilisation du cache KV)
  support.py           # agents Contexte et Chercheur
  pipeline.py          # boucle asynchrone et consensus
  history.py           # transcript, rendu append-only
  temperature.py       # optimiseur bayésien
  web.py               # recherche et triangulation
  server.py            # serveur HTTP+SSE local
  native_widget.py     # mascotte flottante Win32
  assistant_actions.py # micro (WinRT) et OCR
web/                   # interface (HTML/CSS/JS, KaTeX embarqué)
skills/                # règles de formatage par domaine
tests/
```

## Licence, contributions et usage commercial

LOUPe est un projet **source disponible** : les contributions, corrections,
forks et retours sont bienvenus. La bêta est publiée sous la
[LOUPe Non-Commercial Source License 1.0](LICENSE).

Cette licence autorise l’utilisation, l’étude, la modification et le partage
à des fins personnelles, éducatives, de recherche ou communautaires non
commerciales. La vente, la revente, l’intégration dans un produit ou service
payant, le SaaS payant et toute exploitation à avantage commercial sont
interdits sans autorisation écrite des ayants droit.

Cette licence personnalisée n’est pas une licence open source approuvée par
l’OSI ; le code reste public et ouvert aux contributions, mais la restriction
non commerciale est explicite.
