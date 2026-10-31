# Changelog

## 1.0.0 — 2026-11-01

首个独立项目版本。

- Addressee 解析：多 @、别名、引用链、回复对象。
- 冷启动与跨群先验：新群使用全局平均权重，积累足够反馈后才改权重。
- LLM 群规范反思：定期总结 1~3 条群规范并注入 Planner。
- 频率闭环：按目标回复率自动微调 talk_value 倍率，默认关闭。
- 强制改写保护：白名单、连续负反馈熔断、重置恢复。
- 运行指标：回复率、点名响应率、负反馈率、健康计数。
- 独立仓库工程化：pyproject、CI、release workflow、校验/打包脚本、市场元数据、LICENSE。
- 侧边栏自定义 HTML：所有参数 `?` 悬浮说明、指标/反思/健康/权重展示。

## 0.3.1 — 2026-11-01

- 侧边栏改为自定义 HTML 页面：每个可修改参数旁有 `?` 图标，悬停/键盘聚焦显示低门槛说明和调参影响。
- 增加刚发言保护/负反馈锁定的 UI 控件与说明；强制改写默认关闭并突出风险提示。
- 自定义页每 8 秒只刷新统计，不覆盖未保存的输入；支持无刷新保存/重置。

## 0.3.0 — 2026-11-01

- 反馈判定改为 LLM 裁判（`feedback_mode=llm` 默认）：positive/negative/neutral + confidence + reason；低置信度/失败按中性关闭。
- LLM 裁判结果按置信度加权更新每群 logistic 权重；保留 hybrid/keyword 回退。
- `after_response` 改为 BLOCKING：active 模式下可把 Planner 的 `stop/wait` 直接改写为 `reply`（`force_reply`，默认关闭；带冷却/负反馈锁定），实际文本由 MaiBot replyer 生成。
- 记录 `forced`、`reply_text`、`label_source`、`judge_reason`，WebUI 展示强制改写与裁判理由。
- 新增机器人容忍度 `bot_tolerance`：群友对机器人主动发言容忍度更高，不再严格套人类话轮规则。
- 新增 `llm.generate` 能力声明与后台裁判任务；卸载时取消未完成任务。
- WebUI 新增容忍度、强制 reply、强制最低分/冷却/负反馈锁定、feedback_mode 控件与权重量表。

## 0.2.0 — 2026-10-31

- 新增侧边栏声明式 WebUI（`webui.json`, schema v2）：参数调试、学习统计、最近决策表、策略重置。
- 新增运行时设置表：`enabled` / `reply_threshold` / `talk_multiplier` / `participation_mode` / `learning_rate`，保存到插件 SQLite，不修改 `config.toml`。
- 新增 `talk_multiplier`：通过 `frequency.set_adjust` 乘到 Maibot 的会话 `talk_value`，直接提高/降低发言频率。
- 新增 `participation_mode`：`conservative` / `balanced` / `active`，默认 `active`；active 允许未被点名时短接一句。
- 新增 `dashboard` / `update_settings` API。
- 默认值从“过度保守”调整为更活跃：`reply_threshold=0.38`、`talk_multiplier=1.8`；低置信度提示从“先 wait/stop”改为“可短回、不硬接”。
- 新增 `frequency.set_adjust` / `frequency.get_current_talk_value` / `frequency.get_adjust` 能力声明。
- 修复机器人上次发言在最近窗口外时被误报为 9999 秒。

## 0.1.0 — 2026-10-31

首个可用版本。

- 新增 `maisaka.planner.before_request` Hook：注入发言对象/话轮/节奏状态。
- 新增 `maisaka.planner.after_response` Hook：记录 reply/stop 决策。
- 新增 `send_service.after_send` Hook：记录机器人出站消息，支持 reply_to 判断。
- 新增 `chat.receive.after_process` Hook 与兼容 `ON_MESSAGE` 处理器：消息入库、session→group 映射、反馈收集。
- 新增每群独立的在线 logistic 策略与 SQLite 持久化。
- 新增 `stats` / `recent_decisions` / `label` / `reset` API。
- 新增设计、开发、路线图文档与单元测试。
