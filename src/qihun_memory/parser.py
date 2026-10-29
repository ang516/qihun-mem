"""LLM 输出解析（v1.5-r2 §4.2 / §4.2.3 / 附录 D2）。

流水线顺序固定、不可互换：
1. 哨兵切分：收集所有哨兵起始位置，从后往前逐个尝试；
2. 括号平衡扫描：无哨兵时只从 memories 起点模式配对（字符串感知，未闭合切到文末）；
3. 有限清洗：BOM / 零宽字符、首尾围栏、字符串外尾逗号（仅当 json.loads 失败时）；
4. json.loads；
5. 顶层结构校验 + 逐条归一化（normalize_item：坏条单条丢弃，affect 非法只丢字段）；
6. dedup_within_batch（dedup_key 精确去重）+ 最多 5 条。

v1.5-r2 边界（附录 D2）：
- 解析层只返回 raw_answer（不清洗 answer），永不抛异常；
- 发送层 clean_answer_for_send 是唯一清洗点（硬规则 13 在此单点可测）；
- 解析层禁止调 _drop_fences，发送层禁止调 _loads_candidate。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from .config import ExtractionConfig
from .metrics import Metrics
from .models import ALLOWED_AFFECT_LABELS, sanitize_affect

log = logging.getLogger("memory")

# --------------------------------------------------------------------------
# 协议常量（v1.5 §4.2.1 / §9）
# --------------------------------------------------------------------------

SENT_OPEN = "<MEMORY_JSON>"
SENT_CLOSE = "</MEMORY_JSON>"
ALLOWED_TYPES = {"episode", "fact", "preference"}
MAX_ITEMS = 5
MAX_CONTENT = 500
MAX_TAGS = 8
MAX_TAG_LEN = 16
# 三反引号围栏；源码里统一用 chr(96) 拼接，避免被 Markdown / 文档渲染吞掉
FENCE = chr(96) * 3

# §4.2.2 第 3 步：只去 BOM / 零宽字符
INVISIBLE = ("\ufeff", "\u200b", "\u200c", "\u200d")
_INVISIBLE_TABLE = {ord(ch): None for ch in INVISIBLE}

# 兜底扫描的候选起点：只认 memories 键开头的对象（冒号前后允许空白）
MEM_START_RE = re.compile(r'\{\s*"memories"\s*:')
TICK_RE = re.compile(FENCE + r"[A-Za-z0-9_+.-]*")
LEAD_FENCE_RE = re.compile(r"^\s*" + FENCE + r"[A-Za-z0-9_+.-]*\s*")
TAIL_FENCE_RE = re.compile(r"\s*" + FENCE + r"\s*$")

# §4.2 注入脱敏：全角化后不会再被识别成哨兵 / 围栏
_SAFE_OPEN = "\uff1cMEMORY_JSON\uff1e"
_SAFE_CLOSE = "\uff1c/MEMORY_JSON\uff1e"
_SAFE_FENCE = "\uff40" * 3

# §12.1 指标事件名（ok / empty / no_sentinel 由调用方 pipeline 记录）
METRIC_BAD_JSON = "mem_extract.bad_json"
METRIC_FAIL = "mem_extract.fail"
METRIC_TRUNCATED = "mem_extract.truncated"
METRIC_AFFECT_INVALID = "mem_extract.affect_invalid"
METRIC_ITEM_INVALID = "mem_extract.item_invalid"

# 中文标点 → 半角（只用于 dedup_key；不改原文）
_PUNCT_MAP = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "：": ":",
        "；": ";",
        "、": ",",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "《": "<",
        "》": ">",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "…": "...",
        "—": "-",
        "～": "~",
    }
)


@dataclass(frozen=True)
class ExtractResult:
    """一次解析的完整结果（v1.5 增加 raw_answer，保留旧字段）。"""

    answer: str
    memories: list[dict]
    status: str
    sentinel_found: bool
    raw_answer: str = ""
    truncated: bool = False
    exc_type: str | None = None
    raw_len: int = 0
    raw_hash: str = ""
    error: str | None = None


# --------------------------------------------------------------------------
# 文本清洗（发送层 / 候选清洗）
# --------------------------------------------------------------------------

def _clean_invisible(text: str) -> str:
    """去掉 BOM / 零宽字符（U+200B/200C/200D/FEFF），只做这一层。"""
    return text.translate(_INVISIBLE_TABLE)


def _drop_fences(text: str) -> str:
    """正文清洗：不可见字符 + 控制字符 + 首尾围栏 + 残留围栏标记，再压平多余空行。

    只允许发送层（clean_answer_for_send / _clean_answer）调用；解析层禁止调用。
    """
    text = _clean_invisible(text)
    text = "".join(
        ch
        for ch in text
        if ch in "\n\t\r" or not (ord(ch) < 32 or ord(ch) == 127 or 0x80 <= ord(ch) <= 0x9F)
    )
    text = LEAD_FENCE_RE.sub("", text)
    text = TAIL_FENCE_RE.sub("", text)
    text = TICK_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_candidate(text: str) -> str:
    """候选 JSON 的有限清洗：不可见字符 + 首尾围栏 + 首尾多余逗号。"""
    text = _clean_invisible(text).strip()
    text = LEAD_FENCE_RE.sub("", text)
    text = TAIL_FENCE_RE.sub("", text)
    return text.strip().rstrip(",").strip()


def _drop_trailing_commas(text: str) -> str:
    """只在字符串外删除紧跟右括号的尾逗号（字符串内的逗号原样保留）。"""
    out: list[str] = []
    in_str = False
    escaped = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            if j < length and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _loads_candidate(candidate: str) -> Any:
    """先严格 json.loads；失败才在字符串外去尾逗号再试一次。"""
    text = _clean_candidate(candidate)
    try:
        return json.loads(text)
    except Exception:
        return json.loads(_drop_trailing_commas(text))


# --------------------------------------------------------------------------
# 括号平衡扫描（字符串感知；绝对禁止用正则抠嵌套 JSON）
# --------------------------------------------------------------------------

def _balanced_end(text: str, start: int) -> int | None:
    """返回 start 处左花括号对应的右花括号之后的下标；未闭合返回 None。"""
    depth = 0
    in_str = False
    escaped = False
    i = start
    length = len(text)
    while i < length:
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _candidate_spans(raw: str) -> list[tuple[int, int]]:
    """所有疑似 memories 块的 [start, end)；未闭合的块 end = len(raw)（截断兜底）。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        match = MEM_START_RE.search(raw, pos)
        if match is None:
            break
        start = match.start()
        end = _balanced_end(raw, start)
        if end is None:
            spans.append((start, len(raw)))
            break
        spans.append((start, end))
        pos = end
    return spans


