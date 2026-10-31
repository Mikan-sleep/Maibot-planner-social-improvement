"""Store-level v1 features: global prior, reflections, frequency state, metrics."""

from __future__ import annotations

import asyncio
import time

from planner_social.models import MessageRecord
from planner_social.store import SocialStore


def test_v1_store_features(tmp_path):
    async def scenario() -> None:
        store = SocialStore(tmp_path / "social.sqlite3", store_text=True, retention_days=1)
        await store.initialize()
        now = time.time()
        await store.map_session_group("s1", "g1", "测试群")
        await store.record_message(
            MessageRecord(
                message_id="m1", session_id="s1", group_id="g1", user_id="u1",
                user_name="A", text="在吗", timestamp=now, is_at=True,
            )
        )
        await store.record_decision(
            decision_id="d1", session_id="s1", group_id="g1", action="reply",
            target_message_id="m1", utility=0.8, threshold=0.38,
            features=[1.0] * 10, evidence=["本条 @ 了机器人"], addressed=True,
        )
        await store.attach_reply_text("s1", "在")
        await store.record_message(
            MessageRecord(
                message_id="m2", session_id="s1", group_id="g1", user_id="bot",
                user_name="柚子", text="在", timestamp=now + 1, is_bot=True,
            )
        )
        metrics = await store.metrics("g1", window_hours=1)
        assert metrics["users"] == 1
        assert metrics["bot_messages"] == 1
        assert metrics["replies"] == 1
        assert metrics["addressed_response_rate"] == 1.0

        await store.save_policy("g1", {"weights": [1.0] * 10, "positive": 2, "negative": 0, "observations": 2})
        await store.save_policy("g2", {"weights": [3.0] * 10, "positive": 1, "negative": 0, "observations": 1})
        prior = await store.get_global_prior(min_groups=2)
        assert prior is not None
        assert prior["weights"][0] == 2.0
        assert prior["prior_source"] == "global"

        await store.add_reflection("g1", "群友不喜欢长篇大论", 8)
        assert await store.get_latest_reflection("g1") == "群友不喜欢长篇大论"

        await store.save_frequency_state("g1", 1.2, "回复率偏低")
        state = await store.get_frequency_state("g1")
        assert state["auto_multiplier"] == 1.2
        assert state["reason"] == "回复率偏低"
        store.close()

    asyncio.run(scenario())
