# 设计文档：Planner 社交策略

## 1. 问题

MaiBot 的 Planner 是一个“提示词 + LLM + 工具”的决策器。它能看懂聊天内容，但缺少三类**显式状态**：

1. **发言对象**：当前这句话是对机器人、对另一个群友，还是对全场说的。
2. **话轮**：现在是不是机器人接话的位置，还是应该等对方说完。
3. **插话成本**：这句话现在说出去，是补充信息还是打断别人。

现有的 `expression` / `jargon` 学习只改变“怎么说”，没有改变“何时说、对谁说”。因此本插件把社交判断从 LLM 的自由发挥中拆出来，做成可解释、可学习的一层。

## 2. 研究依据

### 2.1 话轮转换（Conversation Analysis）

Sacks、Schegloff、Jefferson 的经典话轮研究指出：对话的话轮是局部管理的，存在“转换相关位置”（transition-relevance place, TRP）；参与者会在话可能说完的边界接话，并通过 gap / overlap 协调。

- 原文：Sacks, Schegloff & Jefferson, *A Simplest Systematics for the Organization of Turn-Taking for Conversation*, 1974。
- 跨语言证据：Stivers et al. 发现不同语言的话轮间隙分布高度一致，说明“等一个自然的边界”不是某个文化的习惯。

**对插件的含义**：不要仅凭“有新消息”就回复；先判断当前消息是否处于一个可接的边界，以及机器人是否被纳入这一轮。

### 2.2 发言对象是连续的

Mori et al. (SIGDIAL 2026) 把多人对话中的 addressee 从离散分类改成**连续等级**，发现它不只预测接话，也与注视、backchannel 相关；连续模型比离散标签拟合更好。

- https://aclanthology.org/2026.sigdial-1.47/

**对插件的含义**：不要只做 `at_bot: true/false`，而是给“机器人被 addressed 的程度”一个 0~1 的分数，并同时保留证据（@、名字、引用、上一条发言者）。

### 2.3 介入时机 = 收益 − 打扰成本

TMLR 2026 的 always-on assistant 综述把“何时主动介入”归纳为：

> 当且仅当行动的期望收益大于打断的期望成本时介入。

综述把问题拆成 signals、decision、action、memory、evaluation 五层，并指出开放域主动行为缺少统一、可验证的 reward。

- https://tmlr.infinite-conf.org/paper_pages/b0yKEdAXEr.html

**对插件的含义**：Planner 输出前应有一个“收益/成本”判断；群聊里的打扰成本至少包括：打断两人对聊、刷屏、刚说完又追问、把别人的“你”当成自己。

### 2.4 共同 ground 与心智理论

MindDial 让模型显式跟踪三层信念：说话者的信念、说话者推测的听者信念、两者差距；在共同 ground 对齐和协商中比直接生成更像人。

- https://aclanthology.org/2024.sigdial-1.63/

Clark & Brennan 的 grounding 理论强调：共同 ground 不是自动获得的，需要证据（acknowledgement、相关性、下一个话轮等）。

**对插件的含义**：不要把“对方知道”当作默认；对“这是不是在跟我说”“对方知不知道这个梗”保持不确定性，并用后续反馈更新。

### 2.5 Backchannel 与完整回复是两种行为

Qian & Skantze (ACL 2026) 研究 backchannel（“嗯”“mhm”“right”）的形式与语境关系，发现其形式高度依赖扩展上下文，且和完整回复的时间/形式不同。

- https://aclanthology.org/2026.acl-long.629/

**对插件的含义**：不是所有“接话”都要走完整 reply；低置信度时更适合短反应或等待，而不是生成一段完整回复。

## 3. 设计原则

1. **不改核心代码**：只通过 Hook / Event 观察和改写，插件可单独卸载。
2. **先可解释，再学习**：所有分数都能追溯到特征和证据；学习只调低维权重。
3. **可回退的深度干预**：`force_reply` 可把 stop/wait 改写为 reply，但默认关闭；
   开启后有冷却、刚发言保护和负反馈锁定，避免连续刷屏。
4. **按群独立**：不同群的节奏、容忍度不同，策略按 `group_id` 隔离。
5. **隐私最小化**：数据落在插件目录，可关闭文本存储、设置保留期。

## 4. 数据流

```
chat.receive.after_process Hook ──► 消息入库、session→group 映射、反馈评估
                     │
before_request ──► 解析 Context Items + 本地历史
                     │  build_signals → feature vector → policy.predict
                     ▼
              注入 SystemMessageItem（社交状态）
                     │
after_response ──► 记录决策；active 模式可把 stop/wait 改写为 reply
                     │
send_service.after_send ──► 记录机器人出站消息与实际回复文本
                     │
下一条用户消息 ──► LLM 裁判 positive/negative/neutral → 置信度加权 SGD
```

