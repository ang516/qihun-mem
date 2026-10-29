"""v1.5-r2 L1 解析器回归矩阵（契约 §2.5 / §2.13）。

覆盖：
- `normalize_text` 金样 + property（幂等 / NFKC 后键稳定 / 全半角 / 大小写 / 空白折叠 /
  不折叠重复字符 / emoji 保留 / 不截断）；
- `dedup_key` 金样与边界（type 大小写、全半角、中文标点、零宽、重复字符不同键）；
- `validate_affect` 7 类 / 越界截断 / 非数字 → None；
- `normalize_item` 坏条 / 哨兵围栏 / content 500 截断 / tags 8x16 / affect 可选；
- `dedup_within_batch` 保首次（含 affect）；
- `split_raw_candidate` / `parse_memories` / `parse_llm_output` / `clean_answer_for_send`
  的边界与「解析层不清洗 answer」；
- 单点边界：解析层不调 `_drop_fences`、发送层不调 `_loads_candidate`；
- 指标：`mem_extract.affect_invalid` / `item_invalid` / `bad_json`。

v1.4 规范 §10.2 的 16 条中，13（写盘失败）/15（正文空）/16（turn_key 幂等）属于 L3 turn
集成范围，本文件不重复；其余解析边界在新归一化/去重语义下重新锁定。
"""

from __future__ import annotations

import json
import random
import unicodedata

import pytest

from qihun_memory import parser as parser_mod
from qihun_memory.config import ExtractionConfig
from qihun_memory.metrics import Metrics
from qihun_memory.models import ALLOWED_AFFECT_LABELS
from qihun_memory.parser import (
    MAX_CONTENT,
    MAX_TAG_LEN,
    MAX_TAGS,
    clean_answer_for_send,
    dedup_key,
    dedup_within_batch,
    normalize_item,
    normalize_text,
    parse_llm_output,
    parse_memories,
    split_raw_candidate,
    validate_affect,
)

SENT_OPEN = "<MEMORY_JSON>"
SENT_CLOSE = "</MEMORY_JSON>"
FENCE = chr(96) * 3
ZW = chr(0x200B)
BOM = chr(0xFEFF)
ZWJ = chr(0x200D)


# --------------------------------------------------------------------------
# 测试辅助
# --------------------------------------------------------------------------

def _json_raw(memories) -> str:
    return json.dumps({"memories": memories}, ensure_ascii=False)


def _wrap(memories, answer: str = "") -> str:
    return answer + SENT_OPEN + _json_raw(memories) + SENT_CLOSE


def _mem_item(content: str = "用户喜欢猫", item_type: str = "fact", tags=None, affect=None) -> dict:
    item: dict = {"type": item_type, "content": content}
    if tags is not None:
        item["tags"] = tags
    if affect is not None:
        item["affect"] = affect
    return item


def _normalized(content: str = "用户喜欢猫", item_type: str = "fact", tags=None, affect=None) -> dict:
    item: dict = {"type": item_type, "content": content, "tags": tags if tags is not None else []}
    if affect is not None:
        item["affect"] = affect
    return item


# 幂等 / 键稳定 property 的语料：覆盖 NFKC、全半角、中文标点、emoji、零宽、控制字符、长文本
_CORPUS = [
    "",
    "用户喜欢猫",
    " Hello   World ",
    "你好，世界。",
    "ＡＢＣ１２３",
    "开心" + chr(0x1F600) + chr(0x1F433),
    "猫" + ZW + BOM,
    "价格：100；日期…",
    "（测试）【重要】《书名》",
    "① Ⅳ ｶﾀｶﾅ",
    "a" * 600,
    "好" * 3,
    chr(0x1F468) + ZWJ + chr(0x1F469) + ZWJ + chr(0x1F467),
]

_ALPHABET = (
    list("aA1 xyz")
    + list("，。！？“”【】《》…—～")
    + ["猫", "好", "😀", chr(0x1F433), ZW, BOM, ZWJ, "①", "Ａ", "１", "ｶ", chr(9), chr(10)]
)


