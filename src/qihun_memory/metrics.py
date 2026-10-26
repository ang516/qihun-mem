"""观测指标（v1.4 §12）：每轮/每次关键操作的 JSONL 结构化日志。

日志字段遵守 §12.2 隐私最小化：默认不记正文，只记长度、哈希、计数与异常类型。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sanitize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        # 代理字符会让 json 写出 / utf-8 写盘抛 UnicodeEncodeError，日志绝不能影响主流程
        value = value.encode("utf-8", "replace").decode("utf-8")
        return value if len(value) <= 300 else value[:300] + "…"
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in list(value)[:10]]
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in list(value.items())[:10]}
    return str(value)[:300]


class Metrics:
    """线程安全的本地指标记录器；文件写入失败不影响主流程。"""

    def __init__(self, log_path: str | Path | None = None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.log_path = Path(log_path) if log_path else None
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, name: str, **fields: Any) -> None:
        """记录一个指标事件；任何异常都被吞掉，绝不影响主流程。"""
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": name,
        }
        for key, value in fields.items():
            if value is not None:
                record[key] = _sanitize(value)
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + 1
        if self.log_path is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with open(self.log_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            # 磁盘/权限问题不允许影响聊天主流程（§4.2.5 / §4.2.4 同一原则）
            pass

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def count(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "log_path": str(self.log_path) if self.log_path else None,
            "counters": self.counters(),
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
