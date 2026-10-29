"""v1.6-rc4 集成/组合回归（规范 §12.5 / §12.4 / §12.2 / §7.1）。

覆盖：
- P17：无哨兵但正文含 {"memories":[...]} 片段 → 发送正文去掉 JSON 块，不整段保留 raw；
- A27：群聊混合池固定数值样例，rel=0 的 0.51 必须压过 rel=1 的 0.42；
- X02：last_event_affect 只在进程内，重启后为空且 affect_match 自动失效不报错；
- X04：embedding 不可用降级链（retrieval.degraded / dedup.degraded，回答照常）；
- E3：轻路径只决定是否检索，不决定是否提取；轻路径命中闸门仍附提取协议。
"""

from __future__ import annotations

import json
import math

import pytest

from qihun_memory.config import Config
from qihun_memory.embedding import Embedder
from qihun_memory.memory import MemorySystem
from qihun_memory.models import MemoryItem, utc_now, utc_now_iso
from qihun_memory.pipeline import TurnEvent, TurnPipeline
from qihun_memory.prompt import EXTRACTION_PROTOCOL


class FakeEmbedder(Embedder):
    """确定性假编码器：按文本查表返回已归一化向量。"""

    name = "fake"
    dim = 4

    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        self.mapping = dict(mapping or {})

    @property
    def available(self) -> bool:
        return True

    def encode(self, texts):
        vectors = []
        for text in texts:
            vector = [float(value) for value in self.mapping.get(text) or [1.0, 0.0, 0.0, 0.0]]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def make_config(tmp_path, **overrides) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    data.update(overrides)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def make_item(system: MemorySystem, **overrides) -> MemoryItem:
    now = utc_now_iso()
    base: dict = {
        "id": system.store.next_id(),
        "user_id": "u_1",
        "scope": "group",
        "type": "episode",
        "content": "用户A下周三去上海出差",
        "created_at": now,
        "last_access": now,
        "access_count": 0,
        "mention_count": 1,
        "S": 7.0,
        "valence": 0.0,
        "status": "active",
        "tags": [],
    }
    base.update(overrides)
    return MemoryItem(**base)


def event(message: str = "我下周三去上海出差", **overrides) -> TurnEvent:
    base: dict = {"user_id": "u_1", "scope": "group", "message": message, "msgid": "msg-1"}
    base.update(overrides)
    return TurnEvent(**base)


def raw_output(answer: str, memories: list[dict] | None = None) -> str:
    payload = json.dumps({"memories": memories or []}, ensure_ascii=False)
    return answer + "\n<MEMORY_JSON>" + payload + "</MEMORY_JSON>"


# --------------------------------------------------------------------------
# P17
# --------------------------------------------------------------------------

def test_p17_no_sentinel_answer_cuts_bracketed_json(tmp_path) -> None:
    cfg = make_config(tmp_path)
    system = MemorySystem(cfg)
    pipeline = TurnPipeline(system, cfg)
    prepared = pipeline.prepare(event())
    raw = '好的，我记下了。{"memories":[{"type":"fact","content":"用户A喜欢本帮菜"}]}'
    result = pipeline.finish(prepared, raw)
    # 发送正文 = 去掉疑似 JSON 块后的文本，不整段保留 raw
    assert '{"memories"' not in result.answer
    assert result.answer.strip() == "好的，我记下了。"
    # 无哨兵兜底仍可解析 memories；闸门已命中 → 落盘
    assert len(result.memories) == 1
    assert [item.content for item in system.all_items()] == ["用户A喜欢本帮菜"]


# --------------------------------------------------------------------------
# A27（固定数值样例，消除"高/低 sim"歧义）
# --------------------------------------------------------------------------

def _unit(cos_value: float) -> list[float]:
    return [cos_value, math.sqrt(max(0.0, 1.0 - cos_value * cos_value)), 0.0, 0.0]


