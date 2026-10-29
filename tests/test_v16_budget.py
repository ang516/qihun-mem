"""v1.6-rc4 D05：prompt 总预算 3850 / 截断顺序 / 日记注入（§2 / §8.4）。

单测级用例（不跑 pipeline）：直接构造 persona / 摘要 / 日记节选 / 检索结果 / 最近轮，
验证：

- 正常预算下总开销 ≤ limits.total_tokens（3850），且各分项在 §2 预算内；
- 超限时严格按「日记 → 检索 → 最近轮 → 最小可用集」顺序截断，
  丢弃日记第 2 篇 / 全部时记 diary.truncated，进最小可用集记 prompt.budget_over；
- measure_prompt_tokens 口径 = system + 各 message content 之和；
- 日记前缀与位置正确（【会话摘要】之后、【相关记忆】之前）；
- 桥接系统提示仍只出现在 memory_block（不混排进 persona / 系统提示）。
"""

from __future__ import annotations

import pytest

from qihun_memory.config import PromptConfig
from qihun_memory.diary import DIARY_PREFIX, DiaryEntry
from qihun_memory.metrics import Metrics
from qihun_memory.prompt import (
    BRIDGE_SYSTEM_NOTICE,
    EXTRACTION_PROTOCOL,
    MEMORY_DATA_NOTICE,
    build_prompt,
    measure_prompt_tokens,
)
from qihun_memory.state import UserDirectory
from qihun_memory.tokens import estimate_tokens

SCOPE = "group"  # D05 固定群聊语境：日记只注入 group
INF = 10 ** 9  # 基线与「不截断」测量用预算


# --------------------------------------------------------------------------
# 固定构造：500 persona / 200 摘要 / 300 日记 / 1200 检索 / 750 最近轮
# --------------------------------------------------------------------------


def _cjk(count: int) -> str:
    """n 个 CJK 字符 = 估算 n tokens（tokens.estimate_tokens 的契约）。"""
    return "字" * count


def _diary(index: int, date: str, *, tokens: int = 120) -> DiaryEntry:
    content = _cjk(tokens)
    return DiaryEntry(
        id=f"d_{index:04d}",
        date=date,
        scope_tag=SCOPE,
        content=content,
        tokens=estimate_tokens(content),
        turn_range=("turn_001", "turn_005"),
        created_at=date + "T23:50:00+08:00",
    )


def _injected(index: int, tokens: int, *, bridge: bool = False) -> dict:
    return {
        "id": f"m_{index:04d}",
        "user_id": "u_1",
        "scope": SCOPE,
        "type": "fact",
        "content": _cjk(tokens),
        "created_at": "2026-10-23T10:00:00+00:00",
        "r": 0.5,
        "score": 0.6,
        "affect": None,
        "bridge": bridge,
        "source_pool": "bridge" if bridge else "group",
        "tokens": tokens,
    }


def _limits(total_tokens: int = 3850) -> PromptConfig:
    return PromptConfig(total_tokens=total_tokens)


def _recent_turns() -> list[tuple[str, str]]:
    """5 轮 × 150 tokens = 750（正好用满最近轮分项预算）。"""
    return [(_cjk(60), _cjk(90)) for _ in range(5)]


def _build(
    users,
    *,
    limits: PromptConfig,
    diary_excerpts=(),
    injected_items=(),
    recent_turns=None,
    gate_passed: bool = True,
    metrics=None,
):
    return build_prompt(
        persona=_cjk(500),
        summary=_cjk(200),
        users=users,
        recent_turns=_recent_turns() if recent_turns is None else recent_turns,
        user_message="在吗",
        gate_passed=gate_passed,
        limits=limits,
        injected_items=list(injected_items),
        diary_excerpts=list(diary_excerpts),
        metrics=metrics,
    )


@pytest.fixture()
def users(tmp_path) -> UserDirectory:
    directory = UserDirectory(tmp_path / "users.json")
    directory.refresh({"u_1": "用户A"})
    return directory


@pytest.fixture()
def metrics() -> Metrics:
    return Metrics()


# --------------------------------------------------------------------------
# 1) 正常预算：D05 固定构造总开销 ≤3850
# --------------------------------------------------------------------------


