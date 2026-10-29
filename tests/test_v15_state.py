"""v1.5-r2 L2 状态单点回归矩阵（契约 §2.3 / §2.4 / §2.13）。

覆盖：
- `compute_S`：init / revive / reinforce 三支、k_v / k_a / cap、非法 mode ValueError；
  旧函数 `initial_stability` / `revive_stability` / `reinforce_stability` 只是薄封装，
  必须实际调用 `compute_S`（禁止第二份公式）；
- `UserAffectStore`：get / step_update / decay / load+save 原子；
  未知字段（如 trust）忽略、坏文件 fail 指标、stale >= 30 天归零；
- `MemorySystem.update_memory` 白名单：T2 落地前 skip，Lead 通知后移除 skip 并生效。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from qihun_memory import lifecycle
from qihun_memory.config import AffectConfig, DecayConfig
from qihun_memory.lifecycle import (
    compute_S,
    initial_stability,
    reinforce_stability,
    revive_stability,
)
from qihun_memory.memory import MemorySystem
from qihun_memory.metrics import Metrics
from qihun_memory.models import Inc, Set
from qihun_memory.state import UserAffectStore

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _store(tmp_path, payload=None, *, cfg=None, metrics=None) -> UserAffectStore:
    path = tmp_path / "user_affect.json"
    if payload is not None:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return UserAffectStore(path, cfg=cfg, metrics=metrics)


def _record(annoyance: float = 0.8, updated_at: datetime = NOW) -> dict:
    return {
        "affection": 0.7,
        "annoyance": annoyance,
        "familiarity": 0.6,
        "updated_at": _iso(updated_at),
    }


# ==========================================================================
# compute_S：唯一 S 计算入口（附录 D1）
# ==========================================================================

def test_compute_S_init_baseline() -> None:
    cfg = DecayConfig()
    assert compute_S("init", "episode", cfg=cfg) == pytest.approx(7.0)
    assert compute_S("init", "fact", cfg=cfg) == pytest.approx(30.0)
    assert compute_S("init", "preference", cfg=cfg) == pytest.approx(30.0)


def test_compute_S_init_valence_modulation() -> None:
    cfg = DecayConfig()
    assert compute_S("init", "episode", valence=1.0, cfg=cfg) == pytest.approx(10.5)
    assert compute_S("init", "fact", valence=0.9, cfg=cfg) == pytest.approx(43.5)
    # |valence|：负性同样放大 S
    assert compute_S("init", "fact", valence=-1.0, cfg=cfg) == pytest.approx(45.0)


def test_compute_S_init_affect_modulation() -> None:
    cfg = DecayConfig()
    assert compute_S(
        "init", "episode", affect={"label": "joy", "intensity": 0.5}, cfg=cfg
    ) == pytest.approx(7.0 * 1.15)
    assert compute_S(
        "init", "episode", affect={"label": "anger", "intensity": 0.2}, cfg=cfg
    ) == pytest.approx(7.0 * 1.06)
    # neutral 不参与情绪调制
    assert compute_S(
        "init", "episode", affect={"label": "neutral", "intensity": 1.0}, cfg=cfg
    ) == pytest.approx(7.0)
    assert compute_S("init", "episode", affect=None, cfg=cfg) == pytest.approx(7.0)


def test_compute_S_init_valence_and_affect_multiply() -> None:
    cfg = DecayConfig()
    value = compute_S(
        "init", "fact", valence=1.0, affect={"label": "affection", "intensity": 1.0}, cfg=cfg
    )
    assert value == pytest.approx(30.0 * 1.5 * 1.3)


def test_compute_S_init_cap() -> None:
    cfg = DecayConfig()
    assert compute_S("init", "fact", valence=5.0, cfg=cfg) == pytest.approx(90.0)
    custom = DecayConfig(s_max=50.0)
    assert compute_S("init", "fact", valence=5.0, cfg=custom) == pytest.approx(50.0)


def test_compute_S_kv_and_ka_are_configurable() -> None:
    flat = DecayConfig(kv=0.0, ka=0.0)
    assert compute_S(
        "init",
        "episode",
        valence=1.0,
        affect={"label": "joy", "intensity": 1.0},
        cfg=flat,
    ) == pytest.approx(7.0)
    kv_double = DecayConfig(kv=1.0)
    assert compute_S("init", "episode", valence=1.0, cfg=kv_double) == pytest.approx(14.0)
    ka_double = DecayConfig(ka=1.0)
    assert compute_S(
        "init", "episode", affect={"label": "joy", "intensity": 1.0}, cfg=ka_double
    ) == pytest.approx(14.0)


def test_compute_S_revive_is_bare_s0() -> None:
    cfg = DecayConfig()
    assert compute_S("revive", "episode", cfg=cfg) == pytest.approx(7.0)
    assert compute_S("revive", "fact", cfg=cfg) == pytest.approx(30.0)
    assert compute_S("revive", "preference", cfg=cfg) == pytest.approx(30.0)
    # 复活不带情绪调制（裸 S0）
    assert compute_S(
        "revive",
        "episode",
        valence=1.0,
        affect={"label": "joy", "intensity": 1.0},
        cfg=cfg,
    ) == pytest.approx(7.0)


def test_compute_S_reinforce_formula_and_cap() -> None:
    cfg = DecayConfig()
    assert compute_S("reinforce", "episode", S=7.0, R=0.0, cfg=cfg) == pytest.approx(14.0)
    assert compute_S("reinforce", "episode", S=7.0, R=1.0, cfg=cfg) == pytest.approx(21.0)
    assert compute_S("reinforce", "episode", S=45.0, R=1.0, cfg=cfg) == pytest.approx(90.0)
    assert compute_S("reinforce", "episode", S=90.0, R=1.0, cfg=cfg) == pytest.approx(90.0)
    # 负 R 按 0 处理，不许缩水
    assert compute_S("reinforce", "episode", S=10.0, R=-0.5, cfg=cfg) == pytest.approx(20.0)


def test_compute_S_spec_formula_self_check() -> None:
    """规范 §10.4 公式自检（并入 L1/L2 最小 CI）。"""
    assert compute_S("init", "episode", valence=0.0) == pytest.approx(7.0)
    assert compute_S("init", "fact", valence=0.9) == pytest.approx(43.5)
    assert compute_S(
        "init", "fact", valence=0.9, affect={"label": "anger", "intensity": 0.8}
    ) == pytest.approx(53.94)
    assert compute_S(
        "init", "episode", valence=0.0, affect={"label": "neutral", "intensity": 1.0}
    ) == pytest.approx(7.0)
    assert compute_S("revive", "episode") == pytest.approx(7.0)
    assert compute_S("revive", "fact") == pytest.approx(30.0)
    # 规范 §10.4 自检行写的是 10.5（等于 S·(1+R)，漏了 +S），与 §4.7/§5.3
    # 及工程契约 §2.3 的 min(S + S·(1+R), 90) 冲突；以正文公式为准 → 17.5。
    assert compute_S("reinforce", "episode", S=7, R=0.5) == pytest.approx(17.5)
    assert compute_S("reinforce", "episode", S=80, R=0.5) == pytest.approx(90.0)


def test_compute_S_old_data_without_affect_has_no_ka() -> None:
    """规范 §10.3-20：老数据 affect=None 时 compute_S 不乘 (1+k_a·I)。"""
    assert compute_S("init", "fact", valence=0.0, affect=None) == pytest.approx(30.0)
    assert compute_S(
        "init", "fact", valence=0.0, affect={"label": None}
    ) == pytest.approx(30.0)


def test_compute_S_unknown_mode_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_S("bogus", "episode")
    with pytest.raises(ValueError):
        compute_S("INIT", "episode")  # 大小写敏感，不许静默降级
    with pytest.raises(ValueError):
        compute_S("reinforce", "episode")  # 缺 S 也拒绝


def test_legacy_wrappers_match_compute_S() -> None:
    cfg = DecayConfig()
    assert initial_stability("fact", 0.9, cfg) == compute_S("init", "fact", valence=0.9, cfg=cfg)
    assert initial_stability(
        "episode", 0.0, cfg, affect={"label": "joy", "intensity": 0.5}
    ) == compute_S("init", "episode", affect={"label": "joy", "intensity": 0.5}, cfg=cfg)
    assert revive_stability("episode", cfg) == compute_S("revive", "episode", cfg=cfg)
    assert reinforce_stability(7.0, 1.0, cfg) == compute_S(
        "reinforce", "episode", S=7.0, R=1.0, cfg=cfg
    )


def test_legacy_wrappers_delegate_to_compute_S(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake(mode, item_type, **kwargs):
        calls.append((mode, item_type, kwargs))
        return 123.0

    monkeypatch.setattr(lifecycle, "compute_S", fake)
    cfg = DecayConfig()
    assert initial_stability("episode", 0.4, cfg, affect={"label": "joy", "intensity": 0.5}) == 123.0
    assert revive_stability("fact", cfg) == 123.0
    assert reinforce_stability(7.0, 0.8, cfg) == 123.0
    assert [call[0] for call in calls] == ["init", "revive", "reinforce"]


# ==========================================================================
# UserAffectStore：唯一读写入口（附录 D1）
# ==========================================================================

def test_get_unknown_user_returns_config_neutral(tmp_path) -> None:
    affect = _store(tmp_path).get("u_missing", NOW)
    assert affect.affection == pytest.approx(0.5)
    assert affect.annoyance == pytest.approx(0.0)
    assert affect.familiarity == pytest.approx(0.3)
    assert affect.stale is False
    assert affect.updated_at == ""


def test_get_fresh_record(tmp_path) -> None:
    store = _store(tmp_path, {"u1": _record(updated_at=NOW)})
    affect = store.get("u1", NOW)
    assert (affect.affection, affect.annoyance, affect.familiarity) == pytest.approx((0.7, 0.8, 0.6))
    assert affect.stale is False
    assert affect.updated_at == _iso(NOW)


def test_get_stale_30_days_returns_neutral_and_zero_familiarity(tmp_path) -> None:
    store = _store(tmp_path, {"u1": _record(updated_at=NOW - timedelta(days=30))})
    affect = store.get("u1", NOW)
    assert affect.stale is True
    assert affect.affection == pytest.approx(0.5)
    assert affect.annoyance == pytest.approx(0.0)
    assert affect.familiarity == pytest.approx(0.0)


def test_get_just_below_stale_threshold_kept(tmp_path) -> None:
    store = _store(tmp_path, {"u1": _record(updated_at=NOW - timedelta(days=29, hours=23))})
    affect = store.get("u1", NOW)
    assert affect.stale is False
    assert affect.familiarity == pytest.approx(0.6)


def test_get_missing_or_invalid_updated_at_is_stale(tmp_path) -> None:
    payload = {
        "u1": {"affection": 0.7, "annoyance": 0.2, "familiarity": 0.6},
        "u2": {"affection": 0.7, "annoyance": 0.2, "familiarity": 0.6, "updated_at": "not-a-time"},
    }
    store = _store(tmp_path, payload)
    assert store.get("u1", NOW).stale is True
    assert store.get("u2", NOW).stale is True


def test_get_stale_does_not_write_or_record_stale_metric(tmp_path) -> None:
    metrics = Metrics()
    store = _store(
        tmp_path, {"u1": _record(updated_at=NOW - timedelta(days=31))}, metrics=metrics
    )
    path = tmp_path / "user_affect.json"
    before = path.read_bytes()
    assert store.get("u1", NOW).stale is True
    assert path.read_bytes() == before
    assert metrics.count("user_affect.stale") == 0


def test_load_ignores_unknown_fields(tmp_path) -> None:
    record = _record(updated_at=NOW)
    record["trust"] = 0.9
    store = _store(tmp_path, {"u1": record})
    assert store.get("u1", NOW).affection == pytest.approx(0.7)
    assert "trust" not in store.snapshot()["u1"]


def test_load_bad_file_is_neutral_with_fail_metric(tmp_path) -> None:
    path = tmp_path / "user_affect.json"
    path.write_text("not json", encoding="utf-8")
    metrics = Metrics()
    store = UserAffectStore(path, metrics=metrics)
    affect = store.get("u1", NOW)
    assert affect.stale is False
    assert affect.familiarity == pytest.approx(0.3)
    assert metrics.count("user_affect.fail") == 1


def test_load_non_object_root_records_fail(tmp_path) -> None:
    metrics = Metrics()
    store = _store(tmp_path, ["not", "object"], metrics=metrics)
    assert store.get("u1", NOW).familiarity == pytest.approx(0.3)
    assert metrics.count("user_affect.fail") == 1


def test_load_bad_records_dropped_and_metric(tmp_path) -> None:
    payload = {
        "u1": "not-a-record",
        "u2": {"affection": "x", "annoyance": 0.1, "familiarity": 0.3, "updated_at": _iso(NOW)},
        "u3": _record(updated_at=NOW),
    }
    metrics = Metrics()
    store = _store(tmp_path, payload, metrics=metrics)
    assert metrics.count("user_affect.fail") == 2
    assert store.get("u1", NOW).affection == pytest.approx(0.5)
    assert store.get("u3", NOW).annoyance == pytest.approx(0.8)


def test_load_clamps_out_of_range_values(tmp_path) -> None:
    payload = {
        "u1": {
            "affection": 1.7,
            "annoyance": -0.5,
            "familiarity": 0.4,
            "updated_at": _iso(NOW),
        }
    }
    store = _store(tmp_path, payload)
    affect = store.get("u1", NOW)
    assert affect.affection == pytest.approx(1.0)
    assert affect.annoyance == pytest.approx(0.0)
    assert affect.familiarity == pytest.approx(0.4)


def test_load_missing_file_has_no_metric(tmp_path) -> None:
    metrics = Metrics()
    store = _store(tmp_path, metrics=metrics)
    assert store.get("u1", NOW).familiarity == pytest.approx(0.3)
    assert metrics.count("user_affect.fail") == 0


def test_step_update_formula_and_updated_at(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.step_update("u1", {"familiarity": 0.1}, now=NOW)
    assert first.familiarity == pytest.approx(0.37)  # 0.3 + 0.1 * (1 - 0.3)
    second = store.step_update(
        "u1", {"affection": 0.1, "annoyance": 0.1}, now=NOW + timedelta(minutes=1)
    )
    assert second.affection == pytest.approx(0.55)  # 0.5 + 0.1 * 0.5
    assert second.annoyance == pytest.approx(0.1)  # 0.0 + 0.1 * 1.0
    assert second.familiarity == pytest.approx(0.37)  # 未传的字段不变
    assert second.updated_at == _iso(NOW + timedelta(minutes=1))


def test_step_update_clamps_unit_interval(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.step_update("high", {"familiarity": 10}, now=NOW).familiarity == pytest.approx(1.0)
    assert store.step_update("low", {"familiarity": -10}, now=NOW).familiarity == pytest.approx(0.0)


def test_step_update_ignores_non_numeric_and_unknown_deltas(tmp_path) -> None:
    store = _store(tmp_path, {"u1": _record(annoyance=0.2, updated_at=NOW)})
    before = store.get("u1", NOW)
    updated = store.step_update("u1", {"affection": "abc", "bogus": 0.5}, now=NOW)
    assert updated.affection == pytest.approx(before.affection)
    assert updated.annoyance == pytest.approx(before.annoyance)
    assert updated.updated_at == _iso(NOW)


def test_step_update_missing_user_starts_from_initial(tmp_path) -> None:
    store = _store(tmp_path)
    updated = store.step_update("new_user", {"affection": 0.1, "annoyance": 0.1}, now=NOW)
    assert updated.affection == pytest.approx(0.55)
    assert updated.annoyance == pytest.approx(0.1)
    assert updated.familiarity == pytest.approx(0.3)


def test_step_update_stale_record_resets_before_update(tmp_path) -> None:
    store = _store(
        tmp_path,
        {
            "u1": {
                "affection": 0.9,
                "annoyance": 0.5,
                "familiarity": 0.9,
                "updated_at": _iso(NOW - timedelta(days=40)),
            }
        },
    )
    updated = store.step_update("u1", {"familiarity": 0.1}, now=NOW)
    assert updated.familiarity == pytest.approx(0.1)  # 0 + 0.1 * 1
    assert updated.affection == pytest.approx(0.5)  # 初始值，不继承 stale 旧值
    assert updated.annoyance == pytest.approx(0.0)


def test_step_update_persists_atomically(tmp_path) -> None:
    store = _store(tmp_path)
    store.step_update("u1", {"familiarity": 0.1}, now=NOW)
    path = tmp_path / "user_affect.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["u1"]["familiarity"] == pytest.approx(0.37)
    assert list(tmp_path.glob("*.tmp-*")) == []
    assert UserAffectStore(path).get("u1", NOW).familiarity == pytest.approx(0.37)


def test_save_failure_records_metric_without_raising(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    metrics = Metrics()
    store = UserAffectStore(blocker / "user_affect.json", metrics=metrics)
    updated = store.step_update("u1", {"familiarity": 0.1}, now=NOW)
    assert updated.familiarity == pytest.approx(0.37)  # 内存保持权威
    assert metrics.count("user_affect.fail") >= 1


@pytest.mark.parametrize(
    "days,expected",
    [(6.9, 0.8), (7.0, 0.72), (14.0, 0.648), (21.0, 0.5832)],
)
def test_decay_annoyance_per_week(tmp_path, days: float, expected: float) -> None:
    store = _store(tmp_path, {"u1": _record(updated_at=NOW)})
    assert store.decay("u1", days=days, now=NOW).annoyance == pytest.approx(expected)


def test_decay_days_none_derives_from_updated_at(tmp_path) -> None:
    store = _store(tmp_path, {"u1": _record(updated_at=NOW - timedelta(days=14))})
    assert store.decay("u1", now=NOW).annoyance == pytest.approx(0.648)


def test_decay_zero_or_negative_days_is_noop(tmp_path) -> None:
    store = _store(tmp_path, {"u1": _record(updated_at=NOW)})
    assert store.decay("u1", days=0, now=NOW).annoyance == pytest.approx(0.8)
    assert store.decay("u1", days=-5, now=NOW).annoyance == pytest.approx(0.8)


def test_decay_missing_user_returns_neutral_and_does_not_write(tmp_path) -> None:
    store = _store(tmp_path)
    affect = store.decay("nope", now=NOW)
    assert affect.familiarity == pytest.approx(0.3)
    assert not (tmp_path / "user_affect.json").exists()


def test_decay_stale_resets_all_and_records_metric(tmp_path) -> None:
    metrics = Metrics()
    store = _store(
        tmp_path, {"u1": _record(updated_at=NOW - timedelta(days=31))}, metrics=metrics
    )
    affect = store.decay("u1", now=NOW)
    assert affect.stale is False  # 归零后按当前时间落盘
    assert affect.affection == pytest.approx(0.5)
    assert affect.annoyance == pytest.approx(0.0)
    assert affect.familiarity == pytest.approx(0.0)
    assert metrics.count("user_affect.stale") == 1
    persisted = json.loads((tmp_path / "user_affect.json").read_text(encoding="utf-8"))
    assert persisted["u1"]["familiarity"] == pytest.approx(0.0)
    assert persisted["u1"]["affection"] == pytest.approx(0.5)


def test_neutral_uses_config_initial_values(tmp_path) -> None:
    cfg = AffectConfig(initial_affection=0.2, initial_annoyance=0.05, initial_familiarity=0.4)
    affect = _store(tmp_path, cfg=cfg).get("u1", NOW)
    assert (affect.affection, affect.annoyance, affect.familiarity) == pytest.approx((0.2, 0.05, 0.4))


def test_stale_threshold_uses_config(tmp_path) -> None:
    cfg = AffectConfig(stale_days=1.0)
    store = _store(tmp_path, {"u1": _record(updated_at=NOW - timedelta(days=2))}, cfg=cfg)
    assert store.get("u1", NOW).stale is True


def test_decay_custom_base_and_period(tmp_path) -> None:
    cfg = AffectConfig(annoyance_decay_base=0.5, annoyance_decay_period_days=2.0)
    store = _store(tmp_path, {"u1": _record(updated_at=NOW)}, cfg=cfg)
    assert store.decay("u1", days=4, now=NOW).annoyance == pytest.approx(0.8 * 0.25)


def test_snapshot_is_a_copy(tmp_path) -> None:
    store = _store(tmp_path, {"u1": _record(updated_at=NOW)})
    snapshot = store.snapshot()
    snapshot["u1"]["affection"] = 0.1
    assert store.get("u1", NOW).affection == pytest.approx(0.7)


# ==========================================================================
# MemorySystem.update_memory 白名单（规范 §10.5-23~25 / A2 / A3）
# ==========================================================================

def _system_with_item(config) -> tuple[MemorySystem, object]:
    system = MemorySystem(config)
    report = system.remember(
        [{"type": "fact", "content": "白名单测试条目"}],
        user_id="u1",
        scope="dm",
        valence=0.0,
    )
    assert report.created, "前置条件：remember() 应创建一条记忆"
    return system, report.created[0]


def test_memory_system_exposes_update_memory_and_affect_store(config) -> None:
    """契约 §2.9：MemorySystem 暴露 affect_store；update_memory 是状态写入口。"""
    system = MemorySystem(config)
    try:
        assert hasattr(system, "update_memory")
        assert system.affect_store.path == config.user_affect_file
    finally:
        system.close()


def test_update_memory_set_whitelist(config) -> None:
    system, item = _system_with_item(config)
    try:
        assert system.update_memory(item.id, [Set("S", 42.0)], reason="t4-set") is True
        assert system.get_item(item.id).S == pytest.approx(42.0)
        assert system.update_memory(item.id, [Set("status", "dormant")], reason="t4-set") is True
        assert system.get_item(item.id).status == "dormant"
        assert (
            system.update_memory(
                item.id, [Set("last_access", "2026-10-29T00:00:00+00:00")], reason="t4-set"
            )
            is True
        )
        assert system.get_item(item.id).last_access.startswith("2026-10-29")
    finally:
        system.close()


def test_update_memory_accepts_single_operation(config) -> None:
    """契约 §2.9：ops 支持单条 Set/Inc。"""
    system, item = _system_with_item(config)
    try:
        assert system.update_memory(item.id, Set("S", 12.5), reason="t4-single") is True
        assert system.get_item(item.id).S == pytest.approx(12.5)
    finally:
        system.close()


def test_update_memory_s_clamped_to_90(config) -> None:
    """规范 §10.5-23：Set(S=100) clamp 到 90。"""
    system, item = _system_with_item(config)
    try:
        assert system.update_memory(item.id, [Set("S", 100.0)], reason="cap") is True
        assert system.get_item(item.id).S == pytest.approx(90.0)
        assert system.update_memory(item.id, [Set("S", 500.0)], reason="cap") is True
        assert system.get_item(item.id).S == pytest.approx(90.0)
        assert system.update_memory(item.id, [Set("S", -5.0)], reason="floor") is True
        assert system.get_item(item.id).S == pytest.approx(0.0)
    finally:
        system.close()


def test_update_memory_inc_whitelist(config) -> None:
    """规范 §10.5-24：Inc 内部读旧值，两次自增不丢更新。"""
    system, item = _system_with_item(config)
    try:
        before_access = item.access_count
        assert system.update_memory(item.id, [Inc("access_count", 1)], reason="t4-inc") is True
        assert system.update_memory(item.id, [Inc("access_count", 1)], reason="t4-inc") is True
        assert system.get_item(item.id).access_count == before_access + 2
        before_mention = item.mention_count
        assert system.update_memory(item.id, [Inc("mention_count")], reason="t4-inc") is True
        assert system.get_item(item.id).mention_count == before_mention + 1
    finally:
        system.close()


def test_update_memory_same_batch_inc_reads_prior_result(config) -> None:
    """A3：同一批 ops 内 Inc 也要能读到前一个 Inc 的结果。"""
    system, item = _system_with_item(config)
    try:
        before = item.access_count
        assert (
            system.update_memory(
                item.id, [Inc("access_count", 1), Inc("access_count", 1)], reason="t4-batch"
            )
            is True
        )
        assert system.get_item(item.id).access_count == before + 2
    finally:
        system.close()


def test_update_memory_rejects_illegal_ops_atomically(config) -> None:
    """规范 §10.5-25 / A2：任一非法操作 → 整批不生效 + state.write_guard。"""
    system, item = _system_with_item(config)
    try:
        system.metrics.reset()
        original_s = item.S
        assert system.update_memory(item.id, [Set("S", 50.0), Set("content", "篡改")], reason="bad") is False
        assert system.get_item(item.id).S == pytest.approx(original_s)
        assert system.get_item(item.id).content == "白名单测试条目"
        assert system.metrics.count("state.write_guard") == 1
        guard_before = system.metrics.count("state.write_guard")
        assert system.update_memory(item.id, [Set("type", "episode")], reason="bad") is False
        assert system.update_memory(item.id, [Inc("S", 1)], reason="bad") is False
        assert system.update_memory(item.id, [Set("status", "bogus")], reason="bad") is False
        assert system.update_memory("m_missing", [Set("S", 10.0)], reason="bad") is False
        assert system.metrics.count("state.write_guard") == guard_before + 4
        # 非法尝试后白名单写入仍然正常
        assert system.update_memory(item.id, [Set("S", 33.0)], reason="ok") is True
        assert system.get_item(item.id).S == pytest.approx(33.0)
    finally:
        system.close()


@pytest.mark.parametrize(
    "ops",
    [
        [Set("created_at", "2026-10-29T00:00:00+00:00")],
        [Set("valence", 0.5)],
        [Set("affect", {"label": "joy", "intensity": 0.5})],
        [Set("content", "篡改")],
        [Set("S", "high")],
        [Set("S", True)],
        [Inc("access_count", True)],
        [Inc("access_count", "1")],
        ["不是操作符"],
    ],
)
def test_update_memory_each_illegal_op_guarded(config, ops) -> None:
    system, item = _system_with_item(config)
    try:
        system.metrics.reset()
        before = item.to_dict()
        assert system.update_memory(item.id, ops, reason="a2") is False
        assert system.get_item(item.id).to_dict() == before
        assert system.metrics.count("state.write_guard") == 1
    finally:
        system.close()
