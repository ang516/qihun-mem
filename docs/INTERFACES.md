# 接口契约（冻结）

本文件是模块之间的**接口约定**，实现时必须严格遵守；修改契约需要 Lead 同意。
设计规范：仓库根目录 `QiHun-Memory-System-v1.4.md`。

## 1. 工程约定

- Python `>=3.11`（本机 3.14），核心实现只允许标准库；测试用 `pytest`。
- 包根目录：`src/qihun_memory/`；测试目录：`tests/`。
- 日志统一用 `logging.getLogger("memory")`；库代码禁止 `print`。
- 不允许网络访问；不允许在 import 时做重 I/O。
- 时间一律 UTC ISO-8601（`models.utc_now_iso()`），解析用 `models.parse_iso()`。
- 数据 schema 以 §2.1 为准；字段名不得改动。
- 每个模块只改自己的文件（见任务 write scope），不要动其他人的文件。

## 2. 已有基础模块（Lead 维护，不要修改）

### `models.py`
- `MemoryItem` 字段：`id, user_id, scope, type, content, created_at, last_access,
  access_count, mention_count, S, valence, status, tags, turn_key=None, vector=None`。
- `vector` 是运行时字段（`list[float] | None`），**不写 JSONL**，由 `memory.vec.jsonl` 装载。
- `ALLOWED_TYPES = ("episode","fact","preference")`，`ALLOWED_SCOPES = ("group","dm")`。
- `to_dict()` / `from_dict()`、`created_dt` / `last_access_dt` 属性可直接用。
- `parse_iso(value) -> datetime`（无时区按 UTC）。

### `config.py`
- `Config`（frozen dataclass）字段：`data_dir, vault_dir, persona_path, embedding,
  decay, retrieval, extraction, prompt, gate, state, metrics, service`。
- 嵌套配置类：`EmbeddingConfig, DecayConfig, RetrievalConfig, ExtractionConfig,
  PromptConfig, GateConfig, StateConfig, MetricsConfig, ServiceConfig`（默认值 = §8）。
- `load_config(path=None, data_dir=None) -> Config`。

### `tokens.py`
- `estimate_tokens(text: str) -> int`：CJK 1 token/字，其余 4 字符/token（确定性）。

### `lifecycle.py`
- `retention(age_days, stability_days) -> float`：`exp(-Δt/S)`。
- `effective_stability(item, avoidance_now, cfg: DecayConfig) -> float`：§5.2 压抑调制 S_eff。
- `retention_now(item, *, now, avoidance_now=0.0, cfg=None) -> float`：含压抑的当前 R。
- `initial_stability(item_type, valence, cfg) -> float`：§4.3 `S0(type)·(1+k_v·|valence|)`，cap 90。
- `revive_stability(item_type, cfg) -> float`：中性 S0。
- `reinforce_stability(stability, r, cfg) -> float`：`min(S·(1+R), 90)`。
- `clamp_stability(stability, cfg) -> float`。
- `mood_consistency(current_valence, item_valence) -> float`：同号 1.0 / 异号 0.0 / 任一侧 0 → 0.5。
- `mood_bias_factor(current_valence, item_valence, beta) -> float`：`1 + β(2A−1)`。

### `metrics.py`
- `Metrics(log_path=None, enabled=True)`，方法 `event(name, **fields)` / `counters()` /
  `count(name)` / `snapshot()` / `reset()`。事件名用 §12.1 的固定名字。
- 事件写入失败不得抛异常（已实现）。

### `state.py`
- `BotState(valence, avoidance, updated_at, stale, source, reason)`。
- `load_bot_state(path, *, cfg=None, now=None, metrics=None) -> BotState`：缺失/损坏/过期 → 中性 + stale。
- `UserDirectory(path)`：`nickname(user_id)`、`snapshot()`、`refresh(members)`。

## 3. 待实现模块 A：`src/qihun_memory/parser.py`

对应规范 §4.2（哨兵协议 / 解析流水线 / 校验 / 注入防护）。

### 公开接口

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ExtractResult:
    answer: str                          # 回答正文（= 哨兵前内容，经 _drop_fences 清洗）
    memories: list[dict]                 # 通过校验的条目，键仅 type/content/tags
    status: str                          # "ok" | "empty" | "bad_json" | "no_sentinel" | "fail"
    sentinel_found: bool
    truncated: bool = False              # 超过 5 条被截断，或候选 JSON 未闭合
    exc_type: str | None = None
    raw_len: int = 0
    raw_hash: str = ""                   # sha256(raw)[:16]，空输入为 ""
    error: str | None = None             # 仅诊断用（不含正文）

