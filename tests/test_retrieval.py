"""retrieval.py 测试（v1.4 §6.2 / §6.3 / §6.5）。

覆盖：scope+user_id 过滤、BM25（CJK bigram + 拉丁词、批内归一化）、语义 cos、
降级（NullEmbedder / 缺向量 / 编码失败）、R 衰减与压抑、打分系数（type_bonus /
dormant / 心境一致性）、确定性排序、预算装填与只读性。

测试自包含：FakeEmbedder 只在测试内定义，不依赖 sentence-transformers、不联网。
"""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from qihun_memory import lifecycle
from qihun_memory.bridge import BridgeDecision, should_bridge
from qihun_memory.config import BridgeConfig, Config, DecayConfig, RetrievalConfig
from qihun_memory.embedding import Embedder, NullEmbedder
from qihun_memory.metrics import Metrics
from qihun_memory.models import MemoryItem, utc_now_iso
from qihun_memory.retrieval import (
    DEGRADED_EMBEDDER_UNAVAILABLE,
    DEGRADED_ENCODE_FAILED,
    DEGRADED_NO_VECTORS,
    SOURCE_POOL_BRIDGE,
    SOURCE_POOL_DM,
    SOURCE_POOL_GROUP,
    RetrievalResult,
    Retriever,
    SelectedMemory,
    _bm25_raw,
    _minmax,
    _tokenize,
    _type_bonus,
    affect_similarity,
    fill,
)
from qihun_memory.state import BotState, UserAffect
from qihun_memory.tokens import estimate_tokens

NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 测试替身与工厂（自包含，不依赖 conftest）
# --------------------------------------------------------------------------


class FakeEmbedder(Embedder):
    """确定性假编码器：按文本查表返回 L2 归一化向量。"""

    name = "fake"

    def __init__(
        self,
        vectors: dict[str, list[float]] | None = None,
        *,
        dim: int = 4,
        available: bool = True,
        fail: bool = False,
    ) -> None:
        self._vectors = dict(vectors or {})
        self.dim = dim
        self._available = available
        self.fail = fail
        self.calls: list[list[str]] = []
        default = [1.0] + [0.0] * (dim - 1) if dim > 0 else []
        self._default = default

    @property
    def available(self) -> bool:
        return self._available

    def encode(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("encode boom")
        result: list[list[float]] = []
        for text in texts:
            vector = [float(value) for value in self._vectors.get(text, self._default)]
            norm = math.sqrt(sum(value * value for value in vector))
            result.append([value / norm for value in vector] if norm > 0 else vector)
        return result


def make_item(
    item_id: str,
    content: str,
    *,
    scope: str = "group",
    user_id: str = "u1",
    item_type: str = "fact",
    age_days: float = 0.0,
    created_age_days: float = 90.0,
    stability: float = 30.0,
    valence: float = 0.0,
    status: str = "active",
    vector: list[float] | None = None,
    affect: dict | None = None,
) -> MemoryItem:
    last_access = (NOW - timedelta(days=age_days)).isoformat(timespec="seconds")
    created_at = (NOW - timedelta(days=created_age_days)).isoformat(timespec="seconds")
    return MemoryItem(
        id=item_id,
        user_id=user_id,
        scope=scope,
        type=item_type,
        content=content,
        created_at=created_at,
        last_access=last_access,
        S=stability,
        valence=valence,
        status=status,
        vector=vector,
        affect=affect,
    )


def make_retriever(
    embedder: Embedder | None = None,
    *,
    retrieval: RetrievalConfig | None = None,
    decay: DecayConfig | None = None,
    metrics: Metrics | None = None,
    bridge: BridgeConfig | None = None,
) -> tuple[Retriever, Config]:
    cfg = Config(
        retrieval=retrieval or RetrievalConfig(),
        decay=decay or DecayConfig(),
        bridge=bridge or BridgeConfig(),
    )
    return Retriever(embedder if embedder is not None else NullEmbedder(), cfg, metrics), cfg


def search(
    retriever: Retriever,
    items,
    query: str = "",
    *,
    scope: str = "group",
    user_id: str = "u1",
    bot_state: BotState | None = None,
    user_affect: UserAffect | None = None,
    last_event_affect: dict | None = None,
):
    return retriever.search(
        items,
        query,
        scope=scope,
        user_id=user_id,
        bot_state=bot_state,
        now=NOW,
        user_affect=user_affect,
        last_event_affect=last_event_affect,
    )


def ids(result: RetrievalResult) -> list[str]:
    return [entry.item.id for entry in result.selected]


def degraded_reason(tmp_path, embedder, items, query: str = "上海") -> str:
    """跑一次检索并读回 metric 里的降级原因（顺带断言只记一次）。"""
    log_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=log_path)
    retriever, _ = make_retriever(embedder=embedder, metrics=metrics)
    search(retriever, items, query)
    assert metrics.count("retrieval.degraded") == 1
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [record for record in records if record.get("event") == "retrieval.degraded"]
    assert len(events) == 1
    return events[0]["reason"]


# --------------------------------------------------------------------------
# 1. scope + user_id 先过滤（§3.2）
# --------------------------------------------------------------------------


def test_scope_isolation_between_group_and_dm():
    items = [
        make_item("g1", "上海出差", scope="group", user_id="u1"),
        make_item("g2", "上海出差", scope="group", user_id="u2"),
        make_item("d1", "上海出差", scope="dm", user_id="u1"),
        make_item("d2", "上海出差", scope="dm", user_id="u2"),
    ]
    retriever, _ = make_retriever()

    group_result = search(retriever, items, "上海", scope="group")
    assert group_result.candidates == 2
    assert ids(group_result) == ["g1", "g2"]

    # dm 只检索同一 user：别的用户私聊记忆不可见
    dm_result = search(retriever, items, "上海", scope="dm", user_id="u1")
    assert dm_result.candidates == 1
    assert ids(dm_result) == ["d1"]


def test_unknown_scope_returns_empty_result():
    retriever, _ = make_retriever()
    result = search(retriever, [make_item("g1", "上海出差")], "上海", scope="other")
    assert result == RetrievalResult(selected=[], candidates=0, tokens=0, budget=1200, degraded=False)


def test_empty_candidates_short_circuit():
    retriever, cfg = make_retriever(embedder=NullEmbedder())
    result = search(retriever, [], "上海")
    assert result.selected == []
    assert result.candidates == 0
    assert result.tokens == 0
    assert result.budget == cfg.retrieval.budget_tokens
    assert result.degraded is False


def test_none_memory_items_in_input_are_ignored():
    retriever, _ = make_retriever()
    items = [make_item("g1", "上海出差"), None, "not-a-memory"]  # type: ignore[list-item]
    result = search(retriever, items, "上海")
    assert result.candidates == 1
    assert ids(result) == ["g1"]


