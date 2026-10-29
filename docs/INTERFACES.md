# 接口契约（v1.5-r2 冻结）

本文件是模块之间的**接口约定**，实现时必须严格遵守；修改契约必须先更新本文件并同步设计规范。
设计规范（当前权威）：[../QiHun-Memory-System-v1.5-r2.md](../QiHun-Memory-System-v1.5-r2.md)；
工程契约：[UPGRADE-CONTRACT-v1.5-r2.md](UPGRADE-CONTRACT-v1.5-r2.md)。

## 1. 工程约定

- Python `>=3.11`（本机 3.14），核心实现只允许标准库；测试用 `pytest`。
- 包根目录：`src/qihun_memory/`；测试目录：`tests/`。
- 日志统一用 `logging.getLogger("memory")`；库代码禁止 `print`。
- 不允许网络访问；不允许在 import 时做重 I/O。
- 时间一律 UTC ISO-8601（`models.utc_now_iso()`），解析用 `models.parse_iso()`。
- 数据 schema 以规范 §2.1 为准；字段名不得改动。
- 模块之间以本文件为接口边界，不得绕过公共入口直接访问其他模块内部状态。

## 2. 基础模块

### `models.py`

- `ALLOWED_TYPES = ("episode","fact","preference")`；`ALLOWED_SCOPES = ("group","dm")`；
  `ALLOWED_STATUS = ("active","dormant")`；`MAX_STABILITY = 90.0`。
- `ALLOWED_AFFECT_LABELS = ("joy","sadness","anger","anxiety","affection","disgust","neutral")`；
  `JOY_AFFECT_LABELS = {"joy","affection"}`；`ANGER_AFFECT_LABELS = {"anger","disgust"}`。
- `MemoryItem` 字段：`id, user_id, scope, type, content, created_at, last_access,
  access_count, mention_count, S, valence, status, tags, turn_key=None, affect=None, vector=None`。
  - `vector` 是运行时字段（`list[float] | None`），**不写 JSONL**，由 `memory.vec.jsonl` 装载。
  - `to_dict()` 在 `affect` 非空时输出该键；`from_dict()` 对非法 affect 容忍（置 None，不抛）。
- `sanitize_affect(value) -> dict | None`：label 非法 / intensity 非数字 / 非有限 → None；
  intensity 越界 clamp [0,1] 并 round(...,6)；返回只含 `label/intensity` 两个键。
- `affect_label(affect) -> str`、`affect_intensity(affect) -> float`：安全读取，缺失 → neutral / 0.0。
- `Set(field, value)` / `Inc(field, delta=1)`：`update_memory` 的白名单操作符（frozen dataclass）。
- `parse_iso(value) -> datetime`（无时区按 UTC）；`utc_now()` / `utc_now_iso()`。

### `config.py`

- `Config`（frozen dataclass）字段：`data_dir, vault_dir, persona_path, embedding, decay, retrieval,
  affect, bridge, extraction, prompt, gate, state, metrics, service`。
- `AffectConfig`：`initial_affection=0.5, initial_annoyance=0.0, initial_familiarity=0.3,
  step_alpha=0.1, stale_days=30.0, annoyance_decay_base=0.9, annoyance_decay_period_days=7.0`。
- `BridgeConfig`：`enabled=True, weight=0.7, budget_tokens=300`，以及 `strong_words / weak_words /
  entity_patterns`（见规范 §8.4）。
- `DecayConfig` 新增 `ka=0.3`；`RetrievalConfig` 新增 `header_tokens=24, gamma_u=0.15,
  familiarity_eta=0.1, affect_match=False, gamma_a=0.1`。
- 路径属性：`user_affect_file`、`affect_bad_file`、`memory_file`、`vector_file`、`bad_file`、
  `archive_file`、`summary_file`、`bot_state_file`、`users_file`、`metrics_file`。
- `load_config(path=None, data_dir=None,ensure_dirs=True) -> Config`。

### `tokens.py`

- `estimate_tokens(text: str) -> int`：CJK 1 token/字，其余 4 字符/token（确定性）。

### `lifecycle.py`

```python
compute_S(mode, type, valence=0.0, affect=None, S=None, R=None, cfg: DecayConfig | None = None) -> float
```

- `init`：`S0·(1 + k_v·|valence|)`，affect 非 neutral 再乘 `(1 + k_a·intensity)`，cap 90。
- `revive`：裸 S0（episode 7 / fact|preference 30），cap 90。
- `reinforce`：`min(S + S·(1+R), 90)`（r1 冻结公式；S=7、R=0.5 → 17.5）。
- 其它 mode：抛 `ValueError`（不许静默降级，A5）。
- 旧函数 `initial_stability / revive_stability / reinforce_stability` 保留为薄封装（内部调 `compute_S`），
  禁止出现第二份公式。
