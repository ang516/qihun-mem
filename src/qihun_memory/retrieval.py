"""检索（v1.5-r2 §6 / §7.2 / §7.3 / §7.4 / §7.6）。

顺序固定：建池 → BM25（批内 min-max 归一化）/ 语义 cos
（embedder 不可用 / 缺向量时降级 weights=(0,1)）→ R（lifecycle.retention_now）
→ 单公式打分（含 rel / familiarity / user_affect / affect_match 调制）
→ 跨池去重（重复对保留 score 高者）→ 确定性排序
→ `fill(entries, constraints)` 预算装填 → injected_items。

池的语义（§7.2 / §7.4 / §7.6）：
- `scope="group"`：**混合池**——所有 group 记忆都参与（不按 user_id 预过滤），
  `rel=1` 仅当前用户；rel 决定 familiarity / user_affect 调制是否生效；
- `scope="dm"`：只取同 user 的 dm 记忆；`should_bridge(query)` 命中时追加
  同 user 的 **active group** 记忆（普通池权重 0.7、独立预算、跨池去重）；
- 桥接预算由 `fill` 的 constraints 传入，本模块不硬编码 300。

检索只读：不修改传入的 MemoryItem，不写盘，不写 user_affect.json；
不得因为单条脏数据抛异常到主流程。
"""

from __future__ import annotations

import logging
import math
import operator
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from . import lifecycle
from .bridge import BridgeDecision, should_bridge
from .config import Config
from .embedding import EmbeddingUnavailable, Embedder
from .metrics import Metrics
from .models import (
    ALLOWED_AFFECT_LABELS,
    ANGER_AFFECT_LABELS,
    JOY_AFFECT_LABELS,
    MemoryItem,
    affect_intensity,
    affect_label,
    utc_now,
)
from .parser import dedup_key, normalize_text
from .state import BotState
from .tokens import estimate_tokens

log = logging.getLogger("memory")

# CJK 统一表意文字（含扩展 A / 兼容表意文字）：按字符 run 切 bigram。
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
# 拉丁 / 数字 / 下划线：按小写词切。
_WORD_RE = re.compile(r"[a-z0-9_]+")

DEGRADED_EMBEDDER_UNAVAILABLE = "embedder_unavailable"
DEGRADED_ENCODE_FAILED = "encode_failed"
DEGRADED_NO_VECTORS = "no_usable_vectors"
DEGRADED_DEDUP_NO_VECTORS = "no_usable_vectors"

VALID_SCOPES = ("group", "dm")

# §6 / §7.4：池标记（SelectedMemory.source_pool / injected_items.source_pool 的取值）
SOURCE_POOL_GROUP = "group"
SOURCE_POOL_DM = "dm"
SOURCE_POOL_BRIDGE = "bridge"
SOURCE_POOLS = (SOURCE_POOL_GROUP, SOURCE_POOL_DM, SOURCE_POOL_BRIDGE)

# §6.4 constraints 的默认形态（调用方约定值；fill 自身不以此为兜底，全部读 constraints）
DEFAULT_FILL_TOTAL = 1200
DEFAULT_HEAD_PER_ITEM = 24
# §11 硬规则 18：桥接低权重 ≤ 0.7、单独限额 ≤ 300 tokens（retrieval 侧对配置做上限约束）
DEFAULT_BRIDGE_WEIGHT = 0.7
MAX_BRIDGE_WEIGHT = 0.7
MAX_BRIDGE_BUDGET_TOKENS = 300

# 同族 affect：joy/affection 一族、anger/disgust 一族（§7.2 affect_sim）
_AFFECT_FAMILIES = (JOY_AFFECT_LABELS, ANGER_AFFECT_LABELS)


def _event(metrics: Metrics | None, name: str, **fields) -> None:
    if metrics is None:
        return
    try:
        metrics.event(name, **fields)
    except Exception:  # pragma: no cover - 指标失败绝不影响检索
        pass


def _safe_int(raw, default: int) -> int:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _safe_float(raw, default: float) -> float:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# --------------------------------------------------------------------------
# affect_sim（§7.2 affect_match）
# --------------------------------------------------------------------------


