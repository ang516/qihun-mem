"""遗忘与强化的纯函数（v1.4 §4.3 / §5 / §6.2）。

这些函数不依赖 I/O，检索、写入、测试共用，避免各模块各写一套公式。
"""

from __future__ import annotations

import math

from .config import DecayConfig
from .models import MemoryItem


def retention(age_days: float, stability_days: float) -> float:
    """R = exp(-Δt / S)（§5.1）。"""
    age = max(0.0, float(age_days))
    stability = max(1e-9, float(stability_days))
    return math.exp(-age / stability)


def effective_stability(item: MemoryItem, avoidance_now: float, cfg: DecayConfig) -> float:
    """§5.2 负性压抑：只调制 S_eff，永不改写 S。

    仅当「编码时 bot 情绪低落」且「当前回避强度足够高」时生效，
    且 S_eff 不低于 0.8·S（衰减最多加快约 25%）。
    """
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


def initial_stability(item_type: str, valence: float, cfg: DecayConfig) -> float:
    """S₀ = S0(type) · (1 + k_v · |valence|)，cap 90（§4.3）。"""
    base = cfg.s0_episode if item_type == "episode" else cfg.s0_fact
    if item_type not in ("episode", "fact", "preference"):
        base = cfg.s0_fact
    s0 = base * (1.0 + cfg.kv * abs(float(valence)))
    return min(s0, cfg.s_max)


def revive_stability(item_type: str, cfg: DecayConfig) -> float:
    """§5.1 dormant 强匹配召回：S 重置为对应 type 的中性 S₀。"""
    base = cfg.s0_episode if item_type == "episode" else cfg.s0_fact
    return min(base, cfg.s_max)


def reinforce_stability(stability: float, r: float, cfg: DecayConfig) -> float:
    """S ← min(S·(1+R), 90)（§5.3 / §8）。"""
    return min(float(stability) * (1.0 + max(0.0, float(r))), cfg.s_max)


def clamp_stability(stability: float, cfg: DecayConfig) -> float:
    return max(0.0, min(float(stability), cfg.s_max))


def mood_consistency(current_valence: float, item_valence: float) -> float:
    """心境一致性 A：同号 1.0、异号 0.0、任一侧中性 0.5（§6.2）。"""
    current = float(current_valence or 0.0)
    item = float(item_valence or 0.0)
    if current == 0.0 or item == 0.0:
        return 0.5
    return 1.0 if (current > 0) == (item > 0) else 0.0


def mood_bias_factor(current_valence: float, item_valence: float, beta: float) -> float:
    """score × (1 + β·(2A − 1))；同号 1+β、异号 1−β、中性 1（§6.2）。"""
    a = mood_consistency(current_valence, item_valence)
    return 1.0 + beta * (2.0 * a - 1.0)