def _random_texts(count: int = 200, seed: int = 20261029) -> list[str]:
    """固定种子生成 Unicode 混合语料（不引入 hypothesis 依赖）。"""
    rng = random.Random(seed)
    texts: list[str] = []
    for _ in range(count):
        length = rng.randint(0, 40)
        texts.append("".join(rng.choice(_ALPHABET) for _ in range(length)))
    return texts


def _fullwidth_ascii(text: str) -> str:
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        elif ch == " ":
            out.append(chr(0x3000))
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------
# normalize_text：金样
# --------------------------------------------------------------------------

NORMALIZE_GOLD = [
    ("ＡＢＣ１２３", "abc123"),
    ("你好，世界。", "你好,世界."),
    ("他说：“好”", '他说:"好"'),
    ("（测试）【重要】《书名》", "(测试)[重要]<书名>"),
    ("价格：100；日期…", "价格:100;日期..."),
    ("a" + chr(9) + chr(10) + "  b", "a b"),
    ("好好好", "好好好"),
    ("猫" + ZW, "猫"),
    ("猫" + BOM, "猫"),
    ("开心" + chr(0x1F600), "开心" + chr(0x1F600)),
    (chr(0x1F468) + ZWJ + chr(0x1F469), chr(0x1F468) + chr(0x1F469)),
    (chr(7) + "abc" + chr(0), "abc"),
    ("①", "1"),
    ("Ⅳ", "iv"),
    ("ｶﾀｶﾅ", "カタカナ"),
    ("", ""),
]


