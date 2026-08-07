"""Analyse la complexité d'un prompt et choisit automatiquement le modèle et les paramètres.

Au lieu de laisser l'utilisateur deviner quel backend et combien de cycles
utiliser, ce module examine le prompt et retourne une configuration adaptée :
- Simple (question courte, pas de code) → modèle local rapide, 1 cycle
- Moyen (question technique, un peu de code) → modèle local standard, 2 cycles
- Complexe (long prompt, beaucoup de code, maths avancées) → modèle cloud, 3-4 cycles

Ça évite de gaspiller des cycles sur une question triviale ou de sous-estimer
un problème complexe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Difficulty(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


@dataclass
class RoutingDecision:
    """Configuration recommandée pour un prompt donné."""

    difficulty: Difficulty
    cycles: int
    max_tokens: int
    backend_preference: str  # "local" ou "cloud"
    reasoning: str  # explication pour debug


def analyze_difficulty(prompt: str) -> RoutingDecision:
    """Analyse un prompt et retourne la configuration optimale.

    Critères :
    - Longueur : <100 chars = simple, 100-500 = moyen, >500 = complexe
    - Code : présence de ``` ou def/class = +1 complexité
    - Maths : présence de \[ ou $ = +1 complexité
    - Mots techniques : "optimize", "refactor", "prove" = +1 complexité
    """

    score = 0
    reasons = []

    # Longueur
    length = len(prompt)
    if length < 100:
        reasons.append(f"prompt court ({length} car)")
    elif length > 500:
        score += 1
        reasons.append(f"prompt long ({length} car)")

    # Code
    if "```" in prompt or re.search(r"\b(def|class|function|import)\b", prompt):
        score += 1
        reasons.append("contient du code")

    # Maths
    if "\\[" in prompt or "$" in prompt or re.search(r"\b(integral|derivative|proof)\b", prompt, re.I):
        score += 1
        reasons.append("contient des maths")

    # Mots techniques
    if re.search(r"\b(optimize|refactor|prove|implement|debug)\b", prompt, re.I):
        score += 1
        reasons.append("tâche technique complexe")

    # Décision
    if score == 0:
        return RoutingDecision(
            difficulty=Difficulty.SIMPLE,
            cycles=1,
            max_tokens=256,
            backend_preference="local",
            reasoning=f"Simple: {', '.join(reasons)}"
        )
    elif score <= 2:
        return RoutingDecision(
            difficulty=Difficulty.MEDIUM,
            cycles=2,
            max_tokens=512,
            backend_preference="local",
            reasoning=f"Moyen: {', '.join(reasons)}"
        )
    else:
        return RoutingDecision(
            difficulty=Difficulty.COMPLEX,
            cycles=3,
            max_tokens=1024,
            backend_preference="cloud",
            reasoning=f"Complexe: {', '.join(reasons)}"
        )
