"""Closed-loop frequency tuning.

The core ``talk_value`` gate and the social policy gate are separate in MaiBot.
This controller keeps a per-group auto multiplier from recent reply-rate
telemetry, so operators can target a rate instead of guessing a multiplier.
It is opt-in; manual ``talk_multiplier`` remains the fallback.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrequencyTune:
    multiplier: float
    changed: bool
    reason: str


def tune_multiplier(
    current: float,
    *,
    users: int,
    bots: int,
    target_reply_rate: float,
    min_samples: int,
    step: float,
    low: float,
    high: float,
) -> FrequencyTune:
    """Return the next auto multiplier for one group.

    Args:
        current: Current auto multiplier.
        users: User messages in the measurement window.
        bots: Bot messages in the same window.
        target_reply_rate: Desired bot/user ratio (e.g. 0.08).
        min_samples: Minimum user messages before tuning.
        step: Multiplier step per tune.
        low: Lower clamp.
        high: Upper clamp.
    """

    current = max(low, min(high, float(current)))
    if users < max(1, min_samples):
        return FrequencyTune(current, False, "样本不足")
    rate = bots / max(users, 1)
    target = max(0.0, float(target_reply_rate))
    new_value = current
    if rate < target * 0.75:
        new_value = min(high, current + step)
        reason = f"回复率 {rate:.1%} 低于目标 {target:.1%}，提高倍率"
    elif rate > target * 1.25:
        new_value = max(low, current - step)
        reason = f"回复率 {rate:.1%} 高于目标 {target:.1%}，降低倍率"
    else:
        reason = f"回复率 {rate:.1%} 接近目标 {target:.1%}"
    return FrequencyTune(round(new_value, 3), abs(new_value - current) > 1e-9, reason)


def effective_multiplier(manual: float, auto: float, low: float = 0.2, high: float = 3.0) -> float:
    """Combine the manual knob with the auto controller, clamped for MaiBot."""

    return round(max(low, min(high, float(manual) * float(auto))), 3)