def test_d05_fixed_components_fit_total_budget(users, metrics) -> None:
    limits = PromptConfig()
    # 契约 §2.1：总预算固定 3850 = 500 + 200 + 300 + 1200 + 750 + 300 + 600
    assert limits.total_tokens == 3850
    assert limits.diary_tokens == 300
    assert limits.protocol_tokens == 300
    assert limits.system_misc_tokens == 600
    bundle = _build(
        users,
        limits=limits,
        diary_excerpts=[_diary(1, "2026-10-28", tokens=120), _diary(2, "2026-10-29", tokens=110)],
        injected_items=[_injected(index, 180) for index in range(1, 7)],
        metrics=metrics,
    )
    # total_tokens 是实测值（同一 tokenizer），与 measure_prompt_tokens 口径一致
    assert bundle.total_tokens == measure_prompt_tokens(bundle.system_prompt, bundle.messages)
    assert bundle.total_tokens <= limits.total_tokens
    assert bundle.truncated is False
    assert metrics.count("diary.truncated") == 0
    assert metrics.count("prompt.budget_over") == 0
    # 分项预算：日记 ≤300 / 检索 ≤1200 / 最近轮 5 轮 ≈750 / persona 500 / 摘要 200 / 协议 300
    assert estimate_tokens(bundle.diary_block) <= limits.diary_tokens
    assert estimate_tokens(bundle.memory_block) <= 1200
    assert estimate_tokens(bundle.system_prompt) <= (
        limits.persona_tokens + limits.protocol_tokens + limits.system_misc_tokens
    )
    assert len(bundle.memory_ids) == 6
    assert len(bundle.messages) == 5 * 2 + 1  # 最近 5 轮 + 当前消息
    assert bundle.system_prompt.endswith(EXTRACTION_PROTOCOL)
    # to_dict 增量字段（旧字段不变）
    payload = bundle.to_dict()
    assert payload["system"] == bundle.system_prompt
    assert payload["total_tokens"] == bundle.total_tokens
    assert payload["diary_block"] == bundle.diary_block
    assert payload["truncated"] is False


# --------------------------------------------------------------------------
# 2) measure_prompt_tokens 口径
# --------------------------------------------------------------------------


def test_measure_prompt_tokens_sums_system_and_contents() -> None:
    # system「系」×10 = 10 tokens；"abc" 非 CJK 4 字符/token 向上取整 = 1
    assert measure_prompt_tokens("系" * 10, [{"role": "user", "content": "abc"}]) == 11
    assert measure_prompt_tokens("", []) == 0
    assert measure_prompt_tokens("", [{"role": "assistant", "content": ""}]) == 0
    messages = (
        {"role": "user", "content": "字" * 7},
        {"role": "assistant", "content": _cjk(3) + "abcd"},  # 3 + 1
    )
    assert measure_prompt_tokens("字" * 3, messages) == 3 + 7 + 4


# --------------------------------------------------------------------------
# 3) 截断顺序：日记 → 检索 → 最近轮 → 最小可用集
# --------------------------------------------------------------------------


def test_truncation_drops_second_diary_before_retrieval(users, metrics) -> None:
    diaries = [_diary(1, "2026-10-28", tokens=120), _diary(2, "2026-10-29", tokens=110)]
    retrieval = [_injected(index, 180) for index in range(1, 7)]
    recent = _recent_turns()
    full = _build(users, limits=_limits(INF), diary_excerpts=diaries, injected_items=retrieval)
    # 只超 1 token：丢掉第 2 篇日记就应回到预算内，检索与最近轮原样保留
    bundle = _build(
        users,
        limits=_limits(full.total_tokens - 1),
        diary_excerpts=diaries,
        injected_items=retrieval,
        metrics=metrics,
    )
    assert bundle.truncated is True
    assert bundle.total_tokens <= full.total_tokens - 1
    assert bundle.diary_block.count("[日记 ") == 1  # 只留第 1 篇
    assert len(bundle.memory_ids) == 6  # 检索没有被丢
    assert len(bundle.messages) == len(recent) * 2 + 1  # 最近轮没有被丢
    assert metrics.count("diary.truncated") == 1
    assert metrics.count("prompt.budget_over") == 0


def test_truncation_drops_retrieval_after_all_diary(users, metrics) -> None:
    diaries = [_diary(1, "2026-10-28", tokens=120), _diary(2, "2026-10-29", tokens=110)]
    retrieval = [_injected(index, 180) for index in range(1, 7)]
    no_diary = _build(users, limits=_limits(INF), diary_excerpts=(), injected_items=retrieval)
    # 超到「日记全丢仍不够」：应继续从尾部逐条丢检索，最近轮不动
    bundle = _build(
        users,
        limits=_limits(no_diary.total_tokens - 1),
        diary_excerpts=diaries,
        injected_items=retrieval,
        metrics=metrics,
    )
    assert bundle.truncated is True
    assert bundle.total_tokens <= no_diary.total_tokens - 1
    assert bundle.diary_block == ""  # 日记两篇都丢
    assert 0 < len(bundle.memory_ids) < 6  # 检索从尾部丢，未全丢
    assert len(bundle.messages) == 5 * 2 + 1  # 最近轮仍未被丢
    assert metrics.count("diary.truncated") == 2  # 2→1、1→0 各一次
    assert metrics.count("prompt.budget_over") == 0


