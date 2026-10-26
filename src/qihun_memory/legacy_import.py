"""旧 Obsidian 记忆库 → v1.4 数据目录的一次性导入（docs/INTEGRATION.md §6）。

契约要点：
- 只读旧库（例如 D:/AI-Agent-Vault），绝不修改 vault 下任何文件，也不写 state.json；
- 直连 MemoryStore 追加 MemoryItem，保留旧文件日期（当天 20:00 +08:00）与旧强度 S，
  因此不能走 MemorySystem.remember()（它会用当前时间与 S0 重新计算，丢历史）；
- 幂等：turn_key = "legacy:<vault 目录名>:<相对 posix 路径>"，已存在则计入
  skipped_existing，同一文件永不重复写；
- 单文件损坏只计入 failed 并继续；--dry-run 只统计、不落盘。

映射（§6）：
- memory/episodes/*.md（兼容 episodes/*.md）→ type=episode
- clues/*.md（排除 index.md）→ type=fact，S=30
- memory/summaries/current_summary.md → type=fact，S=60 + RollingSummary.seed()
- persona/state.json → type=fact，S=60
- persona/core.md → data/persona.md（已存在且非 --force 则跳过）
- users.json ← owner 昵称
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .fsutil import atomic_write_text
from .memory import MemorySystem
from .metrics import Metrics
from .models import MAX_STABILITY, MemoryItem
from .state import UserDirectory
from .summary import RollingSummary, conversation_key
from .tokens import estimate_tokens

log = logging.getLogger("memory")

DEFAULT_OWNER_NICKNAME = "群记忆"
TZ8 = timezone(timedelta(hours=8))
MAX_CONTENT = 500
MAX_TAGS = 8
MAX_TAG_LEN = 16
SUMMARY_UNIT_LIMIT = 120
# 旧库摘要各段在导入时的优先级顺序（P1-9：超预算从尾部丢整段）
_SUMMARY_PRIORITY = ("一句话现状", "关系温度", "未了结", "最近话题", "近期事件", "最近教训")
FENCE = chr(96) * 3  # 代码围栏，避免源码里出现三反引号
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TABLE_SEP_RE = re.compile(r"^:?-{2,}:?$")
_CODE_FENCE_RE = re.compile(re.escape(FENCE) + r".*?(?:" + re.escape(FENCE) + r"|$)", re.S)

# 情绪词 → valence（§6：开心/乐/得意 +0.5~0.6，怒/哀 -0.4~-0.6，中性 0）
_EMOTION_VALENCE: dict[str, float] = {
    "非常开心": 0.7,
    "不开心": -0.6,
    "开心": 0.6,
    "高兴": 0.6,
    "喜悦": 0.6,
    "兴奋": 0.6,
    "激动": 0.6,
    "乐": 0.6,
    "生气": -0.6,
    "愤怒": -0.6,
    "怒": -0.6,
    "难过": -0.55,
    "伤心": -0.55,
    "悲伤": -0.55,
    "得意": 0.5,
    "骄傲": 0.5,
    "满足": 0.5,
    "感动": 0.5,
    "甜蜜": 0.5,
    "喜欢": 0.5,
    "委屈": -0.5,
    "失望": -0.5,
    "害怕": -0.5,
    "惧": -0.5,
    "讨厌": -0.5,
    "厌": -0.5,
    "哀": -0.5,
    "吃醋": -0.45,
    "焦虑": -0.45,
    "烦": -0.45,
    "孤独": -0.45,
    "温暖": 0.45,
    "期待": 0.4,
    "安心": 0.4,
    "撒娇": 0.4,
    "愧疚": -0.4,
    "担心": -0.4,
    "害羞": 0.35,
    "尴尬": -0.35,
    "中性": 0.0,
    "平静": 0.0,
    "平常": 0.0,
    "普通": 0.0,
    "无": 0.0,
}
_IMPORTANCE_FACTOR = {1: 1.0, 2: 1.3, 3: 1.6}
_STATE_NUM_FIELDS = (
    ("clinginess", "粘人"),
    ("tsundere", "傲娇"),
    ("intimacy", "亲密度"),
    ("rice_craving", "馋饭"),
)


class LegacyVaultError(RuntimeError):
    """vault 不存在 / 不可读等整体性错误（CLI 映射为 exit code 2）。"""


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------

def _collapse(text: Any) -> str:
    """压成单行：去代码围栏、哨兵串、控制字符，折叠空白。"""
    if not text:
        return ""
    cleaned = str(text)
    cleaned = _CODE_FENCE_RE.sub(" ", cleaned)
    cleaned = cleaned.replace(FENCE, " ")
    for sentinel in ("<MEMORY_JSON>", "</MEMORY_JSON>"):
        cleaned = cleaned.replace(sentinel, " ")
    chars: list[str] = []
    for ch in cleaned:
        code = ord(ch)
        if code < 32 and ch not in "\n\t":
            continue
        if code == 127 or 0x80 <= code <= 0x9F or 0xD800 <= code <= 0xDFFF:
            continue
        chars.append(ch)
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _clip(text: Any, limit: int = MAX_CONTENT) -> str:
    """单行 + 硬上限（§6 content ≤500 字）。"""
    return _collapse(text)[:limit].strip()


def _ensure_stop(text: str) -> str:
    text = _collapse(text)
    if not text:
        return ""
    if text[-1] in "。！？!?；;":
        return text
    return text + "。"


def _clean_tags(tags: Iterable[str]) -> list[str]:
    """tags ≤8 个、每个 ≤16 字（§6 / §8）。"""
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        text = _collapse(tag)[:MAX_TAG_LEN].strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= MAX_TAGS:
            break
    return result


def _split_long_piece(piece: str, limit: int) -> list[str]:
    """超长句按二级标点（逗号/顿号/冒号）再切，避免 hard cut 出「了。」碎片（P1-9c）。"""
    if len(piece) <= limit:
        return [piece]
    chunks: list[str] = []
    buffer = ""
    for part in re.split(r"(?<=[，,、：:])", piece):
        if len(buffer) + len(part) <= limit:
            buffer += part
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        buffer = part
        while len(buffer) > limit:  # 没有任何标点的超长串才硬切
            chunks.append(buffer[:limit])
            buffer = buffer[limit:]
    if buffer:
        chunks.append(buffer)
    return chunks


def _split_units(text: str, limit: int = SUMMARY_UNIT_LIMIT) -> list[str]:
    """按句号/分号切句并合并到 limit 内；单元顺序保持不变（P1-9c）。"""
    units: list[str] = []
    buffer = ""
    for piece in re.split(r"(?<=[。；！？;])", text):
        piece = piece.strip()
        if not piece:
            continue
        if len(buffer) + len(piece) <= limit:
            buffer += piece
            continue
        if buffer:
            units.append(buffer)
            buffer = ""
        parts = _split_long_piece(piece, limit)
        units.extend(parts[:-1])
        buffer = parts[-1]
    if buffer:
        units.append(buffer)
    return units


# --------------------------------------------------------------------------
# 解析辅助
# --------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """极简 YAML frontmatter 解析；任何坏数据退化为「无 frontmatter」，绝不抛异常。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for index in range(1, min(len(lines), 200)):
        if lines[index].strip() == "---":
            end = index
            break
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    pending: str | None = None  # 形如 "clues:" 的键，后续缩进 "- x" 行是它的列表值
    pending_items: list[str] = []

    def flush_list() -> None:
        nonlocal pending, pending_items
        if pending and pending_items:
            meta[pending] = "[" + ", ".join(pending_items) + "]"
        pending, pending_items = None, []

    for raw in lines[1:end]:
        item = re.match(r"^\s+-\s+(.+?)\s*$", raw)
        if pending is not None and item:
            pending_items.append(item.group(1).strip().strip('"').strip("'"))
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        name = key.strip().lower()
        if not name or name.startswith("#"):
            continue
        flush_list()
        text = value.strip().strip('"').strip("'")
        if text:
            meta[name] = text
        else:
            pending, pending_items = name, []
    flush_list()
    return meta, "\n".join(lines[end + 1:])


