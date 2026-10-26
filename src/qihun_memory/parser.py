"""LLM 输出解析（v1.4 §4.2）。

接口契约见 docs/INTERFACES.md 第 3 节。解析流水线顺序固定、不可互换：

1. 哨兵切分：收集所有哨兵起始位置，从后往前逐个尝试；
2. 括号平衡扫描：无哨兵时只从 memories 起点模式配对（字符串感知，未闭合切到文末）；
3. 有限清洗：BOM / 零宽字符、首尾围栏、字符串外尾逗号（仅当 json.loads 失败时）；
4. json.loads；
5. 顶层结构校验 + 逐条校验（坏条单条丢弃，不丢整批）；
6. 批内去重，最多 5 条。

设计目标：parse() 永不抛异常；回答正文永不包含隐藏 JSON（含截断残片）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import ExtractionConfig
from .metrics import Metrics

log = logging.getLogger("memory")

# --------------------------------------------------------------------------
# 协议常量（v1.4 §4.2.1 / §8）
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

# §4.2.6 注入脱敏：全角化后不会再被识别成哨兵 / 围栏
_SAFE_OPEN = "\uff1cMEMORY_JSON\uff1e"
_SAFE_CLOSE = "\uff1c/MEMORY_JSON\uff1e"
_SAFE_FENCE = "\uff40" * 3

# §12.1 指标事件名（parser 只负责 bad_json / fail / truncated 三个，
# ok / empty / no_sentinel 由调用方 pipeline 记录）
METRIC_BAD_JSON = "mem_extract.bad_json"
METRIC_FAIL = "mem_extract.fail"
METRIC_TRUNCATED = "mem_extract.truncated"


@dataclass(frozen=True)
class ExtractResult:
    """一次解析的完整结果（冻结契约，字段语义见 docs/INTERFACES.md 第 3 节）。"""

    answer: str
    memories: list[dict]
    status: str
    sentinel_found: bool
    truncated: bool = False
    exc_type: str | None = None
    raw_len: int = 0
    raw_hash: str = ""
    error: str | None = None


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------

def _clean_invisible(text: str) -> str:
    """去掉 BOM / 零宽字符（U+200B/200C/200D/FEFF），只做这一层。"""
    return text.translate(_INVISIBLE_TABLE)


def _drop_fences(text: str) -> str:
    """正文清洗：不可见字符 + 控制字符 + 首尾围栏 + 残留围栏标记，再压平多余空行。"""
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
    """只在字符串外删除紧跟右括号的尾逗号（字符串内的逗号原样保留）。

    这是「有限清洗」的边界：例如 content 里的「a,}b」必须原样保留。
    """
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
            # 未闭合的起点一路切到文末：截断残片不切掉就会漏到聊天里（§4.2.2 第 2 步）
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
# 逐条校验
# --------------------------------------------------------------------------

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


def _validate_item(item: Any, limits: ExtractionConfig) -> dict[str, Any] | None:
    """单条校验；坏条返回 None（§4.2.2 第 6 步：坏条目单条丢弃）。"""
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type not in ALLOWED_TYPES:
        return None
    raw_content = item.get("content")
    if not isinstance(raw_content, str):
        return None
    content = _clean_content(raw_content, limits.max_content)
    if not content:
        return None
    # content 含哨兵串或三反引号围栏 → 丢弃该条（防注入回流，§4.2.3）
    if SENT_OPEN in content or SENT_CLOSE in content or FENCE in content:
        return None
    tags = _clean_tags(item.get("tags"), limits.max_tags, limits.max_tag_len)
    return {"type": item_type, "content": content, "tags": tags}


def _validate_items(raw_items: Any, limits: ExtractionConfig) -> tuple[list[dict], bool]:
    """校验 + 批内去重 + 最多 N 条；返回 (条目, 是否因超限被截断)。"""
    if not isinstance(raw_items, list):
        return [], False
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    truncated = False
    for raw_item in raw_items:
        validated = _validate_item(raw_item, limits)
        if validated is None:
            continue
        key = (validated["type"], validated["content"])
        if key in seen:
            continue
        seen.add(key)
        if len(clean) >= limits.max_items:
            truncated = True
            break
        clean.append(validated)
    return clean, truncated


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------

def _raw_hash(raw: str) -> str:
    """raw 的 sha256 前 16 位；空输入返回空串（§12.2 隐私最小化）。"""
    if not raw:
        return ""
    try:
        payload = raw.encode("utf-8")
    except UnicodeEncodeError:  # 极端代理字符：哈希不能让解析失败
        payload = raw.encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:16]


def _strip_surrogates(text: str) -> str:
    """把孤立代理字符换成 U+FFFD；避免后续 utf-8 写盘 / 哈希抛 UnicodeEncodeError。"""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def _strip_partial_sentinel(text: str) -> str:
    """去掉末尾被截断的哨兵前缀（如 "...<MEMORY_JS"），确保残片不发给用户。

    只处理长度 ≥5 的前缀，避免误伤正文里正常的 "<MEMO" 之类文本。
    """
    for length in range(len(SENT_OPEN) - 1, 4, -1):
        prefix = SENT_OPEN[:length]
        if text.endswith(prefix):
            return text[:-length]
    return text


def _clean_answer(text: str) -> str:
    """正文清洗：去不可见字符 / 首尾围栏 / 哨兵残片，再压平空行。"""
    return _drop_fences(_strip_partial_sentinel(text))


def _fallback_answer(raw: str) -> str:
    """兜底口径：有哨兵取哨兵前；无哨兵剔除疑似 JSON 块；最差原样返回。"""
    try:
        positions = _sentinel_positions(raw)
        if positions:
            return _clean_answer(_cut_spans(raw[: positions[0]], _candidate_spans(raw[: positions[0]])))
        return _clean_answer(_cut_spans(raw, _candidate_spans(raw)))
    except Exception:
        try:
            return _strip_partial_sentinel(raw).strip()
        except Exception:  # pragma: no cover - str.strip 不会抛，纯防御
            return ""


# --------------------------------------------------------------------------
# 解析器
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

    # ---------- 主流程（§4.2.2 顺序固定） ----------

    def _parse(self, raw: str) -> ExtractResult:
        raw_len = len(raw)
        raw_hash = _raw_hash(raw)
        positions = _sentinel_positions(raw)
        if positions:
            return self._parse_sentinel(raw, positions, raw_len, raw_hash)
        spans = _candidate_spans(raw)
        if not spans:
            return ExtractResult(
                answer=_clean_answer(raw),
                memories=[],
                status="no_sentinel",
                sentinel_found=False,
                raw_len=raw_len,
                raw_hash=raw_hash,
            )
        return self._parse_fallback(raw, spans, raw_len, raw_hash)

    def _parse_sentinel(
        self,
        raw: str,
        positions: list[int],
        raw_len: int,
        raw_hash: str,
    ) -> ExtractResult:
        """第 1 步：哨兵从后往前逐个尝试，取第一个能解析出 memories 列表的。"""
        memories: list[Any] | None = None
        exc_type: str | None = None
        error: str | None = None
        unclosed = False
        for index in reversed(positions):
            _, candidate = _split_at(raw, index)
            if _candidate_unclosed(candidate):
                unclosed = True
            try:
                obj = _loads_candidate(candidate)
            except Exception as exc:
                exc_type = type(exc).__name__
                error = "json_decode"
                continue
            if isinstance(obj, dict) and isinstance(obj.get("memories"), list):
                memories = obj["memories"]
                exc_type = None
                error = None
                # 解析成功即证明该候选 JSON 是闭合的；后面（已被切掉）的残片不再算截断
                unclosed = False
                break
            exc_type = "invalid_structure"
            error = "invalid_structure"

        # 回答正文 = 最早哨兵之前的全部内容（§4.2.1 硬规则 2）：
        # 即便靠后的哨兵解析成功，靠前的伪哨兵块也一并切掉，保证正文永不含隐藏 JSON。
        # 哨兵前若还有 {"memories":...} 块（模型顺序漂移），也要切掉，否则会泄给用户（B7）。
        pre_sentinel = raw[: positions[0]]
        answer = _clean_answer(_cut_spans(pre_sentinel, _candidate_spans(pre_sentinel)))
        if memories is None:
            self._record_bad_json(raw_len, raw_hash, exc_type)
            if unclosed:
                self._record_truncated(raw_len, raw_hash)
            return ExtractResult(
                answer=answer,
                memories=[],
                status="bad_json",
                sentinel_found=True,
                truncated=unclosed,
                exc_type=exc_type,
                raw_len=raw_len,
                raw_hash=raw_hash,
                error=error,
            )
        items, over_cap = _validate_items(memories, self.extraction)
        truncated = unclosed or over_cap
        if truncated:
            self._record_truncated(raw_len, raw_hash)
        return ExtractResult(
            answer=answer,
            memories=items,
            status="ok" if items else "empty",
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
        """第 2 步：无哨兵时用括号平衡扫描兜底（只认 memories 起点）。"""
        start, end = spans[-1]
        candidate = raw[start:end]
        # 兜底一律把所有候选块从正文里剔除（含截断残片）
        answer = _clean_answer(_cut_spans(raw, spans))
        unclosed = _balanced_end(raw, start) is None
        try:
            obj = _loads_candidate(candidate)
        except Exception as exc:
            exc_type = type(exc).__name__
            self._record_bad_json(raw_len, raw_hash, exc_type)
            if unclosed:
                self._record_truncated(raw_len, raw_hash)
            return ExtractResult(
                answer=answer,
                memories=[],
                status="bad_json",
                sentinel_found=False,
                truncated=unclosed,
                exc_type=exc_type,
                raw_len=raw_len,
                raw_hash=raw_hash,
                error="json_decode",
            )
        if not isinstance(obj, dict) or not isinstance(obj.get("memories"), list):
            self._record_bad_json(raw_len, raw_hash, "invalid_structure")
            if unclosed:
                self._record_truncated(raw_len, raw_hash)
            return ExtractResult(
                answer=answer,
                memories=[],
                status="bad_json",
                sentinel_found=False,
                truncated=unclosed,
                exc_type="invalid_structure",
                raw_len=raw_len,
                raw_hash=raw_hash,
                error="invalid_structure",
            )
        items, over_cap = _validate_items(obj["memories"], self.extraction)
        truncated = unclosed or over_cap
        if truncated:
            self._record_truncated(raw_len, raw_hash)
        return ExtractResult(
            answer=answer,
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
        except Exception:  # pragma: no cover - Metrics.event 已自带兜底
            log.warning("memory_extract metrics event failed: %s", name)

    def _record_bad_json(self, raw_len: int, raw_hash: str, exc_type: str | None) -> None:
        """bad_json 每次解析最多记一次，字段只用 raw_len / raw_hash / exc_type。"""
        log.warning(
            "memory_extract bad_json raw_len=%s raw_hash=%s exc=%s",
            raw_len,
            raw_hash or "-",
            exc_type or "-",
        )
        self._event(METRIC_BAD_JSON, raw_len=raw_len, raw_hash=raw_hash, exc_type=exc_type)

    def _record_truncated(self, raw_len: int, raw_hash: str) -> None:
        self._event(METRIC_TRUNCATED, raw_len=raw_len, raw_hash=raw_hash)


# --------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------

_DEFAULT_PARSER = MemoryOutputParser()


def parse_llm_output(raw: str | None) -> tuple[str, list[dict]]:
    """§4.2.3 原签名：返回 (answer, memories)，永不抛异常。"""
    try:
        result = _DEFAULT_PARSER.parse(raw)
        return result.answer, result.memories
    except Exception:  # pragma: no cover - parse 自身已兜底
        return (raw.strip() if isinstance(raw, str) else ""), []


def sanitize_for_prompt(text: str) -> str:
    """注入防护（§4.2.6）：哨兵 / 代码围栏全角化，幂等且永不抛异常。

    用户消息、persona、滚动摘要进 prompt 前都过这一层，防止用户借哨兵改写
    解析边界；全角形式不会被解析器识别成哨兵，也不影响语义阅读。
    """
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
    except Exception:  # pragma: no cover - str.replace 不会抛，纯防御
        return ""