def _cut_spans(raw: str, spans: list[tuple[int, int]]) -> str:
    """删掉所有候选块，只留正文。"""
    if not spans:
        return raw
    parts: list[str] = []
    pos = 0
    for start, end in spans:
        parts.append(raw[pos:start])
        pos = end
    parts.append(raw[pos:])
    return "".join(parts)


# --------------------------------------------------------------------------
# 哨兵切分
# --------------------------------------------------------------------------

def _sentinel_positions(raw: str) -> list[int]:
    """所有哨兵出现位置（可能被正文里的伪造哨兵污染，故需逐个尝试）。"""
    positions: list[int] = []
    start = raw.find(SENT_OPEN)
    while start >= 0:
        positions.append(start)
        start = raw.find(SENT_OPEN, start + len(SENT_OPEN))
    return positions


def _split_at(raw: str, index: int) -> tuple[str, str]:
    """切成 (哨兵前正文, 哨兵后候选 JSON)；闭合标签缺失时候选取到末尾。"""
    rest = raw[index + len(SENT_OPEN):]
    close = rest.find(SENT_CLOSE)
    if close >= 0:
        rest = rest[:close]
    return raw[:index], rest


def _candidate_unclosed(candidate: str) -> bool:
    """候选 JSON 是否括号未闭合（只做 truncated 诊断，不参与解析）。"""
    text = _clean_candidate(candidate)
    brace = text.find("{")
    if brace < 0:
        return False
    return _balanced_end(text, brace) is None


