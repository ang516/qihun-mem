"""示例文件可用性：config / state / users / memory.jsonl / summary / raw 输出。"""

from __future__ import annotations

import json
from pathlib import Path

from qihun_memory.config import Config
from qihun_memory.models import MemoryItem
from qihun_memory.parser import MemoryOutputParser
from qihun_memory.summary import RollingSummary

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def load_json(name: str):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_config_example(tmp_path) -> None:
    cfg = Config.from_dict(load_json("config.example.json"), base_dir=tmp_path)
    assert cfg.decay.s_max == 90.0
    assert cfg.retrieval.budget_tokens == 1200
    assert cfg.decay.revive_sim == 0.7
    assert cfg.service.host == "127.0.0.1"


def test_persona_example() -> None:
    text = (EXAMPLES / "persona.example.md").read_text(encoding="utf-8")
    assert "器魂" in text
    assert len(text) > 50


def test_state_and_users_example() -> None:
    state = load_json("state.example.json")
    assert -1.0 <= state["valence"] <= 1.0
    assert 0.0 <= state["avoidance"] <= 1.0
    users = load_json("users.example.json")
    assert users and all(isinstance(key, str) for key in users)


def test_memory_jsonl_example() -> None:
    lines = (EXAMPLES / "memory.example.jsonl").read_text(encoding="utf-8").strip().splitlines()
    items = [MemoryItem.from_dict(json.loads(line)) for line in lines]
    assert len(items) == 3
    assert {item.type for item in items} == {"episode", "fact", "preference"}
    assert any(item.status == "dormant" for item in items)


def test_summary_example(tmp_path) -> None:
    target = tmp_path / "summary.json"
    target.write_text((EXAMPLES / "summary.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    summary = RollingSummary(target)
    assert "上海" in summary.get("group")


def test_raw_output_example() -> None:
    raw = (EXAMPLES / "raw-output.example.txt").read_text(encoding="utf-8")
    result = MemoryOutputParser().parse(raw)
    assert result.status == "ok"
    assert result.answer.startswith("好的")
    assert len(result.memories) == 1
    assert result.memories[0]["content"] == "用户A下周三去上海出差"