- 其它纯函数：`retention(age_days, stability_days)`、`effective_stability(item, avoidance_now, cfg)`、
  `retention_now(item, *, now, avoidance_now=0.0, cfg=None)`、`clamp_stability`、
  `mood_consistency(current_valence, item_valence)`、`mood_bias_factor(current_valence, item_valence, beta)`。

### `metrics.py`

- `Metrics(log_path=None, enabled=True)`；方法 `event(name, **fields)` / `counters()` /
  `count(name)` / `snapshot()` / `reset()`。
- 事件名用规范 §12.1 的固定名字；事件写入失败不得抛异常（已实现）。

### `state.py`

```python
BotState(valence, avoidance, updated_at, stale, source, reason)
load_bot_state(path, *, cfg=None, now=None, metrics=None) -> BotState   # 缺失/损坏/过期 → 中性 + stale
UserDirectory(path)  # nickname(user_id) / snapshot() / refresh(members) / save()
```

**`UserAffect` / `UserAffectStore`（v1.5，唯一写入口）**：

```python
@dataclass(frozen=True)
class UserAffect:
    affection: float = 0.5
    annoyance: float = 0.0
    familiarity: float = 0.3
    updated_at: str = ""
    stale: bool = False

class UserAffectStore:
    def __init__(self, path, cfg: AffectConfig | None = None, metrics: Metrics | None = None): ...
    def load(self) -> None                 # 容错；未知字段忽略；坏文件中性 + user_affect.fail
    def get(self, user_id, now=None) -> UserAffect       # 中性兜底；stale(≥30 天) → 中性且 familiarity=0
    def step_update(self, user_id, deltas, *, now=None) -> UserAffect   # x += delta·(1−x)，clamp [0,1]
    def decay(self, user_id, days=None, *, now=None) -> UserAffect      # annoyance *= 0.9^floor(days/7)
    def save(self) -> bool                 # 原子写；失败只记指标
```

- 每轮每分量调用方只调一次（I1 / 27-rev）；`stale` 由 `updated_at` 计算，不是持久化字段。
- 其他模块只能通过 get / step_update / decay 访问；禁止直接读写 `user_affect.json`。

## 3. `parser.py`（规范 §4.2 / §4.3 / 附录 D2）

### 3.1 公开接口

```python
normalize_text(text: str) -> str
dedup_key(item: Mapping) -> tuple[str, str]
validate_affect(item: Mapping) -> dict | None
normalize_item(m, limits: ExtractionConfig | None = None, metrics=None) -> dict | None
dedup_within_batch(items: list[dict]) -> list[dict]
split_raw_candidate(raw: str | None) -> tuple[str, str | None]     # (raw_answer, candidate)
parse_memories(candidate_json, limits=None, metrics=None) -> list[dict]
parse_llm_output(raw: str | None) -> tuple[str, list[dict]]        # raw_answer 未清洗
clean_answer_for_send(raw_answer: str | None) -> str               # 发送层唯一清洗点
sanitize_for_prompt(text: str) -> str                              # 注入脱敏
```

- `MemoryOutputParser(metrics=None, extraction=None).parse(raw) -> ExtractResult` 保留兼容：
  `ExtractResult` 增加 `raw_answer` 字段，`.answer = clean_answer_for_send(raw_answer)`；
  `status` ∈ `ok / empty / bad_json / no_sentinel / fail`。
- 算法顺序（规范 §4.3.1）：哨兵从后往前 → 括号平衡（字符串感知、未闭合切到文末）→ 有限清洗 →
  `json.loads` → 顶层校验 → `normalize_item` 逐条 → `dedup_within_batch` → ≤5 条。
- 坏条单条丢弃；affect 非法只丢字段（`mem_extract.affect_invalid`）；坏条记 `mem_extract.item_invalid`。
- `normalize_item`：content 500、tags 8×16、content 含哨兵/围栏丢弃；item 只含 `type/content/tags[/affect]`。
- `split_raw_candidate` 有哨兵时 `raw_answer` 一律取**最早哨兵前**；无哨兵时剔除全部候选块。
- **边界**：解析层禁止调用 `_drop_fences`；发送层禁止调用 `_loads_candidate`；发送层函数不接收
  memories、不导入解析内部（A6）。
- `clean_answer_for_send`：哨兵前 → 剔候选块 → 不可见字符 / 首尾围栏 / 截断哨兵残片；最差原样；
  永不抛异常。
- 测试要求：§10.2 全 16 条 + §10.3 affect 17~21 + `dedup_key` 金样/property；性能（20k 左花括号 < 0.5s）；
  metrics 事件可观测；answer 永不含 `"memories"` 或截断残片。

## 4. `bridge.py`（v1.5 新增）