def _inline_list(value: Any) -> list[str]:
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return []
    items: list[str] = []
    for part in text.split(","):
        item = part.strip().strip('"').strip("'").strip()
        if item:
            items.append(item)
    return items


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _importance(value: Any) -> int:
    parsed = _int_value(value, 1)
    return parsed if parsed in (1, 2, 3) else 1


def _safe_date(year: Any, month: Any, day: Any) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _date_from_text(value: Any) -> date | None:
    if not value:
        return None
    match = _DATE_RE.search(str(value))
    if not match:
        return None
    return _safe_date(match.group(1), match.group(2), match.group(3))


def _filename_date(path: Path) -> date | None:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})-", path.name)
    if not match:
        return None
    return _safe_date(match.group(1), match.group(2), match.group(3))


def _mtime_date(path: Path) -> date:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone(TZ8).date()


def _created_at(day: date) -> str:
    """旧库日期 → 当天 20:00 +08:00 的 ISO 时间（带时区）。"""
    return datetime.combine(day, time(hour=20), tzinfo=TZ8).isoformat(timespec="seconds")


def _emotion_valence(emotion: Any) -> float:
    text = _collapse(emotion)
    if not text:
        return 0.0
    if text in _EMOTION_VALENCE:
        return _EMOTION_VALENCE[text]
    for keyword in sorted(_EMOTION_VALENCE, key=len, reverse=True):
        if keyword in text:
            return _EMOTION_VALENCE[keyword]
    return 0.0


