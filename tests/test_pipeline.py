"""每轮流程：先发后写 / 闸门与检索解耦 / 幂等 / 失败降级 / 强化（§7、§10.2）。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from qihun_memory.config import Config
from qihun_memory.lifecycle import compute_S
from qihun_memory.memory import MemorySystem
from qihun_memory.models import MemoryItem, utc_now, utc_now_iso
from qihun_memory.pipeline import (
    ContextViolation,
    TurnEvent,
    TurnPipeline,
    aggregate,
    first_non_neutral,
)
from qihun_memory.prompt import EXTRACTION_PROTOCOL
from qihun_memory.retrieval import RetrievalResult


def raw_output(answer: str, memories: list[dict] | None = None) -> str:
    payload = json.dumps({"memories": memories or []}, ensure_ascii=False)
    block = "<MEMORY_JSON>\n" + payload + "\n</MEMORY_JSON>"
    return (answer + "\n" + block) if answer else block


def memory(content: str, item_type: str = "episode", tags: list[str] | None = None) -> dict:
    return {"type": item_type, "content": content, "tags": tags or ["测试"]}


def make_item(system: MemorySystem, **overrides) -> MemoryItem:
    now = utc_now_iso()
    base = dict(
        id=system.store.next_id(),
        user_id="u_1",
        scope="group",
        type="episode",
        content="用户A下周三去上海出差",
        created_at=now,
        last_access=now,
        access_count=0,
        mention_count=1,
        S=7.0,
        valence=0.0,
        status="active",
        tags=[],
    )
    base.update(overrides)
    return MemoryItem(**base)


@pytest.fixture()
def system(config) -> MemorySystem:
    return MemorySystem(config)


@pytest.fixture()
def pipeline(system) -> TurnPipeline:
    return TurnPipeline(system, system.cfg)


def event(message: str = "我下周三去上海出差", **overrides) -> TurnEvent:
    base = dict(user_id="u_1", scope="group", message=message, msgid="msg-1")
    base.update(overrides)
    return TurnEvent(**base)


def test_basic_turn_sends_before_writing(pipeline, system) -> None:
    sent: list[tuple[str, int]] = []
    prepared = pipeline.prepare(event())
    assert prepared.gate_passed is True
    assert prepared.light_path is False
    assert EXTRACTION_PROTOCOL in prepared.prompt.system_prompt

    result = pipeline.finish(
        prepared,
        raw_output("好的，记下了。", [memory("用户A下周三去上海出差")]),
        sender=lambda text: sent.append((text, len(system.all_items()))),
    )
    assert result.answer == "好的，记下了。"
    assert result.status == "ok"
    assert len(result.write_report.created) == 1
    assert sent == [("好的，记下了。", 0)]  # 发送时还没有任何写入（§7 顺序 7→8）
    assert len(system.all_items()) == 1
    assert system.metrics.count("turn.finish") == 1
    assert system.metrics.count("mem_extract.ok") == 1


def test_finish_without_memories(pipeline, system) -> None:
    prepared = pipeline.prepare(event())
    sent: list[str] = []
    result = pipeline.finish(prepared, raw_output("好呀"), sender=sent.append)
    assert result.status == "empty"
    assert sent == ["好呀"]
    assert system.all_items() == []
    assert system.metrics.count("mem_extract.empty") == 1


def test_no_sentinel_when_gate_passed_metric(pipeline) -> None:
    prepared = pipeline.prepare(event())
    result = pipeline.finish(prepared, "只是普通回答，没有协议块。")
    assert result.status == "no_sentinel"
    assert pipeline.metrics.count("mem_extract.no_sentinel") == 1


def test_truncated_json_answer_never_leaks(pipeline, system) -> None:
    prepared = pipeline.prepare(event())
    broken = "好的，记下了。\n<MEMORY_JSON>\n{\"memories\":[{\"type\":\"episode\",\"content\":\"用户A下周三"
    sent: list[str] = []
    result = pipeline.finish(prepared, broken, sender=sent.append)
    assert result.status == "bad_json"
    assert sent == ["好的，记下了。"]
    assert "memories" not in result.answer
    assert system.all_items() == []
    assert pipeline.metrics.count("mem_extract.bad_json") >= 1


def test_empty_answer_with_memories_no_air_bubble(pipeline, system) -> None:
    prepared = pipeline.prepare(event())
    sent: list[str] = []
    result = pipeline.finish(
        prepared,
        raw_output("", [memory("用户A下周三去上海出差")]),
        sender=sent.append,
    )
    assert result.answer == ""
    assert sent == []
    assert len(system.all_items()) == 1


def test_turn_key_duplicate_consumption(pipeline, system) -> None:
    first = pipeline.prepare(event(msgid="msg-42"))
    pipeline.finish(first, raw_output("好的", [memory("用户A下周三去上海出差")]))
    second = pipeline.prepare(event(msgid="msg-42"))
    result = pipeline.finish(second, raw_output("好的", [memory("用户A下周三去上海出差")]))
    assert result.write_report.turn_duplicate is True
    assert len(system.all_items()) == 1


def test_light_path_skips_retrieval_but_gate_independent(pipeline) -> None:
    prepared = pipeline.prepare(event("在吗"))
    assert prepared.light_path is True
    assert prepared.gate_passed is False
    assert prepared.retrieval.selected == []
    assert EXTRACTION_PROTOCOL not in prepared.prompt.system_prompt

    prepared2 = pipeline.prepare(event("开会好累"))
    assert prepared2.light_path is False  # 有信息量 → 检索
    assert prepared2.gate_passed is False  # 但不附提取协议
    assert EXTRACTION_PROTOCOL not in prepared2.prompt.system_prompt


def test_summary_and_recent_turns(pipeline) -> None:
    for index in range(7):
        prepared = pipeline.prepare(event(f"第{index}条消息内容"))
        pipeline.finish(prepared, raw_output(f"回答{index}"))
    prepared = pipeline.prepare(event("第7条消息内容"))
    contents = [message["content"] for message in prepared.prompt.messages]
    recent = "".join(contents[:-1])  # 最近轮次（最后一条含摘要 + 当前消息）
    assert len(prepared.prompt.messages) == 11
    assert "第0条消息内容" not in recent
    assert "第6条消息内容" in recent
    summary = pipeline.summaries.get("group")
    assert "第6条消息内容" in summary
    assert "【会话摘要】" in contents[-1]


def test_reinforce_selected_memory(pipeline, system) -> None:
    item = make_item(system, content="用户A下周三去上海出差")
    decoy = make_item(system, id=system.store.next_id(), content="用户B喜欢本帮菜")
    system.store.append(item)
    system.store.append(decoy)
    prepared = pipeline.prepare(event("上海出差怎么样"))
    assert prepared.retrieval.selected
    result = pipeline.finish(prepared, raw_output("还行"))
    assert result.reinforced >= 1
    assert item.access_count >= 1
    assert item.S > 7.0


def test_unknown_prepared_raises(pipeline) -> None:
    prepared = pipeline.prepare(event())
    pipeline.finish(prepared, raw_output("好"))
    with pytest.raises(ValueError):
        pipeline.finish(prepared, raw_output("好"))


def test_run_turn_convenience(pipeline, system) -> None:
    result = pipeline.run_turn(
        event(),
        lambda prompt: raw_output("收到", [memory("用户A下周三去上海出差")]),
    )
    assert result.answer == "收到"
    assert len(system.all_items()) == 1
    assert pipeline.metrics.count("mem_extract.ok") == 1


def test_retrieval_degraded_metric(pipeline, system) -> None:
    system.store.append(make_item(system, content="用户A下周三去上海出差"))
    prepared = pipeline.prepare(event("上海出差怎么样"))
    assert prepared.retrieval.degraded is True
    pipeline.finish(prepared, raw_output("嗯"))
    assert pipeline.metrics.count("retrieval.degraded") >= 1

# ---------------------------------------------------------------------------
# v1.5-r2：ctx 只读冻结（§7.2 / 用例 41）
# ---------------------------------------------------------------------------

def test_ctx_is_frozen_but_three_fields_writable(pipeline) -> None:
    prepared = pipeline.prepare(event())
    frozen_values = {
        "persona": "新人格",
        "state": prepared.state,
        "summary": "新摘要",
        "candidates": 99,
        "retrieval": prepared.retrieval,
        "prompt": prepared.prompt,
        "injected_items": [],
        "user_id": "u_2",
        "scope": "dm",
        "gate_passed": False,
        "turn_key": "hacked",
        "light_path": True,
        "last_event_affect_at_read": {"label": "joy", "intensity": 1.0},
        "turn_id": "x",
        "now": prepared.now,
    }
    for name, value in frozen_values.items():
        with pytest.raises(ContextViolation):
            setattr(prepared, name, value)
    assert pipeline.metrics.count("ctx.violation") == len(frozen_values)
    # 冻结字段保持不变
    assert prepared.user_id == "u_1"
    assert prepared.scope == "group"
    assert prepared.gate_passed is True
    # 可写白名单
    prepared.last_event_affect = {"label": "anger", "intensity": 0.5}
    prepared.metrics = {"custom": 1}
    prepared.pending = [{"type": "fact", "content": "x"}]
    prepared.pending.append({"type": "fact", "content": "y"})
    assert prepared.last_event_affect["label"] == "anger"
    assert prepared.metrics == {"custom": 1}
    assert len(prepared.pending) == 2


# ---------------------------------------------------------------------------
# v1.5-r2：顺序锁定 emit → summary → write_back；8a → 8b → 9a → 9b（§7.3 / D6）
# ---------------------------------------------------------------------------

class RecordingRetriever:
    """只记录 search 入参的假检索器（不产生候选）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(
        self,
        items,
        query,
        *,
        scope,
        user_id,
        bot_state=None,
        now=None,
        user_affect=None,
        last_event_affect=None,
    ) -> RetrievalResult:
        self.calls.append(
            {
                "query": query,
                "scope": scope,
                "user_id": user_id,
                "user_affect": user_affect,
                "last_event_affect": last_event_affect,
            }
        )
        return RetrievalResult(
            selected=[], candidates=0, tokens=0, budget=1200, degraded=False
        )