# --------------------------------------------------------------------------
# v1.5 归一化 / 去重（附录 D1：dedup_key 是唯一键定义）
# --------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """去重键的规范化（v1.5 §4.2.2）：

    1. Unicode NFKC；
    2. 全角 ASCII → 半角（NFKC 已完成）；
    3. 中文标点统一为半角（仅键用，不改原文）；
    4. 删除零宽字符 / BOM / 控制字符；
    5. 折叠连续空白为单空格，去首尾；
    6. ASCII 大小写折叠；
    7. emoji 保留原样，不折叠；
    8. 不折叠重复字符（"好好好" ≠ "好"）；
    9. 不截断（键用完整 normalize 文本，不截 500）。
    """
    if not isinstance(text, str):
        try:
            text = "" if text is None else str(text)
        except Exception:
            return ""
    if not text:
        return ""
    try:
        text = unicodedata.normalize("NFKC", text)
        text = text.translate(_PUNCT_MAP)
        text = "".join(
            ch
            for ch in text
            if ch not in INVISIBLE
            and not (0xD800 <= ord(ch) <= 0xDFFF)
            and unicodedata.category(ch) != "Cc"
        )
        text = re.sub(r"\s+", " ", text).strip()
        text = "".join(ch.lower() if ord(ch) < 128 else ch for ch in text)
        return text
    except Exception:  # pragma: no cover - 纯防御
        return text.strip()


def dedup_key(item: Mapping[str, Any]) -> tuple[str, str]:
    """唯一去重键定义：批内 / 跨轮 / 跨池共用，禁止各层自行拼键。"""
    if not isinstance(item, Mapping):
        return ("", "")
    item_type = str(item.get("type") or "").strip().lower()
    content = item.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    return (item_type, normalize_text(content))


def validate_affect(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """校验单条 affect；非法返回 None（调用方只丢字段，不丢条）。

    非法 = label 不在 7 类枚举 / intensity 非数字；intensity 越界按 [0,1] 截断。
    """
    if not isinstance(item, Mapping):
        return None
    raw = item.get("affect")
    if raw is None:
        return None
    return sanitize_affect(raw)


def _clean_content(value: str, max_content: int = MAX_CONTENT) -> str:
    """剔除控制字符（C0 + DEL + C1）与孤立代理字符，strip 后截断。"""
    chars: list[str] = []
    for ch in value:
        code = ord(ch)
        if code < 32 or code == 127 or 0x80 <= code <= 0x9F or 0xD800 <= code <= 0xDFFF:
            continue
        chars.append(ch)
    return "".join(chars).strip()[:max_content]


def _clean_tags(
    raw_tags: Any,
    max_tags: int = MAX_TAGS,
    max_tag_len: int = MAX_TAG_LEN,
) -> list[str]:
    """tags 是 list 时取前 N 个非空串、每个 ≤L 字符；否则 []。"""
    if not isinstance(raw_tags, list):
        return []
    tags: list[str] = []
    for tag in raw_tags:
        try:
            text = str(tag).strip()
        except Exception:  # 极端对象：单条 tag 失败不影响整条记忆
            continue
        if not text:
            continue
        tags.append(text[:max_tag_len])
        if len(tags) >= max_tags:
            break
    return tags


def normalize_item(
    m: Any,
    limits: ExtractionConfig | None = None,
    metrics: Metrics | None = None,
) -> dict[str, Any] | None:
    """逐条归一化：只做清洗与校验，不做去重（v1.5 §4.2.2 第 6 步）。

    坏条目单条丢弃，不丢整批；affect 非法只丢 affect，记忆仍写入。
    """
    limits = limits or ExtractionConfig()
    if not isinstance(m, dict):
        _event(metrics, METRIC_ITEM_INVALID, reason="not_object")
        return None
    item_type = m.get("type")
    if not isinstance(item_type, str) or item_type not in ALLOWED_TYPES:
        _event(metrics, METRIC_ITEM_INVALID, reason="bad_type")
        return None
    raw_content = m.get("content")
    if not isinstance(raw_content, str):
        _event(metrics, METRIC_ITEM_INVALID, reason="bad_content")
        return None
    content = _clean_content(raw_content, limits.max_content)
    if not content:
        _event(metrics, METRIC_ITEM_INVALID, reason="empty_content")
        return None
    # content 含哨兵串或三反引号围栏 → 丢弃该条（防注入回流，§4.2.2）
    if SENT_OPEN in content or SENT_CLOSE in content or FENCE in content:
        _event(metrics, METRIC_ITEM_INVALID, reason="sentinel_or_fence")
        return None
    tags = _clean_tags(m.get("tags"), limits.max_tags, limits.max_tag_len)
    affect: dict[str, Any] | None = None
    if m.get("affect") is not None:
        affect = validate_affect(m)
        if affect is None:
            _event(metrics, METRIC_AFFECT_INVALID)
    item: dict[str, Any] = {"type": item_type, "content": content, "tags": tags}
    if affect is not None:
        item["affect"] = affect
    return item


def dedup_within_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 dedup_key 精确去重，保留首次（affect 不参与键，保留首次 affect）。"""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = dedup_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _collect_items(
    raw_items: Any,
    limits: ExtractionConfig,
    metrics: Metrics | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """normalize → dedup → 上限；返回 (items, over_cap)。"""
    if not isinstance(raw_items, list):
        return [], False
    normalized: list[dict[str, Any]] = []
    for raw_item in raw_items:
        item = normalize_item(raw_item, limits, metrics)
        if item is not None:
            normalized.append(item)
    items = dedup_within_batch(normalized)
    if len(items) > limits.max_items:
        return items[: limits.max_items], True
    return items, False


# 兼容旧内部名（test/旧文档可能引用）
def _validate_items(raw_items: Any, limits: ExtractionConfig, metrics: Metrics | None = None):
    return _collect_items(raw_items, limits, metrics)


# --------------------------------------------------------------------------
# 解析层 / 发送层（附录 D2 两个入口）
# --------------------------------------------------------------------------

def split_raw_candidate(raw: str | None) -> tuple[str, str | None]:
    """只做哨兵/括号平衡定位，返回 (raw_answer, candidate_json_or_None)。

    - 有哨兵：从后往前尝试，取第一个结构上含 memories 列表的候选；
      raw_answer 一律取**最早哨兵之前**（防止伪哨兵块泄给用户）；
    - 无哨兵：取最后一个括号平衡块，raw_answer 剔除所有候选块；
    - 返回的 raw_answer 未清洗（发送层负责）。
    """
    try:
        text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        text = _strip_surrogates(text)
        positions = _sentinel_positions(text)
        if positions:
            for index in reversed(positions):
                _before, candidate = _split_at(text, index)
                try:
                    obj = _loads_candidate(candidate)
                except Exception:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("memories"), list):
                    return text[: positions[0]], candidate
            return text[: positions[0]], None
        spans = _candidate_spans(text)
        if not spans:
            return text, None
        start, end = spans[-1]
        return _cut_spans(text, spans), text[start:end]
    except Exception:
        return (raw if isinstance(raw, str) else ""), None


