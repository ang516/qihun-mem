"""v1.6-rc4 叙事层集成回归（规范 §8 / §12.8 D01~D12）。

夹具说明：
- 所有用例都经 MemorySystem 的真实接线（diary / diary_worker / select_diary_excerpts），
  不手工构造 Context，保证 D01~D12 验证的是产品路径而不是独立模块。
- 生成用注入的 llm callable 模拟宿主/独立 worker；日记永远不进每轮 LLM 调用。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from qihun_memory.config import Config
from qihun_memory.diary import DiaryEntry
from qihun_memory.memory import MemorySystem
from qihun_memory.models import MemoryItem, utc_now, utc_now_iso
from qihun_memory.pipeline import TurnEvent, TurnPipeline
from qihun_memory.prompt import measure_prompt_tokens


DIARY_TEXT = "今天群里有人聊到出差，我想起自己其实不太喜欢机场，站在安检口总会发呆。"


def make_config(tmp_path, diary: dict | None = None, **overrides) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
        "diary": dict(diary or {}),
    }
    data.update(overrides)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def event(scope: str = "group", message: str = "我下周三去上海出差", **overrides) -> TurnEvent:
    base: dict = {"user_id": "u_1", "scope": scope, "message": message, "msgid": "msg-1"}
    base.update(overrides)
    return TurnEvent(**base)


def seed_diary(store, *, scope: str = "group", content: str = DIARY_TEXT, created_at: str | None = None,
               date: str = "2026-10-29", tokens: int = 20, turn_range=()) -> DiaryEntry:
    entry = DiaryEntry(
        id=store.next_id(),
        date=date,
        scope_tag=scope,
        content=content,
        tokens=int(tokens),
        turn_range=tuple(turn_range),
        created_at=created_at or utc_now_iso(),
    )
    store.append(entry)
    return entry


def generate(system: MemorySystem, scope: str = "group", text: str = DIARY_TEXT, *, force: bool = True,
             now=None, turn_id: str | None = None):
    worker = system.diary_worker
    if turn_id:
        worker.record_turn(scope, turn_id)
    return worker.generate(scope, lambda _prompt: text, force=force, now=now)


# --------------------------------------------------------------------------
# D01 生成失败：不写日记，记 diary.fail，回答正常
# --------------------------------------------------------------------------

def test_d01_diary_generation_failure_degrades(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    worker = system.diary_worker

    def boom(_prompt: str) -> str:
        raise RuntimeError("llm down")

    report = worker.generate("group", boom, force=True)
    assert report.status == "fail"
    assert len(system.diary.entries) == 0
    assert system.metrics.count("diary.fail") >= 1

    # 回答链路不受影响
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event(message="我下周三去上海出差", msgid="msg-1"))
    result = pipeline.finish(prepared, "好的。")
    assert result.answer.strip() == "好的。"


# --------------------------------------------------------------------------
# D02 / D03 scope 隔离
# --------------------------------------------------------------------------

def test_d02_dm_does_not_inject_group_diary(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    seed_diary(system.diary, scope="group", content="群聊日记：今天群里有人聊到出差。")
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event(scope="dm", message="我下周三去上海出差", msgid="msg-dm"))
    assert prepared.diary_excerpts == []
    text = "\n".join(message["content"] for message in prepared.prompt.messages)
    assert "群聊日记" not in text


def test_d03_group_does_not_inject_dm_diary(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    seed_diary(system.diary, scope="dm", content="私聊日记：私下里聊到出差。")
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event(scope="group", message="我下周三去上海出差", msgid="msg-g"))
    assert prepared.diary_excerpts == []
    text = "\n".join(message["content"] for message in prepared.prompt.messages)
    assert "私聊日记" not in text


# --------------------------------------------------------------------------
# D04 / D09 隐私护栏
# --------------------------------------------------------------------------

def test_d04_other_nickname_redacted(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    system.refresh_users({"u_1": "小明", "u_2": "小红"})
    report = generate(system, "group", "今天小红说她下周三去上海出差，我有点羡慕。")
    assert report.status == "ok"
    entry = system.diary.entries[report.entry_id]
    assert "小红" not in entry.content
    assert "有人" in entry.content
    assert system.metrics.count("diary.privacy") >= 1


def test_d09_privacy_three_strategies(tmp_path) -> None:
    cfg = make_config(tmp_path, diary={"bot_nickname": "器魂"})
    system = MemorySystem(cfg)
    system.refresh_users({"u_1": "小明", "u_2": "小红"})

    # 他人昵称 → "有人"
    other = generate(system, "group", "小红今天聊到出差。", turn_id="t-1")
    assert other.status == "ok"
    assert "有人" in system.diary.entries[other.entry_id].content

    # bot 自身昵称 → "我"（不拒写）
    bot = generate(system, "group", "我器魂今天也有点感慨。", turn_id="t-2")
    assert bot.status == "ok"
    assert "器魂" not in system.diary.entries[bot.entry_id].content
    assert "我" in system.diary.entries[bot.entry_id].content

    # user_id 字面量 → 拒写
    before = len(system.diary.entries)
    rejected = generate(system, "group", "今天 u_123456 说了一句奇怪的话。", turn_id="t-3")
    assert rejected.status == "privacy"
    assert len(system.diary.entries) == before
    assert system.metrics.count("diary.privacy") >= 3


# --------------------------------------------------------------------------
# D05 日记 + 检索 + 最近轮互不挤占（同一 tokenizer 实测）
# --------------------------------------------------------------------------

def test_d05_diary_retrieval_recent_budgets_coexist(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    seed_diary(system.diary, scope="group", content=DIARY_TEXT * 2, tokens=280)
    seed_diary(system.diary, scope="group", content=DIARY_TEXT, tokens=40,
               created_at=(utc_now() - timedelta(minutes=5)).isoformat(timespec="seconds"))
    for index in range(30):
        now = utc_now_iso()
        system.store.append(
            MemoryItem(
                id=system.store.next_id(),
                user_id="u_1",
                scope="group",
                type="fact",
                content=f"用户A喜欢本帮菜并且去过上海{index}次，这是一个足够长的记忆内容用于占用预算。",
                created_at=now,
                last_access=now,
                mention_count=1,
                S=30.0,
                tags=[],
            )
        )
    pipeline = TurnPipeline(system, system.cfg)
    for index in range(6):
        prepared = pipeline.prepare(
            event(message=f"我下周三去上海出差第{index}次", msgid=f"msg-{index}")
        )
        pipeline.finish(prepared, "好的。")
    prepared = pipeline.prepare(event(message="我下周三去上海出差", msgid="msg-final"))
    assert len(prepared.diary_excerpts) <= 2
    total = measure_prompt_tokens(prepared.prompt.system_prompt, prepared.prompt.messages)
    assert total <= system.cfg.prompt.total_tokens
    assert prepared.prompt.total_tokens <= system.cfg.prompt.total_tokens
    assert prepared.retrieval.tokens <= system.cfg.retrieval.budget_tokens


# --------------------------------------------------------------------------
# D06 幂等
# --------------------------------------------------------------------------

def test_d06_diary_idempotent(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    worker = system.diary_worker
    now = utc_now()
    first = worker.generate("group", lambda _p: DIARY_TEXT, force=True, now=now)
    assert first.status == "ok"
    second = worker.generate("group", lambda _p: DIARY_TEXT + "（重复）", force=True, now=now)
    assert second.status == "dup"
    assert len(system.diary.entries) == 1
    assert system.metrics.count("diary.dup") >= 1


# --------------------------------------------------------------------------
# D07 满 30 天移档
# --------------------------------------------------------------------------

def test_d07_diary_archive_after_30_days(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    now = utc_now()
    old_stamp = (now - timedelta(days=31)).isoformat(timespec="seconds")
    old = seed_diary(system.diary, content="三十一天前的旧日记。", created_at=old_stamp)
    fresh = seed_diary(system.diary, content="今天的日记。", created_at=now.isoformat(timespec="seconds"))

    report = system.maintain(now=now)
    assert old.id not in system.diary.entries
    assert fresh.id in system.diary.entries
    assert report.diary_archived >= 1
    # 移档后不再注入
    excerpts = system.select_diary_excerpts("group", now=now)["excerpts"]
    assert old.id not in [entry.id for entry in excerpts]


# --------------------------------------------------------------------------
# D08 every_n 按 scope 分别计数
# --------------------------------------------------------------------------

def test_d08_every_n_counts_per_scope(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path, diary={"trigger": "every_n", "every_n": 2}))
    worker = system.diary_worker
    worker.record_turn("group", "g-1")
    worker.record_turn("group", "g-2")
    worker.record_turn("dm", "d-1")
    assert worker.due("group") is True
    assert worker.due("dm") is False
    # every_n 当段无新增记忆会 skip；补一条当段新增记忆（§8.2）
    system.remember(
        [{"type": "episode", "content": "用户A下周三去上海出差"}],
        user_id="u_1",
        scope="group",
        valence=0.0,
    )

    report = worker.generate("group", lambda _p: DIARY_TEXT, force=False)
    assert report.status == "ok"
    assert system.diary.entries[report.entry_id].scope_tag == "group"
    assert worker.due("dm") is False
    worker.record_turn("dm", "d-2")
    assert worker.due("dm") is True


# --------------------------------------------------------------------------
# D10 mixed 不自动生成 / 不落盘 / 不自动注入
# --------------------------------------------------------------------------

def test_d10_mixed_not_generated_or_injected(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    report = system.diary_worker.generate("mixed", lambda _p: DIARY_TEXT, force=True)
    assert report.status in ("disabled", "skip", "fail")
    assert all(entry.scope_tag in ("group", "dm") for entry in system.diary.entries.values())
    assert system.select_diary_excerpts("mixed")["excerpts"] == []


# --------------------------------------------------------------------------
# D11 同 scope 窗口 30 篇上限
# --------------------------------------------------------------------------

def test_d11_diary_cap_30_per_scope(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    now = utc_now()
    stamp = now.isoformat(timespec="seconds")
    for index in range(30):
        seed_diary(system.diary, content=f"第{index}篇日记。", created_at=stamp)
    assert system.diary.active_count("group", now=now) == 30
    report = generate(system, "group", "再来一篇。", now=now)
    assert report.status == "cap_hit"
    assert system.diary.active_count("group", now=now) == 30
    assert system.metrics.count("diary.cap_hit") >= 1


# --------------------------------------------------------------------------
# D12 窗口内只有 1 篇 dm，请求 2 篇 → 只注 1 篇，不回退 archive
# --------------------------------------------------------------------------

def test_d12_single_dm_diary_injected_without_fallback(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    seed_diary(system.diary, scope="dm", content="私聊日记：私下里聊到出差。")
    picked = system.select_diary_excerpts("dm")
    assert picked["injected"] == 1
    assert len(picked["excerpts"]) == 1
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event(scope="dm", message="我下周三去上海出差", msgid="msg-dm"))
    assert len(prepared.diary_excerpts) == 1
    assert system.metrics.count("diary.fail") == 0
