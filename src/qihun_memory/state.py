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
from typing import Mapping

from .config import AffectConfig, StateConfig
from .fsutil import atomic_write_text
from .metrics import Metrics
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

# --------------------------------------------------------------------------
# v1.5 §5.3 / §5.4：user_affect 状态单点（附录 D1）
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class UserAffect:
    """某用户的感情聚合快照（只读视图）。"""

    affection: float = 0.5
    annoyance: float = 0.0
    familiarity: float = 0.3
    updated_at: str = ""
    stale: bool = False

    def to_dict(self) -> dict:
        return {
            "affection": round(float(self.affection), 6),
            "annoyance": round(float(self.annoyance), 6),
            "familiarity": round(float(self.familiarity), 6),
            "updated_at": self.updated_at,
            "stale": bool(self.stale),
        }


class UserAffectStore:
    """`user_affect.json` 的唯一读写入口。

    其他模块只能通过 get / step_update / decay 访问；直接读写 json 一律视为回归。
    写入失败不抛回主流程：内存保持权威，记 `user_affect.fail`（§3.3 / B4）。
    """

    _FIELDS = ("affection", "annoyance", "familiarity")

    def __init__(
        self,
        path: str | Path,
        cfg: AffectConfig | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.path = Path(path)
        self.cfg = cfg or AffectConfig()
        self.metrics = metrics
        self._data: dict[str, dict] = {}
        self.load()

    # ---------- 中性值 ----------

    def neutral(self, *, stale: bool = False) -> UserAffect:
        return UserAffect(
            affection=float(self.cfg.initial_affection),
            annoyance=float(self.cfg.initial_annoyance),
            familiarity=0.0 if stale else float(self.cfg.initial_familiarity),
            updated_at="",
            stale=stale,
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    # ---------- 读写 ----------

    def load(self) -> None:
        """容错加载：未知字段（如 trust）忽略；坏文件按中性处理并记 fail。"""
        self._data = {}
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._fail("unreadable", exc)
            return
        if not isinstance(raw, dict):
            self._fail("invalid_root", None)
            return
        for user_id, record in raw.items():
            if not isinstance(record, dict):
                self._fail("invalid_record", None)
                continue
            clean = self._clean_record(record)
            if clean is None:
                self._fail("invalid_values", None)
                continue
            self._data[str(user_id)] = clean

    def _clean_record(self, record: dict) -> dict | None:
        values: dict[str, float] = {}
        for field in self._FIELDS:
            raw_value = record.get(field)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            values[field] = self._clamp(value)
        updated_raw = record.get("updated_at")
        updated_at = ""
        if isinstance(updated_raw, str) and updated_raw.strip():
            try:
                updated_at = parse_iso(updated_raw).isoformat(timespec="seconds")
            except (ValueError, TypeError):
                updated_at = ""
        values["updated_at"] = updated_at
        return values

    def save(self) -> bool:
        """原子写；失败只记指标，不抛回调用方。"""
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            atomic_write_text(self.path, payload + "\n")
            return True
        except OSError as exc:
            self._fail("write_failed", exc)
            return False

    # ---------- 公开接口 ----------

    def _stale(self, record: dict, now: datetime) -> bool:
        updated_at = record.get("updated_at") or ""
        if not updated_at:
            return True
        try:
            updated = parse_iso(updated_at)
        except (ValueError, TypeError):
            return True
        age_days = (now - updated).total_seconds() / 86400.0
        return age_days >= float(self.cfg.stale_days)

    def get(self, user_id: str, now: datetime | None = None) -> UserAffect:
        """读 + stale 判定 + 中性兜底；不写盘。"""
        current = now or utc_now()
        record = self._data.get(str(user_id))
        if record is None:
            return self.neutral()
        if self._stale(record, current):
            return self.neutral(stale=True)
        return UserAffect(
            affection=float(record["affection"]),
            annoyance=float(record["annoyance"]),
            familiarity=float(record["familiarity"]),
            updated_at=str(record.get("updated_at") or ""),
            stale=False,
        )

    def step_update(
        self,
        user_id: str,
        deltas: Mapping[str, float],
        *,
        now: datetime | None = None,
    ) -> UserAffect:
        """每轮聚合后的单次更新：x += delta·(1-x)，clamp [0,1]，刷新 updated_at。

        调用方只传“步长”（如 familiarity: 0.1 / affection: 0.1*I_joy），
        不自己算映射、不自己写 json。
        """
        current = now or utc_now()
        user = str(user_id)
        record = self._data.get(user)
        if record is None or self._stale(record, current):
            base = self.neutral(stale=record is not None)
            record = {
                "affection": base.affection,
                "annoyance": base.annoyance,
                "familiarity": 0.0 if record is not None else base.familiarity,
                "updated_at": "",
            }
        updated = dict(record)
        for field in self._FIELDS:
            if field not in deltas:
                continue
            try:
                delta = float(deltas[field])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(delta):
                continue
            old = float(updated.get(field, 0.0))
            updated[field] = self._clamp(old + delta * (1.0 - old))
        updated["updated_at"] = current.isoformat(timespec="seconds")
        self._data[user] = updated
        self.save()
        return UserAffect(
            affection=updated["affection"],
            annoyance=updated["annoyance"],
            familiarity=updated["familiarity"],
            updated_at=updated["updated_at"],
            stale=False,
        )

    def decay(
        self,
        user_id: str,
        days: float | None = None,
        *,
        now: datetime | None = None,
    ) -> UserAffect:
        """惰性衰减 + stale 归零：annoyance *= 0.9^floor(days/7)。"""
        current = now or utc_now()
        user = str(user_id)
        record = self._data.get(user)
        if record is None:
            return self.neutral()
        if self._stale(record, current):
            # stale：整条中性、familiarity 归零；updated_at 用当前时间，避免反复告警
            self._data[user] = {
                "affection": float(self.cfg.initial_affection),
                "annoyance": float(self.cfg.initial_annoyance),
                "familiarity": 0.0,
                "updated_at": current.isoformat(timespec="seconds"),
            }
            self.save()
            self._event("user_affect.stale", user_id=user)
            return self.get(user, current)
        if days is None:
            updated_raw = record.get("updated_at") or ""
            try:
                days = (current - parse_iso(updated_raw)).total_seconds() / 86400.0
            except (ValueError, TypeError):
                days = 0.0
        days = max(0.0, float(days))
        if days <= 0:
            return self.get(user, current)
        period = max(1e-9, float(self.cfg.annoyance_decay_period_days))
        steps = int(days // period)
        if steps > 0:
            base = float(self.cfg.annoyance_decay_base)
            record = dict(record)
            record["annoyance"] = self._clamp(record["annoyance"] * (base ** steps))
            self._data[user] = record
            self.save()
        return self.get(user, current)

    # ---------- 观察 ----------

    def snapshot(self) -> dict:
        return {user: dict(record) for user, record in self._data.items()}

    def _event(self, name: str, **fields) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.event(name, **fields)
        except Exception:  # pragma: no cover
            pass

    def _fail(self, reason: str, exc: BaseException | None) -> None:
        log.warning("user_affect.%s: %r", reason, exc)
        self._event("user_affect.fail", reason=reason, exc_type=type(exc).__name__ if exc else None)