```python
@dataclass(frozen=True)
class BridgeDecision:
    triggered: bool = False
    strong: bool = False
    reason: str = "none"        # disabled / strong / weak_count / weak_entity / none
    matched: tuple[str, ...] = ()

def should_bridge(query: str, cfg: BridgeConfig | None = None) -> BridgeDecision: ...
```

- 强词单命中触发；强词表含裸「群」（v1.5-r2 已知过触发，v1.6 收敛）。弱词 ≥2 或弱词 + 实体指代
  （`entity_patterns`）触发。
- 只收时间 / 地点 / 具体事件名词，不收日常活动动词。
- 无 I/O、无状态；触发词表与实体表来自 `BridgeConfig`。测试不得依赖网络。

## 5. `retrieval.py`（规范 §6.2~6.4 / 附录 D3）

```python
@dataclass(frozen=True)
class SelectedMemory:
    item: MemoryItem
    score: float
    r: float
    cos: float
    bm25: float
    sim: float                    # cos（语义可用）否则 bm25 归一值
    header_tokens: int = 20       # 实际装填成本由 fill 的 constraints.head_per_item 传入
    bridge: bool = False          # Retriever 用 config.retrieval.header_tokens（默认 24）
    source_pool: str = "dm"       # "dm" | "group" | "bridge"
    tokens: int = 0
    score_detail: dict | None = None

@dataclass(frozen=True)
class RetrievalResult:
    selected: list[SelectedMemory]
    candidates: int
    tokens: int
    budget: int
    degraded: bool
    injected_items: list = []     # fill 产出：记忆字段 + r/score/affect/bridge/source_pool/tokens/header_tokens
    bridge_hit: int = 0           # 最终注入的桥接条数（fill 之后）
    bridge_candidates: int = 0    # 跨池去重后、进 fill 前的桥接候选数
    bridge_truncated: bool = False
    dedup_removed: int = 0        # 跨池去重删除的重复对数
    dedup_degraded: bool = False
    affect_bias: int = 0          # 本轮 rel=1 且 affect_match 实际改变排序（affect_sim > 0）的条数

class Retriever:
    def __init__(self, embedder, cfg, metrics=None): ...
    def search(self, items, query, *, scope, user_id, user_affect=None,
               last_event_affect=None, bot_state=None, now=None) -> RetrievalResult: ...
```

**装填接口（冻结）**：

```python
fill(entries, constraints, *, metrics=None) -> {"injected_items": [...], "truncated": bool, "total_tokens": int}

# constraints 由 Retriever 显式传入；fill 本身不做 1200/300 的缺省兜底
constraints = {
    "total": cfg.retrieval.budget_tokens,
    "head_per_item": cfg.retrieval.header_tokens,
    "subgroup": {"bridge": min(300, cfg.bridge.budget_tokens)},
}
```

实现步骤（顺序固定）：

1. 候选池：群聊 = 全部 `scope=group`（混合池，不按 user_id 预过滤），`rel=1` 仅当前用户；
   私聊 = `scope=dm and user_id==user_id`；`should_bridge` 命中时追加同 user 的 active group 记忆。
2. BM25：CJK 字符 bigram + 拉丁词；`k1/b` 取配置；批内 min-max 归一化（max==min → 0）。
3. 语义分：embedder 可用且有向量时 `cos = dot(query_vec, item.vector)`；否则降级 BM25 并记
   `retrieval.degraded`。
4. R：`retention_now(item, now=now, avoidance_now=bot_state.avoidance or 0, cfg=cfg.decay)`。
5. 打分（顺序）：`(0.6cos+0.4bm25)` → ×R → ×type_bonus（rel=1 再 ×(1+0.1·familiarity)）→
   dormant×0.3 → ×(1+β(2A−1)) → rel=1 ×(1+0.15·(affection−annoyance)) →
   affect_match 开启且 last_event_affect 非空 ×(1+0.1·affect_sim) → bridge ×0.7。
6. **跨池去重（打分之后、排序之前）**：`dedup_key` 精确优先，余弦 >0.9 二次判定；重复对保留
   score 高者、同分保留 dm；删除对数记 `group_bridge.dedup`；向量不可用退化原文匹配并记
   `dedup.degraded`。
7. 排序确定性：`score` 降序 → `r` 降序 → `id` 升序。
8. 装填（最后一步）：单条成本 `head_per_item + estimate_tokens(content)`；跳过装不下的继续；
   总量 ≤ total；桥接子预算 ≤ subgroup.bridge；超限记 `group_bridge.truncated` /
   `retrieval.truncated`。**顺序是「过滤/建池 → 打分 → 跨池去重 → 排序 → fill」，不是先去重再打分。**
- 只读：不写盘、不改 items、不调 `update_memory`；`user_affect` 由调用方传入（`store.get`）。
- 硬规则 18 的配置上限由 Retriever 强制：桥接 weight 实际生效 ≤0.7、桥接子预算实际生效 ≤300。

