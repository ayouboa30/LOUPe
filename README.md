# 3loop

`3loop` est un framework Python asynchrone de débat multi-agents pour la génération de code et la résolution mathématique. Chaque cycle exécute trois identités séquentielles sur un backend LLM partagé:

1. **Heuristique**: propose un sketch, une preuve ou un algorithme.
2. **Critique**: cherche les hypothèses cachées, les erreurs et les cas limites.
3. **Rédacteur**: intègre les corrections et produit le code ou le LaTeX final.

Les trois agents votent ensuite indépendamment sur la solution finale. Une majorité absolue de deux votes arrête la boucle; sinon l’historique complet est réinjecté dans le cycle suivant.

## Installation

Le cœur n’a aucune dépendance obligatoire:

```bash
cd 3loop
python -m pip install -e ".[dev]"
```

Extras disponibles:

```bash
python -m pip install -e ".[ui,web]"       # Streamlit + recherche DDG
python -m pip install -e ".[llama]"         # llama-cpp-python local
python -m pip install -e ".[litellm]"       # LiteLLM
```

## Démarrage rapide hors ligne

```bash
cd 3loop
python -m three_loop "Implementer une fonction Python de recherche binaire" --cycles 4
```

Le `DemoBackend` sert aux tests et à la démonstration. Il est remplaçable par `LlamaCppBackend` ou `LiteLLMBackend`; les trois agents reçoivent exactement le même objet backend. L’adaptateur Llama protège l’objet `Llama` par un verrou asynchrone et déporte l’inférence bloquante dans un thread.

## Interface Streamlit

```bash
cd 3loop
streamlit run app.py
```

Pour une utilisation sans installer Python ni taper de commande, construire le lanceur Windows:

```powershell
cd 3loop
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Le fichier `dist\3loop.exe` démarre le serveur local et ouvre automatiquement `http://localhost:8501`. Le premier lancement peut prendre quelques secondes, car l’EXE extrait ses dépendances dans un dossier temporaire.

L’interface affiche les sorties Agent 1 -> Agent 2 -> Agent 3, les votes, les sources triangulées et l’évolution de la moyenne de température de chaque posterior.

## API minimale

```python
import asyncio

from three_loop import DemoBackend, PipelineConfig, TemperatureOptimizer, ThreeLoopPipeline


async def main() -> None:
    pipeline = ThreeLoopPipeline(
        DemoBackend(),
        optimizer=TemperatureOptimizer(seed=42),
        config=PipelineConfig(max_cycles=5),
    )
    result = await pipeline.run(
        "Prouver que la somme de deux nombres pairs est paire",
        kind="math",
    )
    print(result.final_solution)


asyncio.run(main())
```

Pour le rendu en temps réel, consommer `pipeline.stream(...)` au lieu de `run(...)`:

```python
async for event in pipeline.stream(task):
    print(event.event_type, event.message)
```

Pour réduire fortement la latence, activer le mode compact:

```python
pipeline = ThreeLoopPipeline(
    DemoBackend(),
    config=PipelineConfig(max_cycles=3, compact_debate=True),
)
```

Ce mode réalise un appel par cycle: le même contexte autoregressif produit les trois artefacts et les trois votes. Il s’agit d’un partage de contexte/KV-cache, pas d’un transfert arbitraire de vecteurs cachés entre rôles; ce dernier nécessiterait un modèle entraîné pour accepter ces états.

## Température bayésienne

`TemperatureOptimizer` maintient un prior Beta indépendant par identité. Avec les valeurs par défaut:

```text
T = 0.2 + Beta(alpha, beta) * (0.7 - 0.2)
```

Après un cycle, une récompense `R` est transformée en pseudo-observations:

```text
alpha <- alpha + learning_rate * R
beta  <- beta  + learning_rate * (1 - R)
```

La récompense par défaut combine le ratio de votes positifs, la validation syntaxique/structurelle et la vitesse d’obtention du consensus. Un `external_validator` ou un `reward_function` permet d’ajouter un signal métier.

## Recherche triangulée

Quand `research=True`, chaque rôle produit sa requête sans voir les requêtes des autres. `triangulate_sources` lance les recherches en parallèle et conserve les liens exacts vus par au moins deux agents; si les liens diffèrent, il conserve un représentant d’un domaine commun. Les erreurs d’un fournisseur sont isolées dans `WebResearchResult.errors`.

Un fournisseur externe respecte simplement le protocole:

```python
class MyProvider:
    async def search(self, query: str, *, max_results: int = 5):
        ...
```

## Structure

```text
three_loop/
  three_loop/
    agents.py       # trois identités et parseurs de votes
    backend.py      # backend partagé, Llama.cpp, LiteLLM, demo
    history.py      # transcript complet
    models.py       # contrats et événements
    pipeline.py     # boucle asynchrone et consensus
    streamlit_app.py
    temperature.py  # TemperatureOptimizer isolé
    validation.py
    web.py          # recherche et intersection robuste
  tests/
```
