"""公共 API 门面（对应 §2 存储结构里的 tools.py：检索 / 衰减 / 提取后处理）。

库内部实现分布在 parser / store / memory / retrieval / pipeline / state 等模块；
外部宿主（CLI、HTTP 适配层、qq-bridge）统一从这里取入口，避免依赖内部路径。

v1.5-r2 新增导出：状态单点操作符（Set/Inc）、compute_S、UserAffectStore、
装填接口 fill、发送层唯一清洗点 clean_answer_for_send、聚合函数 aggregate 等。
"""

from __future__ import annotations

from .bridge import BridgeDecision, should_bridge
from .config import Config, load_config
from .embedding import Embedder, NullEmbedder, get_embedder
from .lifecycle import (
    compute_S,
    effective_stability,
    initial_stability,
    mood_bias_factor,
    reinforce_stability,
    retention,
    retention_now,
    revive_stability,
)
from .memory import MaintenanceReport, MemorySystem, WriteReport
from .models import (
    ALLOWED_AFFECT_LABELS,
    ANGER_AFFECT_LABELS,
    JOY_AFFECT_LABELS,
    Inc,
    MemoryItem,
    Set,
    affect_intensity,
    affect_label,
    sanitize_affect,
)
from .obsidian import ReadOnlyObsidianVault, VaultError, vault_from_config
from .parser import (
    ExtractResult,
    MemoryOutputParser,
    clean_answer_for_send,
    dedup_key,
    dedup_within_batch,
    normalize_item,
    normalize_text,
    parse_llm_output,
    parse_memories,
    sanitize_for_prompt,
    split_raw_candidate,
    validate_affect,
)
from .pipeline import (
    ContextViolation,
    PreparedTurn,
    TurnContext,
    TurnEvent,
    TurnPipeline,
    TurnResult,
    aggregate,
    first_non_neutral,
    retrieval_summary,
)
from .prompt import build_prompt, render_memory_block
from .retrieval import RetrievalResult, Retriever, SelectedMemory, fill
from .state import UserAffect, UserAffectStore, load_bot_state
from .store import MemoryStore
from .tokens import estimate_tokens

__all__ = [
    "Config",
    "load_config",
    "BridgeDecision",
    "should_bridge",
    "Embedder",
    "NullEmbedder",
    "get_embedder",
    # 公式 / 遗忘（compute_S 是唯一 S 入口）
    "compute_S",
    "effective_stability",
    "initial_stability",
    "mood_bias_factor",
    "reinforce_stability",
    "retention",
    "retention_now",
    "revive_stability",
    # 状态单点
    "Set",
    "Inc",
    "UserAffect",
    "UserAffectStore",
    "load_bot_state",
    # affect
    "ALLOWED_AFFECT_LABELS",
    "JOY_AFFECT_LABELS",
    "ANGER_AFFECT_LABELS",
    "sanitize_affect",
    "affect_label",
    "affect_intensity",
    # 记忆门面
    "MaintenanceReport",
    "MemorySystem",
    "WriteReport",
    "MemoryItem",
    "ReadOnlyObsidianVault",
    "VaultError",
    "vault_from_config",
    # 解析 / 发送层
    "ExtractResult",
    "MemoryOutputParser",
    "parse_llm_output",
    "parse_memories",
    "split_raw_candidate",
    "clean_answer_for_send",
    "normalize_item",
    "normalize_text",
    "dedup_key",
    "dedup_within_batch",
    "validate_affect",
    "sanitize_for_prompt",
    # 每轮流程
    "ContextViolation",
    "PreparedTurn",
    "TurnContext",
    "TurnEvent",
    "TurnPipeline",
    "TurnResult",
    "retrieval_summary",
    "aggregate",
    "first_non_neutral",
    # 检索 / prompt
    "build_prompt",
    "render_memory_block",
    "RetrievalResult",
    "Retriever",
    "SelectedMemory",
    "fill",
    "MemoryStore",
    "estimate_tokens",
]
