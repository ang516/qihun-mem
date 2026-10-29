"""v1.6-rc4 叙事层单测（D01~D12 的单测级覆盖）。

覆盖范围（实现契约 §2.2 / 规范 §8）：
- Config：DiaryConfig / PromptConfig 预算字段 / 路径属性 / JSON 数组转 tuple；
- DiaryStore：加载容错与 diary.bad.jsonl 幂等搬移、append/rewrite/archive、
  archive_expired 30 天硬窗口、active 窗口与排序、id 续号（含 archive）、只读模式；
- DiaryWorker：daily / every_n 按 scope 分别触发、当段无新增记忆的 daily/every_n 分支、
  prepare prompt 快照、隐私三策略、截断、cap、幂等、LLM 失败重试 ≤1、异常降级；
- select_excerpts / render_diary_block：scope 过滤、第 1 篇必装、第 2 篇预算、
  不足 2 篇按实际注入、格式与空串。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qihun_memory.config import Config, DiaryConfig
from qihun_memory.diary import (
    DIARY_PREFIX,
    DIARY_SCOPES,
    DiaryEntry,
    DiaryLoadReport,
    DiaryPrompt,
    DiaryRunReport,
    DiaryStore,
    DiaryWorker,
    render_diary_block,
    select_excerpts,
)
from qihun_memory.metrics import Metrics
from qihun_memory.models import MemoryItem
from qihun_memory.state import BotState, UserDirectory

TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# 夹具 / 辅助
# --------------------------------------------------------------------------

def at(hour: int = 23, minute: int = 55, day: int = 29) -> datetime:
    """2026-10-29 的本地时间（+08:00）。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def make_cfg(tmp_path: Path, diary: dict | None = None, **extra) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    if diary is not None:
        data["diary"] = diary
    data.update(extra)
    return Config.from_dict(data, base_dir=tmp_path)


def make_store(cfg: Config, metrics: Metrics | None = None) -> DiaryStore:
    store = DiaryStore(cfg, metrics)
    store.load()
    return store


def make_users(cfg: Config, mapping: dict) -> UserDirectory:
    cfg.users_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.users_file.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return UserDirectory(cfg.users_file)


def entry(
    entry_id: str = "d_0001",
    *,
    scope_tag: str = "group",
    content: str = "今天群里聊到出差，我有些感慨。",
    date: str = "2026-10-29",
    created_at: str = "2026-10-29T12:00:00+08:00",
    tokens: int = 5,
    turn_range: tuple[str, ...] = ("t_1", "t_2"),
) -> DiaryEntry:
    return DiaryEntry(
        id=entry_id,
        date=date,
        scope_tag=scope_tag,
        content=content,
        tokens=tokens,
        turn_range=turn_range,
        created_at=created_at,
    )


def mem(
    content: str,
    *,
    scope: str = "group",
    created_at: str = "2026-10-29T10:00:00+08:00",
    item_type: str = "episode",
    item_id: str = "m_0001",
) -> MemoryItem:
    return MemoryItem(
        id=item_id,
        user_id="u_123456",
        scope=scope,
        type=item_type,
        content=content,
        created_at=created_at,
        last_access=created_at,
    )


def make_worker(
    cfg: Config,
    store: DiaryStore,
    *,
    metrics: Metrics | None = None,
    memories=(),
    state: BotState | None = None,
    users=None,
    affects: dict | None = None,
    now: datetime | None = None,
) -> DiaryWorker:
    metrics = metrics if metrics is not None else Metrics()
    return DiaryWorker(
        cfg,
        store,
        metrics=metrics,
        memory_items=lambda: list(memories),
        state_loader=(lambda: state) if state is not None else None,
        users=users,
        affect_snapshot=(lambda: dict(affects)) if affects is not None else None,
        now_fn=(lambda: now) if now is not None else None,
    )


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ==========================================================================
# 1. Config 增量
# ==========================================================================

