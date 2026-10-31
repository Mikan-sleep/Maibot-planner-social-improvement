# 开发与维护文档

## 目录结构

```
plugins/maibot-team.planner-social/
├── _manifest.json          # 插件清单（SDK v2）
├── config.toml             # 默认配置
├── plugin.py               # 入口：Hook / Event / API
├── planner_social/
│   ├── config.py           # PluginConfigBase 配置模型
│   ├── models.py           # MessageRecord / SocialSignals / SocialState
│   ├── features.py         # 消息解析、信号、特征向量（纯函数）
│   ├── policy.py           # 在线 logistic 策略
│   ├── prompt.py           # Planner 提示词渲染
│   └── store.py            # 插件本地 SQLite
├── webui.json              # WebUI 协议 v2，custom page 声明
├── ui/
│   └── dashboard.html      # 侧边栏页面：参数 ? 提示、统计、最近决策
├── docs/
│   ├── design.md           # 设计依据与公式
│   ├── development.md      # 本文件
│   └── roadmap.md          # 路线图
├── tests/                  # 纯函数/策略单测
├── README.md
└── CHANGELOG.md
```

## 生命周期

- `on_load`：创建/迁移 SQLite，加载策略缓存，清理过期数据。
- `on_unload`：关闭 SQLite 连接。
- `on_config_update`（可选）：配置热重载时清空策略缓存（当前不需要，权重从 DB 读）。

## 使用的 MaiBot Hook

### `maisaka.planner.before_request`

- 载荷：`items`、`session_id`、`tool_definitions` 等。
- 本插件：
  1. 解析 `UserMessageItem` / `AssistantMessageItem` 的文本与 meta；
  2. 合并本地最近消息；
  3. 计算 signals → features → policy.predict；
  4. 用 `SystemMessageItem` 插入到“最后一条用户消息之前”。
- 返回：`{"action": "continue", "modified_kwargs": {...,"items": new_items}}`。
- 注意：插入的 Item 必须有合法的 `meta`（`item_id`、`logical_turn_id`、ISO `timestamp`）和 `parts: [{type:"text",text:...}]`。

### `maisaka.planner.after_response`

- 载荷：`output_items`、`session_id` 等。
- 本插件是 BLOCKING 模式：记录 `reply` / `stop` / 其他动作；active 模式下可把
  `stop/wait` 改写为 `reply`，并返回 `modified_kwargs.output_items`。
- 改写后的 reply 使用 `reply_reference` + `reply_style=简短表达`，实际文本仍由 MaiBot
  replyer 生成；决策记录 `forced=1`。
- `reply` 决策保持 `open`，等待后续消息反馈。

### `frequency.set_adjust` 能力

- 插件声明 `frequency.set_adjust` / `get_current_talk_value` / `get_adjust` 能力；
- 入站消息时把 `talk_multiplier` 写入会话级调整值；WebUI 保存参数后对已知会话立即重放；
- 该值是 MaiBot `talk_value` 的乘数，不是直接写 `bot_config.toml`。

### `llm.generate` 能力

- 插件声明 `llm.generate` 能力，用于 LLM 反馈裁判；
- 只在明确信号或攒够 `neutral_limit` 条消息时调用，后台任务执行，不阻塞消息链；
- 裁判提示词在 `plugin._build_judge_prompt`，只发目标消息、机器人回复、后续消息和社交信号；
- 返回 JSON 解析失败/低置信度/网络异常都按中性关闭，不污染权重。

### `send_service.after_send`

- 载荷：序列化的出站 `message`、`sent`。
- 本插件记录机器人自己的 `message_id` 和实际文本；文本会挂到最近的 open/judging 决策上，
  供 LLM 裁判使用。

### `chat.receive.after_process` Hook

- 载荷：`message`（序列化的 SessionMessage，含 `message_info`、`session_id`、`processed_plain_text`、`reply_to`、`is_at` 等）。
- 本插件记录入站消息并评估上一条 reply 的反馈。
- 当前 MaiBot 版本里 `ON_MESSAGE` 事件分发被注释掉，所以入站消息走这个 Hook；`EventHandler(ON_MESSAGE)` 仅保留兼容。

## 数据模型

SQLite 文件：`data/plugins/maibot-team.planner-social/planner_social.sqlite3`

```sql
messages(id, message_id, session_id, group_id, user_id, user_name, text,
         timestamp, is_bot, reply_to, is_at, is_mentioned)
session_groups(session_id, group_id, group_name, updated_at)
decisions(id, decision_id, session_id, group_id, created_at, action,
          target_message_id, utility, threshold, features_json,
          evidence_json, status, reward, feedback_at, neutral_count,
          reply_text, forced, label_source, judge_reason)
policies(group_id, weights_json, positive, negative, updated_at)
settings(key, value_json, updated_at)
session_groups(session_id, group_id, group_name, updated_at)
schema_meta(key, value)
```

- `messages` 唯一键 `(session_id, message_id)`，重复事件不会膨胀。
- `decisions.status`：`open` 等待反馈，`closed` 已处理。
- `policies.weights_json` 与 `FEATURE_NAMES` 对齐；读取时维度不符会回退默认权重。

## 配置

