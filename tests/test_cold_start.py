"""Cold start and prior policy tests."""

from __future__ import annotations

from planner_social.policy import LogisticPolicy


def _features() -> list[float]:
    return [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_observe_does_not_change_weights():
    policy = LogisticPolicy()
    before = list(policy.weights)
    policy.observe(1)
    assert policy.observations == 1
    assert policy.positive == 1
    assert policy.weights == before


def test_learn_weights_changes_weights_without_double_counting():
    policy = LogisticPolicy()
    policy.observe(1)
    before = list(policy.weights)
    policy.learn_weights(_features(), 1, learning_rate=0.1, max_weight=6.0)
    assert policy.observations == 1
    assert policy.weights != before


def test_global_prior_restores_source():
    policy = LogisticPolicy.from_dict({"weights": [0.0] * 10, "positive": 3, "negative": 1, "prior_source": "global"})
    assert policy.prior_source == "global"
    assert policy.observations == 4
