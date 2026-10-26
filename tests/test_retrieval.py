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
from qihun_memory.config import Config, DecayConfig, RetrievalConfig
from qihun_memory.embedding import Embedder, NullEmbedder
from qihun_memory.metrics import Metrics
from qihun_memory.models import MemoryItem, utc_now_iso
from qihun_memory.retrieval import (
    DEGRADED_EMBEDDER_UNAVAILABLE,
    DEGRADED_ENCODE_FAILED,
    DEGRADED_NO_VECTORS,
    RetrievalResult,
    Retriever,
    SelectedMemory,
    _bm25_raw,
    _minmax,
    _tokenize,
    _type_bonus,
)
from qihun_memory.state import BotState
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
    )


def make_retriever(
    embedder: Embedder | None = None,
    *,
    retrieval: RetrievalConfig | None = None,
    decay: DecayConfig | None = None,
    metrics: Metrics | None = None,
) -> tuple[Retriever, Config]:
    cfg = Config(
        retrieval=retrieval or RetrievalConfig(),
        decay=decay or DecayConfig(),
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
):
    return retriever.search(
        items,
        query,
        scope=scope,
        user_id=user_id,
        bot_state=bot_state,
        now=NOW,
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
    assert result.selected[0].header_tokens == 20


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
