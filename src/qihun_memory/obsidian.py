"""Obsidian 只读访问（本项目对 Obsidian 的唯一交互方式）。

硬约束：
- 本模块**只提供读取方法**，没有任何写入/删除 API；
- 所有路径必须落在库根目录内（拒绝 .. 穿越、绝对路径、符号链接逃逸）；
- 读写权限上只以 "r" 模式打开文件，不创建/修改/删除库内任何内容。

记忆数据由本系统自己的 store 直接写数据目录（不受此限制）；Obsidian 只作为
只读的知识/人设来源，绝不通过 Obsidian 写回（用户要求「obsidian 仅做只读」）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("memory")

_ALLOWED_SUFFIXES = {".md", ".txt", ".json"}


class VaultError(RuntimeError):
    """只读库访问错误。"""


class ReadOnlyObsidianVault:
    """一个只读的 Obsidian 库视图。"""

    readonly = True

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    def _resolve(self, relative: str) -> Path:
        if not relative or relative.strip() == "":
            raise VaultError("path must not be empty")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise VaultError("absolute paths are not allowed")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise VaultError(f"path escapes vault: {relative!r}") from exc
        if not resolved.is_file():
            raise VaultError(f"note not found: {relative!r}")
        return resolved

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list_notes(self) -> list[str]:
        """列出库内所有 Markdown 笔记（相对路径，posix 风格，排序稳定）。"""
        if not self.exists:
            return []
        notes: list[str] = []
        for path in self.root.rglob("*.md"):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            if not path.is_file():
                continue
            notes.append(relative.as_posix())
        return sorted(notes)

    def read_note(self, relative: str) -> str:
        path = self._resolve(relative)
        if path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise VaultError(f"unsupported file type: {path.suffix!r}")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def read_persona(self, name: str = "persona.md") -> str:
        try:
            return self.read_note(name)
        except VaultError:
            return ""

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, str]]:
        """在 Markdown 笔记里做只读子串搜索。"""
        needle = (query or "").strip().casefold()
        if not needle:
            return []
        hits: list[dict[str, str]] = []
        for relative in self.list_notes():
            try:
                text = self.read_note(relative)
            except (VaultError, OSError):
                continue
            lowered = text.casefold()
            index = lowered.find(needle)
            if index < 0:
                continue
            start = max(0, index - 30)
            snippet = text[start : index + len(needle) + 50].replace("\n", " ")
            hits.append({"path": relative, "snippet": snippet.strip()})
            if len(hits) >= limit:
                break
        return hits

    # 明确禁止写入：不提供 write/append/delete/move/copy 方法
    def __repr__(self) -> str:
        return f"ReadOnlyObsidianVault(root={str(self.root)!r})"


def vault_from_config(cfg: Config) -> ReadOnlyObsidianVault | None:
    """配置了 vault_dir 时返回只读库视图，否则 None。"""
    if not cfg.vault_dir:
        return None
    return ReadOnlyObsidianVault(cfg.vault_dir)