class MemoryOutputParser:
    def __init__(self, metrics: Metrics | None = None) -> None: ...
    def parse(self, raw: str | None) -> ExtractResult: ...

def parse_llm_output(raw: str | None) -> tuple[str, list[dict]]: ...
def sanitize_for_prompt(text: str) -> str: ...
```

### status 语义

| status | 条件 |
|---|---|
| `ok` | 解析成功且 `memories` 非空（逐条校验后） |
| `empty` | 解析成功但 0 条（含空 memories 数组） |
| `bad_json` | 找到哨兵或 memories 候选，但 JSON 无法解析 / 顶层结构非法 |
| `no_sentinel` | 没有哨兵，也没有 memories 候选 |
| `fail` | 解析器内部未预期异常（已兜底，不抛） |

- `answer` 永久不含隐藏 JSON，包括截断残片（§4.2.2 第 2 步未闭合切到文末）。
- 无哨兵但有候选（围栏包住的 JSON 等）：走括号平衡兜底，`sentinel_found=False`；
  能否解析成功决定 status 为 `ok`/`empty`/`bad_json`。
- `truncated=True` 时 metrics 记 `mem_extract.truncated`。
- parser 自身负责记 metrics：`bad_json` 与 `fail` 各记一次（字段：`raw_len`、`raw_hash`、
  `exc_type`）；`truncated` 记一次。`ok`/`empty`/`no_sentinel` 由调用方（pipeline）记。

### 必须遵守的算法（§4.2.2，顺序不可换）

1. 哨兵切分：收集所有哨兵起始位置，**从后往前**逐个尝试；闭合标签缺失则取到末尾；
   取第一个能解析出 `memories` 列表的。
2. 括号平衡扫描：只从 memories 起点模式扫描；字符串感知；未闭合切到文末。
3. 有限清洗：去 BOM/零宽字符；**只剥首尾**围栏；仅当 `json.loads` 失败才在字符串外去尾逗号。
4. `json.loads`。
5. 顶层校验：`dict` 且 `memories` 是 `list`。
6. 逐条校验：`type` 合法；`content` 是 `str`、剔除控制字符、strip 后非空、截断 500；
   `tags` 是 list 时取前 8 个 str、每个 ≤16 字符，否则 `[]`；坏条单条丢弃。
   `content` 含哨兵串或三反引号围栏 → 丢弃该条。
7. 批内去重：同 `type` + 同 `content` 只留一条；最多 5 条。

### 绝对禁止

- 用正则直接抠 `{...}` 嵌套 JSON；靠三反引号分割取 JSON 的写法；
- 解析失败时把半截 JSON 当回答返回（正文必须切掉残片）；
- 抛出异常（`parse()` 必须永远返回 `ExtractResult`）。

### 注入防护 `sanitize_for_prompt(text)`

- 把哨兵起止串替换为不可被解析为哨兵的形式（如全角形式）；
- 把三反引号围栏替换为全角或空；
- 幂等、永不抛异常；空输入返回空串。

### 测试要求（`tests/test_parser.py`）

覆盖 §10.2 的 1–12、14 号用例（解析相关部分）+ 补充：
- 纯 JSON、前置/后置解释、围栏、尾逗号、截断、正文含空对象花括号、多块取最后、
  memories 非数组、空数组、部分坏条、注入、无哨兵兜底；
- 性能：构造 20k 个左花括号的正文 + 合法哨兵块，`parse()` < 0.5s；
- metrics：bad_json / fail / truncated 事件可被 `Metrics(log_path=tmp)` 观测；
- 断言 `answer` 永不含 `"memories"` 或截断残片。

## 4. 待实现模块 B：`src/qihun_memory/embedding.py` + `retrieval.py`

### embedding.py（§6.5）

```python
class EmbeddingUnavailable(RuntimeError): ...

class Embedder(ABC):
    name: str
    dim: int
    @property
    def available(self) -> bool: ...
    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...  # L2 归一化向量

class NullEmbedder(Embedder):   # available=False, dim=0, encode 抛 EmbeddingUnavailable
class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5",
                 model_path: str = "", device: str = "cpu", dim: int = 512) -> None: ...

