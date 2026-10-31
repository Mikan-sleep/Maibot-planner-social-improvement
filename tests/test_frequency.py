"""Frequency controller unit tests."""

from __future__ import annotations

from planner_social.frequency import effective_multiplier, tune_multiplier


def test_tune_raises_below_target():
    result = tune_multiplier(
        1.0, users=100, bots=2, target_reply_rate=0.08, min_samples=20, step=0.1, low=0.5, high=2.5
    )
    assert result.changed is True
    assert result.multiplier == 1.1


def test_tune_lowers_above_target():
    result = tune_multiplier(
        1.0, users=100, bots=20, target_reply_rate=0.08, min_samples=20, step=0.1, low=0.5, high=2.5
    )
    assert result.changed is True
    assert result.multiplier == 0.9


def test_tune_waits_for_samples():
    result = tune_multiplier(
        1.0, users=5, bots=0, target_reply_rate=0.08, min_samples=20, step=0.1, low=0.5, high=2.5
    )
    assert result.changed is False
    assert result.multiplier == 1.0


def test_effective_multiplier_clamps():
    assert effective_multiplier(3.0, 2.0) == 3.0
    assert effective_multiplier(0.2, 0.5) == 0.2
