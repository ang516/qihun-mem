"""Python 3.11 语法兼容性守卫。

pyproject.toml 声明 requires-python >= 3.11，CI 矩阵覆盖 3.11–3.14。
PEP 701（Python 3.12）放宽了 f-string 内的引号复用、反斜杠与注释限制；
这类写法在本地 3.12+ 能通过，却会让 3.11 在测试收集阶段直接 SyntaxError。

本测试在 3.12+ 上用新版 f-string 分词器扫描源码，禁止出现 3.11 不接受的写法，
避免再次出现只在 CI 的 3.11 任务里暴露的收集失败。
"""

from __future__ import annotations

import io
import pathlib
import tokenize

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("src", "tests")
QUOTES = ('"""', "'''", '"', "'")
BACKSLASH = chr(92)

_HAS_PEP701_TOKENIZER = hasattr(tokenize, "FSTRING_START")


def _quote_of(literal: str) -> str:
    """取出字符串字面量的引号定界符（跳过 f/r/b/u 前缀）。"""
    index = 0
    while index < len(literal) and literal[index].lower() in "frbu":
        index += 1
    rest = literal[index:]
    for quote in QUOTES:
        if rest.startswith(quote):
            return quote
    return rest


def _reuses_quote(outer: str, inner: str) -> bool:
    """判断 3.11 是否会因为内外引号相同而解析失败。

    单引号 f-string 不能包含同种引号的字符串；三引号 f-string 可以包含
    同种单引号字符串，但不能再嵌套同种三引号。
    """
    if outer[0] != inner[0]:
        return False
    return len(outer) == 1 or len(inner) == 3


def _iter_python_files():
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT)
            if any(part.startswith(".") for part in relative.parts):
                continue
            yield path


def _scan(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    problems: list[tuple[int, str]] = []
    stack: list[tuple[str, int]] = []
    for tok in tokens:
        if tok.type == tokenize.FSTRING_START:
            quote = _quote_of(tok.string)
            if stack and _reuses_quote(stack[-1][0], quote):
                problems.append((tok.start[0], f"f-string 内复用同种引号 {quote!r}（3.11 不支持）"))
            stack.append((quote, tok.start[0]))
        elif tok.type == tokenize.FSTRING_END:
            if stack:
                stack.pop()
        elif tok.type == tokenize.STRING and stack:
            literal = tok.string
            quote = _quote_of(literal)
            if any(_reuses_quote(outer, quote) for outer, _ in stack):
                problems.append((tok.start[0], f"f-string 表达式内字符串复用同种引号 {quote!r}（3.11 不支持）"))
            if BACKSLASH in literal:
                problems.append((tok.start[0], "f-string 表达式内出现反斜杠（3.11 不支持）"))
        elif tok.type == tokenize.COMMENT and stack:
            problems.append((tok.start[0], "f-string 表达式内出现注释（3.11 不支持）"))
    return problems


@pytest.mark.skipif(
    not _HAS_PEP701_TOKENIZER,
    reason="需要 Python 3.12+ 的新版 f-string 分词器才能扫描兼容性",
)
def test_python_sources_support_python311():
    offenders: list[str] = []
    for path in _iter_python_files():
        for line, message in _scan(path):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {message}")
    assert not offenders, (
        "发现仅 Python 3.12+ 支持的 f-string 写法，CI 的 3.11 任务会在收集阶段失败：\n"
        + "\n".join(offenders)
    )
