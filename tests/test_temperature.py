from three_loop import AgentRole, TemperatureOptimizer


def test_beta_sampling_is_scaled_and_update_is_recorded() -> None:
    optimizer = TemperatureOptimizer(seed=4, alpha=2, beta=2)

    temperature = optimizer.sample(AgentRole.HEURISTIC)
    assert 0.2 <= temperature <= 0.7

    before = optimizer.posterior(AgentRole.HEURISTIC)
    observation = optimizer.update(
        AgentRole.HEURISTIC,
        1.0,
        cycle=1,
        temperature=temperature,
    )
    after = optimizer.posterior(AgentRole.HEURISTIC)

    assert after.alpha == before.alpha + 1.0
    assert after.beta == before.beta
    assert observation.temperature == temperature
    assert observation.mean_temperature > 0.2
    assert len(optimizer.history) == 1


def test_all_roles_have_independent_posteriors() -> None:
    optimizer = TemperatureOptimizer(seed=1)
    temperatures = {
        role: optimizer.sample_temperature(role)
        for role in (AgentRole.HEURISTIC, AgentRole.CRITIC, AgentRole.WRITER)
    }
    optimizer.update_all(0.0, temperatures, cycle=2)

    assert len(optimizer.history) == 3
    for role in (AgentRole.HEURISTIC, AgentRole.CRITIC, AgentRole.WRITER):
        posterior = optimizer.posterior(role)
        assert posterior.alpha == 2.0
        assert posterior.beta == 3.0