def episode_stability(importance: int, valence: float) -> float:
    """episode S = min(90, 7*(1+0.5*|valence|)*{1:1.0,2:1.3,3:1.6})（§6）。"""
    factor = _IMPORTANCE_FACTOR.get(importance, 1.0)
    return min(MAX_STABILITY, 7.0 * (1.0 + 0.5 * abs(float(valence))) * factor)


def _section_map(body: str) -> dict[str, str]:
    """按 markdown 标题切段，返回 {标题: 正文单行}。"""
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING_RE.match(raw)
        if heading:
            current = _collapse(heading.group(1))
            sections.setdefault(current, [])
            continue
        text = _collapse(re.sub(r"^[-*+]\s+", "", line))
        sections.setdefault(current, []).append(text)
    return {key: " ".join(part for part in parts if part).strip() for key, parts in sections.items()}


def _find_section(sections: dict[str, str], *keywords: str) -> str:
    for keyword in keywords:
        for key, value in sections.items():
            if keyword in key and value:
                return value
    return ""


def _strip_headings(body: str) -> str:
    parts: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or _HEADING_RE.match(raw):
            continue
        line = re.sub(r"^>\s*", "", line)
        text = _collapse(line)
        if text:
            parts.append(text)
    return " ".join(parts)


# --------------------------------------------------------------------------
# 各类文件的映射
# --------------------------------------------------------------------------

def _episode_title(meta: dict[str, str], body: str, path: Path) -> str:
    title = _collapse(meta.get("title") or "")
    if title:
        return title
    for raw in body.splitlines():
        match = _HEADING_RE.match(raw)
        if match:
            candidate = _collapse(match.group(1))
            if candidate:
                return candidate
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)
    return _collapse(stem) or path.stem


def _episode_content(day: date, title: str, body: str) -> str:
    sections = _section_map(body)
    happened = _find_section(sections, "发生了什么", "经过", "事件")
    remember = _find_section(sections, "以后要记得", "要记得", "以后记得", "教训")
    if not happened:
        happened = _strip_headings(body)
    if not happened and not remember:
        return ""
    parts = [f"{day.isoformat()} 事件：{_ensure_stop(title)}"]
    if happened:
        parts.append("发生了什么：" + _ensure_stop(happened))
    if remember:
        parts.append("以后要记得：" + _ensure_stop(remember))
    return _clip("".join(parts))


def _clue_phrase(body: str) -> str:
    sections = _section_map(body)
    for key, value in sections.items():
        if "一句话" in key and value:
            return value
    return ""


