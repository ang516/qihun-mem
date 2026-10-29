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
    ka: float = 0.3
    lam: float = 0.2
    suppression_valence: float = -0.3
    suppression_avoidance: float = 0.5
    min_effective_ratio: float = 0.8


@dataclass(frozen=True)
class RetrievalConfig:
    """§6 检索参数。"""

    budget_tokens: int = 1200
    header_tokens: int = 24
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
    # v1.5 §7.2：rel/familiarity/user_affect/affect_match 调制
    gamma_u: float = 0.15
    familiarity_eta: float = 0.1
    affect_match: bool = False
    gamma_a: float = 0.1


@dataclass(frozen=True)
class AffectConfig:
    """v1.5 §5.3 / §5.4：user_affect 聚合与衰减。"""

    initial_affection: float = 0.5
    initial_annoyance: float = 0.0
    initial_familiarity: float = 0.3
    step_alpha: float = 0.1
    stale_days: float = 30.0
    annoyance_decay_base: float = 0.9
    annoyance_decay_period_days: float = 7.0


@dataclass(frozen=True)
class BridgeConfig:
    """v1.5 §7.6：私聊 → 同用户群聊记忆的单向桥接。"""

    enabled: bool = True
    weight: float = 0.7
    budget_tokens: int = 300
    # 强词单命中即触发；弱词需 ≥2 个或弱词 + 实体指代
    strong_words: tuple[str, ...] = (
        "群里",
        "群",
        "群里说的",
        "刚才群里",
        "群聊",
        "聊天记录",
    )
    weak_words: tuple[str, ...] = (
        "刚才",
        "刚刚",
        "之前",
        "上次",
        "前面",
        "说过",
        "提到",
        "聊过",
        "记录",
    )
    # 实体表只收时间 / 地点 / 具体事件名词；不含日常活动动词
    entity_patterns: tuple[str, ...] = (
        r"(?:昨|今|明|后|大后)天",
        r"(?:今|昨|明)晚",
        r"(?:这|下|上)(?:周|星期|个月|月)",
        r"周末|月底|年底|明年|今年",
        r"(?:周|星期)[一二三四五六日天]",
        r"\d{1,2}\s*[点时:：]\s*\d{0,2}",
        r"\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?",
        r"(?:上海|北京|广州|深圳|杭州|成都|重庆|武汉|西安|南京|天津|苏州|长沙|郑州|青岛|厦门|福州|合肥|济南|昆明|大连|沈阳|哈尔滨|长春|石家庄|太原|南昌|贵阳|南宁|海口|三亚|兰州|西宁|银川|乌鲁木齐|拉萨|呼和浩特|香港|澳门|台北|东京|大阪|首尔|新加坡)",
        r"[\u4e00-\u9fff]{2,8}(?:市|省|区|县|广场|机场|车站|火车站|高铁站|医院|公司|学校|大学|公园|酒店|餐厅|景区|岛|山|湖|路|街)",
        r"(?:出差|旅游|加班|考试|面试|搬家|生病|住院|结婚|生日|聚餐|开会|上课|放假|请假|回家|回老家|生娃|怀孕)",
    )


@dataclass(frozen=True)
class ExtractionConfig:
    """§4.2 提取结果上限。"""

    max_items: int = 5
    max_content: int = 500
    max_tags: int = 8
    max_tag_len: int = 16


@dataclass(frozen=True)
class PromptConfig:
    """§6.3 / §7 会话摘要与最近轮次（§1 预算：persona ≈500 / 最近 5 轮 ≈750）。

    v1.6-rc4 §2：8k 窗口最坏预算合计 3850 = 500 + 200 + 300 + 1200 + 750 + 300 + 600，
    新增 total_tokens（总预算）与 diary/protocol/system_misc 三个分项。
    """

    summary_tokens: int = 200
    recent_turns: int = 5
    persona_tokens: int = 500
    recent_tokens: int = 750
    # v1.6-rc4 §2：总预算与分项（实现契约 §2.1 冻结）
    total_tokens: int = 3850
    diary_tokens: int = 300
    protocol_tokens: int = 300
    system_misc_tokens: int = 600


