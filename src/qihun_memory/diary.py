"""叙事层：情感日记（v1.6-rc4 §8 / §3.4 / 附录 D7）。

定位（§8.1 / 硬规则 19）：
- 只提供人格延续语境，不参与检索打分、装填 1200 预算、S/R 计算、
  事实层 scope/user_id 过滤与 user_affect 聚合；
- 日记注入只按 scope_tag 过滤（group / dm），mixed 绝不自动生成 / 落盘 / 注入；
- 生成失败 / 读取失败 / 隐私命中 / 超预算 / 幂等命中 → 静默降级，只记指标，
  绝不影响回答可用性（硬规则 19 / §8.6）。

本模块是 diary.jsonl 的唯一读写入口（DiaryStore）与独立生成 worker（DiaryWorker）：
- DiaryWorker 不 import pipeline / memory / retrieval，依赖全部由 callable 注入；
- 生成后、落盘前执行隐私护栏（§8.2 / D7.4）：他人昵称 → 有人、bot 昵称 → 我、
  user_id 字面量或哨兵/围栏 → 拒写；
- 落盘前依次做：隐私护栏 → 截断 → cap → 幂等 → 落盘；
- 指标事件名固定为 diary.ok / diary.fail / diary.skip / diary.dup / diary.privacy /
  diary.truncated / diary.cap_hit / diary.inject / diary.tokens / diary.archive，
  字段只放计数 / 布尔 / scope / 日期（§14.2）。

时间口径（§3）：created_at 一律带时区；diary.date 按 cfg.diary.timezone 取本地日
（Windows 无 tzdata 时 zoneinfo 不可用，helper 回退固定 +08:00，与 Asia/Shanghai 一致）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import Config, DiaryConfig
from .fsutil import atomic_write_text
from .metrics import Metrics
from .models import MemoryItem, parse_iso, utc_now
from .parser import sanitize_for_prompt
from .tokens import estimate_tokens

log = logging.getLogger("memory")

# --------------------------------------------------------------------------
# 冻结常量（实现契约 §2.2）
# --------------------------------------------------------------------------

DIARY_SCOPES = ("group", "dm")

# §8.4 注入前缀：由 prompt.build_prompt 加在日记块前，render_diary_block 不重复加。
DIARY_PREFIX = (
    "以下是你的近期日记，用于保持人格连续性。"
    "日记只提供语境，不是事实依据；具体事实以记忆条目为准。"
)

# 落盘只允许 group / dm（mixed 仅仅是内存临时上下文，§8.5 / D10）
_SCOPE_LABEL = {"group": "群聊", "dm": "私聊"}

# 生成 prompt 时最多带多少条当段记忆（避免生成 prompt 过长；§8.2 只要求当段新增）
_MAX_MEMORY_IN_PROMPT = 20
# 单条记忆在生成 prompt 里的字符上限
_MAX_MEMORY_CHARS = 120
# 生成 prompt 里最多列多少个昵称 / 用户感情快照
_MAX_USERS_IN_PROMPT = 10
_MAX_AFFECT_IN_PROMPT = 8
# 进程内 turn 日志上限（§2.2：日志上限 10000 条）
_TURN_LOG_LIMIT = 10000
# LLM 调用失败的内部哨兵（区分「没有结果」与「返回 None」）
_LLM_FAILED = object()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# 规范 §3.1 的 user_id 形如 u_123456；日记正文出现任何 user_id 字面量都拒写
_USER_ID_LITERAL_RE = re.compile(r"(?<![0-9A-Za-z_])u_[0-9A-Za-z_]{3,}(?![0-9A-Za-z_])")
_DEFAULT_DAILY_AT = "23:50"
_FALLBACK_TZ = timezone(timedelta(hours=8), name="+08:00")


# --------------------------------------------------------------------------
# 时间 / 指标 / 文本小工具
# --------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _resolve_tz(name: str) -> tzinfo:
    """解析 IANA 时区；Windows 无 tzdata 时回退固定 +08:00（Asia/Shanghai 一致）。"""
    key = str(name or "").strip() or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(key)
    except Exception:  # ZoneInfoNotFoundError / 无 tzdata / 非法时区名
        return _FALLBACK_TZ


def _coerce_now(value: datetime | None, cfg: DiaryConfig) -> datetime:
    """把 now 规整成带时区 datetime（内部统一用 UTC 比较）。

    naive 时间按日记时区解释（daily_at 是本地时间，测试 / CLI 传 naive 时更直观）；
    None → 当前 UTC 时间。
    """
    if not isinstance(value, datetime):
        value = utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=_resolve_tz(cfg.timezone))
    return value.astimezone(timezone.utc)


def _local_date_str(value: datetime, cfg: DiaryConfig) -> str:
    return value.astimezone(_resolve_tz(cfg.timezone)).strftime("%Y-%m-%d")


def _local_day_start(value: datetime, cfg: DiaryConfig) -> datetime:
    local = value.astimezone(_resolve_tz(cfg.timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_hhmm(text: str) -> time:
    """解析 daily_at（HH:MM）；非法值回退默认 23:50，绝不影响触发判定。"""
    match = _HHMM_RE.match(str(text or "").strip())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    log.warning("diary.daily_at invalid %r, fallback to %s", text, _DEFAULT_DAILY_AT)
    return time(23, 50)


def _parse_ts(value: str) -> datetime:
    """解析 ISO 时间并换算 UTC（models.parse_iso：无时区按 UTC）。"""
    return parse_iso(str(value))


def _seq_of(entry_id: str) -> int:
    try:
        return int(str(entry_id).split("_")[-1])
    except (ValueError, IndexError):
        return 0


def _safe_float(raw: Any, default: float = 0.0) -> float:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _emit(metrics: Metrics | None, name: str, **fields: Any) -> None:
    """记一条 diary 指标；指标自身异常绝不能影响主流程。"""
    if metrics is None:
        return
    try:
        metrics.event(name, **fields)
    except Exception:  # pragma: no cover - 指标绝不反噬主流程
        log.warning("diary metrics event failed: %s", name)


def _truncate_tokens(text: str, budget: int) -> str:
    """按 estimate_tokens 截断到预算内（与 prompt._truncate_tokens 同口径）。"""
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip()


def _clip_text(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _clean_generated_text(text: str) -> str:
    """生成结果的基础清洗：去控制符 / 孤立代理字符，压平首尾空白。"""
    chars: list[str] = []
    for ch in str(text or ""):
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            continue
        if code < 32 and ch not in "\n\t":
            continue
        if code == 127 or 0x80 <= code <= 0x9F:
            continue
        chars.append(ch)
    return "".join(chars).strip()


def _normalize_scope(scope: Any) -> str | None:
    """落盘只允许 group / dm；mixed / 其它一律 None（§8.5 / D10）。"""
    text = str(scope or "").strip().lower()
    return text if text in DIARY_SCOPES else None


def _validate_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not _DATE_RE.match(text):
        raise ValueError("invalid diary date: " + repr(value))
    # 逐字段校验，避免 2026-99-99 这类格式合法但日期非法的值
    datetime.strptime(text, "%Y-%m-%d")
    return text


def _parse_created_at(value: Any) -> str:
    """校验 created_at：必须是带时区的合法 ISO 时间（§3 禁止 naive）。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("created_at is required")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid created_at: " + repr(value)) from exc
    if parsed.tzinfo is None:
        raise ValueError("created_at must carry a timezone offset")
    return text