def test_truncation_drops_oldest_recent_turn_last(users, metrics) -> None:
    # 最近轮内容可区分（同 token 数、不同字），用于断言「丢最旧、留最新」
    distinct = [(_cjk(58) + ch + ch, _cjk(90)) for ch in "甲乙丙丁戊"]
    no_diary_no_retrieval = _build(
        users,
        limits=_limits(INF),
        diary_excerpts=(),
        injected_items=(),
        recent_turns=distinct,
    )
    bundle = _build(
        users,
        limits=_limits(no_diary_no_retrieval.total_tokens - 1),
        diary_excerpts=(),
        injected_items=(),
        recent_turns=distinct,
        metrics=metrics,
    )
    assert bundle.truncated is True
    assert bundle.total_tokens <= no_diary_no_retrieval.total_tokens - 1
    assert bundle.diary_block == ""
    assert bundle.memory_ids == ()
    # 从小到旧丢：只剩新 4 轮 + 当前消息，且第一条是最初的第 2 轮
    assert len(bundle.messages) == 4 * 2 + 1
    assert bundle.messages[0]["content"] == _cjk(58) + "乙乙"
    assert metrics.count("prompt.budget_over") == 0


def test_budget_over_falls_back_to_minimal_set(users, metrics) -> None:
    bundle = _build(
        users,
        limits=_limits(10),  # 任何可用集都装不下 → 最小可用集 + budget_over
        diary_excerpts=[_diary(1, "2026-10-28")],
        injected_items=[_injected(1, 180)],
        metrics=metrics,
    )
    assert metrics.count("prompt.budget_over") == 1
    assert bundle.truncated is True
    # 最小可用集 = persona + 最近 1 轮 + 输出协议（gate_passed 时）；不含摘要/日记/检索
    assert _cjk(500) in bundle.system_prompt
    assert MEMORY_DATA_NOTICE in bundle.system_prompt
    assert EXTRACTION_PROTOCOL in bundle.system_prompt
    assert bundle.diary_block == ""
    assert bundle.memory_ids == ()
    assert bundle.memory_block == ""
    assert "【会话摘要】" not in bundle.messages[-1]["content"]
    assert "【当前消息】" in bundle.messages[-1]["content"]
    assert len(bundle.messages) == 3  # 最近 1 轮（user + assistant）+ 当前消息
    # 最小可用集仍按 gate_passed 决定是否附协议
    no_gate = _build(
        users,
        limits=_limits(10),
        diary_excerpts=[_diary(1, "2026-10-28")],
        injected_items=[_injected(1, 180)],
        gate_passed=False,
    )
    assert EXTRACTION_PROTOCOL not in no_gate.system_prompt
    assert no_gate.truncated is True


# --------------------------------------------------------------------------
# 4) 日记前缀 / 位置；桥接系统提示仍只在 memory_block
# --------------------------------------------------------------------------


def test_diary_block_prefix_and_position(users) -> None:
    bundle = _build(
        users,
        limits=_limits(INF),
        diary_excerpts=[_diary(1, "2026-10-28"), _diary(2, "2026-10-29")],
        injected_items=[_injected(1, 100)],
    )
    last = bundle.messages[-1]["content"]
    assert last.count(DIARY_PREFIX) == 1
    assert bundle.diary_block.startswith(DIARY_PREFIX)
    # 位置：用户上下文区，【会话摘要】之后、【相关记忆】之前
    summary_at = last.find("【会话摘要】")
    diary_at = last.find(DIARY_PREFIX)
    memory_at = last.find("【相关记忆】")
    current_at = last.find("【当前消息】")
    assert -1 not in (summary_at, diary_at, memory_at, current_at)
    assert summary_at < diary_at < memory_at < current_at
    # 格式（§8.4）：[日记 2026-10-29 | 群聊] + 正文；两篇都在
    assert "[日记 2026-10-28 | 群聊]" in last
    assert "[日记 2026-10-29 | 群聊]" in last
    # 日记只进用户上下文区，不进系统提示
    assert DIARY_PREFIX not in bundle.system_prompt


def test_bridge_system_notice_only_in_memory_block(users) -> None:
    bundle = _build(
        users,
        limits=_limits(INF),
        diary_excerpts=[_diary(1, "2026-10-28")],
        injected_items=[_injected(1, 100, bridge=True), _injected(2, 100)],
    )
    last = bundle.messages[-1]["content"]
    assert BRIDGE_SYSTEM_NOTICE in bundle.memory_block
    assert BRIDGE_SYSTEM_NOTICE not in bundle.system_prompt
    # 系统提示只出现一次，且随记忆块整体出现在用户上下文区一次
    assert last.count(BRIDGE_SYSTEM_NOTICE) == 1
    assert last.count(bundle.memory_block) == 1