def affect_similarity(left, right) -> float:
    """两条 affect 的相似度（0~1，供 affect_match 调制使用）。

    语义：
    - 任一侧缺失 / neutral → 0（无信息，不做加成）；
    - label 相同 → 1.0；
    - 同族（joy↔affection、anger↔disgust）→ 0.5；
    - 其它组合 → 0.0。
    """
    left_label = affect_label(left) if isinstance(left, Mapping) else "neutral"
    right_label = affect_label(right) if isinstance(right, Mapping) else "neutral"
    if left_label == "neutral" or right_label == "neutral":
        return 0.0
    if left_label not in ALLOWED_AFFECT_LABELS or right_label not in ALLOWED_AFFECT_LABELS:
        return 0.0
    if left_label == right_label:
        return 1.0
    for family in _AFFECT_FAMILIES:
        if left_label in family and right_label in family:
            return 0.5
    return 0.0


def _user_affect_values(user_affect) -> dict | None:
    """把 UserAffect / Mapping 规整为 {affection, annoyance, familiarity}；无效返回 None。"""
    if user_affect is None:
        return None
    if isinstance(user_affect, Mapping):
        source = user_affect
    else:
        source = {
            "affection": getattr(user_affect, "affection", None),
            "annoyance": getattr(user_affect, "annoyance", None),
            "familiarity": getattr(user_affect, "familiarity", None),
        }
    values: dict[str, float] = {}
    found = False
    for name in ("affection", "annoyance", "familiarity"):
        raw = source.get(name) if isinstance(source, Mapping) else None
        if raw is None:
            values[name] = 0.0
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        values[name] = _clamp01(number)
        found = True
    return values if found else None


def _normalize_last_event_affect(last_event_affect) -> dict | None:
    """last_event_affect 只接受非空、非 neutral 的 affect mapping。"""
    if not isinstance(last_event_affect, Mapping):
        return None
    if affect_label(last_event_affect) == "neutral":
        return None
    return dict(last_event_affect)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectedMemory:
    """一条被选中的记忆及其打分明细（供 §5.3 强化判定与观测）。"""

    item: MemoryItem
    score: float
    r: float
    cos: float
    bm25: float  # 批内 min-max 归一化后的 BM25 值
    sim: float  # 语义可用时 = cos，否则 = bm25 归一值（§5.3 强化判定用）
    header_tokens: int = 20
    # v1.5：池标记。bridge=True 表示来自「私聊→群聊桥接池」。
    bridge: bool = False
    source_pool: str = SOURCE_POOL_DM  # dm | group | bridge（接口文档默认 dm）
    tokens: int = 0  # 装填后的 (header + content) token 成本；0 = 尚未装填
    score_detail: dict | None = None


@dataclass(frozen=True)
class RetrievalResult:
    """一次检索的结果。"""

    selected: list[SelectedMemory]
    candidates: int
    tokens: int
    budget: int
    degraded: bool
    # v1.5：装填层产物与桥接/去重摘要（selected 保留兼容）
    injected_items: list = field(default_factory=list)
    bridge_hit: int = 0  # 最终注入的桥接条目数（fill 之后）
    bridge_candidates: int = 0  # 跨池去重后、进 fill 前的桥接候选数
    bridge_truncated: bool = False
    dedup_removed: int = 0
    dedup_degraded: bool = False
    affect_bias: int = 0


@dataclass(frozen=True)
class _PoolItem:
    item: MemoryItem
    source_pool: str


@dataclass(frozen=True)
class _Pool:
    entries: list
    bridge_triggered: bool = False
    bridge_strong: bool = False
    bridge_candidates: int = 0


@dataclass(frozen=True)
class _FillOutcome:
    entries: list
    injected_items: list
    truncated: bool
    total_tokens: int
    bridge_truncated: bool
    bridge_tokens: int


# --------------------------------------------------------------------------
# 检索算法：分词 / BM25 / 归一化 / 打分辅助（模块级纯函数，便于测试）
# --------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """CJK 字符 bigram（单字 run 用单字）+ 拉丁/数字小写词（§6.2）。"""
    if not text:
        return []
    tokens: list[str] = []
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_WORD_RE.findall(text.lower()))
    return tokens