## 5. 特征

固定 10 维，顺序必须与 `features.FEATURE_NAMES` 和 `policy.DEFAULT_WEIGHTS` 一致：

| # | 特征 | 含义 | 默认权重 |
| --- | --- | --- | --- |
| 0 | bias | 基础倾向 | -1.20 |
| 1 | at_bot | 本条 @ 机器人 | +3.00 |
| 2 | name_mention | 本条叫机器人名字 | +1.60 |
| 3 | reply_to_bot | 本条引用机器人消息 | +2.40 |
| 4 | direct_question | 本条像在提问 | +0.60 |
| 5 | last_bot_speaker | 上一条是机器人 | +0.40 |
| 6 | other_pair | 最近是其他两人接话 | -1.80 |
| 7 | burst | 窗口消息密度（0~1） | -1.00 |
| 8 | recent_bot | 机器人刚说过话 | -1.00 |
| 9 | unaddressed | 未被 address 的程度 | -1.20 |

开口收益：

```
raw = sigmoid(w · x)
utility = clamp(raw + (bot_tolerance - 0.5) * 0.3, 0, 1)
recommendation =
  utility >= threshold          -> 轮到你（可以回复）
  utility >= threshold - 0.12  -> 可能轮到你（短反应或等待）
  else                          -> 没轮到你（建议等待）
```

`threshold` 是配置项，默认 0.38；`bot_tolerance` 默认 0.8，用 +0.09 左右的收益偏移表达
“群友对机器人主动发言的容忍度高于人类”。学习通过改变权重调整决策边界。

### 5.1 发言频率倍率

Planner 是否进入“思考”还受 MaiBot 的 `talk_value` 门控影响。插件通过
`frequency.set_adjust(chat_id, talk_multiplier)` 设置会话级调整值：

```
effective_talk = talk_multiplier * ChatConfigUtils.get_talk_value(chat_id)
```

默认 `talk_multiplier=1.8`，即把 0.35/0.50/0.60 的群配置提高到约
0.63/0.90/1.08。它和 `reply_threshold` 是两层独立的旋钮：

- `talk_multiplier`：让 Planner 更频繁地进入决策；
- `reply_threshold`：进入决策后，多高的社交收益才建议回复；
- 两者都在侧边栏 UI 中可调，保存到插件 SQLite，不修改 `config.toml`。

### 5.2 LLM 反馈裁判

`feedback_mode=llm` 时，回复后出现明确 @/负反馈，或攒够 `neutral_limit` 条消息，插件会调用
宿主 `llm.generate`，把“被回复消息 + 机器人实际回复 + 之后的群友消息 + 社交信号”交给裁判，
要求只返回：

```json
{"verdict": "positive|negative|neutral", "confidence": 0.0, "reason": "不超过80字"}
```

- `positive` / `negative` 且置信度 >= `judge_min_confidence`：按 ±1 更新该群策略，学习率乘以置信度；
- `neutral` 或低置信度：只关闭决策，不更新权重；
- LLM 调用失败：按中性关闭，避免把网络错误当成负反馈；
- `hybrid` 保留关键词的即时强信号，模糊情况交给 LLM；`keyword` 完全回退，省 token。

这解决了“只靠关键词”的两个问题：反讽/玩笑会被误判，以及“没人接但也没反感”被粗暴当成负反馈。

### 5.3 深度决策改写：stop → reply

`after_response` Hook 不只是观察。当：

- `participation_mode=active`
- `force_reply=true`（默认关闭）
- 本轮有真实目标消息
- `utility >= force_min_utility`
- 距离上次强制改写超过 `force_cooldown_seconds`，且机器人刚发言超过
  `force_after_bot_seconds`；只有直接 @/引用机器人时豁免
- 该会话没有被负反馈锁定（`force_lock_after_negative_seconds`）

插件会把 Planner 的 `stop/wait` FunctionCallItem 替换成 `reply` FunctionCallItem，并写入
`reply_reference` 与 `reply_style=简短表达`，交给 MaiBot 的 replyer 生成实际文本。

这是“更深入地修改 Planner 决策”，不是再加一段提示词；被改写的决策会记录 `forced=1`，
面板可查看实际回复和 LLM 裁判理由。关闭 `force_reply` 即可完全回退到只注入上下文。

## 6. 学习信号