def make_config(tmp_path, **overrides) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    data.update(overrides)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_write_back_order_and_single_step_update(pipeline, system, monkeypatch) -> None:
    system.store.append(
        make_item(
            system,
            content="用户A下周三去上海出差",
            affect={"label": "joy", "intensity": 0.8},
        )
    )
    prepared = pipeline.prepare(event("上海出差怎么样"))
    assert prepared.injected_items  # fill 产物先于 write_back（9b 在 fill 之后）

    order: list[str] = []
    original_step = system.affect_store.step_update
    original_remember = system.remember
    original_reinforce = system.reinforce
    original_summary = pipeline.summaries.update

    def step(user_id, deltas, *, now=None):
        order.append("9b" if "familiarity" not in deltas else "8a")
        return original_step(user_id, deltas, now=now)

    def remember(*args, **kwargs):
        order.append("8b")
        return original_remember(*args, **kwargs)

    def reinforce(*args, **kwargs):
        order.append("9a")
        return original_reinforce(*args, **kwargs)

    def summary(*args, **kwargs):
        order.append("summary")
        return original_summary(*args, **kwargs)

    monkeypatch.setattr(system.affect_store, "step_update", step)
    monkeypatch.setattr(system, "remember", remember)
    monkeypatch.setattr(system, "reinforce", reinforce)
    monkeypatch.setattr(pipeline.summaries, "update", summary)

    result = pipeline.finish(
        prepared,
        raw_output(
            "好的",
            [memory("用户A喜欢本帮菜", "fact")],
        ),
        sender=lambda _text: order.append("emit"),
    )
    assert len(result.write_report.merged) == 0
    assert order[:3] == ["emit", "summary", "8a"]
    assert order.count("8a") == 1
    assert order.count("8b") == 1
    assert order.count("9b") == 1
    assert order.index("8a") < order.index("8b") < order.index("9a") < order.index("9b")
    assert len(result.write_report.created) == 1


