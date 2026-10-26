"""每轮流程（v1.4 §7）。

实现为两阶段，便于与「LLM 调用不由本系统发起」的宿主（qq-bridge / DSH）集成：

- prepare：读 state（只读）→ 闸门（§4.1）→ 检索（§6.1 路由，与闸门解耦）
  → 组 prompt（§6.4 / §4.2.1）；
- finish：解析 raw（parser，永不抛异常）→ **先发送 answer**（§7 顺序 7）
  → 更新滚动摘要 / 最近轮次 → 写记忆（try/except）→ 强化 → 指标。

run_turn(event, llm_call) 是两者组合的便捷入口。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .config import Config
from .gate import is_light_message, should_extract
from .memory import MemorySystem, WriteReport
from .metrics import Metrics
from .models import utc_now
from .parser import ExtractResult, MemoryOutputParser
from .prompt import PromptBundle, build_prompt
from .retrieval import RetrievalResult
from .state import BotState, load_bot_state
from .summary import RollingSummary, conversation_key


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
        """§2.4 幂等键 = user_id + 消息 msgid；没有 msgid 时不做去重。"""
        if not self.msgid:
            return ""
        return f"{self.user_id}:{self.msgid}"


@dataclass
class PreparedTurn:
    turn_id: str
    event: TurnEvent
    gate_passed: bool
    light_path: bool
    retrieval: RetrievalResult
    prompt: PromptBundle
    bot_state: BotState
    summary_text: str
    now: datetime
    turn_key: str = ""

    @property
    def retrieval_ids(self) -> list[str]:
        return [entry.item.id for entry in self.retrieval.selected]


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


def user_id_hash(user_id: str) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]


def retrieval_summary(result: RetrievalResult) -> dict:
    return {
        "candidates": result.candidates,
        "selected": [entry.item.id for entry in result.selected],
        "tokens": result.tokens,
        "budget": result.budget,
        "degraded": result.degraded,
    }


class TurnPipeline:
    """§7 的每轮流程；线程内单写者使用。"""

    MAX_PREPARED = 128

    def __init__(self, system: MemorySystem, config: Config | None = None) -> None:
        self.system = system
        self.cfg = config or system.cfg
        self.metrics: Metrics = system.metrics
        self.parser = MemoryOutputParser(self.metrics)
        self.summaries = RollingSummary(self.cfg.summary_file, self.cfg.prompt)
        self._recent: dict[str, deque[tuple[str, str]]] = {}
        self._prepared: dict[str, PreparedTurn] = {}

    # ------------------------------------------------------------------
    # 阶段一：准备
    # ------------------------------------------------------------------

    def _recent_for(self, key: str) -> deque[tuple[str, str]]:
        if key not in self._recent:
            self._recent[key] = deque(maxlen=max(1, self.cfg.prompt.recent_turns))
        return self._recent[key]

    def prepare(self, event: TurnEvent) -> PreparedTurn:
        started = time.perf_counter()
        now = event.now or utc_now()
        turn_key = event.make_turn_key()

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
        light_path = bool(self.cfg.retrieval.light_path_enabled) and is_light_message(event.message)
        if light_path:
            retrieval = RetrievalResult(
                selected=[],
                candidates=0,
                tokens=0,
                budget=self.cfg.retrieval.budget_tokens,
                degraded=False,
            )
        else:
            retrieval = self.system.search(event.message, event.scope, event.user_id, now=now)

        # 3. 组 prompt：persona + 摘要 + 检索结果 + 最近 5 轮 + 当前消息
        key = conversation_key(event.scope, event.user_id)
        summary_text = self.summaries.get(key)
        prompt = build_prompt(
            persona=self.system.persona(),
            summary=summary_text,
            retrieval=retrieval,
            users=self.system.users,
            recent_turns=list(self._recent_for(key)),
            user_message=event.message,
            gate_passed=gate_passed,
        )

        turn_id = uuid.uuid4().hex
        prepared = PreparedTurn(
            turn_id=turn_id,
            event=event,
            gate_passed=gate_passed,
            light_path=light_path,
            retrieval=retrieval,
            prompt=prompt,
            bot_state=bot_state,
            summary_text=summary_text,
            now=now,
            turn_key=turn_key,
        )
        self._prepared[turn_id] = prepared
        while len(self._prepared) > self.MAX_PREPARED:
            self._prepared.pop(next(iter(self._prepared)))
        self.metrics.event(
            "turn.prepare",
            turn_id=turn_id,
            user_id_hash=user_id_hash(event.user_id),
            scope=event.scope,
            gate=gate_passed,
            light_path=light_path,
            candidates=retrieval.candidates,
            selected=len(retrieval.selected),
            tokens=retrieval.tokens,
            degraded=retrieval.degraded,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return prepared

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
        now = utc_now()
        raw_text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))

        # 6. 解析（永不抛异常；正文 = 哨兵前内容）
        parsed: ExtractResult = self.parser.parse(raw_text)
        self._record_extract_metrics(parsed, prepared)

        answer = parsed.answer

        # 7. 先发送回答（空正文不发空气泡）
        if sender is not None and answer.strip():
            sender(answer)

        # 8. 摘要 / 最近轮次
        key = conversation_key(prepared.event.scope, prepared.event.user_id)
        self.summaries.update(key, prepared.event.message, answer)
        self._recent_for(key).append((prepared.event.message, answer))

        # 9. 写记忆（独立 try/except；turn_key 幂等；valence = 编码时 bot 情绪快照）
        write_report = self.system.remember(
            parsed.memories,
            user_id=prepared.event.user_id,
            scope=prepared.event.scope,
            valence=prepared.bot_state.valence,
            turn_key=prepared.turn_key,
            now=now,
        )

        # 10. 被选中的记忆强化（只改内存；异常忽略）
        reinforced = 0
        try:
            reinforced = self.system.reinforce(prepared.retrieval, now=now)
        except Exception as exc:  # 强化失败不能影响回答
            self.metrics.event("memory.reinforce_fail", exc_type=type(exc).__name__)

        self.system.record_debug_raw(prepared.turn_id, raw_text)
        duration_ms = (time.perf_counter() - started) * 1000
        self._prepared.pop(prepared.turn_id, None)
        metrics_payload = {
            "extract_status": parsed.status,
            "gate": prepared.gate_passed,
            "light_path": prepared.light_path,
            "sentinel": parsed.sentinel_found,
            "items_in": len(parsed.memories),
            "items_out": len(parsed.memories),
            "written_created": len(write_report.created),
            "written_merged": len(write_report.merged),
            "write_failed": write_report.failed,
            "write_skipped": write_report.skipped,
            "turn_duplicate": write_report.turn_duplicate,
            "reinforced": reinforced,
            "retrieval_degraded": prepared.retrieval.degraded,
            "duration_ms": round(duration_ms, 3),
        }
        self.metrics.event(
            "turn.finish",
            turn_id=prepared.turn_id,
            user_id_hash=user_id_hash(prepared.event.user_id),
            scope=prepared.event.scope,
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