@pytest.mark.parametrize("raw,expected", NORMALIZE_GOLD)
def test_normalize_text_gold_samples(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalize_text_non_string_inputs_are_safe() -> None:
    assert normalize_text(None) == ""
    assert normalize_text(123) == "123"
    assert normalize_text(3.5) == "3.5"


# --------------------------------------------------------------------------
# normalize_text：property（幂等 / NFKC 后键稳定 / 全半角 / 空白折叠 / 大小写 / 不折叠重复）
# --------------------------------------------------------------------------

def test_normalize_text_is_idempotent() -> None:
    for text in _CORPUS + _random_texts():
        once = normalize_text(text)
        assert normalize_text(once) == once, "非幂等输入: " + repr(text)


def test_normalize_text_does_not_truncate() -> None:
    long_text = "长" * 600
    assert normalize_text(long_text) == long_text
    assert len(normalize_text(long_text)) == 600


def test_normalize_text_does_not_fold_repeated_chars() -> None:
    assert normalize_text("好好好") == "好好好"
    assert normalize_text("好好好") != normalize_text("好")
    assert normalize_text("aab") != normalize_text("ab")


def test_normalize_text_preserves_emoji() -> None:
    assert normalize_text("开心😀") == "开心😀"
    assert normalize_text("👨" + ZWJ + "👩") == "👨👩"


def test_normalize_text_ascii_case_folding_only() -> None:
    assert normalize_text("Hello WORLD") == "hello world"
    assert normalize_text("用户ABC") == "用户abc"


def test_normalize_text_folds_whitespace() -> None:
    assert normalize_text("你好  世界") == "你好 世界"
    # 制表/换行属于控制字符（Cc），先被删除；相邻空格再折叠
    assert normalize_text("你好" + chr(9) + "世界") == "你好世界"
    assert normalize_text("你好" + chr(10) + "世界") == "你好世界"
    assert normalize_text("a" + chr(9) + chr(10) + "  b") == "a b"
    assert normalize_text("你好" + chr(0x3000) + "世界") == "你好 世界"
    assert normalize_text("  你好世界  ") == "你好世界"


def test_dedup_key_stable_after_nfkc() -> None:
    for text in _CORPUS + _random_texts():
        direct = dedup_key({"type": "fact", "content": text})
        nfkc = dedup_key({"type": "fact", "content": unicodedata.normalize("NFKC", text)})
        assert direct == nfkc, "NFKC 后键不稳定: " + repr(text)


def test_dedup_key_stable_after_normalize_text() -> None:
    for text in _CORPUS + _random_texts():
        assert dedup_key({"type": "fact", "content": normalize_text(text)}) == dedup_key(
            {"type": "fact", "content": text}
        )


@pytest.mark.parametrize("text", ["hello world", "abc123", "A B C", "x,y.z", "user_id 42"])
def test_fullwidth_ascii_shares_key_with_halfwidth(text: str) -> None:
    assert dedup_key({"type": "fact", "content": _fullwidth_ascii(text)}) == dedup_key(
        {"type": "fact", "content": text}
    )


# --------------------------------------------------------------------------
# dedup_key：金样与边界
# --------------------------------------------------------------------------

def test_dedup_key_gold_samples() -> None:
    assert dedup_key({"type": "fact", "content": "用户喜欢猫"}) == ("fact", "用户喜欢猫")
    # 首尾空白不参与键
    assert dedup_key({"type": "fact", "content": " 用户喜欢猫 "}) == dedup_key(
        {"type": "fact", "content": "用户喜欢猫"}
    )
    # 同内容不同类型是不同键
    assert dedup_key({"type": "fact", "content": "用户喜欢猫"}) != dedup_key(
        {"type": "episode", "content": "用户喜欢猫"}
    )
    # 零宽 / BOM 不参与键
    assert dedup_key({"type": "fact", "content": "猫" + ZW}) == dedup_key(
        {"type": "fact", "content": "猫"}
    )
    assert dedup_key({"type": "fact", "content": BOM + "猫"}) == dedup_key(
        {"type": "fact", "content": "猫"}
    )
    # 全角数字 / 字母与半角同键
    assert dedup_key({"type": "fact", "content": "ＡＢＣ１２３"}) == (
        "fact",
        "abc123",
    )
    # 中文标点与半角标点同键（仅键用，不改原文）
    assert dedup_key({"type": "fact", "content": "你好，世界。"}) == dedup_key(
        {"type": "fact", "content": "你好,世界."}
    )
    # emoji 不同则键不同
    assert dedup_key({"type": "fact", "content": "开心😀"}) != dedup_key(
        {"type": "fact", "content": "开心😁"}
    )
    # 重复字符不折叠：不同键
    keys = {
        dedup_key({"type": "fact", "content": "好"}),
        dedup_key({"type": "fact", "content": "好好"}),
        dedup_key({"type": "fact", "content": "好好好"}),
    }
    assert len(keys) == 3


@pytest.mark.parametrize(
    "value,expected",
    [
        ("FACT", "fact"),
        ("  Fact  ", "fact"),
        ("Episode", "episode"),
        ("PREFERENCE", "preference"),
    ],
)
def test_dedup_key_type_is_lowercased_and_stripped(value: str, expected: str) -> None:
    assert dedup_key({"type": value, "content": "x"})[0] == expected


def test_dedup_key_non_mapping_and_missing_fields() -> None:
    assert dedup_key(None) == ("", "")  # type: ignore[arg-type]
    assert dedup_key({"content": "x"}) == ("", "x")
    assert dedup_key({"type": "fact"}) == ("fact", "")
    assert dedup_key({"type": "fact", "content": None}) == ("fact", "")
    assert dedup_key({"type": "fact", "content": 123}) == ("fact", "123")


def test_dedup_key_does_not_truncate_at_500() -> None:
    base = "a" * 600
    assert dedup_key({"type": "fact", "content": base})[1] == base
    assert dedup_key({"type": "fact", "content": base + "X"}) != dedup_key(
        {"type": "fact", "content": base + "Y"}
    )


# --------------------------------------------------------------------------
# validate_affect：7 类 / 越界截断 / 非数字 → None
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label", ALLOWED_AFFECT_LABELS)
def test_validate_affect_accepts_all_seven_labels(label: str) -> None:
    result = validate_affect({"affect": {"label": label, "intensity": 0.5}})
    assert result == {"label": label, "intensity": 0.5}


def test_validate_affect_missing_or_none_is_none() -> None:
    assert validate_affect({}) is None
    assert validate_affect({"affect": None}) is None
    assert validate_affect({"affect": "joy"}) is None
    assert validate_affect({"affect": ["joy", 0.5]}) is None
    assert validate_affect(None) is None  # type: ignore[arg-type]


def test_validate_affect_label_case_and_whitespace_normalized() -> None:
    assert validate_affect({"affect": {"label": " JOY ", "intensity": 0.4}}) == {
        "label": "joy",
        "intensity": 0.4,
    }


def test_validate_affect_defaults_intensity_to_zero() -> None:
    assert validate_affect({"affect": {"label": "joy"}}) == {"label": "joy", "intensity": 0.0}


def test_validate_affect_out_of_range_intensity_is_clamped() -> None:
    assert validate_affect({"affect": {"label": "joy", "intensity": 1.5}})["intensity"] == 1.0
    assert validate_affect({"affect": {"label": "joy", "intensity": -0.5}})["intensity"] == 0.0
    assert validate_affect({"affect": {"label": "joy", "intensity": 42}})["intensity"] == 1.0


def test_affect_case_18_non_enum_labels_are_invalid() -> None:
    # 规范 §10.3-18：非法 label（trust / 大小写不匹配的 LOVE）只丢字段
    for label in ("trust", "love", "LOVE", "Happy", ""):
        assert validate_affect({"affect": {"label": label, "intensity": 0.5}}) is None


def test_affect_case_20_missing_or_null_affect_means_none() -> None:
    # 规范 §10.3-20：老数据无 affect / affect:null → 无 affect 字段，不报错
    expected = {"type": "fact", "content": "老数据", "tags": []}
    assert normalize_item({"type": "fact", "content": "老数据"}) == expected
    assert normalize_item({"type": "fact", "content": "老数据", "affect": None}) == expected
    assert validate_affect({"type": "fact", "content": "老数据"}) is None


def test_affect_case_21_neutral_is_legal_but_plain() -> None:
    # 规范 §10.3-21：neutral 合法，但不参与正负聚合 / k_a 调制（k_a 侧在 L2 断言）
    assert validate_affect({"affect": {"label": "neutral", "intensity": 1.0}}) == {
        "label": "neutral",
        "intensity": 1.0,
    }


def test_validate_affect_numeric_string_intensity_accepted() -> None:
    assert validate_affect({"affect": {"label": "joy", "intensity": "0.4"}}) == {
        "label": "joy",
        "intensity": 0.4,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"label": "happiness", "intensity": 0.4},
        {"label": "", "intensity": 0.4},
        {"label": 123, "intensity": 0.4},
        {"intensity": 0.4},
        {"label": "joy", "intensity": "abc"},
        {"label": "joy", "intensity": True},
        {"label": "joy", "intensity": None},
        {"label": "joy", "intensity": float("nan")},
        {"label": "joy", "intensity": float("inf")},
    ],
)
def test_validate_affect_illegal_returns_none(bad: dict) -> None:
    assert validate_affect({"affect": bad}) is None