def test_8a_familiarity_step_once_per_turn(pipeline, system) -> None:
    prepared = pipeline.prepare(event())
    pipeline.finish(prepared, raw_output("好"))
    affect = system.affect_store.get("u_1")
    assert affect.familiarity == pytest.approx(0.3 + 0.1 * (1 - 0.3))  # 0.37
    assert affect.affection == pytest.approx(0.5)
    assert affect.annoyance == pytest.approx(0.0)


def test_write_failure_goes_pending_and_keeps_answer(pipeline, system, monkeypatch) -> None:
    def boom(item):
        raise OSError("disk full")

    monkeypatch.setattr(system.store, "append", boom)
    prepared = pipeline.prepare(event())
    sent: list[str] = []
    result = pipeline.finish(
        prepared,
        raw_output("好的", [memory("用户A下周三去上海出差")]),
        sender=sent.append,
    )
    assert result.answer == "好的"
    assert sent == ["好的"]
    assert result.write_report.failed == 1
    assert len(prepared.pending) == 1  # ctx.pending 只追加待重放新记忆
    assert system.pending_count == 1
    assert pipeline.metrics.count("mem_write.fail") >= 1


def test_pending_replayed_at_next_prepare(pipeline, system, monkeypatch) -> None:
    original_append = system.store.append

    def boom(item):
        raise OSError("disk full")

    monkeypatch.setattr(system.store, "append", boom)
    prepared = pipeline.prepare(event(msgid="p1"))
    pipeline.finish(prepared, raw_output("好的", [memory("用户A下周三去上海出差")]))
    assert system.pending_count == 1
    assert system.all_items() == []

    monkeypatch.setattr(system.store, "append", original_append)
    pipeline.prepare(event("下一轮消息", msgid="p2"))  # 下一轮开始前重放 1 次
    assert system.pending_count == 0
    assert len(system.all_items()) == 1


