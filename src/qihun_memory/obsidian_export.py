"""器魂记忆 → Obsidian 只读视图导出（单向：记忆系统 → Markdown，不回读）。

设计约束（与 v1.4「Obsidian 仅只读」一致）：
- 记忆系统运行时不写 Obsidian；本模块是**显式的导出工具**，由 CLI / kqq-bridge
  在需要时调用，生成一份给人看的只读镜像；
- 导出的 Markdown 只读取记忆数据，不参与检索/写入；Obsidian 里的修改不会回流；
- 幂等：数据没变就不写文件，避免 Obsidian 反复重载；
- 输出目录不能是 data_dir 本身或其子目录（防止覆盖权威 JSONL）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .fsutil import atomic_write_text
from .lifecycle import retention_now
from .memory import MemorySystem
from .models import MemoryItem, parse_iso, utc_now
from .summary import RollingSummary

DEFAULT_SUBDIR = "器魂记忆"
VIEW_NAMES = (
    "00_器魂记忆说明.md",
    "10_群聊记忆.md",
    "11_私聊记忆.md",
    "20_滚动摘要.md",
    "30_用户与状态.md",
    "40_已归档记忆.md",
)


class ExportError(RuntimeError):
    """导出参数/路径错误。"""


@dataclass
class ExportReport:
    out_dir: str
    written: int = 0
    updated: int = 0
    unchanged: int = 0
    items: int = 0
    archived: int = 0
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "out_dir": self.out_dir,
            "written": self.written,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "items": self.items,
            "archived": self.archived,
            "files": list(self.files),
        }


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _human_time(value: str) -> str:
    try:
        dt = parse_iso(value)
    except (ValueError, TypeError):
        return str(value or "")
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _esc_cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _fmt_num(value: float) -> str:
    return f"{float(value):.2f}"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _write_if_changed(path: Path, text: str, report: ExportReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current == text:
        report.unchanged += 1
        return
    atomic_write_text(path, text)
    report.files.append(path.name)
    if current is None:
        report.written += 1
    else:
        report.updated += 1


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def _header(frontmatter_type: str, snapshot: str) -> str:
    return (
        "---\n"
        f"type: {frontmatter_type}\n"
        "source: qihun-memory-v1.4\n"
        "readonly: true\n"
        f"snapshot: {snapshot}\n"
        "---\n\n"
    )


def _snapshot_text(items: Iterable[MemoryItem], state_updated: str) -> str:
    stamps: list[str] = []
    for item in items:
        stamps.extend([item.created_at, item.last_access])
    if state_updated:
        stamps.append(state_updated)
    if not stamps:
        return "空"
    return max(stamps)


def _render_memory(item: MemoryItem, users, now: datetime) -> str:
    nickname = users.nickname(item.user_id) if users is not None else item.user_id
    r = retention_now(item, now=now, avoidance_now=0.0, cfg=None)
    tags = "、".join(f"`{t}`" for t in item.tags) or "无"
    lines = [
        f"### {item.id} · {_human_time(item.created_at)} · {item.type}",
        "",
        f"- 归属：{nickname}（{item.user_id}）",
        f"- 状态：{item.status} ｜ R {_fmt_num(r)} ｜ S {_fmt_num(item.S)} ｜ 提及 {item.mention_count} 次",
        f"- 标签：{tags}",
        "",
        item.content,
        "",
    ]
    return "\n".join(lines)


def _render_scope(items: list[MemoryItem], users, now: datetime, title: str) -> str:
    snapshot = _snapshot_text(items, "")
    body = [_header("qihun-memory-view", snapshot), f"# {title}（{len(items)} 条）", ""]
    if not items:
        body += ["> 暂无记忆。", ""]
        return "\n".join(body)
    groups: dict[str, list[MemoryItem]] = {}
    for item in sorted(items, key=lambda x: (x.created_at, x.id), reverse=True):
        day = item.created_at[:10]
        groups.setdefault(day, []).append(item)
    for day in sorted(groups, reverse=True):
        body += [f"## {day}", ""]
        for item in groups[day]:
            body.append(_render_memory(item, users, now))
    return "\n".join(body)


def _render_summary(path: Path) -> str:
    snapshot = "空"
    conversations: dict[str, list[str]] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                snapshot = str(raw.get("updated_at") or "空")
                for key, value in (raw.get("conversations") or {}).items():
                    units = value.get("units") if isinstance(value, dict) else None
                    if isinstance(units, list):
                        conversations[str(key)] = [str(u) for u in units if str(u).strip()]
        except (OSError, ValueError):
            conversations = {}
    body = [_header("qihun-memory-summary", snapshot), "# 滚动摘要", ""]
    if not conversations:
        body += ["> 暂无摘要（还没有聊天积累到摘要预算）。", ""]
        return "\n".join(body)
    labels = {"group": "群聊", "dm": "私聊"}
    for key in sorted(conversations):
        units = conversations[key]
        label = labels.get(key, key)
        if key.startswith("dm:"):
            label = f"私聊 {key[3:]}"
        body += [f"## {label}（{len(units)} 行）", ""]
        for unit in units:
            body.append(f"- {unit}")
        body.append("")
    return "\n".join(body)


def _render_users_state(system: MemorySystem, report: ExportReport) -> str:
    state = system.state()
    counts = system.store.counts()
    users = system.users.snapshot()
    snapshot = state.updated_at or _snapshot_text(system.all_items(), "")
    body = [_header("qihun-memory-state", snapshot), "# 用户与状态", ""]
    body += ["## 统计", ""]
    body += [
        f"- 记忆总数：{counts.get('total', 0)}（active {counts.get('active', 0)} / dormant {counts.get('dormant', 0)}）",
        f"- 归档：{report.archived} 条（见 [[40_已归档记忆]]）",
        f"- embedding：{"可用" if getattr(system.embedder, 'available', False) else "不可用（纯 BM25 降级）"}",
        "",
        "## 昵称快照（users.json）",
        "",
    ]
    if users:
        body += ["| user_id | 昵称 |", "| --- | --- |"]
        for user_id in sorted(users):
            body.append(f"| {_esc_cell(user_id)} | {_esc_cell(users[user_id])} |")
    else:
        body.append("> 暂无昵称快照。")
    body += [
        "",
        "## 情感状态（state.json，记忆系统只读）",
        "",
        f"- valence：{_fmt_num(state.valence)}",
        f"- avoidance：{_fmt_num(state.avoidance)}",
        f"- 更新时间：{_human_time(state.updated_at) if state.updated_at else '（缺失）'}",
        f"- 数据源：{state.source}" + (f"（{state.reason}）" if state.reason else ""),
        "",
    ]
    return "\n".join(body)


def _read_archived(cfg: Config) -> list[MemoryItem]:
    path = cfg.archive_file
    items: list[MemoryItem] = []
    if not path.is_file():
        return items
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    items.append(MemoryItem.from_dict(json.loads(line)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    return items


def _render_archived(items: list[MemoryItem], users, now: datetime) -> str:
    snapshot = _snapshot_text(items, "")
    body = [_header("qihun-memory-archive", snapshot), f"# 已归档记忆（{len(items)} 条）", ""]
    body += [
        "> 归档 = R 低于阈值、不再参与检索的事件；原始数据仍在 memory.archive.jsonl，不物理删除。",
        "",
    ]
    if not items:
        body.append("> 暂无归档。")
        return "\n".join(body)
    for item in sorted(items, key=lambda x: (x.created_at, x.id), reverse=True):
        body.append(_render_memory(item, users, now))
    return "\n".join(body)


def _render_readme(cfg: Config, report: ExportReport, snapshot: str, counts: dict[str, int]) -> str:
    body = [_header("qihun-memory-readme", snapshot), "# 器魂记忆 · 只读视图", ""]
    body += [
        "> 本目录由**器魂记忆系统 v1.4** 自动导出，只读镜像；**不要手改**，修改不会回流到记忆数据。",
        "> 权威数据在 qq-bridge 的 `state/qihun-memory/data/`（memory.jsonl / summary.json / users.json / state.json）。",
        "",
        "## 怎么看",
        "",
        "- [[10_群聊记忆]]：群聊（scope=group）的全部记忆，按日期倒序",
        "- [[11_私聊记忆]]：私聊（scope=dm）的全部记忆",
        "- [[20_滚动摘要]]：每轮对话的滚动摘要",
        "- [[30_用户与状态]]：昵称快照、情感状态、统计",
        "- [[40_已归档记忆]]：已移档、不再参与检索的记忆",
        "",
        "## 当前快照",
        "",
        f"- 数据快照：{_human_time(snapshot) if snapshot != '空' else '空'}",
        f"- 记忆总数：{counts.get('total', 0)}（active {counts.get('active', 0)} / dormant {counts.get('dormant', 0)}）",
        f"- 归档：{report.archived} 条",
        f"- 仓库：https://github.com/ang516/qihun-mem",
        "",
        "> 说明：Obsidian 仅作为**只读视图**；记忆系统不读取本目录，也不会把这里的编辑写回记忆。",
        "",
    ]
    return "\n".join(body)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def export_obsidian_view(
    cfg: Config,
    out_dir: str | Path,
    *,
    now: datetime | None = None,
) -> ExportReport:
    """把当前记忆数据导出为 Obsidian 只读视图。

    - `out_dir` 不能是 data_dir 或其子目录；
    - 幂等：内容未变的文件不重写；
    - 返回各文件写入统计。
    """
    target = Path(out_dir).expanduser().resolve()
    data_dir = Path(cfg.data_path).resolve()
    if target == data_dir or _is_within(target, data_dir):
        raise ExportError(f"导出目录不能位于数据目录内：{target}")
    report = ExportReport(out_dir=str(target))
    current = now or utc_now()

    system = MemorySystem(cfg, read_only=True)
    try:
        items = system.all_items()
        archived = _read_archived(cfg)
        report.items = len(items)
        report.archived = len(archived)
        counts = system.store.counts()
        snapshot = _snapshot_text(items, system.state().updated_at)
        report_files = {
            "00_器魂记忆说明.md": _render_readme(cfg, report, snapshot, counts),
            "10_群聊记忆.md": _render_scope(
                [item for item in items if item.scope == "group"], system.users, current, "群聊记忆"
            ),
            "11_私聊记忆.md": _render_scope(
                [item for item in items if item.scope == "dm"], system.users, current, "私聊记忆"
            ),
            "20_滚动摘要.md": _render_summary(cfg.summary_file),
            "30_用户与状态.md": _render_users_state(system, report),
            "40_已归档记忆.md": _render_archived(archived, system.users, current),
        }
        for name in VIEW_NAMES:
            _write_if_changed(target / name, report_files[name], report)
    finally:
        system.close()
    return report


def format_export_report(report: ExportReport) -> str:
    return (
        f"Obsidian 只读视图已导出：{report.out_dir}\n"
        f"  记忆 {report.items} 条（归档 {report.archived} 条）\n"
        f"  文件：新建 {report.written} / 更新 {report.updated} / 未变 {report.unchanged}"
    )
