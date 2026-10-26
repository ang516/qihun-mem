"""情感状态与用户目录（v1.4 §2.3 / §3.3）。

`state.json` 由情感系统写入，记忆系统**只读**（§0 原则 8 / §11 规则 9）。
`users.json` 是昵称快照：昵称会变、user_id 不变；注入时按当前昵称渲染。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import StateConfig
from .fsutil import atomic_write_text
from .models import parse_iso, utc_now

log = logging.getLogger("memory")


@dataclass(frozen=True)
class BotState:
    """bot 当前情绪（只读快照）。"""

    valence: float = 0.0
    avoidance: float = 0.0
    updated_at: str = ""
    stale: bool = True
    source: str = "neutral"  # "file" 或 "neutral"
    reason: str = ""  # neutral 的原因："missing" / "invalid" / "expired" / ""

    @property
    def is_positive(self) -> bool:
        return self.valence > 0


def load_bot_state(
    path: str | Path,
    *,
    cfg: StateConfig | None = None,
    now: datetime | None = None,
    metrics=None,
) -> BotState:
    """读取 state.json。缺失 / 损坏 / 过期 → 中性值，绝不抛异常（§2.3）。"""
    cfg = cfg or StateConfig()
    current = now or utc_now()
    file_path = Path(path)
    reason = ""
    try:
        if not file_path.is_file():
            reason = "missing"
        else:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                reason = "invalid"
            else:
                valence = float(raw.get("valence", 0.0))
                # NaN/inf 不是「极端情绪」，是脏数据（§2.3 按中性处理，绝不能 clamp 成最大值）
                if not math.isfinite(valence):
                    reason = "invalid"
                    valence = 0.0
                else:
                    valence = max(-1.0, min(1.0, valence))
                # §5.2：没有 avoidance 字段时用 max(0, -valence) 近似
                if raw.get("avoidance") is None:
                    avoidance = max(0.0, -valence)
                else:
                    avoidance = float(raw["avoidance"])
                    if not math.isfinite(avoidance):
                        reason = "invalid"
                        avoidance = 0.0
                    else:
                        avoidance = max(0.0, min(1.0, avoidance))
                updated_raw = str(raw.get("updated_at") or "")
                if not updated_raw:
                    reason = "invalid"
                elif reason:
                    pass
                else:
                    updated = parse_iso(updated_raw)
                    age_hours = (current - updated).total_seconds() / 3600.0
                    if age_hours > cfg.stale_hours:
                        reason = "expired"
                    elif age_hours < -1.0:
                        # 时间戳在未来超过 1 小时：视为时钟/数据异常，不当成新鲜状态
                        reason = "future"
                    else:
                        state = BotState(
                            valence=valence,
                            avoidance=avoidance,
                            updated_at=updated.isoformat(timespec="seconds"),
                            stale=False,
                            source="file",
                        )
                        if metrics is not None:
                            metrics.event("state.read", valence=state.valence, avoidance=state.avoidance)
                        return state
    except (OSError, ValueError, TypeError) as exc:
        reason = "invalid"
        log.warning("state.json unreadable, fallback to neutral: %r", exc)
    if metrics is not None:
        metrics.event("state.stale", reason=reason)
    return BotState(stale=True, source="neutral", reason=reason)


class UserDirectory:
    """users.json 的读写封装（昵称快照）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._users: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        self._users = {}
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._users = {str(k): str(v) for k, v in raw.items() if str(v).strip()}
        except (OSError, ValueError) as exc:
            log.warning("users.json unreadable, starting empty: %r", exc)

    def nickname(self, user_id: str) -> str:
        return self._users.get(str(user_id)) or f"用户{str(user_id)[-4:]}" if str(user_id) else str(user_id)

    def snapshot(self) -> dict[str, str]:
        return dict(self._users)

    def refresh(self, members: dict[str, str]) -> int:
        """用群成员列表刷新昵称快照；返回变化条数。"""
        changed = 0
        for user_id, nickname in members.items():
            key, value = str(user_id), str(nickname).strip()
            if value and self._users.get(key) != value:
                self._users[key] = value
                changed += 1
        if changed:
            self.save()
        return changed

    def save(self) -> None:
        payload = json.dumps(self._users, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            atomic_write_text(self.path, payload + "\n")
        except OSError as exc:
            log.warning("users.json write failed (nickname refresh skipped): %r", exc)