def test_duplicate_turn_skips_write_back_side_effects(pipeline, system) -> None:
    first = pipeline.prepare(event(msgid="dup-1"))
    pipeline.finish(first, raw_output("好的", [memory("用户A下周三去上海出差")]))
    familiarity_before = system.affect_store.get("u_1").familiarity
    affection_before = system.affect_store.get("u_1").affection

    second = pipeline.prepare(event(msgid="dup-1"))
    result = pipeline.finish(second, raw_output("好的", [memory("用户A下周三去上海出差")]))
    assert result.write_report.turn_duplicate is True
    affect = system.affect_store.get("u_1")
    assert affect.familiarity == pytest.approx(familiarity_before)  # 8a 不重复
    assert affect.affection == pytest.approx(affection_before)  # 9b 不重复
    assert len(system.all_items()) == 1


# ---------------------------------------------------------------------------
# v1.5-r2：9b 感情聚合 / 10 last_event_affect（§5.5 / 用例 44）
# ---------------------------------------------------------------------------

def test_9b_aggregates_injected_affect_for_current_user(pipeline, system) -> None:
    system.store.append(
        make_item(system, content="用户A下周三去上海出差", affect={"label": "joy", "intensity": 0.8})
    )
    system.store.append(
        make_item(
            system,
            id=system.store.next_id(),
            content="用户A周末去杭州",
            affect={"label": "anger", "intensity": 1.0},
        )
    )
    prepared = pipeline.prepare(event("上海出差和杭州"))
    pipeline.finish(prepared, raw_output("嗯"))
    affect = system.affect_store.get("u_1")
    assert affect.affection == pytest.approx(0.5 + 0.1 * 0.8 * (1 - 0.5))  # 0.54
    assert affect.annoyance == pytest.approx(0.1)
    deltas = prepared.metrics["affect_deltas"]
    assert deltas["affection"] == pytest.approx(0.08)
    assert deltas["annoyance"] == pytest.approx(0.1)


def test_9b_ignores_other_users_and_neutral(pipeline, system) -> None:
    system.store.append(
        make_item(system, user_id="u_2", content="用户B下周三去上海出差", affect={"label": "anger", "intensity": 1.0})
    )
    system.store.append(
        make_item(
            system,
            id=system.store.next_id(),
            content="用户A下周三去上海出差",
            affect={"label": "neutral", "intensity": 1.0},
        )
    )
    prepared = pipeline.prepare(event("上海出差"))
    pipeline.finish(prepared, raw_output("嗯"))
    affect = system.affect_store.get("u_1")
    assert affect.annoyance == pytest.approx(0.0)
    assert affect.affection == pytest.approx(0.5)


