"""Addressee resolver tests: multi-@, reply chains and bot aliases."""

from __future__ import annotations

import time

from planner_social.addressee import parse_at_names, resolve_addressee
from planner_social.models import MessageRecord


def _msg(message_id: str, user: str, text: str, *, is_bot: bool = False, user_id: str = "") -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        session_id="s1",
        group_id="g1",
        user_id=user_id or user,
        user_name=user,
        text=text,
        timestamp=time.time(),
        is_bot=is_bot,
    )


def test_parse_at_names_deduplicates_and_strips_punctuation():
    assert parse_at_names("@柚子 @Yuzi，@柚子 来看") == ("柚子", "Yuzi")


def test_multi_at_lowers_exclusive_score():
    current = _msg("u1", "A", "@柚子 @B 你们看")
    result = resolve_addressee(current, bot_names=["柚子"])
    assert result.at_bot is True
    assert result.multi_at is True
    assert result.other_at_names == ("B",)
    assert result.score < 0.55


def test_reply_to_bot_marks_bot_target():
    bot = _msg("b1", "柚子", "我在", is_bot=True)
    current = _msg("u1", "A", "那你来", user_id="u1")
    result = resolve_addressee(current, bot_names=["柚子"], reply_target=bot)
    assert result.reply_to_bot is True
    assert result.target_user_id == "柚子"


def test_name_alias_and_no_false_at():
    current = _msg("u1", "A", "柚子今天在吗")
    result = resolve_addressee(current, bot_names=["Yuzi", "柚子"])
    assert result.name_mention is True
    assert result.at_bot is False