def _bm25_raw(
    query_tokens: Sequence[str],
    docs: Sequence[Sequence[str]],
    *,
    k1: float,
    b: float,
) -> list[float]:
    """对一批文档计算 BM25 原始分，返回顺序与 docs 一致。

    查询词去重后按字典序累加，保证同输入下浮点加法顺序固定（确定性排序）。
    """
    count = len(docs)
    if count == 0:
        return []
    lengths = [len(doc) for doc in docs]
    avgdl = sum(lengths) / float(count)
    try:
        k1_value = max(0.0, float(k1))
        b_value = max(0.0, min(1.0, float(b)))
    except (TypeError, ValueError):
        k1_value, b_value = 1.5, 0.75
    terms = sorted({token for token in query_tokens if token})
    if not terms:
        return [0.0] * count
    frequencies: list[dict[str, int]] = []
    for doc in docs:
        freq: dict[str, int] = {}
        for token in doc:
            freq[token] = freq.get(token, 0) + 1
        frequencies.append(freq)
    scores = [0.0] * count
    for term in terms:
        df = sum(1 for freq in frequencies if term in freq)
        # 平滑 idf：恒为正，且 df 越大权重越小
        idf = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
        for index, freq in enumerate(frequencies):
            tf = freq.get(term, 0)
            if tf <= 0:
                continue
            length_ratio = (lengths[index] / avgdl) if avgdl > 0 else 0.0
            denominator = tf + k1_value * (1.0 - b_value + b_value * length_ratio)
            if denominator <= 0:
                continue
            scores[index] += idf * (tf * (k1_value + 1.0)) / denominator
    return scores


def _minmax(values: Sequence[float]) -> list[float]:
    """批内 min-max 归一化到 [0,1]。

    max==min 的边界：全部 0 分 → 0；全部同为正分（含只有一个候选）→ 1.0。
    后者避免「单候选永远 sim=0、永不强化/复活」的退化（降级模式尤其明显）。
    """
    if not values:
        return []
    low = min(values)
    high = max(values)
    if not math.isfinite(low) or not math.isfinite(high):
        return [0.0] * len(values)
    if high == low:
        return [1.0 if high > 0 else 0.0] * len(values)
    span = high - low
    return [min(1.0, max(0.0, (value - low) / span)) for value in values]


def _has_vector(item: MemoryItem, dim: int) -> bool:
    """条目是否带有可用向量（结构校验，长度必须等于 embedder.dim）。"""
    vector = item.vector
    return isinstance(vector, (list, tuple)) and dim > 0 and len(vector) == dim


def _dot(left: Sequence[float], right: Sequence[float]) -> float | None:
    """点积（向量已 L2 归一化，点积即 cos）；脏数据返回 None。"""
    try:
        return float(sum(map(operator.mul, left, right)))
    except (TypeError, ValueError):
        pass
    try:
        # 慢路径：允许元素是数字字符串等可 float() 的脏数据
        return float(sum(float(x) * float(y) for x, y in zip(left, right)))
    except (TypeError, ValueError):
        return None


def _type_bonus(bonus_map: Mapping[str, float] | None, item_type: str) -> float:
    """type_bonus：preference 1.1 / fact 1.0 / episode 0.9，缺省 1.0（§6.2）。"""
    if not isinstance(bonus_map, Mapping):
        return 1.0
    try:
        return float(bonus_map.get(item_type, 1.0))
    except (TypeError, ValueError):
        return 1.0


# --------------------------------------------------------------------------
# §7.3 装填：fill(entries, constraints)
# --------------------------------------------------------------------------


def _resolve_constraints(constraints) -> tuple[int | None, int, int | None]:
    """解析 constraints → (total|None, head_per_item, bridge_budget|None)。

    - total 缺省 = None（不设上限）：装填层绝不硬编码 1200（§6.4 / D3）；
    - bridge 子预算只来自 `constraints["subgroup"]["bridge"]`，缺省不单独限额；
    - head_per_item 缺省 0：调用方应传 §6.4 的 24。
    """
    raw = constraints if isinstance(constraints, Mapping) else {}
    total_raw = raw.get("total")
    total: int | None = None if total_raw is None else max(0, _safe_int(total_raw, 0))
    head = max(0, _safe_int(raw.get("head_per_item"), 0))
    bridge_budget: int | None = None
    subgroup = raw.get("subgroup")
    if isinstance(subgroup, Mapping) and subgroup.get("bridge") is not None:
        bridge_budget = max(0, _safe_int(subgroup.get("bridge"), 0))
    return total, head, bridge_budget


def _normalized_pool(entry: SelectedMemory) -> str:
    if entry.bridge:
        return SOURCE_POOL_BRIDGE
    pool = str(entry.source_pool or "")
    if pool in SOURCE_POOLS:
        return pool
    return entry.item.scope


