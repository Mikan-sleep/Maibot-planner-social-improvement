"""Planner 社交策略插件入口。

设计目标：
1. 在 Planner 请求前，把“谁在对谁说、是否轮到机器人、最近节奏”注入上下文；
2. 观察 Planner 的 reply/stop 决策和后续群聊反馈；
3. 用每个群独立的小型在线 logistic 策略学习“是否该开口”；
4. 不改 MaiBot 核心代码，所有状态落在插件自己的 SQLite 中。

心理/对话研究依据见 ``docs/design.md``。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from maibot_sdk import API, EventHandler, HookHandler, MaiBotPlugin
from maibot_sdk.types import EventType, HookMode, HookOrder

from .planner_social.config import PlannerSocialConfig
from .planner_social.features import (
    FEATURE_NAMES,
    build_feature_vector,
    build_signals,
    contains_negative_feedback,
    extract_item_text,
    is_context_material,
    parse_message_xml,
    parse_timestamp,
)
from .planner_social.frequency import effective_multiplier, tune_multiplier
from .planner_social.models import MessageRecord, SocialState
from .planner_social.policy import LogisticPolicy
from .planner_social.prompt import build_social_prompt
from .planner_social.store import SocialStore

_SOCIAL_ITEM_PREFIX = "planner-social-"


class PlannerSocialPlugin(MaiBotPlugin):
    """用社交状态和在线反馈改善 Planner 的开口时机。"""

    config_model = PlannerSocialConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: SocialStore | None = None
        self._policies: dict[str, LogisticPolicy] = {}
        self._pending_states: dict[str, SocialState] = {}
        self._seen_message_ids: set[str] = set()
        self._settings: dict[str, Any] = {}
        self._talk_adjust_applied: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._last_force_at: dict[str, float] = {}
        self._force_locked_until: dict[str, float] = {}
        self._force_blocked_groups: set[str] = set()
        self._force_negative_streak: dict[str, int] = {}
        self._reflection_inflight: set[str] = set()
        self._message_counter: dict[str, int] = {}
        self._health = {"hook_errors": 0, "llm_errors": 0, "llm_ok": 0, "last_error": ""}

    DEFAULT_SETTINGS = {
        "enabled": True,
        "reply_threshold": 0.38,
        "talk_multiplier": 1.8,
        "learning_rate": 0.05,
        "participation_mode": "active",
        "bot_tolerance": 0.8,
        "force_reply": False,
        "force_min_utility": 0.2,
        "force_cooldown_seconds": 60,
        "force_after_bot_seconds": 90,
        "force_lock_after_negative_seconds": 900,
        "force_whitelist": "",
        "force_auto_disable_after_negatives": 3,
        "auto_frequency": False,
        "target_reply_rate": 0.08,
        "frequency_tune_messages": 30,
        "frequency_step": 0.1,
        "frequency_min_multiplier": 0.5,
        "frequency_max_multiplier": 2.5,
        "feedback_mode": "llm",
    }

    # ------------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        """初始化本地存储和策略缓存。"""

        self._store = SocialStore(
            self.ctx.paths.data_dir / "planner_social.sqlite3",
            store_text=bool(self.config.privacy.store_message_text),
            retention_days=int(self.config.privacy.retention_days),
        )
        await self._store.initialize()
        await self._load_settings()
        blocked = await self._store.get_setting("force_blocked_groups", [])
        if isinstance(blocked, list):
            self._force_blocked_groups = {str(item) for item in blocked if item}
        self.ctx.logger.info("Planner 社交策略已加载，数据目录=%s", self.ctx.paths.data_dir)

    async def _load_settings(self) -> None:
        """从插件 DB 读取 WebUI 覆盖值；没有覆盖时用 config.toml 默认。"""

        assert self._store is not None
        defaults = {
            "enabled": bool(self.config.social.enabled),
            "reply_threshold": float(self.config.social.reply_threshold),
            "talk_multiplier": float(self.config.social.talk_multiplier),
            "learning_rate": float(self.config.social.learning_rate),
            "participation_mode": str(self.config.social.participation_mode),
            "bot_tolerance": float(self.config.social.bot_tolerance),
            "force_reply": bool(self.config.social.force_reply),
            "force_min_utility": float(self.config.social.force_min_utility),
            "force_cooldown_seconds": int(self.config.social.force_cooldown_seconds),
            "force_after_bot_seconds": int(self.config.social.force_after_bot_seconds),
            "force_lock_after_negative_seconds": int(self.config.social.force_lock_after_negative_seconds),
            "force_whitelist": ",".join(str(item) for item in self.config.social.force_whitelist),
            "force_auto_disable_after_negatives": int(self.config.social.force_auto_disable_after_negatives),
            "auto_frequency": bool(self.config.social.auto_frequency),
            "target_reply_rate": float(self.config.social.target_reply_rate),
            "frequency_tune_messages": int(self.config.social.frequency_tune_messages),
            "frequency_step": float(self.config.social.frequency_step),
            "frequency_min_multiplier": float(self.config.social.frequency_min_multiplier),
            "frequency_max_multiplier": float(self.config.social.frequency_max_multiplier),
            "feedback_mode": str(self.config.learning.feedback_mode),
        }
        self._settings = {
            key: await self._store.get_setting(key, default) for key, default in defaults.items()
        }
        if bool(self._settings.get("force_reply")) and not str(self._settings.get("force_whitelist") or "").strip():
            self._settings["force_reply"] = False
            await self._store.set_setting("force_reply", False)
            self.ctx.logger.warning(
                "检测到 force_reply=true 但没有白名单，已自动关闭；请先配置 force_whitelist 再开启"
            )

    def _setting(self, key: str) -> Any:
        return self._settings.get(key, self.DEFAULT_SETTINGS.get(key))

    def _record_health(self, key: str, error: BaseException | None = None) -> None:
        """记录轻量运行时健康计数；不把异常抛回消息链。"""

        self._health[key] = int(self._health.get(key, 0)) + 1
        if error is not None:
            self._health["last_error"] = f"{type(error).__name__}: {error}"[:300]

    async def on_unload(self) -> None:
        """关闭 SQLite 连接，取消未完成的 LLM 裁判任务。"""

        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        if self._store is not None:
            self._store.close()
            self._store = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热重载时清空策略缓存，下一次请求从 SQLite 重新读取。"""

        del scope, config_data, version
        self._policies.clear()
        self._pending_states.clear()
        if self._store is not None:
            await self._load_settings()
        self.ctx.logger.info("Planner 社交策略配置已热重载，策略缓存已清空")

    # ------------------------------------------------------------------ Hook：Planner 前
    @HookHandler(
        "maisaka.planner.before_request",
        name="inject_social_state",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2500,
    )
    async def inject_social_state(self, **kwargs: Any) -> dict[str, Any] | None:
        """在 Planner 请求中插入一段社交状态说明。"""

        if not bool(self._setting("enabled")) or self._store is None:
            return None
        items = kwargs.get("items")
        session_id = str(kwargs.get("session_id") or "")
        if not isinstance(items, list) or not session_id:
            return None
        try:
            state = await self._build_state(session_id, items)
            self._pending_states[session_id] = state
            block = build_social_prompt(state, str(self._setting("participation_mode")))
            new_items = self._insert_social_item(items, session_id, block)
            self.ctx.logger.debug(
                "注入社交状态 session=%s utility=%.2f addressed=%.2f evidence=%s",
                session_id,
                state.utility,
                state.signals.addressed_score,
                "；".join(state.signals.evidence),
            )
            return {"action": "continue", "modified_kwargs": {**kwargs, "items": new_items}}
        except Exception as exc:
            # 社交增强失败不能阻塞正常聊天；记录后让原请求继续。
            self._record_health("hook_errors", exc)
            self.ctx.logger.exception("构建 Planner 社交状态失败，已跳过本次注入")
            return None

    def _insert_social_item(self, items: list[dict[str, Any]], session_id: str, block: str) -> list[dict[str, Any]]:
        """替换旧社交 Item，并插入到最后一条用户消息之前。"""

        cleaned = [
            item
            for item in items
            if not (
                isinstance(item, dict)
                and str((item.get("meta") or {}).get("item_id") or "").startswith(_SOCIAL_ITEM_PREFIX)
            )
        ]
        social_item = {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"{_SOCIAL_ITEM_PREFIX}{session_id}",
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": block}],
        }
        # 放在当前用户消息之前，让最后一条消息仍然是用户的原始发言。
        insert_at = len(cleaned)
        for index in range(len(cleaned) - 1, -1, -1):
            if cleaned[index].get("item_type") == "UserMessageItem":
                insert_at = index
                break
        return [*cleaned[:insert_at], social_item, *cleaned[insert_at:]]

    async def _build_state(self, session_id: str, items: list[dict[str, Any]]) -> SocialState:
        """从当前 Item 列表和本地历史构造社交状态。"""

        assert self._store is not None
        messages = await self._collect_messages(session_id, items)
        last_bot = await self._store.get_last_bot(session_id)
        if last_bot is not None and not any(message.message_id == last_bot.message_id for message in messages):
            # 机器人上次发言可能已在最近窗口之外；补进来，避免 recent_bot 误判为 9999 秒。
            messages = sorted([*messages, last_bot], key=lambda message: message.timestamp)
        group_id, group_name = await self._store.get_session_group(session_id)
        if not group_id:
            group_id = session_id
        current = next((message for message in reversed(messages) if not message.is_bot), None)
        if current is None:
            current = messages[-1] if messages else MessageRecord(
                message_id="", session_id=session_id, group_id=group_id, user_id="", user_name="",
                text="", timestamp=time.time(),
            )

        reply_target = None
        if current.reply_to:
            reply_target = await self._store.get_message(current.reply_to)
        now = time.time()
        signals = build_signals(
            current,
            messages,
            list(self.config.social.bot_names),
            reply_target=reply_target,
            now=now,
            burst_window_seconds=int(self.config.social.burst_window_seconds),
        )
        features = build_feature_vector(
            signals,
            burst_threshold=int(self.config.social.burst_threshold),
            recent_bot_seconds=int(self.config.social.recent_bot_seconds),
        )
        policy = await self._get_policy(group_id)
        raw_utility = policy.predict(features)
        # 机器人不是人类：群友对机器人主动发言的容忍度更高，允许用容忍度抬高收益。
        tolerance = max(0.0, min(1.0, float(self._setting("bot_tolerance"))))
        utility = max(0.0, min(1.0, raw_utility + (tolerance - 0.5) * 0.3))
        threshold = float(self._setting("reply_threshold"))
        if utility >= threshold:
            recommendation = "轮到你（可以回复）"
        elif utility >= threshold - 0.12:
            recommendation = "可能轮到你（可短反应或继续等）"
        else:
            recommendation = "没轮到你（建议等待）"
        reflection = await self._store.get_latest_reflection(group_id)
        norms = tuple(line.strip(" -") for line in reflection.splitlines() if line.strip()) if reflection else ()
        return SocialState(
            session_id=session_id,
            group_id=group_id,
            group_name=group_name,
            speaker_name=signals.speaker_name or current.user_name,
            signals=signals,
            features=features,
            utility=utility,
            threshold=threshold,
            recommendation=recommendation,
            target_message_id=current.message_id,
            target_text=current.text,
            policy=policy.to_dict(),
            norms=norms,
            created_at=now,
        )

    async def _collect_messages(self, session_id: str, items: list[dict[str, Any]]) -> list[MessageRecord]:
        """合并 Planner 上下文中的消息和本地最近消息。"""

        assert self._store is not None
        parsed: list[MessageRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("item_type")
            if item_type not in {"UserMessageItem", "AssistantMessageItem"}:
                continue
            text = extract_item_text(item)
            if not text or is_context_material(text):
                continue
            attrs, body = parse_message_xml(text)
            if not body:
                continue
            is_bot_item = item_type == "AssistantMessageItem"
            if is_bot_item:
                # 上一条机器人回复通常是没有 <message> 包裹的 AssistantMessageItem。
                message_id = str((item.get("meta") or {}).get("item_id") or "")
                user_id = "bot"
                user_name = str(attrs.get("user") or "机器人")
            else:
                # 只接受真正的聊天消息；Planner 末尾的“请你进行分析……”是合成指令，不是发言。
                if not attrs or not attrs.get("user"):
                    continue
                if body.startswith("[事件-") or attrs.get("user") in {"系统通知", "系统消息"}:
                    continue
                message_id = str(attrs.get("msg_id") or (item.get("meta") or {}).get("item_id") or "")
                user_id = ""
                user_name = str(attrs.get("user") or attrs.get("group_card") or "")
            if not message_id:
                continue
            parsed.append(
                MessageRecord(
                    message_id=message_id,
                    session_id=session_id,
                    group_id="",
                    user_id=user_id,
                    user_name=user_name,
                    text=body,
                    timestamp=parse_timestamp(item, attrs),
                    is_bot=is_bot_item,
                )
            )

        stored = await self._store.get_recent(session_id, max(5, int(self.config.social.history_window)))
        merged: dict[str, MessageRecord] = {}
        for record in parsed:
            key = record.message_id or f"ts:{record.timestamp:.3f}"
            merged[key] = record
        # 本地事件记录更完整（含 reply_to / 真实 user_id），同 ID 时覆盖上下文解析结果。
        for record in stored:
            key = record.message_id or f"ts:{record.timestamp:.3f}"
            merged[key] = record
        ordered = sorted(merged.values(), key=lambda message: message.timestamp)
        return ordered[-max(5, int(self.config.social.history_window)) :]

    # ------------------------------------------------------------------ Hook：Planner 后
    @HookHandler(
        "maisaka.planner.after_response",
        name="record_planner_decision",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=6000,
    )
    async def record_planner_decision(self, **kwargs: Any) -> dict[str, Any] | None:
        """记录 Planner 动作；active 模式下可把 stop/wait 改写为 reply。"""

        if self._store is None:
            return None
        session_id = str(kwargs.get("session_id") or "")
        state = self._pending_states.pop(session_id, None)
        if not session_id or state is None:
            return None
        output_items = kwargs.get("output_items")
        action, target_message_id = self._extract_action(output_items)
        if not action:
            return None
        forced = False
        new_output_items: list[dict[str, Any]] | None = None
        if action in {"stop", "wait"} and self._should_force_reply(session_id, state):
            new_output_items = self._force_reply_items(output_items, state)
            if new_output_items is not None:
                action = "reply"
                target_message_id = state.target_message_id
                forced = True
        decision_id = uuid4().hex
        try:
            await self._store.record_decision(
                decision_id=decision_id,
                session_id=session_id,
                group_id=state.group_id,
                action=action,
                target_message_id=target_message_id,
                utility=state.utility,
                threshold=state.threshold,
                features=state.features,
                evidence=list(state.signals.evidence),
                status="open" if action == "reply" else "closed",
                forced=forced,
                addressed=bool(state.signals.addressed_score >= 0.55),
            )
            self.ctx.logger.info(
                "Planner 决策 action=%s%s utility=%.2f addressed=%.2f session=%s",
                action,
                "（插件改写）" if forced else "",
                state.utility,
                state.signals.addressed_score,
                session_id,
            )
        except Exception as exc:
            self._record_health("hook_errors", exc)
            self.ctx.logger.exception("记录 Planner 决策失败")
        if new_output_items is not None:
            return {"action": "continue", "modified_kwargs": {**kwargs, "output_items": new_output_items}}
        return None

    def _should_force_reply(self, session_id: str, state: SocialState) -> bool:
        """决定是否把 Planner 的 stop/wait 改写成 reply。"""

        if not bool(self._setting("force_reply")):
            return False
        if str(self._setting("participation_mode")) != "active":
            return False
        if not state.target_message_id or not state.target_text.strip():
            return False
        if state.group_id in self._force_blocked_groups or session_id in self._force_blocked_groups:
            return False
        whitelist = {
            item.strip() for item in str(self._setting("force_whitelist") or "").split(",") if item.strip()
        }
        # 安全默认：force_reply=true 但没配白名单时，不执行任何强制改写。
        # 若要全群启用，显式填写 "*"，不要依赖空字符串。
        if not whitelist:
            return False
        if "*" not in whitelist and state.group_id not in whitelist and session_id not in whitelist:
            return False
        now = time.time()
        if now < self._force_locked_until.get(session_id, 0.0):
            return False
        if state.utility < float(self._setting("force_min_utility")):
            return False
        # 只把“直接 @ / 引用机器人”当作可豁免的强信号；叫名字可能只是转述。
        direct = state.signals.at_bot or state.signals.reply_to_bot
        after_bot = max(0, int(self._setting("force_after_bot_seconds")))
        if not direct and after_bot and state.signals.seconds_since_bot < after_bot:
            return False
        cooldown = max(0, int(self._setting("force_cooldown_seconds")))
        last_forced = self._last_force_at.get(session_id, 0.0)
        return direct or not cooldown or now - last_forced >= cooldown

    def _force_reply_items(self, output_items: Any, state: SocialState) -> list[dict[str, Any]] | None:
        """把输出中的 stop/wait 工具调用替换为 reply；保留 reasoning 等非工具 Item。"""

        if not isinstance(output_items, list):
            return None
        kept: list[dict[str, Any]] = []
        stop_item: dict[str, Any] | None = None
        for item in output_items:
            if not isinstance(item, dict):
                continue
            if item.get("item_type") == "FunctionCallItem":
                tool_call = item.get("tool_call") or {}
                name = str(tool_call.get("func_name") or "")
                if name in {"stop", "wait"}:
                    stop_item = stop_item or item
                    continue
            kept.append(item)
        if stop_item is None:
            return None
        meta = stop_item.get("meta") if isinstance(stop_item.get("meta"), dict) else {}
        seed = (
            f"群聊里可以主动接一句。当前消息（{state.speaker_name or '群友'}）："
            f"{state.target_text[:300]}。用角色口吻简短回复，不解释规则，不重复刚说过的话；"
            "群友对机器人主动发言的容忍度比人类高，不必等被点名。"
        )
        reply_item = {
            "item_type": "FunctionCallItem",
            "meta": {
                "item_id": uuid4().hex,
                "logical_turn_id": meta.get("logical_turn_id"),
                "timestamp": datetime.now().isoformat(),
            },
            "tool_call": {
                "call_id": f"call_plugin_force_{uuid4().hex[:16]}",
                "func_name": "reply",
                "args": {
                    "msg_id": state.target_message_id,
                    "reply_reference": seed,
                    "reply_style": "简短表达",
                },
                "extra_content": {"tool_call_source": "response"},
            },
        }
        self._last_force_at[state.session_id] = time.time()
        kept.append(reply_item)
        return kept

    @staticmethod
    def _extract_action(output_items: Any) -> tuple[str, str]:
        if not isinstance(output_items, list):
            return "", ""
        first_action = ("", "")
        stop_action = ("", "")
        for item in output_items:
            if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
                continue
            tool_call = item.get("tool_call") or {}
            name = str(tool_call.get("func_name") or "").strip()
            if not name:
                continue
            args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
            action = (name, str(args.get("msg_id") or ""))
            if name == "reply":
                return action
            if name in {"stop", "wait"} and not stop_action[0]:
                stop_action = action
            if not first_action[0]:
                first_action = action
        return stop_action if stop_action[0] else first_action

    # ------------------------------------------------------------------ Hook：出站消息
    @HookHandler(
        "send_service.after_send",
        name="observe_bot_send",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=1500,
    )
    async def observe_bot_send(self, **kwargs: Any) -> None:
        """记录机器人自己的出站消息，供后续 reply_to 判断。"""

        if self._store is None or not kwargs.get("sent"):
            return
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "")
        message_id = str(message.get("message_id") or "")
        if not session_id or not message_id:
            return
        text = str(message.get("processed_plain_text") or "")
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        user = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        record = MessageRecord(
            message_id=message_id,
            session_id=session_id,
            group_id="",
            user_id=str(user.get("user_id") or "bot"),
            user_name=str(user.get("user_nickname") or "机器人"),
            text=text,
            timestamp=time.time(),
            is_bot=True,
        )
        await self._store.record_message(record)
        if text:
            await self._store.attach_reply_text(session_id, text)

    # ------------------------------------------------------------------ Hook：入站消息
    @HookHandler(
        "chat.receive.after_process",
        name="record_incoming_message",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=1500,
    )
    async def record_incoming_message(self, **kwargs: Any) -> None:
        """MaiBot 当前版本通过 Hook 分发入站消息；ON_MESSAGE 事件可能被注释掉。"""

        await self.observe_incoming_message(message=kwargs.get("message"))

    # ------------------------------------------------------------------ Event：入站消息（兼容保留）
    @EventHandler("observe_incoming_message", description="记录入站消息并收集 Planner 反馈", event_type=EventType.ON_MESSAGE)
    async def observe_incoming_message(self, message: Any = None, stream_id: str = "", **kwargs: Any) -> tuple[bool, bool, Any, Any, Any]:
        """记录消息，并在消息中寻找对上一次回复的正/负反馈。"""

        del kwargs
        if self._store is None or not isinstance(message, dict):
            return True, True, None, None, None
        session_id = str(message.get("session_id") or stream_id or "")
        if not session_id:
            return True, True, None, None, None
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        user = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        group = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
        account_id = str(message.get("account_id") or "")
        user_id = str(user.get("user_id") or "")
        is_bot = bool(account_id and user_id == account_id)
        text = str(message.get("processed_plain_text") or message.get("plain_text") or "")
        reply_to = str(message.get("reply_to") or "")
        group_id = str(group.get("group_id") or "")
        group_name = str(group.get("group_name") or "")
        record = MessageRecord(
            message_id=str(message.get("message_id") or ""),
            session_id=session_id,
            group_id=group_id,
            user_id=user_id,
            user_name=str(user.get("user_cardname") or user.get("user_nickname") or user_id),
            text=text,
            timestamp=self._parse_event_timestamp(message.get("timestamp")),
            is_bot=is_bot,
            reply_to=reply_to,
            is_at=bool(message.get("is_at")),
            is_mentioned=bool(message.get("is_mentioned")),
        )
        dedupe_key = f"{session_id}:{record.message_id or record.timestamp}"
        if dedupe_key in self._seen_message_ids:
            return True, True, None, None, None
        self._seen_message_ids.add(dedupe_key)
        if len(self._seen_message_ids) > 2000:
            self._seen_message_ids.clear()
        try:
            if group_id:
                await self._store.map_session_group(session_id, group_id, group_name)
            await self._store.record_message(record)
            if not is_bot and text:
                await self._apply_talk_multiplier(session_id)
                await self._maybe_tune_frequency(session_id, group_id)
                await self._evaluate_feedback(session_id, record, group_id)
        except Exception:
            self.ctx.logger.exception("处理入站消息失败")
        return True, True, None, None, None

    async def _evaluate_feedback(self, session_id: str, record: MessageRecord, group_id: str) -> None:
        """用下一条消息判断上一次 reply 是否被接住/被反感。"""

        assert self._store is not None
        decisions = await self._store.get_open_decisions(session_id, limit=2)
        if not decisions:
            return
        reply_target_bot = False
        if record.reply_to:
            target = await self._store.get_message(record.reply_to)
            reply_target_bot = bool(target and target.is_bot)
        at_bot, name_mention = self._mentions(record.text)
        mode = str(self._setting("feedback_mode")).lower()
        for decision in decisions:
            decision_id = str(decision.get("decision_id") or "")
            if not decision_id:
                continue
            negative = contains_negative_feedback(record.text, self.config.social.negative_keywords)
            explicit = negative or at_bot or name_mention or reply_target_bot
            if mode != "llm" and explicit:
                # hybrid 保留关键词的即时强信号；llm 模式则交给裁判统一判断。
                reward = -1 if negative else 1
                await self._apply_reward(
                    decision_id,
                    decision,
                    reward,
                    label_source="keyword",
                    judge_reason="关键词/点名规则",
                )
                continue
            if mode in {"llm", "hybrid"}:
                # LLM 模式：明确信号立即裁判；否则等若干条中性消息再裁判。
                if mode == "llm" and explicit:
                    if await self._store.claim_decision(decision_id):
                        self._spawn(self._judge_decision(decision_id, decision, session_id))
                    continue
                neutral_limit = int(self.config.social.neutral_limit)
                if not await self._store.increment_neutral(decision_id, neutral_limit):
                    continue
                if await self._store.claim_decision(decision_id):
                    self._spawn(self._judge_decision(decision_id, decision, session_id))
                else:
                    await self._apply_reward(
                        decision_id,
                        decision,
                        0,
                        label_source="neutral",
                        judge_reason="裁判任务已在队列中",
                    )
                continue
            neutral_limit = int(self.config.social.neutral_limit)
            if not await self._store.increment_neutral(decision_id, neutral_limit):
                continue
            await self._apply_reward(
                decision_id,
                decision,
                -0.3,
                label_source="neutral",
                judge_reason="连续中性消息，未发现明确反馈",
            )

    def _spawn(self, coro: Any) -> None:
        """保持后台任务强引用，避免被 GC 提前回收。"""

        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _judge_decision(self, decision_id: str, decision: dict[str, Any], session_id: str) -> None:
        """调用 LLM 判断一次机器人发言是正反馈、负反馈还是中性。"""

        assert self._store is not None
        try:
            decision = await self._store.get_decision(decision_id) or decision
            if not str(decision.get("reply_text") or "").strip():
                await self._apply_reward(
                    decision_id,
                    decision,
                    0,
                    label_source="neutral",
                    judge_reason="未记录到实际回复文本，跳过 LLM 裁判",
                )
                return
            created_at = float(decision.get("created_at") or 0)
            messages = await self._store.get_messages_since(session_id, created_at, limit=8)
            user_messages = [item for item in messages if not item.is_bot and item.text.strip()]
            if not user_messages:
                await self._apply_reward(
                    decision_id,
                    decision,
                    0,
                    label_source="neutral",
                    judge_reason="回复后没有可判断的后续消息",
                )
                return
            target = await self._store.get_message(str(decision.get("target_message_id") or ""))
            prompt = self._build_judge_prompt(decision, target, user_messages)
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt,
                    model=str(self.config.learning.judge_model or ""),
                    temperature=float(self.config.learning.judge_temperature),
                    max_tokens=int(self.config.learning.judge_max_tokens),
                ),
                timeout=max(1, int(self.config.learning.judge_timeout_seconds)),
            )
            verdict, confidence, reason = self._parse_judge_result(result)
            if verdict is None or confidence < float(self.config.learning.judge_min_confidence):
                await self._apply_reward(
                    decision_id,
                    decision,
                    0,
                    label_source="llm",
                    judge_reason=reason or "LLM 判断置信度不足",
                )
                return
            reward = 1 if verdict == "positive" else -1 if verdict == "negative" else 0
            await self._apply_reward(
                decision_id,
                decision,
                reward,
                label_source="llm",
                judge_reason=reason,
                confidence=confidence,
            )
            self._health["llm_ok"] = int(self._health.get("llm_ok", 0)) + 1
            self.ctx.logger.info(
                "LLM 反馈裁判 decision=%s verdict=%s confidence=%.2f reason=%s",
                decision_id,
                verdict,
                confidence,
                reason,
            )
        except Exception as exc:
            self._record_health("llm_errors", exc)
            self.ctx.logger.exception("LLM 反馈裁判失败，按中性关闭")
            try:
                await self._apply_reward(
                    decision_id,
                    decision,
                    0,
                    label_source="neutral",
                    judge_reason="LLM 裁判异常",
                )
            except Exception:
                self.ctx.logger.exception("关闭 LLM 裁判决策失败")

    def _build_judge_prompt(
        self,
        decision: dict[str, Any],
        target: MessageRecord | None,
        user_messages: list[MessageRecord],
    ) -> str:
        """构造低成本的反馈裁判提示词，只要求 JSON。"""

        reply_text = str(decision.get("reply_text") or "").strip() or "（未记录到实际发送文本）"
        target_text = target.text[:300] if target else "（目标消息不可用）"
        try:
            evidence = json.loads(decision.get("evidence_json") or "[]")
        except (TypeError, ValueError):
            evidence = []
        forced_note = "（这次是插件按主动模式改写的 reply）" if decision.get("forced") else ""
        lines = [
            "你是群聊社交裁判。判断机器人刚才这次发言是否合适。",
            f"机器人名字：{', '.join(self.config.social.bot_names)}",
            f"被回复的消息：{target_text}",
            f"机器人实际发言：{reply_text[:500]}{forced_note}",
            f"机器人的社交信号：{'；'.join(str(item) for item in evidence) or '无'}",
            "之后群友的消息：",
        ]
        for message in user_messages[-6:]:
            lines.append(f"- {message.user_name}: {message.text[:300]}")
        lines.extend(
            [
                "",
                "判定标准：",
                "- positive：有人接机器人、回应它、话题因它推进，或这次主动发言自然融入。",
                "- negative：有人明确反感/制止/嘲讽它插嘴，或发言明显答非所问、打断重要对话。",
                "- neutral：没人接但也没有反感，或信息不足。",
                "注意：群友对机器人主动发言的容忍度高于人类，不要因为“没被点名”就判 negative。",
                '只输出 JSON：{"verdict":"positive|negative|neutral","confidence":0.0,"reason":"不超过80字"}',
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _parse_judge_result(result: Any) -> tuple[str | None, float, str]:
        """解析 LLM JSON；失败时返回 None 让调用方按中性处理。"""

        if not isinstance(result, dict) or not result.get("success", True):
            return None, 0.0, "LLM 调用失败"
        raw = result.get("response") or result.get("content") or result.get("text") or ""
        if not isinstance(raw, str):
            return None, 0.0, "LLM 返回为空"
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None, 0.0, "LLM 未返回 JSON"
        try:
            payload = json.loads(match.group(0))
        except (TypeError, ValueError):
            return None, 0.0, "LLM JSON 解析失败"
        verdict = str(payload.get("verdict") or "").lower()
        if verdict not in {"positive", "negative", "neutral"}:
            return None, 0.0, "LLM verdict 非法"
        try:
            confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(payload.get("reason") or "")[:200]
        return verdict, confidence, reason

    async def _reflect_group(self, group_id: str, sample_count: int) -> None:
        """让 LLM 从近期裁判理由中总结 1~3 条群规范，注入后续 Planner。"""

        if not group_id or group_id in self._reflection_inflight or self._store is None:
            return
        self._reflection_inflight.add(group_id)
        try:
            decisions = await self._store.list_decisions(limit=40, group_id=group_id)
            samples = [
                item for item in decisions
                if str(item.get("judge_reason") or "").strip()
            ][:14]
            if not samples:
                return
            lines = [
                "你是群聊社交教练。根据下面机器人发言的裁判记录，总结这个群 1~3 条可执行的社交规范。",
                "只总结可观察的行为，不要评价人格；不要编造记录里没有的事实。",
                "注意：群友对机器人主动发言的容忍度通常高于人类，不要把“未点名”一律当成错误。",
                "记录：",
            ]
            for item in samples:
                lines.append(
                    f"- 动作={item.get('action')} 反馈={item.get('reward')} "
                    f"理由={str(item.get('judge_reason') or '')[:180]} "
                    f"实际回复={str(item.get('reply_text') or '')[:120]}"
                )
            lines.append('只输出 JSON：{"norms":["规范1","规范2"]}')
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    "\n".join(lines),
                    model=str(self.config.learning.reflection_model or ""),
                    temperature=0.0,
                    max_tokens=int(self.config.learning.reflection_max_tokens),
                ),
                timeout=max(2, int(self.config.learning.judge_timeout_seconds) + 5),
            )
            raw = result.get("response") or result.get("content") or result.get("text") if isinstance(result, dict) else ""
            norms: list[str] = []
            if isinstance(raw, str):
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if match:
                    try:
                        payload = json.loads(match.group(0))
                        values = payload.get("norms")
                        if isinstance(values, list):
                            norms = [str(value).strip()[:200] for value in values if str(value).strip()][:3]
                    except (TypeError, ValueError):
                        norms = []
            if not norms:
                return
            await self._store.add_reflection(group_id, "\n".join(norms), sample_count)
            self.ctx.logger.info("群规范反思完成 group=%s norms=%s", group_id, norms)
        except Exception as exc:
            self._record_health("llm_errors", exc)
            self.ctx.logger.exception("群规范反思失败")
        finally:
            self._reflection_inflight.discard(group_id)

    @staticmethod
    def _parse_event_timestamp(raw: Any) -> float:
        """事件里的 timestamp 可能是 epoch 字符串，也可能是 ISO 字符串。"""

        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw or "").strip()
        if not text:
            return time.time()
        try:
            return float(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                return time.time()

    def _mentions(self, text: str) -> tuple[bool, bool]:
        from .planner_social.features import mentions_bot

        return mentions_bot(text, list(self.config.social.bot_names))

    async def _apply_reward(
        self,
        decision_id: str,
        decision: dict[str, Any],
        reward: float,
        *,
        label_source: str = "",
        judge_reason: str = "",
        confidence: float = 1.0,
    ) -> None:
        """关闭决策并按强/弱反馈更新策略。"""

        assert self._store is not None
        group_id = str(decision.get("group_id") or "")
        if reward == -1:
            lock_seconds = max(0, int(self._setting("force_lock_after_negative_seconds")))
            if lock_seconds:
                lock_key = str(decision.get("session_id") or group_id)
                if lock_key:
                    self._force_locked_until[lock_key] = time.time() + lock_seconds
                if group_id:
                    self._force_locked_until[group_id] = time.time() + lock_seconds
                auto_disable = max(0, int(self._setting("force_auto_disable_after_negatives")))
                if auto_disable:
                    streak_key = lock_key or group_id
                    self._force_negative_streak[streak_key] = self._force_negative_streak.get(streak_key, 0) + 1
                    if self._force_negative_streak[streak_key] >= auto_disable and group_id:
                        self._force_blocked_groups.add(group_id)
                        await self._store.set_setting(
                            "force_blocked_groups", sorted(self._force_blocked_groups)
                        )
                        self.ctx.logger.warning(
                            "连续负反馈 %d 次，自动关闭强制改写 group=%s；可在 UI 重置",
                            self._force_negative_streak[streak_key],
                            group_id,
                        )
                self.ctx.logger.warning(
                    "收到负反馈，强制改写锁定 %s 秒 session=%s",
                    lock_seconds,
                    lock_key,
                )
        await self._store.close_decision(
            decision_id,
            reward,
            label_source=label_source,
            judge_reason=judge_reason,
        )
        if reward not in (-1, 1):
            return
        try:
            features = json.loads(decision.get("features_json") or "[]")
            if not group_id or not isinstance(features, list):
                return
            policy = await self._get_policy(group_id)
            observations = policy.observe(int(reward))
            cold_start = max(0, int(self.config.learning.cold_start_min_feedback))
            if observations < cold_start:
                policy.updated_at = time.time()
                await self._store.save_policy(group_id, policy.to_dict())
                self.ctx.logger.info(
                    "策略冷启动 group=%s observations=%d/%d reward=%+d（暂不改权重）",
                    group_id,
                    observations,
                    cold_start,
                    int(reward),
                )
                return
            policy.learn_weights(
                [float(value) for value in features],
                int(reward),
                learning_rate=float(self._setting("learning_rate")) * max(0.1, min(1.0, confidence)),
                max_weight=float(self.config.social.max_weight),
            )
            policy.updated_at = time.time()
            await self._store.save_policy(group_id, policy.to_dict())
            self.ctx.logger.info(
                "策略更新 group=%s reward=%+.1f positive=%d negative=%d observations=%d",
                group_id,
                reward,
                policy.positive,
                policy.negative,
                policy.observations,
            )
            if bool(self.config.learning.reflection_enabled):
                every = max(1, int(self.config.learning.reflection_every))
                if policy.observations % every == 0:
                    self._spawn(self._reflect_group(group_id, policy.observations))
        except Exception:
            self.ctx.logger.exception("更新社交策略失败")

    # ------------------------------------------------------------------ 策略
    async def _get_policy(self, group_id: str) -> LogisticPolicy:
        assert self._store is not None
        if group_id in self._policies:
            return self._policies[group_id]
        payload = await self._store.get_policy(group_id)
        using_prior = False
        if payload is None and bool(self.config.learning.cross_group_prior):
            payload = await self._store.get_global_prior(int(self.config.learning.cross_group_min_groups))
            using_prior = payload is not None
        policy = LogisticPolicy.from_dict(payload)
        if using_prior:
            # 先验只借权重，不把其他群的样本数算成本群样本；新群仍走冷启动。
            policy.positive = 0
            policy.negative = 0
            policy.observations = 0
            policy.prior_source = "global"
        self._policies[group_id] = policy
        return policy

    async def _apply_talk_multiplier(self, chat_id: str) -> None:
        """把手动倍率和自动频率倍率合成后写入 MaiBot 会话级 talk 调整。"""

        if not chat_id or not self._store:
            return
        manual = float(self._setting("talk_multiplier"))
        if manual <= 0:
            return
        group_id, _ = await self._store.get_session_group(chat_id)
        auto = 1.0
        if bool(self._setting("auto_frequency")):
            state = await self._store.get_frequency_state(group_id or chat_id)
            auto = float(state.get("auto_multiplier") or 1.0)
        value = effective_multiplier(
            manual,
            auto,
            low=float(self._setting("frequency_min_multiplier")),
            high=float(self._setting("frequency_max_multiplier")),
        )
        if self._talk_adjust_applied.get(chat_id) == value:
            return
        try:
            result = await self.ctx.frequency.set_adjust(chat_id, value)
            if isinstance(result, dict) and not result.get("success", True):
                self.ctx.logger.warning("设置发言频率倍率失败: %s", result)
                return
            self._talk_adjust_applied[chat_id] = value
            self.ctx.logger.info("已应用发言频率倍率 chat=%s multiplier=%.2f", chat_id, value)
        except Exception:
            self.ctx.logger.exception("设置发言频率倍率异常")

    async def _maybe_tune_frequency(self, session_id: str, group_id: str) -> None:
        """按每 N 条用户消息的回复率，闭环微调该群自动倍率。"""

        if not self._store or not bool(self._setting("auto_frequency")):
            return
        every = max(5, int(self._setting("frequency_tune_messages")))
        count = self._message_counter.get(session_id, 0) + 1
        if count < every:
            self._message_counter[session_id] = count
            return
        self._message_counter[session_id] = 0
        try:
            metrics = await self._store.metrics(group_id, window_hours=2)
            state = await self._store.get_frequency_state(group_id or session_id)
            tune = tune_multiplier(
                float(state.get("auto_multiplier") or 1.0),
                users=int(metrics.get("users") or 0),
                bots=int(metrics.get("bot_messages") or 0),
                target_reply_rate=float(self._setting("target_reply_rate")),
                min_samples=max(5, every // 2),
                step=float(self._setting("frequency_step")),
                low=float(self._setting("frequency_min_multiplier")),
                high=float(self._setting("frequency_max_multiplier")),
            )
            await self._store.save_frequency_state(group_id or session_id, tune.multiplier, tune.reason)
            self.ctx.logger.info("自动频率 group=%s multiplier=%.2f changed=%s reason=%s", group_id, tune.multiplier, tune.changed, tune.reason)
            await self._apply_talk_multiplier(session_id)
        except Exception as exc:
            self._record_health("hook_errors", exc)
            self.ctx.logger.exception("自动频率调整失败")

    # ------------------------------------------------------------------ API
    async def _dashboard_payload(self, stream_id: str = "") -> dict[str, Any]:
        """汇总 WebUI 页面需要的统计数据。"""

        assert self._store is not None
        group_id = stream_id
        if stream_id:
            mapped, _ = await self._store.get_session_group(stream_id)
            group_id = mapped or stream_id
        decisions = await self._store.list_decisions(limit=200, group_id=group_id)
        policy = await self._get_policy(group_id) if group_id else LogisticPolicy()
        metrics = await self._store.metrics(group_id, window_hours=24)
        reflection = await self._store.get_latest_reflection(group_id) if group_id else ""
        frequency = await self._store.get_frequency_state(group_id) if group_id else {
            "auto_multiplier": 1.0, "tuned_at": 0.0, "reason": ""
        }
        manual_multiplier = float(self._setting("talk_multiplier"))
        effective = effective_multiplier(
            manual_multiplier,
            float(frequency.get("auto_multiplier") or 1.0),
            low=float(self._setting("frequency_min_multiplier")),
            high=float(self._setting("frequency_max_multiplier")),
        )
        cold_start_min = max(0, int(self.config.learning.cold_start_min_feedback))
        recent: list[dict[str, Any]] = []
        for item in decisions[:20]:
            try:
                evidence = json.loads(item.get("evidence_json") or "[]")
            except (TypeError, ValueError):
                evidence = []
            recent.append(
                {
                    "created_at": datetime.fromtimestamp(float(item.get("created_at") or 0)).strftime("%m-%d %H:%M:%S"),
                    "action": item.get("action") or "",
                    "utility": round(float(item.get("utility") or 0), 2),
                    "threshold": round(float(item.get("threshold") or 0), 2),
                    "status": item.get("status") or "",
                    "reward": item.get("reward"),
                    "source": item.get("label_source") or "",
                    "forced": bool(item.get("forced")),
                    "reason": item.get("judge_reason") or "",
                    "reply_text": str(item.get("reply_text") or "")[:120],
                    "evidence": "；".join(str(x) for x in evidence),
                }
            )
        return {
            "success": True,
            "group_id": group_id,
            "total": len(decisions),
            "replies": sum(1 for item in decisions if item.get("action") == "reply"),
            "stops": sum(1 for item in decisions if item.get("action") == "stop"),
            "forced": sum(1 for item in decisions if item.get("forced")),
            "llm_judged": sum(1 for item in decisions if item.get("label_source") == "llm"),
            "positive": sum(1 for item in decisions if item.get("reward") == 1),
            "negative": sum(1 for item in decisions if item.get("reward") == -1),
            "policy_positive": policy.positive,
            "policy_negative": policy.negative,
            "force_summary": (
                f"强制改写 {sum(1 for item in decisions if item.get('forced'))} 次；"
                f"LLM 裁判 {sum(1 for item in decisions if item.get('label_source') == 'llm')} 次"
            ),
            "policy_summary": (
                f"策略正反馈 {policy.positive} / 负反馈 {policy.negative}；"
                f"样本 {policy.observations}/{cold_start_min}（冷启动）；"
                f"先验 {policy.prior_source}；"
                f"阈值 {float(self._setting('reply_threshold')):.2f}；"
                f"发言倍率 {float(self._setting('talk_multiplier')):.2f}；"
                f"参与模式 {self._setting('participation_mode')}；"
                f"容忍度 {float(self._setting('bot_tolerance')):.2f}；"
                f"学习步长 {float(self._setting('learning_rate')):.3f}"
            ),
            "metrics": metrics,
            "reflection": reflection,
            "health": dict(self._health),
            "frequency": {
                "manual_multiplier": manual_multiplier,
                "auto_multiplier": float(frequency.get("auto_multiplier") or 1.0),
                "effective_multiplier": effective,
                "reason": str(frequency.get("reason") or ""),
                "tuned_at": float(frequency.get("tuned_at") or 0.0),
            },
            "cold_start": {
                "observations": policy.observations,
                "minimum": cold_start_min,
                "ready": policy.observations >= cold_start_min,
            },
            "settings": dict(self._settings),
            "policy_weights": [round(value, 2) for value in policy.weights],
            "feature_names": list(FEATURE_NAMES),
            "weights_table": [
                {"feature": name, "weight": round(weight, 2)}
                for name, weight in zip(FEATURE_NAMES, policy.weights, strict=True)
            ],
            "recent": recent,
            "groups": await self._store.list_sessions(),
        }

    @API("dashboard", description="读取社交策略调试面板数据", version="1")
    async def api_dashboard(self, stream_id: str = "") -> dict[str, Any]:
        """WebUI 页面查询入口。"""

        if self._store is None:
            return {"success": False, "error": "插件尚未加载"}
        return await self._dashboard_payload(stream_id)

    @API("update_settings", description="更新运行参数（阈值/频率倍率/学习率/开关）", version="1")
    async def api_update_settings(
        self,
        enabled: bool,
        reply_threshold: float,
        talk_multiplier: float,
        learning_rate: float,
        participation_mode: str,
        bot_tolerance: float,
        force_reply: bool,
        force_min_utility: float,
        force_cooldown_seconds: int,
        force_after_bot_seconds: int,
        force_lock_after_negative_seconds: int,
        force_whitelist: str,
        force_auto_disable_after_negatives: int,
        auto_frequency: bool,
        target_reply_rate: float,
        frequency_tune_messages: int,
        frequency_step: float,
        frequency_min_multiplier: float,
        frequency_max_multiplier: float,
        feedback_mode: str,
    ) -> dict[str, Any]:
        """把调试参数写入插件 DB，并立即应用到已知会话。"""

        if self._store is None:
            return {"success": False, "error": "插件尚未加载"}
        if not 0.0 <= reply_threshold <= 1.0:
            return {"success": False, "error": "reply_threshold 必须在 0~1"}
        if not 0.2 <= talk_multiplier <= 3.0:
            return {"success": False, "error": "talk_multiplier 必须在 0.2~3.0"}
        if not 0.001 <= learning_rate <= 0.5:
            return {"success": False, "error": "learning_rate 必须在 0.001~0.5"}
        if force_reply and not str(force_whitelist or "").strip():
            return {"success": False, "error": "开启 force_reply 前必须填写 force_whitelist（群号或 *）"}
        if participation_mode not in {"conservative", "balanced", "active"}:
            return {"success": False, "error": "participation_mode 必须是 conservative/balanced/active"}
        if not 0.0 <= bot_tolerance <= 1.0:
            return {"success": False, "error": "bot_tolerance 必须在 0~1"}
        if not 0.0 <= force_min_utility <= 1.0:
            return {"success": False, "error": "force_min_utility 必须在 0~1"}
        if not 0 <= force_cooldown_seconds <= 120:
            return {"success": False, "error": "force_cooldown_seconds 必须在 0~120"}
        if not 0 <= force_after_bot_seconds <= 600:
            return {"success": False, "error": "force_after_bot_seconds 必须在 0~600"}
        if not 0 <= force_lock_after_negative_seconds <= 3600:
            return {"success": False, "error": "force_lock_after_negative_seconds 必须在 0~3600"}
        if not 0 <= force_auto_disable_after_negatives <= 20:
            return {"success": False, "error": "force_auto_disable_after_negatives 必须在 0~20"}
        if feedback_mode not in {"llm", "hybrid", "keyword"}:
            return {"success": False, "error": "feedback_mode 必须是 llm/hybrid/keyword"}
        if not 0.0 <= target_reply_rate <= 1.0:
            return {"success": False, "error": "target_reply_rate 必须在 0~1"}
        if not 0.2 <= frequency_min_multiplier <= frequency_max_multiplier <= 3.0:
            return {"success": False, "error": "frequency 倍率范围必须在 0.2~3.0 且 min <= max"}
        if not 0.01 <= frequency_step <= 1.0:
            return {"success": False, "error": "frequency_step 必须在 0.01~1.0"}
        if not 5 <= frequency_tune_messages <= 500:
            return {"success": False, "error": "frequency_tune_messages 必须在 5~500"}
        values = {
            "enabled": bool(enabled),
            "reply_threshold": float(reply_threshold),
            "talk_multiplier": float(talk_multiplier),
            "learning_rate": float(learning_rate),
            "participation_mode": str(participation_mode),
            "bot_tolerance": float(bot_tolerance),
            "force_reply": bool(force_reply),
            "force_min_utility": float(force_min_utility),
            "force_cooldown_seconds": int(force_cooldown_seconds),
            "force_after_bot_seconds": int(force_after_bot_seconds),
            "force_lock_after_negative_seconds": int(force_lock_after_negative_seconds),
            "force_whitelist": str(force_whitelist or ""),
            "force_auto_disable_after_negatives": int(force_auto_disable_after_negatives),
            "auto_frequency": bool(auto_frequency),
            "target_reply_rate": float(target_reply_rate),
            "frequency_tune_messages": int(frequency_tune_messages),
            "frequency_step": float(frequency_step),
            "frequency_min_multiplier": float(frequency_min_multiplier),
            "frequency_max_multiplier": float(frequency_max_multiplier),
            "feedback_mode": str(feedback_mode),
        }
        for key, value in values.items():
            await self._store.set_setting(key, value)
        self._settings.update(values)
        self._talk_adjust_applied.clear()
        for session in await self._store.list_sessions():
            await self._apply_talk_multiplier(str(session.get("session_id") or ""))
        self.ctx.logger.info("WebUI 更新社交策略参数: %s", values)
        return await self._dashboard_payload()

    @API("stats", description="查看社交策略和各群的决策统计", version="1")
    async def api_stats(self, stream_id: str = "") -> dict[str, Any]:
        """返回策略摘要；传 stream_id 时只返回该会话。"""

        if self._store is None:
            return {"success": False, "error": "插件尚未加载"}
        group_id = stream_id
        if stream_id:
            mapped, _ = await self._store.get_session_group(stream_id)
            group_id = mapped or stream_id
        stats = await self._store.stats(group_id)
        policy = await self._get_policy(group_id)
        return {
            "success": True,
            "group_id": group_id,
            "policy": policy.to_dict(),
            "feature_names": list(FEATURE_NAMES),
            "decisions": stats.get("decisions") or {},
        }

    @API("recent_decisions", description="查看最近的 Planner 决策", version="1")
    async def api_recent_decisions(self, stream_id: str = "", limit: int = 10) -> dict[str, Any]:
        """返回最近的决策记录，便于调试和人工标注。"""

        if self._store is None:
            return {"success": False, "error": "插件尚未加载"}
        mapped, _ = await self._store.get_session_group(stream_id) if stream_id else ("", "")
        group_id = mapped or stream_id
        # 复用 SQLite 查询，避免把展示逻辑塞进 store。
        decisions = await self._store.get_open_decisions(stream_id, limit=max(1, min(50, int(limit))))
        return {"success": True, "group_id": group_id, "open_decisions": decisions}

    @API("label", description="人工给一条决策打正/负反馈", version="1")
    async def api_label(self, decision_id: str, reward: float) -> dict[str, Any]:
        """人工校准入口；reward 只接受 -1/0/1。"""

        if self._store is None or not decision_id:
            return {"success": False, "error": "缺少决策或插件未加载"}
        if reward not in (-1, 0, 1):
            return {"success": False, "error": "reward 只接受 -1/0/1"}
        decision = await self._store.get_decision(decision_id)
        if decision is None:
            return {"success": False, "error": "决策不存在"}
        await self._apply_reward(decision_id, decision, float(reward))
        return {"success": True, "decision_id": decision_id, "reward": reward}

    @API("reset", description="重置某个会话/群的社交策略", version="1")
    async def api_reset(self, stream_id: str = "") -> dict[str, Any]:
        """清空策略权重；消息和决策记录保留到保留期结束。"""

        if self._store is None:
            return {"success": False, "error": "插件尚未加载"}
        group_id = stream_id
        if stream_id:
            mapped, _ = await self._store.get_session_group(stream_id)
            group_id = mapped or stream_id
        await self._store.reset_policy(group_id)
        self._policies.pop(group_id, None)
        if group_id:
            self._force_blocked_groups.discard(group_id)
            self._force_negative_streak.pop(group_id, None)
        else:
            self._force_blocked_groups.clear()
            self._force_negative_streak.clear()
        await self._store.set_setting("force_blocked_groups", sorted(self._force_blocked_groups))
        return {"success": True, "group_id": group_id}


def create_plugin() -> PlannerSocialPlugin:
    """插件入口；由 MaiBot 插件加载器调用。"""

    return PlannerSocialPlugin()