# --------------------------------------------------------------------------
# 2. BM25：分词、公式、批内 min-max 归一化
# --------------------------------------------------------------------------


def test_tokenize_cjk_bigram_and_latin_words():
    assert _tokenize("上海出差") == ["上海", "海出", "出差"]
    assert _tokenize("好") == ["好"]  # 单字 run 用单字
    assert _tokenize("上海 好") == ["上海", "好"]
    assert _tokenize("ABC Shanghai 2024") == ["abc", "shanghai", "2024"]
    assert _tokenize("") == []


def test_bm25_raw_matches_hand_computed_formula():
    query = ["apple"]
    docs = [["apple", "apple"], ["apple"]]
    raw = _bm25_raw(query, docs, k1=1.5, b=0.75)
    assert raw[0] == pytest.approx(0.23525362166961883)
    assert raw[1] == pytest.approx(0.21449594916935832)
    assert raw[0] > raw[1]  # tf 更高但文档更长，仍占优

    # b=0 关闭长度归一化：长文档相对受益、短文档失去长度优势
    flat = _bm25_raw(query, docs, k1=1.5, b=0.0)
    assert flat[0] == pytest.approx(0.26045936684850656)
    assert flat[1] == pytest.approx(0.1823215567939546)
    assert flat[0] > raw[0]
    assert flat[1] < raw[1]


def test_minmax_normalization_boundaries():
    assert _minmax([]) == []
    assert _minmax([0.0, 0.0]) == [0.0, 0.0]  # max == min == 0
    assert _minmax([2.0, 2.0]) == [1.0, 1.0]  # 同为正分（含单候选）→ 1.0
    normalized = _minmax([1.0, 2.0, 3.0])
    assert normalized == pytest.approx([0.0, 0.5, 1.0])
    assert all(0.0 <= value <= 1.0 for value in normalized)


def test_bm25_batch_normalization_in_search():
    items = [make_item("match", "上海出差"), make_item("other", "深圳旅游")]
    retriever, _ = make_retriever()
    result = search(retriever, items, "上海")
    by_id = {entry.item.id: entry for entry in result.selected}
    assert by_id["match"].bm25 == pytest.approx(1.0)
    assert by_id["other"].bm25 == pytest.approx(0.0)


def test_bm25_single_candidate_max_equals_min_is_one():
    retriever, _ = make_retriever()
    result = search(retriever, [make_item("only", "上海出差")], "上海")
    assert len(result.selected) == 1
    # 单候选同为正分 → 归一化 1.0，避免降级模式永不强化/复活（B5）
    assert result.selected[0].bm25 == 1.0
    assert result.selected[0].score > 0.0
    assert result.selected[0].sim == 1.0


def test_latin_query_is_case_insensitive():
    items = [make_item("en", "Shanghai trip"), make_item("zh", "上海出差")]
    retriever, _ = make_retriever()
    result = search(retriever, items, "SHANGHAI")
    assert ids(result)[0] == "en"
    assert result.selected[0].bm25 == pytest.approx(1.0)

    result_zh = search(retriever, items, "上海")
    assert ids(result_zh)[0] == "zh"


# --------------------------------------------------------------------------
# 3. 打分公式：type_bonus / dormant / 心境一致性 / R（§6.2）
# --------------------------------------------------------------------------


def test_type_bonus_ranking_and_ratios():
    items = [
        make_item("pref", "上海出差", item_type="preference"),
        make_item("fact", "上海出差", item_type="fact"),
        make_item("epi", "上海出差", item_type="episode"),
        make_item("filler", "深圳旅游", item_type="fact"),
    ]
    retriever, _ = make_retriever()
    result = search(retriever, items, "上海")
    assert ids(result) == ["pref", "fact", "epi", "filler"]
    scores = {entry.item.id: entry.score for entry in result.selected}
    assert scores["pref"] == pytest.approx(scores["fact"] * 1.1)
    assert scores["epi"] == pytest.approx(scores["fact"] * 0.9)
    assert result.selected[0].header_tokens == 24  # §7.3 默认 head_per_item


def test_type_bonus_helper_defaults_to_one():
    assert _type_bonus({"preference": 1.1, "fact": 1.0, "episode": 0.9}, "preference") == pytest.approx(1.1)
    assert _type_bonus({"preference": 1.1}, "episode") == 1.0
    assert _type_bonus(None, "fact") == 1.0
    assert _type_bonus({}, "fact") == 1.0
    assert _type_bonus({"fact": "bad"}, "fact") == 1.0


def test_dormant_factor_multiplies_score_by_0_3():
    items = [
        make_item("active", "上海出差"),
        make_item("dormant", "上海出差", status="dormant"),
        make_item("filler", "深圳旅游"),
    ]
    retriever, _ = make_retriever()
    result = search(retriever, items, "上海")
    scores = {entry.item.id: entry.score for entry in result.selected}
    assert scores["dormant"] == pytest.approx(scores["active"] * 0.3)
    assert ids(result)[0] == "active"


def test_mood_bias_same_neutral_opposite_valence():
    items = [
        make_item("pos", "上海出差", valence=0.5),
        make_item("neu", "上海出差", valence=0.0),
        make_item("neg", "上海出差", valence=-0.5),
        make_item("filler", "深圳旅游"),
    ]
    retriever, _ = make_retriever()
    result = search(retriever, items, "上海", bot_state=BotState(valence=0.5))
    scores = {entry.item.id: entry.score for entry in result.selected}
    # β=0.2：同号 ×1.2、中性 ×1.0、异号 ×0.8
    assert scores["pos"] == pytest.approx(scores["neu"] * 1.2)
    assert scores["neg"] == pytest.approx(scores["neu"] * 0.8)
    assert ids(result)[:3] == ["pos", "neu", "neg"]


def test_mood_bias_can_be_disabled_by_config():
    items = [
        make_item("pos", "上海出差", valence=0.5),
        make_item("neg", "上海出差", valence=-0.5),
    ]
    retriever, _ = make_retriever(
        retrieval=RetrievalConfig(mood_bias_enabled=False)
    )
    result = search(retriever, items, "上海", bot_state=BotState(valence=0.5))
    scores = [entry.score for entry in result.selected]
    assert scores[0] == pytest.approx(scores[1])


