"""数据模型（v1.5-r2 §3.1 单行 schema / §2.3 state）。

`MemoryItem` 是 `memory.jsonl` 的一行在内存中的表示；`vector` 是向量 sidecar
的运行时投影，不落 memory.jsonl（§3 存在 `memory.vec.jsonl`）。

v1.5 新增：
- `affect` 事件级标签（label/intensity），非法只丢字段不丢条；
- `Set` / `Inc` 操作符（update_memory 的白名单写入口，附录 D1）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

ALLOWED_TYPES = ("episode", "fact", "preference")
ALLOWED_SCOPES = ("group", "dm")
ALLOWED_STATUS = ("active", "dormant")
# §8 硬上限：任何路径下 S 都不得超过 90 天（§11 规则 7）
MAX_STABILITY = 90.0

# v1.5 §3.1.1：7 类事件 affect；neutral 不参与正负聚合
ALLOWED_AFFECT_LABELS = (
    "joy",
    "sadness",
    "anger",
    "anxiety",
    "affection",
    "disgust",
    "neutral",
)
JOY_AFFECT_LABELS = frozenset({"joy", "affection"})
ANGER_AFFECT_LABELS = frozenset({"anger", "disgust"})


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


# --------------------------------------------------------------------------
# affect 规整（模型层只做容错；解析层负责计数指标）
# --------------------------------------------------------------------------

def sanitize_affect(value: Any) -> dict[str, Any] | None:
    """把任意输入规整为 {label, intensity}；非法返回 None（调用方决定是否丢字段）。

    §3.1.1：label 必须属于 7 类枚举；intensity ∈ [0,1]，越界截断；
    非数字 → None。label 大小写/空白在这里做一次宽容归一。
    """
    if not isinstance(value, Mapping):
        return None
    raw_label = value.get("label")
    if not isinstance(raw_label, str):
        return None
    label = raw_label.strip().lower()
    if label not in ALLOWED_AFFECT_LABELS:
        return None
    raw_intensity = value.get("intensity", 0.0)
    if isinstance(raw_intensity, bool):
        return None
    try:
        intensity = float(raw_intensity)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(intensity):
        return None
    intensity = max(0.0, min(1.0, intensity))
    return {"label": label, "intensity": round(intensity, 6)}


def affect_label(affect: Mapping[str, Any] | None) -> str:
    if not isinstance(affect, Mapping):
        return "neutral"
    label = affect.get("label")
    return str(label).strip().lower() if isinstance(label, str) else "neutral"


def affect_intensity(affect: Mapping[str, Any] | None) -> float:
    if not isinstance(affect, Mapping):
        return 0.0
    try:
        value = float(affect.get("intensity", 0.0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


# --------------------------------------------------------------------------
# update_memory 操作符（附录 D1：状态单点）
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Set:
    """把白名单字段设为指定值（S / status / last_access）。"""

    field: str
    value: Any


@dataclass(frozen=True)
class Inc:
    """对白名单计数字段做自增（access_count / mention_count）；旧值在单点内部读取。"""

    field: str
    delta: int = 1


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
    affect: dict[str, Any] | None = None
    # 运行时投影，不写入 memory.jsonl；由 memory.vec.jsonl 装载（§3 / §7.5）
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
        if self.affect is not None:
            data["affect"] = dict(self.affect)
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
            access_count = max(0, int(data.get("access_count") or 0))
            mention_count = max(0, int(data.get("mention_count") or 0))
            stability = float(data.get("S") or 0.0)
            valence = float(data.get("valence") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"memory line has non-numeric counters: {exc}") from exc
        # 时间戳必须可解析：坏行由 store 隔离到 memory.bad.jsonl，不能让检索/维护 500（§3.3）
        created_at = str(data["created_at"])
        last_access = str(data["last_access"])
        parse_iso(created_at)
        parse_iso(last_access)
        if not math.isfinite(stability) or stability <= 0:
            raise ValueError("memory line has invalid S")
        stability = min(stability, MAX_STABILITY)  # §11 规则 7：加载路径也封顶
        if not math.isfinite(valence):
            valence = 0.0
        valence = max(-1.0, min(1.0, valence))
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            scope=scope,
            type=item_type,
            content=str(data["content"]),
            created_at=created_at,
            last_access=last_access,
            access_count=access_count,
            mention_count=mention_count,
            S=stability,
            valence=valence,
            status=status,
            tags=tags,
            turn_key=str(data["turn_key"]) if data.get("turn_key") else None,
            affect=sanitize_affect(data.get("affect")),
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

    @property
    def affect_label(self) -> str:
        return affect_label(self.affect)

    @property
    def affect_intensity(self) -> float:
        return affect_intensity(self.affect)
