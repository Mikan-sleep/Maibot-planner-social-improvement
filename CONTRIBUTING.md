# Contributing

## 开发流程

1. 从 `main` 开分支；
2. 修改代码/文档；
3. 本地跑：

```bash
ruff check plugin.py planner_social tests scripts experiments
python -m pytest
python scripts/validate_plugin.py
python scripts/build_plugin.py
```

4. 提交 PR，描述行为变化、默认值变化和风险。

## 设计约束

- 不修改 MaiBot 核心代码；
- 所有 Hook 异常都不能阻塞聊天；
- 新增外部调用必须有超时和降级；
- 影响默认行为的改动必须在 CHANGELOG 和 README 写明；
- 不得把部署机路径、群号、会话 ID、API Key 写入源码或测试。

## 测试

- 纯函数测试优先（`features`、`policy`、`frequency`、`addressee`）；
- SQLite 用 `tmp_path`；
- 不在 CI 中调用真实 LLM；
- 涉及 Hook/Plugin API 的改动在 PR 里附本地验证日志。

## 版本

- `feat` → minor；`fix`/docs → patch；破坏性变更 → major；
- 同步更新 `_manifest.json`、`pyproject.toml`、`planner_social/__init__.py`、`CHANGELOG.md`；
- Release tag 与 manifest 版本保持一致。
