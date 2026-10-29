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


# ---------------------------------------------------------------------------
# v1.6-rc4 §7.1 轻路径五条规则（E3：轻路径只决定是否检索，不影响闸门）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "   ", "😄", "😄😄", "……", "！！！", "？？？", "哈哈哈哈哈", "666666"],
)
def test_light_rule_a_no_text(text: str) -> None:
    """(a) 纯表情 / 纯标点 / 纯 emoji / 纯重复字符（无文字）→ 轻路径。"""
    assert is_light_message(text) is True


@pytest.mark.parametrize("text", ["谢谢你啦😄", "晚上好啊", "帮我写个代码"])
def test_light_rule_a_negative_has_text(text: str) -> None:
    assert is_light_message(text) is False


@pytest.mark.parametrize("text", ["去上海", "在忙", "吃了吗", "好的呀"])
def test_light_rule_b_short_no_question(text: str) -> None:
    """(b) 去首尾空白后 <4 字、不含问号、非 @bot → 轻路径。"""
    assert is_light_message(text) is True


@pytest.mark.parametrize("text", ["去哪？", "去上海吗?", "四字短句啊", "我下周三去上海出差"])
def test_light_rule_b_negative(text: str) -> None:
    assert is_light_message(text) is False


def test_light_rule_b_at_bot_is_not_light_by_short_rule() -> None:
    # (b) 明确排除 @bot：短句被 @ 时交给闸门/提取判定，不因「短」走轻路径
    assert is_light_message("去哪", at_bot=True) is False


@pytest.mark.parametrize("text", ["你好", "你好！！", "晚安。", "收到～", "早上好！"])
def test_light_rule_c_greeting_words(text: str) -> None:
    """(c) 寒暄词表：去标点后整句完全相等（单字项不做子串匹配）。"""
    assert is_light_message(text) is True


@pytest.mark.parametrize("text", ["你好呀呀", "快说你好", "早上好大家好"])
def test_light_rule_c_greeting_not_substring(text: str) -> None:
    assert is_light_message(text) is False


def test_light_rule_c_greeting_words_configurable() -> None:
    from qihun_memory.config import GateConfig

    assert is_light_message("晚上好啊") is False  # 默认词表里没有
    cfg = GateConfig(greeting_words=("晚上好啊",))
    assert is_light_message("晚上好啊！", cfg=cfg) is True


def test_light_rule_d_repeat_of_last_user_message() -> None:
    """(d) 复读：与最近一条用户消息去标点后完全相同 → 轻路径。"""
    assert is_light_message("今天真的好热啊", last_user_message="今天真的好热啊！") is True
    assert is_light_message("今天真的好热啊") is False
    assert is_light_message("今天真的好热啊", last_user_message="今天好冷啊") is False


def test_light_rule_e_pure_at_bot() -> None:
    """(e) 纯 @bot 无正文（去掉 @提及 / CQ 码 / 昵称后无实质内容且无问号）。"""
    assert is_light_message("@小明", at_bot=True) is True
    assert is_light_message("[CQ:at,qq=123]", at_bot=True) is True
    assert is_light_message("[CQ:at,qq=123,name=小明] 在吗", at_bot=True) is True
    assert is_light_message("@小明 去上海吗？", at_bot=True) is False
    assert is_light_message("@小明 去上海", at_bot=True) is False


def test_light_path_and_gate_are_decoupled() -> None:
    """E3：轻路径只决定是否检索，不决定是否提取——「去上海」两者可同时命中。"""
    assert is_light_message("去上海") is True
    assert should_extract("去上海") is True
    assert is_light_message("晚安") is True
    assert should_extract("晚安") is False
