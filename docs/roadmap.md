# 路线图

## v0.1 — 已归档

- Planner 前注入社交状态
- reply/stop 决策记录
- 每群 logistic 策略
- SQLite、隐私开关、基础文档

## v0.2 — 已归档

- 侧边栏 UI
- `talk_multiplier` / `participation_mode` / `bot_tolerance`
- 运行时设置表
- 强制 reply 开关

## v0.3 — 已归档

- LLM 反馈裁判（positive/negative/neutral + confidence + reason）
- 置信度加权学习
- `after_response` stop→reply 深度改写
- 安全冷却/负反馈锁定

## v1.0 — 当前（2026-11-01）

- [x] Addressee resolver：多 @、别名、引用链
- [x] 冷启动 + 跨群先验
- [x] LLM 群规范反思 + 注入
- [x] 回复率闭环（默认关闭）
- [x] 强制改写白名单/连续负反馈熔断
- [x] 指标：回复率/点名响应/负反馈/健康
- [x] 自定义 HTML UI，所有参数 `?` 说明
- [x] 独立仓库：pyproject、CI、Release、校验/打包脚本
- [x] 离线 replay 实验

## v1.1 — 下一步

- [ ] 截图与市场详情页素材
- [ ] 浏览器级 UI 回归（保存/回填/tooltip）
- [ ] 更细的 addressee：引用链多级回溯、群名片变更
- [ ] 反思质量评估：人工抽检，错误规范可删除
- [ ] 频率控制器的抗抖动（滞回/最小调整间隔）

## v2.0 — 长期

- [ ] 更细的白名单/按群策略
- [ ] 跨群迁移学习只共享先验，不共享内容
- [ ] 公开复现实验：真实群 A/B + 人类评分
- [ ] 与 MaiBot 核心的 Planner 工具选择更深度集成（需核心配合）

## 指标

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| 回复率 | 机器人消息 / 用户消息 | 可按群设目标 |
| 点名响应率 | addressed 决策中 reply 的比例 | 不下降 |
| 负反馈率 | reward=-1 / replies | 下降 |
| 冷启动样本 | policies.observations | 达到阈值后才学习 |
| 健康错误 | Hook/LLM 错误计数 | 可观测、不阻塞聊天 |
