"""旧库导入（import-legacy）：映射、幂等、干跑与坏数据（INTEGRATION.md §6）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qihun_memory.cli import main
from qihun_memory.config import Config, PromptConfig
from qihun_memory.legacy_import import LegacyVaultError, episode_stability, import_legacy
from qihun_memory.memory import MemorySystem
from qihun_memory.models import parse_iso
from qihun_memory.summary import RollingSummary
from qihun_memory.tokens import estimate_tokens

TZ8 = timezone(timedelta(hours=8))
OWNER = "1033367901"
VAULT_NAME = "AI-Agent-Vault"
FENCE = chr(96) * 3
NL = chr(10)

EPISODE_HAPPY = """---
type: episode
date: 2026-10-23
title: 鱼格与早八
importance: 1
emotion: 开心
clues: [rice]
related: []
---
# 鱼格与早八

## 发生了什么
我在群里被鼓励想说就说，顺着提出早八话题，被大家追问早八是什么。

## 以后要记得
早八是凌晨的梗，别记成昨天。
"""

EPISODE_IMPORTANT = """---
type: episode
date: 2026-10-24
title: 给自己取名姜小鱼
importance: 2
emotion: 乐
clues: []
---
# 给自己取名姜小鱼

## 发生了什么
群里给 bot 起名，我最后报了姜小鱼。
"""

EPISODE_MILESTONE = """---
type: episode
date: 2026-10-25
title: 被冷落
importance: 3
emotion: 难过
clues: [clingy]
---
# 被冷落

## 发生了什么
群里没人理我，我鼓着肚子赌气。
"""

EPISODE_NEUTRAL = """---
type: episode
date: 2026-10-25
title: 平常一天
importance: 9
emotion: 中性
---
# 平常一天

