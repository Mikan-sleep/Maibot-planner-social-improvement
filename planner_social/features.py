"""消息解析与社交信号计算。

这里是插件的“特征层”：只做确定性的文本/时间分析，便于单测和解释。
真正的在线学习在 :mod:`planner_social.policy`，感知层不依赖模型。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .addressee import resolve_addressee
from .models import MessageRecord, SocialSignals

# 特征顺序必须与 policy.DEFAULT_WEIGHTS 对齐；改动时同步更新文档和测试。
FEATURE_NAMES: tuple[str, ...] = (
    "bias",
    "at_bot",
    "name_mention",
    "reply_to_bot",
    "direct_question",
    "last_bot_speaker",
    "other_pair",
    "burst",
    "recent_bot",
    "unaddressed",
)

_MESSAGE_RE = re.compile(r"^<message(?P<attrs>[^>]*)>\s*(?P<body>.*)$", re.DOTALL)
_ATTR_RE = re.compile(r'(?P<key>[\w-]+)="(?P<value>[^"]*)"')
_SUMMARY_PREFIXES = ("【上下文状态摘要-内部参考】", "【表达习惯参考", "【长期记忆检索结果-内部参考】")
_QUESTION_MARKERS = ("?", "？", "吗", "呢", "怎么", "为什么", "是不是", "你觉得", "你说", "要不要", "能不能")
_REFERENCE_WORDS = ("回复", "引用", "reply", "quote")


def clamp(value: float, low: float, high: float) -> float:
    """把数值限制在闭区间内。"""

    return max(low, min(high, value))


def sigmoid(value: float) -> float:
    """数值稳定的 sigmoid。"""

    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def extract_item_text(item: dict[str, Any]) -> str:
    """从一个 Hook Context Item 快照中取出文本内容。"""

    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


def parse_message_xml(text: str) -> tuple[dict[str, str], str]:
    """解析 Planner 上下文里的 ``<message ...>正文`` 格式。

    Returns:
        (attrs, body)。不符合消息格式时返回空 attrs 和原文。
    """

    match = _MESSAGE_RE.match(text.strip())
    if not match:
        return {}, text.strip()
    attrs = {m.group("key"): m.group("value") for m in _ATTR_RE.finditer(match.group("attrs") or "")}
    return attrs, (match.group("body") or "").strip()


def is_context_material(text: str) -> bool:
    """判断是否是需要忽略的内部材料（摘要、记忆、表达习惯等）。"""

    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in _SUMMARY_PREFIXES)


def parse_timestamp(item: dict[str, Any], attrs: dict[str, str]) -> float:
    """优先用 Item meta 的 ISO 时间；退化到消息里的 HH:MM:SS。"""

    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    raw = meta.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    raw_time = attrs.get("time")
    if raw_time:
        try:
            today = datetime.now().date()
            parsed = datetime.strptime(raw_time, "%H:%M:%S").time()
            return datetime.combine(today, parsed).timestamp()
        except ValueError:
            pass
    return time.time()


def mentions_bot(text: str, bot_names: Iterable[str]) -> tuple[bool, bool]:
    """返回 (at_bot, name_mention)。

    ``@名字`` 记为 at_bot；仅出现名字记为 name_mention；两者可同时为真。
    """

    normalized = text.replace("＠", "@")
    at_bot = False
    name_mention = False
    for raw_name in bot_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        if re.search(rf"@{re.escape(name)}(?:\s|$|[，,。.!！?？])", normalized):
            at_bot = True
        if name in normalized:
            name_mention = True
    return at_bot, name_mention


def has_direct_question(text: str) -> bool:
    """粗略判断是否在提问；宁可漏判，避免把所有陈述都当提问。"""

    return any(marker in text for marker in _QUESTION_MARKERS)


def has_reference_word(text: str) -> bool:
    """消息里是否带“回复/引用”类文字，用于补充 reply_to 线索。"""

    lowered = text.lower()
    return any(word in lowered for word in _REFERENCE_WORDS)


def build_signals(
    current: MessageRecord,
    history: list[MessageRecord],
    bot_names: Iterable[str],
    *,
    reply_target: MessageRecord | None,
    now: float,
    burst_window_seconds: int,
) -> SocialSignals:
    """根据当前消息、历史和可能的引用目标计算社交信号。

    Args:
        current: 当前需要判断的用户消息。
        history: 同一会话最近的消息，按时间升序。
        bot_names: 机器人名字/别名。
        reply_target: 当前消息 reply_to 指向的消息；找不到时为 None。
        now: 当前时间戳。
        burst_window_seconds: 速率统计窗口。
    """

    previous = [msg for msg in history if msg.message_id != current.message_id]
    last_speaker = previous[-1] if previous else None
    addressee = resolve_addressee(
        current,
        bot_names=bot_names,
        reply_target=reply_target,
        previous=last_speaker,
    )
    at_bot = addressee.at_bot
    name_mention = addressee.name_mention
    reply_to_bot = addressee.reply_to_bot

    last_bot = next((msg for msg in reversed(previous) if msg.is_bot), None)
    last_bot_speaker = bool(last_speaker and last_speaker.is_bot)
    seconds_since_bot = now - last_bot.timestamp if last_bot else 9999.0

    # 两个人快速对聊：最近两条都不是 bot，且说话人交替或当前人在回复另一个人。
    other_pair = False
    if len(previous) >= 2 and not previous[-1].is_bot and not previous[-2].is_bot:
        other_pair = previous[-1].user_id != previous[-2].user_id
    if reply_target is not None and not reply_target.is_bot and reply_target.user_id != current.user_id:
        other_pair = True

    burst_count = sum(1 for msg in history if now - msg.timestamp <= burst_window_seconds and not msg.is_bot)
    evidence: list[str] = list(addressee.evidence)
    if last_bot_speaker:
        evidence.append("上一条是机器人")
    if other_pair:
        evidence.append("最近是其他两人在接话")
    if seconds_since_bot <= 30:
        evidence.append(f"机器人 {int(seconds_since_bot)} 秒前刚说过话")
    if burst_count >= 3:
        evidence.append(f"最近 {burst_window_seconds} 秒有 {burst_count} 条消息")

    return SocialSignals(
        at_bot=at_bot,
        name_mention=name_mention,
        reply_to_bot=reply_to_bot,
        direct_question=has_direct_question(current.text),
        last_bot_speaker=last_bot_speaker,
        other_pair=other_pair,
        burst_count=burst_count,
        seconds_since_bot=seconds_since_bot,
        speaker_name=current.user_name,
        reply_target_name=reply_target.user_name if reply_target else "",
        multi_at=addressee.multi_at,
        other_at_names=addressee.other_at_names,
        target_user_id=addressee.target_user_id,
        target_name=addressee.target_name,
        evidence=tuple(evidence),
    )


def build_feature_vector(signals: SocialSignals, *, burst_threshold: int, recent_bot_seconds: int) -> list[float]:
    """把社交信号转成固定顺序的数值特征。"""

    burst = clamp(signals.burst_count / max(1, burst_threshold), 0.0, 1.0)
    recent_bot = 1.0 if signals.seconds_since_bot <= recent_bot_seconds else 0.0
    return [
        1.0,  # bias
        1.0 if signals.at_bot else 0.0,
        1.0 if signals.name_mention else 0.0,
        1.0 if signals.reply_to_bot else 0.0,
        1.0 if signals.direct_question else 0.0,
        1.0 if signals.last_bot_speaker else 0.0,
        1.0 if signals.other_pair else 0.0,
        burst,
        recent_bot,
        1.0 - signals.addressed_score,
    ]


def contains_negative_feedback(text: str, keywords: Iterable[str]) -> bool:
    """是否出现明确的“别插嘴/没问你”类负反馈。"""

    return any(str(word).strip() and str(word) in text for word in keywords)
