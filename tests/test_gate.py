"""写入闸门（§4.1）与检索轻路径（§6.1）。"""

from __future__ import annotations

import pytest

from qihun_memory.gate import is_light_message, should_extract


@pytest.mark.parametrize(
    "text",
    [
        "我下周三去上海出差",
        "这周六下午三点见",
        "到时候叫我",
        "记得提醒我买药",
        "我喜欢吃辣",
        "我不吃香菜",
        "我感冒了",
        "我最近辞职了",
        "明天去机场",
        "周末去杭州",
        "帮我带杯咖啡",
        "12月15号考试",
    ],
)
def test_gate_positive(text: str) -> None:
    assert should_extract(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "哈哈哈",
        "在吗",
        "晚安",
        "6",
        "好的",
        "嗯嗯",
        "笑死",
        "？",
        "早",
        "路过",
    ],
)
def test_gate_negative(text: str) -> None:
    assert should_extract(text) is False


def test_gate_at_or_reply() -> None:
    assert should_extract("随便说说", at_bot=True) is True
    assert should_extract("随便说说", reply_to_bot=True) is True
    assert should_extract("随便说说") is False


def test_gate_disabled() -> None:
    from qihun_memory.config import GateConfig

    assert should_extract("我下周三去上海出差", cfg=GateConfig(enabled=False)) is False


def test_gate_extra_patterns() -> None:
    from qihun_memory.config import GateConfig

    cfg = GateConfig(extra_patterns=(r"原神",))
    assert should_extract("原神启动", cfg=cfg) is True
    assert should_extract("原神启动") is False


@pytest.mark.parametrize(
    "text",
    ["", "   ", "哈哈", "哈", "在吗", "晚安", "😄", "？？？", "666666", "嗯", "好", "收到"],
)
def test_light_messages(text: str) -> None:
    assert is_light_message(text) is True


@pytest.mark.parametrize(
    "text",
    ["上海怎么样", "我下周三去上海出差", "你最近在忙什么", "帮我写个代码"],
)
def test_not_light_messages(text: str) -> None:
    assert is_light_message(text) is False
