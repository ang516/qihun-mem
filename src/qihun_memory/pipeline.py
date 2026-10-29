"""每轮流程（v1.6-rc4 §9）。

实现为两阶段，便于与「LLM 调用不由本系统发起」的宿主（qq-bridge / DSH）集成：

- prepare（build_context）：读 state（只读）→ 闸门（§5.1）→ 检索（§7 路由，
  含桥接 / 打分 / fill 装填 → injected_items）→ 叙事层日记节选（§8.4，不参与检索）
  → 组 prompt（总预算 3850，§2）；随后冻结 ctx：只有 last_event_affect / metrics / pending 可写。
- finish：parse → clean_answer_for_send → emit（先发送）→ 摘要/最近轮 → 日记 turn 记账
  → write_back（8a 熟悉度 → 8b pending 重放+逐条落盘 → 9a 强化/复活 → 9b 感情聚合 → 10 事件 affect）；
  顺序由 §9 / 附录 C D5 锁定。

run_turn(event, llm_call) 是两者组合的便捷入口。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .config import Config
from .gate import is_light_message, should_extract
from .memory import MemorySystem, WriteReport
from .metrics import Metrics
from .models import (
    ANGER_AFFECT_LABELS,
    JOY_AFFECT_LABELS,
    affect_intensity,
    affect_label,
    sanitize_affect,
    utc_now,
)
from .parser import ExtractResult, MemoryOutputParser, clean_answer_for_send
from .prompt import PromptBundle, build_prompt
from .retrieval import RetrievalResult
from .state import BotState, load_bot_state
from .summary import RollingSummary, conversation_key

# §7.2：ctx 只写白名单（其余字段一律冻结）
_CTX_MUTABLE_FIELDS = frozenset({"last_event_affect", "metrics", "pending"})


class ContextViolation(RuntimeError):
    """试图修改 PreparedTurn/TurnContext 的冻结字段（§7.2 / 附录 E R1）。"""

    def __init__(self, field_name: str, *, turn_id: str = "") -> None:
        super().__init__(f"ctx field is frozen: {field_name}")
        self.field = field_name
        self.turn_id = turn_id


@dataclass(frozen=True)
class TurnEvent:
    """一条入站消息。"""

    user_id: str
    scope: str  # "group" / "dm"
    message: str
    msgid: str = ""
    at_bot: bool = False
    reply_to_bot: bool = False
    nickname: str = ""
    now: datetime | None = None

    def make_turn_key(self) -> str:
        """§2.6 幂等键 = user_id + 消息 msgid；没有 msgid 时不做去重。"""
        if not self.msgid:
            return ""
        return f"{self.user_id}:{self.msgid}"


@dataclass
class PreparedTurn:
    """一轮的只读上下文（TurnContext）。构造后仅三个字段可写（§7.2）。

    frozen dataclass + mutable_fields 白名单：越界写抛 ContextViolation 并记
    ctx.violation；由架构测试 / L3 断言守护（附录 E R1）。
    """

    # ---------- 冻结字段（构造后不可改） ----------
    turn_id: str
    event: TurnEvent
    user_id: str
    scope: str
    gate_passed: bool
    light_path: bool
    persona: str
    state: BotState
    summary: str
    candidates: int
    retrieval: RetrievalResult
    prompt: PromptBundle
    injected_items: list
    turn_key: str
    now: datetime
    last_event_affect_at_read: dict | None
    # v1.6 §9：叙事层注入快照（只读，≤2 篇 / ≤300 tokens）与昵称快照（只读）
    diary_excerpts: list = field(default_factory=list)
    users: dict = field(default_factory=dict)
    # ---------- 可写白名单（仅以下三个） ----------
    last_event_affect: dict | None = None
    metrics: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    # ---------- 内部（不参与语义） ----------
    _metrics_sink: Metrics | None = field(default=None, repr=False, compare=False)
    _sealed: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_sealed" or not self.__dict__.get("_sealed", False):
            object.__setattr__(self, name, value)
            return
        if name in _CTX_MUTABLE_FIELDS:
            object.__setattr__(self, name, value)
            return
        sink = self.__dict__.get("_metrics_sink")
        turn_id = str(self.__dict__.get("turn_id", ""))
        if sink is not None:
            try:
                sink.event("ctx.violation", turn_id=turn_id, field=name)
            except Exception:  # pragma: no cover - 指标绝不反噬主流程
                pass
        raise ContextViolation(name, turn_id=turn_id)

    # ---------- 兼容旧字段名（只读属性） ----------

    @property
    def bot_state(self) -> BotState:
        return self.state

    @property
    def summary_text(self) -> str:
        return self.summary

    @property
    def retrieval_ids(self) -> list[str]:
        return [entry.item.id for entry in self.retrieval.selected]

    @property
    def injected_ids(self) -> list[str]:
        return [str(item.get("id")) for item in self.injected_items if isinstance(item, Mapping)]


# 兼容旧名：TurnContext 是 PreparedTurn 的语义别名
TurnContext = PreparedTurn


@dataclass
class TurnResult:
    turn_id: str
    answer: str
    status: str
    memories: list[dict]
    write_report: WriteReport
    reinforced: int
    duration_ms: float
    metrics: dict

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "answer": self.answer,
            "status": self.status,
            "memories": self.memories,
            "written": self.write_report.counts(),
            "turn_duplicate": self.write_report.turn_duplicate,
            "reinforced": self.reinforced,
            "duration_ms": round(self.duration_ms, 3),
            "metrics": self.metrics,
        }


# --------------------------------------------------------------------------
# 指标 / 聚合工具（纯函数，便于单测）
# --------------------------------------------------------------------------

def user_id_hash(user_id: str) -> str:
    # errors="replace"：user_id 带孤立代理字符时也不能让 prepare/finish 抛异常
    return hashlib.sha256(str(user_id).encode("utf-8", "replace")).hexdigest()[:16]


def retrieval_summary(result: RetrievalResult) -> dict:
    """检索摘要（service / cli / turn.finish 共用；新增桥接与去重字段）。"""
    payload = {
        "candidates": result.candidates,
        "selected": [entry.item.id for entry in result.selected],
        "tokens": result.tokens,
        "budget": result.budget,
        "degraded": result.degraded,
    }
    injected = getattr(result, "injected_items", None)
    if injected is not None:
        payload["injected"] = len(injected)
    for name in (
        "bridge_hit",
        "bridge_candidates",
        "bridge_truncated",
        "dedup_removed",
        "dedup_degraded",
        "affect_bias",
    ):
        value = getattr(result, name, None)
        if value is not None:
            payload[name] = value
    return payload


def aggregate(
    injected_items: Iterable[Mapping[str, Any]] | None,
    *,
    user_id: str,
    alpha: float = 0.1,
) -> dict[str, float]:
    """9b 唯一聚合函数（§5.5 / 附录 D6 I3）。

    只消费本轮 injected_items 中 user_id 等于当前用户、且 affect 非 neutral 的条目：
    I_joy = max(joy/affection 的 intensity)，I_anger = max(anger/disgust 的 intensity)，
    deltas = {affection: alpha * I_joy, annoyance: alpha * I_anger}（0 分量不传）。
    """
    user = str(user_id)
    joy = 0.0
    anger = 0.0
    for item in injected_items or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("user_id") or "") != user:
            continue
        affect = item.get("affect")
        if not isinstance(affect, Mapping):
            continue
        label = affect_label(affect)
        if label == "neutral":
            continue
        intensity = affect_intensity(affect)
        if label in JOY_AFFECT_LABELS:
            joy = max(joy, intensity)
        elif label in ANGER_AFFECT_LABELS:
            anger = max(anger, intensity)
    step = max(0.0, float(alpha))
    deltas: dict[str, float] = {}
    if joy > 0:
        deltas["affection"] = step * joy
    if anger > 0:
        deltas["annoyance"] = step * anger
    return deltas


def first_non_neutral(memories: Iterable[Mapping[str, Any]] | None) -> dict | None:
    """10：本轮新记忆里第一个非 neutral affect（只供下一轮 affect_match）。"""
    for item in memories or []:
        if not isinstance(item, Mapping):
            continue
        affect = sanitize_affect(item.get("affect"))
        if affect is not None and affect_label(affect) != "neutral":
            return affect
    return None


# --------------------------------------------------------------------------
# TurnPipeline
# --------------------------------------------------------------------------

class TurnPipeline:
    """§7 的每轮流程；线程内单写者使用。"""

    MAX_PREPARED = 128

    def __init__(self, system: MemorySystem, config: Config | None = None) -> None:
        self.system = system
        self.cfg = config or system.cfg
        self.metrics: Metrics = system.metrics
        self.parser = MemoryOutputParser(self.metrics, extraction=self.cfg.extraction)
        self.summaries = RollingSummary(self.cfg.summary_file, self.cfg.prompt)
        self._recent: dict[str, deque[tuple[str, str]]] = {}
        self._prepared: dict[str, PreparedTurn] = {}
        # 进程内「上一轮事件 affect」：只供下一轮 affect_match（重启后为空，用例 44）
        self._last_event_affect: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 阶段一：准备（build_context）
    # ------------------------------------------------------------------

    def _recent_for(self, key: str) -> deque[tuple[str, str]]:
        if key not in self._recent:
            self._recent[key] = deque(maxlen=max(1, self.cfg.prompt.recent_turns))
        return self._recent[key]

    @staticmethod
    def _empty_retrieval(cfg: Config, budget: int | None = None) -> RetrievalResult:
        """轻路径 / 降级路径的空检索结果（只带兼容必需字段）。"""
        return RetrievalResult(
            selected=[],
            candidates=0,
            tokens=0,
            budget=int(cfg.retrieval.budget_tokens) if budget is None else budget,
            degraded=False,
        )

    def prepare(self, event: TurnEvent) -> PreparedTurn:
        started = time.perf_counter()
        now = event.now or utc_now()
        turn_key = event.make_turn_key()
        key = conversation_key(event.scope, event.user_id)
        last_read = self._last_event_affect.get(key)
        try:
            return self._prepare(
                event,
                now=now,
                key=key,
                turn_key=turn_key,
                last_read=last_read,
                started=started,
            )
        except Exception as exc:  # §7.1：prepare 异常 → 无记忆注入的降级 prompt
            self.metrics.event("turn.prepare_error", exc_type=type(exc).__name__)
            return self._degraded_prepare(
                event,
                now=now,
                key=key,
                turn_key=turn_key,
                last_read=last_read,
                started=started,
            )

    def _prepare(
        self,
        event: TurnEvent,
        *,
        now: datetime,
        key: str,
        turn_key: str,
        last_read: dict | None,
        started: float,
    ) -> PreparedTurn:
        # 0. pending 重放已按 v1.6 §5.5/§9 前移到 write_back 8b 开始（单一重放点），
        #    prepare 不再重放，避免同轮两次重放与顺序锁定的歧义。

        # 1. state.json 只读（缺失/过期 → 中性，不抛异常）
        bot_state = load_bot_state(
            self.cfg.bot_state_file,
            cfg=self.cfg.state,
            now=now,
            metrics=self.metrics,
        )

        # 2. 闸门（只决定是否附提取协议）与检索路由（§6.1）解耦
        gate_passed = should_extract(
            event.message,
            at_bot=event.at_bot,
            reply_to_bot=event.reply_to_bot,
            cfg=self.cfg.gate,
        )
        recent_turns = list(self._recent_for(key))
        last_user_message = recent_turns[-1][0] if recent_turns else None
        # v1.6 §7.1：轻路径五条规则（含复读与纯 @bot）需要最近一条用户消息与 at_bot 上下文
        light_path = bool(self.cfg.retrieval.light_path_enabled) and is_light_message(
            event.message,
            last_user_message=last_user_message,
            at_bot=event.at_bot,
            cfg=self.cfg.gate,
        )
        if light_path:
            retrieval = self._empty_retrieval(self.cfg)
        else:
            try:
                retrieval = self.system.search(
                    event.message,
                    event.scope,
                    event.user_id,
                    now=now,
                    last_event_affect=last_read,
                )
            except Exception as exc:  # 检索失败降级为空注入，回答照常
                self.metrics.event(
                    "turn.prepare_error",
                    stage="search",
                    exc_type=type(exc).__name__,
                )
                retrieval = self._empty_retrieval(self.cfg)

        # 2b. 叙事层（v1.6 §8.4）：同 scope 的近期日记节选（≤300 tokens，最多 2 篇）。
        #     叙事层只提供人格语境，不参与检索 / 打分 / S/R / user_affect；失败只降语境不降回答。
        diary_excerpts: list = []
        diary_info = {"injected": 0, "tokens": 0}
        if bool(getattr(self.cfg.diary, "enabled", True)):
            try:
                picked = self.system.select_diary_excerpts(event.scope, now=now)
                diary_excerpts = list(picked.get("excerpts") or [])
                diary_info = {
                    "injected": int(picked.get("injected", len(diary_excerpts)) or 0),
                    "tokens": int(picked.get("tokens", 0) or 0),
                }
            except Exception as exc:  # 硬规则 19：日记读取失败不影响回答
                self.metrics.event("diary.fail", stage="select_excerpts", exc_type=type(exc).__name__)

        # 3. 组 prompt：persona + 摘要 + 日记节选 + injected_items + 最近 5 轮 + 当前消息
        summary = self.summaries.get(key)
        persona_text = self.system.persona()
        prompt = build_prompt(
            persona=persona_text,
            summary=summary,
            retrieval=retrieval,
            users=self.system.users,
            recent_turns=recent_turns,
            user_message=event.message,
            gate_passed=gate_passed,
            limits=self.cfg.prompt,
            diary_excerpts=diary_excerpts,
            metrics=self.metrics,
        )
        injected_items = list(getattr(retrieval, "injected_items", []) or [])
        affect_bias = int(getattr(retrieval, "affect_bias", 0) or 0)
        if affect_bias:
            self.metrics.event("retrieval.affect_bias", count=affect_bias, scope=event.scope)
        return self._build_ctx(
            event,
            turn_key=turn_key,
            last_read=last_read,
            started=started,
            bot_state=bot_state,
            gate_passed=gate_passed,
            light_path=light_path,
            retrieval=retrieval,
            prompt=prompt,
            summary=summary,
            injected_items=injected_items,
            persona=persona_text,
            diary_excerpts=diary_excerpts,
            users=self.system.users.snapshot(),
            diary_info=diary_info,
        )

    def _degraded_prepare(
        self,
        event: TurnEvent,
        *,
        now: datetime,
        key: str,
        turn_key: str,
        last_read: dict | None,
        started: float,
    ) -> PreparedTurn:
        bot_state = load_bot_state(
            self.cfg.bot_state_file,
            cfg=self.cfg.state,
            now=now,
            metrics=self.metrics,
        )
        retrieval = self._empty_retrieval(self.cfg)
        summary = ""
        try:
            summary = self.summaries.get(key)
        except Exception:
            summary = ""
        prompt = build_prompt(
            persona="",
            summary=summary,
            retrieval=retrieval,
            users=self.system.users,
            recent_turns=[],
            user_message=event.message,
            gate_passed=False,
            limits=self.cfg.prompt,
            diary_excerpts=[],
            metrics=self.metrics,
        )
        return self._build_ctx(
            event,
            turn_key=turn_key,
            last_read=last_read,
            started=started,
            bot_state=bot_state,
            gate_passed=False,
            light_path=True,
            retrieval=retrieval,
            prompt=prompt,
            summary=summary,
            injected_items=[],
            persona="",
            diary_excerpts=[],
            users={},
            diary_info={"injected": 0, "tokens": 0},
        )

    def _build_ctx(
        self,
        event: TurnEvent,
        *,
        turn_key: str,
        last_read: dict | None,
        started: float,
        bot_state: BotState,
        gate_passed: bool,
        light_path: bool,
        retrieval: RetrievalResult,
        prompt: PromptBundle,
        summary: str,
        injected_items: list,
        persona: str,
        diary_excerpts: list | None = None,
        users: dict | None = None,
        diary_info: dict | None = None,
    ) -> PreparedTurn:
        turn_id = uuid.uuid4().hex
        ctx = PreparedTurn(
            turn_id=turn_id,
            event=event,
            user_id=str(event.user_id),
            scope=str(event.scope),
            gate_passed=gate_passed,
            light_path=light_path,
            persona=persona,
            state=bot_state,
            summary=summary,
            candidates=int(getattr(retrieval, "candidates", 0) or 0),
            retrieval=retrieval,
            prompt=prompt,
            injected_items=injected_items,
            turn_key=turn_key,
            now=event.now or utc_now(),
            last_event_affect_at_read=last_read,
            diary_excerpts=list(diary_excerpts or []),
            users=dict(users or {}),
            _metrics_sink=self.metrics,
        )
        self._prepared[turn_id] = ctx
        while len(self._prepared) > self.MAX_PREPARED:
            self._prepared.pop(next(iter(self._prepared)))
        self.metrics.event(
            "turn.prepare",
            turn_id=turn_id,
            user_id_hash=user_id_hash(event.user_id),
            scope=event.scope,
            gate=gate_passed,
            light_path=light_path,
            candidates=ctx.candidates,
            selected=len(retrieval.selected),
            injected=len(injected_items),
            tokens=int(getattr(retrieval, "tokens", 0) or 0),
            bridge_hit=int(getattr(retrieval, "bridge_hit", 0) or 0),
            degraded=bool(getattr(retrieval, "degraded", False)),
            diary_injected=int((diary_info or {}).get("injected", 0) or 0),
            diary_tokens=int((diary_info or {}).get("tokens", 0) or 0),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return ctx

    # ------------------------------------------------------------------
    # 阶段二：完成
    # ------------------------------------------------------------------

    def finish(
        self,
        prepared: PreparedTurn,
        raw: str | None,
        *,
        sender: Callable[[str], None] | None = None,
    ) -> TurnResult:
        if prepared.turn_id not in self._prepared:
            raise ValueError(f"turn already finished or unknown: {prepared.turn_id}")
        started = time.perf_counter()
        raw_text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))

        # 6. 解析（永不抛异常）→ 7. 发送层唯一清洗点
        parsed: ExtractResult = self.parser.parse(raw_text)
        self._record_extract_metrics(parsed, prepared)
        raw_answer = parsed.raw_answer if isinstance(parsed.raw_answer, str) else ""
        answer = clean_answer_for_send(raw_answer)

        # 8. emit：先发送回答（空正文不发空气泡），早于任何写回（§7.3 顺序 1）
        if sender is not None and answer.strip():
            try:
                sender(answer)
            except Exception as exc:  # 宿主发送回调失败也不能中断后续流程
                self.metrics.event("turn.sender_fail", exc_type=type(exc).__name__)

        key = conversation_key(prepared.scope, prepared.user_id)

        # §11 规则 5：未命中闸门的轮次不写记忆（防模型漂移/注入污染）；
        # 闸门只约束写入，不影响检索与回答（§7.1）。
        memories_to_write = parsed.memories
        ungated = 0
        if memories_to_write and not prepared.gate_passed and self.cfg.gate.enforce_write:
            ungated = len(memories_to_write)
            memories_to_write = []
            self.metrics.event("mem_extract.ungated", count=ungated)

        # turn_key 重放：不重复摘要、不重复写记忆（幂等，§2.6）
        duplicate_turn = bool(prepared.turn_key) and self.system.store.has_turn_key(
            prepared.turn_key
        )

        # 9. 摘要 / 最近轮次（独立 try；任何异常都不能影响回答）
        if not duplicate_turn:
            try:
                self.summaries.update(key, prepared.event.message, answer)
                if answer.strip():
                    self._recent_for(key).append((prepared.event.message, answer))
            except Exception as exc:
                self.metrics.event("turn.finish_error", stage="summary", exc_type=type(exc).__name__)

        # 9b. 叙事层 turn 记账：every_n 按 scope 分别计数 / turn_range 取该 scope 的 turn id（§8.2）。
        #     只记账，不生成、不落盘；失败只记指标（硬规则 20：日记生成离线异步）。
        if not duplicate_turn:
            try:
                worker = getattr(self.system, "diary_worker", None)
                if worker is not None:
                    worker.record_turn(prepared.scope, prepared.turn_id)
            except Exception as exc:
                self.metrics.event("diary.fail", stage="record_turn", exc_type=type(exc).__name__)

        # 10. write_back：8a → 8b → 9a → 9b → 10（唯一写回路径，§7.5）
        write_report = WriteReport(skipped=len(memories_to_write), turn_duplicate=duplicate_turn)
        try:
            write_report = self.write_back(prepared, memories_to_write)
        except ContextViolation:  # ctx 越界写是实现缺陷，必须冒泡给测试/CI（§7.2）
            raise
        except Exception as exc:  # 写回失败 = 本轮无新记忆，回答照常（§11 规则 13）
            self.metrics.event("turn.finish_error", stage="write_back", exc_type=type(exc).__name__)
            write_report = WriteReport(
                failed=len(memories_to_write),
                turn_duplicate=duplicate_turn,
            )
            if memories_to_write:
                prepared.pending.extend(memories_to_write)

        reinforced = int(prepared.metrics.get("reinforced", 0) or 0)

        # 向量补算 / 调试原文：都在回答之后，失败全部吞掉
        try:
            self.system.ensure_vectors()
        except Exception:
            pass
        try:
            self.system.record_debug_raw(prepared.turn_id, raw_text)
        except Exception:
            pass

        duration_ms = (time.perf_counter() - started) * 1000
        self._prepared.pop(prepared.turn_id, None)
        metrics_payload = {
            "extract_status": parsed.status,
            "gate": prepared.gate_passed,
            "light_path": prepared.light_path,
            "sentinel": parsed.sentinel_found,
            "items_in": len(parsed.memories),
            "items_out": len(memories_to_write),
            "ungated": ungated,
            "written_created": len(write_report.created),
            "written_merged": len(write_report.merged),
            "write_failed": write_report.failed,
            "write_skipped": write_report.skipped,
            "turn_duplicate": write_report.turn_duplicate or duplicate_turn,
            "reinforced": reinforced,
            "retrieval_degraded": prepared.retrieval.degraded,
            "injected": len(prepared.injected_items),
            "diary_injected": len(prepared.diary_excerpts),
            "bridge_hit": int(getattr(prepared.retrieval, "bridge_hit", 0) or 0),
            "dedup_removed": int(getattr(prepared.retrieval, "dedup_removed", 0) or 0),
            "dedup_degraded": bool(getattr(prepared.retrieval, "dedup_degraded", False)),
            "affect_bias": int(getattr(prepared.retrieval, "affect_bias", 0) or 0),
            "familiarity_step": prepared.metrics.get("familiarity_step"),
            "affect_deltas": prepared.metrics.get("affect_deltas"),
            "last_event_affect": affect_label(prepared.last_event_affect),
            "duration_ms": round(duration_ms, 3),
        }
        self.metrics.event(
            "turn.finish",
            turn_id=prepared.turn_id,
            user_id_hash=user_id_hash(prepared.user_id),
            scope=prepared.scope,
            raw_len=len(raw_text),
            raw_hash=parsed.raw_hash or hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16],
            **metrics_payload,
        )
        return TurnResult(
            turn_id=prepared.turn_id,
            answer=answer,
            status=parsed.status,
            memories=parsed.memories,
            write_report=write_report,
            reinforced=reinforced,
            duration_ms=duration_ms,
            metrics=metrics_payload,
        )

    def write_back(self, ctx: PreparedTurn, memories: Iterable[dict] | None) -> WriteReport:
        """§7.5 / 附录 D6：8a → 8b → 9a → 9b → 10（唯一写回路径）。

        每步独立 try/except；失败只降记忆增益，不抛回聊天主流程。
        """
        now = utc_now()
        alpha = float(self.cfg.affect.step_alpha)
        items = list(memories or [])
        duplicate = bool(ctx.turn_key) and self.system.store.has_turn_key(ctx.turn_key)
        report = WriteReport(skipped=len(items), turn_duplicate=duplicate)

        # 8a：熟悉度先记「这轮聊过」，先于落盘（§7.3 顺序 2）
        if not duplicate:
            try:
                self.system.affect_store.step_update(
                    ctx.user_id, {"familiarity": alpha}, now=now
                )
                ctx.metrics["familiarity_step"] = alpha
            except ContextViolation:
                raise
            except Exception as exc:
                self.metrics.event(
                    "turn.finish_error", stage="write_back.8a", exc_type=type(exc).__name__
                )

        # 8b：pending 重放（≤1 次）→ 逐条 persist 新记忆（失败 → pending + mem_write.fail）。
        # v1.6 §5.5/§9：重放点固定在 8b 开始，prepare 与 remember() 都不再隐式重放。
        if duplicate:
            self.metrics.event(
                "mem_write.turn_dup",
                turn_key_hash=hashlib.sha256(
                    ctx.turn_key.encode("utf-8", "replace")
                ).hexdigest()[:16],
            )
        else:
            try:
                self.system.retry_pending(now=now)
            except Exception as exc:  # 重放失败只降记忆增益，不影响本轮新记忆写入
                self.metrics.event(
                    "turn.finish_error",
                    stage="write_back.8b.retry",
                    exc_type=type(exc).__name__,
                )
            report = self._persist_items(ctx, items, now=now)

        # 9a：被选中注入的记忆强化 / 复活（只改内存；永不进 pending）
        if not duplicate:
            try:
                reinforced = self.system.reinforce(ctx.retrieval, now=now)
                ctx.metrics["reinforced"] = reinforced
            except ContextViolation:
                raise
            except Exception as exc:
                self.metrics.event(
                    "turn.finish_error", stage="write_back.9a", exc_type=type(exc).__name__
                )
                ctx.metrics.setdefault("reinforced", 0)

        # 9b：只聚合本轮 injected_items（fill 之后），单次 step_update
        if not duplicate:
            try:
                deltas = aggregate(ctx.injected_items, user_id=ctx.user_id, alpha=alpha)
                if deltas:
                    self.system.affect_store.step_update(ctx.user_id, deltas, now=now)
                ctx.metrics["affect_deltas"] = deltas
            except ContextViolation:
                raise
            except Exception as exc:
                self.metrics.event(
                    "turn.finish_error", stage="write_back.9b", exc_type=type(exc).__name__
                )

        # 10：last_event_affect 只供下一轮 affect_match（不参与 user_affect）
        try:
            ctx.last_event_affect = first_non_neutral(items)
        except ContextViolation:
            raise
        except Exception as exc:  # pragma: no cover - 纯函数防御
            self.metrics.event(
                "turn.finish_error", stage="write_back.10", exc_type=type(exc).__name__
            )
            ctx.last_event_affect = None
        key = conversation_key(ctx.scope, ctx.user_id)
        if ctx.last_event_affect is None:
            self._last_event_affect.pop(key, None)
        else:
            self._last_event_affect[key] = dict(ctx.last_event_affect)
        return report

    def _persist_items(
        self,
        ctx: PreparedTurn,
        items: list[dict],
        *,
        now: datetime,
    ) -> WriteReport:
        """8b：逐条落盘；单条失败不影响其余条目，失败条目进 ctx.pending。"""
        report = WriteReport()
        valence = float(ctx.state.valence)
        for index, item in enumerate(items):
            # turn_key 只挂在第一条，避免第二条起被 remember 的幂等判定整轮跳过
            turn_key = ctx.turn_key if index == 0 else ""
            try:
                one = self.system.remember(
                    [item],
                    user_id=ctx.user_id,
                    scope=ctx.scope,
                    valence=valence,
                    turn_key=turn_key,
                    now=now,
                )
            except ContextViolation:
                raise
            except Exception as exc:
                report.failed += 1
                ctx.pending.append(item)
                self.metrics.event("mem_write.fail", exc_type=type(exc).__name__)
                self.metrics.event(
                    "turn.finish_error", stage="write_back.8b", exc_type=type(exc).__name__
                )
                continue
            report.created.extend(one.created)
            report.merged.extend(one.merged)
            report.skipped += one.skipped
            report.failed += one.failed
            report.pending = one.pending
            report.turn_duplicate = report.turn_duplicate or one.turn_duplicate
            if one.failed:
                ctx.pending.append(item)
        # 首条无效（被跳过）时兜底登记 turn_key，保证重放幂等；有失败条目不登记，
        # 否则 pending 里带同一 turn_key 的失败条目会被误判为"已落盘"而丢失。
        if (
            report.failed == 0
            and ctx.turn_key
            and (report.created or report.merged)
            and not self.system.store.has_turn_key(ctx.turn_key)
        ):
            anchor = (report.created or report.merged)[0]
            self.system.store.register_turn_key(ctx.turn_key, anchor.id)
        report.pending = self.system.pending_count
        return report

    def _record_extract_metrics(self, parsed: ExtractResult, prepared: PreparedTurn) -> None:
        """§12.1 事件：ok / empty / no_sentinel 由 pipeline 记；bad_json / fail 由 parser 记。"""
        fields = {
            "items": len(parsed.memories),
            "sentinel": parsed.sentinel_found,
            "raw_len": parsed.raw_len,
            "raw_hash": parsed.raw_hash,
            "gate": prepared.gate_passed,
        }
        if parsed.status == "ok":
            self.metrics.event("mem_extract.ok", **fields)
        elif parsed.status == "empty":
            self.metrics.event("mem_extract.empty", **fields)
        elif parsed.status == "no_sentinel" and prepared.gate_passed:
            # 命中闸门却没带哨兵才值得告警（§12.1）
            self.metrics.event("mem_extract.no_sentinel", **fields)

    # ------------------------------------------------------------------
    # 便捷入口
    # ------------------------------------------------------------------

    def run_turn(
        self,
        event: TurnEvent,
        llm_call: Callable[[PromptBundle], str],
        *,
        sender: Callable[[str], None] | None = None,
    ) -> TurnResult:
        prepared = self.prepare(event)
        raw = llm_call(prepared.prompt)
        return self.finish(prepared, raw, sender=sender)

    def close(self) -> None:
        self.summaries.save()
        self.system.close()