def test_retention_uses_lifecycle_retention_now():
    fresh = make_item("fresh", "上海出差", age_days=0)
    old = make_item("old", "上海出差", age_days=10, stability=30.0)
    items = [fresh, old, make_item("filler", "深圳旅游")]
    retriever, cfg = make_retriever()
    result = search(retriever, items, "上海")
    by_id = {entry.item.id: entry for entry in result.selected}
    expected_old_r = lifecycle.retention_now(old, now=NOW, avoidance_now=0.0, cfg=cfg.decay)
    assert by_id["old"].r == pytest.approx(expected_old_r)
    assert by_id["old"].r == pytest.approx(math.exp(-10.0 / 30.0))
    assert by_id["fresh"].r == pytest.approx(1.0)
    assert by_id["old"].score == pytest.approx(by_id["fresh"].score * math.exp(-10.0 / 30.0))


def test_bot_state_avoidance_suppresses_negative_memory_r():
    item = make_item("neg", "上海出差", valence=-0.5, age_days=5, stability=10.0)
    retriever, cfg = make_retriever()
    calm = search(retriever, [item], "上海", bot_state=BotState(valence=0.0, avoidance=0.0))
    avoid = search(retriever, [item], "上海", bot_state=BotState(valence=0.0, avoidance=0.8))
    expected = lifecycle.retention_now(item, now=NOW, avoidance_now=0.8, cfg=cfg.decay)
    assert avoid.selected[0].r == pytest.approx(expected)
    assert avoid.selected[0].r < calm.selected[0].r


def test_full_score_formula_with_all_factors():
    embedder = FakeEmbedder(vectors={"上海": [1.0, 0.0, 0.0, 0.0]}, dim=4)
    items = [
        make_item(
            "a",
            "上海出差",
            item_type="preference",
            age_days=3,
            valence=0.5,
            vector=[1.0, 0.0, 0.0, 0.0],
        ),
        make_item(
            "b",
            "上海出差",
            item_type="episode",
            age_days=3,
            valence=-0.5,
            status="dormant",
            vector=[0.0, 1.0, 0.0, 0.0],
        ),
        make_item("c", "深圳旅游", item_type="fact", age_days=3, vector=[0.0, 0.0, 0.0, 1.0]),
    ]
    retriever, cfg = make_retriever(embedder=embedder, metrics=Metrics())
    result = search(retriever, items, "上海", bot_state=BotState(valence=0.5))
    assert result.degraded is False
    by_id = {entry.item.id: entry for entry in result.selected}
    r = math.exp(-3.0 / 30.0)

    expected_a = (0.6 * 1.0 + 0.4 * 1.0) * r * 1.1 * 1.2
    assert by_id["a"].cos == pytest.approx(1.0)
    assert by_id["a"].bm25 == pytest.approx(1.0)
    assert by_id["a"].r == pytest.approx(r)
    assert by_id["a"].score == pytest.approx(expected_a)
    assert by_id["a"].sim == pytest.approx(by_id["a"].cos)

    expected_b = (0.6 * 0.0 + 0.4 * 1.0) * r * 0.9 * 0.3 * 0.8
    assert by_id["b"].cos == pytest.approx(0.0)
    assert by_id["b"].score == pytest.approx(expected_b)

    assert by_id["c"].score == pytest.approx(0.0)
    assert ids(result) == ["a", "b", "c"]
    assert result.tokens == sum(
        entry.header_tokens + estimate_tokens(entry.item.content) for entry in result.selected
    )


def test_configured_semantic_weights_are_applied():
    embedder = FakeEmbedder(vectors={"上海": [1.0, 0.0, 0.0, 0.0]}, dim=4)
    items = [
        make_item("cos", "上海出差", vector=[1.0, 0.0, 0.0, 0.0]),
        make_item("bm25", "上海出差", vector=[0.0, 1.0, 0.0, 0.0]),
    ]
    retriever, _ = make_retriever(
        embedder=embedder,
        retrieval=RetrievalConfig(w_vector=1.0, w_bm25=0.0),
    )
    result = search(retriever, items, "上海")
    by_id = {entry.item.id: entry for entry in result.selected}
    assert by_id["cos"].score == pytest.approx(by_id["cos"].r)
    assert by_id["bm25"].score == pytest.approx(0.0)


# --------------------------------------------------------------------------
# 4. 语义 cos 与降级（§6.5）
# --------------------------------------------------------------------------


def test_null_embedder_degrades_to_pure_bm25_and_records_metric(tmp_path):
    items = [make_item("a", "上海出差"), make_item("b", "深圳旅游")]
    log_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=log_path)
    retriever, _ = make_retriever(embedder=NullEmbedder(), metrics=metrics)
    result = search(retriever, items, "上海")

    assert result.degraded is True
    assert metrics.count("retrieval.degraded") == 1
    for entry in result.selected:
        assert entry.cos == 0.0
        assert entry.sim == pytest.approx(entry.bm25)
        assert entry.score == pytest.approx(entry.bm25 * entry.r * 1.0)  # fact bonus 1.0

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [record for record in records if record.get("event") == "retrieval.degraded"]
    assert len(events) == 1
    assert events[0]["reason"] == DEGRADED_EMBEDDER_UNAVAILABLE


def test_partial_vectors_keep_semantic_mode():
    embedder = FakeEmbedder(vectors={"上海": [1.0, 0.0, 0.0, 0.0]}, dim=4)
    items = [
        make_item("full", "上海出差", vector=[1.0, 0.0, 0.0, 0.0]),
        make_item("missing", "上海出差", vector=None),
        make_item("wrong_dim", "上海出差", vector=[1.0, 0.0]),
    ]
    metrics = Metrics()
    retriever, _ = make_retriever(embedder=embedder, metrics=metrics)
    result = search(retriever, items, "上海")

    assert result.degraded is False
    assert metrics.count("retrieval.degraded") == 0
    by_id = {entry.item.id: entry for entry in result.selected}
    assert by_id["full"].cos == pytest.approx(1.0)
    assert by_id["missing"].cos == 0.0
    assert by_id["wrong_dim"].cos == 0.0
    for entry in result.selected:
        assert entry.sim == pytest.approx(entry.cos)


def test_no_usable_vector_degrades(tmp_path):
    items = [make_item("a", "上海出差"), make_item("b", "上海出差")]
    embedder = FakeEmbedder(dim=4)
    reason = degraded_reason(tmp_path, embedder, items)
    assert reason == DEGRADED_NO_VECTORS
    assert embedder.calls == []  # 没有可用向量时不调用编码器

    metrics = Metrics()
    retriever, _ = make_retriever(embedder=embedder, metrics=metrics)
    result = search(retriever, items, "上海")
    assert result.degraded is True
    assert all(entry.cos == 0.0 for entry in result.selected)


