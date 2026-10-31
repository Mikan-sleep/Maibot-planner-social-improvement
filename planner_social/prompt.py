"""把社交状态渲染成 Planner 可读的上下文块。

这里只负责措辞，不做判断；判断结果来自 features/policy，便于测试和解释。
"""

from __future__ import annotations

from .models import SocialState

_HARD_RULES = """判定规则（优先于直觉）：
- “你”只有在本条 @ 机器人、叫机器人名字、引用了机器人的消息，或上一条就是机器人时才指机器人；回复其他人的消息默认不是在跟机器人说话。
- 如果最近是其他两人在快速接话，除非被点名，不要插话。
- 机器人刚说完且没人接话时，不要连续追问；先等下一轮。"""

_HARD_RULES_ACTIVE = """判定规则（优先于直觉）：
- “你”只有在本条 @ 机器人、叫机器人名字、引用了机器人的消息，或上一条就是机器人时才指机器人；回复其他人的消息默认不是在跟机器人说话。
- 如果最近是其他两人在快速接话，优先不打断；但你有短评或信息补充时可以插一句，不要长篇。
- 机器人刚说完且没人接话时，不要连续追问；先等下一轮。"""


def build_social_prompt(state: SocialState, participation_mode: str = "balanced") -> str:
    """构建注入 Planner 的社交状态块。

    Args:
        state: 本次 Planner 请求的社交状态。
        participation_mode: ``conservative`` / ``balanced`` / ``active``；
            active 会明确允许“没有被点名也可以短接一句”，用于提高发言频率。
    """

    signals = state.signals
    addressed = signals.addressed_score
    other = max(0.0, 1.0 - addressed)
    evidence = "；".join(signals.evidence) if signals.evidence else "没有明确指向机器人的信号"
    mode = participation_mode if participation_mode in {"conservative", "balanced", "active"} else "balanced"
    if state.utility >= state.threshold:
        advice = "可以回复，但只回给当前说话的人，不要接其他人之间的对聊。"
    elif mode == "active":
        advice = (
            "群聊不要求每句都被点名：如果这句话你有反应、或能补一句短评，可以接一句；"
            "只有明显是别人私聊，或你刚说完没人接时才等。"
        )
    elif mode == "conservative":
        advice = "没有明确指向你：先等，不要主动插话；除非被点名或能补充关键事实。"
    else:
        advice = (
            "没有明确指向你：通常继续观察；如果这句和你有关、或你能补一句有用的话，"
            "可以简短回复，不要为了刷存在感硬接。"
        )

    # active 模式下不要一边说“可以短接”，一边又把话轮写成“没轮到你”，否则模型会倾向 stop。
    if mode == "active" and state.utility < state.threshold:
        turn_text = "开放话轮（没被点名也可以短接一句；但别打断两人私聊）"
    else:
        turn_text = state.recommendation

    policy_note = ""
    if state.policy:
        positive = int(state.policy.get("positive") or 0)
        negative = int(state.policy.get("negative") or 0)
        policy_note = f"\n- 本群学习记录：正反馈 {positive} / 负反馈 {negative}；当前阈值 {state.threshold:.2f}"

    hard_rules = _HARD_RULES_ACTIVE if mode == "active" else _HARD_RULES
    norms_note = ""
    if state.norms:
        norms_note = "\n- 本群近期规范（LLM 反思）：\n" + "\n".join(f"  - {item}" for item in state.norms[:3])
    return f"""【社交状态 · Planner 参考】
- 群：{state.group_name or state.group_id or "未知群"}；当前说话人：{state.speaker_name or "未知"}
- 发言对象估计：机器人 {addressed:.2f} / 其他人 {other:.2f}（{evidence}）
- 话轮判断：{turn_text}
- 参与模式：{mode}
- 消息密度：最近窗口 {signals.burst_count} 条；机器人上次发言在 {int(min(signals.seconds_since_bot, 9999))} 秒前
- 策略分数（仅参考）：{state.utility:.2f}（阈值 {state.threshold:.2f}）{policy_note}{norms_note}
- 行为建议：{advice}

{hard_rules}
"""
