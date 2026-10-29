"""遗忘、强化与 S 初始化的纯函数（v1.5-r2 §4.3 / §5 / §7.2）。

v1.5 起 `compute_S` 是**唯一** S 计算入口（附录 D1）：
init / revive / reinforce 三支都在这里实现，k_v / k_a / cap 只出现一次。
旧函数保留为薄封装，内部调用 compute_S，避免旧调用方破坏单点。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .config import DecayConfig
from .models import MemoryItem, affect_intensity, affect_label

# 设计文档 §4.3 的裸 S0 表；实际取值以 DecayConfig 为准（允许配置覆盖）
TYPE_S0 = {"episode": 7.0, "fact": 30.0, "preference": 30.0}


def compute_S(
    mode: str,
    type: str,
    valence: float = 0.0,
    affect: Mapping[str, Any] | None = None,
    S: float | None = None,
    R: float | None = None,
    cfg: DecayConfig | None = None,
) -> float:
    """唯一 S 计算入口（v1.5 §4.3 / §5.1 / §5.3）。

    - init：S0·(1 + k_v·|valence|)，非 neutral affect 再乘 (1 + k_a·intensity)；
    - revive：裸 S0（不带情绪调制）；
    - reinforce：min(S + S·(1+R), cap)；
    - 其它 mode：ValueError（不许静默降级，A5）。
    """
    cfg = cfg or DecayConfig()
    base = cfg.s0_episode if type == "episode" else cfg.s0_fact
    if mode == "init":
        s = base * (1.0 + cfg.kv * abs(float(valence or 0.0)))
        if affect_label(affect) != "neutral":
            s *= 1.0 + cfg.ka * affect_intensity(affect)
        return min(s, cfg.s_max)
    if mode == "revive":
        return min(base, cfg.s_max)
    if mode == "reinforce":
        if S is None:
            raise ValueError("reinforce requires S")
        r = max(0.0, float(R or 0.0))
        return min(float(S) + float(S) * (1.0 + r), cfg.s_max)
    raise ValueError(f"unknown compute_S mode: {mode!r}")


def retention(age_days: float, stability_days: float) -> float:
    """R = exp(-Δt / S)（§5.1 / §6.1）。"""
    age = max(0.0, float(age_days))
    stability = max(1e-9, float(stability_days))
    return math.exp(-age / stability)


def effective_stability(item: MemoryItem, avoidance_now: float, cfg: DecayConfig) -> float:
    """§5.2 负性压抑：只调制 S_eff，永不改写 S。"""
    avoid = max(0.0, min(1.0, float(avoidance_now or 0.0)))
    if item.valence < cfg.suppression_valence and avoid >= cfg.suppression_avoidance:
        s_eff = item.S * (1.0 - cfg.lam * avoid)
    else:
        s_eff = item.S
    floor = cfg.min_effective_ratio * item.S
    return max(s_eff, floor, 1e-9)


def retention_now(
    item: MemoryItem,
    *,
    now,
    avoidance_now: float = 0.0,
    cfg: DecayConfig | None = None,
) -> float:
    """按当前时间计算一条记忆的 R（含压抑调制）。"""
    if cfg is None:
        cfg = DecayConfig()
    age_days = (now - item.last_access_dt).total_seconds() / 86400.0
    return retention(age_days, effective_stability(item, avoidance_now, cfg))


def initial_stability(
    item_type: str,
    valence: float,
    cfg: DecayConfig,
    affect: Mapping[str, Any] | None = None,
) -> float:
    """薄封装：S₀ = compute_S("init", ...)（保留旧签名，兼容 legacy_import / 旧测试）。"""
    return compute_S("init", item_type, valence=valence, affect=affect, cfg=cfg)


def revive_stability(item_type: str, cfg: DecayConfig) -> float:
    """薄封装：dormant 强匹配召回 → compute_S("revive", ...)。"""
    return compute_S("revive", item_type, cfg=cfg)


def reinforce_stability(stability: float, r: float, cfg: DecayConfig, item_type: str = "episode") -> float:
    """薄封装：S ← compute_S("reinforce", ...)（保留旧签名）。"""
    return compute_S("reinforce", item_type, S=stability, R=r, cfg=cfg)


def clamp_stability(stability: float, cfg: DecayConfig) -> float:
    return max(0.0, min(float(stability), cfg.s_max))


def mood_consistency(current_valence: float, item_valence: float) -> float:
    """心境一致性 A：同号 1.0、异号 0.0、任一侧中性 0.5（§7.2）。"""
    current = float(current_valence or 0.0)
    item = float(item_valence or 0.0)
    if current == 0.0 or item == 0.0:
        return 0.5
    return 1.0 if (current > 0) == (item > 0) else 0.0


def mood_bias_factor(current_valence: float, item_valence: float, beta: float) -> float:
    """score × (1 + β·(2A − 1))；同号 1+β、异号 1−β、中性 1（§7.2）。"""
    a = mood_consistency(current_valence, item_valence)
    return 1.0 + beta * (2.0 * a - 1.0)