# --------------------------------------------------------------------------
# normalize_item：坏条 / 哨兵围栏 / 500 截断 / tags 8x16 / affect 可选
# --------------------------------------------------------------------------

def test_normalize_item_cleans_and_keeps_valid_item() -> None:
    item = normalize_item({"type": "episode", "content": "  用户下周三去上海  ", "tags": ["出差"]})
    assert item == {"type": "episode", "content": "用户下周三去上海", "tags": ["出差"]}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "字符串",
        42,
        [],
        {},
        {"type": "note", "content": "x"},
        {"type": "fact"},
        {"type": "fact", "content": 123},
        {"type": "fact", "content": "   "},
        {"content": "x"},
    ],
)
def test_normalize_item_rejects_bad_items(bad) -> None:
    assert normalize_item(bad) is None


def test_normalize_item_keeps_only_known_fields() -> None:
    item = normalize_item(
        {
            "type": "fact",
            "content": "用户喜欢猫",
            "valence": 0.9,
            "user_id": "u1",
            "id": "m_1",
            "affect": {"label": "joy", "intensity": 0.5},
        }
    )
    assert set(item) == {"type", "content", "tags", "affect"}
    assert "valence" not in item
    assert "user_id" not in item


def test_normalize_item_truncates_content_to_500() -> None:
    item = normalize_item({"type": "fact", "content": "长" * 600})
    assert len(item["content"]) == MAX_CONTENT == 500


def test_normalize_item_custom_limits() -> None:
    limits = ExtractionConfig(max_content=5, max_tags=2, max_tag_len=3)
    item = normalize_item(
        {"type": "fact", "content": "abcdefg", "tags": ["abcdef", "b", "c"]},
        limits=limits,
    )
    assert item["content"] == "abcde"
    assert item["tags"] == ["abc", "b"]


