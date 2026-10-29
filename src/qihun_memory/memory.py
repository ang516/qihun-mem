"""记忆生命周期与系统门面（v1.5-r2 §4 / §5 / §7.2 / §7.3 / 附录 D1）。

职责：
- 写入：dedup_key 精确去重 → 余弦去重 → affect/valence 调制 S₀ → 追加 JSONL；
  失败进 pending，下一轮重试一次（§4.2.5）；
- 遗忘：惰性计算 R、dormant 翻转、强匹配复活、档案移动（§5 / §8）；
- 强化：被选中注入的条目经 compute_S("reinforce")，cap 90（§5.3）；
- 状态单点：所有条目状态写盘（S/status/last_access/access_count/mention_count）
  只经 update_memory(Set/Inc)，禁止第二份写入路径（附录 D1）；
- 检索：委托 retrieval.Retriever；检索前对 UserAffectStore 做惰性 decay 并传入；
- 维护：每日重写、R<0.02 移档、清理调试日志（§2.4 / §8 / §12.2）。

v1.5-r2 新增：
- 每条记忆 affect（parser 归一化 → 落库；S₀ 用 compute_S("init", ...) 调制）；
- UserAffectStore 接线（本模块只通过实例方法读写 user_affect.json）；
- pending 只重放“新记忆写入”（带 turn_key 幂等）；强化/感情更新永不进 pending。

v1.6-rc4（§0.1 / E1 / E4 / E5）：
- 新条目 mention_count=0，仅去重命中 active 条目时 Inc(1)（E1）；
- 去重范围只含同 user+scope+type 的 active 条目；dormant 不参与、不因去重复活（E4）；
- 复活的落库动作 = Set(S/status/last_access)，不 Inc(access_count)、不叠 reinforce（E5）；
- pending 重放点只在 write_back 8b 开始（remember 内部不再隐式重放，§5.5/§9）；
- maintain() 逐阶段 try/except，失败只记 state.write_guard / mem_write.fail，不抛（§3.5）。

历史：v1.5-r2 为当前事实层基线；v1.6-rc4 追加叙事层（情感日记）接线：
- DiaryStore 是 diary.jsonl 唯一读写入口；DiaryWorker 走独立 worker（§8.2 / D7.3），
  turn() 内绝不主动生成日记，只记账 turn；
- 叙事层不参与检索 / S / R / user_affect，注入选择只按 scope_tag 过滤（D7.1 / D7.2）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import Config
from .diary import DiaryStore, DiaryWorker, select_excerpts
from .embedding import Embedder, get_embedder
from .fsutil import atomic_write_text
from .lifecycle import compute_S, retention_now
from .metrics import Metrics
from .models import (
    ALLOWED_SCOPES,
    ALLOWED_STATUS,
    MAX_STABILITY,
    Inc,
    MemoryItem,
    Set,
    affect_label,
    parse_iso,
    sanitize_affect,
    utc_now,
    utc_now_iso,
)
from .parser import dedup_key, normalize_item
from .retrieval import RetrievalResult, Retriever
from .state import BotState, UserAffect, UserAffectStore, UserDirectory, load_bot_state
from .store import MemoryStore

log = logging.getLogger("memory")

# 附录 D1：update_memory 的字段白名单（任何不在表内的字段 = 越界写）
_SET_FIELDS = frozenset({"S", "status", "last_access"})
_INC_FIELDS = frozenset({"access_count", "mention_count"})


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
    # v1.6-rc4 §3.5：叙事层过期日记移档数
    diary_archived: int = 0


@dataclass
class _PendingWrite:
    """待重放的新记忆写入（只允许新记忆；强化/感情更新不进此队列）。"""

    item: MemoryItem


class MemorySystem:
    """单实例记忆系统门面。"""

    def __init__(
        self,
        cfg: Config,
        *,
        metrics: Metrics | None = None,
        embedder: Embedder | None = None,
        read_only: bool = False,
    ) -> None:
        self.cfg = cfg
        # 只读模式（--dry-run）：不建目录、不写 memory.bad.jsonl/日志，也不允许写记忆（P1-1）
        self.read_only = bool(read_only)
        if not self.read_only:
            cfg.ensure_dirs()
        if metrics is not None:
            self.metrics = metrics
        elif self.read_only:
            self.metrics = Metrics(None, enabled=False)
        else:
            self.metrics = Metrics(
                cfg.metrics_file if cfg.metrics.enabled else None,
                enabled=cfg.metrics.enabled,
            )
        self.embedder: Embedder = embedder if embedder is not None else get_embedder(cfg.embedding)
        self.store = MemoryStore(
            cfg,
            self.metrics,
            vector_dim=getattr(self.embedder, "dim", 0) or None,
            read_only=self.read_only,
        )
        self.store.load()
        # user_affect 的唯一读写入口（附录 D1）；禁止其他模块直接读写 user_affect.json
        self.affect_store = UserAffectStore(cfg.user_affect_file, cfg.affect, self.metrics)
        self.users = UserDirectory(cfg.users_file)
        self.retriever = Retriever(self.embedder, cfg, self.metrics)
        self._pending: list[_PendingWrite] = []
        # 叙事层（v1.6 §8）：DiaryStore 是 diary.jsonl 唯一读写入口；DiaryWorker 独立、
        # 只读依赖注入；llm 由宿主/定时器在 generate() 时传入，turn() 内绝不调用（D7.3）。
        self.diary = DiaryStore(cfg, self.metrics, read_only=self.read_only)
        try:
            self.diary.load()
        except Exception as exc:  # 读取失败只降人格语境，不影响事实层（硬规则 19）
            self.metrics.event("diary.fail", stage="load", exc_type=type(exc).__name__)
        self.diary_worker = DiaryWorker(
            cfg,
            self.diary,
            metrics=self.metrics,
            memory_items=lambda: list(self.store.items.values()),
            state_loader=lambda: self.state(),
            users=self.users,
            affect_snapshot=lambda: self.affect_store.snapshot(),
        )

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    @property
    def items(self) -> dict[str, MemoryItem]:
        return self.store.items

    @property
    def pending_count(self) -> int:
        return len(self._pending)

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

    def user_affect(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        decay: bool = False,
    ) -> UserAffect:
        """用户感情快照（只读；decay=True 时先做惰性衰减）。"""
        if decay:
            return self.affect_store.decay(user_id, now=now)
        return self.affect_store.get(user_id, now)

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

    def select_diary_excerpts(
        self, scope: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """叙事层注入选择（只读；任何异常 → 空节选 + diary.fail，不影响回答 §8.6）。

        只按 scope_tag 过滤（group/dm；mixed 返回空），不参与事实层检索与装填。
        """
        empty: dict[str, Any] = {"excerpts": [], "tokens": 0, "injected": 0, "truncated": False}
        try:
            if not self.cfg.diary.enabled:
                return empty
            return select_excerpts(
                self.diary,
                str(scope),
                now=now or utc_now(),
                budget=self.cfg.diary.inject_budget,
                count=self.cfg.diary.inject_count,
                metrics=self.metrics,
            )
        except Exception as exc:
            self.metrics.event("diary.fail", stage="select_excerpts", exc_type=type(exc).__name__)
            return empty

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
            "user_affect_users": len(self.affect_store.snapshot()),
            "pending_writes": len(self._pending),
            "diary": self.diary.counts(),
            "diary_files": self.diary.file_sizes(),
            "metrics": self.metrics.counters(),
        }

    # ------------------------------------------------------------------
    # 状态单点：update_memory(Set/Inc)（附录 D1）
    # ------------------------------------------------------------------

    def update_memory(self, item_id: str, ops: Any, *, reason: str = "") -> bool:
        """唯一的状态写入入口：白名单 Set/Inc，整体生效或整体不生效。

        返回 True = 全部操作已生效；False = 任一操作非法（整批不生效）或条目不存在。
        非法字段/值记 state.write_guard；S 写 clamp [0, 90]。
        """
        item = self.store.items.get(str(item_id))
        if item is None:
            self._write_guard(item_id, field=None, error="unknown_item", reason=reason)
            return False
        if isinstance(ops, (Set, Inc)):
            ops = [ops]
        try:
            operations = list(ops)
        except TypeError:
            self._write_guard(item_id, field=None, error="invalid_ops", reason=reason)
            return False
        if not operations:
            return True
        pending: dict[str, Any] = {}
        for op in operations:
            if isinstance(op, Set):
                if op.field not in _SET_FIELDS:
                    self._write_guard(item_id, op.field, "field_not_allowed", reason)
                    return False
                try:
                    pending[op.field] = self._validated_set(op.field, op.value)
                except (TypeError, ValueError):
                    self._write_guard(item_id, op.field, "invalid_value", reason)
                    return False
            elif isinstance(op, Inc):
                if op.field not in _INC_FIELDS:
                    self._write_guard(item_id, op.field, "field_not_allowed", reason)
                    return False
                if isinstance(op.delta, bool) or not isinstance(op.delta, int):
                    self._write_guard(item_id, op.field, "invalid_delta", reason)
                    return False
                # Inc 内部读旧值（同批次先前操作的结果也算旧值）
                base = pending.get(op.field, getattr(item, op.field, 0))
                pending[op.field] = max(0, int(base) + int(op.delta))
            else:
                self._write_guard(item_id, field=None, error="invalid_operation", reason=reason)
                return False
        # 校验通过后才整体应用：任一项非法都不会留下半写状态
        for field_name, value in pending.items():
            setattr(item, field_name, value)
        return True

    @staticmethod
    def _validated_set(field_name: str, value: Any) -> Any:
        if field_name == "S":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("S must be a number")
            number = float(value)
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError("S must be finite")
            return max(0.0, min(number, MAX_STABILITY))
        if field_name == "status":
            if value not in ALLOWED_STATUS:
                raise ValueError(f"invalid status: {value!r}")
            return value
        if field_name == "last_access":
            if not isinstance(value, str):
                raise TypeError("last_access must be an ISO string")
            return parse_iso(value).isoformat(timespec="seconds")
        raise ValueError(f"field not allowed: {field_name!r}")

    def _write_guard(
        self,
        item_id: Any,
        field: str | None,
        error: str,
        reason: str = "",
    ) -> None:
        try:
            self.metrics.event(
                "state.write_guard",
                item_id=str(item_id),
                field=field,
                error=error,
                reason=reason or None,
            )
        except Exception:  # pragma: no cover - 指标绝不反噬主流程
            pass

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
        if self.embedder.available is False:
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
        """惰性归档：R < 0.1 → dormant（只翻内存状态，文件不动）。

        判定使用**未压抑**的 R：dormant 是持久状态，压抑只允许临时影响可提取性
        （§5.2「不是忘掉，是不易想起」）；否则几轮回避就会把记忆永久降级（A4）。
        状态写走 update_memory（附录 D1）。
        """
        changed = 0
        for item in self._scope_items(scope, user_id):
            if item.status != "active":
                continue
            r = retention_now(item, now=now, avoidance_now=0.0, cfg=self.cfg.decay)
            if r < self.cfg.decay.dormant_r:
                if self.update_memory(item.id, Set("status", "dormant"), reason="refresh_statuses"):
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
        last_event_affect: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        """纯检索：不强化（强化只在「被选中并注入」之后由 reinforce 执行）。

        检索前对当前用户的 UserAffectStore 做惰性 decay（§5.4），并把快照 /
        上一轮事件 affect 传给 Retriever（§7.2 rel/familiarity/affect_match）。
        """
        current = now or utc_now()
        user = str(user_id)
        bot_state = self.state(current)
        # 检索前 decay：annoyance *= 0.9^floor(days/7)，stale 归零（只写 user_affect.json）
        user_affect = self.affect_store.decay(user, now=current)
        # 不在检索路径里补算向量：embedding 属于写入/后台补算（§6.5），不能阻塞宿主取 prompt
        self._refresh_statuses(scope, user, bot_state, current)
        return self.retriever.search(
            self.store.items.values(),
            query,
            scope=scope,
            user_id=user,
            bot_state=bot_state,
            now=current,
            user_affect=user_affect,
            last_event_affect=last_event_affect,
        )

    def reinforce(
        self,
        result: RetrievalResult,
        *,
        now: datetime | None = None,
        revived_this_turn: set[str] | None = None,
    ) -> int:
        """§6.1 / §6.3 / E5：被注入的记忆强化；dormant 强匹配复活（S 重置为 S₀）。

        v1.6-rc4（E5）复活优先：
        - dormant 且 sim>0.7 → 只 revive（Set S/status/last_access），
          不 Inc(access_count)、不再叠 reinforce（不乘 (1+R)）；
        - active 且 sim>0.5 → reinforce（Set S/last_access + Inc access_count）；
        - `revived_this_turn`：同轮已复活的条目不再强化（轮内去重；调用方不传时
          按本次调用去重），避免「复活后叠强化」。
        所有状态写只经 update_memory（Set/Inc）；强化/感情更新永不进 pending。
        """
        current = now or utc_now()
        now_iso = current.isoformat(timespec="seconds")
        revived: set[str] = revived_this_turn if revived_this_turn is not None else set()
        changed = 0
        for entry in result.selected:
            item = entry.item
            # E5：同轮已复活 → 不叠 reinforce（不 Inc access_count、不乘 (1+R)）
            if item.id in revived:
                continue
            if item.status == "dormant":
                # 只有强匹配才复活；0.5~0.7 区间不强化、不刷新 last_access，
                # 避免出现「永远 dormant 但一直在注入」的僵尸状态（B4）
                if entry.sim <= self.cfg.decay.revive_sim:
                    continue
                ok = self.update_memory(
                    item.id,
                    [
                        Set("S", compute_S("revive", item.type, cfg=self.cfg.decay)),
                        Set("status", "active"),
                        Set("last_access", now_iso),
                    ],
                    reason="reinforce.revive",
                )
                if ok:
                    revived.add(item.id)
                    self.metrics.event("memory.revived", reason="strong_match", item_id=item.id)
                    changed += 1
                continue
            if entry.sim <= self.cfg.decay.reinforce_sim:
                continue
            new_s = compute_S(
                "reinforce",
                item.type,
                S=item.S,
                R=entry.r,
                cfg=self.cfg.decay,
            )
            ok = self.update_memory(
                item.id,
                [
                    Set("S", new_s),
                    Set("last_access", now_iso),
                    Inc("access_count"),
                ],
                reason="reinforce",
            )
            if ok:
                self.metrics.event("memory.reinforced", item_id=item.id, S=round(item.S, 6))
                changed += 1
        return changed

    # ------------------------------------------------------------------
    # 写入（§4.2.8 / §4.3 / §4.2.5）
    # ------------------------------------------------------------------

    def _dedup_candidates(self, user_id: str, scope: str, item_type: str) -> list[MemoryItem]:
        """去重候选：同 user + 同 scope + 同 type 的 **active** 条目（§6.3 / E4）。

        v1.6-rc4：dormant 不参与 mention 比对，也不会因去重命中而复活
        （dormant 只走检索强匹配 sim>0.7 的 revive 路径）。
        """
        return [
            item
            for item in self.store.items.values()
            if item.user_id == user_id
            and item.scope == scope
            and item.type == item_type
            and item.status == "active"
        ]

    @staticmethod
    def _exact_duplicate(
        candidates: Iterable[MemoryItem],
        item_type: str,
        content: str,
    ) -> MemoryItem | None:
        """第一优先级：dedup_key 精确命中（type.lower + normalize_text(content)）。"""
        wanted = dedup_key({"type": item_type, "content": content})
        for candidate in candidates:
            if dedup_key(candidate.to_dict()) == wanted:
                return candidate
        return None

    def _similar_duplicate(
        self,
        candidates: list[MemoryItem],
        new_vector: list[float] | None,
    ) -> tuple[MemoryItem | None, str | None]:
        """第二优先级：同 user+scope+type 内 cos > 0.9。

        返回 (命中条目|None, 降级原因|None)。降级 = 本可用余弦但向量不可用；
        调用方记 dedup.degraded（与跨池 group_bridge.dedup 分开）。
        """
        if not candidates:
            return None, None
        if not self.embedder.available:
            return None, "embedder_unavailable"
        if new_vector is None:
            return None, "encode_failed"
        best: MemoryItem | None = None
        best_sim = -1.0
        usable = 0
        for candidate in candidates:
            if not candidate.vector or len(candidate.vector) != len(new_vector):
                continue
            usable += 1
            sim = _dot(new_vector, candidate.vector)
            if sim > best_sim:
                best, best_sim = candidate, sim
        if best is None:
            return None, "no_vectors" if usable == 0 else None
        if best_sim > self.cfg.decay.dedupe_sim:
            return best, None
        return None, None

    def _merge_duplicate(
        self,
        duplicate: MemoryItem,
        now_iso: str,
        *,
        match: str,
        item_type: str,
    ) -> bool:
        """E4：重复提及命中 active 条目 → 只 Set(last_access) + Inc(mention_count, 1)。

        v1.6-rc4 语义：不 compute_S、不强化 S、不写 JSONL 新行、不进 pending；
        更新失败只降记忆增益，记 mem_write.mention_fail，不抛给聊天主流程。
        """
        ops: list[Any] = [Set("last_access", now_iso), Inc("mention_count")]
        ok = self.update_memory(duplicate.id, ops, reason="dedup.merge")
        if not ok:  # 白名单字段不会失败；防御性记录，避免静默丢合并
            self.metrics.event(
                "mem_write.mention_fail",
                item_id=duplicate.id,
                type=item_type,
                match=match,
            )
            return False
        self.metrics.event("memory.deduped", item_id=duplicate.id, type=item_type, match=match)
        return True

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
        """写入提取结果（输入按 parser 归一化后的条目；兼容直接 API 调用时再归一化）。

        v1.6-rc4：
        - 去重范围只含 active 条目，命中只 Set(last_access)+Inc(mention_count,1)（E4）；
        - 新条目 mention_count=0（E1）；
        - 任何写盘失败都不抛异常（§4.2.5）；失败的新记忆进 pending，
          由 write_back 8b 开始重放一次（§5.5/§9，本方法不再隐式重放）。
        """
        current = now or utc_now()
        now_iso = current.isoformat(timespec="seconds")
        report = WriteReport()
        items_in = list(extracted)
        user = str(user_id)
        if not user.strip() or scope not in ALLOWED_SCOPES:
            report.skipped = len(items_in)
            self.metrics.event("mem_write.invalid", reason="missing_user_or_scope")
            return report
        if turn_key and self.store.has_turn_key(turn_key):
            report.turn_duplicate = True
            report.skipped = len(items_in)
            self.metrics.event(
                "mem_write.turn_dup",
                turn_key_hash=hashlib.sha256(turn_key.encode("utf-8", "replace")).hexdigest()[:16],
            )
            return report

        # v1.6 §5.5/§9：pending 重放点固定在 write_back 8b 开始（单一位置），
        # remember() 直调 API 时不再隐式重放；失败的新记忆仍进 pending 由上游重放一次。
        self.ensure_vectors()
        degraded_noted = False
        try:
            valence_value = float(valence)
        except (TypeError, ValueError):
            valence_value = 0.0
        if valence_value != valence_value or valence_value in (float("inf"), float("-inf")):
            valence_value = 0.0
        valence_value = max(-1.0, min(1.0, valence_value))

        for raw in items_in:
            normalized = normalize_item(raw, self.cfg.extraction, self.metrics)
            if normalized is None:
                report.skipped += 1
                self.metrics.event("mem_write.skipped", reason="invalid_item")
                continue
            item_type = str(normalized["type"])
            content = str(normalized["content"])
            tags = list(normalized.get("tags") or [])
            affect = sanitize_affect(normalized.get("affect")) if normalized.get("affect") else None
            candidates = self._dedup_candidates(user, scope, item_type)

            # 1) dedup_key 精确优先（不需要向量）
            duplicate = self._exact_duplicate(candidates, item_type, content)
            match = "dedup_key"
            if duplicate is None:
                # 2) 再余弦 > 0.9；向量不可用只降级并记 dedup.degraded
                new_vector = self._encode(content)
                duplicate, degraded_reason = self._similar_duplicate(candidates, new_vector)
                match = "cosine"
                if degraded_reason and not degraded_noted:
                    degraded_noted = True
                    self.metrics.event("dedup.degraded", reason=degraded_reason, scope=scope)
            if duplicate is not None:
                if self._merge_duplicate(duplicate, now_iso, match=match, item_type=item_type):
                    report.merged.append(duplicate)
                else:
                    report.skipped += 1
                continue

            item = MemoryItem(
                id=self.store.next_id(),
                user_id=user,
                scope=scope,
                type=item_type,
                content=content,
                created_at=now_iso,
                last_access=now_iso,
                access_count=0,
                # v1.6-rc4（E1）：新建条目 mention_count=0，仅去重命中 active 时 +1
                mention_count=0,
                # 唯一 S 计算入口：影响因子 k_v / k_a 只在 lifecycle.compute_S 出现
                S=compute_S(
                    "init",
                    item_type,
                    valence=valence_value,
                    affect=affect,
                    cfg=self.cfg.decay,
                ),
                valence=valence_value,
                status="active",
                tags=tags,
                turn_key=turn_key or None,
                affect=affect,
                vector=new_vector,
            )
            try:
                self.store.append(item)
            except Exception as exc:  # 含 UnicodeEncodeError 等：落盘失败绝不能影响回答（§11 规则 13）
                report.failed += 1
                self.metrics.event("mem_write.fail", exc_type=type(exc).__name__)
                # pending 只存“新记忆写入”，并把 turn_key 一并带上（幂等重放）
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
                affect=affect_label(affect),
            )

        # turn_key 在「整轮全部命中合并」时也要登记，否则重放会重复累加 mention_count（B3）
        if turn_key and (report.created or report.merged):
            self.store.register_turn_key(turn_key, (report.created or report.merged)[0].id)
        report.pending = len(self._pending)
        return report

    def retry_pending(self, *, now: datetime | None = None) -> dict[str, int]:
        """§4.2.5：失败条目下一轮重试 1 次；仍失败则丢弃并记 mem_write.fail。

        v1.5：pending 只重放新记忆写入；重放前查 turn_key（已落盘则跳过）与 id
        （同一条已在内存中则跳过），避免重复追加。
        """
        if not self._pending:
            return {"retried": 0, "written": 0, "dropped": 0, "skipped": 0}
        pending = self._pending
        self._pending = []
        written = 0
        dropped = 0
        skipped = 0
        for entry in pending:
            item = entry.item
            if item.id in self.store.items:
                skipped += 1
                continue
            if item.turn_key and self.store.has_turn_key(item.turn_key):
                skipped += 1
                continue
            try:
                self.store.append(item)
            except Exception as exc:
                dropped += 1
                self.metrics.event("mem_write.fail", exc_type=type(exc).__name__, reason="retry_dropped")
                continue
            if item.vector:
                self.store.append_vector(item)
            written += 1
        return {"retried": len(pending), "written": written, "dropped": dropped, "skipped": skipped}

    # ------------------------------------------------------------------
    # 维护（§2.4 / §8 / §12.2）
    # ------------------------------------------------------------------

    def maintain(self, *, now: datetime | None = None) -> MaintenanceReport:
        """每日一次：判定 dormant / 移档死记忆 / 全量原子重写（§3.5）。

        v1.6-rc4：逐阶段 try/except，任何阶段失败只记指标
        （state.write_guard / mem_write.fail），绝不抛给聊天主流程。
        """
        current = now or utc_now()
        report = MaintenanceReport()
        dead_ids: list[str] = []

        # 阶段 1：state 快照 + 未压抑 R 判定 dormant / 移档（A4：临时回避不造成永久降级）
        try:
            self.state(current)  # 只读快照（过期/缺失记 state.stale，不抛）
            for item in list(self.store.items.values()):
                try:
                    r = retention_now(
                        item, now=current, avoidance_now=0.0, cfg=self.cfg.decay
                    )
                except Exception as exc:  # 单条时间戳损坏不拖垮整个维护
                    self._maintain_fail("scan_item", exc)
                    continue
                if r < self.cfg.decay.archive_r:
                    dead_ids.append(item.id)
                elif r < self.cfg.decay.dormant_r and item.status == "active":
                    try:
                        if self.update_memory(
                            item.id, Set("status", "dormant"), reason="maintain.dormant"
                        ):
                            report.dormant += 1
                    except Exception as exc:
                        self._maintain_fail("dormant", exc, metric="state.write_guard")
        except Exception as exc:  # 兜底：扫描失败不阻塞后续重写 / 清理
            self._maintain_fail("scan", exc, metric="state.write_guard")

        # 阶段 2：移档 + 全量原子重写（*.tmp → fsync → os.replace）
        try:
            if dead_ids:
                report.archived = self.store.archive(dead_ids)
            self.store.rewrite()
            report.rewritten = True
        except Exception as exc:  # 含 OSError / UnicodeEncodeError 等：维护失败只记指标
            log.warning("maintenance rewrite failed: %r", exc)
            self._maintain_fail("rewrite", exc)

        # 阶段 2b：叙事层维护（§3.5）：先移档过期日记，再原子重写 diary.jsonl。
        # 叙事层失败只降人格语境，不影响事实层（D7.1 / 硬规则 19）。
        try:
            report.diary_archived = self.diary.archive_expired(current)
            self.diary.rewrite()
        except Exception as exc:
            self._maintain_fail("diary", exc, metric="diary.fail")

        # 阶段 3：pending 重放一次（失败已在 retry_pending 内部记 mem_write.fail）
        try:
            retry = self.retry_pending(now=current)
            report.pending_retried = retry["written"]
            report.pending_dropped = retry["dropped"]
        except Exception as exc:
            self._maintain_fail("pending_retry", exc)

        # 阶段 4：debug 原文日志清理（7 天，§12.2）
        try:
            report.debug_raw_cleaned = self.clean_debug_logs(current)
        except Exception as exc:
            self._maintain_fail("debug_clean", exc)

        # 阶段 5：补算缺失向量（不阻塞聊天，§6.5）
        try:
            self.ensure_vectors()
        except Exception as exc:
            self._maintain_fail("ensure_vectors", exc)

        # 阶段 6：指标落盘（指标失败也不能反噬维护）
        try:
            if report.archived:
                self.metrics.event("memory.archived", count=report.archived)
            self.metrics.event(
                "memory.maintain",
                archived=report.archived,
                dormant=report.dormant,
                rewritten=report.rewritten,
                diary_archived=report.diary_archived,
            )
        except Exception:  # pragma: no cover - 指标失败绝不影响维护结果
            pass
        return report

    def _maintain_fail(
        self, stage: str, exc: BaseException, *, metric: str = "mem_write.fail"
    ) -> None:
        """维护阶段失败：只记指标（state.write_guard / mem_write.fail），绝不抛（§3.5）。"""
        try:
            self.metrics.event(metric, stage=stage, exc_type=type(exc).__name__, reason="maintain")
        except Exception:  # pragma: no cover - 指标失败绝不反噬维护
            pass

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
        try:
            text = text.encode("utf-8", "replace").decode("utf-8")
        except Exception:
            return
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
        except Exception:
            pass

    def clean_debug_logs(self, now: datetime | None = None) -> int:
        # 即使 debug_raw 已关闭，也要清理历史残留的原文日志（隐私最小化）
        if not self.debug_raw_path.is_file():
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
        """进程退出时重试 pending + 全量重写一次（§2：全量重写只在退出或每日一次）。"""
        if self.read_only:
            return
        try:
            self.retry_pending()
        except Exception:
            pass
        try:
            self.store.rewrite()
        except Exception as exc:
            log.warning("final rewrite failed: %r", exc)
        # §3：diary.jsonl 同样只在每日维护或退出时全量原子替换
        try:
            self.diary.rewrite()
        except Exception as exc:
            log.warning("final diary rewrite failed: %r", exc)
