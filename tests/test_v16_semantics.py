"""v1.6-rc4 单点级语义用例：E1 / E4 / E5 / C11 + maintain 加固。

对应 review/QiHun-Memory-System-v1.6-rc4.md 与 review/V16-IMPLEMENTATION-CONTRACT.md §3：
- E1（§3.1）：新建条目 mention_count=0；
- E4（§6.3）：写回去重范围只含同 user+scope+type 的 active 条目；命中 active 只
  Set(last_access)+Inc(mention_count,1)，不 compute_S、不强化 S、不写 JSONL 新行、
  不进 pending；dormant 不参与比对、不因去重复活；失败记 mem_write.mention_fail；
- E5（§6.1）：dormant 且 sim>0.7 → 只 revive（Set S/status/last_access），
  不 Inc(access_count)、不叠 reinforce；同轮用 revived_this_turn 去重；
- C11（§12.3）：compute_S("init") 对 |valence| 对称、valence=0 → 7；
  revive / reinforce 不受 valence 影响；
- maintain() 逐阶段 try/except 加固（§3.5）：失败只记指标，不抛给聊天主流程。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from qihun_memory.lifecycle import compute_S
from qihun_memory.memory import MemorySystem
from qihun_memory.models import MemoryItem, utc_now, utc_now_iso
from qihun_memory.retrieval import RetrievalResult, SelectedMemory


@pytest.fixture()
def system(config) -> MemorySystem:
    return MemorySystem(config)


def make_item(system: MemorySystem, **overrides) -> MemoryItem:
    """直接构造并落一条记忆（绕过 remember，便于指定 dormant / S / mention_count）。"""
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
        mention_count=0,
        S=7.0,
        valence=0.0,
        status="active",
        tags=["出差"],
    )
    base.update(overrides)
    item = MemoryItem(**base)
    system.store.append(item)
    return item


def extracted(content: str = "用户A下周三去上海出差", **overrides) -> dict:
    item = {"type": "episode", "content": content, "tags": ["出差"]}
    item.update(overrides)
    return item


def entry(item: MemoryItem, *, sim: float = 0.9, r: float = 0.5) -> SelectedMemory:
    """构造一条「被选中并注入」的检索条目（sim 决定强化/复活判定）。"""
    return SelectedMemory(item=item, score=1.0, r=r, cos=sim, bm25=sim, sim=sim)


def result(*entries: SelectedMemory) -> RetrievalResult:
    return RetrievalResult(
        selected=list(entries),
        candidates=len(entries),
        tokens=0,
        budget=1200,
        degraded=False,
    )


# ---------------------------------------------------------------------------
# E1：新建条目 mention_count=0（§3.1）
# ---------------------------------------------------------------------------


def test_e1_memory_item_default_mention_count_is_zero() -> None:
    now = utc_now_iso()
    item = MemoryItem(
        id="m_0001",
        user_id="u_1",
        scope="group",
        type="episode",
        content="用户A下周三去上海出差",
        created_at=now,
        last_access=now,
    )
    assert item.mention_count == 0
    assert item.to_dict()["mention_count"] == 0
    assert MemoryItem.from_dict(item.to_dict()).mention_count == 0


def test_e1_remember_new_item_mention_count_is_zero(system) -> None:
    report = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    item = report.created[0]
    assert item.mention_count == 0
    lines = system.cfg.memory_file.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["mention_count"] == 0


# ---------------------------------------------------------------------------
# E4：去重范围 active-only；命中 active 只 Set(last_access)+Inc(mention_count,1)
# ---------------------------------------------------------------------------


def test_e4_dedupe_hits_active_only_and_just_counts_mention(system) -> None:
    base = utc_now()
    first = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0, now=base)
    item = first.created[0]
    s_before = item.S
    later = base + timedelta(hours=1)
    report = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0, now=later)
    assert report.created == []
    assert report.merged == [item]
    assert item.mention_count == 1  # E1 新建 0 → E4 去重命中 active +1
    assert item.S == pytest.approx(s_before)  # E4：不 compute_S、不强化 S
    assert item.access_count == 0  # 重复提及不是召回
    assert item.last_access == later.isoformat(timespec="seconds")  # E4：Set(last_access)
    assert system.metrics.count("memory.deduped") == 1
    assert system.pending_count == 0  # E4：不进 pending
    lines = system.cfg.memory_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # E4：不写 JSONL 新行（只改内存，磁盘由全量重写镜像）
    assert json.loads(lines[0])["mention_count"] == 0  # append-only 日志不追加状态行
    system.store.rewrite()  # 全量重写后磁盘镜像与内存一致
    assert json.loads(
        system.cfg.memory_file.read_text(encoding="utf-8").strip()
    )["mention_count"] == 1


def test_e4_dedupe_does_not_touch_dormant(system) -> None:
    # dormant 不参与 mention 比对，也不会因去重命中而复活（E4b）
    dormant = make_item(system, status="dormant", mention_count=3, S=2.5)
    report = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    assert len(report.created) == 1  # dormant 不入候选 → 走新建
    assert report.created[0].id != dormant.id
    assert dormant.status == "dormant"  # 不复活
    assert dormant.mention_count == 3  # 不计数
    assert dormant.S == pytest.approx(2.5)  # 不触发 compute_S
    assert len(system.all_items()) == 2
    assert system.metrics.count("memory.revived") == 0


def test_e4_dedupe_scope_is_same_user_scope_type(system) -> None:
    # 去重范围 = 同 user_id + 同 scope + 同 type 的 active 条目（§6.3）
    base = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    assert len(base.created) == 1
    same_content = extracted()
    assert len(system.remember([same_content], user_id="u_1", scope="dm", valence=0.0).created) == 1
    assert len(system.remember([same_content], user_id="u_2", scope="group", valence=0.0).created) == 1
    assert (
        len(
            system.remember(
                [extracted(**{"type": "fact"})], user_id="u_1", scope="group", valence=0.0
            ).created
        )
        == 1
    )
    assert len(system.all_items()) == 4
    assert all(item.mention_count == 0 for item in system.all_items())


def test_e4_dedupe_update_failure_records_mention_fail(system, monkeypatch) -> None:
    make_item(system, mention_count=0)
    monkeypatch.setattr(system, "update_memory", lambda *args, **kwargs: False)
    report = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    assert report.created == []
    assert report.merged == []
    assert report.skipped == 1
    assert system.metrics.count("mem_write.mention_fail") == 1
    assert system.pending_count == 0
    assert len(system.all_items()) == 1


# ---------------------------------------------------------------------------
# E5：dormant sim>0.7 只 revive；同轮不叠 reinforce
# ---------------------------------------------------------------------------


def test_e5_dormant_strong_match_revives_without_access_count(system) -> None:
    item = make_item(system, status="dormant", S=1.0)
    refresh = utc_now() + timedelta(seconds=5)
    changed = system.reinforce(result(entry(item, sim=0.8)), now=refresh)
    assert changed == 1
    assert item.status == "active"
    assert item.S == pytest.approx(7.0)  # compute_S("revive") = 裸 S0
    assert item.access_count == 0  # E5：复活不 Inc(access_count)
    assert item.last_access == refresh.isoformat(timespec="seconds")
    assert system.metrics.count("memory.revived") == 1
    assert system.metrics.count("memory.reinforced") == 0  # E5：不叠 reinforce


def test_e5_revived_this_turn_blocks_reinforce(system) -> None:
    item = make_item(system, status="dormant", S=1.0)
    revived: set[str] = set()
    refresh = utc_now() + timedelta(seconds=5)
    assert system.reinforce(result(entry(item, sim=0.8)), now=refresh, revived_this_turn=revived) == 1
    assert revived == {item.id}
    assert item.access_count == 0
    assert item.S == pytest.approx(7.0)
    # 同轮同一条再次被选中（此时已 active）：revived_this_turn 拦住，不叠 reinforce
    assert (
        system.reinforce(result(entry(item, sim=0.95)), now=refresh, revived_this_turn=revived)
        == 0
    )
    assert item.access_count == 0
    assert item.S == pytest.approx(7.0)
    # 轮末丢弃 revived_this_turn → 下一轮可正常强化
    next_round = refresh + timedelta(seconds=1)
    assert system.reinforce(result(entry(item, sim=0.95)), now=next_round) == 1
    assert item.access_count == 1
    assert item.S > 7.0


def test_e5_reinforce_threshold_is_strictly_greater_than_0_5(system) -> None:
    item = make_item(system)
    assert system.reinforce(result(entry(item, sim=0.5))) == 0  # sim<=0.5 不强化
    assert item.access_count == 0
    assert system.reinforce(result(entry(item, sim=0.51))) == 1  # sim>0.5 才强化
    assert item.access_count == 1
    assert item.S > 7.0


def test_e5_dormant_mid_similarity_not_revived(system) -> None:
    item = make_item(system, status="dormant", S=1.0)
    assert system.reinforce(result(entry(item, sim=0.6))) == 0  # sim<=0.7 不复活
    assert item.status == "dormant"
    assert item.S == pytest.approx(1.0)
    assert item.access_count == 0


# ---------------------------------------------------------------------------
# C11：valence 对称 / revive、reinforce 不受 valence 影响（§12.3）
# ---------------------------------------------------------------------------


def test_c11_valence_symmetry_and_revive_reinforce_ignore_valence() -> None:
    plus = compute_S("init", "episode", valence=1.0)
    minus = compute_S("init", "episode", valence=-1.0)
    assert plus == pytest.approx(minus) == pytest.approx(10.5)  # |valence| 对称
    assert compute_S("init", "episode", valence=0.0) == pytest.approx(7.0)  # 中性 → 裸 S0
    assert compute_S("init", "fact", valence=1.0) == pytest.approx(
        compute_S("init", "fact", valence=-1.0)
    )
    # revive 裸 S0，不受 valence 影响
    assert compute_S("revive", "episode", valence=1.0) == pytest.approx(7.0)
    assert compute_S("revive", "episode", valence=-1.0) == pytest.approx(7.0)
    # reinforce 只看旧 S 与 R，不看 valence
    reinforced_plus = compute_S("reinforce", "episode", valence=1.0, S=7.0, R=0.5)
    reinforced_minus = compute_S("reinforce", "episode", valence=-1.0, S=7.0, R=0.5)
    assert reinforced_plus == pytest.approx(reinforced_minus) == pytest.approx(17.5)


# ---------------------------------------------------------------------------
# maintain() 逐阶段加固：失败只记指标、不抛（§3.5）
# ---------------------------------------------------------------------------


def test_maintain_rewrite_failure_records_metric_and_returns(system, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(system.store, "rewrite", boom)
    report = system.maintain()
    assert report.rewritten is False
    assert system.metrics.count("mem_write.fail") >= 1
    # 维护事件仍然落指标，且阶段 3/4/5 继续执行（不因重写失败中断）
    assert system.metrics.count("memory.maintain") == 1


def test_maintain_scan_failure_does_not_block_later_stages(system, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("state boom")

    monkeypatch.setattr(system, "state", boom)
    report = system.maintain()
    assert system.metrics.count("state.write_guard") >= 1  # 失败记 state.write_guard
    assert report.rewritten is True  # 后续阶段继续执行且成功


def test_maintain_pending_retry_failure_does_not_raise(system, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("pending boom")

    monkeypatch.setattr(system, "retry_pending", boom)
    report = system.maintain()
    assert report.rewritten is True
    assert system.metrics.count("mem_write.fail") >= 1