def test_normalize_item_strips_control_chars() -> None:
    item = normalize_item(
        {"type": "fact", "content": chr(1) + "有控制字符" + chr(7) + chr(127) + chr(0x85)}
    )
    assert item["content"] == "有控制字符"


@pytest.mark.parametrize(
    "content",
    [
        "含哨兵 " + SENT_OPEN,
        "含闭合 " + SENT_CLOSE,
        "含围栏 " + FENCE,
        "含围栏中段 " + FENCE + "json",
    ],
)
def test_normalize_item_sentinel_or_fence_content_dropped(content: str) -> None:
    metrics = Metrics()
    assert normalize_item({"type": "fact", "content": content}, metrics=metrics) is None
    assert metrics.count("mem_extract.item_invalid") == 1


def test_normalize_item_tags_limited_to_8x16() -> None:
    tags = ["t" + str(i) for i in range(10)]
    item = normalize_item({"type": "fact", "content": "x", "tags": tags})
    assert len(item["tags"]) == MAX_TAGS == 8
    assert all(len(tag) <= MAX_TAG_LEN for tag in item["tags"])


def test_normalize_item_tags_non_list_and_blank_entries() -> None:
    assert normalize_item({"type": "fact", "content": "x", "tags": "not-a-list"})["tags"] == []
    assert normalize_item({"type": "fact", "content": "x", "tags": ["  ", "正常", 123]})["tags"] == [
        "正常",
        "123",
    ]


def test_normalize_item_valid_affect_kept() -> None:
    item = normalize_item(
        {"type": "fact", "content": "x", "affect": {"label": "anger", "intensity": 0.6}}
    )
    assert item["affect"] == {"label": "anger", "intensity": 0.6}


def test_normalize_item_invalid_affect_dropped_item_kept_with_metric() -> None:
    metrics = Metrics()
    item = normalize_item(
        {"type": "fact", "content": "x", "affect": {"label": "nope", "intensity": 0.6}},
        metrics=metrics,
    )
    assert item is not None
    assert "affect" not in item
    assert metrics.count("mem_extract.affect_invalid") == 1
    assert metrics.count("mem_extract.item_invalid") == 0


def test_normalize_item_affect_none_means_no_field() -> None:
    item = normalize_item({"type": "fact", "content": "x", "affect": None})
    assert item == {"type": "fact", "content": "x", "tags": []}
    assert "affect" not in item


def test_normalize_item_out_of_range_affect_truncated_without_metric() -> None:
    metrics = Metrics()
    item = normalize_item(
        {"type": "fact", "content": "x", "affect": {"label": "joy", "intensity": 9}},
        metrics=metrics,
    )
    assert item["affect"]["intensity"] == 1.0
    assert metrics.count("mem_extract.affect_invalid") == 0


def test_normalize_item_bad_items_counted_individually() -> None:
    metrics = Metrics()
    bad_items = ["字符串", {"type": "note", "content": "x"}, {"type": "fact", "content": ""}]
    assert [normalize_item(item, metrics=metrics) for item in bad_items] == [None, None, None]
    assert metrics.count("mem_extract.item_invalid") == 3


def test_item_invalid_metric_records_reason_without_content(tmp_path) -> None:
    log_path = tmp_path / "metrics.jsonl"
    metrics = Metrics(log_path=log_path)
    normalize_item({"type": "note", "content": "敏感正文"}, metrics=metrics)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "mem_extract.item_invalid"
    assert event["reason"] == "bad_type"
    # 隐私最小化：指标不记正文
    assert "敏感正文" not in lines[0]


# --------------------------------------------------------------------------
# dedup_within_batch：保首次（含 affect）
# --------------------------------------------------------------------------

def test_dedup_within_batch_keeps_first_including_affect() -> None:
    first = {"type": "fact", "content": "猫", "tags": [], "affect": {"label": "joy", "intensity": 0.8}}
    second = {
        "type": "fact",
        "content": "猫",
        "tags": [],
        "affect": {"label": "anger", "intensity": 0.2},
    }
    assert dedup_within_batch([first, second]) == [first]


