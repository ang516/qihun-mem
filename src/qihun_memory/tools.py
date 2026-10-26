"""公共 API 门面（对应 §2 存储结构里的 tools.py：检索 / 衰减 / 提取后处理）。

库内部实现分布在 parser / store / memory / retrieval / pipeline 等模块；
外部宿主（CLI、HTTP 适配层、qq-bridge）统一从这里取入口，避免依赖内部路径。
"""

from __future__ import annotations

from .config import Config, load_config
from .embedding import Embedder, NullEmbedder, get_embedder
from .lifecycle import (
    effective_stability,
    initial_stability,
    mood_bias_factor,
    reinforce_stability,
    retention,
    retention_now,
    revive_stability,
)
from .memory import MaintenanceReport, MemorySystem, WriteReport
from .models import MemoryItem
from .obsidian import ReadOnlyObsidianVault, VaultError, vault_from_config
from .parser import ExtractResult, MemoryOutputParser, parse_llm_output, sanitize_for_prompt
from .pipeline import PreparedTurn, TurnEvent, TurnPipeline, TurnResult, retrieval_summary
from .prompt import build_prompt, render_memory_block
from .retrieval import RetrievalResult, Retriever, SelectedMemory
from .store import MemoryStore
from .tokens import estimate_tokens

__all__ = [
    "Config",
    "load_config",
    "Embedder",
    "NullEmbedder",
    "get_embedder",
    "effective_stability",
    "initial_stability",
    "mood_bias_factor",
    "reinforce_stability",
    "retention",
    "retention_now",
    "revive_stability",
    "MaintenanceReport",
    "MemorySystem",
    "WriteReport",
    "MemoryItem",
    "ReadOnlyObsidianVault",
    "VaultError",
    "vault_from_config",
    "ExtractResult",
    "MemoryOutputParser",
    "parse_llm_output",
    "sanitize_for_prompt",
    "PreparedTurn",
    "TurnEvent",
    "TurnPipeline",
    "TurnResult",
    "retrieval_summary",
    "build_prompt",
    "render_memory_block",
    "RetrievalResult",
    "Retriever",
    "SelectedMemory",
    "MemoryStore",
    "estimate_tokens",
]
