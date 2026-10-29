"""v1.5-r2 L4：组合场景用例 43~46（规范 §10.7）。

- 用例 43 · 组合：私聊桥接命中 5 条（合计 400 tokens）+ dm 命中 3 条，其中 2 对同事实
  且桥接侧分数更高 → 跨池去重去掉 2 条 dm；桥接仍 400 tokens → 截断到 ≤300；
  dedup=2 与 group_bridge.truncated=1 同时出现且不互相覆盖；总 tokens ≤1200；回答正常。
- 用例 44 · 时序：轮 1 提取 affect=anger:0.8；轮 2 同一 pipeline 看到 affect_match /
  affect_bias 生效；轮 3 模拟重启后 last_event_affect 为空、affect_match 自动失效、
  不被历史 memories[].affect 直接驱动、不报错。
- 用例 45 · 迁移：老 memory.jsonl 无 affect + 老 user_affect.json 含 trust → 加载不报错、
  affect=None、注入无 affect= 段、compute_S("init") 不乘 k_a。
- 用例 46 · 降级链：sidecar 缺失 + 私聊桥接触发 + 跨池去重退化 → 回答正常，
  retrieval.degraded / group_bridge.hit / dedup.degraded 三者都出现、无异常。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from qihun_memory.config import Config
from qihun_memory.lifecycle import compute_S
from qihun_memory.memory import MemorySystem
from qihun_memory.metrics import Metrics
from qihun_memory.models import MemoryItem, utc_now, utc_now_iso
from qihun_memory.pipeline import TurnEvent, TurnPipeline
from qihun_memory.tokens import estimate_tokens


# --------------------------------------------------------------------------
# 测试辅助
# --------------------------------------------------------------------------

def make_config(tmp_path, **overrides) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    data.update(overrides)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def raw_output(answer: str, memories: list[dict] | None = None) -> str:
    payload = json.dumps({"memories": memories or []}, ensure_ascii=False)
    return answer + "\n<MEMORY_JSON>" + payload + "</MEMORY_JSON>"


def event(message: str, **overrides) -> TurnEvent:
    base: dict = {"user_id": "u_1", "scope": "group", "message": message, "msgid": "msg-1"}
    base.update(overrides)
    return TurnEvent(**base)


def make_item(
    item_id: str,
    content: str,
    *,
    scope: str = "group",
    user_id: str = "u_1",
    item_type: str = "fact",
    age_days: float = 0.0,
    affect: dict | None = None,
) -> MemoryItem:
    now = utc_now()
    last = now - timedelta(days=age_days)
    return MemoryItem(
        id=item_id,
        user_id=user_id,
        scope=scope,
        type=item_type,
        content=content,
        created_at=last.isoformat(timespec="seconds"),
        last_access=last.isoformat(timespec="seconds"),
        access_count=0,
        mention_count=1,
        S=7.0,
        valence=0.0,
        status="active",
        tags=[],
        affect=affect,
    )


def read_metric_events(path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def bridge_content(index: int) -> str:
    """56 个纯 CJK 字符：head 24 + content 56 = 80 tokens/条，5 条共 400。"""
    suffix = chr(0x4E00 + index) + chr(0x4E10 + index)
    body = "上海出差项目记录内容" + "补充说明" * 11 + suffix
    assert len(body) == 56
    return body


# ==========================================================================
# 用例 43 · 组合：跨池去重 + 桥接预算截断同时发生
# ==========================================================================

def test_case43_dedup_and_bridge_truncation_coexist(tmp_path) -> None:
    cfg = make_config(tmp_path)
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=metrics_path)
    system = MemorySystem(cfg, metrics=metrics)
    try:
        # 桥接侧 5 条，每条 80 tokens（24 head + 56 内容）→ 合计 400 tokens
        for index in range(5):
            system.store.append(make_item("m_01" + str(index).zfill(2), bridge_content(index)))
        # dm 侧 3 条：其中 2 条与桥接侧同事实（同 type + content），但 last_access 更旧 →
        # 桥接侧分数更高，跨池去重保留桥接、删掉这 2 条 dm
        for index in range(2):
            system.store.append(
                make_item(
                    "m_02" + str(index).zfill(2),
                    bridge_content(index),
                    scope="dm",
                    age_days=30.0,
                )
            )
        system.store.append(
            make_item("m_0300", "私聊里提到的另一件上海出差事情", scope="dm")
        )

        pipeline = TurnPipeline(system, cfg)
        prepared = pipeline.prepare(
            event("群里刚才说的上海出差项目记录内容", scope="dm", user_id="u_1")
        )
        retrieval = prepared.retrieval
        header = cfg.retrieval.header_tokens
        cost_per_bridge = header + estimate_tokens(bridge_content(0))
        assert cost_per_bridge == 80

        # 1) 跨池去重去掉 2 条 dm（保留分数更高的桥接侧）
        assert retrieval.dedup_removed == 2
        selected_ids = {entry.item.id for entry in retrieval.selected}
        assert "m_0200" not in selected_ids and "m_0201" not in selected_ids
        # 2) 桥接仍 400 tokens（5 条候选），随后被桥接子预算截断到 ≤300
        assert retrieval.bridge_candidates == 5
        assert retrieval.bridge_candidates * cost_per_bridge == 400
        assert retrieval.bridge_truncated is True
        bridge_injected = [item for item in prepared.injected_items if item["bridge"]]
        assert bridge_injected
        bridge_tokens = sum(item["tokens"] for item in bridge_injected)
        assert 0 < bridge_tokens <= 300
        # 3) 总 tokens ≤1200，回答正常
        assert retrieval.tokens <= 1200
        # 4) dedup=2 与 truncated=1 同时出现且不互相覆盖
        assert metrics.count("group_bridge.dedup") == 1
        assert metrics.count("group_bridge.truncated") == 1
        dedup_events = [
            record for record in read_metric_events(metrics_path) if record["event"] == "group_bridge.dedup"
        ]
        assert dedup_events and dedup_events[-1]["count"] == 2

        result = pipeline.finish(prepared, raw_output("好的"))
        assert result.answer == "好的"
        assert result.metrics["dedup_removed"] == 2
        assert result.metrics["bridge_hit"] == len(bridge_injected)
    finally:
        system.close()


# ==========================================================================
# 用例 44 · 时序：last_event_affect 跨轮 + 重启失效
# ==========================================================================

def test_case44_affect_timing_across_restart(tmp_path) -> None:
    cfg = make_config(tmp_path, retrieval={"affect_match": True})
    assert cfg.retrieval.affect_match is True
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=metrics_path)
    system = MemorySystem(cfg, metrics=metrics)
    try:
        pipeline = TurnPipeline(system, cfg)
        # 轮 1：提取 affect=anger:0.8
        first = pipeline.prepare(event("我下周三去上海出差，真烦", msgid="r1"))
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

        # 轮 2：同一 pipeline → affect_match 生效，affect_bias 反映匹配条数
        second = pipeline.prepare(event("上海出差", msgid="r2"))
        assert second.last_event_affect_at_read == {"label": "anger", "intensity": 0.8}
        assert second.retrieval.affect_bias >= 1
        assert metrics.count("retrieval.affect_bias") >= 1
        pipeline.finish(second, raw_output("嗯"))
    finally:
        system.close()

    # 轮 3：模拟进程重启（新建 system 从磁盘加载 + 新 TurnPipeline）
    restarted = MemorySystem(cfg, metrics=metrics)
    try:
        pipeline3 = TurnPipeline(restarted, cfg)
        third = pipeline3.prepare(event("上海出差", msgid="r3"))
        assert third.last_event_affect_at_read is None
        assert third.retrieval.affect_bias == 0
        # 历史 memories[].affect 仍在，但不得直接驱动 affect_bias（R4）
        assert any(
            item.affect and item.affect.get("label") == "anger"
            for item in restarted.all_items()
        )
        result = pipeline3.finish(third, raw_output("嗯"))
        assert result.answer == "嗯"
        finish_events = [
            record
            for record in read_metric_events(metrics_path)
            if record["event"] == "turn.finish" and record.get("turn_id") == third.turn_id
        ]
        assert finish_events and finish_events[0]["affect_bias"] == 0
    finally:
        restarted.close()


# ==========================================================================
# 用例 45 · 迁移：老 memory.jsonl 无 affect + 老 user_affect.json 含 trust
# ==========================================================================

def test_case45_old_data_without_affect_and_trust_field(tmp_path) -> None:
    cfg = make_config(tmp_path)
    now = utc_now_iso()
    old_record = {
        "id": "m_0001",
        "user_id": "u_1",
        "scope": "group",
        "type": "fact",
        "content": "用户A下周三去上海出差",
        "created_at": now,
        "last_access": now,
        "access_count": 0,
        "mention_count": 1,
        "S": 30.0,
        "valence": 0.0,
        "status": "active",
        "tags": [],
    }
    cfg.memory_file.write_text(json.dumps(old_record, ensure_ascii=False) + "\n", encoding="utf-8")
    cfg.user_affect_file.write_text(
        json.dumps(
            {
                "u_1": {
                    "affection": 0.7,
                    "annoyance": 0.2,
                    "familiarity": 0.6,
                    "updated_at": now,
                    "trust": 0.9,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    metrics = Metrics()
    system = MemorySystem(cfg, metrics=metrics)  # 不崩溃
    try:
        items = system.all_items()
        assert len(items) == 1
        assert items[0].affect is None
        # 老 user_affect.json 的 trust 被忽略
        affect = system.affect_store.get("u_1")
        assert affect.affection == pytest.approx(0.7)
        assert "trust" not in system.affect_store.snapshot()["u_1"]
        # 新写入记忆：affect=None，compute_S("init") 不乘 k_a → S = 裸 S0
        report = system.remember(
            [{"type": "fact", "content": "迁移后的新事实"}],
            user_id="u_1",
            scope="group",
            valence=0.0,
        )
        assert report.created
        new_item = report.created[0]
        assert new_item.affect is None
        assert new_item.S == pytest.approx(compute_S("init", "fact", valence=0.0))
        assert new_item.S == pytest.approx(30.0)
        # 注入无 affect= 段，检索/写入正常
        pipeline = TurnPipeline(system, cfg)
        prepared = pipeline.prepare(event("上海出差", scope="group"))
        assert prepared.injected_items
        assert all(item.get("affect") is None for item in prepared.injected_items)
        assert "affect=" not in prepared.prompt.memory_block
        result = pipeline.finish(prepared, raw_output("好"))
        assert result.answer == "好"
    finally:
        system.close()


# ==========================================================================
# 用例 46 · 降级链：sidecar 缺失 + 桥接 + 跨池去重退化
# ==========================================================================

def test_case46_degradation_chain_end_to_end(tmp_path) -> None:
    cfg = make_config(tmp_path)
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=metrics_path)
    system = MemorySystem(cfg, metrics=metrics)
    try:
        pipeline = TurnPipeline(system, cfg)
        system.store.append(make_item("m_0001", "私聊内容", scope="dm"))
        system.store.append(make_item("m_0002", "群里内容", scope="group"))
        # sidecar 缺失（从未生成）+ 向量不可用
        assert not cfg.vector_file.exists()

        prepared = pipeline.prepare(
            event("群里刚才说的那个事情", scope="dm", user_id="u_1", msgid="r46")
        )
        assert prepared.retrieval.degraded is True
        assert prepared.retrieval.bridge_hit >= 1
        assert prepared.retrieval.dedup_degraded is True

        result = pipeline.finish(prepared, "好的")
        assert result.answer == "好的"
        assert metrics.count("retrieval.degraded") >= 1
        assert metrics.count("group_bridge.hit") >= 1
        assert metrics.count("dedup.degraded") >= 1

        finish_events = [
            record
            for record in read_metric_events(metrics_path)
            if record["event"] == "turn.finish" and record.get("turn_id") == prepared.turn_id
        ]
        assert finish_events
        payload = finish_events[0]
        assert payload["retrieval_degraded"] is True
        assert payload["bridge_hit"] >= 1
        assert payload["dedup_degraded"] is True
    finally:
        system.close()
