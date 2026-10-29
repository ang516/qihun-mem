"""v1.5-r2 L3：turn 集成回归矩阵（规范 §10.5-31 / §10.6-41/42 / §7.3 / §7.5 / 附录 D6）。

覆盖：
- turn 顺序：用 spy 记录真实调用顺序，断言 emit 早于 summary 早于 8a 早于 8b 早于
  9a 早于 9b（不是靠代码行号）；9b 消费的是 fill 产出的 ctx.injected_items；
- ctx 只读冻结：除 last_event_affect / metrics / pending 外所有字段赋值抛
  ContextViolation 并记 ctx.violation（字段名可核对）；可写字段正常；
- pending：写盘失败下一轮 prepare 重放；turn_key 幂等不重复累加 mention_count；
  强化 / 感情更新失败不进 pending；
- 9b：只聚合 injected_items 中 user_id==当前用户且非 neutral 的 affect，
  I_joy / I_anger 取 max，桥接条目同样参与但仅限同一 user，每轮每分量至多一次；
- 指标：turn.finish 的 bridge_hit / dedup_removed / dedup_degraded / affect_bias，
  以及 retrieval.affect_bias 事件。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from qihun_memory import pipeline as pipeline_mod
from qihun_memory.config import Config
from qihun_memory.memory import MemorySystem
from qihun_memory.metrics import Metrics
from qihun_memory.models import MemoryItem, utc_now_iso
from qihun_memory.pipeline import (
    ContextViolation,
    PreparedTurn,
    TurnEvent,
    TurnPipeline,
)


# --------------------------------------------------------------------------
# 测试辅助
# --------------------------------------------------------------------------

def raw_output(answer: str, memories: list[dict] | None = None) -> str:
    payload = json.dumps({"memories": memories or []}, ensure_ascii=False)
    return answer + "\n<MEMORY_JSON>" + payload + "</MEMORY_JSON>"


def memory(content: str, item_type: str = "episode", affect: dict | None = None) -> dict:
    item: dict = {"type": item_type, "content": content, "tags": ["测试"]}
    if affect is not None:
        item["affect"] = affect
    return item


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


def read_metric_events(path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def find_events(events: list[dict], name: str) -> list[dict]:
    return [record for record in events if record.get("event") == name]


@pytest.fixture()
def system(config) -> MemorySystem:
    return MemorySystem(config)


@pytest.fixture()
def pipeline(system) -> TurnPipeline:
    return TurnPipeline(system, system.cfg)


# ==========================================================================
# turn 顺序：emit → summary → 8a → 8b → 9a → 9b（D6，不靠代码位置）
# ==========================================================================

def test_turn_order_full_chain(pipeline, system, monkeypatch) -> None:
    system.store.append(
        make_item(system, content="用户A下周三去上海出差", affect={"label": "joy", "intensity": 0.8})
    )
    prepared = pipeline.prepare(event("上海出差怎么样"))
    assert prepared.injected_items, "9b 的输入来自 fill 产物"

    order: list[str] = []
    real_summary = pipeline.summaries.update
    real_append = system.store.append
    real_step = system.affect_store.step_update
    real_reinforce = system.reinforce
    real_aggregate = pipeline_mod.aggregate

    def summary(*args, **kwargs):
        order.append("summary")
        return real_summary(*args, **kwargs)

    def append(item):
        order.append("8b.append")
        return real_append(item)

    def step_update(user_id, deltas, *, now=None):
        order.append("9b" if "familiarity" not in deltas else "8a")
        return real_step(user_id, deltas, now=now)

    def reinforce(*args, **kwargs):
        order.append("9a")
        return real_reinforce(*args, **kwargs)

    def aggregate(injected_items, **kwargs):
        order.append("9b.aggregate")
        assert list(injected_items) == prepared.injected_items, "9b 必须消费 fill 产物"
        return real_aggregate(injected_items, **kwargs)

    monkeypatch.setattr(pipeline.summaries, "update", summary)
    monkeypatch.setattr(system.store, "append", append)
    monkeypatch.setattr(system.affect_store, "step_update", step_update)
    monkeypatch.setattr(system, "reinforce", reinforce)
    monkeypatch.setattr(pipeline_mod, "aggregate", aggregate)

    result = pipeline.finish(
        prepared,
        raw_output("好的", [memory("用户A喜欢本帮菜", "fact")]),
        sender=lambda _text: order.append("emit"),
    )

    assert order and order[0] == "emit", "emit 必须最早（先发送后写回）: " + repr(order)
    expected = ["emit", "summary", "8a", "8b.append", "9a", "9b.aggregate", "9b"]
    positions = [order.index(name) for name in expected]
    assert positions == sorted(positions), "顺序错乱: " + repr(order)
    assert order.count("8a") == 1
    assert order.count("9b") == 1
    assert order.count("9b.aggregate") == 1
    assert len(result.write_report.created) == 1


def test_9b_step_update_called_once_per_turn_without_affect(pipeline, system, monkeypatch) -> None:
    """没有非 neutral affect 注入时：9b 不产生 deltas，step_update 只有 8a 一次。"""
    prepared = pipeline.prepare(event())
    calls: list[dict] = []
    real_step = system.affect_store.step_update

    def spy(user_id, deltas, *, now=None):
        calls.append(dict(deltas))
        return real_step(user_id, deltas, now=now)

    monkeypatch.setattr(system.affect_store, "step_update", spy)
    pipeline.finish(prepared, raw_output("好"))
    assert calls == [{"familiarity": pytest.approx(0.1)}]
    assert pipeline.metrics.count("turn.finish") == 1


# ==========================================================================
# ctx 只读冻结（§7.2 / 用例 41）
# ==========================================================================

def test_ctx_all_frozen_fields_raise_and_record_metric(tmp_path) -> None:
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=metrics_path)
    cfg = make_config(tmp_path)
    system = MemorySystem(cfg, metrics=metrics)
    try:
        pipeline = TurnPipeline(system, cfg)
        prepared = pipeline.prepare(event())
        mutable = {"last_event_affect", "metrics", "pending"}
        frozen_names = [
            field.name
            for field in dataclasses.fields(PreparedTurn)
            if field.name not in mutable and field.name != "_sealed"
        ]
        assert "persona" in frozen_names and "injected_items" in frozen_names
        for name in frozen_names:
            original = getattr(prepared, name)
            with pytest.raises(ContextViolation) as excinfo:
                setattr(prepared, name, original)
            assert excinfo.value.field == name
            assert excinfo.value.turn_id == prepared.turn_id
            assert getattr(prepared, name) is original or getattr(prepared, name) == original
        assert metrics.count("ctx.violation") == len(frozen_names)
        events = find_events(read_metric_events(metrics_path), "ctx.violation")
        assert {record["field"] for record in events} == set(frozen_names)
    finally:
        system.close()


def test_ctx_mutable_fields_assignable(pipeline) -> None:
    prepared = pipeline.prepare(event())
    prepared.last_event_affect = {"label": "anger", "intensity": 0.5}
    prepared.metrics = {"custom": 1}
    prepared.pending = [{"type": "fact", "content": "x"}]
    prepared.pending.append({"type": "fact", "content": "y"})
    prepared.metrics["extra"] = 2
    prepared.last_event_affect["intensity"] = 0.6
    assert prepared.last_event_affect == {"label": "anger", "intensity": 0.6}
    assert prepared.metrics == {"custom": 1, "extra": 2}
    assert len(prepared.pending) == 2
    assert pipeline.metrics.count("ctx.violation") == 0


def test_ctx_violation_inside_write_back_propagates(pipeline, system, monkeypatch) -> None:
    prepared = pipeline.prepare(event())

    def evil(user_id, deltas, *, now=None):
        prepared.summary = "被篡改的检索侧字段"

    monkeypatch.setattr(system.affect_store, "step_update", evil)
    with pytest.raises(ContextViolation):
        pipeline.finish(prepared, raw_output("好"))
    assert prepared.summary != "被篡改的检索侧字段"
    assert pipeline.metrics.count("ctx.violation") == 1


# ==========================================================================
# pending 边界（§7.4 / 用例 42 / B2）
# ==========================================================================

def test_write_failure_pending_then_replay_turn_key_idempotent(pipeline, system, monkeypatch) -> None:
    original_append = system.store.append

    def boom(item):
        raise OSError("disk full")

    monkeypatch.setattr(system.store, "append", boom)
    first = pipeline.prepare(event("用户A下周三去上海出差", msgid="p-1"))
    result = pipeline.finish(first, raw_output("好的", [memory("用户A下周三去上海出差")]))
    assert result.answer == "好的"
    assert result.write_report.failed == 1
    assert system.pending_count == 1
    assert system.all_items() == []

    monkeypatch.setattr(system.store, "append", original_append)
    pipeline.prepare(event("下一轮消息", msgid="p-2"))  # prepare 开头重放一次
    assert system.pending_count == 0
    assert len(system.all_items()) == 1
    stored = system.all_items()[0]
    assert stored.turn_key == "u_1:p-1"

    # 重放后 turn_key 已登记：再次消费同一 msgid 不重复写入、不重复累加 mention_count
    mentions_before = stored.mention_count
    duplicate = pipeline.prepare(event("用户A下周三去上海出差", msgid="p-1"))
    duplicate_result = pipeline.finish(
        duplicate, raw_output("好的", [memory("用户A下周三去上海出差")])
    )
    assert duplicate_result.write_report.turn_duplicate is True
    assert len(system.all_items()) == 1
    assert system.all_items()[0].mention_count == mentions_before


def test_affect_update_failure_does_not_enter_pending(pipeline, system, monkeypatch) -> None:
    def boom(user_id, deltas, *, now=None):
        raise OSError("affect disk full")

    monkeypatch.setattr(system.affect_store, "step_update", boom)
    prepared = pipeline.prepare(event())
    sent: list[str] = []
    result = pipeline.finish(
        prepared,
        raw_output("好的", [memory("用户A下周三去上海出差")]),
        sender=sent.append,
    )
    assert sent == ["好的"]
    assert len(result.write_report.created) == 1
    assert prepared.pending == []
    assert system.pending_count == 0
    assert pipeline.metrics.count("turn.finish_error") >= 1


def test_reinforce_failure_does_not_enter_pending(pipeline, system, monkeypatch) -> None:
    system.store.append(make_item(system, content="用户A下周三去上海出差"))
    prepared = pipeline.prepare(event("上海出差怎么样"))

    def boom(*args, **kwargs):
        raise OSError("reinforce write failed")

    monkeypatch.setattr(system, "reinforce", boom)
    sent: list[str] = []
    result = pipeline.finish(
        prepared,
        raw_output("好的", [memory("用户A喜欢本帮菜", "fact")]),
        sender=sent.append,
    )
    assert sent == ["好的"]
    assert len(result.write_report.created) == 1
    assert prepared.pending == []
    assert system.pending_count == 0


# ==========================================================================
# 9b：只聚合本轮 injected_items 中当前用户且非 neutral 的 affect（§5.5）
# ==========================================================================

def _seed_bridge_scenario(system: MemorySystem) -> None:
    system.store.append(
        make_item(
            system,
            id="m_0001",
            scope="dm",
            content="私聊喜欢大白饭",
            affect={"label": "joy", "intensity": 0.5},
        )
    )
    system.store.append(
        make_item(
            system,
            id="m_0002",
            scope="group",
            content="群里说周末去爬山",
            affect={"label": "anger", "intensity": 0.9},
        )
    )
    system.store.append(
        make_item(
            system,
            id="m_0003",
            scope="group",
            user_id="u_2",
            content="群里说周末去爬山别人",
            affect={"label": "anger", "intensity": 1.0},
        )
    )


def test_9b_bridge_items_participate_but_only_same_user(pipeline, system, monkeypatch) -> None:
    _seed_bridge_scenario(system)
    prepared = pipeline.prepare(
        event("群里刚才说的周末去爬山，还有大白饭", scope="dm", user_id="u_1")
    )
    assert prepared.retrieval.bridge_hit >= 1
    bridge_items = [item for item in prepared.injected_items if item.get("bridge")]
    assert bridge_items, "桥接条目应被注入"
    assert {item["user_id"] for item in bridge_items} == {"u_1"}

    calls: list[dict] = []
    real_step = system.affect_store.step_update

    def spy(user_id, deltas, *, now=None):
        calls.append(dict(deltas))
        return real_step(user_id, deltas, now=now)

    monkeypatch.setattr(system.affect_store, "step_update", spy)
    result = pipeline.finish(prepared, raw_output("好"))
    assert result.answer == "好"

    step_8a = [call for call in calls if "familiarity" in call]
    step_9b = [call for call in calls if "familiarity" not in call]
    assert len(step_8a) == 1, "8a 每轮一次"
    assert len(step_9b) == 1, "9b 每轮每分量至多一次 step_update"
    # I_joy = max(joy 0.5) = 0.5；I_anger = max(u_1 的 0.9)，不含 u_2 的 1.0
    assert step_9b[0]["affection"] == pytest.approx(0.05)
    assert step_9b[0]["annoyance"] == pytest.approx(0.09)
    affect = system.affect_store.get("u_1")
    assert affect.familiarity == pytest.approx(0.37)
    assert affect.affection == pytest.approx(0.5 + 0.05 * 0.5)
    assert affect.annoyance == pytest.approx(0.09)


def test_9b_aggregate_max_within_family(pipeline, system) -> None:
    for index, (label, intensity) in enumerate(
        [("joy", 0.4), ("affection", 0.9), ("anger", 0.2), ("disgust", 0.7)]
    ):
        system.store.append(
            make_item(
                system,
                id="m_010" + str(index),
                scope="dm",
                content="用户A下周三去上海出差第" + str(index) + "条",
                affect={"label": label, "intensity": intensity},
            )
        )
    prepared = pipeline.prepare(event("上海出差", scope="dm"))
    pipeline.finish(prepared, raw_output("嗯"))
    deltas = prepared.metrics["affect_deltas"]
    assert deltas["affection"] == pytest.approx(0.09)  # max(joy 0.4, affection 0.9)
    assert deltas["annoyance"] == pytest.approx(0.07)  # max(anger 0.2, disgust 0.7)


# ==========================================================================
# turn.finish / retrieval.affect_bias 指标
# ==========================================================================

def test_turn_finish_metrics_bridge_and_dedup(tmp_path) -> None:
    cfg = make_config(tmp_path)
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=metrics_path)
    system = MemorySystem(cfg, metrics=metrics)
    try:
        pipeline = TurnPipeline(system, cfg)
        system.store.append(
            make_item(system, id="m_0001", scope="dm", content="重复事实内容")
        )
        system.store.append(
            make_item(system, id="m_0002", scope="group", content="重复事实内容")
        )
        prepared = pipeline.prepare(
            event("群里刚才说的重复事实内容", scope="dm", user_id="u_1")
        )
        pipeline.finish(prepared, raw_output("好"))
        finish_events = find_events(read_metric_events(metrics_path), "turn.finish")
        assert finish_events, "turn.finish 指标必须落盘"
        payload = finish_events[-1]
        injected_bridge = [
            item for item in prepared.injected_items if item.get("bridge")
        ]
        assert payload["bridge_hit"] == len(injected_bridge)
        assert payload["dedup_removed"] == prepared.retrieval.dedup_removed
        assert payload["dedup_degraded"] == prepared.retrieval.dedup_degraded
        assert payload["affect_bias"] == prepared.retrieval.affect_bias
        assert payload["injected"] == len(prepared.injected_items)
        # 桥接 + 同事实：跨池去重必须留痕（当前实现保留 dm 侧）
        assert payload["dedup_removed"] == 1
        assert metrics.count("group_bridge.dedup") == 1
    finally:
        system.close()


def test_turn_finish_metrics_affect_bias(tmp_path) -> None:
    cfg = make_config(tmp_path, retrieval={"affect_match": True})
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=metrics_path)
    system = MemorySystem(cfg, metrics=metrics)
    try:
        pipeline = TurnPipeline(system, cfg)
        first = pipeline.prepare(event("我下周三去上海出差，真烦", msgid="bias-1"))
        pipeline.finish(
            first,
            raw_output(
                "好",
                [
                    {
                        "type": "episode",
                        "content": "用户A下周三去上海出差",
                        "affect": {"label": "anger", "intensity": 0.8},
                    }
                ],
            ),
        )
        assert first.last_event_affect == {"label": "anger", "intensity": 0.8}

        second = pipeline.prepare(event("上海出差", msgid="bias-2"))
        assert second.retrieval.affect_bias >= 1
        assert metrics.count("retrieval.affect_bias") >= 1
        pipeline.finish(second, raw_output("嗯"))

        finish_events = find_events(read_metric_events(metrics_path), "turn.finish")
        assert finish_events
        assert finish_events[-1]["affect_bias"] == second.retrieval.affect_bias
        assert finish_events[-1]["affect_bias"] >= 1
        # 轮 1 提取到 anger：turn.finish 记录 last_event_affect=anger，供轮 2 affect_match
        first_finish = [
            record for record in finish_events if record.get("turn_id") == first.turn_id
        ]
        assert first_finish and first_finish[0]["last_event_affect"] == "anger"
        # 轮 2 没有新记忆 → last_event_affect 置空（neutral）
        assert finish_events[-1]["last_event_affect"] == "neutral"
    finally:
        system.close()
