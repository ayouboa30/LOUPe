# 3loop

Framework Python asynchrone de débat multi-agents, livré comme application
de bureau Windows. Chaque cycle exécute trois identités sur un backend LLM
partagé :

1. **Heuristique** — propose une esquisse, une preuve ou un algorithme.
2. **Critique** — cherche les hypothèses cachées, les erreurs, les cas limites.
3. **Rédacteur** — intègre les corrections et produit la réponse finale.

Les trois votent ensuite indépendamment. Une majorité de deux arrête la
boucle ; sinon l'historique est réinjecté dans le cycle suivant.

Deux rôles de support ne votent pas : **Contexte** distille ce qui est
transmis d'un cycle à l'autre, **Chercheur** résume les résultats web avant
qu'ils n'atteignent le débat.

## Application de bureau

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Produit `dist\3loop\3loop.exe` (mode `--onedir` : le `--onefile` ré-extrayait
~350 Mo à chaque lancement). L'app ouvre une fenêtre WebView2 et une
mascotte flottante — clic gauche pour ouvrir l'app, clic droit pour la
fermer, et au survol deux actions : micro (dictée hors ligne) et loupe
(capture d'écran + OCR).

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

Extras : `desktop` (app Windows), `web` (recherche DDG), `llama`
(llama-cpp-python), `litellm`, `airllm`.

## Utilisation en ligne de commande

```bash
python -m three_loop "Implementer une recherche binaire en Python" --cycles 4
```

## API

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