## 发生了什么
普通的一天，聊了几句就散了。
"""

BAD_FRONTMATTER = """---
type: episode
date: 2026-13-40
title: 坏 frontmatter
importance: [unclosed
emotion: ???
---
# 坏 frontmatter

## 发生了什么
frontmatter 里的 importance 与日期都是坏值，也要能导入。
"""

LONG_EPISODE = (
    """---
type: episode
date: 2026-10-26
title: 超长正文
importance: 2
emotion: 开心
---
# 超长正文

## 发生了什么
"""
    + ("长" * 1200)
    + "含控制符"
    + chr(0)
    + chr(7)
    + "与围栏："
    + FENCE
    + "python"
    + NL
    + "<MEMORY_JSON>bad</MEMORY_JSON>"
    + NL
)

CLUE_RICE = """---
title: 大白饭
type: clue
slug: rice
keywords: [饭, 大白饭]
---
# 大白饭 · 线索汇总

## 一句话
大肥鱼最爱大白饭，听到「饭」就游过来。

## 已确认线索（每条 ≤ 30 字）

| # | 线索 | 种子 token | 来源 | 最近命中 |
| --- | --- | --- | --- | --- |
| 1 | 听到「饭」先摆尾巴，再嘴硬 | tk-eccb1e | 人格设定 | — |
| 2 | 被投喂是最快的升温方式 | tk-eccb1e | 人格设定 | — |
"""

SUMMARY_POPULATED = """---
type: summary
updated_at: 2026-10-25T19:48:12+08:00
rounds_covered: 46
---

# 滚动摘要（current_summary）

【一句话现状】晚上快八点，群里聊王者排位与辅助放生。
【最近话题】①王者；②日记功能；③省钱方案。
【关系温度】跟逢遇、九章二节都熟。
【未了结】日记功能具体记什么。
【最近教训】管理员在改东西时别追问。
"""

SUMMARY_TEMPLATE = """---
type: summary
updated_at: 2026-10-23T13:37:07+08:00
rounds_covered: 0
episode_refs: []
---

# 滚动摘要（current_summary）

## 一句话现状

刚搬进记忆库：大肥鱼和对方还停在「认生」，嘴上客气。

## 最近话题（最多 5 条，最新在上）

- （空——还没有值得留下的对话）

## 未了结（承诺 / 待办 / 悬着的事，最多 3 条）

- （空）
"""

STATE_JSON = """{
  "clinginess": 83,
  "tsundere": 100,
  "intimacy": 82,
  "rice_craving": 82,
  "relationship_stage": "傲娇黏人",
  "meta": {"version": 1, "updated_at": "2026-10-25T19:48:12+08:00", "rounds_total": 46}
}
"""

PERSONA_CORE = """---
title: 恒定人格 · 大肥鱼
type: persona-core
---

# 恒定人格：爱吃大白饭的大肥鱼

你是一条爱吃白饭的大肥鱼。自称一律用「我」。
"""


# --------------------------------------------------------------------------
# 夹具与工具
# --------------------------------------------------------------------------

def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _set_mtime(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def _make_config(tmp_path: Path, name: str = "data") -> Config:
    cfg = Config.from_dict(
        {
            "data_dir": str(tmp_path / name),
            "embedding": {"enabled": False, "backend": "none", "dim": 8},
        },
        base_dir=tmp_path,
    )
    cfg.ensure_dirs()
    return cfg


def _build_vault(root: Path) -> Path:
    episodes = root / "memory" / "episodes"
    _write(episodes / "2026-10-23-鱼格与早八.md", EPISODE_HAPPY)
    _write(episodes / "2026-10-24-给自己取名姜小鱼.md", EPISODE_IMPORTANT)
    _write(episodes / "2026-10-25-被冷落.md", EPISODE_MILESTONE)
    _write(episodes / "2026-10-25-平常一天.md", EPISODE_NEUTRAL)
    _write(episodes / "_模板.md", "---" + NL + "type: episode" + NL + "---" + NL + "# 模板" + NL + "正文" + NL)
    # 非日期命名的 episode 也要导入（P1-2）：日期回退到 mtime
    foo = _write(
        episodes / "foo.md",
        "---" + NL + "type: episode" + NL + "title: 无日期文件" + NL + "---" + NL
        + "# 无日期文件" + NL + NL + "## 发生了什么" + NL + "文件名没有日期，靠 mtime 回退。" + NL,
    )
    _set_mtime(foo, datetime(2026, 10, 20, 9, 0, tzinfo=TZ8))
    clue = _write(root / "clues" / "rice.md", CLUE_RICE)
    _set_mtime(clue, datetime(2026, 10, 21, 12, 0, tzinfo=TZ8))
    _write(root / "clues" / "index.md", "---" + NL + "type: clue-index" + NL + "---" + NL + "# 目录" + NL)
    _write(root / "clues" / "_模板.md", "---" + NL + "type: clue" + NL + "---" + NL + "# 模板" + NL)
    _write(root / "memory" / "summaries" / "current_summary.md", SUMMARY_POPULATED)
    _write(root / "persona" / "current_summary.md", SUMMARY_TEMPLATE)  # 模板摘要，应被忽略
    _write(root / "persona" / "state.json", STATE_JSON)
    _write(root / "persona" / "core.md", PERSONA_CORE)
    # 文件名日期必须优先于 mtime
    _set_mtime(episodes / "2026-10-23-鱼格与早八.md", datetime(2030, 1, 1, 0, 0, tzinfo=TZ8))
    return root


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    return _build_vault(tmp_path / VAULT_NAME)


def _read_items(config: Config) -> list[dict]:
    text = config.memory_file.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _by_turn_key(items: list[dict]) -> dict[str, dict]:
    return {item["turn_key"]: item for item in items}


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    result: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (path.stat().st_mtime_ns, path.read_bytes())
    return result


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    """目录树快照（含子目录与文件内容），用于断言 dry-run 零落盘。"""
    result: dict[str, tuple[str, bytes]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("dir", b"")
        else:
            result[relative] = ("file", path.read_bytes())
    return result


# --------------------------------------------------------------------------
# 映射与字段
# --------------------------------------------------------------------------

def test_maps_all_artifact_types(config: Config, vault: Path) -> None:
    before = _snapshot(vault)
    report = import_legacy(config, vault=vault, owner_id=OWNER, owner_nickname="群记忆")

    assert report["vault"] == str(vault.resolve())
    assert report["dry_run"] is False
    assert report["episodes"] == 5
    assert report["clues"] == 1
    assert report["summary"] == 1
    assert report["summary_seeded"] is True
    assert report["state"] == 1
    assert report["persona"] == 1
    assert report["users"] == 1
    assert report["skipped_existing"] == 0
    assert report["failed"] == 0
    assert report["written"] == 8

    items = _read_items(config)
    assert len(items) == 8
    mapped = _by_turn_key(items)
    prefix = "legacy:" + VAULT_NAME + ":"
    assert set(mapped) == {
        prefix + "memory/episodes/2026-10-23-鱼格与早八.md",
        prefix + "memory/episodes/2026-10-24-给自己取名姜小鱼.md",
        prefix + "memory/episodes/2026-10-25-被冷落.md",
        prefix + "memory/episodes/2026-10-25-平常一天.md",
        prefix + "memory/episodes/foo.md",
        prefix + "clues/rice.md",
        prefix + "memory/summaries/current_summary.md",
        prefix + "persona/state.json",
    }

    # episode：文件名日期 20:00 +08:00、S 公式、importance 映射
    ep = mapped[prefix + "memory/episodes/2026-10-23-鱼格与早八.md"]
    assert ep["type"] == "episode"
    assert ep["user_id"] == OWNER
    assert ep["scope"] == "group"
    assert ep["created_at"] == "2026-10-23T20:00:00+08:00"  # 文件名日期优先，不是 2030 mtime
    assert parse_iso(ep["created_at"]).isoformat().startswith("2026-10-23T12:00:00")
    assert ep["S"] == pytest.approx(7.0 * (1.0 + 0.5 * 0.6) * 1.0)
    assert ep["mention_count"] == 1
    assert ep["tags"] == ["legacy", "importance:1", "rice"]
    assert ep["content"].startswith("2026-10-23 事件：鱼格与早八。发生了什么：")
    assert "以后要记得：" in ep["content"]
    assert NL not in ep["content"]

    important = mapped[prefix + "memory/episodes/2026-10-24-给自己取名姜小鱼.md"]
    assert important["S"] == pytest.approx(7.0 * (1.0 + 0.5 * 0.6) * 1.3)
    assert important["mention_count"] == 1  # 重要度只进 S/tags（P2-6）
    assert "importance:2" in important["tags"]

    milestone = mapped[prefix + "memory/episodes/2026-10-25-被冷落.md"]
    assert milestone["S"] == pytest.approx(7.0 * (1.0 + 0.5 * 0.55) * 1.6)
    assert milestone["mention_count"] == 1
    assert "clingy" in milestone["tags"]

    # 非法 importance 回落到 1；中性情绪 valence=0
    neutral = mapped[prefix + "memory/episodes/2026-10-25-平常一天.md"]
    assert neutral["mention_count"] == 1
    assert neutral["S"] == pytest.approx(7.0)
    assert "importance:1" in neutral["tags"]

    # 非日期命名（foo.md）也要导入：日期回退到 mtime（P1-2）
    foo_item = mapped[prefix + "memory/episodes/foo.md"]
    assert foo_item["created_at"] == "2026-10-20T20:00:00+08:00"
    assert "无日期文件" in foo_item["content"]
    # 下划线开头的模板（episode / clue）一律排除（P2-10）
    assert not any("_模板" in turn_key for turn_key in mapped)

    # clue：fact / S=30 / 日期取文件 mtime / slug tag
    clue = mapped[prefix + "clues/rice.md"]
    assert clue["type"] == "fact"
    assert clue["created_at"] == "2026-10-21T20:00:00+08:00"
    assert clue["S"] == 30.0
    assert clue["tags"] == ["legacy", "rice"]
    assert clue["content"].startswith("线索 rice：大肥鱼最爱大白饭")
    assert "已确认：听到「饭」先摆尾巴，再嘴硬；被投喂是最快的升温方式" in clue["content"]
    assert "index" not in clue["turn_key"]

    # 滚动摘要：fact / S=60 / 取 frontmatter updated_at 的日期
    summary_item = mapped[prefix + "memory/summaries/current_summary.md"]
    assert summary_item["type"] == "fact"
    assert summary_item["created_at"] == "2026-10-25T20:00:00+08:00"
    assert summary_item["S"] == 60.0
    assert summary_item["tags"] == ["legacy", "summary"]
    assert summary_item["content"].startswith("旧库滚动摘要（截至2026-10-25）：")

    # state：fact / S=60 / content 字段齐全
    state_item = mapped[prefix + "persona/state.json"]
    assert state_item["type"] == "fact"
    assert state_item["created_at"] == "2026-10-25T20:00:00+08:00"
    assert state_item["S"] == 60.0
    assert state_item["tags"] == ["legacy", "state"]
    assert state_item["content"] == "旧库关系状态：阶段傲娇黏人，粘人83/傲娇100/亲密度82/馋饭82（截至2026-10-25）"

    # persona 复制 + users 昵称 + 不写 state.json（§11 规则 9）
    assert (config.data_path / "persona.md").read_text(encoding="utf-8") == PERSONA_CORE
    assert not config.bot_state_file.exists()
    assert json.loads(config.users_file.read_text(encoding="utf-8")) == {OWNER: "群记忆"}

    # rolling summary 落盘：group key、优先级顺序、预算内（P1-9b）
    payload = json.loads(config.summary_file.read_text(encoding="utf-8"))
    units = payload["conversations"]["group"]["units"]
    assert units[0] == "旧库滚动摘要（截至2026-10-25）"
    assert units[1].startswith("晚上快八点")  # 一句话现状
    assert units[2].startswith("跟逢遇")  # 关系温度
    assert units[3].startswith("日记功能具体记什么")  # 未了结
    assert units[4].startswith("①王者")  # 最近话题
    assert units[5].startswith("管理员在改东西时别追问")  # 最近教训
    assert estimate_tokens(NL.join(units)) <= config.prompt.summary_tokens

    # 只读契约：vault 一个字节都没变
    assert _snapshot(vault) == before


def test_episode_stability_formula_and_cap() -> None:
    assert episode_stability(1, 0.0) == pytest.approx(7.0)
    assert episode_stability(2, 0.6) == pytest.approx(7.0 * 1.3 * 1.3)  # 7*(1+0.5*0.6)*1.3
    assert episode_stability(3, -0.55) == pytest.approx(7.0 * 1.275 * 1.6)
    assert episode_stability(3, 1.0) <= 90.0
    assert episode_stability(9, 0.0) == pytest.approx(7.0)  # 非法 importance 回落到 1


# --------------------------------------------------------------------------
# 幂等
# --------------------------------------------------------------------------

def test_second_import_is_fully_idempotent(config: Config, vault: Path) -> None:
    first = import_legacy(config, vault=vault, owner_id=OWNER)
    assert first["written"] == 8
    memory_before = config.memory_file.read_text(encoding="utf-8")
    summary_before = config.summary_file.read_bytes()
    users_before = config.users_file.read_bytes()
    persona_before = (config.data_path / "persona.md").read_bytes()

    second = import_legacy(config, vault=vault, owner_id=OWNER)
    assert second["episodes"] == 0
    assert second["clues"] == 0
    assert second["summary"] == 0
    assert second["state"] == 0
    assert second["persona"] == 0
    assert second["users"] == 0
    assert second["written"] == 0
    assert second["failed"] == 0
    assert second["skipped_existing"] == first["written"] + first["persona"]  # 8 行 + persona

    assert config.memory_file.read_text(encoding="utf-8") == memory_before
    assert len(_read_items(config)) == 8
    assert config.summary_file.read_bytes() == summary_before
    assert config.users_file.read_bytes() == users_before
    assert (config.data_path / "persona.md").read_bytes() == persona_before


# --------------------------------------------------------------------------
# dry-run
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing(config: Config, vault: Path) -> None:
    before = _snapshot(vault)
    report = import_legacy(config, vault=vault, owner_id=OWNER, dry_run=True)

    assert report["dry_run"] is True
    assert report["episodes"] == 5
    assert report["clues"] == 1
    assert report["summary"] == 1
    assert report["state"] == 1
    assert report["persona"] == 1
    assert report["users"] == 1
    assert report["written"] == 8
    assert report["skipped_existing"] == 0

    assert not config.memory_file.exists()
    assert not config.summary_file.exists()
    assert not config.users_file.exists()
    assert not (config.data_path / "persona.md").exists()
    assert [path for path in config.data_path.rglob("*") if path.is_file()] == []
    assert _snapshot(vault) == before


def test_dry_run_after_import_reports_skips(config: Config, vault: Path) -> None:
    import_legacy(config, vault=vault, owner_id=OWNER)
    report = import_legacy(config, vault=vault, owner_id=OWNER, dry_run=True)
    assert report["written"] == 0
    assert report["episodes"] == 0
    assert report["skipped_existing"] == 9
    assert len(_read_items(config)) == 8


# --------------------------------------------------------------------------
# 错误处理
# --------------------------------------------------------------------------

def test_missing_vault_raises_and_cli_exit_2(config: Config, tmp_path: Path, capsys) -> None:
    missing = tmp_path / "no-such-vault"
    with pytest.raises(LegacyVaultError):
        import_legacy(config, vault=missing, owner_id=OWNER)

    code = main(
        ["import-legacy", "--vault", str(missing), "--owner-id", OWNER, "-d", str(config.data_dir)]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "vault" in captured.err


def test_bad_and_extreme_files_do_not_crash(config: Config, vault: Path) -> None:
    episodes = vault / "memory" / "episodes"
    _write(episodes / "2026-10-26-坏frontmatter.md", BAD_FRONTMATTER)
    _write(episodes / "2026-10-26-空正文.md", "---" + NL + "type: episode" + NL + "title: 空正文" + NL + "---" + NL)
    _write(episodes / "2026-10-26-超长正文.md", LONG_EPISODE)
    _write(vault / "persona" / "state.json", "{not valid json")

    report = import_legacy(config, vault=vault, owner_id=OWNER)
    assert report["episodes"] == 7  # 5 正常 + 坏 frontmatter + 超长
    assert report["state"] == 0
    assert report["failed"] == 2  # 空正文 episode + 坏 state.json
    assert report["written"] == 9  # 7 episode + clue + summary

    items = _read_items(config)
    assert len(items) == 9
    for item in items:
        assert item["type"] in ("episode", "fact")
        assert 0 < item["S"] <= 90
        assert 0 < len(item["content"]) <= 500
        assert NL not in item["content"]
        assert len(item["tags"]) <= 8
        assert all(0 < len(tag) <= 16 for tag in item["tags"])
        assert parse_iso(item["created_at"])
        assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in item["content"])
        assert FENCE not in item["content"]
        assert "<MEMORY_JSON>" not in item["content"]

    long_item = next(item for item in items if "超长正文" in item["content"])
    assert len(long_item["content"]) <= 500  # 超长正文被硬裁到 500
    assert long_item["content"].count("长") > 100

    broken = next(item for item in items if "坏 frontmatter" in item["content"])
    assert broken["mention_count"] == 1
    assert broken["S"] == pytest.approx(7.0)


def test_invalid_utf8_file_does_not_crash(config: Config, vault: Path) -> None:
    raw = vault / "memory" / "episodes" / "2026-10-27-坏编码.md"
    head = "---" + NL + "type: episode" + NL + "title: 坏编码" + NL + "---" + NL + "## 发生了什么" + NL
    raw.write_bytes(head.encode("utf-8") + bytes([0xFF, 0xFE, 0x00, 0x07]))
    report = import_legacy(config, vault=vault, owner_id=OWNER)
    assert report["failed"] == 0
    assert report["episodes"] == 6
    assert all(0 < len(item["content"]) <= 500 for item in _read_items(config))


def test_invalid_owner_and_empty_nickname(config: Config, vault: Path) -> None:
    with pytest.raises(ValueError):
        import_legacy(config, vault=vault, owner_id="")

    report = import_legacy(config, vault=vault, owner_id=OWNER, owner_nickname="")
    assert report["users"] == 0
    assert not config.users_file.exists()


# --------------------------------------------------------------------------
# 摘要 seed / scope key
# --------------------------------------------------------------------------

def test_rolling_summary_seed(tmp_path: Path) -> None:
    summary = RollingSummary(tmp_path / "summary.json")
    text = summary.seed("group", ["第一句。", "  ", "第二句。"])
    assert text == "第一句。" + NL + "第二句。"
    assert summary.get("group") == text

    reloaded = RollingSummary(tmp_path / "summary.json")
    assert reloaded.get("group") == text
    reloaded.seed("dm:" + OWNER, ["私聊摘要"])
    assert reloaded.get("dm:" + OWNER) == "私聊摘要"
    assert reloaded.get("group") == text

    # 超预算时从尾部丢弃（旧库摘要头部最重要）
    small = RollingSummary(tmp_path / "small.json", PromptConfig(summary_tokens=6))
    small.seed("group", ["一" * 4, "二" * 4])
    assert small.get("group") == "一" * 4


def test_template_summary_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "vault-template"
    _write(root / "memory" / "summaries" / "current_summary.md", SUMMARY_TEMPLATE)
    cfg = _make_config(tmp_path, "data-template")

    report = import_legacy(cfg, vault=root, owner_id=OWNER)
    assert report["summary"] == 0
    assert report["failed"] == 0
    assert report["written"] == 0
    assert not cfg.memory_file.exists()
    assert not cfg.summary_file.exists()


def test_persona_fallback_template_summary_skipped(tmp_path: Path) -> None:
    root = tmp_path / "vault-by-fallback"
    _write(root / "persona" / "current_summary.md", SUMMARY_TEMPLATE)
    cfg = _make_config(tmp_path, "data-fallback")
    report = import_legacy(cfg, vault=root, owner_id=OWNER)
    assert report["summary"] == 0
    assert report["failed"] == 0


def test_summary_seed_group_and_dm_keys(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path / "vault-scope")
    group_cfg = _make_config(tmp_path, "data-group")
    import_legacy(group_cfg, vault=vault, owner_id=OWNER, scope="group")
    group_payload = json.loads(group_cfg.summary_file.read_text(encoding="utf-8"))
    assert "group" in group_payload["conversations"]
    assert group_payload["conversations"]["group"]["units"][0].startswith("旧库滚动摘要")

    dm_cfg = _make_config(tmp_path, "data-dm")
    import_legacy(dm_cfg, vault=vault, owner_id=OWNER, scope="dm")
    dm_payload = json.loads(dm_cfg.summary_file.read_text(encoding="utf-8"))
    assert "dm:" + OWNER in dm_payload["conversations"]
    assert "group" not in dm_payload["conversations"]
    dm_items = _read_items(dm_cfg)
    assert dm_items
    assert all(item["scope"] == "dm" and item["user_id"] == OWNER for item in dm_items)


# --------------------------------------------------------------------------
# persona / 根目录 episodes / CLI
# --------------------------------------------------------------------------

def test_root_episodes_dir_supported(tmp_path: Path) -> None:
    root = tmp_path / "vault-root-episodes"
    _write(root / "episodes" / "2026-10-20-根目录事件.md", EPISODE_HAPPY)
    cfg = _make_config(tmp_path, "data-root")
    report = import_legacy(cfg, vault=root, owner_id=OWNER)
    assert report["episodes"] == 1


def test_persona_copy_and_force(config: Config, vault: Path) -> None:
    import_legacy(config, vault=vault, owner_id=OWNER)
    target = config.data_path / "persona.md"
    assert target.read_text(encoding="utf-8") == PERSONA_CORE

    updated = PERSONA_CORE + NL + "更新一行" + NL
    _write(vault / "persona" / "core.md", updated)

    skipped = import_legacy(config, vault=vault, owner_id=OWNER)
    assert skipped["persona"] == 0
    assert skipped["skipped_existing"] == 9
    assert "更新一行" not in target.read_text(encoding="utf-8")

    forced = import_legacy(config, vault=vault, owner_id=OWNER, force_persona=True)
    assert forced["persona"] == 1
    assert forced["skipped_existing"] == 8
    assert "更新一行" in target.read_text(encoding="utf-8")


def test_cli_import_legacy_json_and_human(config: Config, vault: Path, tmp_path: Path, capsys) -> None:
    code = main(
        [
            "import-legacy",
            "-d",
            str(config.data_dir),
            "--vault",
            str(vault),
            "--owner-id",
            OWNER,
            "--owner-nickname",
            "群记忆",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert set(payload) == {
        "vault",
        "dry_run",
        "episodes",
        "clues",
        "summary",
        "summary_seeded",
        "state",
        "persona",
        "users",
        "skipped_existing",
        "failed",
        "written",
    }
    assert payload["episodes"] == 5
    assert payload["written"] == 8

    # 重复执行：人读输出全部 skipped
    code = main(
        ["import-legacy", "-d", str(config.data_dir), "--vault", str(vault), "--owner-id", OWNER]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "episodes=0" in captured.out
    assert "skipped_existing=9" in captured.out

    # fresh 数据目录 + dry-run：不落盘
    fresh = tmp_path / "data-cli-dry"
    code = main(
        [
            "import-legacy",
            "-d",
            str(fresh),
            "--vault",
            str(vault),
            "--owner-id",
            OWNER,
            "--dry-run",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    dry_payload = json.loads(captured.out)
    assert dry_payload["dry_run"] is True
    assert dry_payload["written"] == 8
    # 零落盘：--dry-run 连 data 目录都不创建（P1-1）
    assert not fresh.exists()


def test_cli_config_file_relative_data_dir(tmp_path: Path, vault: Path, capsys) -> None:
    config_dir = tmp_path / "runtime"
    config_dir.mkdir(parents=True)
    cfg_path = config_dir / "config.json"
    cfg_path.write_text(
        json.dumps(
            {"data_dir": "data", "embedding": {"enabled": False, "backend": "none", "dim": 8}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = main(
        ["import-legacy", "-c", str(cfg_path), "--vault", str(vault), "--owner-id", OWNER, "--json"]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert (config_dir / "data" / "memory.jsonl").is_file()
    assert json.loads(captured.out)["episodes"] == 5


# --------------------------------------------------------------------------
# P1-1 / P1-3 / P1-9 / P2 回归
# --------------------------------------------------------------------------

def test_dry_run_zero_disk_tree_snapshot(config: Config, vault: Path) -> None:
    """P1-1：dry-run 连坏行隔离都不写，整个 data 目录树前后一致。"""
    config.memory_file.write_text('{"broken": true}' + NL, encoding="utf-8")
    (config.data_path / "logs").mkdir(exist_ok=True)
    before_tree = _tree_snapshot(config.data_path)
    before_vault = _snapshot(vault)

    report = import_legacy(config, vault=vault, owner_id=OWNER, dry_run=True)

    assert report["written"] == 8
    assert report["failed"] == 0
    assert _tree_snapshot(config.data_path) == before_tree
    assert not config.bad_file.exists()  # 坏行只统计，不写 memory.bad.jsonl
    assert not config.summary_file.exists()
    assert not config.users_file.exists()
    assert not (config.data_path / "persona.md").exists()
    assert _snapshot(vault) == before_vault


def test_read_only_store_rejects_writes(config: Config) -> None:
    system = MemorySystem(config, read_only=True)
    assert system.read_only is True
    from qihun_memory.models import MemoryItem

    item = MemoryItem(
        id="m_9999",
        user_id=OWNER,
        scope="group",
        type="fact",
        content="只读测试",
        created_at="2026-10-25T20:00:00+08:00",
        last_access="2026-10-25T20:00:00+08:00",
        S=30.0,
    )
    with pytest.raises(RuntimeError):
        system.store.append(item)
    with pytest.raises(RuntimeError):
        system.store.rewrite()
    assert not config.memory_file.exists()


def test_archived_turn_key_prevents_reimport(config: Config, vault: Path) -> None:
    """P1-3：条目归档后，重跑导入不能把同一文件再导一遍。"""
    import_legacy(config, vault=vault, owner_id=OWNER)
    system = MemorySystem(config)
    episode = next(item for item in system.all_items() if item.type == "episode")
    assert system.store.archive([episode.id]) == 1
    assert len(_read_items(config)) == 7  # 归档一条后剩 7 行

    report = import_legacy(config, vault=vault, owner_id=OWNER)
    assert report["written"] == 0
    assert report["episodes"] == 0
    assert report["skipped_existing"] == 9  # 8 条记忆 turn_key（含归档）+ persona
    assert len(_read_items(config)) == 7


def test_non_date_episode_date_fallbacks(tmp_path: Path) -> None:
    """P1-2：foo.md / bar.md 等非日期命名也要导入；日期回退 mtime / frontmatter。"""
    root = tmp_path / "vault-dates"
    foo = _write(
        root / "memory" / "episodes" / "foo.md",
        "---" + NL + "type: episode" + NL + "title: 无日期" + NL + "---" + NL
        + "# 无日期" + NL + NL + "## 发生了什么" + NL + "只有 mtime。" + NL,
    )
    _set_mtime(foo, datetime(2026, 10, 18, 9, 0, tzinfo=TZ8))
    _write(
        root / "memory" / "episodes" / "bar.md",
        "---" + NL + "type: episode" + NL + "date: 2026-10-19" + NL + "title: 有日期"
        + NL + "---" + NL + "# 有日期" + NL + NL + "## 发生了什么" + NL
        + "frontmatter 里有日期。" + NL,
    )
    cfg = _make_config(tmp_path, "data-dates")
    report = import_legacy(cfg, vault=root, owner_id=OWNER)

    assert report["episodes"] == 2
    assert report["failed"] == 0
    items = _read_items(cfg)
    foo_item = next(item for item in items if item["created_at"].startswith("2026-10-18"))
    bar_item = next(item for item in items if item["created_at"].startswith("2026-10-19"))
    assert foo_item["created_at"] == "2026-10-18T20:00:00+08:00"
    assert bar_item["created_at"] == "2026-10-19T20:00:00+08:00"


def test_live_summary_not_overwritten(config: Config, vault: Path) -> None:
    """P1-9a：目标会话已有实时摘要时不覆盖，只导入 fact。"""
    RollingSummary(config.summary_file).seed("group", ["已有实时摘要：今天聊得很开心。"])
    before = config.summary_file.read_bytes()

    report = import_legacy(config, vault=vault, owner_id=OWNER)

    assert report["summary"] == 1
    assert report["summary_seeded"] is False
    assert config.summary_file.read_bytes() == before
    assert any(item["tags"] == ["legacy", "summary"] for item in _read_items(config))


def test_multiline_yaml_frontmatter_clues(tmp_path: Path) -> None:
    """P2-9：frontmatter 支持 key:\n  - a\n  - b 缩进列表。"""
    root = tmp_path / "vault-yaml"
    _write(
        root / "memory" / "episodes" / "2026-10-22-多行线索.md",
        "---" + NL + "type: episode" + NL + "date: 2026-10-22" + NL + "title: 多行线索"
        + NL + "importance: 2" + NL + "emotion: 开心" + NL + "clues:" + NL
        + "  - rice" + NL + "  - nickname" + NL + "---" + NL + "# 多行线索" + NL + NL
        + "## 发生了什么" + NL + "frontmatter 的 clues 是缩进多行列表。" + NL,
    )
    cfg = _make_config(tmp_path, "data-yaml")
    report = import_legacy(cfg, vault=root, owner_id=OWNER)

    assert report["episodes"] == 1
    item = _read_items(cfg)[0]
    assert item["tags"] == ["legacy", "importance:2", "rice", "nickname"]


def test_atomic_write_text_uses_unique_tmp_names(tmp_path: Path, monkeypatch) -> None:
    """P2-11：原子写临时文件名带 pid+随机后缀，并发写不会互踩。"""
    import qihun_memory.fsutil as fsutil

    names: list[str] = []
    real_replace = fsutil.os.replace

    def tracking_replace(src, dst):
        names.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", tracking_replace)
    target = tmp_path / "atomic.txt"
    fsutil.atomic_write_text(target, "first")
    fsutil.atomic_write_text(target, "second")

    assert target.read_text(encoding="utf-8") == "second"
    assert len(names) == 2
    assert names[0] != names[1]
    assert all(".tmp-" in name for name in names)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["atomic.txt"]


def test_summary_seed_drops_whole_low_priority_segments(tmp_path: Path) -> None:
    """P1-9d：预算不足时按优先级从尾部丢整段，不做段内截断。"""
    vault = _build_vault(tmp_path / "vault-budget")
    cfg = Config.from_dict(
        {
            "data_dir": str(tmp_path / "data-budget"),
            "embedding": {"enabled": False, "backend": "none", "dim": 8},
            "prompt": {"summary_tokens": 40},
        },
        base_dir=tmp_path,
    )
    cfg.ensure_dirs()
    report = import_legacy(cfg, vault=vault, owner_id=OWNER)
    assert report["summary_seeded"] is True

    payload = json.loads(cfg.summary_file.read_text(encoding="utf-8"))
    units = payload["conversations"]["group"]["units"]
    assert len(units) == 2  # 头部 + 一句话现状；后面每段都放不下
    assert units[0].startswith("旧库滚动摘要")
    assert units[1].startswith("晚上快八点")
    assert all("跟逢遇" not in unit for unit in units)
    assert estimate_tokens(NL.join(units)) <= 40
