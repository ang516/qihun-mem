"""v1.5-r2 guards：架构静态检查 + A 类绕开单点用例（规范 §10.8 / 附录 D1/D2/D3）。

注意：本文件只依赖已冻结的 parser / lifecycle / memory / retrieval 接口，
不依赖 pipeline/service/cli 的未冻结细节（T2 还在改）。

覆盖：
- A1：`update_memory` 是唯一状态写入口（静态 AST：无直接属性写、setattr 只此一处；
  动态 spy：reinforce / 惰性 dormant / maintain 都走 update_memory，spy 不落地时字段不被直接改）；
- A4：`user_affect.json` 路径只在 config 定义、只由 UserAffectStore.load/save 读写，
  UserAffectStore 只在 MemorySystem 里实例化；业务操作不得直接 IO 该文件；
- A5：`compute_S` 唯一公式（kv/ka 只在 lifecycle、compute_S 只被 lifecycle/memory 调用、
  非法 mode 抛 ValueError 且不被吞）；
- A6：发送层边界（`clean_answer_for_send` 只收 raw_answer、不接收 memories、
  不调 `_loads_candidate`；parser 不反向 import 编排层；无模块 import parser 下划线内部名）；
- A7：`fill` 预算完全跟随 constraints（total / head_per_item / subgroup.bridge），
  装填层不硬编码 300；
- dedup_key 三处单点（batch / 跨轮 / 跨池共享同一函数对象，且回归行为一致）。
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qihun_memory import memory as memory_mod
from qihun_memory import parser as parser_mod
from qihun_memory import retrieval as retrieval_mod
from qihun_memory.config import Config
from qihun_memory.embedding import get_embedder
from qihun_memory.memory import MemorySystem
from qihun_memory.metrics import Metrics
from qihun_memory.models import MemoryItem, Set, utc_now_iso
from qihun_memory.retrieval import (
    SOURCE_POOL_BRIDGE,
    SOURCE_POOL_DM,
    RetrievalResult,
    Retriever,
    SelectedMemory,
    fill,
)
from qihun_memory.tokens import estimate_tokens

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src" / "qihun_memory"

# 附录 D1 白名单字段（任何直接改这些属性的代码都是绕开 update_memory）
STATE_FIELDS = frozenset({"S", "status", "last_access", "access_count", "mention_count"})
AFFECT_FIELDS = frozenset({"affection", "annoyance", "familiarity"})

# parser 只能被这些编排/展示模块作为公共接口消费（且不允许下划线内部名）
PARSER_INTERNAL_FORBIDDEN = {"pipeline", "service", "memory", "retrieval", "cli", "tools"}

# dedup_key 的合法 importer：memory/retrieval 是实际使用方，tools.py 只做公共 API 再导出
DEDUP_KEY_IMPORTERS = frozenset({"memory.py", "retrieval.py", "tools.py"})
# 实际调用 dedup_key 的模块（tools.py 只 re-export，不得调用）
DEDUP_KEY_CALLERS = frozenset({"memory.py", "retrieval.py"})


# --------------------------------------------------------------------------
# AST 辅助
# --------------------------------------------------------------------------

def _src_files() -> list[Path]:
    return sorted(SRC_DIR.glob("*.py"))


def _parse_file(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _scoped_nodes(tree: ast.Module) -> list[tuple[ast.AST, tuple[str, ...]]]:
    """返回 (节点, 所属类/函数名元组) 列表；节点自身不包含在内。"""
    result: list[tuple[ast.AST, tuple[str, ...]]] = []

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scope = scope + (child.name,)
                result.append((child, child_scope))
                walk(child, child_scope)
            else:
                result.append((child, scope))
                walk(child, scope)

    walk(tree, ())
    return result


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _assignment_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return [node.target]
    return []


def _make_item(
    seq: int,
    content: str,
    *,
    scope: str = "dm",
    user_id: str = "u1",
    item_type: str = "fact",
) -> MemoryItem:
    now = utc_now_iso()
    return MemoryItem(
        id="m_" + str(seq).zfill(4),
        user_id=user_id,
        scope=scope,
        type=item_type,
        content=content,
        created_at=now,
        last_access=now,
    )


def _make_entry(
    item: MemoryItem,
    *,
    bridge: bool = False,
    score: float = 1.0,
    r: float = 1.0,
    sim: float = 1.0,
) -> SelectedMemory:
    return SelectedMemory(
        item=item,
        score=score,
        r=r,
        cos=sim,
        bm25=sim,
        sim=sim,
        bridge=bridge,
        source_pool=SOURCE_POOL_BRIDGE if bridge else SOURCE_POOL_DM,
    )


# ==========================================================================
# A1：update_memory 是唯一状态写入口
# ==========================================================================

UPDATE_MEMORY_SCOPE = "MemorySystem.update_memory"


def test_a1_state_field_writes_only_inside_update_memory() -> None:
    """A1 静态检查：S/status/last_access/access_count/mention_count 的属性写只允许在单点内。

    允许两种等价实现：update_memory 内部 setattr 或直接属性赋值；其它位置一律违规。
    """
    update_memory_sites = 0
    offenders: list[tuple[str, int, str, str]] = []
    setattr_offenders: list[tuple[str, int, str]] = []
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node, scope in _scoped_nodes(tree):
            qual = ".".join(scope)
            for target in _assignment_targets(node):
                if isinstance(target, ast.Attribute) and target.attr in STATE_FIELDS:
                    if path.name == "memory.py" and qual == UPDATE_MEMORY_SCOPE:
                        update_memory_sites += 1
                    elif path.name == "service.py" and qual == "ServiceError.__init__":
                        pass  # HTTP 状态码，不是记忆状态
                    else:
                        offenders.append((path.name, getattr(node, "lineno", 0), qual, target.attr))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr":
                if path.name == "memory.py" and qual == UPDATE_MEMORY_SCOPE:
                    update_memory_sites += 1
                else:
                    setattr_offenders.append((path.name, getattr(node, "lineno", 0), qual))
    assert offenders == [], "发现绕开 update_memory 的状态字段直写: " + repr(offenders)
    assert setattr_offenders == [], "发现绕开 update_memory 的 setattr: " + repr(setattr_offenders)
    assert update_memory_sites >= 1, "update_memory 内部应有实际写入，静态检查不能空跑通过"


def test_a1_update_memory_whitelist_fields() -> None:
    assert memory_mod._SET_FIELDS == frozenset({"S", "status", "last_access"})
    assert memory_mod._INC_FIELDS == frozenset({"access_count", "mention_count"})


def test_a1_public_state_mutations_go_through_update_memory(config, monkeypatch) -> None:
    system = MemorySystem(config)
    try:
        report = system.remember(
            [{"type": "fact", "content": "A1 状态写入口"}],
            user_id="u1",
            scope="dm",
            valence=0.0,
        )
        item = report.created[0]
        old_last_access = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat(
            timespec="seconds"
        )
        assert system.update_memory(item.id, Set("last_access", old_last_access), reason="setup")

        calls: list[tuple[str, list, str]] = []

        def spy(item_id, ops, *, reason=""):
            calls.append((str(item_id), list(ops) if not isinstance(ops, (Set, memory_mod.Inc)) else [ops], reason))
            return True

        monkeypatch.setattr(system, "update_memory", spy)
        before = item.to_dict()

        entry = _make_entry(item, sim=0.9)
        system.reinforce(RetrievalResult(selected=[entry], candidates=1, tokens=0, budget=1200, degraded=False))
        assert calls, "reinforce 必须经 update_memory"
        assert item.to_dict() == before, "spy 不落地时 reinforce 不得直接改条目字段"

        calls.clear()
        system.search("A1 状态写入口", "dm", "u1")
        assert calls, "惰性 dormant 判定必须经 update_memory"
        assert item.to_dict() == before, "spy 不落地时 search 不得直接改条目字段"

        calls.clear()
        system.maintain()
        assert calls, "maintain 的 status 写必须经 update_memory"
        assert item.to_dict() == before, "spy 不落地时 maintain 不得直接改条目字段"
    finally:
        system.close()


# ==========================================================================
# A4：user_affect.json 唯一读写入口
# ==========================================================================

def test_a4_user_affect_json_literal_only_in_config() -> None:
    files: set[str] = set()
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "user_affect.json":
                files.add(path.name)
    assert files == {"config.py"}, "路径字面量只允许在 config.user_affect_file 定义: " + repr(files)


def test_a4_user_affect_store_instantiated_only_in_memory_system() -> None:
    sites: list[tuple[str, str]] = []
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node, scope in _scoped_nodes(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "UserAffectStore":
                sites.append((path.name, ".".join(scope)))
    assert sites == [("memory.py", "MemorySystem.__init__")], sites


def test_a4_user_affect_file_attribute_used_only_to_construct_store() -> None:
    sites: list[tuple[str, str]] = []
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node, scope in _scoped_nodes(tree):
            if isinstance(node, ast.Attribute) and node.attr == "user_affect_file":
                sites.append((path.name, ".".join(scope)))
    # 只允许 MemorySystem.__init__ 里作为 UserAffectStore 的构造参数；config 里是属性定义（FunctionDef 名，不是 Attribute）
    assert sites == [("memory.py", "MemorySystem.__init__")], sites


def test_a4_no_direct_file_io_referencing_user_affect() -> None:
    offenders: list[tuple[str, int, str]] = []
    for path in _src_files():
        source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in {"open", "read_text", "write_text", "atomic_write_text"}:
                continue
            segment = ast.get_source_segment(source, node) or ""
            if "user_affect" in segment:
                offenders.append((path.name, node.lineno, segment[:80]))
    assert offenders == [], "不得绕开 UserAffectStore 直接 IO user_affect.json: " + repr(offenders)


def test_a4_state_py_path_io_only_in_directory_and_affect_store() -> None:
    source, tree = _parse_file(SRC_DIR / "state.py")
    sites: set[tuple[str, str]] = set()
    for node, scope in _scoped_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        segment = ast.get_source_segment(source, node) or ""
        if name in {"atomic_write_text", "open", "read_text", "write_text"} and "self.path" in segment:
            sites.add((name, ".".join(scope)))
    assert sites == {
        ("read_text", "UserDirectory.load"),
        ("atomic_write_text", "UserDirectory.save"),
        ("read_text", "UserAffectStore.load"),
        ("atomic_write_text", "UserAffectStore.save"),
    }, sites


# ==========================================================================
# A5：compute_S 唯一公式
# ==========================================================================

def test_a5_compute_S_defined_once_and_kv_ka_only_in_lifecycle() -> None:
    definitions: list[str] = []
    kvka_files: set[str] = set()
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "compute_S":
                definitions.append(path.name)
            if isinstance(node, ast.Attribute) and node.attr in {"kv", "ka"}:
                kvka_files.add(path.name)
    assert definitions == ["lifecycle.py"], definitions
    assert kvka_files == {"lifecycle.py"}, "k_v / k_a 只允许在 compute_S 出现: " + repr(kvka_files)


def test_a5_compute_S_callers_limited_to_lifecycle_and_memory() -> None:
    callers: set[str] = set()
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "compute_S":
                callers.add(path.name)
    assert callers <= {"lifecycle.py", "memory.py"}, callers
    assert {"lifecycle.py", "memory.py"} <= callers


def test_a5_lifecycle_does_not_swallow_value_error() -> None:
    _source, tree = _parse_file(SRC_DIR / "lifecycle.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                names = set()
                if handler.type is None:
                    names.add("bare_except")
                elif isinstance(handler.type, ast.Name):
                    names.add(handler.type.id)
                elif isinstance(handler.type, ast.Tuple):
                    names.update(elt.id for elt in handler.type.elts if isinstance(elt, ast.Name))
                assert "ValueError" not in names, "非法 mode 的 ValueError 不得被 lifecycle 吞掉"
                assert "bare_except" not in names


def test_a5_invalid_mode_raises_value_error() -> None:
    from qihun_memory.lifecycle import compute_S

    for mode in ("bogus", "", "INIT", "reinforce2"):
        with pytest.raises(ValueError):
            compute_S(mode, "episode")
    with pytest.raises(ValueError):
        compute_S("reinforce", "episode")


# ==========================================================================
# A6：解析层 / 发送层分离（附录 D2）
# ==========================================================================

def test_a6_no_module_imports_parser_internal_names() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _src_files():
        if path.name == "parser.py":
            continue
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[-1] == "parser":
                for alias in node.names:
                    if alias.name.startswith("_"):
                        offenders.append((path.name, alias.name))
    assert offenders == [], "不得 import parser 的下划线内部实现: " + repr(offenders)


def test_a6_parser_does_not_import_orchestration_layers() -> None:
    _source, tree = _parse_file(SRC_DIR / "parser.py")
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[-1] for alias in node.names)
    forbidden = imports & PARSER_INTERNAL_FORBIDDEN
    assert forbidden == set(), "parser 不得反向依赖编排层: " + repr(forbidden)


def test_a6_clean_answer_for_send_signature_takes_only_raw_answer() -> None:
    signature = inspect.signature(parser_mod.clean_answer_for_send)
    parameters = list(signature.parameters.values())
    assert [parameter.name for parameter in parameters] == ["raw_answer"]
    assert len(parameters) == 1
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters[0].default is inspect.Parameter.empty


def test_a6_clean_answer_for_send_never_touches_memories() -> None:
    _source, tree = _parse_file(SRC_DIR / "parser.py")
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "clean_answer_for_send"
    )
    body = [
        statement
        for statement in function.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    names: set[str] = set()
    calls: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Call):
                calls.add(_call_name(node))
    assert "memories" not in names
    assert "memory" not in names
    assert calls.isdisjoint({"parse_memories", "_loads_candidate"}), calls


def test_a6_all_clean_answer_for_send_calls_pass_single_argument() -> None:
    offenders: list[tuple[str, int]] = []
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "clean_answer_for_send":
                if len(node.args) != 1 or node.keywords:
                    offenders.append((path.name, node.lineno))
    assert offenders == [], "发送层唯一清洗点只接收 raw_answer: " + repr(offenders)


# ==========================================================================
# A7：fill 预算跟随 constraints（不硬编码 total / head / bridge）
# ==========================================================================

def test_a7_fill_returns_documented_shape() -> None:
    result = fill([], None)
    assert set(result) == {"injected_items", "truncated", "total_tokens"}
    assert result == {"injected_items": [], "truncated": False, "total_tokens": 0}


def test_a7_fill_does_not_hardcode_total_or_head() -> None:
    """constraints 缺省 = 不设总上限、head=0；装填层不得兜底 1200 / 24。"""
    entries = [_make_entry(_make_item(i, "长" * 200)) for i in range(10)]  # 10 * 200 = 2000 tokens
    result = fill(entries, None)
    assert len(result["injected_items"]) == 10
    assert result["total_tokens"] == 2000
    assert result["truncated"] is False
    assert all(item["header_tokens"] == 0 for item in result["injected_items"])


def test_a7_fill_head_per_item_follows_constraints() -> None:
    entry = _make_entry(_make_item(1, "测试内容测试内容"))  # 8 CJK -> 8 tokens
    content_tokens = estimate_tokens(entry.item.content)
    for head in (0, 3, 24, 40):
        result = fill([entry], {"total": 1000, "head_per_item": head})
        injected = result["injected_items"][0]
        assert injected["header_tokens"] == head
        assert injected["tokens"] == head + content_tokens
        assert result["total_tokens"] == head + content_tokens


def test_a7_fill_total_budget_follows_constraints() -> None:
    head = 24
    entries = [_make_entry(_make_item(i, "总预算内容十个字")) for i in range(3)]
    cost = head + estimate_tokens(entries[0].item.content)
    budget = cost * 2 + cost // 2  # 只放得下 2 条
    metrics = Metrics()
    result = fill(entries, {"total": budget, "head_per_item": head}, metrics=metrics)
    assert len(result["injected_items"]) == 2
    assert result["total_tokens"] == cost * 2
    assert result["total_tokens"] <= budget
    assert result["truncated"] is True
    assert metrics.count("retrieval.truncated") == 1

    generous = fill(entries, {"total": 1000, "head_per_item": head})
    assert len(generous["injected_items"]) == 3
    assert generous["truncated"] is False


def test_a7_bridge_budget_follows_constraints_not_hardcoded_300() -> None:
    head = 24
    entries = [_make_entry(_make_item(i, "桥接记忆桥接记忆"), bridge=True) for i in range(10)]
    cost = head + estimate_tokens(entries[0].item.content)  # 24+8=32

    tight = fill(entries, {"total": 1000, "head_per_item": head, "subgroup": {"bridge": 120}})
    tight_kept = len(tight["injected_items"])
    assert tight_kept == 120 // cost
    assert sum(item["tokens"] for item in tight["injected_items"]) <= 120
    assert tight["truncated"] is True

    loose = fill(entries, {"total": 1000, "head_per_item": head, "subgroup": {"bridge": 300}})
    assert len(loose["injected_items"]) == 300 // cost
    assert len(loose["injected_items"]) > tight_kept

    no_subgroup = fill(entries, {"total": 1000, "head_per_item": head})
    assert len(no_subgroup["injected_items"]) == len(entries), "constraints 缺省时不得兜底成 300"
    assert no_subgroup["truncated"] is False


def test_a7_fill_has_no_hardcoded_budget_constants() -> None:
    """A7 静态检查：装填路径源码里不得出现 300 / 1200 这类硬编码预算。"""
    _source, tree = _parse_file(SRC_DIR / "retrieval.py")
    function_names = {"fill", "_fill_entries", "_resolve_constraints", "_injected_item"}
    offenders: list[tuple[str, int, int]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef) or function.name not in function_names:
            continue
        docstring_nodes = set()
        if (
            function.body
            and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)
        ):
            docstring_nodes.add(id(function.body[0].value))
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
                if id(node) in docstring_nodes:
                    continue
                if node.value in {300, 1200}:
                    offenders.append((function.name, node.lineno, node.value))
    assert offenders == [], "装填层出现硬编码预算: " + repr(offenders)


def test_a7_bridge_budget_zero_skips_all_bridge() -> None:
    head = 24
    bridge_entries = [_make_entry(_make_item(10 + i, "桥接记忆"), bridge=True) for i in range(3)]
    dm_entries = [_make_entry(_make_item(20 + i, "私聊记忆")) for i in range(2)]
    result = fill(
        dm_entries + bridge_entries,
        {"total": 1000, "head_per_item": head, "subgroup": {"bridge": 0}},
    )
    assert len(result["injected_items"]) == len(dm_entries)
    assert all(item["bridge"] is False for item in result["injected_items"])
    assert result["truncated"] is True


def test_a7_non_bridge_entries_do_not_consume_bridge_budget() -> None:
    head = 24
    dm_entries = [_make_entry(_make_item(30 + i, "私聊记忆")) for i in range(2)]
    bridge_entries = [_make_entry(_make_item(40 + i, "桥接记忆"), bridge=True) for i in range(5)]
    bridge_budget = 120
    cost = head + estimate_tokens("桥接记忆")
    result = fill(
        dm_entries + bridge_entries,
        {"total": 1000, "head_per_item": head, "subgroup": {"bridge": bridge_budget}},
    )
    kept_bridge = [item for item in result["injected_items"] if item["bridge"]]
    assert len(kept_bridge) == bridge_budget // cost
    assert sum(item["tokens"] for item in kept_bridge) <= bridge_budget
    assert len(result["injected_items"]) == len(dm_entries) + len(kept_bridge)


def test_a7_bridge_truncated_metric_recorded_once() -> None:
    metrics = Metrics()
    head = 24
    entries = [_make_entry(_make_item(50 + i, "桥接记忆桥接记忆"), bridge=True) for i in range(10)]
    fill(
        entries,
        {"total": 1000, "head_per_item": head, "subgroup": {"bridge": 120}},
        metrics=metrics,
    )
    assert metrics.count("group_bridge.truncated") == 1


def test_a7_injected_items_carry_pool_and_tokens() -> None:
    head = 24
    entries = [
        _make_entry(_make_item(60, "私聊记忆"), bridge=False),
        _make_entry(_make_item(61, "桥接记忆"), bridge=True),
    ]
    result = fill(entries, {"total": 1000, "head_per_item": head, "subgroup": {"bridge": 300}})
    assert len(result["injected_items"]) == 2
    by_pool = {item["source_pool"]: item for item in result["injected_items"]}
    assert set(by_pool) == {SOURCE_POOL_DM, SOURCE_POOL_BRIDGE}
    assert by_pool[SOURCE_POOL_BRIDGE]["bridge"] is True
    assert by_pool[SOURCE_POOL_DM]["bridge"] is False
    for item in result["injected_items"]:
        assert isinstance(item["tokens"], int) and item["tokens"] > 0


def test_a7_fill_rejects_raw_dict_entries() -> None:
    with pytest.raises(TypeError):
        fill([{"content": "不是 SelectedMemory"}], {"total": 1000})


# ==========================================================================
# dedup_key 三处单点：batch / 跨轮 / 跨池
# ==========================================================================

def test_dedup_key_defined_only_in_parser() -> None:
    definitions: list[tuple[str, str]] = []
    for path in _src_files():
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "dedup" in node.name and "key" in node.name:
                definitions.append((path.name, node.name))
    assert definitions == [("parser.py", "dedup_key")], definitions


def test_dedup_key_imported_only_by_known_consumers() -> None:
    importers: set[str] = set()
    for path in _src_files():
        if path.name == "parser.py":
            continue
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[-1] == "parser":
                if any(alias.name == "dedup_key" for alias in node.names):
                    importers.add(path.name)
    assert importers == DEDUP_KEY_IMPORTERS, importers


def test_dedup_key_called_only_by_memory_and_retrieval() -> None:
    """除 parser 自身外，只有 memory / retrieval 真正调用 dedup_key（tools.py 只再导出）。"""
    callers: set[str] = set()
    for path in _src_files():
        if path.name == "parser.py":
            continue
        _source, tree = _parse_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dedup_key":
                callers.add(path.name)
    assert callers == DEDUP_KEY_CALLERS, callers


def test_dedup_key_is_same_function_object_everywhere() -> None:
    from qihun_memory import tools as tools_mod

    assert memory_mod.dedup_key is parser_mod.dedup_key
    assert retrieval_mod.dedup_key is parser_mod.dedup_key
    assert tools_mod.dedup_key is parser_mod.dedup_key


def test_batch_dedup_goes_through_dedup_key(monkeypatch) -> None:
    real = parser_mod.dedup_key
    calls: list[dict] = []

    def wrapped(item):
        calls.append(item)
        return real(item)

    monkeypatch.setattr(parser_mod, "dedup_key", wrapped)
    parser_mod.dedup_within_batch(
        [{"type": "fact", "content": "批内键"}, {"type": "fact", "content": "批内键"}]
    )
    assert len(calls) == 2


def test_cross_turn_dedup_goes_through_dedup_key(config, monkeypatch) -> None:
    real = memory_mod.dedup_key
    calls: list[dict] = []

    def wrapped(item):
        calls.append(item)
        return real(item)

    monkeypatch.setattr(memory_mod, "dedup_key", wrapped)
    system = MemorySystem(config)
    try:
        report = system.remember(
            [
                {"type": "fact", "content": "ＡＢＣ跨轮去重"},
                {"type": "fact", "content": "abc跨轮去重"},
            ],
            user_id="u1",
            scope="dm",
            valence=0.0,
        )
        assert [item.content for item in report.created] == ["ＡＢＣ跨轮去重"]
        assert len(report.merged) == 1
        assert calls, "跨轮去重必须走 parser.dedup_key，禁止自行拼键"
    finally:
        system.close()


def test_cross_pool_dedup_goes_through_dedup_key(config, monkeypatch) -> None:
    metrics = Metrics()
    real = retrieval_mod.dedup_key
    calls: list[dict] = []

    def wrapped(item):
        calls.append(item)
        return real(item)

    monkeypatch.setattr(retrieval_mod, "dedup_key", wrapped)
    retriever = Retriever(get_embedder(config.embedding), config, metrics)
    group_item = _make_item(70, "ＡＢＣ群里同事实", scope="group", user_id="u1")
    dm_item = _make_item(71, "abc群里同事实", scope="dm", user_id="u1")
    result = retriever.search(
        [group_item, dm_item],
        "群里刚才说的那件事",
        scope="dm",
        user_id="u1",
    )
    assert result.dedup_removed == 1
    assert metrics.count("group_bridge.dedup") == 1
    assert calls, "跨池去重必须走 parser.dedup_key，禁止自行拼键"