#!/usr/bin/env python3
"""Build a deterministic, installable plugin zip.

The archive contains the plugin directory contents at its root, which is the
layout MaiBot's local plugin installer and GitHub branch installs expect.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
INCLUDE_FILES = ["_manifest.json", "config.toml", "plugin.py", "webui.json", "README.md", "CHANGELOG.md", "LICENSE"]
INCLUDE_DIRS = ["planner_social", "ui", "assets", "docs"]
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def iter_files() -> list[Path]:
    files = [ROOT / name for name in INCLUDE_FILES]
    for directory in INCLUDE_DIRS:
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file())
    return sorted(
        path for path in files
        if path.is_file() and not any(part in EXCLUDE_PARTS for part in path.parts)
    )


def main() -> None:
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    plugin_id = manifest["id"]
    DIST.mkdir(exist_ok=True)
    archive = DIST / f"{plugin_id}-{version}.zip"
    files = iter_files()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(ROOT).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (DIST / f"{plugin_id}-{version}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"[OK] {archive} ({len(files)} files, {archive.stat().st_size} bytes)")
    print(f"[OK] sha256 {digest}")


if __name__ == "__main__":
    main()