def test_config_diary_defaults_and_paths() -> None:
    cfg = Config()
    assert isinstance(cfg.diary, DiaryConfig)
    assert cfg.diary.enabled is True
    assert cfg.diary.trigger == "daily"
    assert cfg.diary.every_n == 50
    assert cfg.diary.daily_at == "23:50"
    assert cfg.diary.timezone == "Asia/Shanghai"
    assert cfg.diary.max_tokens == 300
    assert cfg.diary.inject_budget == 300
    assert cfg.diary.inject_count == 2
    assert cfg.diary.retain_days == 30
    assert cfg.diary.max_per_scope == 30
    assert cfg.diary.privacy_check is True
    assert cfg.diary.bot_nickname == ""
    assert cfg.diary.gen_model == ""
    # v1.6 §2 预算：总 3850，分项与旧字段保持
    assert cfg.prompt.total_tokens == 3850
    assert cfg.prompt.diary_tokens == 300
    assert cfg.prompt.protocol_tokens == 300
    assert cfg.prompt.system_misc_tokens == 600
    assert cfg.prompt.summary_tokens == 200
    assert cfg.prompt.recent_tokens == 750
    # §3.4 三个日记文件路径
    data_path = Path(cfg.data_dir)
    assert cfg.diary_file == data_path / "diary.jsonl"
    assert cfg.diary_archive_file == data_path / "diary.archive.jsonl"
    assert cfg.diary_bad_file == data_path / "diary.bad.jsonl"
    # v1.6 §7.1 寒暄词表：tuple 且默认非空
    assert isinstance(cfg.gate.greeting_words, tuple)
    assert "你好" in cfg.gate.greeting_words
    assert len(cfg.gate.greeting_words) >= 10


def test_config_from_dict_converts_tuple_fields(tmp_path: Path) -> None:
    cfg = Config.from_dict(
        {
            "data_dir": "d",
            "diary": {"trigger": "every_n", "every_n": 5, "inject_count": 1},
            "gate": {
                "greeting_words": ["嗨", "哈喽"],
                "extra_patterns": ["<X>"],
            },
            "prompt": {"total_tokens": 4000},
        },
        base_dir=tmp_path,
    )
    assert cfg.diary.trigger == "every_n"
    assert cfg.diary.every_n == 5
    assert cfg.diary.inject_count == 1
    assert cfg.gate.greeting_words == ("嗨", "哈喽")
    assert cfg.gate.extra_patterns == ("<X>",)
    assert cfg.prompt.total_tokens == 4000


# ==========================================================================
# 2. DiaryStore：加载 / 坏行 / 追加 / 重写 / 移档 / 续号
# ==========================================================================