def _clue_confirmed(body: str) -> str:
    """取「已确认线索」表格里的线索列，压成一行。"""
    rows: list[str] = []
    in_section = False
    for raw in body.splitlines():
        heading = _HEADING_RE.match(raw)
        if heading:
            in_section = "已确认" in _collapse(heading.group(1))
            continue
        if not in_section:
            continue
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [_collapse(cell) for cell in line.strip("|").split("|")]
        cells = [cell for cell in cells if cell]
        if not cells:
            continue
        if all(_TABLE_SEP_RE.match(cell) for cell in cells):
            continue
        if any(cell in ("#", "线索") for cell in cells) or any("种子" in cell for cell in cells):
            continue
        if len(cells) >= 2 and re.fullmatch(r"[\d#\-. ]+", cells[0]):
            clue = cells[1]
        elif len(cells) >= 2 and cells[0] not in ("—", "-"):
            clue = cells[0]
        elif len(cells) >= 2:
            clue = cells[1]
        else:
            clue = cells[0]
        clue = re.sub(r"^[\d#.\-]+\s*", "", clue).strip()
        if clue and clue not in rows:
            rows.append(clue)
        if len(rows) >= 6:
            break
    return "；".join(rows)


def _clue_content(slug: str, title: str, phrase: str, confirmed: str) -> str:
    lead = phrase or title or slug
    if title and phrase and title not in phrase:
        lead = title + "——" + phrase
    content = f"线索 {_collapse(slug)}：{_ensure_stop(_collapse(lead))}"
    if confirmed:
        content += "已确认：" + _ensure_stop(confirmed)
    return _clip(content)


def _summary_path(vault: Path) -> Path | None:
    primary = vault / "memory" / "summaries" / "current_summary.md"
    if primary.is_file():
        return primary
    # 兼容旧布局；模板空摘要会在 _is_empty_summary 里被识别并跳过
    fallback = vault / "persona" / "current_summary.md"
    if fallback.is_file():
        return fallback
    return None


def _is_empty_summary(meta: dict[str, str], body: str) -> bool:
    """模板空摘要：rounds_covered=0 且正文含「刚搬进记忆库」/大量「（空」。"""
    if _int_value(meta.get("rounds_covered"), 0) > 0:
        return False
    text = _collapse(body)
    if "刚搬进记忆库" in text:
        return True
    return text.count("（空") >= 2 or text.count("(空") >= 2


def _summary_content(day: date, body: str) -> str:
    text = _strip_headings(body)
    if not text:
        return ""
    return _clip(f"旧库滚动摘要（截至{day.isoformat()}）：{_ensure_stop(text)}")


def _summary_sections(body: str) -> list[tuple[str, str]]:
    """把旧库摘要切成 (段名, 正文) 列表，兼容 ## 标题与【段名】行内标记。"""
    sections: list[tuple[str, list[str]]] = [("", [])]  # 第一个标题前的内容也保留
    current: list[str] = sections[0][1]
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("---"):
            continue
        heading = _HEADING_RE.match(raw)
        inline = re.match(r"^【([^】]+)】(.*)$", line)
        if heading:
            current = []
            sections.append((_collapse(heading.group(1)), current))
            continue
        if inline:
            current = []
            sections.append((_collapse(inline.group(1)), current))
            line = inline.group(2)
        line = re.sub(r"^>\s*", "", line)
        text = _collapse(line)
        if text and current is not None:
            current.append(text)
    return [(label, " ".join(parts).strip()) for label, parts in sections]


def _summary_units(day: date, body: str, *, max_tokens: int | None = None) -> list[str]:
    """按 P1-9 的优先级顺序生成单元：一句话现状 → 关系温度 → 未了结 →
    最近话题 → 近期事件 → 最近教训。

    max_tokens 给出时按**整段**从尾部丢弃：低优先级段一旦放不下，整段和它之后的
    段全部丢掉，不做段内截断（P1-9d）。
    """
    sections = _summary_sections(body)
    ordered: list[str] = []
    used: set[int] = set()
    for label in _SUMMARY_PRIORITY:
        for index, (section_label, text) in enumerate(sections):
            if index in used or not text:
                continue
            if label in section_label:
                ordered.append(text)
                used.add(index)
    for index, (label, text) in enumerate(sections):
        if index in used or not text:
            continue
        if "滚动摘要" in label:  # 文档大标题不算内容
            continue
        ordered.append(text)
    segments = [_split_units(text) for text in ordered]
    segments = [segment for segment in segments if segment]
    units = [f"旧库滚动摘要（截至{day.isoformat()}）"]
    if max_tokens is None:
        for segment in segments:
            units.extend(segment)
        return units
    for segment in segments:
        candidate = units + segment
        if estimate_tokens("\n".join(candidate)) > max_tokens:
            break
        units = candidate
    return units