def test_embedder_encode_failure_degrades(tmp_path):
    items = [make_item("a", "上海出差", vector=[1.0, 0.0, 0.0, 0.0])]
    embedder = FakeEmbedder(dim=4, fail=True)
    reason = degraded_reason(tmp_path, embedder, items)
    assert reason == DEGRADED_ENCODE_FAILED

    metrics = Metrics()
    retriever, _ = make_retriever(embedder=embedder, metrics=metrics)
    result = search(retriever, items, "上海")
    assert result.degraded is True
    assert len(result.selected) == 1
    assert result.selected[0].cos == 0.0


def test_embedder_unavailable_takes_precedence_over_existing_vectors(tmp_path):
    items = [make_item("a", "上海出差", vector=[1.0, 0.0, 0.0, 0.0])]
    reason = degraded_reason(tmp_path, NullEmbedder(), items)
    assert reason == DEGRADED_EMBEDDER_UNAVAILABLE


# --------------------------------------------------------------------------
# 5. 预算装填（§6.3）
# --------------------------------------------------------------------------


def test_budget_skips_oversized_item_without_truncating():
    big = make_item("a_big", "记" * 50)  # estimate_tokens = 50
    small = make_item("b_small", "短记")  # estimate_tokens = 2
    assert estimate_tokens(big.content) == 50
    retriever, _ = make_retriever(
        retrieval=RetrievalConfig(budget_tokens=22, header_tokens=20)
    )
    result = search(retriever, [big, small], "")

    # 分数全 0，同分同 R 按 id 升序：先遇大条目（跳过）再装小条目
    assert ids(result) == ["b_small"]
    assert result.tokens == 22  # header 20 + content 2
    assert result.budget == 22
    assert result.selected[0].header_tokens == 20
    assert result.selected[0].item.content == "短记"  # 不截断正文


def test_budget_exact_boundary_and_below_boundary():
    items = [make_item("a_big", "记" * 50), make_item("b_small", "短记")]
    retriever, _ = make_retriever(
        retrieval=RetrievalConfig(budget_tokens=21, header_tokens=20)
    )
    below = search(retriever, items, "")
    assert below.selected == []
    assert below.tokens == 0
    assert below.candidates == 2

    retriever_ok, _ = make_retriever(
        retrieval=RetrievalConfig(budget_tokens=22, header_tokens=20)
    )
    exact = search(retriever_ok, items, "")
    assert ids(exact) == ["b_small"]
    assert exact.tokens == 22


def test_budget_accumulates_multiple_items_and_respects_config_header():
    items = [make_item("b1", "短记"), make_item("b2", "短事")]
    retriever, _ = make_retriever(
        retrieval=RetrievalConfig(budget_tokens=44, header_tokens=20)
    )
    result = search(retriever, items, "")
    assert ids(result) == ["b1", "b2"]
    assert result.tokens == 44

    # 自定义 header 开销：7 + 2 = 9
    retriever_header, _ = make_retriever(
        retrieval=RetrievalConfig(budget_tokens=9, header_tokens=7)
    )
    custom = search(retriever_header, [make_item("b1", "短记")], "")
    assert ids(custom) == ["b1"]
    assert custom.selected[0].header_tokens == 7
    assert custom.tokens == 9


def test_budget_all_items_too_large():
    items = [make_item("a", "记" * 100), make_item("b", "记" * 100)]
    retriever, _ = make_retriever(
        retrieval=RetrievalConfig(budget_tokens=5, header_tokens=20)
    )
    result = search(retriever, items, "")
    assert result.selected == []
    assert result.tokens == 0
    assert result.candidates == 2


# --------------------------------------------------------------------------
# 6. 确定性排序 / 只读性
# --------------------------------------------------------------------------


def test_deterministic_across_runs_and_input_order():
    items = [
        make_item("m1", "上海出差", item_type="preference"),
        make_item("m2", "上海出差", item_type="episode", age_days=4),
        make_item("m3", "深圳旅游"),
        make_item("m4", "上海本帮菜很好吃", user_id="u1"),
    ]
    retriever, _ = make_retriever()

    first = search(retriever, items, "上海")
    second = search(retriever, items, "上海")
    reversed_result = search(retriever, list(reversed(items)), "上海")

    def signature(result: RetrievalResult):
        return [
            (entry.item.id, entry.score, entry.r, entry.cos, entry.bm25, entry.sim)
            for entry in result.selected
        ]

    assert signature(first) == signature(second) == signature(reversed_result)
    assert first.tokens == reversed_result.tokens


def test_ties_broken_by_r_then_id():
    same_content = [
        make_item("z", "上海出差"),
        make_item("a", "上海出差"),
        make_item("m", "上海出差"),
    ]
    retriever, _ = make_retriever()
    result = search(retriever, same_content, "上海")
    assert ids(result) == ["a", "m", "z"]

    # 分数相同（BM25 为 0）时 r 更大者优先
    items = [
        make_item("old", "深圳", age_days=10),
        make_item("new", "上海出差", age_days=5),
    ]
    result_r = search(retriever, items, "")
    assert ids(result_r)[0] == "new"


def test_search_does_not_mutate_items():
    items = [
        make_item("a", "上海出差", vector=[1.0, 0.0, 0.0, 0.0]),
        make_item("b", "深圳旅游", status="dormant"),
    ]
    before = [copy.deepcopy(item.to_dict()) for item in items]
    before_vectors = [list(item.vector) if item.vector is not None else None for item in items]
    retriever, _ = make_retriever(embedder=FakeEmbedder(dim=4))
    search(retriever, items, "上海")

    assert [item.to_dict() for item in items] == before
    assert [
        list(item.vector) if item.vector is not None else None for item in items
    ] == before_vectors


def test_selected_memory_and_result_fields():
    retriever, cfg = make_retriever()
    result = search(retriever, [make_item("a", "上海出差")], "上海")
    assert isinstance(result, RetrievalResult)
    assert isinstance(result.selected[0], SelectedMemory)
    assert isinstance(result.selected[0].item, MemoryItem)
    assert result.budget == cfg.retrieval.budget_tokens
    assert isinstance(result.degraded, bool)


def test_default_now_uses_current_time():
    item = make_item("fresh", "上海出差")
    item.last_access = utc_now_iso()
    retriever, _ = make_retriever()
    result = retriever.search([item], "上海", scope="group", user_id="u1")
    assert result.selected[0].r > 0.99

# --------------------------------------------------------------------------
# 7. bridge.should_bridge（§7.6：强词单命中 / 弱词≥2 / 弱词+实体）
# --------------------------------------------------------------------------


def test_should_bridge_strong_word_single_hit():
    decision = should_bridge("刚才群里大家说的那个事你怎么看")
    assert isinstance(decision, BridgeDecision)
    assert decision.triggered is True
    assert decision.strong is True
    assert decision.reason == "strong"
    assert decision.matched


