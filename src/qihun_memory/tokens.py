"""极简 token 估算（v1.4 §6.3 / §8 的固定 token 预算用）。

设计文档要求预算用固定 token 数，不按百分比。系统不依赖具体 tokenizer，
用保守的确定性估算：CJK 字符按 1 token/字，非 CJK 字符按 4 字符/token
（向上取整），空白与标点按非 CJK 计。这样在 8k 窗口下预算只会偏保守。
"""

from __future__ import annotations

import math

_CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK Ext A
    (0x4E00, 0x9FFF),   # CJK Unified
    (0xF900, 0xFAFF),   # Compatibility Ideographs
    (0x3040, 0x30FF),   # Hiragana / Katakana
    (0xAC00, 0xD7AF),   # Hangul
    (0x3000, 0x303F),   # CJK punctuation
    (0xFF00, 0xFFEF),   # Full-width forms
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= code <= hi:
            return True
    return False


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（确定性、无外部依赖）。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 4)