| 事件 | reward | 是否更新权重 |
| --- | --- | --- |
| LLM 裁判 positive，置信度足够 | +1 | 是，学习率 × 置信度 |
| LLM 裁判 negative，置信度足够 | -1 | 是，学习率 × 置信度 |
| LLM 裁判 neutral / 低置信度 | 0 | 否，只关闭决策 |
| `hybrid` 下出现“别插嘴/没问你”等关键词 | -1 | 是（即时强信号） |
| `hybrid` 下 @/叫名字/引用机器人 | +1 | 是（即时强信号） |
| `keyword` 下连续 N 条中性消息 | -0.3 | 否 |
| 人工 `label` API | -1/0/1 | 非 0 时是 |

为什么 LLM 是默认：群聊里的“没接”可能只是话题结束、时间太晚、别人在忙；反讽和玩笑也
不能靠关键词识别。LLM 裁判能结合上下文给出理由和置信度，低置信度时不污染权重。

更新规则：

```
target = 1 if reward > 0 else 0
error = target - sigmoid(w·x)
w_i += lr * confidence * error * x_i
w_i = clamp(w_i, -max_weight, max_weight)
```

## 7. 注入的提示词

见 `planner_social/prompt.py`。它包含：

- 群名、当前说话人
- 发言对象估计 + 证据
- 话轮判断
- 消息密度、机器人上次发言时间
- 策略分数、阈值、本群正负反馈
- 行为建议
- 参与模式：`conservative` / `balanced` / `active`
- 三条硬规则（“你”的归属、对聊不插嘴、刚说完不追问）

`active` 会明确允许“没有被点名也可以短接一句”，并放宽“其他两人对聊不插话”的硬规则；
`conservative` 反过来，只在被点名或能补关键事实时回复。它是比阈值更直接的“发言倾向”旋钮，
和 `talk_multiplier`、`reply_threshold` 一起在侧边栏 UI 中可调。

硬规则是“默认推理”，不是绝对命令；真正的特殊场景仍由 Planner 判断。

## 8. 非目标

- 不做 LLM 微调或在线 RL：开放域社交没有稳定 reward，成本也过高；LLM 只做反馈裁判。
- 不做不可回退的拦截：`force_reply` 默认关闭，开启后也有冷却/负反馈锁定。
- 不做跨群共享：不同群的社交规范不同，默认按群隔离。
- 不替代 MaiBot 的 `talk_value`：本插件输出的是“该不该接这一句”，`talk_value` 控制“这个群整体多活跃”，两者叠加。

## 9. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 反馈噪声大 | LLM 裁判 + 置信度门槛；低置信度只关闭决策，不更新权重 |
| LLM 成本/延迟 | 只在攒够消息或明确信号时调用；后台任务；`keyword` 可完全回退 |
| 过拟合某个群 | 权重上限、学习率小、按群隔离、可 reset |
| 插件异常影响聊天 | 所有 Hook 捕获异常并跳过；`force_reply` 可一键关闭 |
| 隐私 | 插件目录独立 SQLite；可关闭文本存储、设置保留期；LLM 裁判只发必要片段 |
| 适配器无 reply_to | 退回 @/名字/上一条发言等信号；文档标注 |

## 10. v1.0 的新增层

### 10.1 Addressee resolver

`planner_social/addressee.py` 把“谁在对谁说”拆成可解释信号：

- 多个 `@` 中哪个是机器人；
- 同时 @ 了别人时降低“专指机器人”的分数；
- 引用链指向机器人；
- 昵称/别名匹配；
- 目标用户与目标消息。

它只做确定性解析，不调用 LLM；LLM 只用于反馈和反思，避免每轮都付推理成本。

### 10.2 冷启动与跨群先验

新群不直接使用别人的样本数：

- 有足够的历史群时，用它们的**平均权重**做先验；
- 先验只借权重，`positive/negative/observations` 从 0 开始；
- 本群积累 `cold_start_min_feedback` 条强反馈后才开始改权重。

### 10.3 LLM 反思

每 `reflection_every` 条强反馈，后台调用 LLM 从近期裁判理由中总结 1~3 条群规范，
存 `reflections` 表；下次 Planner 请求把最新规范注入上下文。它只使用裁判理由和短回复，
不重新发送整段聊天。

### 10.4 频率闭环

`planner_social/frequency.py` 根据近 2 小时的“机器人消息/用户消息”与
`target_reply_rate` 比较，按 `frequency_step` 调整每群 `auto_multiplier`：

```
effective = clamp(manual_multiplier * auto_multiplier, min, max)
```

默认关闭；开启后仍需手动倍率作为基准，避免控制器单方面把频率拉满。

### 10.5 强制改写的回滚

`force_reply` 之外增加：

- `force_whitelist`：只允许试点群；
- `force_auto_disable_after_negatives`：同一群连续负反馈达到阈值后自动关闭；
- `force_lock_after_negative_seconds`：负反馈后暂时锁定；
- UI 的“重置全部群策略”清除自动关闭状态。

这些是 0.3.0 实测禁言事故后的硬性保护。
