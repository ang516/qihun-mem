"""配置：v1.4 §8 参数总表。

所有参数集中在不可变 dataclass 中，既可代码构造也可从 JSON 载入。
路径规则：`data_dir` / `vault_dir` / `persona_path` 的相对值按配置文件所在目录解析。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("memory")

DEFAULT_CONFIG_FILE = "config.json"


# --------------------------------------------------------------------------
# 参数分组（默认值与 v1.4 §8 一致）
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EmbeddingConfig:
    """§6.5 embedding 依赖。不可用时自动降级为纯 BM25。"""

    enabled: bool = True
    backend: str = "sentence-transformers"  # "sentence-transformers" | "none"
    model: str = "BAAI/bge-small-zh-v1.5"
    model_path: str = ""  # 本地模型目录，优先于 model 名
    dim: int = 512
    device: str = "cpu"


@dataclass(frozen=True)
class DecayConfig:
    """§4.3 / §5 遗忘与强化参数。"""

    s0_episode: float = 7.0
    s0_fact: float = 30.0
    s_max: float = 90.0
    dormant_r: float = 0.1
    archive_r: float = 0.02
    revive_sim: float = 0.7
    reinforce_sim: float = 0.5
    dedupe_sim: float = 0.9
    kv: float = 0.5
    lam: float = 0.2
    suppression_valence: float = -0.3
    suppression_avoidance: float = 0.5
    min_effective_ratio: float = 0.8


@dataclass(frozen=True)
class RetrievalConfig:
    """§6 检索参数。"""

    budget_tokens: int = 1200
    header_tokens: int = 20
    w_vector: float = 0.6
    w_bm25: float = 0.4
    type_bonus: Mapping[str, float] = field(
        default_factory=lambda: {"preference": 1.1, "fact": 1.0, "episode": 0.9}
    )
    dormant_factor: float = 0.3
    mood_bias_enabled: bool = True
    mood_bias_beta: float = 0.2
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    light_path_enabled: bool = True


@dataclass(frozen=True)
class ExtractionConfig:
    """§4.2 提取结果上限。"""

    max_items: int = 5
    max_content: int = 500
    max_tags: int = 8
    max_tag_len: int = 16


@dataclass(frozen=True)
class PromptConfig:
    """§6.3 / §7 会话摘要与最近轮次（§1 预算：persona ≈500 / 最近 5 轮 ≈750）。"""

    summary_tokens: int = 200
    recent_turns: int = 5
    persona_tokens: int = 500
    recent_tokens: int = 750


@dataclass(frozen=True)
class GateConfig:
    """§4.1 写入闸门。

    enforce_write：§11 规则 5「写入必须过闸门」的落地开关。闸门未命中的轮次，
    即使模型漂移输出了哨兵块也不写记忆（只影响写入，不影响检索与回答）。
    """

    enabled: bool = True
    enforce_write: bool = True
    extra_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class StateConfig:
    """§2.3 state.json 过期判定。"""

    stale_hours: float = 6.0


@dataclass(frozen=True)
class MetricsConfig:
    """§12 观测。"""

    enabled: bool = True
    log_path: str = "logs/metrics.jsonl"  # 相对 data_dir
    debug_raw: bool = False
    debug_raw_retention_days: int = 7


@dataclass(frozen=True)
class ServiceConfig:
    """本地 HTTP 适配层（给 qq-bridge 等 Node 侧调用）。"""

    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""


# --------------------------------------------------------------------------
# 顶层配置
# --------------------------------------------------------------------------

_SECTIONS: dict[str, type] = {
    "embedding": EmbeddingConfig,
    "decay": DecayConfig,
    "retrieval": RetrievalConfig,
    "extraction": ExtractionConfig,
    "prompt": PromptConfig,
    "gate": GateConfig,
    "state": StateConfig,
    "metrics": MetricsConfig,
    "service": ServiceConfig,
}

_SCALARS = ("data_dir", "vault_dir", "persona_path")


def _resolve(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _build_section(section_cls: type, raw: Mapping[str, Any]) -> Any:
    allowed = {f.name for f in fields(section_cls)}
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in allowed:
            log.warning("config: unknown key %s.%s ignored", section_cls.__name__, key)
            continue
        kwargs[key] = value
    if "extra_patterns" in kwargs and isinstance(kwargs["extra_patterns"], list):
        kwargs["extra_patterns"] = tuple(str(x) for x in kwargs["extra_patterns"])
    return section_cls(**kwargs)


@dataclass(frozen=True)
class Config:
    """器魂记忆系统的完整配置。"""

    data_dir: str = "data"
    vault_dir: str = ""  # Obsidian 库根目录，只读用途；空 = 不启用
    persona_path: str = ""  # 空 = data_dir/persona.md（若不存在且配了 vault 则读 vault）
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    decay: DecayConfig = field(default_factory=DecayConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    state: StateConfig = field(default_factory=StateConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)

    # ---------- 构造 ----------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], base_dir: str | Path | None = None) -> "Config":
        if not isinstance(data, Mapping):
            raise ValueError("config must be a JSON object")
        base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
        kwargs: dict[str, Any] = {}
        for key, section_cls in _SECTIONS.items():
            raw = data.get(key)
            if raw is None:
                continue
            if not isinstance(raw, Mapping):
                raise ValueError(f"config section {key!r} must be an object")
            kwargs[key] = _build_section(section_cls, raw)
        for key in _SCALARS:
            if key in data and data[key] is not None:
                kwargs[key] = _resolve(str(data[key]), base) if str(data[key]) else ""
        # 未显式给出 data_dir 时，相对配置文件目录解析默认值
        if "data_dir" not in kwargs:
            kwargs["data_dir"] = _resolve("data", base)
        data_path = Path(kwargs["data_dir"])
        if not data_path.is_absolute():
            kwargs["data_dir"] = str((base / data_path).resolve())
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    # ---------- 路径 ----------

    def _under_data(self, name: str) -> Path:
        return Path(self.data_dir) / name

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def vault_path(self) -> Path | None:
        return Path(self.vault_dir) if self.vault_dir else None

    @property
    def memory_file(self) -> Path:
        return self._under_data("memory.jsonl")

    @property
    def vector_file(self) -> Path:
        return self._under_data("memory.vec.jsonl")

    @property
    def bad_file(self) -> Path:
        return self._under_data("memory.bad.jsonl")

    @property
    def archive_file(self) -> Path:
        return self._under_data("memory.archive.jsonl")

    @property
    def users_file(self) -> Path:
        return self._under_data("users.json")

    @property
    def bot_state_file(self) -> Path:
        return self._under_data("state.json")

    @property
    def summary_file(self) -> Path:
        return self._under_data("summary.json")

    @property
    def persona_file(self) -> Path:
        if self.persona_path:
            return Path(self.persona_path)
        candidate = self._under_data("persona.md")
        if candidate.exists():
            return candidate
        if self.vault_path:
            vault_persona = self.vault_path / "persona.md"
            if vault_persona.exists():
                return vault_persona
        return candidate

    @property
    def logs_path(self) -> Path:
        return self._under_data("logs")

    @property
    def metrics_file(self) -> Path:
        log_path = Path(self.metrics.log_path)
        return log_path if log_path.is_absolute() else self._under_data(log_path)

    def ensure_dirs(self) -> None:
        self.data_path.mkdir(parents=True, exist_ok=True)
        if self.metrics.enabled:
            self.metrics_file.parent.mkdir(parents=True, exist_ok=True)


def load_config(
    path: str | Path | None = None,
    data_dir: str | Path | None = None,
    *,
    ensure_dirs: bool = True,
) -> Config:
    """载入配置。

    优先级：显式 `path` > 当前目录 config.json > 全默认。
    `data_dir` 参数覆盖配置文件中的 data_dir（常用于测试与临时库）。
    `ensure_dirs=False` 时不在磁盘上创建 data/ 与 logs/（供 --dry-run 等只读场景，P1-1）。
    """
    data: dict[str, Any] = {}
    base: Path = Path.cwd()
    if path is not None:
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"config file not found: {config_path}")
        data = json.loads(config_path.read_text(encoding="utf-8"))
        base = config_path.parent
    else:
        default_path = Path.cwd() / DEFAULT_CONFIG_FILE
        if default_path.is_file():
            data = json.loads(default_path.read_text(encoding="utf-8"))
            base = default_path.parent
    if data_dir is not None:
        data = dict(data)
        data["data_dir"] = str(data_dir)
        if path is None:
            base = Path.cwd()
    config = Config.from_dict(data, base_dir=base)
    if ensure_dirs:
        config.ensure_dirs()
    return config
