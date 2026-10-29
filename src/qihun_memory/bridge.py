"""私聊 → 群聊记忆的单向桥接触发判定（v1.5-r2 §7.6）。

职责边界：
- 只回答「这条私聊消息是否显式指向群聊记忆」，不决定桥接多少条、不参与打分与
  预算（装填与预算在 retrieval.fill / Retriever 中）；
- **无 I/O、无状态**：不读文件、不写指标、不缓存；同输入必得同输出；
- 词表 / 实体表一律来自 `BridgeConfig`（可默认内置），本模块不硬编码第二份。

触发规则（§7.6）：
- 强触发词单命中即触发（`strong=True`）；
- 弱触发词需 ≥2 个不同词，或 1 个弱词 + 实体指代（时间/地点/具体事件名词）；
- 只有实体、没有弱词不触发；配置 `enabled=False` 永不触发。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .config import BridgeConfig

# BridgeDecision.reason 取值（供调用方记指标/排障，不参与触发判定）
REASON_DISABLED = "disabled"
REASON_STRONG = "strong"
REASON_WEAK_COUNT = "weak_count"
REASON_WEAK_ENTITY = "weak_entity"
REASON_NONE = "none"


@dataclass(frozen=True)
class BridgeDecision:
    """一次触发判定的结果（纯数据，无副作用）。"""

    triggered: bool = False
    strong: bool = False
    reason: str = REASON_NONE
    matched: tuple[str, ...] = field(default_factory=tuple)


def _compile_patterns(patterns) -> list[re.Pattern]:
    """把配置里的实体正则编译成 pattern 列表；非法正则静默跳过（配置容错）。"""
    compiled: list[re.Pattern] = []
    if not isinstance(patterns, (list, tuple)):
        return compiled
    for raw in patterns:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            compiled.append(re.compile(raw))
        except re.error:
            continue
    return compiled


def _dedup_sorted(values) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if str(value)}))


def _match_words(text: str, words) -> list[str]:
    """大小写不敏感的子串命中（中文无大小写，英文词表也兼容）。"""
    folded = text.lower()
    matched: list[str] = []
    if not isinstance(words, (list, tuple)):
        return matched
    for raw in words:
        word = str(raw).strip()
        if word and word.lower() in folded:
            matched.append(word)
    return matched


def _match_entities(text: str, patterns: Sequence[re.Pattern]) -> list[str]:
    matched: list[str] = []
    for pattern in patterns:
        try:
            found = pattern.search(text)
        except (TypeError, ValueError):  # pragma: no cover - 编译期已过滤
            continue
        if found:
            matched.append(found.group(0))
    return matched


def should_bridge(query, cfg: BridgeConfig | None = None) -> BridgeDecision:
    """判断一条私聊消息是否显式指向「群聊记忆」。

    纯函数：不读盘、不写指标、不修改任何全局状态。`query` 允许为 None/非 str
    （按空串处理），任何时候都不抛异常。
    """
    cfg = cfg or BridgeConfig()
    if not bool(getattr(cfg, "enabled", True)):
        return BridgeDecision(reason=REASON_DISABLED)
    text = "" if query is None else str(query)
    if not text.strip():
        return BridgeDecision(reason=REASON_NONE)

    strong = _match_words(text, getattr(cfg, "strong_words", ()))
    if strong:
        return BridgeDecision(
            triggered=True,
            strong=True,
            reason=REASON_STRONG,
            matched=_dedup_sorted(strong),
        )

    weak = _dedup_sorted(_match_words(text, getattr(cfg, "weak_words", ())))
    if len(weak) >= 2:
        return BridgeDecision(
            triggered=True,
            strong=False,
            reason=REASON_WEAK_COUNT,
            matched=weak,
        )
    if weak:
        entities = _match_entities(text, _compile_patterns(getattr(cfg, "entity_patterns", ())))
        if entities:
            return BridgeDecision(
                triggered=True,
                strong=False,
                reason=REASON_WEAK_ENTITY,
                matched=_dedup_sorted(list(weak) + list(entities)),
            )
        return BridgeDecision(triggered=False, strong=False, reason=REASON_NONE, matched=weak)
    return BridgeDecision(reason=REASON_NONE)
