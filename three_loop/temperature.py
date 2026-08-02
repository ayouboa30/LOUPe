"""Bayesian temperature sampling and learning for the three agents."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Mapping

from .models import AGENT_ROLES, AgentRole, TemperatureObservation


@dataclass
class BetaPrior:
    """Unscaled Beta distribution parameters for one agent."""

    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        """Return the mean on the unit interval."""

        return self.alpha / (self.alpha + self.beta)


class TemperatureOptimizer:
    """Sample and update a bounded Beta prior per agent.

    The sampled value is ``T_min + Beta(alpha, beta) * (T_max - T_min)``.
    A reward in ``[0, 1]`` is interpreted as a fractional Bernoulli outcome:
    ``alpha += learning_rate * reward`` and
    ``beta += learning_rate * (1 - reward)``.  This is a lightweight Bayesian
    update that works naturally with Thompson Sampling and does not require
    NumPy or SciPy.

    The optimizer is deliberately independent from the LLM backend.  One
    instance can therefore be shared by all three role objects while the
    backend itself remains a single in-memory model.
    """

    def __init__(
        self,
        *,
        alpha: float = 2.0,
        beta: float = 2.0,
        t_min: float = 0.2,
        t_max: float = 0.7,
        learning_rate: float = 1.0,
        seed: int | None = None,
    ) -> None:
        """Create identical priors for the three agents.

        Args:
            alpha: Initial positive alpha parameter.
            beta: Initial positive beta parameter.
            t_min: Lower bound for a sampled temperature.
            t_max: Exclusive upper bound before floating-point clamping.
            learning_rate: Pseudo-count added by every reward observation.
            seed: Optional deterministic seed, useful for tests and demos.
        """

        if alpha <= 0 or beta <= 0:
            raise ValueError("alpha and beta must be strictly positive")
        if t_min < 0 or t_max <= t_min:
            raise ValueError("temperature bounds must satisfy 0 <= t_min < t_max")
        if learning_rate <= 0:
            raise ValueError("learning_rate must be strictly positive")

        self.alpha = float(alpha)
        self.beta = float(beta)
        self.t_min = float(t_min)
        self.t_max = float(t_max)
        self.learning_rate = float(learning_rate)
        self._rng = random.Random(seed)
        self._priors: dict[AgentRole, BetaPrior] = {
            role: BetaPrior(self.alpha, self.beta) for role in AGENT_ROLES
        }
        self._history: list[TemperatureObservation] = []

    @property
    def history(self) -> tuple[TemperatureObservation, ...]:
        """Return all posterior observations in chronological order."""

        return tuple(self._history)

    def sample(self, role: AgentRole) -> float:
        """Draw one Thompson sample for ``role`` in the configured range."""

        prior = self._get_prior(role)
        unit_sample = self._rng.betavariate(prior.alpha, prior.beta)
        return self.t_min + unit_sample * (self.t_max - self.t_min)

    def sample_temperature(self, role: AgentRole) -> float:
        """Explicit alias for :meth:`sample` used by application code."""

        return self.sample(role)

    def update(
        self,
        role: AgentRole,
        reward: float,
        *,
        cycle: int = 0,
        temperature: float | None = None,
    ) -> TemperatureObservation:
        """Apply a bounded reward and record the resulting posterior.

        ``reward`` is clipped rather than rejected so an external evaluator
        can safely return a slightly out-of-range floating-point value.
        """

        prior = self._get_prior(role)
        clipped_reward = min(1.0, max(0.0, float(reward)))
        prior.alpha += self.learning_rate * clipped_reward
        prior.beta += self.learning_rate * (1.0 - clipped_reward)
        sampled_temperature = (
            self._bounded_temperature(temperature)
            if temperature is not None
            else self.t_min + prior.mean * (self.t_max - self.t_min)
        )
        observation = TemperatureObservation(
            cycle=cycle,
            role=role,
            temperature=sampled_temperature,
            alpha=prior.alpha,
            beta=prior.beta,
            mean_temperature=self.mean_temperature(role),
            reward=clipped_reward,
        )
        self._history.append(observation)
        return observation

    def update_all(
        self,
        reward: float,
        temperatures: Mapping[AgentRole, float],
        *,
        cycle: int,
    ) -> tuple[TemperatureObservation, ...]:
        """Update all three posteriors with the cycle reward."""

        return tuple(
            self.update(
                role,
                reward,
                cycle=cycle,
                temperature=temperatures[role],
            )
            for role in AGENT_ROLES
        )

    def mean_temperature(self, role: AgentRole) -> float:
        """Return the scaled posterior mean for one agent."""

        prior = self._get_prior(role)
        return self.t_min + prior.mean * (self.t_max - self.t_min)

    def posterior(self, role: AgentRole) -> BetaPrior:
        """Return a copy of an agent's current Beta prior."""

        prior = self._get_prior(role)
        return BetaPrior(prior.alpha, prior.beta)

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Return posterior parameters in a UI- and JSON-friendly mapping."""

        return {
            role.value: {
                "alpha": self._get_prior(role).alpha,
                "beta": self._get_prior(role).beta,
                "mean_temperature": self.mean_temperature(role),
                "t_min": self.t_min,
                "t_max": self.t_max,
            }
            for role in AGENT_ROLES
        }

    def _get_prior(self, role: AgentRole) -> BetaPrior:
        """Resolve enum-like role input and fail early for unknown roles."""

        try:
            normalized = role if isinstance(role, AgentRole) else AgentRole(role)
            return self._priors[normalized]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown agent role: {role!r}") from exc

    def _bounded_temperature(self, temperature: float) -> float:
        """Keep externally supplied samples inside the configured interval."""

        return min(self.t_max, max(self.t_min, float(temperature)))
