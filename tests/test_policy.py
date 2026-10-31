"""策略层单元测试：验证默认权重、学习方向和持久化回退。"""

from __future__ import annotations

from planner_social.policy import DEFAULT_WEIGHTS, LogisticPolicy


def _features(**overrides: float) -> list[float]:
    values = {
        "at_bot": 0.0,
        "name_mention": 0.0,
        "reply_to_bot": 0.0,
        "direct_question": 0.0,
        "last_bot_speaker": 0.0,
        "other_pair": 0.0,
        "burst": 0.0,
        "recent_bot": 0.0,
        "unaddressed": 1.0,
    }
    values.update(overrides)
    return [
        1.0,
        values["at_bot"],
        values["name_mention"],
        values["reply_to_bot"],
        values["direct_question"],
        values["last_bot_speaker"],
        values["other_pair"],
        values["burst"],
        values["recent_bot"],
        values["unaddressed"],
    ]


def test_explicit_at_bot_scores_high_and_silence_scores_low():
    policy = LogisticPolicy()
    assert policy.predict(_features(at_bot=1.0, unaddressed=0.0)) > 0.55
    assert policy.predict(_features()) < 0.35
    assert len(policy.weights) == len(DEFAULT_WEIGHTS)


def test_negative_feedback_raises_future_threshold_effect():
    policy = LogisticPolicy()
    before = policy.predict(_features(name_mention=1.0, unaddressed=0.0))
    policy.learn(_features(name_mention=1.0, unaddressed=0.0), -1, learning_rate=0.1, max_weight=6.0)
    after = policy.predict(_features(name_mention=1.0, unaddressed=0.0))
    assert policy.negative == 1
    assert after < before


def test_positive_feedback_lowers_bar():
    policy = LogisticPolicy()
    vector = _features(name_mention=1.0, unaddressed=0.0)
    before = policy.predict(vector)
    policy.learn(vector, 1, learning_rate=0.1, max_weight=6.0)
    assert policy.positive == 1
    assert policy.predict(vector) > before


def test_invalid_persisted_weights_fall_back_to_defaults():
    policy = LogisticPolicy.from_dict({"weights": [1, 2], "positive": 3, "negative": 4})
    assert len(policy.weights) == len(DEFAULT_WEIGHTS)
    assert policy.positive == 3
    assert policy.negative == 4
