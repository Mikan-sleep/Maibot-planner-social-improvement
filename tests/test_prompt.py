"""提示词模式测试：active 不能被写成“没轮到你”。"""

from __future__ import annotations

from planner_social.models import SocialSignals, SocialState
from planner_social.prompt import build_social_prompt


def _state() -> SocialState:
    return SocialState(
        session_id="s1",
        group_id="g1",
        group_name="测试群",
        speaker_name="A",
        signals=SocialSignals(speaker_name="A"),
        features=[1.0] + [0.0] * 9,
        utility=0.05,
        threshold=0.38,
        recommendation="没轮到你（建议等待）",
        policy={"positive": 0, "negative": 0},
        created_at=0.0,
    )


def test_active_mode_uses_open_turn_wording():
    text = build_social_prompt(_state(), "active")
    assert "参与模式：active" in text
    assert "开放话轮" in text
    assert "话轮判断：没轮到你" not in text


def test_conservative_mode_keeps_waiting():
    text = build_social_prompt(_state(), "conservative")
    assert "参与模式：conservative" in text
    assert "先等" in text