def get_embedder(cfg: EmbeddingConfig) -> Embedder:
    """backend != "sentence-transformers" 或 enabled=False → NullEmbedder；
    否则尝试加载本地/已安装的 sentence-transformers；任何失败 → NullEmbedder（只 warning 一次）。
    禁止联网下载：优先 model_path；model 名只在库已装且用户配置了 model 时尝试。"""
```

- `encode` 返回的每个向量必须 L2 归一化（cos_sim 退化为点积）；空输入返回 `[]`。
- 测试不得依赖 sentence-transformers / 不得联网；用本地 FakeEmbedder 验证。

### retrieval.py（§6.2 / §6.3）

```python
@dataclass(frozen=True)
class SelectedMemory:
    item: MemoryItem
    score: float
    r: float
    cos: float
    bm25: float
    sim: float            # cos（语义可用）否则 bm25 归一化值
    header_tokens: int = 20

@dataclass(frozen=True)
class RetrievalResult:
    selected: list[SelectedMemory]
    candidates: int
    tokens: int
    budget: int
    degraded: bool

class Retriever:
    def __init__(self, embedder: Embedder, cfg: Config, metrics: Metrics | None = None): ...
    def search(self, items: Iterable[MemoryItem], query: str, *,
               scope: str, user_id: str,
               bot_state: BotState | None = None,
               now: datetime | None = None) -> RetrievalResult: ...
```

实现步骤（顺序固定）：

1. **先过滤**：`scope=="group"` → 只留 `item.scope=="group"`；
   `scope=="dm"` → 只留 `item.scope=="dm" and item.user_id==user_id`（§3.2）。
2. **BM25**：中文按 CJK 字符 bigram（单字 run 用单字）+ 拉丁/数字按小写分词；
   `k1/b` 取配置；对**本批候选**做 min-max 归一化到 [0,1]（max==min → 全部 0）。
3. **语义分**：`embedder.available` 且有候选带 `item.vector`（长度 = `embedder.dim`）时，
   `cos = dot(query_vec, item.vector)`（已归一化）；缺 vector 的条目 cos=0。
   以下情况降级：`weights=(w_vector=0, w_bm25=1)`、`degraded=True`，并记一次
   `retrieval.degraded`（字段 `reason`）：
   - embedder 不可用；或 query 编码抛异常；或没有任何候选带可用向量。
4. **R**：`lifecycle.retention_now(item, now=now, avoidance_now=bot_state.avoidance if bot_state else 0.0, cfg=cfg.decay)`。
5. **score**：

   ```text
   base = w_vec * cos + w_bm25 * bm25
   score = base * R * type_bonus[item.type]
   if item.status == "dormant": score *= cfg.retrieval.dormant_factor
   if cfg.retrieval.mood_bias_enabled and bot_state is not None:
        score *= lifecycle.mood_bias_factor(bot_state.valence, item.valence, cfg.retrieval.mood_bias_beta)
   ```

   （全部系数取 `cfg`；`type_bonus` 缺省 1.0。）
6. **排序**：`score` 降序；相同则 `r` 降序；再相同按 `id` 升序（确定性）。
7. **预算装填**（§6.3）：按序装入，单条成本 = `header_tokens + estimate_tokens(content)`；
   装不下就**跳过该条继续**（不截断正文）；累计 ≤ `budget_tokens`；`tokens` 为实际占用。
8. 不得修改 `items`、不得写盘；`sim` 供 §5.3 强化判定（cos 可用则用 cos，否则 bm25 归一值）。

### 测试要求（`tests/test_retrieval.py`、可加 `tests/test_embedding.py`）

- scope 隔离（group/dm 互不可见；dm 只检索同一 user）；
- 打分公式与排序（至少验证 type_bonus、dormant×0.3、心境一致性同号/异号、R 衰减）；
- 预算装填：含 header 开销、大条目跳过、不截断；
- 降级：NullEmbedder → 纯 BM25 且记 `retrieval.degraded`；部分条目缺 vector；
- BM25 归一化边界（max==min、空候选）；
- 确定性（同输入同输出，与传入顺序无关）；
- 无网络、无 sentence-transformers 依赖。

## 5. 其它模块（Lead 维护）

`store.py`（jsonl 读写容错 / 向量 sidecar / 原子重写）、`memory.py`（生命周期与写入）、
`gate.py`（§4.1 闸门）、`summary.py`、`prompt.py`、`pipeline.py`（§7 每轮流程）、
`obsidian.py`（只读库）、`service.py`、`cli.py`、`tools.py`。

## 6. 验证命令

```bash
python -m pytest -q                    # 全量
python -m pytest tests/test_parser.py -q
```