## 6. `prompt.py`

```python
EXTRACTION_PROTOCOL           # 含 affect 可选字段示例
render_memory_block(injected_items, users) -> str
build_prompt(*, persona, summary, retrieval=None, injected_items=None, users, recent_turns,
             user_message, gate_passed, limits=None) -> PromptBundle
```

- 行头：`[记忆 m_0001|用户A|episode|R=0.82|affect=anger:0.6|10-23]`；bridge 行头
  `[群聊记忆 ...]`；affect 缺失/neutral 省略。
- 记忆块前附系统提示：「群聊记忆是用户本人在群里说过的话……不是别人说的」。
- 昵称 / content 过 `sanitize_for_prompt`；记忆块只进用户上下文区，不与系统指令混排。
- 协议只在 `gate_passed` 时追加到系统提示末尾。

## 7. `memory.py`

```python
class MemorySystem:
    affect_store: UserAffectStore
    def update_memory(self, item_id, ops, *, reason="") -> bool: ...
    def remember(self, ...) -> ...       # 去重 → compute_S("init") → 落盘
    def reinforce(self, ...) -> ...      # 复活 / 强化，全部经 update_memory
    def search(self, ...) -> RetrievalResult   # 检索前 affect_store.decay
    def retry_pending(self) -> ...       # 只重放新记忆；turn_key 幂等
    def maintain(self) -> MaintenanceReport
```

- `update_memory`：Set 白名单 `S / status / last_access`；Inc 白名单 `access_count / mention_count`；
  Inc 内部读旧值；S 写 clamp [0,90]；非法字段/值 → `state.write_guard`、整体不生效、返回 False。
- `remember`：dedup_key 精确优先、余弦二次；向量不可用记 `dedup.degraded`；affect 写入；
  `compute_S("init", ..., valence=state.valence, affect=item.affect)`。
- `reinforce`：dormant 复活 `compute_S("revive")`；active `compute_S("reinforce", S, R)`；经 update_memory。
- `_refresh_statuses` / `maintain` 的 status 写也经 `update_memory`。
- pending：只存新记忆（带 turn_key），重放前查已存在则跳过；强化/感情更新永不进 pending。
- `search()` 前 `affect_store.decay(user_id)`，并把 `UserAffect` 传给 Retriever。

## 8. `pipeline.py`

```python
PreparedTurn / TurnContext:
    # 冻结（构造后不可改）
    persona, state, summary, candidates, retrieval, prompt, injected_items,
    user_id, scope, gate_passed, turn_key, light_path, last_event_affect_at_read
    # 可写白名单
    last_event_affect, metrics, pending

class TurnPipeline:
    def prepare(self, event) -> PreparedTurn: ...
    def finish(self, ctx, raw, sender=None) -> TurnResult: ...
    def run_turn(self, event, llm_call) -> TurnResult: ...
```

- 越界写冻结字段 → 抛 `ContextViolation` 并记 `ctx.violation`。
- `finish` 顺序：parse → `clean_answer_for_send` → emit → summary/recent → write_back。
- `write_back` 顺序：8a familiarity → 8b persist → 9a 强化/复活（只改内存）→ 9b affect 聚合（单次 step_update）
  → 10 last_event_affect；9b 只消费本轮 `injected_items` 中 `user_id==当前用户` 且非 neutral 的 affect。
- `aggregate()` 唯一聚合函数；`first_non_neutral()` 只决定 `last_event_affect`。
- 各段独立 try/except；失败只记 `turn.*_error`，回答照常返回。

## 9. `service.py` / `cli.py` / `tools.py`

- 服务：item payload 增加 `affect`；search payload 增加
  `bridge={hit,candidates,truncated}`、`dedup={removed,degraded}`；新增 `GET /user_affect?user_id=`；
  `/health` 版本随包版本（0.2.0）。
- CLI：`list` 显示 affect；新增 `affect get --user <id>` / `affect decay --user <id>`；
  `--version` 输出 `qihun-memory 0.2.0`（帮助描述含 v1.5-r2）。
- `tools.py` 导出新增公共符号：`Set / Inc / compute_S / UserAffectStore / UserAffect / fill / should_bridge /
  clean_answer_for_send / normalize_text / dedup_key / normalize_item / dedup_within_batch /
  split_raw_candidate / parse_memories / validate_affect / aggregate / first_non_neutral /
  ContextViolation / TurnContext / load_bot_state` 等（以 `tools.__all__` 为准）。

## 10. 验证命令

```bash
python -m pytest -q                    # 全量（最小 CI：L1~L4 + A/B）
python -m pytest tests/test_parser.py -q
python -m qihun_memory.cli --version
```
