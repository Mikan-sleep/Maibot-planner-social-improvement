# Marketplace listing

## 基本信息

- **名称**：Planner Social
- **插件 ID**：`maibot-team.planner-social`
- **版本**：`1.0.0`
- **分类**：聊天增强 / AI 行为
- **标签**：`planner`, `群聊`, `社交`, `LLM`, `发言频率`
- **许可证**：GPL-3.0-or-later

## 一句话

让 Planner 知道“谁在对谁说、是否轮到你”，并用 LLM 反馈学习每个群的开口边界。

## 详情页文案

Planner 在群聊里经常遇到三个问题：把别人的“你”当成自己、在两个人对聊时插嘴、刚说完又追问。Planner Social 在 Planner 请求前注入发言对象/话轮/节奏状态，在回复后用 LLM 判断正负反馈，并为每个群维护一个可解释的 10 维策略。

它不要求机器人像人类一样严格等待话轮：`bot_tolerance` 承认群友对机器人主动发言有更高容忍度，`participation_mode=active` 允许自然短评；需要安静时切到 `balanced` 或 `conservative`。

### 亮点

- 多 @ / 别名 / 引用链解析
- LLM 反馈裁判，低置信度不污染策略
- 冷启动 + 跨群先验，新群不用从零学
- 每群独立策略 + 群规范反思
- 侧边栏 UI 每个参数都有 `?` 说明
- 强制 reply 默认关闭，必须配置白名单才生效，带冷却和负反馈熔断
- 离线 replay 实验脚本

### 权限说明

- `llm.generate`：仅用于反馈裁判和群规范反思
- `frequency.set_adjust` / `get_current_talk_value` / `get_adjust`：调节本会话发言频率
- 不读取无关群、不修改 MaiBot 核心配置

### 安全提示

默认不开启强制改写。开启前请阅读 README 的安全章节；连续强制回复可能导致禁言。

## 图标

`assets/icon.svg`

## 截图建议

1. 侧边栏参数卡（含 `?` tooltip）
2. 学习情况与权重表
3. 最近决策表（LLM 理由/强制改写）

上传市场前把截图放到 `assets/screenshots/`，并在此处引用。