def test_should_bridge_weak_single_without_entity_not_triggered():
    assert should_bridge("上次那个事").triggered is False
    assert should_bridge("之前那样").triggered is False
    assert should_bridge("上次那个事").reason == "none"


def test_should_bridge_weak_two_distinct_words_triggers():
    decision = should_bridge("之前说过那件事")
    assert decision.triggered is True
    assert decision.strong is False
    assert decision.reason == "weak_count"


def test_should_bridge_weak_plus_entity_triggers():
    decision = should_bridge("上次上海出差怎么样")
    assert decision.triggered is True
    assert decision.strong is False
    assert decision.reason == "weak_entity"
    assert any("上海" in value for value in decision.matched)


def test_should_bridge_entity_only_does_not_trigger():
    assert should_bridge("上海明天天气不错").triggered is False
    assert should_bridge("周末去杭州").triggered is False


def test_should_bridge_disabled_and_bad_input_are_safe():
    assert should_bridge("刚才群里说", BridgeConfig(enabled=False)).triggered is False
    for value in (None, "", "   ", 123):
        assert should_bridge(value).triggered is False
    # 纯函数、无状态：同输入必得同输出
    query = "刚才群里说过上海的事"
    assert should_bridge(query) == should_bridge(query)


def test_should_bridge_strong_words_are_config_driven():
    # Lead 按 §7.6 原文在默认配置补了裸「群」；实现只按配置触发，不写死排除
    assert should_bridge("这个群挺有意思").strong is True
    cfg = BridgeConfig(strong_words=("群里",))
    assert should_bridge("这个群挺有意思", cfg).triggered is False

def test_should_bridge_uses_words_from_config():
    cfg = BridgeConfig(
        strong_words=("翻聊天记录",),
        weak_words=("上次",),
        entity_patterns=(r"杭州",),
    )
    assert should_bridge("你翻聊天记录看看", cfg).strong is True
    assert should_bridge("上次杭州", cfg).triggered is True
    assert should_bridge("上次", cfg).triggered is False


# --------------------------------------------------------------------------
# 8. fill(entries, constraints)（§7.3：injected_items / 预算 / 桥接子预算）
# --------------------------------------------------------------------------


def selected_entry(
    item_id: str,
    content: str,
    *,
    bridge: bool = False,
    pool: str = SOURCE_POOL_GROUP,
    r: float = 0.82,
    score: float = 1.0,
    scope: str = "group",
) -> SelectedMemory:
    return SelectedMemory(
        item=make_item(item_id, content, scope=scope),
        score=score,
        r=r,
        cos=0.0,
        bm25=1.0,
        sim=1.0,
        bridge=bridge,
        source_pool=pool,
    )


def test_fill_returns_required_shape_and_injected_fields():
    entries = [selected_entry("m1", "短记", r=0.82)]
    out = fill(entries, {"total": 100, "subgroup": {"bridge": 50}, "head_per_item": 10})
    assert set(out) == {"injected_items", "truncated", "total_tokens"}
    assert out["truncated"] is False
    assert out["total_tokens"] == 10 + 2
    item = out["injected_items"][0]
    assert item["id"] == "m1"
    assert item["content"] == "短记"
    assert item["type"] == "fact"
    assert item["r"] == pytest.approx(0.82)
    assert item["bridge"] is False
    assert item["source_pool"] == SOURCE_POOL_GROUP
    assert item["tokens"] == 12
    assert item["affect"] is None  # 缺失 affect 也显式给 key，9b 直接 .get 即可


def test_fill_skips_oversized_items_without_truncating_text():
    entries = [selected_entry("big", "记" * 30), selected_entry("small", "短记")]
    out = fill(entries, {"total": 20, "head_per_item": 10})
    assert [item["id"] for item in out["injected_items"]] == ["small"]
    assert out["truncated"] is True
    assert out["total_tokens"] == 12
    assert out["injected_items"][0]["content"] == "短记"