def _number_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return "0"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(number):
        return "0"
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _state_content(day: date, data: dict[str, Any]) -> str:
    stage = _collapse(data.get("relationship_stage")) or "未知"
    numbers = "/".join(f"{label}{_number_text(data.get(key))}" for key, label in _STATE_NUM_FIELDS)
    return _clip(f"旧库关系状态：阶段{stage}，{numbers}（截至{day.isoformat()}）")


# --------------------------------------------------------------------------
# 文件发现与 turn_key
# --------------------------------------------------------------------------

def _episode_files(vault: Path) -> list[Path]:
    """扫描 episodes/*.md（真实库在 memory/episodes/，兼容根目录）。

    只排除下划线开头的模板/草稿（_模板.md 等）与非 .md 文件；
    不按文件名丢弃事件——日期回退链：文件名 → frontmatter date → mtime（P1-2）。
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for base in (vault / "memory" / "episodes", vault / "episodes"):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            if path.name.startswith("_"):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


def _clue_files(vault: Path) -> list[Path]:
    base = vault / "clues"
    if not base.is_dir():
        return []
    return [
        path
        for path in sorted(base.glob("*.md"))
        if path.name.lower() != "index.md" and not path.name.startswith("_")
    ]


def _turn_key(vault: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(vault.resolve())
    except ValueError:
        relative = Path(path.name)
    return f"legacy:{vault.name}:{relative.as_posix()}"


# --------------------------------------------------------------------------
# 导入器
# --------------------------------------------------------------------------

class _LegacyImporter:
    def __init__(
        self,
        cfg: Config,
        vault: Path,
        *,
        owner_id: str,
        owner_nickname: str,
        scope: str,
        dry_run: bool,
        force_persona: bool,
    ) -> None:
        self.cfg = cfg
        self.vault = vault
        self.owner_id = owner_id
        self.owner_nickname = owner_nickname
        self.scope = scope
        self.dry_run = bool(dry_run)
        self.force_persona = bool(force_persona)
        self.report: dict[str, Any] = {
            "vault": str(vault.resolve()),
            "dry_run": self.dry_run,
            "episodes": 0,
            "clues": 0,
            "summary": 0,
            "summary_seeded": False,  # 目标会话已有实时摘要时不覆盖（P1-9a）
            "state": 0,
            "persona": 0,
            "users": 0,
            "skipped_existing": 0,
            "failed": 0,
            "written": 0,
        }
        # 只读模式：不建目录、不写 memory.bad.jsonl；metrics 关闭，--dry-run 零落盘（P1-1）。
        self.system = MemorySystem(cfg, metrics=Metrics(None, enabled=False), read_only=self.dry_run)

    # ---------- 入口 ----------

    def run(self) -> dict[str, Any]:
        self._import_episodes()
        self._import_clues()
        self._import_summary()
        self._import_state()
        self._import_persona()
        self._import_users()
        return dict(self.report)

    # ---------- 通用 ----------

    def _mark_skipped(self, turn_key: str) -> bool:
        if self.system.store.has_turn_key(turn_key):
            self.report["skipped_existing"] += 1
            return True
        return False

    def _record_failure(self, path: Path, exc: Exception) -> None:
        self.report["failed"] += 1
        log.warning("legacy import failed for %s: %r", path, exc)

    def _append_item(
        self,
        *,
        item_type: str,
        content: str,
        tags: Iterable[str],
        created_at: str,
        stability: float,
        valence: float,
        mention_count: int,
        turn_key: str,
    ) -> None:
        clipped = _clip(content)
        if not clipped:
            raise ValueError("content empty after sanitize")
        if self.dry_run:
            self.report["written"] += 1
            return
        item = MemoryItem(
            id=self.system.store.next_id(),
            user_id=self.owner_id,
            scope=self.scope,
            type=item_type,
            content=clipped,
            created_at=created_at,
            last_access=created_at,
            access_count=0,
            mention_count=max(1, int(mention_count)),
            S=min(MAX_STABILITY, max(0.01, float(stability))),
            valence=max(-1.0, min(1.0, float(valence))),
            status="active",
            tags=_clean_tags(tags),
            turn_key=turn_key,
        )
        self.system.store.append(item)
        self.system.store.register_turn_key(turn_key, item.id)
        self.report["written"] += 1

    # ---------- episodes ----------

    def _import_episodes(self) -> None:
        for path in _episode_files(self.vault):
            try:
                self._import_episode(path)
            except Exception as exc:  # 单文件损坏不影响整次导入（§6）
                self._record_failure(path, exc)

    def _import_episode(self, path: Path) -> None:
        turn_key = _turn_key(self.vault, path)
        if self._mark_skipped(turn_key):
            return
        meta, body = _parse_frontmatter(_read_text(path))
        if not _collapse(body):
            raise ValueError("episode 正文为空")
        day = _filename_date(path) or _date_from_text(meta.get("date")) or _mtime_date(path)
        title = _episode_title(meta, body, path)
        importance = _importance(meta.get("importance"))
        valence = _emotion_valence(meta.get("emotion"))
        content = _episode_content(day, title, body)
        self._append_item(
            item_type="episode",
            content=content,
            # 重要度只进 tags/S，不放大 mention_count（P2-6）
            tags=["legacy", f"importance:{importance}", *_inline_list(meta.get("clues"))],
            created_at=_created_at(day),
            stability=episode_stability(importance, valence),
            valence=valence,
            mention_count=1,
            turn_key=turn_key,
        )
        self.report["episodes"] += 1

    # ---------- clues ----------

    def _import_clues(self) -> None:
        for path in _clue_files(self.vault):
            try:
                self._import_clue(path)
            except Exception as exc:
                self._record_failure(path, exc)

    def _import_clue(self, path: Path) -> None:
        turn_key = _turn_key(self.vault, path)
        if self._mark_skipped(turn_key):
            return
        meta, body = _parse_frontmatter(_read_text(path))
        slug = _collapse(meta.get("slug") or "") or path.stem
        title = _collapse(meta.get("title") or "")
        content = _clue_content(slug, title, _clue_phrase(body), _clue_confirmed(body))
        day = _date_from_text(meta.get("updated_at")) or _mtime_date(path)
        self._append_item(
            item_type="fact",
            content=content,
            tags=["legacy", slug],
            created_at=_created_at(day),
            stability=30.0,
            valence=0.0,
            mention_count=1,
            turn_key=turn_key,
        )
        self.report["clues"] += 1

    # ---------- 滚动摘要 ----------

    def _import_summary(self) -> None:
        path = _summary_path(self.vault)
        if path is None:
            return
        try:
            self._import_summary_file(path)
        except Exception as exc:
            self._record_failure(path, exc)

    def _import_summary_file(self, path: Path) -> None:
        turn_key = _turn_key(self.vault, path)
        if self._mark_skipped(turn_key):
            return
        meta, body = _parse_frontmatter(_read_text(path))
        if _is_empty_summary(meta, body):
            log.debug("legacy summary is empty template, skipped: %s", path)
            return
        day = _date_from_text(meta.get("updated_at")) or _mtime_date(path)
        content = _summary_content(day, body)
        if not content:
            raise ValueError("滚动摘要正文为空")
        key = conversation_key(self.scope, self.owner_id)
        summary = RollingSummary(self.cfg.summary_file, self.cfg.prompt)
        if summary.get(key).strip():
            # 目标会话已有实时滚动摘要：不得覆盖，只导入记忆 fact（P1-9a）
            log.info("legacy summary not seeded: conversation %r already has live summary", key)
        else:
            if not self.dry_run:
                units = _summary_units(day, body, max_tokens=self.cfg.prompt.summary_tokens)
                summary.seed(key, units)
            self.report["summary_seeded"] = True
        self._append_item(
            item_type="fact",
            content=content,
            tags=["legacy", "summary"],
            created_at=_created_at(day),
            stability=60.0,
            valence=0.0,
            mention_count=1,
            turn_key=turn_key,
        )
        self.report["summary"] += 1

    # ---------- persona/state.json ----------

    def _import_state(self) -> None:
        path = self.vault / "persona" / "state.json"
        if not path.is_file():
            return
        try:
            self._import_state_file(path)
        except Exception as exc:
            self._record_failure(path, exc)

    def _import_state_file(self, path: Path) -> None:
        turn_key = _turn_key(self.vault, path)
        if self._mark_skipped(turn_key):
            return
        try:
            data = json.loads(_read_text(path))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"state.json 不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("state.json 不是 JSON 对象")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        day = _date_from_text((meta or {}).get("updated_at")) or _mtime_date(path)
        self._append_item(
            item_type="fact",
            content=_state_content(day, data),
            tags=["legacy", "state"],
            created_at=_created_at(day),
            stability=60.0,
            valence=0.0,
            mention_count=1,
            turn_key=turn_key,
        )
        self.report["state"] += 1

    # ---------- persona/core.md ----------

    def _import_persona(self) -> None:
        source = self.vault / "persona" / "core.md"
        if not source.is_file():
            return
        target = self.cfg.data_path / "persona.md"
        if target.exists() and not self.force_persona:
            self.report["skipped_existing"] += 1
            return
        try:
            text = _read_text(source)
            if not text.strip():
                raise ValueError("persona/core.md 为空")
            if not self.dry_run:
                atomic_write_text(target, text)
            self.report["persona"] += 1
        except Exception as exc:
            self._record_failure(source, exc)

    # ---------- users.json ----------

    def _import_users(self) -> None:
        if not self.owner_id or not self.owner_nickname:
            return
        try:
            directory = UserDirectory(self.cfg.users_file)
            if directory.snapshot().get(self.owner_id) == self.owner_nickname:
                return
            if not self.dry_run:
                directory.refresh({self.owner_id: self.owner_nickname})
            self.report["users"] += 1
        except Exception as exc:
            self._record_failure(self.cfg.users_file, exc)


# --------------------------------------------------------------------------
# 公开入口
# --------------------------------------------------------------------------

def import_legacy(
    cfg: Config,
    *,
    vault: str | Path,
    owner_id: str,
    owner_nickname: str = DEFAULT_OWNER_NICKNAME,
    scope: str = "group",
    dry_run: bool = False,
    force_persona: bool = False,
) -> dict[str, Any]:
    """导入旧 Obsidian 记忆库，返回 §6 规定的报告字典。

    - vault 不存在 → 抛 LegacyVaultError（CLI 映射为 exit code 2）；
    - 单个文件损坏 → failed+1，继续处理其余文件；
    - dry_run=True → 零落盘：只读模式加载现有数据做幂等判定，不建 data/ 目录、
      不写 memory.jsonl / memory.bad.jsonl / summary.json / users.json / persona.md。
    """
    vault_path = Path(vault).expanduser()
    if not vault_path.is_dir():
        raise LegacyVaultError(f"vault 目录不存在或不是目录：{vault_path}")
    if not str(owner_id or "").strip():
        raise ValueError("owner_id 不能为空")
    if scope not in ("group", "dm"):
        raise ValueError(f"invalid scope: {scope!r}")
    importer = _LegacyImporter(
        cfg,
        vault_path,
        owner_id=str(owner_id).strip(),
        owner_nickname=str(owner_nickname or "").strip(),
        scope=scope,
        dry_run=dry_run,
        force_persona=force_persona,
    )
    return importer.run()


def format_report(report: dict[str, Any]) -> str:
    """人读摘要（CLI 非 --json 输出）。"""
    suffix = "（dry-run，未写盘）" if report.get("dry_run") else ""
    return "\n".join(
        [
            f"旧库导入：{report.get('vault', '')}{suffix}",
            f"  episodes={report.get('episodes')}  clues={report.get('clues')}  "
            f"summary={report.get('summary')}  summary_seeded={report.get('summary_seeded')}  "
            f"state={report.get('state')}  persona={report.get('persona')}  "
            f"users={report.get('users')}",
            f"  skipped_existing={report.get('skipped_existing')}  "
            f"failed={report.get('failed')}  written={report.get('written')}",
        ]
    )