def _injected_item(entry: SelectedMemory, *, cost: int, head: int) -> dict:
    """injected item = 记忆字段 dict + bridge / source_pool / tokens（9b 直接消费）。"""
    data = entry.item.to_dict()
    data["affect"] = dict(entry.item.affect) if entry.item.affect is not None else None
    data["r"] = float(entry.r)
    data["score"] = float(entry.score)
    data["bridge"] = bool(entry.bridge)
    data["source_pool"] = _normalized_pool(entry)
    data["tokens"] = int(cost)
    data["header_tokens"] = int(head)
    return data


def _fill_entries(entries, constraints, *, metrics: Metrics | None = None) -> _FillOutcome:
    """装填核心：顺序装入，总预算 + 桥接子预算；装不下就跳过（不截断正文）。

    截断留痕（§6.4）：总量超限记 `retrieval.truncated`，桥接子预算超限记
    `group_bridge.truncated`；两者互不覆盖。
    """
    total, head, bridge_budget = _resolve_constraints(constraints)
    kept: list[SelectedMemory] = []
    injected: list[dict] = []
    tokens = 0
    bridge_tokens = 0
    truncated = False
    total_truncated = False
    bridge_truncated = False
    for entry in entries:
        if not isinstance(entry, SelectedMemory):
            raise TypeError(
                "fill() expects SelectedMemory entries, got " + type(entry).__name__
            )
        is_bridge = bool(entry.bridge) or str(entry.source_pool) == SOURCE_POOL_BRIDGE
        cost = head + estimate_tokens(entry.item.content)
        if is_bridge and bridge_budget is not None and bridge_tokens + cost > bridge_budget:
            bridge_truncated = True
            truncated = True
            continue
        if total is not None and tokens + cost > total:
            total_truncated = True
            truncated = True
            continue
        tokens += cost
        if is_bridge:
            bridge_tokens += cost
        filled = replace(entry, tokens=cost, bridge=is_bridge)
        kept.append(filled)
        injected.append(_injected_item(filled, cost=cost, head=head))
    if total_truncated:
        _event(
            metrics,
            "retrieval.truncated",
            reason="total_budget",
            budget=total,
            total_tokens=tokens,
        )
    if bridge_truncated:
        _event(
            metrics,
            "group_bridge.truncated",
            reason="bridge_budget",
            bridge_tokens=bridge_tokens,
            budget=bridge_budget,
        )
    return _FillOutcome(
        entries=kept,
        injected_items=injected,
        truncated=truncated,
        total_tokens=tokens,
        bridge_truncated=bridge_truncated,
        bridge_tokens=bridge_tokens,
    )


def fill(entries, constraints=None, *, metrics: Metrics | None = None) -> dict:
    """§7.3 装填接口（唯一入口，9b / prompt / service 都消费其产物）。

    - `entries`：已排序的 SelectedMemory 序列；
    - `constraints`：`{"total":1200, "subgroup":{"bridge":300}, "head_per_item":24}`；
      桥接预算只从 constraints 读取，本函数不硬编码；
    - 返回 `{"injected_items":[...], "truncated": bool, "total_tokens": int}`，
      injected item 是「记忆字段 dict + bridge/source_pool/tokens」，下游不得重新判断。
    超桥接预算时记 `group_bridge.truncated`（metrics 可选注入）。
    """
    outcome = _fill_entries(list(entries), constraints, metrics=metrics)
    return {
        "injected_items": outcome.injected_items,
        "truncated": outcome.truncated,
        "total_tokens": outcome.total_tokens,
    }


# --------------------------------------------------------------------------
# Retriever
# --------------------------------------------------------------------------