def test_store_load_empty(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    report = store.load()
    assert isinstance(report, DiaryLoadReport)
    assert (report.total_lines, report.valid, report.bad, report.new_bad) == (0, 0, 0, 0)
    assert store.counts() == {"total": 0, "group": 0, "dm": 0}
    assert store.next_id() == "d_0001"
    assert store.active("group", at()) == []
    assert store.last_created_at("group") is None


def test_store_load_bad_lines_moved_idempotently(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    good = entry("d_0001").to_dict()
    bad_scope = entry("d_0002").to_dict()
    bad_scope["scope_tag"] = "mixed"
    cfg.diary_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.diary_file.write_text(
        json.dumps(good, ensure_ascii=False)
        + "\n"
        + "not-json-line\n"
        + json.dumps(bad_scope, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    # 注意：这里直接构造 store 再 load，不能用 make_store（make_store 已 load 一次会把坏行搬走）
    store = DiaryStore(cfg)
    report = store.load()
    assert report.total_lines == 3
    assert report.valid == 1
    assert report.bad == 2
    assert report.new_bad == 2
    assert report.bad_samples
    assert len(read_lines(cfg.diary_bad_file)) == 2
    # 再次加载：坏行仍在，但 new_bad=0（幂等、去重搬移）
    again = store.load()
    assert again.bad == 2
    assert again.new_bad == 0
    assert len(read_lines(cfg.diary_bad_file)) == 2


def test_store_append_and_reload_roundtrip(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    store.append(entry("d_0001", turn_range=("t_1", "t_2")))
    store.append(entry("d_0002", scope_tag="dm", content="私下里有点累。"))
    reloaded = make_store(cfg)
    report = reloaded.load()
    assert report.valid == 2
    reloaded_entry = reloaded.entries["d_0001"]
    assert reloaded_entry.scope_tag == "group"
    assert reloaded_entry.turn_range == ("t_1", "t_2")
    assert reloaded_entry.content == entry().content
    assert reloaded.counts() == {"total": 2, "group": 1, "dm": 1}


def test_store_next_id_continues_from_archive(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cfg.diary_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.diary_file.write_text(
        json.dumps(entry("d_0002").to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cfg.diary_archive_file.write_text(
        json.dumps(entry("d_0007").to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    store = make_store(cfg)
    # 归档里的 d_0007 也参与续号，避免 id 重用
    assert store.next_id() == "d_0008"


def test_store_read_only_never_writes(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cfg.diary_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.diary_file.write_text(
        json.dumps(entry("d_0001").to_dict(), ensure_ascii=False) + "\nbad-line\n",
        encoding="utf-8",
    )
    store = DiaryStore(cfg, Metrics(), read_only=True)
    report = store.load()
    assert report.valid == 1
    assert report.bad == 1
    assert report.new_bad == 0
    assert not cfg.diary_bad_file.exists()
    assert [item.id for item in store.active("group", at())] == ["d_0001"]
    with pytest.raises(RuntimeError):
        store.append(entry("d_0002"))
    with pytest.raises(RuntimeError):
        store.rewrite()
    with pytest.raises(RuntimeError):
        store.archive(["d_0001"])
    assert store.archive_expired(at()) == 0


def test_store_archive_moves_to_archive_file(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    for index in (1, 2, 3):
        store.append(entry("d_000" + str(index)))
    assert store.archive(["d_0001", "d_0003"]) == 2
    assert set(store.entries) == {"d_0002"}
    reloaded = make_store(cfg)
    reloaded.load()
    assert set(reloaded.entries) == {"d_0002"}
    archived_ids = {json.loads(line)["id"] for line in read_lines(cfg.diary_archive_file)}
    assert archived_ids == {"d_0001", "d_0003"}
    # 已不在 diary.jsonl 的 id 再次 archive 返回 0（不重复追加）
    assert store.archive(["d_0001"]) == 0


def test_store_archive_expired_uses_30d_window(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    metrics = Metrics()
    store = make_store(cfg, metrics)
    now = at()
    store.append(entry("d_0001", created_at=(now - timedelta(days=31)).isoformat()))
    store.append(entry("d_0002", created_at=(now - timedelta(days=30)).isoformat()))
    store.append(entry("d_0003", created_at=(now - timedelta(days=29)).isoformat()))
    moved = store.archive_expired(now)
    # created_at ≤ now-30d 移档（硬窗口边界），29 天内的留下
    assert moved == 2
    assert set(store.entries) == {"d_0003"}
    assert metrics.count("diary.archive") == 1
    archived_ids = {json.loads(line)["id"] for line in read_lines(cfg.diary_archive_file)}
    assert archived_ids == {"d_0001", "d_0002"}


def test_store_active_window_and_order(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    now = at()
    store.append(entry("d_0001", created_at=(now - timedelta(hours=1)).isoformat()))
    store.append(entry("d_0002", created_at=(now - timedelta(hours=2)).isoformat()))
    # created_at 相同 → id 倒序：d_0004 在 d_0003 之前
    store.append(entry("d_0003", created_at=(now - timedelta(minutes=10)).isoformat()))
    store.append(entry("d_0004", created_at=(now - timedelta(minutes=10)).isoformat()))
    store.append(entry("d_0005", created_at=(now - timedelta(days=31)).isoformat()))
    store.append(entry("d_0006", created_at=now.isoformat(), scope_tag="dm"))
    active = store.active("group", now)
    assert [item.id for item in active] == ["d_0004", "d_0003", "d_0001", "d_0002"]
    assert store.active_count("group", now) == 4
    assert [item.id for item in store.active("dm", now)] == ["d_0006"]
    # mixed 永不参与注入
    assert store.active("mixed", now) == []
    # 窗口 (now-30d, now]：now 本身包含，now-30d 整点排除
    store.append(entry("d_0007", created_at=now.isoformat()))
    store.append(entry("d_0008", created_at=(now - timedelta(days=30)).isoformat()))
    assert [item.id for item in store.active("group", now)][0] == "d_0007"
    assert "d_0008" not in {item.id for item in store.active("group", now)}


def test_store_last_created_at_per_scope(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    assert store.last_created_at("group") is None
    store.append(entry("d_0001", created_at="2026-10-20T12:00:00+08:00"))
    store.append(entry("d_0002", created_at="2026-10-28T12:00:00+08:00"))
    store.append(entry("d_0003", created_at="2026-10-27T12:00:00+08:00", scope_tag="dm"))
    latest_group = store.last_created_at("group")
    assert latest_group is not None
    assert latest_group == datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)
    assert store.last_created_at("dm") == datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)
    assert store.last_created_at("mixed") is None


def test_store_has_idempotent(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    store.append(entry("d_0001", date="2026-10-29", turn_range=("t_1", "t_2")))
    assert store.has_idempotent("2026-10-29", "group", ("t_1", "t_2")) is True
    assert store.has_idempotent("2026-10-29", "group", ["t_1", "t_2"]) is True
    assert store.has_idempotent("2026-10-29", "group", ("t_1",)) is False
    assert store.has_idempotent("2026-10-30", "group", ("t_1", "t_2")) is False
    assert store.has_idempotent("2026-10-29", "dm", ("t_1", "t_2")) is False
    assert store.has_idempotent("2026-10-29", "mixed", ("t_1", "t_2")) is False


def test_store_duplicate_id_quarantined(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cfg.diary_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.diary_file.write_text(
        json.dumps(entry("d_0001").to_dict(), ensure_ascii=False)
        + "\n"
        + json.dumps(entry("d_0001", content="重复 id").to_dict(), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    store = make_store(cfg)
    report = store.load()
    assert report.valid == 1
    assert report.bad == 1
    assert "duplicate diary id" in report.bad_samples


def test_entry_roundtrip_and_schema_validation() -> None:
    original = entry("d_0001", turn_range=("t_1", "t_2"))
    payload = original.to_dict()
    assert payload["turn_range"] == ["t_1", "t_2"]
    assert DiaryEntry.from_dict(payload) == original

    naive = original.to_dict()
    naive["created_at"] = "2026-10-29T12:00:00"
    with pytest.raises(ValueError):
        DiaryEntry.from_dict(naive)

    bad_scope = original.to_dict()
    bad_scope["scope_tag"] = "mixed"
    with pytest.raises(ValueError):
        DiaryEntry.from_dict(bad_scope)

    bad_date = original.to_dict()
    bad_date["date"] = "2026-99-99"
    with pytest.raises(ValueError):
        DiaryEntry.from_dict(bad_date)

    bad_tokens = original.to_dict()
    bad_tokens["tokens"] = "many"
    with pytest.raises(ValueError):
        DiaryEntry.from_dict(bad_tokens)


# ==========================================================================
# 3. select_excerpts / render_diary_block（§8.4）
# ==========================================================================

def test_select_excerpts_scope_order_and_metrics(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    metrics = Metrics()
    now = at()
    store.append(
        entry(
            "d_0001",
            content="旧" * 10,
            created_at=(now - timedelta(hours=2)).isoformat(),
        )
    )
    store.append(
        entry(
            "d_0002",
            content="新" * 20,
            created_at=(now - timedelta(hours=1)).isoformat(),
        )
    )
    store.append(
        entry(
            "d_0003",
            scope_tag="dm",
            content="私" * 5,
            created_at=(now - timedelta(minutes=30)).isoformat(),
        )
    )
    result = select_excerpts(store, "group", now=now, metrics=metrics)
    assert [item.id for item in result["excerpts"]] == ["d_0002", "d_0001"]
    assert result["injected"] == 2
    assert result["tokens"] == 30
    assert result["truncated"] is False
    assert "d_0003" not in [item.id for item in result["excerpts"]]
    assert metrics.count("diary.inject") == 1
    assert metrics.count("diary.tokens") == 1


def test_select_excerpts_truncates_second_when_budget_tight(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    metrics = Metrics()
    now = at()
    store.append(
        entry("d_0001", content="旧" * 20, created_at=(now - timedelta(hours=2)).isoformat())
    )
    store.append(
        entry("d_0002", content="新" * 20, created_at=(now - timedelta(hours=1)).isoformat())
    )
    result = select_excerpts(store, "group", now=now, budget=30, count=2, metrics=metrics)
    # 第 1 篇必装；第 2 篇放不下整篇则不装、不截断正文
    assert [item.id for item in result["excerpts"]] == ["d_0002"]
    assert result["injected"] == 1
    assert result["tokens"] == 20
    assert result["truncated"] is True
    assert metrics.count("diary.truncated") == 1


def test_select_excerpts_both_fit_under_budget(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    now = at()
    store.append(
        entry("d_0001", content="旧" * 10, created_at=(now - timedelta(hours=2)).isoformat())
    )
    store.append(
        entry("d_0002", content="新" * 10, created_at=(now - timedelta(hours=1)).isoformat())
    )
    result = select_excerpts(store, "group", now=now, budget=300)
    assert [item.id for item in result["excerpts"]] == ["d_0002", "d_0001"]
    assert result["tokens"] == 20
    assert result["truncated"] is False


def test_select_excerpts_first_entry_always_injected(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    now = at()
    store.append(
        entry("d_0001", content="长" * 400, created_at=(now - timedelta(hours=1)).isoformat())
    )
    store.append(
        entry("d_0002", content="短" * 5, created_at=(now - timedelta(hours=2)).isoformat())
    )
    result = select_excerpts(store, "group", now=now, budget=300)
    assert [item.id for item in result["excerpts"]] == ["d_0001"]
    assert result["tokens"] == 400
    assert result["injected"] == 1


def test_select_excerpts_mixed_and_single_dm(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = make_store(cfg)
    now = at()
    store.append(
        entry(
            "d_0001",
            scope_tag="dm",
            content="私下里有点累。",
            created_at=(now - timedelta(hours=1)).isoformat(),
        )
    )
    # mixed 返回空（不注入、不报错）
    mixed = select_excerpts(store, "mixed", now=now)
    assert mixed == {"excerpts": [], "tokens": 0, "injected": 0, "truncated": False}
    # 窗口内只有 1 篇 dm：注入 1 篇，不回退 archive，不报错（D12）
    single = select_excerpts(store, "dm", now=now)
    assert single["injected"] == 1
    assert single["truncated"] is False


def test_render_diary_block_format(tmp_path: Path) -> None:
    group_entry = entry("d_0001", content="今天群里有人聊到出差。", date="2026-10-29")
    dm_entry = entry(
        "d_0002",
        scope_tag="dm",
        content="私下里有点累。",
        date="2026-10-28",
    )
    text = render_diary_block([group_entry, dm_entry])
    assert text == (
        "[日记 2026-10-29 | 群聊]\n今天群里有人聊到出差。\n\n"
        "[日记 2026-10-28 | 私聊]\n私下里有点累。"
    )
    assert DIARY_PREFIX not in text
    assert render_diary_block([]) == ""
    assert render_diary_block(None) == ""
    # mixed / 未知 scope 的条目被跳过
    assert render_diary_block([{"date": "2026-10-29", "scope_tag": "mixed", "content": "x"}]) == ""
    # 兼容 select_excerpts 的返回 dict
    assert "[日记 2026-10-29 | 群聊]" in render_diary_block({"excerpts": [group_entry]})


# ==========================================================================
# 4. DiaryWorker：触发判定（daily / every_n 按 scope）
# ==========================================================================

def test_due_daily_per_scope_and_no_dup_after_write(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily", "daily_at": "23:50"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    assert worker.due("group", at(23, 55)) is True
    assert worker.due("dm", at(23, 55)) is True
    assert worker.due("mixed", at(23, 55)) is False
    # 未过 daily_at 不触发
    assert worker.due("group", at(23, 49)) is False
    prepared = worker.prepare("group", now=at(23, 55))
    assert worker.finish(prepared, "今天群里的感受。").status == "ok"
    # 当日同 scope 已有日记 → daily 不重复；dm 不受 group 影响
    assert worker.due("group", at(23, 56)) is False
    assert worker.due("dm", at(23, 56)) is True
    assert "diary.ok" in metrics.counters()


def test_due_every_n_per_scope_and_disabled(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "every_n", "every_n": 50})
    store = make_store(cfg)
    worker = make_worker(cfg, store)
    now = at()
    assert worker.due("group", now) is False
    for index in range(49):
        worker.record_turn("group", "g_" + str(index), at=now - timedelta(minutes=60))
    assert worker.due("group", now) is False
    worker.record_turn("group", "g_49", at=now - timedelta(minutes=1))
    assert worker.due("group", now) is True
    # every_n 按 scope 独立计数：dm 仍未触发
    assert worker.due("dm", now) is False

    disabled_cfg = make_cfg(tmp_path, {"enabled": False})
    disabled_worker = make_worker(disabled_cfg, make_store(disabled_cfg))
    assert disabled_worker.due("group", now) is False


def test_due_every_n_resets_after_last_diary(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "every_n", "every_n": 3})
    store = make_store(cfg)
    now = at()
    store.append(entry("d_0001", created_at=(now - timedelta(hours=1)).isoformat()))
    worker = make_worker(cfg, store)
    # 上次日记之前的 turn 不计入当段
    for index in range(3):
        worker.record_turn(
            "group", "old_" + str(index), at=now - timedelta(hours=2)
        )
    assert worker.due("group", now) is False
    # 上次日记之后的 turn 才计数
    for index in range(2):
        worker.record_turn(
            "group", "new_" + str(index), at=now - timedelta(minutes=20 - index)
        )
    assert worker.due("group", now) is False
    worker.record_turn("group", "new_2", at=now - timedelta(minutes=10))
    assert worker.due("group", now) is True


# ==========================================================================
# 5. DiaryWorker.prepare：模板 / 快照 / 分支
# ==========================================================================

def test_prepare_daily_prompt_contains_snapshots(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily", "bot_nickname": "器魂"})
    store = make_store(cfg)
    now = at()
    users = make_users(cfg, {"u_123456": "小明"})
    memories = [
        mem("用户A下周三去上海出差", created_at=(now - timedelta(hours=2)).isoformat()),
        mem(
            "私下里说要早点睡",
            scope="dm",
            created_at=(now - timedelta(hours=1)).isoformat(),
            item_id="m_0002",
        ),
    ]
    state = BotState(valence=0.5, avoidance=0.2, stale=False)
    affects = {"u_123456": {"affection": 0.6, "annoyance": 0.1, "familiarity": 0.8}}
    worker = make_worker(
        cfg,
        store,
        memories=memories,
        state=state,
        users=users,
        affects=affects,
    )
    worker.record_turn("group", "t_1", at=now - timedelta(minutes=5))
    worker.record_turn("group", "t_2", at=now - timedelta(minutes=4))
    prepared = worker.prepare("group", now=now)
    assert prepared.due is True
    assert prepared.reason == "daily"
    assert prepared.date == "2026-10-29"
    assert prepared.turn_range == ("t_1", "t_2")
    prompt = prepared.prompt
    assert "你是 bot" in prompt
    assert "群聊（group）" in prompt
    assert "用户A下周三去上海出差" in prompt
    assert "私下里说要早点睡" not in prompt  # scope 过滤
    assert "valence=+0.50" in prompt
    assert "avoidance=0.20" in prompt
    assert "小明" in prompt
    assert "熟悉=0.80" in prompt
    assert "当段新增记忆" in prompt


def test_prepare_not_due_and_invalid_scope(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily", "daily_at": "23:50"})
    store = make_store(cfg)
    worker = make_worker(cfg, store)
    not_due = worker.prepare("group", now=at(23, 0))
    assert not_due.due is False
    assert not_due.prompt == ""
    assert not_due.reason == "not_due"
    mixed = worker.prepare("mixed", now=at(23, 55))
    assert mixed.due is False
    assert mixed.prompt == ""
    assert mixed.reason == "invalid_scope"


def test_prepare_force_bypasses_due(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily", "daily_at": "23:50"})
    store = make_store(cfg)
    worker = make_worker(cfg, store)
    forced = worker.prepare("group", now=at(10, 0), force=True)
    assert forced.due is True
    assert forced.reason == "force"
    assert forced.prompt != ""


def test_prepare_daily_without_new_memory_still_generates(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    worker = make_worker(cfg, store, memories=())
    prepared = worker.prepare("group", now=at())
    # daily 当段无新增记忆仍生成，只在 prompt 里注明
    assert prepared.due is True
    assert "本段没有新增记忆" in prepared.prompt


def test_prepare_every_n_without_new_memory_skips(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "every_n", "every_n": 2})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics, memories=())
    now = at()
    worker.record_turn("group", "t_1", at=now - timedelta(minutes=3))
    worker.record_turn("group", "t_2", at=now - timedelta(minutes=2))
    prepared = worker.prepare("group", now=now)
    assert prepared.due is False
    assert prepared.reason == "every_n_no_memory"
    assert prepared.turn_range == ("t_1", "t_2")

    calls: list[str] = []

    def llm(prompt: str) -> str:
        calls.append(prompt)
        return "不该被调用"

    report = worker.generate("group", llm, now=now)
    assert report.status == "skip"
    assert report.reason == "no_new_memory"
    assert calls == []
    assert metrics.count("diary.skip") == 1
    assert store.counts()["total"] == 0


def test_record_turn_caps_log_and_ignores_invalid_scope(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "every_n", "every_n": 5})
    store = make_store(cfg)
    worker = make_worker(cfg, store)
    now = at()
    for index in range(10005):
        worker.record_turn("group", "t_" + str(index), at=now)
    assert len(worker._turns["group"]) == 10000
    assert worker._turns["group"][0][0] == "t_5"
    before = len(worker._turns["group"])
    worker.record_turn("mixed", "bad", at=now)
    worker.record_turn("", "bad", at=now)
    assert len(worker._turns["group"]) == before
    assert len(worker._turns["dm"]) == 0


# ==========================================================================
# 6. DiaryWorker.finish：成功 / 隐私 / 截断 / cap / 幂等 / 降级
# ==========================================================================

def test_finish_writes_entry_and_metrics(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    now = at()
    worker = make_worker(cfg, store, metrics=metrics)
    worker.record_turn("group", "t_1", at=now - timedelta(minutes=5))
    worker.record_turn("group", "t_2", at=now - timedelta(minutes=4))
    prepared = worker.prepare("group", now=now)
    report = worker.finish(prepared, "今天群里聊到出差，我有些感慨。")
    assert report.status == "ok"
    assert report.entry_id == "d_0001"
    assert report.tokens > 0
    assert metrics.count("diary.ok") == 1
    saved = store.entries["d_0001"]
    assert saved.date == "2026-10-29"
    assert saved.scope_tag == "group"
    assert saved.turn_range == ("t_1", "t_2")
    assert saved.tokens == report.tokens
    assert saved.created_at.endswith("+08:00")
    # 重新加载确认落盘
    reloaded = make_store(cfg)
    reloaded.load()
    assert reloaded.entries["d_0001"].content == "今天群里聊到出差，我有些感慨。"


def test_finish_not_prepared_and_invalid_scope(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    worker = make_worker(cfg, store)
    assert worker.finish(DiaryPrompt("group", "", False, "2026-10-29", (), "not_due"), "x").status == "not_due"
    assert worker.finish(None, "x").status == "not_due"
    mixed = DiaryPrompt("mixed", "prompt", True, "2026-10-29", (), "force")
    report = worker.finish(mixed, "x")
    assert report.status == "skip"
    assert report.reason == "invalid_scope"
    assert store.counts()["total"] == 0


def test_finish_empty_raw_fails(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "")
    assert report.status == "fail"
    assert report.reason == "empty"
    assert store.counts()["total"] == 0
    assert metrics.count("diary.fail") == 1


def test_privacy_other_nickname_replaced(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    users = make_users(cfg, {"u_123456": "小明"})
    worker = make_worker(cfg, store, metrics=metrics, users=users)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "今天小明说他要出差。")
    assert report.status == "ok"
    content = store.entries[report.entry_id].content
    assert "有人" in content
    assert "小明" not in content
    assert metrics.count("diary.privacy") == 1


def test_privacy_bot_nickname_replaced_with_me(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily", "bot_nickname": "器魂"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "器魂今天有点累。")
    assert report.status == "ok"
    content = store.entries[report.entry_id].content
    assert content == "我今天有点累。"
    assert metrics.count("diary.privacy") == 1


def test_privacy_longest_nickname_first(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    users = make_users(cfg, {"u_1": "小明", "u_2": "小明子"})
    worker = make_worker(cfg, store, users=users)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "小明子说他要出差。")
    assert report.status == "ok"
    content = store.entries[report.entry_id].content
    # 最长优先：整段「小明子」→ 有人，而不是「有人子」
    assert content == "有人说他要出差。"
    assert "有人子" not in content


def test_privacy_user_id_rejected(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    users = make_users(cfg, {"u_123456": "小明"})
    worker = make_worker(cfg, store, metrics=metrics, users=users)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "这件事 u_123456 说过。")
    assert report.status == "privacy"
    assert store.counts()["total"] == 0
    assert metrics.count("diary.privacy") == 1


def test_privacy_unknown_user_id_literal_rejected(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    # 没有任何 users / affect 快照，也要拦住规范 §3.1 形态的 user_id 字面量
    worker = make_worker(cfg, store, metrics=metrics)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "今天 u_654321 说了一句奇怪的话。")
    assert report.status == "privacy"
    assert store.counts()["total"] == 0
    assert metrics.count("diary.privacy") == 1


def test_privacy_sentinel_and_fence_rejected(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    prepared = worker.prepare("group", now=at())
    fence = chr(96) * 3
    sentinel = worker.finish(prepared, "今天<MEMORY_JSON>{\"x\":1}</MEMORY_JSON>")
    assert sentinel.status == "privacy"
    fenced = worker.finish(prepared, "今天 " + fence + "python 代码" + fence + " 有点意思")
    assert fenced.status == "privacy"
    assert store.counts()["total"] == 0
    assert metrics.count("diary.privacy") == 2


def test_privacy_check_disabled_skips_replacement_but_rejects_fence(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        {"trigger": "daily", "privacy_check": False, "bot_nickname": "器魂"},
    )
    store = make_store(cfg)
    metrics = Metrics()
    users = make_users(cfg, {"u_123456": "小明"})
    worker = make_worker(cfg, store, metrics=metrics, users=users)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "今天小明和器魂聊天。")
    assert report.status == "ok"
    content = store.entries[report.entry_id].content
    # privacy_check=False：跳过昵称替换（不记 privacy）
    assert "小明" in content
    assert "器魂" in content
    assert metrics.count("diary.privacy") == 0
    # 但哨兵 / 围栏仍然拒写
    rejected = worker.finish(prepared, "今天 " + chr(96) * 3 + " 围栏")
    assert rejected.status == "privacy"
    assert store.counts()["total"] == 1


def test_finish_truncates_over_max_tokens(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily", "max_tokens": 10})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    prepared = worker.prepare("group", now=at())
    report = worker.finish(prepared, "字" * 100)
    assert report.status == "ok"
    assert report.tokens <= 10
    saved = store.entries[report.entry_id]
    assert saved.tokens == report.tokens
    assert len(saved.content) <= 10
    assert metrics.count("diary.truncated") == 1


def test_finish_cap_hit(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily", "max_per_scope": 1})
    store = make_store(cfg)
    metrics = Metrics()
    now = at()
    store.append(entry("d_0001", created_at=(now - timedelta(hours=1)).isoformat()))
    worker = make_worker(cfg, store, metrics=metrics)
    report = worker.generate("group", lambda prompt: "新的一篇日记", now=now, force=True)
    assert report.status == "cap_hit"
    assert store.counts()["total"] == 1
    assert metrics.count("diary.cap_hit") == 1


def test_finish_idempotent_by_turn_range(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    now = at()
    worker = make_worker(cfg, store, metrics=metrics)
    worker.record_turn("group", "t_1", at=now - timedelta(minutes=2))
    prepared = worker.prepare("group", now=now)
    assert worker.finish(prepared, "第一段日记").status == "ok"
    duplicate = worker.finish(prepared, "第二次生成")
    assert duplicate.status == "dup"
    assert store.counts()["total"] == 1
    assert metrics.count("diary.dup") == 1


def test_finish_daily_duplicate_even_with_new_turn_range(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    first = DiaryPrompt("group", "prompt", True, "2026-10-29", ("t_1", "t_2"), "daily")
    assert worker.finish(first, "当日第一篇", now=at()).status == "ok"
    # daily 防重：当日同 scope 已有日记 → 即使 turn_range 不同也 dup
    second = DiaryPrompt("group", "prompt", True, "2026-10-29", ("t_3", "t_4"), "daily")
    report = worker.finish(second, "当日第二篇", now=at(23, 59))
    assert report.status == "dup"
    assert store.counts()["total"] == 1
    assert metrics.count("diary.dup") == 1


# ==========================================================================
# 7. DiaryWorker.generate / run_due：重试与降级
# ==========================================================================

def test_generate_llm_failure_retries_once_then_fail(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    calls: list[str] = []

    def llm(prompt: str) -> str:
        calls.append(prompt)
        raise RuntimeError("llm down")

    report = worker.generate("group", llm, now=at())
    assert report.status == "fail"
    assert report.reason == "llm"
    assert len(calls) == 2  # 失败重试 ≤1
    assert store.counts()["total"] == 0
    assert metrics.count("diary.fail") >= 1


def test_generate_llm_succeeds_after_retry(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    worker = make_worker(cfg, store)
    calls: list[str] = []

    def llm(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("第一次失败")
        return "第一次失败后成功的日记。"

    report = worker.generate("group", llm, now=at())
    assert report.status == "ok"
    assert len(calls) == 2
    assert store.counts()["total"] == 1


def test_generate_llm_missing_fails_without_raising(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)
    report = worker.generate("group", None, now=at())
    assert report.status == "fail"
    assert report.reason == "llm"
    assert store.counts()["total"] == 0
    assert metrics.count("diary.fail") >= 1


def test_generate_write_failure_swallowed(tmp_path: Path, monkeypatch) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    worker = make_worker(cfg, store, metrics=metrics)

    def boom(item) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "append", boom)
    report = worker.generate("group", lambda prompt: "写盘会失败的日记", now=at())
    assert report.status == "fail"
    assert report.reason == "write"
    assert metrics.count("diary.fail") >= 1
    assert metrics.count("diary.ok") == 0


def test_run_due_generates_one_daily_per_scope(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    worker = make_worker(cfg, store)
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return "今天的心情记录。"

    reports = worker.run_due(llm, now=at())
    assert [report.scope_tag for report in reports] == ["group", "dm"]
    assert [report.status for report in reports] == ["ok", "ok"]
    assert len(prompts) == 2
    assert store.counts() == {"total": 2, "group": 1, "dm": 1}
    # 同一时间再次 run_due：两个 scope 都不再 due
    assert worker.run_due(llm, now=at(23, 56)) == []


def test_run_due_every_n_scope_independent(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "every_n", "every_n": 2})
    store = make_store(cfg)
    memories = [mem("用户A说明天要出差", created_at="2026-10-29T10:00:00+08:00")]
    worker = make_worker(cfg, store, memories=memories)
    now = at()
    worker.record_turn("group", "t_1", at=now - timedelta(minutes=3))
    worker.record_turn("group", "t_2", at=now - timedelta(minutes=2))
    reports = worker.run_due(lambda prompt: "群里的事情让我有点感触。", now=now)
    assert len(reports) == 1
    assert reports[0].scope_tag == "group"
    assert reports[0].status == "ok"
    assert store.counts() == {"total": 1, "group": 1, "dm": 0}


def test_metrics_event_names_are_diary_only(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, {"trigger": "daily"})
    store = make_store(cfg)
    metrics = Metrics()
    users = make_users(cfg, {"u_123456": "小明"})
    worker = make_worker(cfg, store, metrics=metrics, users=users)
    worker.run_due(lambda prompt: "小明今天说的那件事让我想了很久。", now=at())
    select_excerpts(store, "group", now=at(), metrics=metrics)
    counters = metrics.counters()
    assert counters
    assert all(name.startswith("diary.") for name in counters)
    assert "diary.ok" in counters
    assert "diary.inject" in counters
    assert "diary.privacy" in counters