def parse_memories(
    candidate_json: str | None,
    limits: ExtractionConfig | None = None,
    metrics: Metrics | None = None,
) -> list[dict[str, Any]]:
    """只做 loads → normalize_item → dedup_within_batch；不碰 answer、不抛异常。"""
    if not candidate_json:
        return []
    limits = limits or ExtractionConfig()
    try:
        obj = _loads_candidate(candidate_json)
    except Exception as exc:
        log.warning("memory_extract bad_json exc=%s", type(exc).__name__)
        _event(metrics, METRIC_BAD_JSON, exc_type=type(exc).__name__)
        return []
    memories = obj.get("memories") if isinstance(obj, dict) else None
    if not isinstance(memories, list):
        _event(metrics, METRIC_BAD_JSON, exc_type="invalid_structure")
        return []
    items, _over_cap = _collect_items(memories, limits, metrics)
    return items


def parse_llm_output(raw: str | None) -> tuple[str, list[dict]]:
    """§4.2.3 解析层入口：返回 (raw_answer, memories)，永不抛异常。

    注意：raw_answer **未清洗**；发送前必须过 clean_answer_for_send（附录 D2）。
    """
    try:
        raw_answer, candidate = split_raw_candidate(raw)
        memories = parse_memories(candidate) if candidate is not None else []
        return raw_answer, memories
    except Exception:  # pragma: no cover - split/parse 自身已兜底
        return (raw if isinstance(raw, str) else ""), []


