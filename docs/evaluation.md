# 评估方法

## 能回答什么

- 同一段决策历史里，不同阈值会改写多少 `stop`；
- 实际回复率、强制改写率、负反馈率；
- 某个群在 24 小时窗口内的用户消息/机器人消息比例。

## 不能回答什么

- 不能证明“插件让聊天更好”：没有真实对照，也没有人类评分；
- 不能用离线 replay 代替线上 A/B；
- 不能把 LLM 裁判的 `neutral` 当成“没有损失”。

## 离线 replay

```bash
python experiments/replay_policy.py \
  --db data/plugins/maibot-team.planner-social/planner_social.sqlite3 \
  --threshold 0.38
```

输出：

```json
{
  "sample": 120,
  "actual": {"replies": 18, "negative_rate": 0.05},
  "counterfactual": {"would_reply": 31, "would_force": 13},
  "caveat": "Offline counterfactual..."
}
```

## 线上灰度建议

1. 选一个低风险群，先 `force_reply=false` 观察 24 小时；
2. 记录 `reply_rate`、`addressed_response_rate`、`negative_rate`；
3. 只改一个参数（阈值或倍率），不要同时改三个；
4. 若负反馈率上升，先切 `balanced`，再降 `talk_multiplier`；
5. 任何 `force_reply=true` 的群必须配置白名单和负反馈熔断。

## 最小指标体系

| 指标 | 来源 | 用途 |
| --- | --- | --- |
| `reply_rate` | 插件 messages | 发言频率 |
| `addressed_response_rate` | decisions.addressed + messages.is_at | 被点名时是否接住 |
| `negative_rate` | decisions.reward=-1 / replies | 插嘴/反感 |
| `forced` | decisions.forced | 深度干预频率 |
| cold-start observations | policies.observations | 新群是否已开始学习 |