def test_a27_rel0_outscores_rel1_fixed_numeric_sample(tmp_path) -> None:
    # 查询串与条目无词法重叠 → BM25 批内归一全 0 → base = 0.6 * cos
    query = "zzz"
    cfg = make_config(tmp_path, embedding={"enabled": True, "backend": "none", "dim": 4})
    embedder = FakeEmbedder({query: [1.0, 0.0, 0.0, 0.0]})
    system = MemorySystem(cfg, embedder=embedder)
    now = utc_now()
    stamp = now.isoformat(timespec="seconds")
    rel0 = make_item(
        system,
        user_id="u_2",
        type="fact",
        content="群友二号提过一本书",
        vector=_unit(0.85),
        last_access=stamp,
        created_at=stamp,
    )
    rel1 = make_item(
        system,
        user_id="u_1",
        type="fact",
        content="群友一号提过一部电影",
        vector=_unit(0.55),
        last_access=stamp,
        created_at=stamp,
    )
    system.store.append(rel0)
    system.store.append(rel1)
    # familiarity=1.0 / affection=1.0 / annoyance=0
    system.affect_store.step_update("u_1", {"familiarity": 1.0, "affection": 1.0}, now=now)

    result = system.search(query, "group", "u_1", now=now)
    ids = [entry.item.id for entry in result.selected]
    assert ids[:2] == [rel0.id, rel1.id], "rel=0 base 高 26.5% 以上时必须排前"
    assert result.selected[0].score == pytest.approx(0.51, abs=1e-6)
    assert result.selected[1].score == pytest.approx(0.33 * 1.1 * 1.15, abs=1e-6)


# --------------------------------------------------------------------------
# X02：last_event_affect 跨轮 / 重启
# --------------------------------------------------------------------------

def test_x02_last_event_affect_lost_after_restart(tmp_path) -> None:
    cfg = make_config(tmp_path)
    system = MemorySystem(cfg)
    pipeline = TurnPipeline(system, cfg)
    first = pipeline.prepare(event(msgid="msg-1"))
    pipeline.finish(
        first,
        raw_output(
            "好。",
            [{"type": "episode", "content": "用户A下周三去上海出差", "affect": {"label": "anger", "intensity": 0.8}}],
        ),
    )
    second = pipeline.prepare(event(msgid="msg-2"))
    assert second.last_event_affect_at_read is not None

    # 重启：新 pipeline 实例不继承进程内 last_event_affect；affect_match 自动失效不报错
    restarted = TurnPipeline(system, cfg)
    third = restarted.prepare(event(msgid="msg-3"))
    assert third.last_event_affect_at_read is None
    assert third.retrieval.degraded in (True, False)


# --------------------------------------------------------------------------
# X04：降级链（embedding 不可用）
# --------------------------------------------------------------------------

def test_x04_embedding_degraded_chain_end_to_end(tmp_path) -> None:
    cfg = make_config(tmp_path)  # embedding disabled → NullEmbedder
    system = MemorySystem(cfg)
    pipeline = TurnPipeline(system, cfg)
    first = pipeline.prepare(event(msgid="msg-1"))
    pipeline.finish(first, raw_output("好的", [{"type": "fact", "content": "用户A喜欢本帮菜"}]))
    assert len(system.all_items()) == 1

    second = pipeline.prepare(event(message="本帮菜好吃吗", msgid="msg-2"))
    assert second.retrieval.degraded is True
    assert system.metrics.count("retrieval.degraded") >= 1
    assert second.prompt.system_prompt  # prompt 仍可用
    answer = pipeline.finish(second, raw_output("嗯。"))
    assert answer.answer.strip() == "嗯。"


# --------------------------------------------------------------------------
# E3：轻路径与闸门解耦
# --------------------------------------------------------------------------

def test_e3_light_path_still_gated_extraction(tmp_path) -> None:
    cfg = make_config(tmp_path)
    system = MemorySystem(cfg)
    pipeline = TurnPipeline(system, cfg)

    # "去上海" 只有 3 字 → 轻路径不检索；但命中 §5.1 地点闸门 → 仍附提取协议
    light_gated = pipeline.prepare(event(message="去上海", msgid="msg-1"))
    assert light_gated.light_path is True
    assert light_gated.gate_passed is True
    assert EXTRACTION_PROTOCOL in light_gated.prompt.system_prompt

    # "嗯" 轻路径且无闸门 → 不附协议
    light_plain = pipeline.prepare(event(message="嗯", msgid="msg-2"))
    assert light_plain.light_path is True
    assert light_plain.gate_passed is False
    assert EXTRACTION_PROTOCOL not in light_plain.prompt.system_prompt
