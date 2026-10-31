"""存储层测试：用临时 SQLite 验证消息、决策和策略读写。"""

from __future__ import annotations

import asyncio
import time

from planner_social.models import MessageRecord
from planner_social.store import SocialStore


def test_store_roundtrip(tmp_path):
    async def scenario() -> None:
        store = SocialStore(tmp_path / "social.sqlite3", store_text=True, retention_days=1)
        await store.initialize()
        now = time.time()
        record = MessageRecord(
            message_id="m1",
            session_id="s1",
            group_id="g1",
            user_id="u1",
            user_name="A",
            text="你好",
            timestamp=now,
        )
        await store.record_message(record)
        recent = await store.get_recent("s1", 5)
        assert len(recent) == 1
        assert recent[0].text == "你好"

        await store.map_session_group("s1", "g1", "测试群")
        assert await store.get_session_group("s1") == ("g1", "测试群")

        await store.record_decision(
            decision_id="d1",
            session_id="s1",
            group_id="g1",
            action="reply",
            target_message_id="m1",
            utility=0.7,
            threshold=0.55,
            features=[1.0] * 10,
            evidence=["本条叫了机器人名字"],
        )
        open_decisions = await store.get_open_decisions("s1")
        assert len(open_decisions) == 1
        await store.attach_reply_text("s1", "机器人实际回复")
        decision = await store.get_decision("d1")
        assert decision is not None
        assert decision["reply_text"] == "机器人实际回复"
        assert await store.claim_decision("d1") is True
        await store.close_decision("d1", 1.0, label_source="llm", judge_reason="有人接话")
        assert await store.get_open_decisions("s1") == []

        await store.save_policy("g1", {"weights": [0.0] * 10, "positive": 1, "negative": 0, "updated_at": now})
        policy = await store.get_policy("g1")
        assert policy is not None
        assert policy["positive"] == 1

        await store.set_setting("talk_multiplier", 1.8)
        assert await store.get_setting("talk_multiplier", 1.0) == 1.8
        decisions = await store.list_decisions(10)
        assert len(decisions) == 1

        await store.reset_policy("g1")
        assert await store.get_policy("g1") is None
        store.close()

    asyncio.run(scenario())


def test_store_redacts_text_when_disabled(tmp_path):
    async def scenario() -> None:
        store = SocialStore(tmp_path / "social.sqlite3", store_text=False, retention_days=1)
        await store.initialize()
        await store.record_message(
            MessageRecord(
                message_id="m2",
                session_id="s2",
                group_id="g2",
                user_id="u2",
                user_name="B",
                text="敏感内容",
                timestamp=time.time(),
            )
        )
        recent = await store.get_recent("s2", 1)
        assert recent[0].text.startswith("<redacted:")
        assert "敏感内容" not in recent[0].text
        store.close()

    asyncio.run(scenario())