def test_dedup_within_batch_preserves_order_and_distinct_items() -> None:
    items = [
        {"type": "fact", "content": "A", "tags": []},
        {"type": "episode", "content": "A", "tags": []},
        {"type": "fact", "content": "B", "tags": []},
    ]
    assert dedup_within_batch(items) == items


def test_dedup_within_batch_uses_normalized_keys() -> None:
    items = [
        {"type": "fact", "content": "ＡＢＣ", "tags": []},
        {"type": "FACT", "content": "abc", "tags": []},
        {"type": "fact", "content": "你好，世界", "tags": []},
        {"type": "fact", "content": "你好,世界", "tags": []},
    ]
    assert dedup_within_batch(items) == [items[0], items[2]]


def test_dedup_within_batch_empty() -> None:
    assert dedup_within_batch([]) == []


def test_dedup_within_batch_calls_single_dedup_key(monkeypatch) -> None:
    calls: list[dict] = []
    real = parser_mod.dedup_key

    def spy(item):
        calls.append(item)
        return real(item)

    monkeypatch.setattr(parser_mod, "dedup_key", spy)
    dedup_within_batch([{"type": "fact", "content": "a"}, {"type": "fact", "content": "a"}])
    assert len(calls) == 2


# --------------------------------------------------------------------------
# split_raw_candidate：只做定位，不清洗 answer
# --------------------------------------------------------------------------

def test_split_raw_candidate_with_sentinel() -> None:
    candidate_json = _json_raw([_mem_item("猫")])
    answer, candidate = split_raw_candidate("好的" + SENT_OPEN + candidate_json + SENT_CLOSE)
    assert answer == "好的"
    assert candidate == candidate_json


def test_split_raw_candidate_unparseable_candidate_returns_none() -> None:
    assert split_raw_candidate("好的" + SENT_OPEN + "不是 JSON" + SENT_CLOSE) == ("好的", None)


def test_split_raw_candidate_fake_block_before_real_block() -> None:
    raw = "A" + SENT_OPEN + "坏" + SENT_CLOSE + "B" + _wrap([_mem_item("真")])
    answer, candidate = split_raw_candidate(raw)
    assert answer == "A"
    assert candidate is not None
    assert "真" in candidate


def test_split_raw_candidate_no_sentinel_removes_all_blocks() -> None:
    raw = "第一块" + _json_raw([_mem_item("旧")]) + "第二块" + _json_raw([_mem_item("新")])
    answer, candidate = split_raw_candidate(raw)
    assert answer == "第一块第二块"
    assert candidate == _json_raw([_mem_item("新")])


def test_split_raw_candidate_plain_text_and_none() -> None:
    assert split_raw_candidate("普通正文") == ("普通正文", None)
    assert split_raw_candidate("") == ("", None)
    assert split_raw_candidate(None) == ("", None)
    assert split_raw_candidate(123) == ("123", None)


def test_split_raw_candidate_does_not_clean_answer() -> None:
    raw = BOM + "正文" + ZW + _wrap([_mem_item("猫")])
    answer, candidate = split_raw_candidate(raw)
    assert answer == BOM + "正文" + ZW
    assert candidate is not None


def test_split_raw_candidate_unclosed_candidate_still_returned() -> None:
    fragment = '{"memories":[{"type":"fact","content":"猫"'
    answer, candidate = split_raw_candidate("正文" + fragment)
    assert answer == "正文"
    assert candidate == fragment


# --------------------------------------------------------------------------
# parse_memories：loads → normalize → dedup → cap，不碰 answer
# --------------------------------------------------------------------------

def test_parse_memories_none_and_empty() -> None:
    assert parse_memories(None) == []
    assert parse_memories("") == []


def test_parse_memories_bad_json_metric() -> None:
    metrics = Metrics()
    assert parse_memories("不是 JSON", metrics=metrics) == []
    assert metrics.count("mem_extract.bad_json") == 1


def test_parse_memories_invalid_structure_metric() -> None:
    metrics = Metrics()
    assert parse_memories('{"memories": {}}', metrics=metrics) == []
    assert metrics.count("mem_extract.bad_json") == 1


def test_parse_memories_fenced_candidate_accepted() -> None:
    candidate = FENCE + "json" + chr(10) + _json_raw([_mem_item("猫")]) + chr(10) + FENCE
    assert parse_memories(candidate) == [{"type": "fact", "content": "猫", "tags": []}]


