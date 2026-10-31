#!/usr/bin/env python3
"""Validate plugin metadata and source without requiring a running MaiBot host.

This is intentionally pure standard library so CI can run it before installing
the host SDK. The host-specific schema validator is exercised by the integration
checklist in docs/development.md.
"""

from __future__ import annotations

import json
import py_compile
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["_manifest.json", "config.toml", "plugin.py", "webui.json", "README.md", "CHANGELOG.md", "LICENSE"]
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def main() -> None:
    for name in REQUIRED:
        if not (ROOT / name).is_file():
            fail(f"缺少必需文件: {name}")

    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 2:
        fail("manifest_version 必须是 2")
    if not ID_RE.fullmatch(str(manifest.get("id", ""))):
        fail("manifest.id 非法")
    if not VERSION_RE.fullmatch(str(manifest.get("version", ""))):
        fail("manifest.version 必须是 semver")
    for key in ("name", "description", "license", "urls", "host_application", "sdk"):
        if key not in manifest:
            fail(f"manifest 缺少 {key}")
    capabilities = set(manifest.get("capabilities", []))
    if "llm.generate" not in capabilities:
        fail("插件需要 llm.generate 能力用于反馈裁判")
    if "frequency.set_adjust" not in capabilities:
        fail("插件需要 frequency.set_adjust 能力用于频率调节")

    config = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    if not isinstance(config.get("social"), dict) or not isinstance(config.get("learning"), dict):
        fail("config.toml 缺少 [social] 或 [learning]")
    if not isinstance(config.get("plugin"), dict) or not isinstance(config["plugin"].get("enabled"), bool):
        fail("config.toml 缺少布尔值 [plugin].enabled")

    webui = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
    if webui.get("schema_version") != 2:
        fail("webui.json 必须使用 schema_version 2")
    pages = webui.get("pages", [])
    if not pages:
        fail("webui.json 至少需要一个 page")
    for page in pages:
        if page.get("mode") == "custom":
            entry = ROOT / str(page.get("entry", ""))
            if not entry.is_file() or entry.suffix.lower() != ".html":
                fail(f"custom page entry 不存在: {entry}")
            text = entry.read_text(encoding="utf-8")
            if "maibot.query" not in text or "maibot.action" not in text:
                fail("custom page 必须使用 maibot.query/action")
        for action in page.get("actions", {}).values():
            for name, parameter in action.get("parameters", {}).items():
                if parameter.get("type") not in {"string", "integer", "number", "boolean", "array", "object"}:
                    fail(f"action 参数类型非法: {name}")

    # UI action parameters must match the plugin API signature; drift here is a
    # common source of "save does nothing" bugs in custom pages.
    import ast

    tree = ast.parse((ROOT / "plugin.py").read_text(encoding="utf-8"))
    api_args: set[str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_update_settings":
            api_args = {arg.arg for arg in node.args.args if arg.arg != "self"}
            break
    if api_args is None:
        fail("plugin.py 缺少 api_update_settings")
    declared = set(pages[0].get("actions", {}).get("save", {}).get("parameters", {}))
    if api_args != declared:
        fail(f"UI save 参数与 API 不一致: API={sorted(api_args)} UI={sorted(declared)}")

    for path in ROOT.rglob("*.py"):
        if any(part in {"__pycache__", ".venv", "dist"} for part in path.parts):
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            fail(f"Python 编译失败 {path}: {exc}")

    for path in [ROOT / "plugin.py", *ROOT.glob("planner_social/*.py")]:
        text = path.read_text(encoding="utf-8")
        if re.search(r"[A-Za-z]:[\\/]", text) or "/home/" in text:
            fail(f"源码不应包含部署机绝对路径: {path}")
        if re.search(r"\b[0-9a-f]{32}\b", text):
            fail(f"源码不应包含会话 ID: {path}")

    print(f"[OK] plugin {manifest['id']} v{manifest['version']}")
    print(f"[OK] {len(list(ROOT.rglob('*.py')))} Python files compiled")
    print(f"[OK] {len(pages)} WebUI page(s)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
