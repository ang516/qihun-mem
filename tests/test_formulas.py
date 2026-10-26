"""§10.3 公式自检 + §4.3 / §5 / §6.2 公式行为（可直接进 CI）。"""

from __future__ import annotations

import math

import pytest

from qihun_memory.config import DecayConfig, RetrievalConfig
from qihun_memory.lifecycle import (
    clamp_stability,
    effective_stability,
    initial_stability,
    mood_bias_factor,
    retention,
    reinforce_stability,
    revive_stability,
)
from qihun_memory.models import MemoryItem, utc_now_iso


def R(days: float, stability: float) -> float:
    return math.exp(-days / stability)


def make_item(**overrides) -> MemoryItem:
    base = dict(
        id="m_0001",
        user_id="u_1",
        scope="group",
        type="episode",
        content="用户A下周三去上海出差",
        created_at=utc_now_iso(),
        last_access=utc_now_iso(),
        access_count=0,
        mention_count=1,
        S=7.0,
        valence=0.0,
        status="active",
        tags=["出差"],
    )
    base.update(overrides)
    return MemoryItem(**base)


def test_retention_reference_values() -> None:
    assert abs(retention(7, 7) - 0.3679) < 1e-3
    assert abs(retention(30, 30) - 0.3679) < 1e-3
    assert abs(retention(7, 30) - 0.7919) < 1e-3
    assert abs(retention(30, 7) - 0.0137) < 1e-3
    assert abs(retention(14, 10.5) - 0.2636) < 1e-3
    assert abs(retention(14, 7) - 0.1353) < 1e-3
    assert abs(retention(14, 30 * (1 + 0.5 * 0.9)) - 0.7249) < 1e-3
    assert abs(retention(14, 10.5 * (1 - 0.2 * 0.8)) - 0.2044) < 1e-3


def test_initial_stability_type_and_valence() -> None:
    cfg = DecayConfig()
    assert initial_stability("episode", 0.0, cfg) == pytest.approx(7.0)
    assert initial_stability("fact", 0.0, cfg) == pytest.approx(30.0)
    assert initial_stability("preference", 0.0, cfg) == pytest.approx(30.0)
    assert initial_stability("episode", 1.0, cfg) == pytest.approx(10.5)
    assert initial_stability("fact", 0.9, cfg) == pytest.approx(43.5)
    assert initial_stability("fact", -1.0, cfg) == pytest.approx(45.0)
    # cap 90
    assert initial_stability("fact", 5.0, cfg) == pytest.approx(90.0)


def test_suppression_effective_stability() -> None:
    cfg = DecayConfig()
    item = make_item(type="episode", valence=-1.0, S=10.5)
    assert effective_stability(item, 0.0, cfg) == pytest.approx(10.5)
    assert effective_stability(item, 0.4, cfg) == pytest.approx(10.5)  # 未达 avoidance 下限
    assert effective_stability(item, 0.8, cfg) == pytest.approx(10.5 * (1 - 0.2 * 0.8))
    # S_eff >= 0.8 S
    assert effective_stability(item, 1.0, cfg) == pytest.approx(10.5 * 0.8)
    # 只有编码时负性 < -0.3 才压抑
    neutral = make_item(type="episode", valence=0.0, S=10.5)
    assert effective_stability(neutral, 0.9, cfg) == pytest.approx(10.5)


def test_reinforcement_cap_90() -> None:
    cfg = DecayConfig()
    s = 7.0
    for _ in range(10):
        s = reinforce_stability(s, 1.0, cfg)
    assert s == pytest.approx(90.0)
    assert clamp_stability(120.0, cfg) == pytest.approx(90.0)
    assert revive_stability("episode", cfg) == pytest.approx(7.0)
    assert revive_stability("fact", cfg) == pytest.approx(30.0)


def test_mood_bias_factor() -> None:
    assert mood_bias_factor(0.8, 0.5, 0.2) == pytest.approx(1.2)
    assert mood_bias_factor(-0.8, 0.5, 0.2) == pytest.approx(0.8)
    assert mood_bias_factor(0.0, 0.5, 0.2) == pytest.approx(1.0)
    assert mood_bias_factor(0.5, 0.0, 0.2) == pytest.approx(1.0)


def test_retrieval_config_defaults() -> None:
    cfg = RetrievalConfig()
    assert cfg.budget_tokens == 1200
    assert cfg.header_tokens == 20
    assert cfg.w_vector == pytest.approx(0.6)
    assert cfg.w_bm25 == pytest.approx(0.4)
    assert cfg.mood_bias_enabled is True
    assert cfg.mood_bias_beta == pytest.approx(0.2)
    assert cfg.dormant_factor == pytest.approx(0.3)
    assert cfg.type_bonus["preference"] == pytest.approx(1.1)