def test_parse_memories_normalizes_and_dedupes() -> None:
    candidate = _json_raw(
        [
            {"type": "fact", "content": " 猫 ", "tags": ["宠物"]},
            {"type": "FACT", "content": "猫", "tags": []},
            {"type": "note", "content": "坏"},
        ]
    )
    assert parse_memories(candidate) == [{"type": "fact", "content": "猫", "tags": ["宠物"]}]


def test_parse_memories_caps_at_five_without_truncated_metric() -> None:
    metrics = Metrics()
    candidate = _json_raw([_mem_item("记忆" + str(i), "episode") for i in range(7)])
    assert len(parse_memories(candidate, metrics=metrics)) == 5
    assert metrics.count("mem_extract.truncated") == 0


def test_parse_memories_custom_limits() -> None:
    candidate = _json_raw([_mem_item("a"), _mem_item("b"), _mem_item("c")])
    limits = ExtractionConfig(max_items=2)
    assert parse_memories(candidate, limits=limits) == [
        {"type": "fact", "content": "a", "tags": []},
        {"type": "fact", "content": "b", "tags": []},
    ]


def test_parse_memories_invalid_affect_only_drops_field() -> None:
    metrics = Metrics()
    candidate = _json_raw(
        [
            {"type": "fact", "content": "有感情", "affect": {"label": "joy", "intensity": 0.7}},
            {"type": "fact", "content": "坏感情", "affect": {"label": "hate", "intensity": 0.7}},
        ]
    )
    items = parse_memories(candidate, metrics=metrics)
    assert items[0]["affect"] == {"label": "joy", "intensity": 0.7}
    assert "affect" not in items[1]
    assert metrics.count("mem_extract.affect_invalid") == 1
    assert metrics.count("mem_extract.item_invalid") == 0


def test_parse_memories_never_raises() -> None:
    for candidate in ("{", "[]", "", '{"memories":', "{}", "x" * 100000):
        assert isinstance(parse_memories(candidate), list)


# --------------------------------------------------------------------------
# parse_llm_output：只组合，永不清洗 answer / 永不抛
# --------------------------------------------------------------------------

def test_parse_llm_output_returns_raw_answer_untouched() -> None:
    raw = (
        BOM
        + FENCE
        + "json"
        + chr(10)
        + "正文"
        + FENCE
        + chr(10)
        + _wrap([_mem_item("猫")])
    )
    answer, memories = parse_llm_output(raw)
    assert answer == raw[: raw.index(SENT_OPEN)]
    assert memories == [{"type": "fact", "content": "猫", "tags": []}]
    # 发送层清洗后才去掉围栏 / BOM
    cleaned = clean_answer_for_send(answer)
    assert FENCE not in cleaned
    assert BOM not in cleaned
    assert "正文" in cleaned


def test_parse_llm_output_no_sentinel_returns_raw_answer() -> None:
    raw = BOM + "正文" + ZW
    answer, memories = parse_llm_output(raw)
    assert answer == raw
    assert memories == []


def test_parse_llm_output_uses_text_before_earliest_sentinel() -> None:
    raw = "A" + _wrap([_mem_item("旧")]) + "B" + _wrap([_mem_item("新")])
    answer, memories = parse_llm_output(raw)
    assert answer == "A"
    assert [m["content"] for m in memories] == ["新"]


@pytest.mark.parametrize("raw", [None, "", "普通回答", 123, b"bytes", object()])
def test_parse_llm_output_never_raises(raw) -> None:
    answer, memories = parse_llm_output(raw)
    assert isinstance(answer, str)
    assert isinstance(memories, list)


def test_partial_bad_items_keep_good_ones_with_new_normalizer() -> None:
    raw = "记一下" + _wrap(
        [
            _mem_item("好记忆"),
            {"type": "note", "content": "坏"},
            {"type": "fact", "content": "  "},
            _mem_item("好记忆2", "episode", affect={"label": "joy", "intensity": 0.5}),
        ]
    )
    answer, memories = parse_llm_output(raw)
    assert answer == "记一下"
    assert [m["content"] for m in memories] == ["好记忆", "好记忆2"]
    assert memories[1]["affect"] == {"label": "joy", "intensity": 0.5}


