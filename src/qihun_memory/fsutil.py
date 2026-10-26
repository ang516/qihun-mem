"""文件系统工具：原子写（v1.4 §2.4）。

重写 `memory.jsonl` / `users.json` / `summary.json` 一律走
「写 `*.tmp` → flush + fsync → `os.replace`」，重写期间崩溃不会破坏旧文件。
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: str | Path, text: str, *, fsync: bool = True) -> None:
    """原子替换写入 UTF-8 文本。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # 已 rename 成功时 tmp 不存在，或残留清理失败不影响结果
                pass