def _strip_partial_sentinel(text: str) -> str:
    """去掉末尾被截断的哨兵前缀（如 "...<MEMORY_JS"），确保残片不发给用户。"""
    for length in range(len(SENT_OPEN) - 1, 4, -1):
        prefix = SENT_OPEN[:length]
        if text.endswith(prefix):
            return text[:-length]
    return text


def clean_answer_for_send(raw_answer: str | None) -> str:
    """发送层唯一清洗点（v1.5 §4.2.3 / 附录 D2）。

    有哨兵取最早哨兵前（并剔除块前伪候选块）；无哨兵剔除疑似 JSON 块；
    最差原样返回。不读取 memories、不调用 _loads_candidate。
    """
    try:
        text = raw_answer if isinstance(raw_answer, str) else ("" if raw_answer is None else str(raw_answer))
        text = _strip_surrogates(text)
        positions = _sentinel_positions(text)
        if positions:
            prefix = text[: positions[0]]
            prefix = _cut_spans(prefix, _candidate_spans(prefix))
            return _drop_fences(_strip_partial_sentinel(prefix))
        cut = _cut_spans(text, _candidate_spans(text))
        return _drop_fences(_strip_partial_sentinel(cut))
    except Exception:
        try:
            return (raw_answer if isinstance(raw_answer, str) else "").strip()
        except Exception:  # pragma: no cover
            return ""


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------

def _raw_hash(raw: str) -> str:
    """raw 的 sha256 前 16 位；空输入返回空串（§12.2 隐私最小化）。"""
    if not raw:
        return ""
    try:
        payload = raw.encode("utf-8")
    except UnicodeEncodeError:
        payload = raw.encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:16]


def _strip_surrogates(text: str) -> str:
    """把孤立代理字符换成 U+FFFD；避免后续 utf-8 写盘 / 哈希抛 UnicodeEncodeError。"""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def _clean_answer(text: str) -> str:
    """兼容旧名：发送层清洗（内部调用 clean_answer_for_send）。"""
    return clean_answer_for_send(text)


def _fallback_answer(raw: str) -> str:
    """兜底口径：有哨兵取哨兵前；无哨兵剔除疑似 JSON 块；最差原样返回。"""
    try:
        return clean_answer_for_send(raw)
    except Exception:  # pragma: no cover
        try:
            return raw.strip()
        except Exception:
            return ""


# --------------------------------------------------------------------------
# 解析器（状态/指标兼容入口；内部复用上面的纯函数）
# --------------------------------------------------------------------------