class Retriever:
    """记忆检索器：建池 → 打分 → 跨池去重 → 排序 → 预算装填（§6 / §7）。"""

    def __init__(self, embedder: Embedder, cfg: Config, metrics: Metrics | None = None) -> None:
        self.embedder = embedder
        self.cfg = cfg
        self.metrics = metrics

    def _event(self, name: str, **fields) -> None:
        _event(self.metrics, name, **fields)

    # ---------- 公开接口 ----------

    def search(
        self,
        items: Iterable[MemoryItem],
        query: str,
        *,
        scope: str,
        user_id: str,
        bot_state: BotState | None = None,
        now: datetime | None = None,
        user_affect=None,
        last_event_affect=None,
    ) -> RetrievalResult:
        """检索当前语境可见的记忆并按预算装填。

        `user_affect`（state.UserAffect）与 `last_event_affect`（dict）只读；
        不传时跳过 v1.5 的新增调制，保持旧调用方打分不变。
        """
        retrieval_cfg = self.cfg.retrieval
        budget = max(0, int(retrieval_cfg.budget_tokens))
        header_tokens = max(0, int(retrieval_cfg.header_tokens))
        wanted = str(scope)
        user = str(user_id)
        if wanted not in VALID_SCOPES:
            log.warning("retrieval: unknown scope %r, returning empty result", scope)
            return RetrievalResult(
                selected=[], candidates=0, tokens=0, budget=budget, degraded=False
            )

        current = now or utc_now()
        query_text = query if isinstance(query, str) else str(query or "")
        item_list = [item for item in items if isinstance(item, MemoryItem)]
        pool = self._build_pool(item_list, scope=wanted, user_id=user, query_text=query_text)
        if not pool.entries:
            return RetrievalResult(
                selected=[], candidates=0, tokens=0, budget=budget, degraded=False
            )

        if wanted == SOURCE_POOL_GROUP and pool.entries:
            self._event(
                "retrieval.pool.mixed",
                candidates=len(pool.entries),
                users=len({entry.item.user_id for entry in pool.entries}),
                rel=sum(1 for entry in pool.entries if entry.item.user_id == user),
            )

        scored, degraded, degraded_reason = self._score_pool(
            pool.entries,
            query_text=query_text,
            current=current,
            user_id=user,
            bot_state=bot_state,
            user_affect=user_affect,
            last_event_affect=last_event_affect,
            header_tokens=header_tokens,
        )
        if degraded and self.metrics is not None:
            self.metrics.event("retrieval.degraded", reason=degraded_reason)
            log.debug("retrieval degraded: %s", degraded_reason)

        # 顺序：过滤 → 打分 → 跨池去重（保留 score 高者）→ 确定性排序 → fill
        deduped, dedup_removed, dedup_degraded = self._cross_pool_dedup(scored)
        deduped.sort(key=lambda entry: (-entry.score, -entry.r, entry.item.id))

        bridge_candidates = sum(1 for entry in deduped if entry.bridge)
        constraints: dict = {
            "total": budget,
            "head_per_item": header_tokens,
            "subgroup": {},
        }
        if bridge_candidates:
            # 硬规则 18：桥接子预算 ≤300（预算值仍来自 constraints；这里只做上限约束）
            bridge_budget = min(
                MAX_BRIDGE_BUDGET_TOKENS,
                max(0, _safe_int(getattr(self.cfg.bridge, "budget_tokens", None), 0)),
            )
            constraints["subgroup"]["bridge"] = bridge_budget
        outcome = _fill_entries(deduped, constraints, metrics=self.metrics)
        selected = outcome.entries
        # §12.1：group_bridge.hit = 最终注入条数；候选数另记 candidates 字段
        bridge_hit = sum(1 for entry in selected if entry.bridge)
        if pool.bridge_triggered:
            self._event(
                "group_bridge.hit",
                count=bridge_hit,
                candidates=bridge_candidates,
                truncated=outcome.bridge_truncated,
            )
        # §6.3 / §12.1：rel=1 且 affect_match 真正改变排序（affect_sim > 0）的条数
        affect_bias = sum(
            1
            for entry in selected
            if entry.score_detail
            and entry.score_detail.get("rel")
            and (entry.score_detail.get("affect_match") or 0.0) > 0.0
        )
        return RetrievalResult(
            selected=selected,
            candidates=len(deduped),
            tokens=outcome.total_tokens,
            budget=budget,
            degraded=degraded,
            injected_items=outcome.injected_items,
            bridge_hit=bridge_hit,
            bridge_candidates=bridge_candidates,
            bridge_truncated=outcome.bridge_truncated,
            dedup_removed=dedup_removed,
            dedup_degraded=dedup_degraded,
            affect_bias=affect_bias,
        )

    # ---------- 建池 ----------

    def _build_pool(
        self,
        items: Sequence[MemoryItem],
        *,
        scope: str,
        user_id: str,
        query_text: str,
    ) -> _Pool:
        if scope == SOURCE_POOL_GROUP:
            # §7.4 群聊混合池：scope=group 全部参与，不按 user_id 预过滤；rel 在打分时判定
            entries = [
                _PoolItem(item, SOURCE_POOL_GROUP)
                for item in items
                if item.scope == SOURCE_POOL_GROUP
            ]
            return _Pool(entries=entries)

        entries = [
            _PoolItem(item, SOURCE_POOL_DM)
            for item in items
            if item.scope == SOURCE_POOL_DM and item.user_id == user_id
        ]
        decision: BridgeDecision = should_bridge(query_text, self.cfg.bridge)
        if not decision.triggered:
            return _Pool(entries=entries)

        self._event(
            "group_bridge.triggered.strong"
            if decision.strong
            else "group_bridge.triggered.weak",
            reason=decision.reason,
            matched=list(decision.matched),
        )
        bridge_entries: list[_PoolItem] = []
        try:
            for item in items:
                if item.scope != SOURCE_POOL_GROUP or item.status != "active":
                    continue
                if str(item.user_id) != user_id:
                    # 别人群聊记忆天然不进入候选（正常过滤，不是数据异常）
                    continue
                bridge_entries.append(_PoolItem(item, SOURCE_POOL_BRIDGE))
        except Exception as exc:  # 桥接失败只退化回 dm 池，不影响主检索
            self._event("group_bridge.fail", exc_type=type(exc).__name__)
            return _Pool(entries=entries, bridge_triggered=True, bridge_strong=decision.strong)
        # §3.3 / §12.1：桥接候选必须 item.user_id == 当前私聊对象，防御性校验并丢弃
        verified: list[_PoolItem] = []
        mismatched = 0
        for entry in bridge_entries:
            if str(entry.item.user_id) != user_id:
                mismatched += 1
                continue
            verified.append(entry)
        if mismatched:
            self._event("group_bridge.userid_mismatch", count=mismatched)
        bridge_entries = verified
        return _Pool(
            entries=entries + bridge_entries,
            bridge_triggered=True,
            bridge_strong=decision.strong,
            bridge_candidates=len(bridge_entries),
        )

    def _cross_pool_dedup(self, entries: Sequence[SelectedMemory]) -> tuple[list, int, bool]:
        """§6.2 / §6.7 跨池去重（在打分之后执行）：dedup_key 精确优先，余弦 >0.9 仅作
        二次判定；重复对**保留 score 高者**（被去掉的可能是 dm 侧或 bridge 侧）。

        向量不可用时退化为原文（normalize_text）匹配并记 `dedup.degraded`；
        被去掉的条数记 `group_bridge.dedup`（两个指标分开）。
        """
        dm_entries = [entry for entry in entries if entry.source_pool == SOURCE_POOL_DM]
        bridge_entries = [
            entry for entry in entries if entry.source_pool == SOURCE_POOL_BRIDGE
        ]
        if not dm_entries or not bridge_entries:
            return list(entries), 0, False

        dedupe_sim = _safe_float(self.cfg.decay.dedupe_sim, 0.9)
        dm_by_key: dict = {}
        dm_by_text: dict = {}
        for entry in dm_entries:
            item = entry.item
            dm_by_key.setdefault(
                dedup_key({"type": item.type, "content": item.content}), []
            ).append(entry)
            dm_by_text.setdefault(normalize_text(item.content), []).append(entry)
        try:
            dim = int(getattr(self.embedder, "dim", 0) or 0)
            embedder_ok = bool(self.embedder.available)
        except Exception:  # pragma: no cover - 编码器异常按不可用处理
            dim, embedder_ok = 0, False

        degraded = False
        pairs: list = []
        # 按桥接条目得分从高到低配对，保证确定性
        ordered_bridge = sorted(
            bridge_entries, key=lambda entry: (-entry.score, -entry.r, entry.item.id)
        )
        for bridge in ordered_bridge:
            item = bridge.item
            exact = dm_by_key.get(dedup_key({"type": item.type, "content": item.content}))
            matches = list(exact) if exact else []
            if not matches:
                compared = False
                if embedder_ok and _has_vector(item, dim):
                    for candidate in dm_entries:
                        if not _has_vector(candidate.item, dim):
                            continue
                        value = _dot(candidate.item.vector, item.vector)
                        if value is None:
                            continue
                        compared = True
                        if value > dedupe_sim:
                            matches.append(candidate)
                if not matches and not compared:
                    # 向量不可用 → 原文匹配（并留痕，说明去重质量降级）
                    degraded = True
                    matches = list(dm_by_text.get(normalize_text(item.content), []))
            if not matches:
                continue
            best_match = max(matches, key=lambda entry: (entry.score, entry.r, entry.item.id))
            pairs.append((best_match, bridge))

        removed = 0
        removed_ids: set = set()
        for dm_entry, bridge in pairs:
            if id(dm_entry) in removed_ids or id(bridge) in removed_ids:
                continue
            # §6.2：重复对保留 score 高者；同分保留 dm（主池）
            if bridge.score > dm_entry.score:
                removed_ids.add(id(dm_entry))
            else:
                removed_ids.add(id(bridge))
            removed += 1
        kept = [entry for entry in entries if id(entry) not in removed_ids]
        if removed:
            self._event("group_bridge.dedup", count=removed)
        if degraded:
            self._event(
                "dedup.degraded",
                reason=DEGRADED_DEDUP_NO_VECTORS,
                pool=SOURCE_POOL_BRIDGE,
            )
        return kept, removed, degraded

    # ---------- 打分 ----------

    def _score_pool(
        self,
        entries: Sequence[_PoolItem],
        *,
        query_text: str,
        current: datetime,
        user_id: str,
        bot_state: BotState | None,
        user_affect,
        last_event_affect,
        header_tokens: int,
    ) -> tuple[list[SelectedMemory], bool, str]:
        retrieval_cfg = self.cfg.retrieval
        items = [entry.item for entry in entries]
        query_tokens = _tokenize(query_text)
        docs = [_tokenize(item.content) for item in items]
        bm25_scores = _minmax(
            _bm25_raw(
                query_tokens,
                docs,
                k1=retrieval_cfg.bm25_k1,
                b=retrieval_cfg.bm25_b,
            )
        )

        cos_scores, degraded, degraded_reason = self._semantic_scores(items, query_text)
        w_vector = float(retrieval_cfg.w_vector)
        w_bm25 = float(retrieval_cfg.w_bm25)
        if degraded:
            # §6.5：向量不可用 → cos 权重置 0、bm25 权重置 1
            w_vector, w_bm25 = 0.0, 1.0

        affect_values = _user_affect_values(user_affect)
        last_affect = _normalize_last_event_affect(last_event_affect)
        familiarity_eta = _safe_float(retrieval_cfg.familiarity_eta, 0.1)
        gamma_u = _safe_float(retrieval_cfg.gamma_u, 0.15)
        gamma_a = _safe_float(retrieval_cfg.gamma_a, 0.1)
        affect_match_enabled = bool(getattr(retrieval_cfg, "affect_match", False))
        # 硬规则 18：桥接权重 ≤0.7
        bridge_weight = min(
            MAX_BRIDGE_WEIGHT,
            max(
                0.0,
                _safe_float(
                    getattr(self.cfg.bridge, "weight", DEFAULT_BRIDGE_WEIGHT),
                    DEFAULT_BRIDGE_WEIGHT,
                ),
            ),
        )

        selected_pool: list[SelectedMemory] = []
        for index, entry in enumerate(entries):
            item = entry.item
            r = self._retention(item, current, bot_state)
            rel = item.user_id == user_id
            base = w_vector * cos_scores[index] + w_bm25 * bm25_scores[index]
            type_bonus = _type_bonus(retrieval_cfg.type_bonus, item.type)
            detail: dict = {
                "base": base,
                "cos": cos_scores[index],
                "bm25": bm25_scores[index],
                "r": r,
                "rel": rel,
                "pool": entry.source_pool,
                "type_bonus": type_bonus,
            }
            if rel and affect_values is not None:
                # §7.2：rel=1 时 type_bonus × (1 + η·familiarity)
                familiarity_factor = 1.0 + familiarity_eta * affect_values["familiarity"]
                type_bonus *= familiarity_factor
                detail["familiarity"] = familiarity_factor
            score = base * r * type_bonus
            if item.status == "dormant":
                dormant_factor = _safe_float(retrieval_cfg.dormant_factor, 0.3)
                score *= dormant_factor
                detail["dormant"] = dormant_factor
            if retrieval_cfg.mood_bias_enabled and bot_state is not None:
                mood_factor = lifecycle.mood_bias_factor(
                    bot_state.valence,
                    item.valence,
                    _safe_float(retrieval_cfg.mood_bias_beta, 0.2),
                )
                score *= mood_factor
                detail["mood"] = mood_factor
            if rel and affect_values is not None:
                # §7.2：rel=1 时 × (1 + γ_u·(affection − annoyance))
                user_affect_factor = 1.0 + gamma_u * (
                    affect_values["affection"] - affect_values["annoyance"]
                )
                score *= user_affect_factor
                detail["user_affect"] = user_affect_factor
            if affect_match_enabled and last_affect is not None:
                # §7.2：affect_match 开启且 last_event_affect 非空时 × (1 + γ_a·affect_sim)
                affect_sim = affect_similarity(item.affect, last_affect)
                affect_factor = 1.0 + gamma_a * affect_sim
                score *= affect_factor
                detail["affect_match"] = affect_sim
                detail["affect_match_factor"] = affect_factor
            if entry.source_pool == SOURCE_POOL_BRIDGE:
                # §7.6：桥接命中低权重
                score *= bridge_weight
                detail["bridge"] = bridge_weight
            sim = cos_scores[index] if not degraded else bm25_scores[index]
            selected_pool.append(
                SelectedMemory(
                    item=item,
                    score=score,
                    r=r,
                    cos=cos_scores[index],
                    bm25=bm25_scores[index],
                    sim=sim,
                    header_tokens=header_tokens,
                    bridge=entry.source_pool == SOURCE_POOL_BRIDGE,
                    source_pool=entry.source_pool,
                    score_detail=detail,
                )
            )

        # 确定性排序：score 降序 → r 降序 → id 升序（§6.2）
        selected_pool.sort(key=lambda entry: (-entry.score, -entry.r, entry.item.id))
        return selected_pool, degraded, degraded_reason

    # ---------- 内部步骤 ----------

    @staticmethod
    def _filter(items: Iterable[MemoryItem], *, scope: str, user_id: str) -> list[MemoryItem]:
        """§3.2 先过滤（兼容旧调用）：群聊只看 group；私聊只看同 user 的 dm。

        新检索入口用 `_build_pool`（群聊混合池 + 桥接池），本方法保留给旧调用方。
        """
        wanted = str(scope)
        if wanted not in VALID_SCOPES:
            log.warning("retrieval: unknown scope %r, returning empty result", scope)
            return []
        user = str(user_id)
        candidates: list[MemoryItem] = []
        for item in items:
            if not isinstance(item, MemoryItem):
                continue
            if wanted == SOURCE_POOL_GROUP:
                if item.scope == SOURCE_POOL_GROUP:
                    candidates.append(item)
            elif item.scope == SOURCE_POOL_DM and item.user_id == user:
                candidates.append(item)
        return candidates

    def _semantic_scores(
        self, candidates: Sequence[MemoryItem], query_text: str
    ) -> tuple[list[float], bool, str]:
        """计算 cos 分；返回 (cos 列表, 是否降级, 降级原因)。"""
        scores = [0.0] * len(candidates)
        if not self.embedder.available:
            return scores, True, DEGRADED_EMBEDDER_UNAVAILABLE
        dim = int(getattr(self.embedder, "dim", 0) or 0)
        has_vector = [_has_vector(item, dim) for item in candidates]
        if not any(has_vector):
            return scores, True, DEGRADED_NO_VECTORS
        try:
            query_vector = self._encode_query(query_text)
        except Exception as exc:  # query 编码失败同样按降级处理（§6.5）
            log.warning("retrieval: query embedding failed, fallback to bm25: %r", exc)
            return scores, True, DEGRADED_ENCODE_FAILED
        for index, item in enumerate(candidates):
            if not has_vector[index]:
                continue  # 缺 vector 的条目 cos=0
            value = _dot(query_vector, item.vector)
            scores[index] = value if value is not None else 0.0
        return scores, False, ""

    def _encode_query(self, query_text: str) -> list[float]:
        encoded = self.embedder.encode([query_text])
        if not encoded:
            raise EmbeddingUnavailable("embedding: empty encode result for query")
        try:
            vector = [float(value) for value in encoded[0]]
        except (TypeError, ValueError) as exc:
            raise EmbeddingUnavailable(f"embedding: invalid query vector: {exc}") from exc
        dim = int(getattr(self.embedder, "dim", 0) or 0)
        if dim > 0 and len(vector) != dim:
            raise EmbeddingUnavailable(
                f"embedding: query vector dim {len(vector)} != embedder dim {dim}"
            )
        return vector

    def _retention(self, item: MemoryItem, now: datetime, bot_state: BotState | None) -> float:
        """§5.1 / §6.2：R 用 lifecycle.retention_now（含 §5.2 压抑调制）。"""
        avoidance = float(bot_state.avoidance) if bot_state is not None else 0.0
        try:
            return lifecycle.retention_now(
                item,
                now=now,
                avoidance_now=avoidance,
                cfg=self.cfg.decay,
            )
        except (ValueError, TypeError, OverflowError) as exc:
            # 单条时间戳损坏不应拖垮整轮检索：按 R=0 参与排序（与极旧记忆同效）
            log.warning("retrieval: bad timestamps for memory %r: %r", item.id, exc)
            return 0.0
