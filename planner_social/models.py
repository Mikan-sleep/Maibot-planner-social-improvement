"""插件内部数据结构。

这些结构只依赖标准库，方便在单元测试中直接构造，不依赖 MaiBot 运行时。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MessageRecord:
    """一条参与社交判断的消息。"""

    message_id: str
    session_id: str
    group_id: str
    user_id: str
    user_name: str
    text: str
    timestamp: float
    is_bot: bool = False
    reply_to: str = ""
    is_at: bool = False
    is_mentioned: bool = False


@dataclass(frozen=True)
class SocialSignals:
    """从消息序列中抽取的、可解释的社交信号。"""

    at_bot: bool = False
    name_mention: bool = False
    reply_to_bot: bool = False
    direct_question: bool = False
    last_bot_speaker: bool = False
    other_pair: bool = False
    burst_count: int = 0
    seconds_since_bot: float = 9999.0
    speaker_name: str = ""
    reply_target_name: str = ""
    multi_at: bool = False
    other_at_names: tuple[str, ...] = ()
    target_user_id: str = ""
    target_name: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def addressed_score(self) -> float:
        """给 WebUI/日志展示的 0~1 发言对象分数，不等于策略分数。"""

        score = 0.0
        if self.at_bot:
            score += 0.55
        if self.name_mention:
            score += 0.25
        if self.reply_to_bot:
            score += 0.20
        return min(1.0, score)


@dataclass
class SocialState:
    """一次 Planner 请求对应的完整社交状态。"""

    session_id: str
    group_id: str
    group_name: str
    speaker_name: str
    signals: SocialSignals
    features: list[float]
    utility: float
    threshold: float
    recommendation: str
    target_message_id: str = ""
    target_text: str = ""
    policy: dict[str, Any] = field(default_factory=dict)
    norms: tuple[str, ...] = ()
    created_at: float = 0.0


@dataclass(frozen=True)
class PlannerDecision:
    """Planner 的一次决策记录，用于后续反馈学习。"""

    decision_id: str
    session_id: str
    group_id: str
    action: str
    target_message_id: str
    utility: float
    threshold: float
    features: tuple[float, ...]
    evidence: tuple[str, ...]
    created_at: float
    status: str = "open"
