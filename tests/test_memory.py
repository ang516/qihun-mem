"""记忆生命周期与系统门面：写入 / 去重 / 遗忘 / 复活 / 强化 / 维护（§4–§8）。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from qihun_memory.memory import MemorySystem
from qihun_memory.models import MemoryItem, utc_now, utc_now_iso


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
    lines = system.cfg.memory_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == item.id


def test_dedupe_same_fact_mention_count(system) -> None:
    system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    report = system.remember([extracted()], user_id="u_1", scope="group", valence=0.0)
    assert len(report.created) == 0
    assert len(report.merged) == 1
    item = report.merged[0]
    assert item.mention_count == 2
    assert item.access_count == 0  # 重复提及不是召回（§10.1）
    assert len(system.all_items()) == 1


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
    assert target.S == pytest.approx(7.0)  # §5.1 重置为 S₀，当轮不再叠加 §5.3
    assert target.access_count == 1
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
