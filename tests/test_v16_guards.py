"""v1.6-rc4 叙事层架构守卫（附录 C D7.1~D7.5 / 硬规则 19/20）。

与 test_v15_guards.py（事实层单点守卫）互补：这里只守叙事层边界，防止后续重构把
日记接进检索、S/R、写回或每轮 LLM 调用。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from qihun_memory.config import Config
from qihun_memory.diary import DiaryEntry, DiaryStore
from qihun_memory.memory import MemorySystem
from qihun_memory.pipeline import ContextViolation, TurnEvent, TurnPipeline

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "qihun_memory"


def _module_imports(filename: str) -> set[str]:
    tree = ast.parse((SRC_DIR / filename).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _source(filename: str) -> str:
    return (SRC_DIR / filename).read_text(encoding="utf-8")


def make_config(tmp_path) -> Config:
    cfg = Config.from_dict(
        {
            "data_dir": str(tmp_path / "data"),
            "embedding": {"enabled": False, "backend": "none", "dim": 8},
        },
        base_dir=tmp_path,
    )
    cfg.ensure_dirs()
    return cfg


def event(message: str = "我下周三去上海出差", **overrides) -> TurnEvent:
    base: dict = {"user_id": "u_1", "scope": "group", "message": message, "msgid": "msg-1"}
    base.update(overrides)
    return TurnEvent(**base)


def raw_output(answer: str = "好的。") -> str:
    return answer


# --------------------------------------------------------------------------
# D7.1：叙事层不参与检索 / 打分 / S / R / user_affect / 去重
# --------------------------------------------------------------------------

def test_d71_retrieval_and_memory_do_not_import_diary_for_scoring() -> None:
    # 检索层绝不能把日记塞进候选池；记忆层只在 maintain/close/初始化接 DiaryStore
    assert "diary" not in _module_imports("retrieval.py")
    # prompt 允许 import 渲染 helper（DIARY_PREFIX / render_diary_block），但不得读 store
    assert "DiaryStore(" not in _source("prompt.py")
    assert ".active(" not in _source("prompt.py")


def test_d71_diary_does_not_import_retrieval_scoring_or_single_points() -> None:
    imports = _module_imports("diary.py")
    for forbidden in ("retrieval", "pipeline", "memory", "service", "store", "bridge"):
        assert forbidden not in imports
    source = _source("diary.py")
    # 日记不参与 S 计算 / 去重键 / 事实层状态写
    for forbidden_token in ("compute_S(", "dedup_key(", "update_memory(", "MemoryStore("):
        assert forbidden_token not in source


def test_d71_diary_store_is_only_written_by_diary_module() -> None:
    for name in ("retrieval.py", "prompt.py", "parser.py", "state.py"):
        source = _source(name)
        assert "DiaryStore(" not in source
        assert ".diary_file" not in source


# --------------------------------------------------------------------------
# D7.2：write_back 不得修改日记；ctx.diary_excerpts 只读
# --------------------------------------------------------------------------

def test_d72_write_back_does_not_modify_diary(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    entry = DiaryEntry(
        id=system.diary.next_id(),
        date="2026-10-29",
        scope_tag="group",
        content="原有日记内容，不应被写回改动。",
        tokens=12,
        turn_range=(),
        created_at="2026-10-29T12:00:00+08:00",
    )
    system.diary.append(entry)
    before = {entry.id: entry.to_dict()}
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event())
    pipeline.finish(prepared, raw_output())
    after = {item.id: item.to_dict() for item in system.diary.entries.values()}
    assert after.keys() == before.keys()
    assert after[entry.id]["content"] == before[entry.id]["content"]
    assert after[entry.id]["created_at"] == before[entry.id]["created_at"]


def test_d72_ctx_diary_excerpts_are_frozen(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event())
    with pytest.raises(ContextViolation):
        prepared.diary_excerpts = []
    with pytest.raises(ContextViolation):
        prepared.users = {}


# --------------------------------------------------------------------------
# D7.3：日记生成走独立 worker，turn() 内不调用 LLM / 不生成
# --------------------------------------------------------------------------

def test_d73_turn_never_calls_diary_generation(tmp_path, monkeypatch) -> None:
    system = MemorySystem(make_config(tmp_path))
    called: list[str] = []

    def boom(*args, **kwargs):
        called.append("called")
        raise AssertionError("turn() 不得调用日记生成")

    monkeypatch.setattr(system.diary_worker, "generate", boom)
    monkeypatch.setattr(system.diary_worker, "run_due", boom)
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event())
    result = pipeline.finish(prepared, raw_output())
    assert result.answer == "好的。"
    assert called == []


# --------------------------------------------------------------------------
# D7.5 / §8.5：mixed 不落盘、不注入
# --------------------------------------------------------------------------

def test_d75_mixed_never_persisted_or_injected(tmp_path) -> None:
    system = MemorySystem(make_config(tmp_path))
    report = system.diary_worker.generate("mixed", lambda _p: "混合语境日记", force=True)
    assert report.status != "ok"
    assert all(entry.scope_tag in ("group", "dm") for entry in system.diary.entries.values())
    assert system.select_diary_excerpts("mixed")["excerpts"] == []


# --------------------------------------------------------------------------
# 叙事层失败不影响回答（硬规则 19）
# --------------------------------------------------------------------------

def test_diary_failure_never_breaks_answer(tmp_path, monkeypatch) -> None:
    system = MemorySystem(make_config(tmp_path))

    def boom(*args, **kwargs):
        raise OSError("diary disk full")

    monkeypatch.setattr(system.diary, "active", boom)
    pipeline = TurnPipeline(system, system.cfg)
    prepared = pipeline.prepare(event())
    assert prepared.diary_excerpts == []
    result = pipeline.finish(prepared, raw_output("好的。"))
    assert result.answer == "好的。"
    assert system.metrics.count("diary.fail") >= 1
