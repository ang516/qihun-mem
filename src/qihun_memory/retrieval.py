"""检索（v1.4 §6.2 / §6.3 / §6.5）。

顺序固定：scope + user_id 先过滤 → BM25（批内 min-max 归一化）→ 语义 cos
（embedder 不可用 / 缺向量时降级 weights=(0,1)）→ R（lifecycle.retention_now）
→ 单公式打分 → 确定性排序 → 预算装填。

检索只读：不修改传入的 MemoryItem，不写盘；不得因为单条脏数据抛异常到主流程。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from . import lifecycle
from .config import Config
from .embedding import EmbeddingUnavailable, Embedder
from .metrics import Metrics
from .models import MemoryItem, utc_now
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

_VALID_SCOPES = ("group", "dm")


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


@dataclass(frozen=True)
class RetrievalResult:
    """一次检索的结果。"""

    selected: list[SelectedMemory]
    candidates: int
    tokens: int
    budget: int
    degraded: bool


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
    """批内 min-max 归一化到 [0,1]；max==min（含空批）时全部为 0。"""
    if not values:
        return []
    low = min(values)
    high = max(values)
    if not math.isfinite(low) or not math.isfinite(high) or high == low:
        return [0.0] * len(values)
    span = high - low
    return [min(1.0, max(0.0, (value - low) / span)) for value in values]


def _has_vector(item: MemoryItem, dim: int) -> bool:
    """条目是否带有可用向量（结构校验，长度必须等于 embedder.dim）。"""
    vector = item.vector
    return isinstance(vector, (list, tuple)) and dim > 0 and len(vector) == dim


def _dot(left: Sequence[float], right: Sequence[float]) -> float | None:
    """点积（向量已 L2 归一化，点积即 cos）；脏数据返回 None。"""
    try:
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
# Retriever
# --------------------------------------------------------------------------


class Retriever:
    """记忆检索器：过滤 → 打分 → 排序 → 预算装填（§6.2 / §6.3）。"""

    def __init__(self, embedder: Embedder, cfg: Config, metrics: Metrics | None = None) -> None:
        self.embedder = embedder
        self.cfg = cfg
        self.metrics = metrics

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
    ) -> RetrievalResult:
        """检索当前语境可见的记忆并按预算装填。"""
        retrieval_cfg = self.cfg.retrieval
        budget = int(retrieval_cfg.budget_tokens)
        header_tokens = max(0, int(retrieval_cfg.header_tokens))

        candidates = self._filter(items, scope=scope, user_id=user_id)
        if not candidates:
            return RetrievalResult(selected=[], candidates=0, tokens=0, budget=budget, degraded=False)

        current = now or utc_now()
        query_text = query if isinstance(query, str) else str(query or "")
        query_tokens = _tokenize(query_text)
        docs = [_tokenize(item.content) for item in candidates]
        bm25_scores = _minmax(
            _bm25_raw(
                query_tokens,
                docs,
                k1=retrieval_cfg.bm25_k1,
                b=retrieval_cfg.bm25_b,
            )
        )

        cos_scores, degraded, degraded_reason = self._semantic_scores(candidates, query_text)
        if degraded and self.metrics is not None:
            self.metrics.event("retrieval.degraded", reason=degraded_reason)
            log.debug("retrieval degraded: %s", degraded_reason)

        w_vector = float(retrieval_cfg.w_vector)
        w_bm25 = float(retrieval_cfg.w_bm25)
        if degraded:
            # §6.5：向量不可用 → cos 权重置 0、bm25 权重置 1
            w_vector, w_bm25 = 0.0, 1.0

        selected_pool: list[SelectedMemory] = []
        for index, item in enumerate(candidates):
            r = self._retention(item, current, bot_state)
            base = w_vector * cos_scores[index] + w_bm25 * bm25_scores[index]
            bonus = _type_bonus(retrieval_cfg.type_bonus, item.type)
            score = base * r * bonus
            if item.status == "dormant":
                score *= float(retrieval_cfg.dormant_factor)
            if retrieval_cfg.mood_bias_enabled and bot_state is not None:
                score *= lifecycle.mood_bias_factor(
                    bot_state.valence,
                    item.valence,
                    float(retrieval_cfg.mood_bias_beta),
                )
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
                )
            )

        # 确定性排序：score 降序 → r 降序 → id 升序（§6.2）
        selected_pool.sort(key=lambda entry: (-entry.score, -entry.r, entry.item.id))

        selected: list[SelectedMemory] = []
        tokens = 0
        for entry in selected_pool:
            cost = entry.header_tokens + estimate_tokens(entry.item.content)
            if tokens + cost > budget:
                continue  # 装不下就跳过该条继续，不截断正文（§6.3）
            selected.append(entry)
            tokens += cost

        return RetrievalResult(
            selected=selected,
            candidates=len(candidates),
            tokens=tokens,
            budget=budget,
            degraded=degraded,
        )

    # ---------- 内部步骤 ----------

    @staticmethod
    def _filter(items: Iterable[MemoryItem], *, scope: str, user_id: str) -> list[MemoryItem]:
        """§3.2 先过滤：群聊只看 group；私聊只看同 user 的 dm。"""
        wanted = str(scope)
        if wanted not in _VALID_SCOPES:
            log.warning("retrieval: unknown scope %r, returning empty result", scope)
            return []
        user = str(user_id)
        candidates: list[MemoryItem] = []
        for item in items:
            if not isinstance(item, MemoryItem):
                continue
            if wanted == "group":
                if item.scope == "group":
                    candidates.append(item)
            elif item.scope == "dm" and item.user_id == user:
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