@dataclass(frozen=True)
class DiaryConfig:
    """v1.6-rc4 §8 叙事层（情感日记）参数。

    字段名 / 默认值由实现契约 §2.1 冻结；diary.py 只读取本配置，不在别处写第二套默认值。
    - trigger ∈ daily / every_n / both；daily_at 为 timezone 时区的本地 HH:MM；
    - every_n / daily 均按 scope_tag（group / dm）分别触发与计数；
    - max_tokens 是单篇生成上限，inject_budget / inject_count 是注入预算与篇数上限；
    - retain_days 是硬窗口（created_at ≤ now-retain_days 移档），max_per_scope 是窗口内上限；
    - bot_nickname 仅用于隐私护栏替换（bot 自身昵称 → 我）；gen_model 只给宿主/文档参考，
      不参与本模块逻辑。
    """

    enabled: bool = True
    trigger: str = "daily"  # daily | every_n | both
    every_n: int = 50
    daily_at: str = "23:50"  # 本地时间 HH:MM（timezone 时区）
    timezone: str = "Asia/Shanghai"
    max_tokens: int = 300  # 单篇上限
    inject_budget: int = 300  # 注入预算
    inject_count: int = 2  # 最多注入篇数
    retain_days: int = 30  # 硬窗口
    max_per_scope: int = 30  # 同 scope 窗口内上限
    privacy_check: bool = True
    bot_nickname: str = ""  # 生成时用于隐私替换（bot 自身昵称 → 我）
    gen_model: str = ""  # 仅文档/宿主用，不参与逻辑


@dataclass(frozen=True)
class GateConfig:
    """§4.1 写入闸门。

    enforce_write：§11 规则 5「写入必须过闸门」的落地开关。闸门未命中的轮次，
    即使模型漂移输出了哨兵块也不写记忆（只影响写入，不影响检索与回答）。
    """

    enabled: bool = True
    enforce_write: bool = True
    extra_patterns: tuple[str, ...] = ()
    # v1.6-rc4 §7.1 轻路径：寒暄词表按「去标点后整句完全相等」匹配（单字项不做子串匹配）
    greeting_words: tuple[str, ...] = (
        "你好",
        "您好",
        "在吗",
        "在么",
        "早",
        "早安",
        "早上好",
        "晚安",
        "哈哈",
        "呵呵",
        "嗯",
        "嗯嗯",
        "哦",
        "哦哦",
        "好",
        "好的",
        "行",
        "收到",
        "谢谢",
        "再见",
    )


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
    "affect": AffectConfig,
    "bridge": BridgeConfig,
    "extraction": ExtractionConfig,
    "prompt": PromptConfig,
    "diary": DiaryConfig,
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


def _tuple_field_names(section_cls: type) -> set[str]:
    """默认值是 tuple 的配置项：JSON 里允许写数组，载入时统一转成 tuple。"""
    names: set[str] = set()
    for field_info in fields(section_cls):
        if isinstance(field_info.default, tuple):
            names.add(field_info.name)
    return names


def _build_section(section_cls: type, raw: Mapping[str, Any]) -> Any:
    allowed = {f.name for f in fields(section_cls)}
    tuple_fields = _tuple_field_names(section_cls)
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in allowed:
            log.warning("config: unknown key %s.%s ignored", section_cls.__name__, key)
            continue
        if key in tuple_fields and isinstance(value, list):
            kwargs[key] = tuple(str(x) for x in value)
        else:
            kwargs[key] = value
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
    affect: AffectConfig = field(default_factory=AffectConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    diary: DiaryConfig = field(default_factory=DiaryConfig)
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
    def user_affect_file(self) -> Path:
        return self._under_data("user_affect.json")

    @property
    def affect_bad_file(self) -> Path:
        return self._under_data("affect.bad.jsonl")

    @property
    def bot_state_file(self) -> Path:
        return self._under_data("state.json")

    @property
    def summary_file(self) -> Path:
        return self._under_data("summary.json")

    # ---------- 叙事层（v1.6-rc4 §3.4） ----------

    @property
    def diary_file(self) -> Path:
        return self._under_data("diary.jsonl")

    @property
    def diary_archive_file(self) -> Path:
        return self._under_data("diary.archive.jsonl")

    @property
    def diary_bad_file(self) -> Path:
        return self._under_data("diary.bad.jsonl")

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
