"""每轮流程：先发后写 / 闸门与检索解耦 / 幂等 / 失败降级 / 强化（§7、§10.2）。"""

from __future__ import annotations

import json

import pytest

from qihun_memory.memory import MemorySystem
from qihun_memory.models import MemoryItem, utc_now_iso
from qihun_memory.pipeline import TurnEvent, TurnPipeline
from qihun_memory.prompt import EXTRACTION_PROTOCOL


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
