# Security

## 数据

- 插件数据保存在 `data/plugins/maibot-team.planner-social/`；
- 默认保存消息文本用于特征/裁判，保留 7 天；
- 可设置 `store_message_text=false` 只保存哈希/长度；
- LLM 裁判只发送必要的目标消息、机器人回复、后续少量消息和社交信号。

## 权限

- `llm.generate`：调用宿主模型做反馈裁判/反思；
- `frequency.*`：调整本会话 talk_value；
- 不访问文件系统外路径，不写 MaiBot 业务数据库。

## 强制改写

`force_reply` 默认关闭。开启后仍受：

- 白名单；
- 最低策略分；
- 冷却；
- 刚发言保护；
- 负反馈锁定；
- 连续负反馈熔断。

如果发现插件导致刷屏/禁言，立即在 UI 关闭 `force_reply` 或切到 `balanced`。

## 报告

请通过仓库 Issues 报告，不要贴出完整聊天记录、QQ 号或 API Key。
