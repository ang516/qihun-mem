"""数据模型（v1.4 §2.1 单行 schema / §2.3 state）。

`MemoryItem` 是 `memory.jsonl` 的一行在内存中的表示；`vector` 是向量 sidecar
的运行时投影，不落 memory.jsonl（§2 存在 `memory.vec.jsonl`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

ALLOWED_TYPES = ("episode", "fact", "preference")
ALLOWED_SCOPES = ("group", "dm")
ALLOWED_STATUS = ("active", "dormant")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601 时间；无时区按 UTC 处理。"""
    if not isinstance(value, str):
        raise ValueError(f"time value must be str, got {type(value).__name__}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class MemoryItem:
    """一条记忆（对应 memory.jsonl 的一行）。"""

    id: str
    user_id: str
    scope: str
    type: str
    content: str
    created_at: str
    last_access: str
    access_count: int = 0
    mention_count: int = 1
    S: float = 7.0
    valence: float = 0.0
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    turn_key: str | None = None
    # 运行时投影，不写入 memory.jsonl；由 memory.vec.jsonl 装载（§2 / §6.5）
    vector: list[float] | None = field(default=None, repr=False, compare=False)

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "user_id": self.user_id,
            "scope": self.scope,
            "type": self.type,
            "content": self.content,
            "created_at": self.created_at,
            "last_access": self.last_access,
            "access_count": int(self.access_count),
            "mention_count": int(self.mention_count),
            "S": round(float(self.S), 6),
            "valence": round(float(self.valence), 6),
            "status": self.status,
            "tags": list(self.tags),
        }
        if self.turn_key:
            data["turn_key"] = self.turn_key
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MemoryItem":
        if not isinstance(data, Mapping):
            raise ValueError("memory line must be a JSON object")
        for key in ("id", "user_id", "scope", "type", "content", "created_at", "last_access"):
            if key not in data:
                raise ValueError(f"memory line missing required field: {key}")
        item_type = str(data["type"])
        scope = str(data["scope"])
        if item_type not in ALLOWED_TYPES:
            raise ValueError(f"invalid memory type: {item_type!r}")
        if scope not in ALLOWED_SCOPES:
            raise ValueError(f"invalid scope: {scope!r}")
        status = str(data.get("status") or "active")
        if status not in ALLOWED_STATUS:
            status = "active"
        tags_raw = data.get("tags")
        tags = [str(t) for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        try:
            access_count = int(data.get("access_count") or 0)
            mention_count = int(data.get("mention_count") or 0)
            stability = float(data.get("S") or 0.0)
            valence = float(data.get("valence") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"memory line has non-numeric counters: {exc}") from exc
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            scope=scope,
            type=item_type,
            content=str(data["content"]),
            created_at=str(data["created_at"]),
            last_access=str(data["last_access"]),
            access_count=access_count,
            mention_count=mention_count,
            S=stability,
            valence=valence,
            status=status,
            tags=tags,
            turn_key=str(data["turn_key"]) if data.get("turn_key") else None,
        )

    # ---------- 便捷属性 ----------

    @property
    def created_dt(self) -> datetime:
        return parse_iso(self.created_at)

    @property
    def last_access_dt(self) -> datetime:
        return parse_iso(self.last_access)

    @property
    def is_dormant(self) -> bool:
        return self.status == "dormant"
