"""特征层单元测试：不依赖 MaiBot 运行时，直接在插件目录运行。"""

from __future__ import annotations

import time

import pytest

from planner_social.features import (
    FEATURE_NAMES,
    build_feature_vector,
    build_signals,
    contains_negative_feedback,
    mentions_bot,
    parse_message_xml,
)
from planner_social.models import MessageRecord


def _msg(message_id: str, user: str, text: str, *, is_bot: bool = False, reply_to: str = "", ts: float | None = None) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        session_id="s1",
        group_id="g1",
        user_id=user,
        user_name=user,
        text=text,
        timestamp=ts if ts is not None else time.time(),
        is_bot=is_bot,
        reply_to=reply_to,
    )


def test_parse_message_xml():
    attrs, body = parse_message_xml('<message msg_id="1" time="12:00:00" user="A">\n你好')
    assert attrs["msg_id"] == "1"
    assert attrs["user"] == "A"
    assert body == "你好"


def test_mentions_bot_by_at_and_name():
    at_bot, name_mention = mentions_bot("柚子 你来一下", ["柚子"])
    assert at_bot is False
    assert name_mention is True

    at_bot, name_mention = mentions_bot("@柚子 你来一下", ["柚子"])
    assert at_bot is True
    assert name_mention is True


def test_reply_to_bot_and_other_pair():
    now = time.time()
    bot = _msg("b1", "bot", "我在", is_bot=True, ts=now - 5)
    other = _msg("u1", "B", "你觉得呢", ts=now - 2)
    current = _msg("u2", "C", "我觉得不行", reply_to="b1", ts=now)
    signals = build_signals(
        current,
        [bot, other, current],
        ["柚子"],
        reply_target=bot,
        now=now,
        burst_window_seconds=12,
    )
    assert signals.reply_to_bot is True
    assert signals.addressed_score >= 0.2
    assert "本条引用了机器人的消息" in signals.evidence

    other_target = _msg("u0", "A", "对", ts=now - 3)
    signals2 = build_signals(
        current,
        [other_target, other, current],
        ["柚子"],
        reply_target=other_target,
        now=now,
        burst_window_seconds=12,
    )
    assert signals2.other_pair is True


def test_feature_vector_is_fixed_order():
    now = time.time()
    current = _msg("u1", "A", "@柚子 在吗", ts=now)
    signals = build_signals(current, [current], ["柚子"], reply_target=None, now=now, burst_window_seconds=12)
    vector = build_feature_vector(signals, burst_threshold=3, recent_bot_seconds=30)
    assert len(vector) == len(FEATURE_NAMES)
    assert vector[1] == 1.0  # at_bot
    assert vector[9] == pytest.approx(0.2)  # unaddressed = 1 - addressed_score


def test_negative_keywords():
    assert contains_negative_feedback("你别插嘴，没问你", ["别插嘴", "没问你"])
    assert not contains_negative_feedback("继续聊", ["别插嘴"])