def test_fill_records_retrieval_truncated_for_total_budget(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    entries = [selected_entry("big", "记" * 30), selected_entry("small", "短记")]
    out = fill(entries, {"total": 20, "head_per_item": 10}, metrics=metrics)
    assert out["truncated"] is True
    assert metrics.count("retrieval.truncated") == 1
    assert metrics.count("group_bridge.truncated") == 0


def test_fill_bridge_subgroup_budget_comes_from_constraints(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    entries = [
        selected_entry("d1", "私聊记", pool=SOURCE_POOL_DM),
        selected_entry("b1", "群聊一", bridge=True),
        selected_entry("b2", "群聊二", bridge=True),
    ]
    out = fill(
        entries,
        {"total": 500, "subgroup": {"bridge": 20}, "head_per_item": 10},
        metrics=metrics,
    )
    bridge_items = [item for item in out["injected_items"] if item["bridge"]]
    assert [item["id"] for item in bridge_items] == ["b1"]  # b2 超出 20 的桥接子预算
    assert out["truncated"] is True
    assert metrics.count("group_bridge.truncated") == 1


def test_fill_does_not_hardcode_bridge_budget(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    entries = [selected_entry("b%d" % index, "群聊内容", bridge=True) for index in range(4)]
    # 头部 10 + 正文 4 = 14/条；子预算 25 → 只装 1 条。若装填层硬编码 300 会装下 4 条。
    out = fill(
        entries,
        {"total": 1000, "subgroup": {"bridge": 25}, "head_per_item": 10},
        metrics=metrics,
    )
    assert len([item for item in out["injected_items"] if item["bridge"]]) == 1
    assert metrics.count("group_bridge.truncated") == 1


def test_fill_without_constraints_does_not_hardcode_budget():
    # §6.4 / D3：装填层不硬编码 1200 / head；constraints 缺省 = 不设上限、头部 0
    out = fill([selected_entry("m1", "短记")])
    assert out["total_tokens"] == 2
    assert out["truncated"] is False


# --------------------------------------------------------------------------
# 9. 群聊混合池与 rel / user_affect 调制（§7.2 / §7.4）
# --------------------------------------------------------------------------


def test_group_pool_is_mixed_and_rel_marks_current_user_only():
    items = [
        make_item("mine", "上海出差", user_id="u1"),
        make_item("other", "上海出差", user_id="u2"),
    ]
    metrics = Metrics()
    retriever, _ = make_retriever(metrics=metrics)
    result = search(retriever, items, "上海", scope="group", user_id="u1")
    by_id = {entry.item.id: entry for entry in result.selected}
    assert set(by_id) == {"mine", "other"}  # 群聊混合池：不按 user_id 预过滤
    assert by_id["mine"].score_detail["rel"] is True
    assert by_id["other"].score_detail["rel"] is False
    assert metrics.count("retrieval.pool.mixed") == 1


def test_rel_user_affect_modulation_applies_only_to_current_user():
    items = [
        make_item("mine", "上海出差", user_id="u1"),
        make_item("other", "上海出差", user_id="u2"),
    ]
    retriever, _ = make_retriever()
    baseline = search(retriever, items, "上海", scope="group", user_id="u1")
    affected = search(
        retriever,
        items,
        "上海",
        scope="group",
        user_id="u1",
        user_affect=UserAffect(affection=1.0, annoyance=0.0, familiarity=1.0),
    )
    base = {entry.item.id: entry.score for entry in baseline.selected}
    tuned = {entry.item.id: entry.score for entry in affected.selected}
    # rel=1：type_bonus ×(1+0.1·familiarity)=1.1，再 ×(1+0.15·(affection-annoyance))=1.15
    assert tuned["mine"] == pytest.approx(base["mine"] * 1.1 * 1.15)
    assert tuned["other"] == pytest.approx(base["other"])


def test_dm_pool_does_not_leak_other_users_group_memories_without_trigger():
    items = [
        make_item("dm1", "上海出差", scope="dm", user_id="u1"),
        make_item("g_other", "上海出差", scope="group", user_id="u2"),
    ]
    retriever, _ = make_retriever()
    result = search(retriever, items, "上海", scope="dm", user_id="u1")
    assert ids(result) == ["dm1"]
    assert result.bridge_hit == 0


# --------------------------------------------------------------------------
# 10. 私聊 → 群聊桥接（§7.6）
# --------------------------------------------------------------------------


def test_bridge_strong_trigger_appends_same_user_active_group_memories():
    metrics = Metrics()
    retriever, _ = make_retriever(metrics=metrics)
    items = [
        make_item("dm1", "我下周三去上海出差", scope="dm", user_id="u1"),
        make_item("g1", "群里说下周三一起去上海出差", scope="group", user_id="u1"),
        make_item("g2", "群里说下周三一起去上海出差", scope="group", user_id="u2"),
        make_item(
            "g_dormant",
            "群里说下周三一起去上海出差",
            scope="group",
            user_id="u1",
            status="dormant",
        ),
    ]
    result = search(retriever, items, "刚才群里说上海出差的事", scope="dm", user_id="u1")
    by_id = {entry.item.id: entry for entry in result.selected}
    assert "dm1" in by_id
    assert "g1" in by_id
    assert by_id["g1"].bridge is True
    assert by_id["g1"].source_pool == SOURCE_POOL_BRIDGE
    assert "g2" not in by_id  # 别人的群聊记忆不桥接
    assert "g_dormant" not in by_id  # 只桥接 active
    assert result.bridge_hit == 1
    assert metrics.count("group_bridge.triggered.strong") == 1
    assert metrics.count("group_bridge.hit") == 1


def test_bridge_item_scored_at_low_weight():
    retriever, _ = make_retriever()
    items = [
        make_item("dm1", "我下周三去上海出差", scope="dm", user_id="u1"),
        make_item("g1", "群里说下周三一起去上海出差", scope="group", user_id="u1"),
    ]
    result = search(retriever, items, "刚才群里说上海出差", scope="dm", user_id="u1")
    by_id = {entry.item.id: entry for entry in result.selected}
    bridge_entry = by_id["g1"]
    detail = bridge_entry.score_detail
    expected = detail["base"] * bridge_entry.r * detail["type_bonus"] * 0.7
    assert bridge_entry.score == pytest.approx(expected)
    assert detail["bridge"] == pytest.approx(0.7)


def test_bridge_hard_rule_caps_weight_and_budget():
    # §11 硬规则 18：即使配置越界，桥接权重 ≤0.7、子预算 ≤300
    retriever, _ = make_retriever(bridge=BridgeConfig(weight=1.5, budget_tokens=1000))
    items = [make_item("dm1", "私聊内容", scope="dm", user_id="u1")]
    items.extend(
        make_item("g%d" % index, "记" * 56, scope="group", user_id="u1")
        for index in range(5)
    )
    result = search(retriever, items, "刚才群里说的事", scope="dm", user_id="u1")
    bridge_entries = [entry for entry in result.selected if entry.bridge]
    assert bridge_entries, result.selected
    assert all(entry.score_detail["bridge"] == pytest.approx(0.7) for entry in bridge_entries)
    assert sum(item["tokens"] for item in result.injected_items if item["bridge"]) <= 300
    assert result.bridge_truncated is True

def test_bridge_weak_trigger_requires_two_words_or_entity():
    items = [
        make_item("dm1", "普通私聊", scope="dm", user_id="u1"),
        make_item("g1", "群里的上海行程", scope="group", user_id="u1"),
    ]
    retriever, _ = make_retriever()
    single = search(retriever, items, "上次那件事", scope="dm", user_id="u1")
    assert single.bridge_hit == 0
    two_words = search(retriever, items, "之前说过那件事", scope="dm", user_id="u1")
    assert two_words.bridge_hit == 1
    entity = search(retriever, items, "上次上海的事", scope="dm", user_id="u1")
    assert entity.bridge_hit == 1


def test_bridge_disabled_returns_dm_pool_only():
    retriever, _ = make_retriever(bridge=BridgeConfig(enabled=False))
    items = [
        make_item("dm1", "上海出差", scope="dm", user_id="u1"),
        make_item("g1", "上海出差", scope="group", user_id="u1"),
    ]
    result = search(retriever, items, "刚才群里说上海出差", scope="dm", user_id="u1")
    assert ids(result) == ["dm1"]
    assert result.bridge_hit == 0


def test_bridge_budget_limits_injected_bridge_items(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(
        metrics=metrics,
        bridge=BridgeConfig(budget_tokens=70, weight=0.7),
    )
    items = [make_item("dm1", "私聊内容", scope="dm", user_id="u1")]
    items.extend(
        make_item("g%d" % index, "群聊第%d条内容" % index, scope="group", user_id="u1")
        for index in range(3)
    )
    result = search(retriever, items, "刚才群里说的事", scope="dm", user_id="u1")
    injected_bridge = [entry for entry in result.selected if entry.bridge]
    assert len(injected_bridge) == 2  # 每条 24+7=31 tokens，70 只装得下 2 条
    assert result.bridge_hit == 2  # Lead 拍板：hit = 最终注入的桥接条目数
    assert result.bridge_candidates == 3  # 去重后、进 fill 前的候选数
    assert result.bridge_truncated is True
    assert metrics.count("group_bridge.truncated") == 1
    assert metrics.count("group_bridge.hit") == 1


def test_bridge_cross_pool_dedup_prefers_exact_key(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(metrics=metrics)
    items = [
        make_item("dm1", "我喜欢本帮菜", scope="dm", user_id="u1"),
        make_item("g1", "我喜欢本帮菜", scope="group", user_id="u1"),
    ]
    result = search(retriever, items, "刚才群里说我喜欢本帮菜", scope="dm", user_id="u1")
    assert "g1" not in ids(result)
    assert result.dedup_removed == 1
    assert result.dedup_degraded is False  # dedup_key 精确命中，不算降级
    assert metrics.count("group_bridge.dedup") == 1
    assert result.bridge_hit == 0


def test_bridge_cross_pool_dedup_uses_cosine_secondary():
    embedder = FakeEmbedder(vectors={"刚才群里说上海": [1.0, 0.0, 0.0, 0.0]}, dim=4)
    metrics = Metrics()
    retriever, _ = make_retriever(embedder=embedder, metrics=metrics)
    items = [
        make_item(
            "dm1",
            "完全不同的私聊内容甲",
            scope="dm",
            user_id="u1",
            vector=[1.0, 0.0, 0.0, 0.0],
        ),
        make_item(
            "g1",
            "完全不同的群聊内容乙",
            scope="group",
            user_id="u1",
            vector=[1.0, 0.0, 0.0, 0.0],
        ),
    ]
    result = search(retriever, items, "刚才群里说上海", scope="dm", user_id="u1")
    assert "g1" not in ids(result)  # cos=1.0 > 0.9 → 二次判定为重复
    assert result.dedup_removed == 1
    assert result.dedup_degraded is False
    assert metrics.count("group_bridge.dedup") == 1


def test_bridge_cross_pool_dedup_keeps_higher_score_side():
    # §6.2 / Lead 拍板：重复对保留 score 高者——桥接侧分数更高时去掉 dm 侧
    retriever, _ = make_retriever()
    items = [
        make_item("dm1", "上海出差", scope="dm", user_id="u1", age_days=60.0),
        make_item("g1", "上海出差", scope="group", user_id="u1", age_days=0.0),
    ]
    result = search(retriever, items, "刚才群里说上海出差", scope="dm", user_id="u1")
    assert result.dedup_removed == 1
    assert "dm1" not in ids(result)  # dm 侧 R 低 → 分数低 → 被去重
    assert "g1" in ids(result)
    assert result.bridge_candidates == 1
    assert result.bridge_hit == 1

def test_bridge_dedup_degrades_to_raw_text_without_vectors(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(metrics=metrics)  # NullEmbedder → 向量不可用
    items = [
        make_item("dm1", "相同内容", scope="dm", user_id="u1", item_type="fact"),
        make_item("g1", "相同内容", scope="group", user_id="u1", item_type="episode"),
    ]
    result = search(retriever, items, "刚才群里说相同内容", scope="dm", user_id="u1")
    # type 不同 → dedup_key 精确不命中；无向量 → 退化原文匹配命中并留痕
    assert "g1" not in ids(result)
    assert result.dedup_removed == 1
    assert result.dedup_degraded is True
    assert metrics.count("dedup.degraded") == 1
    assert metrics.count("group_bridge.dedup") == 1


# --------------------------------------------------------------------------
# 11. affect_match（§7.2）
# --------------------------------------------------------------------------


def test_affect_similarity_matrix():
    assert affect_similarity({"label": "joy"}, {"label": "joy"}) == 1.0
    assert affect_similarity({"label": "joy"}, {"label": "affection"}) == 0.5
    assert affect_similarity({"label": "anger"}, {"label": "disgust"}) == 0.5
    assert affect_similarity({"label": "sadness"}, {"label": "sadness"}) == 1.0
    assert affect_similarity({"label": "joy"}, {"label": "anger"}) == 0.0
    assert affect_similarity({"label": "sadness"}, {"label": "anxiety"}) == 0.0
    assert affect_similarity(None, {"label": "anger"}) == 0.0
    assert affect_similarity({"label": "neutral"}, {"label": "neutral"}) == 0.0


def test_affect_match_boost_only_when_enabled_and_event_affect_present():
    items = [
        make_item("anger", "上海出差", affect={"label": "anger", "intensity": 0.8}),
        make_item("joy", "上海出差", affect={"label": "joy", "intensity": 0.8}),
    ]
    enabled, _ = make_retriever(
        retrieval=RetrievalConfig(affect_match=True, gamma_a=0.1)
    )
    result = search(
        enabled,
        items,
        "上海",
        last_event_affect={"label": "anger", "intensity": 0.6},
    )
    by_id = {entry.item.id: entry for entry in result.selected}
    assert by_id["anger"].score_detail["affect_match"] == 1.0
    assert by_id["joy"].score_detail["affect_match"] == 0.0
    assert by_id["anger"].score == pytest.approx(by_id["joy"].score * 1.1)

    disabled, _ = make_retriever()
    plain = search(
        disabled,
        items,
        "上海",
        last_event_affect={"label": "anger", "intensity": 0.6},
    )
    for entry in plain.selected:
        assert entry.score_detail.get("affect_match") is None

    no_event = search(enabled, items, "上海")
    for entry in no_event.selected:
        assert entry.score_detail.get("affect_match") is None


def test_affect_bias_counts_only_rel_one_items():
    items = [
        make_item("mine", "上海出差", user_id="u1", affect={"label": "anger", "intensity": 0.8}),
        make_item("other", "上海出差", user_id="u2", affect={"label": "anger", "intensity": 0.8}),
        make_item("mine_no_affect", "上海出差", user_id="u1"),
        make_item("mine_joy", "上海出差", user_id="u1", affect={"label": "joy", "intensity": 0.8}),
    ]
    retriever, _ = make_retriever(retrieval=RetrievalConfig(affect_match=True))
    result = search(
        retriever,
        items,
        "上海",
        scope="group",
        user_id="u1",
        last_event_affect={"label": "anger", "intensity": 0.8},
    )
    assert result.affect_bias == 1  # 只有 mine(anger) 实际被加成；neutral/无 affect 不算生效
    by_id = {entry.item.id: entry for entry in result.selected}
    assert set(by_id) == {"mine", "other", "mine_no_affect", "mine_joy"}
    # affect_match 本身对所有条目生效（公式无 rel 条件），指标只统计 rel=1
    assert by_id["mine"].score == pytest.approx(by_id["other"].score)
    assert by_id["mine_no_affect"].score_detail["affect_match"] == 0.0
    assert by_id["mine_joy"].score_detail["affect_match"] == 0.0


# --------------------------------------------------------------------------
# 12. injected_items 与装填结果一致性（§7.3）
# --------------------------------------------------------------------------


def test_search_injected_items_match_selected_and_token_budget():
    items = [
        make_item("a", "上海出差", user_id="u1"),
        make_item("b", "深圳旅游", user_id="u1"),
    ]
    retriever, _ = make_retriever()
    result = search(retriever, items, "上海")
    assert [item["id"] for item in result.injected_items] == [
        entry.item.id for entry in result.selected
    ]
    assert result.tokens == sum(item["tokens"] for item in result.injected_items)
    assert all(item["source_pool"] == SOURCE_POOL_GROUP for item in result.injected_items)

# --------------------------------------------------------------------------
# 13. 规范用例 33~46 的检索侧断言（§10.6 / §10.7）
# --------------------------------------------------------------------------


def read_metric_events(metrics: Metrics) -> list:
    path = metrics.log_path
    if path is None:
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_spec_33_group_scope_never_bridges():
    metrics = Metrics()
    retriever, _ = make_retriever(metrics=metrics)
    items = [
        make_item("g1", "上海出差", user_id="u1"),
        make_item("dm1", "上海出差", scope="dm", user_id="u1"),
    ]
    result = search(retriever, items, "刚才群里说上海出差", scope="group", user_id="u1")
    assert result.bridge_hit == 0
    assert metrics.count("group_bridge.triggered.strong") == 0
    assert ids(result) == ["g1"]  # 群聊检索永远不读 dm 记忆（硬规则 18）


def test_spec_36_bridge_budget_truncates_to_subgroup_limit(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(metrics=metrics)
    # 5 条桥接候选：每条 24 行头 + 56 正文 = 80 tokens（累计 400）；子预算 300 → 只装 3 条
    items = [
        make_item("g%d" % index, "记" * 56, scope="group", user_id="u1")
        for index in range(5)
    ]
    result = search(retriever, items, "刚才群里说的事", scope="dm", user_id="u1")
    injected = [item for item in result.injected_items if item["bridge"]]
    assert len(injected) == 3
    bridge_tokens = sum(item["tokens"] for item in injected)
    assert bridge_tokens == 240
    assert bridge_tokens <= 300
    assert result.tokens <= 1200
    assert result.bridge_candidates == 5  # 去重后 5 条全部进 fill
    assert result.bridge_hit == 3  # 注入 3 条（≤300 tokens）
    assert result.bridge_truncated is True
    assert metrics.count("group_bridge.truncated") == 1


def test_spec_37_cross_pool_dedup_two_pairs(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(metrics=metrics)
    items = [
        make_item("dm1", "事实甲", scope="dm", user_id="u1"),
        make_item("dm2", "事实乙", scope="dm", user_id="u1"),
        make_item("g1", "事实甲", scope="group", user_id="u1"),
        make_item("g2", "事实乙", scope="group", user_id="u1"),
        make_item("g3", "群里独特的新事实", scope="group", user_id="u1"),
    ]
    result = search(retriever, items, "刚才群里说的事实", scope="dm", user_id="u1")
    assert result.dedup_removed == 2
    assert result.bridge_candidates == 1  # 去重后只剩 1 条桥接候选
    assert result.bridge_hit == 1
    events = [event for event in read_metric_events(metrics) if event["event"] == "group_bridge.dedup"]
    assert len(events) == 1
    assert events[0]["count"] == 2
    assert metrics.count("dedup.degraded") == 1


def test_spec_38_familiarity_and_user_affect_modulation_numbers():
    items = [
        make_item("mine", "上海出差", user_id="u1"),
        make_item("other", "上海出差", user_id="u2"),
    ]
    retriever, _ = make_retriever()
    baseline = search(retriever, items, "上海", scope="group", user_id="u1")
    tuned = search(
        retriever,
        items,
        "上海",
        scope="group",
        user_id="u1",
        user_affect=UserAffect(affection=0.9, annoyance=0.1, familiarity=0.8),
    )
    base = {entry.item.id: entry.score for entry in baseline.selected}
    tuned_scores = {entry.item.id: entry.score for entry in tuned.selected}
    assert tuned_scores["mine"] == pytest.approx(base["mine"] * 1.08 * 1.12)
    assert tuned_scores["other"] == pytest.approx(base["other"])


def test_spec_46_degradation_chain_metrics_appear(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(metrics=metrics)  # NullEmbedder：向量不可用
    items = [
        make_item("dm1", "相同内容", scope="dm", user_id="u1", item_type="fact"),
        make_item("g1", "相同内容", scope="group", user_id="u1", item_type="episode"),
    ]
    result = search(retriever, items, "刚才群里说相同内容", scope="dm", user_id="u1")
    assert result.degraded is True
    assert result.dedup_removed == 1
    assert result.dedup_degraded is True
    assert metrics.count("retrieval.degraded") == 1
    assert metrics.count("group_bridge.hit") == 1
    assert metrics.count("dedup.degraded") == 1
    assert result.injected_items  # 回答路径可用：dm 池仍正常注入


def test_search_records_retrieval_truncated_metric(tmp_path):
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(
        metrics=metrics,
        retrieval=RetrievalConfig(budget_tokens=25, header_tokens=20),
    )
    items = [make_item("fit", "短记忆内容"), make_item("big", "记" * 20)]
    result = search(retriever, items, "")
    assert ids(result) == ["fit"]  # 20+5 恰好 25；大条目跳过
    assert metrics.count("retrieval.truncated") == 1

def test_spec_43_dedup_and_bridge_truncation_coexist(tmp_path):
    # 用例 43：2 对同事实，dm 侧分数更低被去重；桥接侧 5 条仍占 400 tokens → 截断
    metrics = Metrics(log_path=tmp_path / "metrics.jsonl")
    retriever, _ = make_retriever(metrics=metrics)
    bridge_contents = ["上海出差第%d条" % index + "记" * 49 for index in range(5)]
    items = [
        make_item("dm_dup_%d" % index, bridge_contents[index], scope="dm", user_id="u1", age_days=60.0)
        for index in range(2)
    ]
    items.extend(
        make_item("g_%d" % index, bridge_contents[index], scope="group", user_id="u1")
        for index in range(5)
    )
    result = search(retriever, items, "刚才群里说上海出差", scope="dm", user_id="u1")
    assert result.dedup_removed == 2  # dm 侧低分条目被去重
    assert result.bridge_candidates == 5  # 桥接侧 5 条全部保留进 fill
    assert result.bridge_truncated is True  # 5 × 80 = 400 > 300
    assert result.bridge_hit == 3
    assert result.tokens <= 1200
    events = read_metric_events(metrics)
    dedup_events = [event for event in events if event["event"] == "group_bridge.dedup"]
    truncated_events = [
        event for event in events if event["event"] == "group_bridge.truncated"
    ]
    assert len(dedup_events) == 1 and dedup_events[0]["count"] == 2
    assert len(truncated_events) == 1