class MemoryOutputParser:
    """解析 LLM 原始输出（回答正文 + 哨兵隐藏 JSON）。"""

    def __init__(
        self,
        metrics: Metrics | None = None,
        extraction: ExtractionConfig | None = None,
    ) -> None:
        self.metrics = metrics
        self.extraction = extraction or ExtractionConfig()

    # ---------- 公开入口 ----------

    def parse(self, raw: str | None) -> ExtractResult:
        """解析；任何输入都返回 ExtractResult，绝不抛异常。"""
        try:
            text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
            text = _strip_surrogates(text)
        except Exception as exc:  # 极端输入（__str__ 抛异常）
            return self._fail(raw_text="", exc=exc, sentinel_found=False)
        sentinel_found = SENT_OPEN in text
        try:
            return self._parse(text)
        except Exception as exc:  # 解析器内部未预期异常：兜底，不抛回聊天主流程
            return self._fail(raw_text=text, exc=exc, sentinel_found=sentinel_found)

    # ---------- 主流程 ----------

    def _parse(self, raw: str) -> ExtractResult:
        raw_len = len(raw)
        raw_hash = _raw_hash(raw)
        positions = _sentinel_positions(raw)
        if positions:
            return self._parse_sentinel(raw, positions, raw_len, raw_hash)
        spans = _candidate_spans(raw)
        if not spans:
            return ExtractResult(
                answer=clean_answer_for_send(raw),
                raw_answer=raw,
                memories=[],
                status="no_sentinel",
                sentinel_found=False,
                raw_len=raw_len,
                raw_hash=raw_hash,
            )
        return self._parse_fallback(raw, spans, raw_len, raw_hash)

    def _extract(self, candidate: str) -> tuple[list[dict] | None, str | None, bool]:
        """解析一个候选；返回 (items 或 None, exc_type, over_cap)。"""
        try:
            obj = _loads_candidate(candidate)
        except Exception as exc:
            return None, type(exc).__name__, False
        if not isinstance(obj, dict) or not isinstance(obj.get("memories"), list):
            return None, "invalid_structure", False
        items, over_cap = _collect_items(obj["memories"], self.extraction, self.metrics)
        return items, None, over_cap

    def _parse_sentinel(
        self,
        raw: str,
        positions: list[int],
        raw_len: int,
        raw_hash: str,
    ) -> ExtractResult:
        memories: list[dict] | None = None
        exc_type: str | None = None
        unclosed = False
        over_cap = False
        for index in reversed(positions):
            _, candidate = _split_at(raw, index)
            if _candidate_unclosed(candidate):
                unclosed = True
            items, candidate_exc, candidate_over_cap = self._extract(candidate)
            if items is not None:
                memories, exc_type, over_cap = items, None, candidate_over_cap
                unclosed = False
                break
            exc_type = candidate_exc
        raw_answer = raw[: positions[0]]
        answer = clean_answer_for_send(raw_answer)
        if memories is None:
            self._record_bad_json(raw_len, raw_hash, exc_type)
            if unclosed:
                self._record_truncated(raw_len, raw_hash)
            return ExtractResult(
                answer=answer,
                raw_answer=raw_answer,
                memories=[],
                status="bad_json",
                sentinel_found=True,
                truncated=unclosed,
                exc_type=exc_type,
                raw_len=raw_len,
                raw_hash=raw_hash,
                error=exc_type,
            )
        truncated = unclosed or over_cap
        if truncated:
            self._record_truncated(raw_len, raw_hash)
        return ExtractResult(
            answer=answer,
            raw_answer=raw_answer,
            memories=memories,
            status="ok" if memories else "empty",
            sentinel_found=True,
            truncated=truncated,
            raw_len=raw_len,
            raw_hash=raw_hash,
        )

    def _parse_fallback(
        self,
        raw: str,
        spans: list[tuple[int, int]],
        raw_len: int,
        raw_hash: str,
    ) -> ExtractResult:
        start, end = spans[-1]
        candidate = raw[start:end]
        raw_answer = _cut_spans(raw, spans)
        answer = clean_answer_for_send(raw_answer)
        unclosed = _balanced_end(raw, start) is None
        items, exc_type, over_cap = self._extract(candidate)
        if items is None:
            self._record_bad_json(raw_len, raw_hash, exc_type)
            if unclosed:
                self._record_truncated(raw_len, raw_hash)
            return ExtractResult(
                answer=answer,
                raw_answer=raw_answer,
                memories=[],
                status="bad_json",
                sentinel_found=False,
                truncated=unclosed,
                exc_type=exc_type,
                raw_len=raw_len,
                raw_hash=raw_hash,
                error=exc_type,
            )
        truncated = unclosed or over_cap
        if truncated:
            self._record_truncated(raw_len, raw_hash)
        return ExtractResult(
            answer=answer,
            raw_answer=raw_answer,
            memories=items,
            status="ok" if items else "empty",
            sentinel_found=False,
            truncated=truncated,
            raw_len=raw_len,
            raw_hash=raw_hash,
        )

    # ---------- 失败兜底 ----------

    def _fail(self, raw_text: str, exc: BaseException, sentinel_found: bool) -> ExtractResult:
        """§4.2.4：解析器自身异常 → 兜底回答 + fail 指标，绝不外抛。"""
        raw_len = len(raw_text)
        raw_hash = _raw_hash(raw_text)
        exc_type = type(exc).__name__
        log.warning(
            "memory_extract fail raw_len=%s raw_hash=%s exc=%s",
            raw_len,
            raw_hash or "-",
            exc_type,
        )
        self._event(METRIC_FAIL, raw_len=raw_len, raw_hash=raw_hash, exc_type=exc_type)
        try:
            answer = _fallback_answer(raw_text)
        except Exception:  # 兜底本身也兜底
            answer = raw_text.strip()
        return ExtractResult(
            answer=answer,
            raw_answer=raw_text,
            memories=[],
            status="fail",
            sentinel_found=sentinel_found,
            truncated=False,
            exc_type=exc_type,
            raw_len=raw_len,
            raw_hash=raw_hash,
            error=exc_type,
        )

    # ---------- 指标 ----------

    def _event(self, name: str, **fields: Any) -> None:
        """记一条指标；metrics 自身异常也绝不能影响解析主流程。"""
        if self.metrics is None:
            return
        try:
            self.metrics.event(name, **fields)
        except Exception:  # pragma: no cover
            log.warning("memory_extract metrics event failed: %s", name)

    def _record_bad_json(self, raw_len: int, raw_hash: str, exc_type: str | None) -> None:
        log.warning(
            "memory_extract bad_json raw_len=%s raw_hash=%s exc=%s",
            raw_len,
            raw_hash or "-",
            exc_type or "-",
        )
        self._event(METRIC_BAD_JSON, raw_len=raw_len, raw_hash=raw_hash, exc_type=exc_type)

    def _record_truncated(self, raw_len: int, raw_hash: str) -> None:
        self._event(METRIC_TRUNCATED, raw_len=raw_len, raw_hash=raw_hash)


def _event(metrics: Metrics | None, name: str, **fields: Any) -> None:
    if metrics is None:
        return
    try:
        metrics.event(name, **fields)
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------

_DEFAULT_PARSER = MemoryOutputParser()


def sanitize_for_prompt(text: str) -> str:
    """注入防护（§4.2.6）：哨兵 / 代码围栏全角化，幂等且永不抛异常。"""
    if not isinstance(text, str):
        try:
            text = "" if text is None else str(text)
        except Exception:
            return ""
    if not text:
        return ""
    try:
        text = text.replace(SENT_CLOSE, _SAFE_CLOSE)
        text = text.replace(SENT_OPEN, _SAFE_OPEN)
        return text.replace(FENCE, _SAFE_FENCE)
    except Exception:  # pragma: no cover
        return ""
