"""记忆生命周期与系统门面：写入 / 去重 / 遗忘 / 复活 / 强化 / 维护（§4–§8）。"""

from __future__ import annotations

import json
import math
from datetime import timedelta

import pytest

from qihun_memory.config import Config
from qihun_memory.embedding import Embedder
from qihun_memory.lifecycle import compute_S
from qihun_memory.memory import MemorySystem
from qihun_memory.models import Inc, MemoryItem, Set, utc_now, utc_now_iso
from qihun_memory.retrieval import RetrievalResult
from qihun_memory.state import UserAffect


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
        # v1.6-rc4（E1）：新建条目 mention_count=0（去重命中 active 才 +1）
        mention_count=0,
        S=7.0,
        valence=0.0,
        status="active",
        tags=["出差"],
    )
    base.update(overrides)
    return MemoryItem(**base)


def write_state(system: MemorySystem, *, valence=0.0, avoidance=0.0, age_hours=0.0) -> None:
    timestamp = utc_now() - timedelta(hours=age_hours)
    system.cfg.bot_state_file.write_text(
        json.dumps(
            {
                "valence": valence,
                "avoidance": avoidance,
                "updated_at": timestamp.isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def extracted(content: str = "用户A下周三去上海出差", **overrides) -> dict:
    item = {"type": "episode", "content": content, "tags": ["出差"]}
    item.update(overrides)
    return item


@pytest.fixture()
def system(config) -> MemorySystem:
    return MemorySystem(config)


def test_remember_writes_memory_jsonl(system) -> None:
    report = system.remember(
        [extracted()],
        user_id="u_1",
        scope="group",
        valence=0.5,
        turn_key="u_1:msg-1",
    )
    assert len(report.created) == 1
    item = report.created[0]
    assert item.user_id == "u_1"
    assert item.scope == "group"
    assert item.type == "episode"
    assert item.S == pytest.approx(7.0 * (1 + 0.5 * 0.5))
    assert item.valence == pytest.approx(0.5)
    assert item.status == "active"
    assert item.turn_key == "u_1:msg-1"
    assert item.mention_count == 0  # E1：新建条目 mention_count=0
    lines = system.cfg.memory_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == item.id


def test_dedupe_same_fact_mention_count(system) -> None:
    first = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    created = first.created[0]
    assert created.mention_count == 0  # E1：新建条目为 0
    s_before = created.S
    report = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    assert len(report.created) == 0
    assert len(report.merged) == 1
    item = report.merged[0]
    assert item.mention_count == 1  # E4：新建 0，仅去重命中 active 时 +1
    assert item.S == pytest.approx(s_before)  # E4：命中 active 不触发 compute_S / 不强化
    assert item.access_count == 0  # 重复提及不是召回（§10.1）
    assert len(system.all_items()) == 1
    # E4：不写 JSONL 新行
    assert len(system.cfg.memory_file.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_invalid_items_dropped_individually(system) -> None:
    payload = [
        {"type": "bogus", "content": "坏类型"},
        {"type": "fact", "content": "   "},
        {"type": "fact", "content": 123},
        {"type": "fact", "content": "用户A喜欢本帮菜", "tags": "not-a-list"},
        extracted("用户B养了一只猫"),
    ]
    report = system.remember(payload, user_id="u_1", scope="group", valence=0.0)
    assert len(report.created) == 2
    assert report.skipped == 3
    merged_item = [item for item in system.all_items() if item.content == "用户A喜欢本帮菜"][0]
    assert merged_item.tags == []


def test_turn_key_idempotency(system) -> None:
    first = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0, turn_key="u_1:msg-1")
    assert len(first.created) == 1
    second = system.remember(
        [extracted("用户A下周三去上海出差（重复消费）")],
        user_id="u_1",
        scope="group",
        valence=0.0,
        turn_key="u_1:msg-1",
    )
    assert second.turn_duplicate is True
    assert len(system.all_items()) == 1


def test_scope_isolation(system) -> None:
    group_item = make_item(system, content="用户A下周三去上海出差")
    dm_item = make_item(
        system,
        id=system.store.next_id(),
        scope="dm",
        content="用户A私聊说下周三去上海出差",
    )
    other_dm = make_item(
        system,
        id=system.store.next_id(),
        user_id="u_2",
        scope="dm",
        content="用户C说上海好玩",
    )
    for item in (group_item, dm_item, other_dm):
        system.store.append(item)

    group_result = system.search("上海出差", "group", "u_1")
    assert all(entry.item.scope == "group" for entry in group_result.selected)

    dm_result = system.search("上海出差", "dm", "u_1")
    assert [entry.item.id for entry in dm_result.selected] == [dm_item.id]

    none_result = system.search("上海出差", "dm", "u_3")
    assert none_result.selected == []


def test_decay_and_dormant_status(system) -> None:
    old = (utc_now() - timedelta(days=40)).isoformat(timespec="seconds")
    item = make_item(system, last_access=old, S=7.0)
    system.store.append(item)
    system.search("上海出差", "group", "u_1")
    assert item.status == "dormant"
    assert system.metrics.count("memory.dormant") >= 1


def test_strong_match_revives_dormant(system) -> None:
    now = utc_now_iso()
    target = make_item(system, content="用户A下周三去上海出差", status="dormant", last_access=now, S=1.0)
    decoy = make_item(
        system,
        id=system.store.next_id(),
        content="用户B喜欢本帮菜",
        last_access=now,
        S=7.0,
    )
    for item in (target, decoy):
        system.store.append(item)

    result = system.search("上海出差", "group", "u_1")
    reinforced = system.reinforce(result)
    assert reinforced >= 1
    assert target.status == "active"
    assert target.S == pytest.approx(7.0)  # §6.1 重置为 S₀，当轮不再叠加 reinforce（E5）
    assert target.access_count == 0  # E5：revive 不 Inc(access_count)
    assert system.metrics.count("memory.revived") >= 1


def test_reinforcement_respects_cap(system) -> None:
    now = utc_now_iso()
    target = make_item(system, content="用户A下周三去上海出差", last_access=now, S=7.0)
    decoy = make_item(system, id=system.store.next_id(), content="用户B喜欢本帮菜", last_access=now)
    for item in (target, decoy):
        system.store.append(item)
    for _ in range(10):
        result = system.search("上海出差", "group", "u_1")
        system.reinforce(result)
    assert target.S == pytest.approx(90.0)
    assert target.S <= 90.0


def test_write_failure_pending_retry(system, monkeypatch) -> None:
    original_append = system.store.append
    calls = {"count": 0}

    def boom(item):
        calls["count"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(system.store, "append", boom)
    report = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    assert report.failed == 1
    assert report.pending == 1
    assert system.metrics.count("mem_write.fail") == 1
    assert system.all_items() == []

    monkeypatch.setattr(system.store, "append", original_append)
    outcome = system.retry_pending()
    assert outcome["written"] == 1
    assert outcome["dropped"] == 0
    assert len(system.all_items()) == 1


def test_maintenance_archives_dead_and_marks_dormant(system) -> None:
    dead = make_item(
        system,
        id=system.store.next_id(),
        content="用户A很久以前说过的事",
        last_access=(utc_now() - timedelta(days=200)).isoformat(timespec="seconds"),
        S=7.0,
    )
    drowsy = make_item(
        system,
        id=system.store.next_id(),
        content="用户A最近没提过的事",
        last_access=(utc_now() - timedelta(days=25)).isoformat(timespec="seconds"),
        S=7.0,
    )
    for item in (dead, drowsy):
        system.store.append(item)
    report = system.maintain()
    assert report.archived == 1
    assert report.rewritten is True
    remaining = [item.id for item in system.all_items()]
    assert remaining == [drowsy.id]
    assert len(system.all_items()) == 1
    assert drowsy.status == "dormant"
    assert system.cfg.archive_file.read_text(encoding="utf-8").strip() != ""


def test_state_is_readonly(system) -> None:
    write_state(system, valence=0.6, avoidance=0.2)
    before = system.cfg.bot_state_file.read_bytes()
    state = system.state()
    assert state.stale is False
    assert state.valence == pytest.approx(0.6)
    assert state.avoidance == pytest.approx(0.2)
    system.remember([extracted()], user_id="u_1", scope="group", valence=state.valence)
    system.search("上海出差", "group", "u_1")
    system.maintain()
    after = system.cfg.bot_state_file.read_bytes()
    assert before == after


def test_state_stale_and_missing(system) -> None:
    write_state(system, valence=0.9, age_hours=7)
    state = system.state()
    assert state.stale is True
    assert state.valence == 0.0
    system.cfg.bot_state_file.unlink()
    missing = system.state()
    assert missing.stale is True
    assert missing.reason == "missing"


def test_persona_read(system) -> None:
    persona = system.cfg.data_path / "persona.md"
    persona.write_text("我是器魂", encoding="utf-8")
    assert system.persona() == "我是器魂"


def test_users_refresh_and_nickname(system) -> None:
    assert system.refresh_users({"u_1": "用户A"}) == 1
    assert system.users.nickname("u_1") == "用户A"
    assert system.users.nickname("u_999") != ""
    assert system.refresh_users({"u_1": "用户A"}) == 0


def test_metrics_snapshot(system) -> None:
    system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    counters = system.metrics.counters()
    assert counters.get("memory.created") == 1
    snapshot = system.stats()
    assert snapshot["counts"]["total"] == 1
    assert "embedder" in snapshot

# ---------------------------------------------------------------------------
# v1.5-r2：状态单点 update_memory(Set/Inc)（附录 D1 / 用例 23~25）
# ---------------------------------------------------------------------------

def make_system(tmp_path, *, embedder=None, **overrides) -> MemorySystem:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    data.update(overrides)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    if embedder is not None:
        return MemorySystem(cfg, embedder=embedder)
    return MemorySystem(cfg)


class FakeEmbedder(Embedder):
    """确定性假编码器：按文本查表返回 L2 归一化向量。"""

    name = "fake"
    dim = 4

    def __init__(self, mapping: dict[str, list[float]] | None = None, *, available: bool = True) -> None:
        self.mapping = dict(mapping or {})
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    def encode(self, texts):
        vectors = []
        for index, text in enumerate(texts):
            vector = list(self.mapping.get(text) or [0.0, 0.0, 0.0, 0.0])
            if not any(vector):
                vector[index % 4] = 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def seed_item(system: MemorySystem, **overrides) -> MemoryItem:
    item = make_item(system, **overrides)
    system.store.append(item)
    return item


def test_update_memory_set_whitelist_and_clamp(system) -> None:
    item = seed_item(system, S=7.0)
    assert system.update_memory(item.id, Set("S", 12.5)) is True
    assert item.S == pytest.approx(12.5)
    assert system.update_memory(item.id, [Set("S", 100.0)]) is True
    assert item.S == pytest.approx(90.0)  # §11 规则 7：写入路径也 clamp 90
    assert system.update_memory(item.id, Set("status", "dormant")) is True
    assert item.status == "dormant"
    assert system.update_memory(item.id, Set("last_access", "2026-01-02T03:04:05+00:00")) is True
    assert item.last_access == "2026-01-02T03:04:05+00:00"
    assert system.metrics.count("state.write_guard") == 0


def test_update_memory_inc_reads_old_value(system) -> None:
    item = seed_item(system, access_count=3, mention_count=1)
    assert system.update_memory(item.id, [Inc("access_count"), Inc("mention_count", 2)]) is True
    assert item.access_count == 4  # Inc 内部读旧值（用例 24）
    assert item.mention_count == 3
    assert system.update_memory(item.id, Inc("access_count", 1)) is True
    assert item.access_count == 5


@pytest.mark.parametrize(
    "ops",
    [
        [Set("content", "改写正文")],
        [Set("valence", -1.0)],
        [Set("created_at", "2026-01-01T00:00:00+00:00")],
        [Set("affect", {"label": "joy", "intensity": 0.5})],
        [Set("unknown_field", 1)],
        [Inc("access_count", "1")],
        [Inc("S")],
        ["not-an-op"],
    ],
)
def test_update_memory_rejects_non_whitelist(system, ops) -> None:
    item = seed_item(system)
    before = item.to_dict()
    assert system.update_memory(item.id, ops) is False
    assert item.to_dict() == before  # 整体不生效（用例 25 / A2）
    assert system.metrics.count("state.write_guard") == 1


def test_update_memory_invalid_values_are_atomic(system) -> None:
    item = seed_item(system)
    assert system.update_memory(item.id, [Set("status", "dormant"), Set("S", "NaN")]) is False
    assert item.status == "active"  # 前一项也不能生效
    assert system.metrics.count("state.write_guard") == 1
    assert system.update_memory("m_missing", Set("S", 1.0)) is False
    assert system.metrics.count("state.write_guard") == 2


# ---------------------------------------------------------------------------
# v1.5-r2：remember 的 affect / compute_S("init") / dedup_key / pending
# ---------------------------------------------------------------------------

def test_remember_persists_affect_and_modulates_s(tmp_path) -> None:
    system = make_system(tmp_path)
    report = system.remember(
        [
            {
                "type": "fact",
                "content": "用户A对方案有点生气",
                "affect": {"label": "anger", "intensity": 0.8},
            }
        ],
        user_id="u_1",
        scope="group",
        valence=0.9,
    )
    item = report.created[0]
    assert item.affect == {"label": "anger", "intensity": 0.8}
    expected = compute_S(
        "init",
        "fact",
        valence=0.9,
        affect={"label": "anger", "intensity": 0.8},
    )
    assert item.S == pytest.approx(expected)
    assert item.S == pytest.approx(53.94)  # 30*(1+0.5*0.9)*(1+0.3*0.8)
    line = system.cfg.memory_file.read_text(encoding="utf-8").strip()
    assert json.loads(line)["affect"] == {"label": "anger", "intensity": 0.8}


def test_remember_invalid_affect_drops_field_not_item(tmp_path) -> None:
    system = make_system(tmp_path)
    report = system.remember(
        [{"type": "episode", "content": "用户A去上海", "affect": {"label": "bogus", "intensity": 1}}],
        user_id="u_1",
        scope="group",
        valence=0.0,
    )
    assert len(report.created) == 1
    assert report.created[0].affect is None
    assert report.created[0].S == pytest.approx(7.0)  # k_a 不生效


def test_dedup_key_exact_match_merges(system) -> None:
    first = system.remember([extracted("用户A下周三去上海出差！")], user_id="u_1", scope="group", valence=0.0)
    assert len(first.created) == 1
    report = system.remember([extracted("用户A下周三去上海出差!")], user_id="u_1", scope="group", valence=0.0)
    assert len(report.created) == 0
    assert len(report.merged) == 1
    assert report.merged[0].mention_count == 1  # E1/E4：新建 0，去重命中 active +1
    assert report.merged[0].access_count == 0  # 重复提及不是召回
    assert len(system.all_items()) == 1
    assert system.metrics.count("memory.deduped") == 1


def test_dedup_cosine_merges_when_key_differs(tmp_path) -> None:
    embedder = FakeEmbedder(
        {
            "用户A下周三去上海出差": [1.0, 0.0, 0.0, 0.0],
            "用户A下周二去上海出差": [1.0, 0.0, 0.0, 0.0],
        }
    )
    system = make_system(tmp_path, embedder=embedder)
    first = system.remember([extracted("用户A下周三去上海出差")], user_id="u_1", scope="group", valence=0.0)
    assert len(first.created) == 1
    report = system.remember([extracted("用户A下周二去上海出差")], user_id="u_1", scope="group", valence=0.0)
    assert len(report.merged) == 1
    assert report.merged[0].mention_count == 1  # E1/E4：新建 0，去重命中 active +1
    assert system.metrics.count("dedup.degraded") == 0


def test_dedup_degraded_metric_when_embedding_unavailable(system) -> None:
    system.remember([extracted("用户A下周三去上海出差")], user_id="u_1", scope="group", valence=0.0)
    report = system.remember([extracted("用户A喜欢吃本帮菜")], user_id="u_1", scope="group", valence=0.0)
    assert len(report.created) == 1
    assert system.metrics.count("dedup.degraded") >= 1


def test_pending_replay_skips_existing_turn_key(system, monkeypatch) -> None:
    original_append = system.store.append

    def boom(item):
        raise OSError("disk full")

    monkeypatch.setattr(system.store, "append", boom)
    report = system.remember(
        [extracted()], user_id="u_1", scope="group", valence=0.0, turn_key="u_1:msg-9"
    )
    assert report.failed == 1
    assert system.pending_count == 1
    assert system.store.items == {}

    monkeypatch.setattr(system.store, "append", original_append)
    system.store.register_turn_key("u_1:msg-9", "m_9999")  # 模拟另一路径已落盘
    outcome = system.retry_pending()
    assert outcome["written"] == 0
    assert outcome["skipped"] == 1
    assert system.all_items() == []


def test_reinforce_never_enters_pending(system) -> None:
    seed_item(system, content="用户A下周三去上海出差")
    result = system.search("上海出差", "group", "u_1")
    system.reinforce(result)
    system.affect_store.step_update("u_1", {"familiarity": 0.1})
    assert system.pending_count == 0  # 强化 / 感情更新永不进 pending（§4.4）


# ---------------------------------------------------------------------------
# v1.5-r2：reinforce / revive / status 写全部经 update_memory（附录 D1）
# ---------------------------------------------------------------------------

def test_reinforce_uses_update_memory_ops(system, monkeypatch) -> None:
    target = seed_item(system, content="用户A下周三去上海出差")
    calls = []
    original = system.update_memory

    def spy(item_id, ops, **kwargs):
        calls.append((item_id, list(ops) if isinstance(ops, (list, tuple)) else ops))
        return original(item_id, ops, **kwargs)

    monkeypatch.setattr(system, "update_memory", spy)
    result = system.search("上海出差", "group", "u_1")
    system.reinforce(result)
    assert target.access_count == 1
    assert target.S > 7.0
    ops = [op for _item_id, batch in calls for op in (batch if isinstance(batch, list) else [batch])]
    assert any(isinstance(op, Set) and op.field == "S" for op in ops)
    assert any(isinstance(op, Inc) and op.field == "access_count" for op in ops)


def test_revive_uses_compute_S_revive(system, monkeypatch) -> None:
    now = utc_now_iso()
    target = make_item(
        system,
        content="用户A下周三去上海出差",
        status="dormant",
        last_access=now,
        S=1.0,
    )
    system.store.append(target)
    calls = []
    original = system.update_memory

    def spy(item_id, ops, **kwargs):
        calls.append((item_id, ops))
        return original(item_id, ops, **kwargs)

    monkeypatch.setattr(system, "update_memory", spy)
    result = system.search("上海出差", "group", "u_1")
    assert system.reinforce(result) == 1
    assert target.status == "active"
    assert target.S == pytest.approx(7.0)
    batch = calls[-1][1]
    assert Set("S", 7.0) in batch
    assert Set("status", "active") in batch
    # E5：复活落库动作只有 S/status/last_access，不 Inc(access_count)、不叠 reinforce
    assert not any(isinstance(op, Inc) and op.field == "access_count" for op in batch)
    assert target.access_count == 0


def test_refresh_statuses_write_goes_through_update_memory(system, monkeypatch) -> None:
    old = (utc_now() - timedelta(days=40)).isoformat(timespec="seconds")
    item = seed_item(system, last_access=old, S=7.0)
    calls = []
    original = system.update_memory

    def spy(item_id, ops, **kwargs):
        calls.append(ops)
        return original(item_id, ops, **kwargs)

    monkeypatch.setattr(system, "update_memory", spy)
    system.search("上海出差", "group", "u_1")
    assert item.status == "dormant"
    assert any(
        isinstance(ops, Set) and ops.field == "status" and ops.value == "dormant" for ops in calls
    )


def test_maintain_status_write_goes_through_update_memory(system, monkeypatch) -> None:
    drowsy = seed_item(
        system,
        last_access=(utc_now() - timedelta(days=25)).isoformat(timespec="seconds"),
        S=7.0,
    )
    calls = []
    original = system.update_memory

    def spy(item_id, ops, **kwargs):
        calls.append(ops)
        return original(item_id, ops, **kwargs)

    monkeypatch.setattr(system, "update_memory", spy)
    report = system.maintain()
    assert report.dormant == 1
    assert drowsy.status == "dormant"
    assert any(
        isinstance(ops, Set) and ops.field == "status" and ops.value == "dormant" for ops in calls
    )


# ---------------------------------------------------------------------------
# v1.5-r2：UserAffectStore 接线（search 前 decay + 传 Retriever）
# ---------------------------------------------------------------------------

def test_search_decays_user_affect_before_retrieval(tmp_path) -> None:
    now = utc_now()
    system = make_system(tmp_path)
    old = (now - timedelta(days=7)).isoformat(timespec="seconds")
    system.cfg.user_affect_file.write_text(
        json.dumps(
            {
                "u_1": {
                    "affection": 0.5,
                    "annoyance": 0.81,
                    "familiarity": 0.3,
                    "updated_at": old,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    system = make_system(tmp_path)  # 重新加载 user_affect.json
    system.search("上海出差", "group", "u_1", now=now)
    affect = system.affect_store.get("u_1", now)
    assert affect.annoyance == pytest.approx(0.729)  # 0.81 * 0.9^floor(7/7)


def test_search_passes_user_affect_and_last_event(tmp_path) -> None:
    system = make_system(tmp_path)
    captured: dict = {}

    class RecordingRetriever:
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
        ):
            captured.update(
                {
                    "user_affect": user_affect,
                    "last_event_affect": last_event_affect,
                    "scope": scope,
                    "user_id": user_id,
                }
            )
            return RetrievalResult(
                selected=[], candidates=0, tokens=0, budget=1200, degraded=False
            )

    system.retriever = RecordingRetriever()
    system.search("查询", "group", "u_1", last_event_affect={"label": "anger", "intensity": 0.8})
    assert isinstance(captured["user_affect"], UserAffect)
    assert captured["last_event_affect"] == {"label": "anger", "intensity": 0.8}
    assert captured["scope"] == "group"
    assert captured["user_id"] == "u_1"


def test_user_affect_helper_and_stats(system) -> None:
    assert system.user_affect("u_1").affection == pytest.approx(0.5)
    assert system.stats()["user_affect_users"] == 0
    system.affect_store.step_update("u_1", {"familiarity": 0.1})
    assert system.stats()["user_affect_users"] == 1
