"""记忆生命周期与系统门面（v1.4 §4 / §5 / §6 / §7 步骤 8-9）。

职责：
- 写入：去重 → S₀ → 先追加 JSONL 再更新内存；失败进 pending，下一轮重试一次（§4.2.5）；
- 遗忘：惰性计算 R、dormant 翻转、强匹配复活、档案移动（§5 / §8）；
- 强化：被选中注入的条目 S·(1+R)，cap 90（§5.3）；
- 检索：委托 retrieval.Retriever（先过滤 scope + user_id，再打分）；
- 维护：每日重写、R<0.02 移档、清理调试日志（§2.4 / §8 / §12.2）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .embedding import Embedder, get_embedder
from .fsutil import atomic_write_text
from .lifecycle import (
    initial_stability,
    reinforce_stability,
    retention_now,
    revive_stability,
)
from .metrics import Metrics
from .models import ALLOWED_SCOPES, ALLOWED_TYPES, MemoryItem, parse_iso, utc_now, utc_now_iso
from .retrieval import RetrievalResult, Retriever
from .state import BotState, UserDirectory, load_bot_state
from .store import MemoryStore

log = logging.getLogger("memory")


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


@dataclass
class WriteReport:
    created: list[MemoryItem] = field(default_factory=list)
    merged: list[MemoryItem] = field(default_factory=list)
    skipped: int = 0
    failed: int = 0
    pending: int = 0
    turn_duplicate: bool = False

    def counts(self) -> dict[str, int]:
        return {
            "created": len(self.created),
            "merged": len(self.merged),
            "skipped": self.skipped,
            "failed": self.failed,
            "pending": self.pending,
        }


@dataclass
class MaintenanceReport:
    archived: int = 0
    dormant: int = 0
    rewritten: bool = False
    pending_retried: int = 0
    pending_dropped: int = 0
    debug_raw_cleaned: int = 0


@dataclass
class _PendingWrite:
    item: MemoryItem


class MemorySystem:
    """单实例记忆系统门面。"""

    def __init__(
        self,
        cfg: Config,
        *,
        metrics: Metrics | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
        self.metrics = metrics if metrics is not None else Metrics(
            cfg.metrics_file if cfg.metrics.enabled else None,
            enabled=cfg.metrics.enabled,
        )
        self.embedder: Embedder = embedder if embedder is not None else get_embedder(cfg.embedding)
        self.store = MemoryStore(cfg, self.metrics)
        self.store.load()
        self.users = UserDirectory(cfg.users_file)
        self.retriever = Retriever(self.embedder, cfg, self.metrics)
        self._pending: list[_PendingWrite] = []

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    @property
    def items(self) -> dict[str, MemoryItem]:
        return self.store.items

    def all_items(self) -> list[MemoryItem]:
        return list(self.store.items.values())

    def get_item(self, item_id: str) -> MemoryItem | None:
        return self.store.items.get(item_id)

    def state(self, now: datetime | None = None) -> BotState:
        return load_bot_state(
            self.cfg.bot_state_file,
            cfg=self.cfg.state,
            now=now,
            metrics=self.metrics,
        )

    def persona(self) -> str:
        """persona.md 只读（可能来自 data_dir 或只读 Obsidian 库）。"""
        path = self.cfg.persona_file
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""

    def refresh_users(self, members: dict[str, str]) -> int:
        return self.users.refresh(members)

    def stats(self) -> dict[str, Any]:
        state = self.state()
        return {
            "data_dir": str(self.cfg.data_path),
            "vault_dir": str(self.cfg.vault_path) if self.cfg.vault_path else "",
            "counts": self.store.counts(),
            "files": self.store.file_sizes(),
            "state": {
                "valence": state.valence,
                "avoidance": state.avoidance,
                "stale": state.stale,
                "source": state.source,
            },
            "embedder": {
                "name": getattr(self.embedder, "name", "unknown"),
                "dim": getattr(self.embedder, "dim", 0),
                "available": bool(self.embedder.available),
            },
            "users": len(self.users.snapshot()),
            "pending_writes": len(self._pending),
            "metrics": self.metrics.counters(),
        }

    # ------------------------------------------------------------------
    # 向量（§6.5）
    # ------------------------------------------------------------------

    def _encode(self, content: str) -> list[float] | None:
        if not self.embedder.available:
            return None
        try:
            vectors = self.embedder.encode([content])
        except Exception as exc:  # 任何 embedding 异常都只降级，不影响主流程
            self.metrics.event("memory.embed_fail", exc_type=type(exc).__name__)
            return None
        if not vectors:
            return None
        vector = vectors[0]
        expected = getattr(self.embedder, "dim", 0)
        if expected and len(vector) != expected:
            self.metrics.event("memory.embed_bad_dim", expected=expected, actual=len(vector))
            return None
        return list(vector)

    def ensure_vectors(self, limit: int = 64) -> int:
        """补算缺失向量（补算前该条不参与语义分，§6.5）。"""
        if not self.embedder.available:
            return 0
        missing = [item for item in self.store.items.values() if not item.vector][:limit]
        if not missing:
            return 0
        try:
            vectors = self.embedder.encode([item.content for item in missing])
        except Exception as exc:
            self.metrics.event("memory.embed_fail", exc_type=type(exc).__name__, batch=len(missing))
            return 0
        added = 0
        expected = getattr(self.embedder, "dim", 0) or len(vectors[0] if vectors else [])
        for item, vector in zip(missing, vectors):
            if expected and len(vector) != expected:
                continue
            item.vector = list(vector)
            if self.store.append_vector(item):
                added += 1
        return added

    # ------------------------------------------------------------------
    # 检索（§6 / §7 步骤 3）
    # ------------------------------------------------------------------

    def _scope_items(self, scope: str, user_id: str) -> list[MemoryItem]:
        if scope == "group":
            return [item for item in self.store.items.values() if item.scope == "group"]
        if scope == "dm":
            return [
                item
                for item in self.store.items.values()
                if item.scope == "dm" and item.user_id == user_id
            ]
        return []

    def _refresh_statuses(
        self,
        scope: str,
        user_id: str,
        bot_state: BotState,
        now: datetime,
    ) -> int:
        """惰性归档：R < 0.1 → dormant（只翻内存状态，文件不动）。"""
        changed = 0
        for item in self._scope_items(scope, user_id):
            if item.status != "active":
                continue
            r = retention_now(item, now=now, avoidance_now=bot_state.avoidance, cfg=self.cfg.decay)
            if r < self.cfg.decay.dormant_r:
                item.status = "dormant"
                changed += 1
        if changed:
            self.metrics.event("memory.dormant", count=changed)
        return changed

    def search(
        self,
        query: str,
        scope: str,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> RetrievalResult:
        """纯检索：不强化（强化只在「被选中并注入」之后由 reinforce 执行）。"""
        current = now or utc_now()
        bot_state = self.state(current)
        self.ensure_vectors()
        self._refresh_statuses(scope, user_id, bot_state, current)
        return self.retriever.search(
            self.store.items.values(),
            query,
            scope=scope,
            user_id=user_id,
            bot_state=bot_state,
            now=current,
        )

    def reinforce(self, result: RetrievalResult, *, now: datetime | None = None) -> int:
        """§5.3 / §5.1：被注入的记忆强化；dormant 强匹配复活（S 重置为 S₀）。"""
        current = now or utc_now()
        now_iso = current.isoformat(timespec="seconds")
        changed = 0
        for entry in result.selected:
            item = entry.item
            if item.status == "dormant" and entry.sim > self.cfg.decay.revive_sim:
                item.S = revive_stability(item.type, self.cfg.decay)
                item.status = "active"
                item.last_access = now_iso
                item.access_count += 1
                self.metrics.event("memory.revived", reason="strong_match", item_id=item.id)
                changed += 1
                continue
            if entry.sim > self.cfg.decay.reinforce_sim:
                item.S = reinforce_stability(item.S, entry.r, self.cfg.decay)
                item.last_access = now_iso
                item.access_count += 1
                self.metrics.event("memory.reinforced", item_id=item.id, S=round(item.S, 6))
                changed += 1
        return changed

    # ------------------------------------------------------------------
    # 写入（§4.2.8 / §4.3 / §4.2.5）
    # ------------------------------------------------------------------

    def _validate_extracted(self, raw: Any) -> tuple[str, str, list[str]] | None:
        if not isinstance(raw, dict):
            return None
        item_type = str(raw.get("type") or "")
        content = raw.get("content")
        if item_type not in ALLOWED_TYPES or not isinstance(content, str):
            return None
        cleaned = "".join(ch for ch in content if ch >= " " and ch != chr(127)).strip()
        cleaned = cleaned[: self.cfg.extraction.max_content]
        if not cleaned:
            return None
        tags_raw = raw.get("tags")
        tags: list[str] = []
        if isinstance(tags_raw, list):
            for tag in tags_raw:
                text = str(tag).strip()
                if text:
                    tags.append(text[: self.cfg.extraction.max_tag_len])
                if len(tags) >= self.cfg.extraction.max_tags:
                    break
        return item_type, cleaned, tags

    def _find_duplicate(
        self,
        user_id: str,
        scope: str,
        item_type: str,
        content: str,
        new_vector: list[float] | None,
    ) -> MemoryItem | None:
        """§4.2.8：同 user_id + scope + type 内，cos > 0.9 视为同一条。"""
        candidates = [
            item
            for item in self.store.items.values()
            if item.user_id == user_id and item.scope == scope and item.type == item_type
        ]
        if not candidates:
            return None
        if self.embedder.available and new_vector is not None:
            best: MemoryItem | None = None
            best_sim = -1.0
            for candidate in candidates:
                if not candidate.vector or len(candidate.vector) != len(new_vector):
                    continue
                sim = _dot(new_vector, candidate.vector)
                if sim > best_sim:
                    best, best_sim = candidate, sim
            if best is not None:
                return best if best_sim > self.cfg.decay.dedupe_sim else None
            self.metrics.event("memory.dedupe_degraded", reason="no_vectors")
        target = content.strip()
        for candidate in candidates:
            if candidate.content.strip() == target:
                return candidate
        return None

    def remember(
        self,
        extracted: Iterable[dict],
        *,
        user_id: str,
        scope: str,
        valence: float,
        turn_key: str = "",
        now: datetime | None = None,
    ) -> WriteReport:
        """写入提取结果。任何写盘失败都不抛异常（§4.2.5）。"""
        current = now or utc_now()
        now_iso = current.isoformat(timespec="seconds")
        report = WriteReport()
        items_in = list(extracted)
        if not str(user_id).strip() or scope not in ALLOWED_SCOPES:
            report.skipped = len(items_in)
            self.metrics.event("mem_write.invalid", reason="missing_user_or_scope")
            return report
        if turn_key and self.store.has_turn_key(turn_key):
            report.turn_duplicate = True
            report.skipped = len(items_in)
            self.metrics.event("mem_write.turn_dup", turn_key=turn_key)
            return report

        self.retry_pending(now=current)
        self.ensure_vectors()

        for raw in items_in:
            parsed = self._validate_extracted(raw)
            if parsed is None:
                report.skipped += 1
                self.metrics.event("mem_write.skipped", reason="invalid_item")
                continue
            item_type, content, tags = parsed
            new_vector = self._encode(content)
            duplicate = self._find_duplicate(user_id, scope, item_type, content, new_vector)
            if duplicate is not None:
                duplicate.last_access = now_iso
                duplicate.mention_count += 1
                if duplicate.status == "dormant":
                    duplicate.S = revive_stability(duplicate.type, self.cfg.decay)
                    duplicate.status = "active"
                    self.metrics.event("memory.revived", reason="dedupe", item_id=duplicate.id)
                report.merged.append(duplicate)
                self.metrics.event("memory.deduped", item_id=duplicate.id, type=item_type)
                continue

            item = MemoryItem(
                id=self.store.next_id(),
                user_id=str(user_id),
                scope=scope,
                type=item_type,
                content=content,
                created_at=now_iso,
                last_access=now_iso,
                access_count=0,
                mention_count=1,
                S=initial_stability(item_type, valence, self.cfg.decay),
                valence=float(valence),
                status="active",
                tags=tags,
                turn_key=turn_key or None,
                vector=new_vector,
            )
            try:
                self.store.append(item)
            except OSError as exc:
                report.failed += 1
                self.metrics.event("mem_write.fail", exc_type=type(exc).__name__)
                self._pending.append(_PendingWrite(item=item))
                continue
            if new_vector:
                self.store.append_vector(item)
            report.created.append(item)
            self.metrics.event(
                "memory.created",
                item_id=item.id,
                type=item_type,
                scope=scope,
                S=round(item.S, 3),
            )

        report.pending = len(self._pending)
        return report

    def retry_pending(self, *, now: datetime | None = None) -> dict[str, int]:
        """§4.2.5：失败条目下一轮重试 1 次；仍失败则丢弃并记 mem_write.fail。"""
        if not self._pending:
            return {"retried": 0, "written": 0, "dropped": 0}
        pending = self._pending
        self._pending = []
        written = 0
        dropped = 0
        for entry in pending:
            try:
                self.store.append(entry.item)
            except OSError as exc:
                dropped += 1
                self.metrics.event("mem_write.fail", exc_type=type(exc).__name__, reason="retry_dropped")
                continue
            if entry.item.vector:
                self.store.append_vector(entry.item)
            written += 1
        return {"retried": len(pending), "written": written, "dropped": dropped}

    # ------------------------------------------------------------------
    # 维护（§2.4 / §8 / §12.2）
    # ------------------------------------------------------------------

    def maintain(self, *, now: datetime | None = None) -> MaintenanceReport:
        """每日一次：判定 dormant / 移档死记忆 / 全量原子重写。"""
        current = now or utc_now()
        bot_state = self.state(current)
        report = MaintenanceReport()
        dead_ids: list[str] = []
        for item in list(self.store.items.values()):
            r = retention_now(item, now=current, avoidance_now=bot_state.avoidance, cfg=self.cfg.decay)
            if r < self.cfg.decay.archive_r:
                dead_ids.append(item.id)
            elif r < self.cfg.decay.dormant_r and item.status == "active":
                item.status = "dormant"
                report.dormant += 1
        try:
            if dead_ids:
                report.archived = self.store.archive(dead_ids)
            self.store.rewrite()
            report.rewritten = True
        except OSError as exc:
            log.warning("maintenance rewrite failed: %r", exc)
        retry = self.retry_pending(now=current)
        report.pending_retried = retry["written"]
        report.pending_dropped = retry["dropped"]
        report.debug_raw_cleaned = self.clean_debug_logs(current)
        if report.archived:
            self.metrics.event("memory.archived", count=report.archived)
        self.metrics.event(
            "memory.maintain",
            archived=report.archived,
            dormant=report.dormant,
            rewritten=report.rewritten,
        )
        return report

    # ------------------------------------------------------------------
    # 调试原文（§12.2：默认不记正文）
    # ------------------------------------------------------------------

    @property
    def debug_raw_path(self) -> Path:
        return self.cfg.logs_path / "raw-debug.jsonl"

    def record_debug_raw(self, turn_id: str, raw: str) -> None:
        if not self.cfg.metrics.debug_raw:
            return
        text = raw or ""
        record = {
            "ts": utc_now_iso(),
            "turn_id": turn_id,
            "raw": text[:500],
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        }
        try:
            self.debug_raw_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.debug_raw_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def clean_debug_logs(self, now: datetime | None = None) -> int:
        if not self.cfg.metrics.debug_raw or not self.debug_raw_path.is_file():
            return 0
        current = now or utc_now()
        cutoff = current - timedelta(days=self.cfg.metrics.debug_raw_retention_days)
        kept: list[str] = []
        removed = 0
        try:
            with open(self.debug_raw_path, "r", encoding="utf-8", errors="replace", newline="") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return 0
        for line in lines:
            if not line.strip():
                continue
            try:
                timestamp = parse_iso(json.loads(line)["ts"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                removed += 1
                continue
            if timestamp >= cutoff:
                kept.append(line)
            else:
                removed += 1
        if removed:
            try:
                atomic_write_text(self.debug_raw_path, "".join(line + "\n" for line in kept))
                self.metrics.event("memory.debug_raw_cleaned", count=removed)
            except OSError:
                pass
        return removed

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def close(self) -> None:
        """进程退出时全量重写一次（§2：全量重写只在退出或每日一次）。"""
        try:
            self.store.rewrite()
        except OSError as exc:
            log.warning("final rewrite failed: %r", exc)