# --------------------------------------------------------------------------
# clean_answer_for_send：发送层唯一清洗点
# --------------------------------------------------------------------------

CLEAN_GOLD = [
    ("好的，记下了。", "好的，记下了。"),
    ("好的" + _wrap([_mem_item("猫")]), "好的"),
    ("好的" + _json_raw([]) + "继续" + SENT_OPEN + "{}", "好的继续"),
    ("正文<MEMORY_JS", "正文"),
    ("正文<MEMORY_J", "正文"),
    ("正文<MEMORY_JSON", "正文"),
    ("正文<MEMORY_JSON>", "正文"),
    ("前" + SENT_OPEN + "不是块", "前"),
    (FENCE + "json" + chr(10) + "abc" + chr(10) + FENCE, "abc"),
    ("看这里" + chr(10) + FENCE + "python" + chr(10) + "print(1)" + chr(10) + FENCE, "看这里" + chr(10) + chr(10) + "print(1)"),
    (BOM + "你好" + ZW, "你好"),
    (chr(1) + "你好" + chr(7), "你好"),
    ("", ""),
    (None, ""),
    ("他发了个 {} 表情", "他发了个 {} 表情"),
    ("memories 这个词", "memories 这个词"),
    ("正文" + '{"memories":[{"type":"fact","content":"隐藏"}]}', "正文"),
]


@pytest.mark.parametrize("raw,expected", CLEAN_GOLD)
def test_clean_answer_for_send_gold(raw, expected: str) -> None:
    assert clean_answer_for_send(raw) == expected


def test_clean_answer_for_send_is_idempotent() -> None:
    for text in (
        "好的" + _wrap([]),
        FENCE + "json" + chr(10) + "正文" + chr(10) + FENCE,
        BOM + "你好" + ZW,
        "他发了个 {} 表情",
    ):
        once = clean_answer_for_send(text)
        assert clean_answer_for_send(once) == once


# --------------------------------------------------------------------------
# 单点边界：解析层不清洗 answer，发送层不解析候选
# --------------------------------------------------------------------------

def test_parse_layer_never_calls_drop_fences(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("解析层禁止调用 _drop_fences")

    monkeypatch.setattr(parser_mod, "_drop_fences", boom)
    raw = "前置说明" + _wrap([_mem_item("猫")])
    assert split_raw_candidate(raw)[0] == "前置说明"
    assert parse_memories(_json_raw([_mem_item("猫")])) == [
        {"type": "fact", "content": "猫", "tags": []}
    ]
    assert parse_llm_output(raw)[1] == [{"type": "fact", "content": "猫", "tags": []}]
    assert normalize_item(_mem_item("猫")) is not None
    assert dedup_within_batch([_mem_item("猫")]) == [_mem_item("猫")]


def test_send_layer_never_calls_loads_candidate(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("发送层禁止调用 _loads_candidate")

    monkeypatch.setattr(parser_mod, "_loads_candidate", boom)
    assert clean_answer_for_send("正文" + SENT_OPEN + "坏" + SENT_CLOSE) == "正文"
    assert clean_answer_for_send("正文" + '{"memories":[]}') == "正文"


# --------------------------------------------------------------------------
# 兼容入口：MemoryOutputParser.ExtractResult.raw_answer
# --------------------------------------------------------------------------

def test_legacy_parser_exposes_raw_answer_and_cleaned_answer() -> None:
    raw = (
        BOM
        + FENCE
        + "json"
        + chr(10)
        + "正文"
        + FENCE
        + chr(10)
        + _wrap([_mem_item("猫")])
    )
    result = parser_mod.MemoryOutputParser().parse(raw)
    assert result.status == "ok"
    assert result.raw_answer == raw[: raw.index(SENT_OPEN)]
    assert result.answer == "正文"
    assert result.memories[0]["content"] == "猫"


def test_legacy_parser_no_sentinel_raw_answer_is_original() -> None:
    result = parser_mod.MemoryOutputParser().parse("普通回答")
    assert result.status == "no_sentinel"
    assert result.raw_answer == "普通回答"
    assert result.answer == "普通回答"