# --------------------------------------------------------------------------
# 数据模型（实现契约 §2.2 冻结）
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DiaryEntry:
    """diary.jsonl 的一行（§3.4 schema；刻意不存在 user_id/affect/S/status）。"""

    id: str
    date: str
    scope_tag: str
    content: str
    tokens: int
    turn_range: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "date": str(self.date),
            "scope_tag": str(self.scope_tag),
            "content": str(self.content),
            "tokens": int(self.tokens),
            "turn_range": [str(item) for item in self.turn_range],
            "created_at": str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DiaryEntry":
        """逐字段校验；任何非法行抛 ValueError，由 DiaryStore 隔离到 diary.bad.jsonl。"""
        if not isinstance(data, Mapping):
            raise ValueError("diary line must be a JSON object")
        for key in ("id", "date", "scope_tag", "content", "created_at"):
            if key not in data:
                raise ValueError("diary line missing required field: " + key)
        entry_id = str(data["id"]).strip()
        if not entry_id:
            raise ValueError("diary id is empty")
        date_text = _validate_date_text(data["date"])
        scope_tag = str(data["scope_tag"]).strip().lower()
        if scope_tag not in DIARY_SCOPES:
            raise ValueError("invalid scope_tag: " + repr(scope_tag))
        content = str(data["content"]).strip()
        if not content:
            raise ValueError("diary content is empty")
        tokens_raw = data.get("tokens", 0)
        if isinstance(tokens_raw, bool):
            raise ValueError("diary tokens must be an integer")
        try:
            tokens = int(tokens_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("diary tokens must be an integer") from exc
        if tokens < 0:
            raise ValueError("diary tokens must be >= 0")
        range_raw = data.get("turn_range") or []
        if not isinstance(range_raw, (list, tuple)):
            raise ValueError("diary turn_range must be a list")
        turn_range = tuple(str(item) for item in range_raw)
        created_at = _parse_created_at(data["created_at"])
        return cls(
            id=entry_id,
            date=date_text,
            scope_tag=scope_tag,
            content=content,
            tokens=tokens,
            turn_range=turn_range,
            created_at=created_at,
        )


@dataclass
class DiaryLoadReport:
    """diary.jsonl 加载报告（与 store.LoadReport 同风格）。"""

    total_lines: int = 0
    valid: int = 0
    bad: int = 0
    new_bad: int = 0
    bad_samples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiaryPrompt:
    """一次日记生成的输入（prepare 产物）；due=False 时 prompt 为空串。"""

    scope_tag: str
    prompt: str
    due: bool
    date: str
    turn_range: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class DiaryRunReport:
    """一次生成 / 落盘的结果。

    status ∈ ok / fail / skip / dup / cap_hit / privacy / disabled / not_due
    （实现契约 §2.2）。
    """

    scope_tag: str
    status: str
    reason: str = ""
    entry_id: str | None = None
    tokens: int = 0


# --------------------------------------------------------------------------
# DiaryStore：diary.jsonl / diary.archive.jsonl / diary.bad.jsonl 唯一读写入口
# --------------------------------------------------------------------------

class DiaryStore:
    """单实例、单写者的叙事层存储（只读模式绝不写盘）。"""

    def __init__(self, cfg: Config, metrics: Metrics | None = None, read_only: bool = False) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.read_only = bool(read_only)
        self._entries: dict[str, DiaryEntry] = {}
        self._seq = 0
        self._loaded = False
        self.load_report = DiaryLoadReport()

    # ---------- 路径 ----------

    @property
    def diary_path(self) -> Path:
        return self.cfg.diary_file

    @property
    def archive_path(self) -> Path:
        return self.cfg.diary_archive_file

    @property
    def bad_path(self) -> Path:
        return self.cfg.diary_bad_file

    @property
    def entries(self) -> dict[str, DiaryEntry]:
        self._ensure_loaded()
        return self._entries

    # ---------- 加载 ----------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def load(self) -> DiaryLoadReport:
        """逐行 json.loads；坏行原样搬 diary.bad.jsonl（幂等、去重、不中断）。"""
        self._entries.clear()
        self._seq = 0
        report = DiaryLoadReport()
        bad_lines: list[str] = []
        for line in self._read_lines(self.diary_path):
            if not line.strip():
                continue
            report.total_lines += 1
            try:
                data = json.loads(line)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                report.bad += 1
                bad_lines.append(line)
                if len(report.bad_samples) < 5:
                    report.bad_samples.append(str(exc)[:120])
                continue
            try:
                entry = DiaryEntry.from_dict(data)
            except (ValueError, TypeError) as exc:
                report.bad += 1
                bad_lines.append(line)
                if len(report.bad_samples) < 5:
                    report.bad_samples.append(str(exc)[:120])
                continue
            if entry.id in self._entries:
                # id 重复无法安全恢复：整行按坏行隔离，不静默覆盖已有日记
                report.bad += 1
                bad_lines.append(line)
                if len(report.bad_samples) < 5:
                    report.bad_samples.append("duplicate diary id")
                continue
            self._entries[entry.id] = entry
            report.valid += 1
        if bad_lines and not self.read_only:
            report.new_bad = self._append_bad_lines(bad_lines)
        # id 续号同时扫描 archive，避免归档后 id 重用（与 MemoryStore 同口径）
        archived_seq = max((_seq_of(entry_id) for entry_id in self._archived_ids()), default=0)
        self._seq = max(
            max((_seq_of(entry_id) for entry_id in self._entries), default=0),
            archived_seq,
        )
        self._loaded = True
        self.load_report = report
        return report

    def _read_lines(self, path: Path) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
                return handle.read().splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("cannot read %s: %r", path, exc)
            return []

    def _append_bad_lines(self, lines: Sequence[str]) -> int:
        existing: set[str] = set()
        if self.bad_path.is_file():
            existing = set(self._read_lines(self.bad_path))
        fresh = [line for line in lines if line not in existing]
        if not fresh:
            return 0
        try:
            self.bad_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.bad_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(fresh) + "\n")
        except OSError as exc:
            log.warning("cannot append diary bad lines: %r", exc)
            return 0
        return len(fresh)

    def _archived_ids(self) -> list[str]:
        """archive 的 id 列表（只用于 id 续号；坏行跳过）。"""
        ids: list[str] = []
        for line in self._read_lines(self.archive_path):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                entry_id = str(data["id"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            ids.append(entry_id)
        return ids

    # ---------- 追加 / 重写 / 移档 ----------

    def next_id(self) -> str:
        """d_0001 续号；加载时已把 archive 里的最大序号并入。"""
        self._ensure_loaded()
        self._seq += 1
        return "d_" + str(self._seq).zfill(4)

    def append(self, entry: DiaryEntry) -> None:
        """追加一行（先落盘、再更新内存；落盘失败不更新内存，由调用方降级）。"""
        if self.read_only:
            raise RuntimeError("diary store is read-only")
        if not isinstance(entry, DiaryEntry):
            raise TypeError("DiaryStore.append expects DiaryEntry")
        if entry.scope_tag not in DIARY_SCOPES:
            raise ValueError("invalid diary scope_tag: " + repr(entry.scope_tag))
        self._ensure_loaded()
        line = json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"))
        self.diary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.diary_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
        self._entries[entry.id] = entry
        self._seq = max(self._seq, _seq_of(entry.id))

    def _write_entries(self, entries: Sequence[DiaryEntry]) -> None:
        body = "".join(
            json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            for entry in entries
        )
        atomic_write_text(self.diary_path, body)

    def rewrite(self) -> None:
        """全量原子重写（*.tmp -> fsync -> os.replace），按 id 升序保证确定性。"""
        if self.read_only:
            raise RuntimeError("diary store is read-only")
        self._ensure_loaded()
        ordered = sorted(self._entries.values(), key=lambda entry: _seq_of(entry.id))
        self._write_entries(ordered)

    def archive(self, ids: Iterable[str]) -> int:
        """把日记移入 diary.archive.jsonl（不物理删除，§3.4 / 附录 D7.5）。

        顺序：先 append archive → 再 rewrite diary.jsonl；幂等：已在 archive 的 id
        不重复追加（archive 成功但 rewrite 失败的重试场景）。
        """
        if self.read_only:
            raise RuntimeError("diary store is read-only")
        self._ensure_loaded()
        wanted: list[str] = []
        seen: set[str] = set()
        for raw_id in ids:
            entry_id = str(raw_id)
            if entry_id in self._entries and entry_id not in seen:
                seen.add(entry_id)
                wanted.append(entry_id)
        if not wanted:
            return 0
        to_move = [self._entries[entry_id] for entry_id in wanted]
        already = set(self._archived_ids())
        fresh = [entry for entry in to_move if entry.id not in already]
        if fresh:
            try:
                self.archive_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.archive_path, "a", encoding="utf-8", newline="\n") as handle:
                    for entry in fresh:
                        line = json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"))
                        handle.write(line + "\n")
            except OSError as exc:
                log.warning("diary archive append failed, keep entries in memory: %r", exc)
                return 0
        remaining = [entry for entry in self._entries.values() if entry.id not in seen]
        try:
            self._write_entries(remaining)
        except OSError as exc:
            # archive 已写入但主文件未重写：内存保持原状，下次重试 archive 幂等
            log.warning("diary rewrite after archive failed: %r", exc)
            return 0
        self._entries = {entry.id: entry for entry in remaining}
        return len(wanted)

    def archive_expired(self, now: datetime | None = None) -> int:
        """created_at ≤ now - retain_days 的全部移档；失败只记指标不抛（§3.5）。"""
        if self.read_only:
            return 0
        try:
            self._ensure_loaded()
            current = _coerce_now(now, self.cfg.diary)
            cutoff = current - timedelta(days=max(0, int(self.cfg.diary.retain_days)))
            expired = [
                entry.id
                for entry in self._entries.values()
                if _parse_ts(entry.created_at) <= cutoff
            ]
            if not expired:
                return 0
            moved = self.archive(expired)
            if moved:
                _emit(self.metrics, "diary.archive", count=moved)
            return moved
        except Exception as exc:  # 维护任务任何失败不得阻塞聊天（§3.5）
            log.warning("diary.archive_expired failed: %r", exc)
            _emit(self.metrics, "diary.fail", reason="archive", exc_type=type(exc).__name__)
            return 0

    # ---------- 查询 ----------

    def active(self, scope_tag: str, now: datetime | None = None) -> list[DiaryEntry]:
        """窗口 (now-retain_days, now] 内该 scope 的日记；created_at 倒序、再 id 倒序。"""
        self._ensure_loaded()
        if scope_tag not in DIARY_SCOPES:
            return []
        try:
            current = _coerce_now(now, self.cfg.diary)
            start = current - timedelta(days=max(0, int(self.cfg.diary.retain_days)))
            result: list[DiaryEntry] = []
            for entry in self._entries.values():
                if entry.scope_tag != scope_tag:
                    continue
                created = _parse_ts(entry.created_at)
                if start < created <= current:
                    result.append(entry)
            result.sort(
                key=lambda entry: (_parse_ts(entry.created_at), _seq_of(entry.id)),
                reverse=True,
            )
            return result
        except Exception as exc:  # 坏 created_at 已在 load 拦截，这里只做防御
            log.warning("diary.active failed: %r", exc)
            return []

    def active_count(self, scope_tag: str, now: datetime | None = None) -> int:
        return len(self.active(scope_tag, now))

    def last_created_at(self, scope_tag: str) -> datetime | None:
        """该 scope 最近一篇日记的 created_at（UTC datetime；没有则 None）。"""
        self._ensure_loaded()
        if scope_tag not in DIARY_SCOPES:
            return None
        latest: datetime | None = None
        for entry in self._entries.values():
            if entry.scope_tag != scope_tag:
                continue
            try:
                created = _parse_ts(entry.created_at)
            except (ValueError, TypeError):
                continue
            if latest is None or created > latest:
                latest = created
        return latest

    def has_idempotent(
        self,
        date: str,
        scope_tag: str,
        turn_range: Iterable[str] | None,
    ) -> bool:
        """幂等键 (date, scope_tag, turn_range) 是否已存在（daily / every_n 共用）。"""
        self._ensure_loaded()
        if scope_tag not in DIARY_SCOPES:
            return False
        date_text = str(date or "")
        wanted = tuple(str(item) for item in (turn_range or ()))
        for entry in self._entries.values():
            if entry.scope_tag != scope_tag or entry.date != date_text:
                continue
            if tuple(entry.turn_range) == wanted:
                return True
        return False

    def counts(self) -> dict[str, int]:
        """条目计数（total / group / dm），供 CLI / service 观测。"""
        self._ensure_loaded()
        result = {"total": len(self._entries), "group": 0, "dm": 0}
        for entry in self._entries.values():
            if entry.scope_tag in result:
                result[entry.scope_tag] += 1
        return result

    def file_sizes(self) -> dict[str, int]:
        """三个日记文件的字节大小（不存在记 0；观测用，失败不抛）。"""
        result: dict[str, int] = {}
        for name, path in (
            ("diary", self.diary_path),
            ("archive", self.archive_path),
            ("bad", self.bad_path),
        ):
            try:
                result[name] = int(path.stat().st_size)
            except OSError:
                result[name] = 0
        return result


# --------------------------------------------------------------------------
# DiaryWorker：独立生成 worker（依赖全部 callable 注入，不 import 事实层模块）
# --------------------------------------------------------------------------

class DiaryWorker:
    """情感日记生成 worker（§8.2 / 附录 D7.3）。

    依赖注入（全部只读，均由宿主绑定；worker 不持有写权限）：
    - memory_items: Callable[[], Iterable[MemoryItem]]，当段新增记忆；
    - state_loader: Callable[[], BotState]，bot 当前情绪快照；
    - users: UserDirectory，昵称快照；
    - affect_snapshot: Callable[[], Mapping]，user_affect.json 快照；
    - now_fn: Callable[[], datetime]，便于测试注入时间。
    """

    TURN_LOG_LIMIT = _TURN_LOG_LIMIT

    def __init__(
        self,
        cfg: Config,
        store: DiaryStore,
        *,
        metrics: Metrics | None = None,
        memory_items: Callable[[], Iterable[MemoryItem]] | None = None,
        state_loader: Callable[[], Any] | None = None,
        users: Any = None,
        affect_snapshot: Callable[[], Mapping[str, Any]] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.metrics = metrics if metrics is not None else getattr(store, "metrics", None)
        self._memory_items = memory_items
        self._state_loader = state_loader
        self._users = users
        self._affect_snapshot = affect_snapshot
        self._now_fn = now_fn or utc_now
        self._turns: dict[str, deque[tuple[str, datetime]]] = {
            scope: deque(maxlen=self.TURN_LOG_LIMIT) for scope in DIARY_SCOPES
        }

    # ---------- 时间 ----------

    def _now(self, now: datetime | None = None) -> datetime:
        value = now
        if value is None:
            try:
                value = self._now_fn()
            except Exception as exc:  # now_fn 异常不能让 worker 抛
                log.warning("diary.now_fn failed: %r", exc)
                value = utc_now()
        return _coerce_now(value, self.cfg.diary)

    # ---------- turn 日志（every_n 计数 / turn_range） ----------

    def record_turn(self, scope: str, turn_id: str, at: datetime | None = None) -> None:
        """记录一条该 scope 的 turn（内存日志，上限 10000 条，不落盘）。"""
        try:
            scope_key = _normalize_scope(scope)
            if scope_key is None:
                return
            log_entries = self._turns.get(scope_key)
            if log_entries is None:
                log_entries = deque(maxlen=self.TURN_LOG_LIMIT)
                self._turns[scope_key] = log_entries
            log_entries.append((str(turn_id or ""), self._now(at)))
        except Exception as exc:  # 记录失败只影响 every_n 触发，不影响主流程
            log.warning("diary.record_turn failed: %r", exc)

    def _turns_between(
        self,
        scope: str,
        start: datetime | None,
        end: datetime,
        *,
        inclusive_start: bool,
    ) -> list[tuple[str, datetime]]:
        result: list[tuple[str, datetime]] = []
        for turn_id, stamp in self._turns.get(scope, ()):
            if start is not None:
                if inclusive_start:
                    if stamp < start:
                        continue
                elif stamp <= start:
                    continue
            if stamp <= end:
                result.append((turn_id, stamp))
        return result

    @staticmethod
    def _range_ids(turns: Sequence[tuple[str, datetime]]) -> tuple[str, ...]:
        ids = [turn_id for turn_id, _stamp in turns if turn_id]
        if not ids:
            return ()
        if len(ids) == 1:
            return (ids[0],)
        return (ids[0], ids[-1])

    # ---------- 触发判定（§8.2：daily / every_n 均按 scope 分别判定） ----------

    def due(self, scope: str, now: datetime | None = None) -> bool:
        try:
            dcfg = self.cfg.diary
            if not dcfg.enabled:
                return False
            scope_key = _normalize_scope(scope)
            if scope_key is None:
                return False
            trigger = str(dcfg.trigger or "").strip().lower()
            if trigger not in ("daily", "every_n", "both"):
                return False
            current = self._now(now)
            if trigger in ("daily", "both") and self._daily_due(scope_key, current):
                return True
            if trigger in ("every_n", "both") and self._every_n_due(scope_key, current):
                return True
            return False
        except Exception as exc:
            log.warning("diary.due failed: %r", exc)
            _emit(self.metrics, "diary.fail", reason="due", exc_type=type(exc).__name__)
            return False

    def _daily_due(self, scope: str, current: datetime) -> bool:
        dcfg = self.cfg.diary
        local_now = current.astimezone(_resolve_tz(dcfg.timezone))
        if local_now.time() < _parse_hhmm(dcfg.daily_at):
            return False
        today = local_now.strftime("%Y-%m-%d")
        return not self._has_daily(scope, today)

    def _every_n_due(self, scope: str, current: datetime) -> bool:
        every_n = max(1, int(self.cfg.diary.every_n))
        start = self.store.last_created_at(scope)
        turns = self._turns_between(scope, start, current, inclusive_start=False)
        return len(turns) >= every_n

    def _has_daily(self, scope: str, date_text: str) -> bool:
        """当日同 scope 是否已有日记（daily 防重；也是 both 模式 daily 优先的判据）。"""
        try:
            for entry in self.store.entries.values():
                if entry.scope_tag == scope and entry.date == date_text:
                    return True
        except Exception as exc:
            log.warning("diary.has_daily failed: %r", exc)
        return False

    # ---------- prepare：组装生成输入 ----------

    def prepare(
        self,
        scope: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> DiaryPrompt:
        """组装一次生成输入；due=False（或 every_n 无新增记忆）时 prompt 为空串。"""
        scope_key = _normalize_scope(scope)
        try:
            current = self._now(now)
            today = _local_date_str(current, self.cfg.diary)
            if scope_key is None:
                return DiaryPrompt(str(scope or ""), "", False, today, (), "invalid_scope")
            if not self.cfg.diary.enabled:
                return DiaryPrompt(scope_key, "", False, today, (), "disabled")
            plan = self._plan(scope_key, current, force=force)
            if plan is None:
                return DiaryPrompt(scope_key, "", False, today, (), "not_due")
            reason, start, inclusive_start = plan
            turns = self._turns_between(scope_key, start, current, inclusive_start=inclusive_start)
            turn_range = self._range_ids(turns)
            memories = self._window_memories(scope_key, start, current, inclusive_start)
            if reason == "every_n" and not memories:
                # 当段无新增记忆：daily 仍生成（可注明无新增），every_n 跳过并记 diary.skip
                return DiaryPrompt(scope_key, "", False, today, turn_range, "every_n_no_memory")
            prompt = self._render_prompt(scope_key, memories, turn_range)
            return DiaryPrompt(scope_key, prompt, True, today, turn_range, reason)
        except Exception as exc:
            log.warning("diary.prepare failed: %r", exc)
            _emit(
                self.metrics,
                "diary.fail",
                scope=str(scope or ""),
                reason="prepare",
                exc_type=type(exc).__name__,
            )
            return DiaryPrompt(str(scope or ""), "", False, "", (), "error")

    def _plan(
        self,
        scope: str,
        current: datetime,
        *,
        force: bool,
    ) -> tuple[str, datetime | None, bool] | None:
        """返回 (reason, 当段起点, 起点是否包含)；None = 未触发。

        窗口口径（§8.2）：
        - daily：本地当日 00:00 起（包含）；
        - every_n：该 scope 上次成功日记 created_at 之后（不包含）；
        - force：上次成功日记之后；没有则全部 turn 日志；
        - both：daily 优先（当日已有日记时 daily 不触发，自然落到 every_n）。
        """
        if force:
            return "force", self.store.last_created_at(scope), False
        trigger = str(self.cfg.diary.trigger or "").strip().lower()
        if trigger in ("daily", "both") and self._daily_due(scope, current):
            return "daily", _local_day_start(current, self.cfg.diary), True
        if trigger in ("every_n", "both") and self._every_n_due(scope, current):
            return "every_n", self.store.last_created_at(scope), False
        return None

    def _window_memories(
        self,
        scope: str,
        start: datetime | None,
        current: datetime,
        inclusive_start: bool,
    ) -> list[MemoryItem]:
        """当段新增记忆：scope 过滤 + 时间窗过滤（只读 memory_items 回调）。"""
        if self._memory_items is None:
            return []
        try:
            items = list(self._memory_items())
        except Exception as exc:
            log.warning("diary.memory_items failed: %r", exc)
            return []
        selected: list[MemoryItem] = []
        for item in items:
            try:
                if str(getattr(item, "scope", "")) != scope:
                    continue
                created = _parse_ts(str(getattr(item, "created_at", "")))
            except (ValueError, TypeError):
                continue
            if start is not None:
                if inclusive_start and created < start:
                    continue
                if not inclusive_start and created <= start:
                    continue
            if created > current:
                continue
            selected.append(item)
        selected.sort(key=lambda item: _parse_ts(str(item.created_at)))
        return selected[-_MAX_MEMORY_IN_PROMPT:]

    def _render_prompt(
        self,
        scope: str,
        memories: Sequence[MemoryItem],
        turn_range: Sequence[str],
    ) -> str:
        """§8.2 生成 prompt 模板 + 当段新增记忆 / state / user_affect / users 快照。"""
        dcfg = self.cfg.diary
        label = _SCOPE_LABEL.get(scope, scope)
        scope_text = label + "（" + scope + "）"
        max_tokens = max(1, int(dcfg.max_tokens))
        approx_chars = max(1, int(round(max_tokens * 280.0 / 300.0)))
        lines: list[str] = [
            "你是 bot。回顾这段时间发生在" + scope_text + "语境里的事和你的感受，用第一人称写一段话。",
            "- 不超过 " + str(max_tokens) + " tokens（约 " + str(approx_chars) + " 字）。",
            "- 只写你自己的视角和感受，不要复述事实细节。",
            '- 不要点名任何具体的人；群聊事件只写"群里有人提到…"，私聊事件只写"私下里…"。',
            "- 只写本语境（" + scope_text + "）内的事件，不要跨语境混合。",
        ]
        lines.append("")
        lines.append("【当段新增记忆】")
        if memories:
            for item in memories:
                content = sanitize_for_prompt(
                    _clip_text(str(getattr(item, "content", "")), _MAX_MEMORY_CHARS)
                )
                if not content:
                    continue
                item_type = str(getattr(item, "type", "") or "")
                lines.append("- [" + item_type + "] " + content)
        else:
            lines.append("- 本段没有新增记忆，可以只写这段时间的感受，不要编造具体事件。")

        # bot 当前情绪：state.stale 时按中性生成，不阻断日记生成（§3.2）
        state = self._load_state()
        stale = True
        valence = 0.0
        avoidance = 0.0
        if state is not None:
            stale = bool(getattr(state, "stale", True))
            valence = _safe_float(getattr(state, "valence", 0.0), 0.0)
            avoidance = _safe_float(getattr(state, "avoidance", 0.0), 0.0)
        if stale:
            valence = 0.0
            avoidance = 0.0
        lines.append("")
        lines.append("【bot 当前情绪】（只用于体会语气，不要直接报数字）")
        emotion_line = "valence=" + format(valence, "+.2f") + " avoidance=" + format(avoidance, ".2f")
        if stale:
            emotion_line += "（state 已过期，按中性书写，不要编造情绪）"
        lines.append("- " + emotion_line)

        affect_rows = self._affect_rows()
        if affect_rows:
            lines.append("")
            lines.append("【用户感情快照】")
            lines.extend(affect_rows)

        users_rows = self._users_rows()
        if users_rows:
            lines.append("")
            lines.append("【昵称快照】（只用于理解谁在说话，日记里不要点名）")
            lines.append("- " + "、".join(users_rows))

        if turn_range:
            lines.append("")
            lines.append("【当段轮次】" + " ~ ".join(str(item) for item in turn_range))
        lines.append("")
        lines.append("现在请写这篇日记（只输出日记正文，不要标题、不要解释）。")
        return "\n".join(lines)

    # ---------- 只读快照（异常全部降级为空） ----------

    def _load_state(self) -> Any:
        if self._state_loader is None:
            return None
        try:
            return self._state_loader()
        except Exception as exc:
            log.warning("diary.state_loader failed: %r", exc)
            return None

    def _affect_rows(self) -> list[str]:
        if self._affect_snapshot is None:
            return []
        try:
            snapshot = self._affect_snapshot()
        except Exception as exc:
            log.warning("diary.affect_snapshot failed: %r", exc)
            return []
        if not isinstance(snapshot, Mapping):
            return []
        records: list[tuple[str, Mapping[str, Any]]] = []
        for user_id, record in snapshot.items():
            if isinstance(record, Mapping):
                records.append((str(user_id), record))
        records.sort(
            key=lambda pair: (-_safe_float(pair[1].get("familiarity"), 0.3), pair[0]),
        )
        rows: list[str] = []
        for user_id, record in records[:_MAX_AFFECT_IN_PROMPT]:
            nickname = sanitize_for_prompt(self._nickname(user_id))
            rows.append(
                "- "
                + nickname
                + ": 好感="
                + format(_safe_float(record.get("affection"), 0.5), ".2f")
                + " 厌恶="
                + format(_safe_float(record.get("annoyance"), 0.0), ".2f")
                + " 熟悉="
                + format(_safe_float(record.get("familiarity"), 0.3), ".2f")
            )
        return rows

    def _users_rows(self) -> list[str]:
        if self._users is None:
            return []
        try:
            snapshot = self._users.snapshot()
        except Exception as exc:
            log.warning("diary.users snapshot failed: %r", exc)
            return []
        if not isinstance(snapshot, Mapping):
            return []
        bot_nickname = str(self.cfg.diary.bot_nickname or "").strip()
        nicknames: list[str] = []
        for value in snapshot.values():
            nickname = str(value or "").strip()
            if not nickname or nickname == bot_nickname or nickname in nicknames:
                continue
            nicknames.append(nickname)
        nicknames.sort()
        return [sanitize_for_prompt(item) for item in nicknames[:_MAX_USERS_IN_PROMPT]]

    def _nickname(self, user_id: str) -> str:
        if self._users is not None:
            try:
                return str(self._users.nickname(user_id))
            except Exception:
                pass
        text = str(user_id or "")
        return "用户" + text[-4:] if text else text

    def _known_user_ids(self) -> list[str]:
        """隐私护栏用：users / user_affect 快照里的 user_id 字面量（长度 ≥3 才比对）。"""
        ids: set[str] = set()
        if self._users is not None:
            try:
                snapshot = self._users.snapshot()
            except Exception:
                snapshot = None
            if isinstance(snapshot, Mapping):
                ids.update(str(key) for key in snapshot.keys())
        if self._affect_snapshot is not None:
            try:
                snapshot = self._affect_snapshot()
            except Exception:
                snapshot = None
            if isinstance(snapshot, Mapping):
                ids.update(str(key) for key in snapshot.keys())
        return sorted((item for item in ids if len(item) >= 3), key=len, reverse=True)

    # ---------- 隐私护栏（生成后、落盘前；§8.2 / D7.4） ----------

    def _apply_privacy(self, text: str) -> tuple[str, dict[str, Any]] | None:
        """返回 (脱敏后文本, 命中统计)；None = 拒写。调用方负责记 diary.privacy。"""
        # 1) 哨兵 / 三反引号围栏：无论 privacy_check 开关都拒绝落盘（注入防护）
        if sanitize_for_prompt(text) != text:
            return None
        # 2) user_id 字面量：可识别他人身份，拒写（与 privacy_check 无关）。
        #    先查 users / user_affect 快照里的真实 id，再兜底匹配规范 §3.1 的 u_xxxx 形态，
        #    避免「快照里暂时没有该用户」时把 user_id 泄漏进日记。
        for user_id in self._known_user_ids():
            if user_id in text:
                return None
        if _USER_ID_LITERAL_RE.search(text):
            return None
        stats: dict[str, Any] = {"replaced_other": 0, "replaced_bot": False}
        if not self.cfg.diary.privacy_check:
            # privacy_check=False：跳过昵称替换，但仍禁止哨兵 / 围栏 / user_id 落盘
            return text, stats
        bot_nickname = str(self.cfg.diary.bot_nickname or "").strip()
        other_nicknames: set[str] = set()
        if self._users is not None:
            try:
                snapshot = self._users.snapshot()
            except Exception:
                snapshot = {}
            if isinstance(snapshot, Mapping):
                for value in snapshot.values():
                    nickname = str(value or "").strip()
                    if nickname and nickname != bot_nickname:
                        other_nicknames.add(nickname)
        # 最长优先：避免「小明」先替换后破坏「小明子」→ 有人子
        for nickname in sorted(other_nicknames, key=len, reverse=True):
            count = text.count(nickname)
            if count:
                text = text.replace(nickname, "有人")
                stats["replaced_other"] += count
        if bot_nickname:
            count = text.count(bot_nickname)
            if count:
                text = text.replace(bot_nickname, "我")
                stats["replaced_bot"] = True
        return text, stats

    # ---------- finish：隐私 → 截断 → cap → 幂等 → 落盘 ----------

    def finish(
        self,
        prepared: DiaryPrompt,
        raw: Any,
        *,
        now: datetime | None = None,
    ) -> DiaryRunReport:
        """把生成结果落盘；任何异常都降级为 fail 报告，绝不抛给调用方。"""
        scope = str(getattr(prepared, "scope_tag", "") or "")
        try:
            if (
                prepared is None
                or not isinstance(prepared, DiaryPrompt)
                or not prepared.due
                or not prepared.prompt
            ):
                return DiaryRunReport(scope, "not_due", "not_due")
            scope_key = _normalize_scope(prepared.scope_tag)
            if scope_key is None:
                # mixed / 非法 scope 绝不写 diary.jsonl（§8.5 / D10）
                return DiaryRunReport(scope, "skip", "invalid_scope")
            current = self._now(now)
            dcfg = self.cfg.diary
            text = _clean_generated_text(
                raw if isinstance(raw, str) else ("" if raw is None else str(raw))
            )
            if not text:
                _emit(
                    self.metrics,
                    "diary.fail",
                    scope=scope_key,
                    reason="empty",
                    date=str(getattr(prepared, "date", "") or ""),
                )
                return DiaryRunReport(scope_key, "fail", "empty")

            # 1) 隐私护栏：生成后 → 落盘前
            privacy = self._apply_privacy(text)
            if privacy is None:
                # 命中 user_id / 哨兵 / 围栏：拒写；字段只记布尔 / scope / 日期
                _emit(
                    self.metrics,
                    "diary.privacy",
                    scope=scope_key,
                    date=str(getattr(prepared, "date", "") or ""),
                    rejected=True,
                )
                return DiaryRunReport(scope_key, "privacy", "privacy")
            text, privacy_stats = privacy
            if privacy_stats.get("replaced_other") or privacy_stats.get("replaced_bot"):
                _emit(
                    self.metrics,
                    "diary.privacy",
                    scope=scope_key,
                    date=str(getattr(prepared, "date", "") or ""),
                    replaced_other=int(privacy_stats.get("replaced_other") or 0),
                    replaced_bot=bool(privacy_stats.get("replaced_bot")),
                )

            # 2) 截断：脱敏后仍超 max_tokens → 截断并记 diary.truncated
            max_tokens = max(1, int(dcfg.max_tokens))
            tokens = estimate_tokens(text)
            if tokens > max_tokens:
                text = _truncate_tokens(text, max_tokens)
                tokens = estimate_tokens(text)
                if not text:
                    _emit(self.metrics, "diary.fail", scope=scope_key, reason="empty_after_truncate")
                    return DiaryRunReport(scope_key, "fail", "empty")
                _emit(
                    self.metrics,
                    "diary.truncated",
                    scope=scope_key,
                    tokens=tokens,
                    date=str(getattr(prepared, "date", "") or ""),
                )

            # 3) cap：落盘前检查窗口内同 scope 篇数（daily / every_n 共用，§8.3）
            if self.store.active_count(scope_key, current) >= max(0, int(dcfg.max_per_scope)):
                _emit(
                    self.metrics,
                    "diary.cap_hit",
                    scope=scope_key,
                    date=str(getattr(prepared, "date", "") or ""),
                )
                return DiaryRunReport(scope_key, "cap_hit", "cap")

            # 4) 幂等：同 (date, scope_tag, turn_range)；daily 另加「当日同 scope 已有日记」防重
            if self._is_duplicate(prepared, scope_key):
                _emit(
                    self.metrics,
                    "diary.dup",
                    scope=scope_key,
                    date=str(getattr(prepared, "date", "") or ""),
                )
                return DiaryRunReport(scope_key, "dup", "idempotent")

            # 5) 落盘：created_at 用日记时区的当前时间（带偏移，§3）
            date_text = str(getattr(prepared, "date", "") or "")
            try:
                date_text = _validate_date_text(date_text) if date_text else _local_date_str(current, dcfg)
            except ValueError:
                date_text = _local_date_str(current, dcfg)
            turn_range = tuple(str(item) for item in (getattr(prepared, "turn_range", ()) or ()))
            created_at = current.astimezone(_resolve_tz(dcfg.timezone)).isoformat(timespec="seconds")
            entry = DiaryEntry(
                id=self.store.next_id(),
                date=date_text,
                scope_tag=scope_key,
                content=text,
                tokens=tokens,
                turn_range=turn_range,
                created_at=created_at,
            )
            try:
                self.store.append(entry)
            except Exception as exc:  # 写盘失败 = 本篇不写，回答照常（硬规则 19）
                log.warning("diary write failed: %r", exc)
                _emit(
                    self.metrics,
                    "diary.fail",
                    scope=scope_key,
                    reason="write",
                    exc_type=type(exc).__name__,
                )
                return DiaryRunReport(scope_key, "fail", "write", None, tokens)
            _emit(
                self.metrics,
                "diary.ok",
                scope=scope_key,
                date=date_text,
                tokens=tokens,
                chars=len(text),
            )
            return DiaryRunReport(
                scope_tag=scope_key,
                status="ok",
                reason=str(getattr(prepared, "reason", "") or ""),
                entry_id=entry.id,
                tokens=tokens,
            )
        except Exception as exc:  # 兜底：任何异常都不外抛（§8.6 / 硬规则 19）
            log.warning("diary.finish failed: %r", exc)
            _emit(
                self.metrics,
                "diary.fail",
                scope=scope,
                reason="exception",
                exc_type=type(exc).__name__,
            )
            return DiaryRunReport(scope, "fail", "exception")

    def _is_duplicate(self, prepared: DiaryPrompt, scope: str) -> bool:
        """幂等判定（D06）。

        - 通用键：(date, scope_tag, turn_range) 完全相同 → dup；
        - daily 防重补充：reason=daily 且当日同 scope 已有日记 → dup（避免同日二次 daily，
          例如首次落盘成功后调用方重试 finish）。
        """
        date_text = str(getattr(prepared, "date", "") or "")
        if not date_text:
            return False
        if str(getattr(prepared, "reason", "") or "") == "daily" and self._has_daily(scope, date_text):
            return True
        return self.store.has_idempotent(date_text, scope, getattr(prepared, "turn_range", ()))

    # ---------- generate / run_due ----------

    def generate(
        self,
        scope: str,
        llm: Callable[[str], Any] | None,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> DiaryRunReport:
        """prepare + llm(prompt)（失败重试 ≤1）+ finish；异常一律降级。"""
        scope_text = str(scope or "")
        try:
            scope_key = _normalize_scope(scope_text)
            if scope_key is None:
                return DiaryRunReport(scope_text, "skip", "invalid_scope")
            if not self.cfg.diary.enabled:
                return DiaryRunReport(scope_key, "disabled", "disabled")
            prepared = self.prepare(scope_key, now=now, force=force)
            if not prepared.due or not prepared.prompt:
                reason = str(prepared.reason or "not_due")
                if reason == "every_n_no_memory":
                    # every_n 当段无新增记忆：跳过生成，记 diary.skip（§8.2）
                    _emit(
                        self.metrics,
                        "diary.skip",
                        scope=scope_key,
                        date=prepared.date,
                        reason="no_new_memory",
                    )
                    return DiaryRunReport(scope_key, "skip", "no_new_memory")
                if reason == "error":
                    return DiaryRunReport(scope_key, "fail", "prepare")
                return DiaryRunReport(scope_key, "not_due", reason)
            raw = self._call_llm(llm, prepared.prompt)
            if raw is _LLM_FAILED:
                return DiaryRunReport(scope_key, "fail", "llm")
            return self.finish(prepared, raw, now=now)
        except Exception as exc:
            log.warning("diary.generate failed: %r", exc)
            _emit(
                self.metrics,
                "diary.fail",
                scope=scope_text,
                reason="generate",
                exc_type=type(exc).__name__,
            )
            return DiaryRunReport(scope_text, "fail", "exception")

    def _call_llm(self, llm: Callable[[str], Any] | None, prompt: str) -> Any:
        """调用宿主注入的 LLM；失败重试 ≤1（最多 2 次），最终失败记 diary.fail。"""
        if not callable(llm):
            _emit(self.metrics, "diary.fail", reason="llm_missing")
            return _LLM_FAILED
        last_exc: BaseException | None = None
        for attempt in (1, 2):
            try:
                return llm(prompt)
            except Exception as exc:
                last_exc = exc
                log.warning("diary.llm failed attempt=%s exc=%s", attempt, type(exc).__name__)
        _emit(
            self.metrics,
            "diary.fail",
            reason="llm",
            attempts=2,
            exc_type=type(last_exc).__name__ if last_exc is not None else None,
        )
        return _LLM_FAILED

    def run_due(
        self,
        llm: Callable[[str], Any] | None,
        *,
        now: datetime | None = None,
    ) -> list[DiaryRunReport]:
        """仅触发 due 的 scope：group / dm 各一篇，互不混合（§8.2 / D10）。"""
        reports: list[DiaryRunReport] = []
        try:
            if not self.cfg.diary.enabled:
                return reports
            for scope in DIARY_SCOPES:
                try:
                    if not self.due(scope, now):
                        continue
                    reports.append(self.generate(scope, llm, now=now))
                except Exception as exc:
                    log.warning("diary.run_due scope=%s failed: %r", scope, exc)
                    _emit(
                        self.metrics,
                        "diary.fail",
                        scope=scope,
                        reason="run_due",
                        exc_type=type(exc).__name__,
                    )
                    reports.append(DiaryRunReport(scope, "fail", "exception"))
        except Exception as exc:  # pragma: no cover - 兜底
            log.warning("diary.run_due failed: %r", exc)
            _emit(self.metrics, "diary.fail", reason="run_due", exc_type=type(exc).__name__)
        return reports


# --------------------------------------------------------------------------
# 注入：select_excerpts / render_diary_block（§8.4）
# --------------------------------------------------------------------------

def select_excerpts(
    store: DiaryStore,
    scope_tag: str,
    *,
    now: datetime | None = None,
    budget: int | None = None,
    count: int | None = None,
    metrics: Metrics | None = None,
) -> dict:
    """选本轮注入的日记节选（§8.4 / D12）。

    - 只按 scope_tag 过滤（group / dm；mixed 返回空）；
    - active() 倒序取前 count（默认 cfg.diary.inject_count）；
    - 第 1 篇必装；第 2+ 篇仅在剩余预算内整篇装，放不下则不装并记 diary.truncated；
    - 返回 {"excerpts", "tokens", "injected", "truncated"}；任何异常返回空结果。
    """
    result: dict[str, Any] = {"excerpts": [], "tokens": 0, "injected": 0, "truncated": False}
    scope_key = _normalize_scope(scope_tag)
    if scope_key is None:
        return result
    try:
        dcfg = store.cfg.diary
        budget_value = int(dcfg.inject_budget) if budget is None else int(budget)
        count_value = int(dcfg.inject_count) if count is None else int(count)
        current = _coerce_now(now, dcfg)
        active = store.active(scope_key, current)
        chosen: list[DiaryEntry] = []
        total = 0
        truncated = False
        limit = max(0, count_value)
        for index, entry in enumerate(active[:limit]):
            cost = estimate_tokens(entry.content)
            if index == 0:
                # 第 1 篇必装（不截断正文；日记单篇已在生成时 ≤ max_tokens）
                chosen.append(entry)
                total += cost
                continue
            if budget_value >= 0 and total + cost <= budget_value:
                chosen.append(entry)
                total += cost
            else:
                # 第 2 篇放不下：不装、不截断正文，记 diary.truncated
                truncated = True
                break
        result["excerpts"] = chosen
        result["tokens"] = total
        result["injected"] = len(chosen)
        result["truncated"] = truncated
        _emit(metrics, "diary.inject", scope=scope_key, count=len(chosen))
        _emit(metrics, "diary.tokens", scope=scope_key, tokens=total)
        if truncated:
            _emit(metrics, "diary.truncated", scope=scope_key, reason="inject")
        return result
    except Exception as exc:
        log.warning("diary.select_excerpts failed: %r", exc)
        _emit(metrics, "diary.fail", scope=scope_key, reason="inject", exc_type=type(exc).__name__)
        return result


def render_diary_block(excerpts: Any) -> str:
    """渲染日记块（§8.4）：每篇 "[日记 YYYY-MM-DD | 群聊/私聊]" + 换行 + content。

    篇间空行；不注入时返回空串。DIARY_PREFIX 由 prompt.build_prompt 添加，
    本函数不重复前缀。外部文本进 prompt 前过 sanitize_for_prompt（实现契约 §0.5）。
    """
    try:
        if excerpts is None:
            return ""
        if isinstance(excerpts, Mapping):
            excerpts = excerpts.get("excerpts") or []
        blocks: list[str] = []
        for entry in excerpts:
            if isinstance(entry, Mapping):
                date_text = str(entry.get("date") or "")
                scope_tag = str(entry.get("scope_tag") or "")
                content = str(entry.get("content") or "")
            else:
                date_text = str(getattr(entry, "date", "") or "")
                scope_tag = str(getattr(entry, "scope_tag", "") or "")
                content = str(getattr(entry, "content", "") or "")
            scope_key = _normalize_scope(scope_tag)
            if scope_key is None:
                continue
            content = sanitize_for_prompt(content.strip())
            if not content:
                continue
            blocks.append("[日记 " + date_text + " | " + _SCOPE_LABEL[scope_key] + "]\n" + content)
        return "\n\n".join(blocks)
    except Exception as exc:  # 渲染失败 = 不注入日记，不影响回答可用性
        log.warning("diary.render_diary_block failed: %r", exc)
        return ""