def test_last_event_affect_written_and_used_next_turn(pipeline, system) -> None:
    prepared = pipeline.prepare(event(msgid="aff-1"))
    pipeline.finish(
        prepared,
        raw_output(
            "好",
            [{"type": "episode", "content": "用户B周末去杭州", "affect": {"label": "anger", "intensity": 0.8}}],
        ),
    )
    assert prepared.last_event_affect == {"label": "anger", "intensity": 0.8}

    # 进程内下一轮：prepare 把上一轮 affect 传给检索（affect_match 输入）
    fresh = TurnPipeline(system, system.cfg)
    assert fresh.prepare(event("上海出差怎么样")).last_event_affect_at_read is None  # 模拟重启

    recorder = RecordingRetriever()
    system.retriever = recorder
    pipeline.prepare(event("上海出差怎么样", msgid="aff-2"))
    assert recorder.calls[-1]["last_event_affect"] == {"label": "anger", "intensity": 0.8}


def test_retrieval_affect_bias_metric(tmp_path) -> None:
    cfg = make_config(tmp_path, retrieval={"affect_match": True})
    system = MemorySystem(cfg)
    pipeline = TurnPipeline(system, cfg)
    first = pipeline.prepare(event("我下周三去上海出差，烦", msgid="bias-1"))
    pipeline.finish(
        first,
        raw_output(
            "好",
            [{"type": "episode", "content": "用户A下周三去上海出差", "affect": {"label": "anger", "intensity": 0.8}}],
        ),
    )
    second = pipeline.prepare(event("上海出差", msgid="bias-2"))
    assert second.retrieval.affect_bias >= 1
    assert pipeline.metrics.count("retrieval.affect_bias") >= 1


def test_state_valence_snapshot_used_for_new_memories(pipeline, system) -> None:
    timestamp = utc_now().isoformat(timespec="seconds")
    system.cfg.bot_state_file.write_text(
        json.dumps({"valence": 0.9, "avoidance": 0.0, "updated_at": timestamp}),
        encoding="utf-8",
    )
    prepared = pipeline.prepare(event())
    assert prepared.state.valence == pytest.approx(0.9)
    result = pipeline.finish(
        prepared,
        raw_output("好", [{"type": "fact", "content": "用户A下周三去上海出差"}]),
    )
    item = result.write_report.created[0]
    assert item.valence == pytest.approx(0.9)
    assert item.S == pytest.approx(compute_S("init", "fact", valence=0.9))


# ---------------------------------------------------------------------------
# v1.5-r2：聚合纯函数（唯一 aggregate / first_non_neutral）
# ---------------------------------------------------------------------------

def test_aggregate_takes_max_per_family_and_only_current_user() -> None:
    injected = [
        {"user_id": "u_1", "affect": {"label": "joy", "intensity": 0.4}},
        {"user_id": "u_1", "affect": {"label": "affection", "intensity": 0.9}},
        {"user_id": "u_1", "affect": {"label": "anger", "intensity": 0.2}},
        {"user_id": "u_1", "affect": {"label": "disgust", "intensity": 0.7}},
        {"user_id": "u_2", "affect": {"label": "anger", "intensity": 1.0}},
        {"user_id": "u_1", "affect": {"label": "neutral", "intensity": 1.0}},
        {"user_id": "u_1", "affect": None},
    ]
    deltas = aggregate(injected, user_id="u_1", alpha=0.1)
    assert deltas["affection"] == pytest.approx(0.09)
    assert deltas["annoyance"] == pytest.approx(0.07)
    assert aggregate([], user_id="u_1", alpha=0.1) == {}


def test_ctx_violation_inside_write_back_propagates(pipeline, system, monkeypatch) -> None:
    prepared = pipeline.prepare(event())
    original_step = system.affect_store.step_update

    def evil(user_id, deltas, *, now=None):
        prepared.persona = "被篡改的检索侧字段"
        return original_step(user_id, deltas, now=now)

    monkeypatch.setattr(system.affect_store, "step_update", evil)
    with pytest.raises(ContextViolation):
        pipeline.finish(prepared, raw_output("好"))
    assert prepared.persona != "被篡改的检索侧字段"
    assert pipeline.metrics.count("ctx.violation") == 1


def test_first_non_neutral() -> None:
    memories = [
        {"type": "episode"},
        {"type": "episode", "affect": {"label": "neutral", "intensity": 1.0}},
        {"type": "episode", "affect": {"label": "joy", "intensity": 0.3}},
    ]
    assert first_non_neutral(memories) == {"label": "joy", "intensity": 0.3}
    assert first_non_neutral([{"type": "episode"}]) is None
    assert first_non_neutral([]) is None
