"""在线学习策略。

使用一个很小的 logistic 回归作为“该不该开口”的排序器：
- 输入是 :mod:`planner_social.features` 里的固定特征；
- 输出是 0~1 的开口收益；
- 群聊里出现明确负反馈/正反馈时，用 SGD 更新该群的权重。

这不是 LLM 微调，也不是强化学习；它只学习一个低维、可解释的决策边界，
因此容易回滚、容易审阅，也不会把整段聊天内容塞进训练目标。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .features import FEATURE_NAMES, clamp, sigmoid

# 权重与 FEATURE_NAMES 一一对应。
# 设计意图：@/名字/引用是强正信号；两人对聊、刷屏、刚说完话是负信号。
DEFAULT_WEIGHTS: tuple[float, ...] = (
    -1.20,  # bias
    3.00,   # at_bot
    1.60,   # name_mention
    2.40,   # reply_to_bot
    0.60,   # direct_question
    0.40,   # last_bot_speaker
    -1.80,  # other_pair
    -1.00,  # burst
    -1.00,  # recent_bot
    -1.20,  # unaddressed
)


@dataclass
class LogisticPolicy:
    """一个群的在线 logistic 策略。"""

    weights: list[float] = field(default_factory=lambda: list(DEFAULT_WEIGHTS))
    positive: int = 0
    negative: int = 0
    observations: int = 0
    updated_at: float = 0.0
    prior_source: str = "default"

    def predict(self, features: list[float]) -> float:
        """返回开口收益（0~1）。"""

        if len(features) != len(self.weights):
            raise ValueError(f"特征维度不匹配: {len(features)} != {len(self.weights)}")
        return sigmoid(sum(weight * value for weight, value in zip(self.weights, features, strict=True)))

    def observe(self, reward: int) -> int:
        """记录一次标签，返回该群累计标签数；不改变权重。"""

        if reward not in (-1, 1):
            return self.observations
        self.observations += 1
        if reward > 0:
            self.positive += 1
        else:
            self.negative += 1
        return self.observations

    def learn_weights(
        self,
        features: list[float],
        reward: int,
        *,
        learning_rate: float,
        max_weight: float,
    ) -> None:
        """只更新权重，不重复计数；供冷启动阈值逻辑使用。"""

        if reward not in (-1, 1):
            return
        target = 1.0 if reward > 0 else 0.0
        prediction = self.predict(features)
        error = target - prediction
        for index, value in enumerate(features):
            updated = self.weights[index] + learning_rate * error * value
            self.weights[index] = clamp(updated, -max_weight, max_weight)

    def learn(self, features: list[float], reward: int, *, learning_rate: float, max_weight: float) -> None:
        """记录标签并做一步 SGD（兼容旧调用）。"""

        if reward not in (-1, 1):
            return
        self.observe(reward)
        self.learn_weights(features, reward, learning_rate=learning_rate, max_weight=max_weight)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 SQLite/API 可用的字典。"""

        return {
            "weights": list(self.weights),
            "feature_names": list(FEATURE_NAMES),
            "positive": self.positive,
            "negative": self.negative,
            "observations": self.observations,
            "updated_at": self.updated_at,
            "prior_source": self.prior_source,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> LogisticPolicy:
        """从持久化数据恢复；损坏/维度不符时回退默认值。"""

        if not isinstance(payload, dict):
            return cls()
        raw_weights = payload.get("weights")
        weights: list[float] = []
        if isinstance(raw_weights, list) and len(raw_weights) == len(DEFAULT_WEIGHTS):
            try:
                weights = [float(value) for value in raw_weights]
            except (TypeError, ValueError):
                weights = []
        return cls(
            weights=weights or list(DEFAULT_WEIGHTS),
            positive=int(payload.get("positive") or 0),
            negative=int(payload.get("negative") or 0),
            observations=int(payload.get("observations") or (int(payload.get("positive") or 0) + int(payload.get("negative") or 0))),
            updated_at=float(payload.get("updated_at") or 0.0),
            prior_source=str(payload.get("prior_source") or "default"),
        )
