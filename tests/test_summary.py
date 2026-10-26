"""滚动会话摘要（§7 步骤 2 / §8 ≤200 tokens）。"""

from __future__ import annotations

from qihun_memory.config import PromptConfig
from qihun_memory.summary import RollingSummary, conversation_key


def test_conversation_key() -> None:
    assert conversation_key("group", "u_1") == "group"
    assert conversation_key("dm", "u_1") == "dm:u_1"
    assert conversation_key("dm", "u_2") == "dm:u_2"


def test_update_and_persist(tmp_path) -> None:
    path = tmp_path / "summary.json"
    summary = RollingSummary(path, PromptConfig(summary_tokens=200))
    summary.update("group", "我下周三去上海", "好的，记下了")
    assert "用户：" in summary.get("group")
    assert "器魂：" in summary.get("group")
    assert path.is_file()

    reloaded = RollingSummary(path, PromptConfig(summary_tokens=200))
    assert reloaded.get("group") == summary.get("group")


def test_trim_to_budget(tmp_path) -> None:
    summary = RollingSummary(tmp_path / "summary.json", PromptConfig(summary_tokens=30))
    for index in range(30):
        summary.update("group", f"第{index}条消息", "好")
    text = summary.get("group")
    from qihun_memory.tokens import estimate_tokens

    assert estimate_tokens(text) <= 30
    # 最旧的被丢弃，最新的保留
    assert "第29条" in text


def test_reset(tmp_path) -> None:
    summary = RollingSummary(tmp_path / "summary.json", PromptConfig())
    summary.update("dm:u_1", "你好", "你好呀")
    assert summary.get("dm:u_1")
    summary.reset("dm:u_1")
    assert summary.get("dm:u_1") == ""