配置模型在 `config.py`，字段说明直接对应 `config.toml`。新增配置时：

1. 在 `SocialSection` / `PrivacySection` 添加 `Field(default=...)`；
2. 在 `config.toml` 写默认值；
3. 在 `README.md` 配置表补充；
4. 如果影响行为，补 `tests/`。

## 扩展方式

### 新增一个特征

1. `features.py`：在 `FEATURE_NAMES` 末尾加名称（不要插在中间，避免旧权重错位）。
2. `features.build_feature_vector`：追加数值；更新 `policy.DEFAULT_WEIGHTS` 同长度。
3. `policy.py`：默认权重加一项；注意 `from_dict` 维度校验会自动回退。
4. 测试：`tests/test_features.py` 断言长度/顺序；`tests/test_policy.py` 断言默认预测方向。
5. `docs/design.md` 特征表更新。

### 新增反馈信号

1. `plugin._evaluate_feedback` 增加判断分支；
2. 明确它属于强反馈（更新权重）还是弱反馈（只关闭决策）；
3. 在 `docs/design.md` 学习信号表更新；
4. 增加单测（纯函数放 features，流程放手工验证）。

### 新增 API

用 `@API("name", description=..., version="1")` 声明；返回值必须是 JSON 可序列化 dict。更新 README API 表。

## 侧边栏 UI

`webui.json` 使用 WebUI 协议 v2（`schema_version=2`、`mode=custom`）。
页面 ID `dashboard`，`placement=sidebar`，图标 `chart`，`entry=ui/dashboard.html`。
自定义页面通过 `window.maibot.query/action` 调用 `dashboard` / `update_settings` / `reset`。

- query：`dashboard`
- actions：`save`（update_settings）、`reset`
- 参数：`enabled` / `reply_threshold` / `talk_multiplier` / `participation_mode` /
  `bot_tolerance` / `force_reply` / `force_min_utility` / `force_cooldown_seconds` /
  `force_after_bot_seconds` / `force_lock_after_negative_seconds` / `feedback_mode` / `learning_rate`
- 参数写入 `settings` 表；页面每 8 秒刷新统计，但不会覆盖未保存的输入
- 每个参数旁有 `?`，悬停/聚焦显示 `role=tooltip` 说明
- 修改 `webui.json` 或 `ui/dashboard.html` 后需要重载插件；协议变更时以
  `src/plugin_runtime/webui_schema.py` 为准

## 测试

```bash
cd plugins/maibot-team.planner-social
PYTHONPATH=. python -m pytest tests -q
```

测试只覆盖纯函数和策略，不启动 MaiBot；插件加载验证需要重启宿主。

## 日志与排查

- 插件日志：`self.ctx.logger`，自动进入宿主日志（`plugin.<id>`）。
- 常见排查：
  - 没有注入：确认 `social.enabled=true`、`bot_names` 包含实际名字、Hook 是否加载。
  - 策略不更新：确认出现明确负反馈/被点名；中性反馈只关闭决策。
  - `reply_to` 不生效：适配器可能没上报，检查 `send_service.after_send` 是否有机器人 message_id。
- 数据检查：用 SQLite 打开插件 DB，看 `decisions` 的 `status/reward` 与 `policies.weights_json`。

## 版本与发布

- 版本写在 `_manifest.json` 和 `__init__.py`，每次行为变化同步 `CHANGELOG.md`。
- 数据库 schema 变化时提升 `SCHEMA_VERSION`，在 `store.initialize` 写迁移；不要静默删用户数据。
- 发布前跑测试，并在测试群观察注入块是否出现、是否 429/超时。

## 独立项目工程化

本仓库是插件源码仓库，QQBot 部署目录只接收发布产物。不要直接在
`MaiBot/plugins/...` 里开发。

### 常用命令

```bash
python -m pip install -e ".[dev]"
ruff check plugin.py planner_social tests scripts experiments
python -m pytest
python scripts/validate_plugin.py
python scripts/build_plugin.py
```

### 发布流程

1. 更新 `_manifest.json`、`pyproject.toml`、`planner_social/__init__.py` 的版本；
2. 更新 `CHANGELOG.md`；
3. 跑 lint/test/validate/build；
4. 提交并打 tag（例如 `v1.0.0`）；
5. GitHub Release workflow 会附上 `dist/maibot-team.planner-social-1.0.0.zip` 和 sha256；
6. 部署机解压 release zip 到 `MaiBot/plugins/maibot-team.planner-social/`，重启/重载插件。

### v1.0 新增模块

- `planner_social/addressee.py`：多 @、别名、引用链；
- `planner_social/frequency.py`：回复率闭环；
- `store.reflections` / `store.frequency_state`：反思与自动倍率；
- `policies.observations` / `prior_source`：冷启动和跨群先验；
- `decisions.addressed`：点名响应指标；
- `experiments/replay_policy.py`：离线 replay；
- `.github/workflows/ci.yml` / `release.yml`：CI 与 Release。

### 数据库迁移

`SCHEMA_VERSION` 当前为 5。新增列通过 `ALTER TABLE` 兼容旧库；新增表通过
`CREATE TABLE IF NOT EXISTS`。不要用“删库重建”解决迁移。
