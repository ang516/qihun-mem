# 接口契约（v1.6-rc4 冻结）

本文件是模块之间的**接口约定**，实现时必须严格遵守；修改契约必须先更新本文件并同步设计规范。
设计规范（当前权威）：[../QiHun-Memory-System-v1.6-rc4.md](../QiHun-Memory-System-v1.6-rc4.md)；
工程契约：[UPGRADE-CONTRACT-v1.6-rc4.md](UPGRADE-CONTRACT-v1.6-rc4.md)；
回归索引：[regression-rc1.md](regression-rc1.md)。

## 1. 工程约定

- Python `>=3.11`（本机 3.14），核心实现只允许标准库；测试用 `pytest`。
- 包根目录：`src/qihun_memory/`；测试目录：`tests/`。
- 日志统一用 `logging.getLogger("memory")`；库代码禁止 `print`。
- 不允许网络访问；不允许在 import 时做重 I/O。
- 时间一律带时区 ISO-8601（`models.utc_now_iso()`），解析用 `models.parse_iso()`；
  跨文件比较前统一换算 UTC；`diary.date` 按 `diary.timezone` 取本地日。
- 数据 schema 以规范 §3.1 / §3.4 为准；字段名不得改动。
- 模块之间以本文件为接口边界，不得绕过公共入口直接访问其他模块内部状态。
- v1.6 新接口全部带关键字默认值，保持向后兼容（`MemorySystem` / `TurnPipeline.prepare/finish` /
  既有 HTTP 路由 / CLI 子命令可用）。

## 2. 基础模块

### `models.py`

- `ALLOWED_TYPES = ("episode","fact","preference")`；`ALLOWED_SCOPES = ("group","dm")`；
  `ALLOWED_STATUS = ("active","dormant")`；`MAX_STABILITY = 90.0`。
- `ALLOWED_AFFECT_LABELS = ("joy","sadness","anger","anxiety","affection","disgust","neutral")`；
  `JOY_AFFECT_LABELS = {"joy","affection"}`；`ANGER_AFFECT_LABELS = {"anger","disgust"}`。
- `MemoryItem` 字段：`id, user_id, scope, type, content, created_at, last_access,
  access_count, mention_count, S, valence, status, tags, turn_key=None, affect=None, vector=None`。
  - **v1.6 / E1**：`mention_count` 默认 `0`（新建条目为 0；仅去重命中 active 时 +1）。
  - `vector` 是运行时字段（`list[float] | None`），**不写 JSONL**，由 `memory.vec.jsonl` 装载。
  - `to_dict()` 在 `affect` 非空时输出该键；`from_dict()` 对非法 affect 容忍（置 None，不抛）。
- `sanitize_affect(value) -> dict | None`：label 非法 / intensity 非数字 / 非有限 → None；
  intensity 越界 clamp [0,1] 并 round(...,6)；返回只含 `label/intensity` 两个键。
- `affect_label(affect) -> str`、`affect_intensity(affect) -> float`：安全读取，缺失 → neutral / 0.0。
- `Set(field, value)` / `Inc(field, delta=1)`：`update_memory` 的白名单操作符（frozen dataclass）。
- `parse_iso(value) -> datetime`（无时区按 UTC）；`utc_now()` / `utc_now_iso()`。

### `config.py`

- `Config`（frozen dataclass）字段：`data_dir, vault_dir, persona_path, embedding, decay, retrieval,
  affect, bridge, extraction, prompt, diary, gate, state, metrics, service`。
- `PromptConfig`（v1.6 增量，关键字默认值）：
  `total_tokens=3850, summary_tokens=200, recent_turns=5, persona_tokens=500, recent_tokens=750,
  diary_tokens=300, protocol_tokens=300, system_misc_tokens=600`。
- **`DiaryConfig`（v1.6 新增，frozen dataclass，字段名/默认值冻结）**：

```python
enabled: bool = True
trigger: str = "daily"          # daily | every_n | both
every_n: int = 50
daily_at: str = "23:50"         # 本地时间 HH:MM（timezone）
timezone: str = "Asia/Shanghai"
max_tokens: int = 300           # 单篇上限
inject_budget: int = 300        # 注入预算
inject_count: int = 2           # 最多注入篇数
retain_days: int = 30           # 硬窗口
max_per_scope: int = 30         # 同 scope_tag 窗口内上限
privacy_check: bool = True
bot_nickname: str = ""          # 生成时隐私替换（bot 自身昵称 → 我）
gen_model: str = ""             # 仅文档/宿主用，不参与逻辑
```

- `AffectConfig`：`initial_affection=0.5, initial_annoyance=0.0, initial_familiarity=0.3,
  step_alpha=0.1, stale_days=30.0, annoyance_decay_base=0.9, annoyance_decay_period_days=7.0`。
- `BridgeConfig`：`enabled=True, weight=0.7, budget_tokens=300`，以及 `strong_words / weak_words /
  entity_patterns`（规范 §10）。
- `DecayConfig` 含 `ka=0.3`；`RetrievalConfig` 含 `header_tokens=24, gamma_u=0.15,
  familiarity_eta=0.1, affect_match=False, gamma_a=0.1`。
- 路径属性：`user_affect_file`、`affect_bad_file`、`memory_file`、`vector_file`、`bad_file`、
  `archive_file`、`summary_file`、`bot_state_file`、`users_file`、`metrics_file`、
  **`diary_file` / `diary_archive_file` / `diary_bad_file`（v1.6，data_dir 下 diary*.jsonl）**。
- `load_config(path=None, data_dir=None, ensure_dirs=True) -> Config`；未知键告警忽略（向后兼容）。

### `tokens.py`

- `estimate_tokens(text: str) -> int`：CJK 1 token/字，其余 4 字符/token（确定性）。
  v1.6 的 prompt 总预算（3850）、日记单篇上限（300）、注入预算（300）均用它实测/估算。

### `lifecycle.py`

```python
compute_S(mode, type, valence=0.0, affect=None, S=None, R=None, cfg: DecayConfig | None = None) -> float
```

- `init`：`S0·(1 + k_v·|valence|)`，affect 非 neutral 再乘 `(1 + k_a·intensity)`，cap 90。
  `|valence|` 对称（C11：+1.0 与 -1.0 结果相同；valence=0 → S0）。
- `revive`：裸 S0（episode 7 / fact|preference 30），cap 90（不受 valence 影响）。
- `reinforce`：`min(S + S·(1+R), 90)`（S=7、R=0.5 → 17.5；不受 valence 影响）。
- 其它 mode：抛 `ValueError`（不许静默降级，G05）。
- 旧函数 `initial_stability / revive_stability / reinforce_stability` 保留为薄封装（内部调 `compute_S`），
  禁止出现第二份公式。
- 其它纯函数：`retention(age_days, stability_days)`、`effective_stability(item, avoidance_now, cfg)`、
  `retention_now(item, *, now, avoidance_now=0.0, cfg=None)`、`clamp_stability`、
  `mood_consistency(current_valence, item_valence)`、`mood_bias_factor(current_valence, item_valence, beta)`。

### `metrics.py`

- `Metrics(log_path=None, enabled=True)`；方法 `event(name, **fields)` / `counters()` /
  `count(name)` / `snapshot()` / `reset()`。
- 事件名用规范 §14.1 的固定名字；事件写入失败不得抛异常（已实现）。
- v1.6 新增事件：`mem_write.mention_fail`、`diary.ok/fail/skip/dup/privacy/truncated/cap_hit/inject/tokens/archive`、
  `prompt.budget_over`；字段只放计数 / 布尔 / scope / 日期，不落正文与昵称。

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

## 3. `parser.py`（规范 §5.2 / §5.3 / 附录 C D2）

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
sanitize_for_prompt(text: str) -> str                              # 注入脱敏（事实层 + 日记层共用）
```

- `MemoryOutputParser(metrics=None, extraction=None).parse(raw) -> ExtractResult` 保留兼容：
  `ExtractResult` 含 `raw_answer` 字段，`.answer = clean_answer_for_send(raw_answer)`；
  `status` ∈ `ok / empty / bad_json / no_sentinel / fail`。
- 算法顺序（规范 §5.3）：哨兵从后往前 → 括号平衡（字符串感知、未闭合切到文末）→ 有限清洗 →
  `json.loads` → 顶层校验 → `normalize_item` 逐条 → `dedup_within_batch` → ≤5 条。
- 坏条单条丢弃；affect 非法只丢字段（`mem_extract.affect_invalid`）；坏条记 `mem_extract.item_invalid`。
- `normalize_item`：content 500、tags 8×16、content 含哨兵/围栏丢弃；item 只含 `type/content/tags[/affect]`。
- `split_raw_candidate` 有哨兵时 `raw_answer` 一律取**最早哨兵前**；无哨兵时剔除全部候选块。
- **P17（v1.6）**：无哨兵但正文含 `{"memories":[...]}` 片段 → `clean_answer_for_send` 返回
  去掉 JSON 块后的正文，不整段保留 raw（硬规则 11b）。
- **边界**：解析层禁止调用 `_drop_fences`；发送层禁止调用 `_loads_candidate`；发送层函数不接收
  memories、不导入解析内部（G06）。
- `clean_answer_for_send`：哨兵前 → 剔候选块 → 不可见字符 / 首尾围栏 / 截断哨兵残片；最差原样；
  永不抛异常。
- 测试要求：P01~P17 + §10.3 affect 17~21（v1.5 编号）+ `dedup_key` 金样/property；性能（20k 左花括号 < 0.5s）；
  metrics 事件可观测；answer 永不含 `"memories"` 或截断残片。

## 4. `bridge.py`（v1.5）

```python
@dataclass(frozen=True)
class BridgeDecision:
    triggered: bool = False
    strong: bool = False
    reason: str = "none"        # disabled / strong / weak_count / weak_entity / none
    matched: tuple[str, ...] = ()

def should_bridge(query: str, cfg: BridgeConfig | None = None) -> BridgeDecision: ...
```

- 强词单命中触发；强词表含裸「群」（v1.5-r2 已知过触发，**v1.6-rc4 §0.1 显式推迟到 v1.7**）。
  弱词 ≥2 或弱词 + 实体指代（`entity_patterns`）触发。
- 方向：仅私聊可桥接群聊记忆；群聊禁止检索任何 dm 记忆（数据流向「群聊池 → 私聊上下文」，反向禁止）。
- 只收时间 / 地点 / 具体事件名词，不收日常活动动词。
- 反向断层话术（§7.6，只定态度不做功能）：群聊不引用私聊内容，用户追问时只回应
  「私聊的事不方便在群里说」；不实现任何 dm → 群聊的查询/拼接/注入。代码层由
  「群聊禁止检索任何 dm 记忆」保证（默认人设 `cli.DEFAULT_PERSONA` / `examples/persona.example.md` 已带该句）。
- 无 I/O、无状态；触发词表与实体表来自 `BridgeConfig`。测试不得依赖网络。

## 5. `retrieval.py`（规范 §7.1~7.4 / 附录 C D3）

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

## 6. `prompt.py`（规范 §2 / §7.4 / §9）

```python
EXTRACTION_PROTOCOL           # 含 affect 可选字段示例
render_memory_block(injected_items, users) -> str
measure_prompt_tokens(system_prompt: str, messages) -> int    # v1.6：同一 tokenizer 实测 system + messages
build_prompt(*, persona, summary, retrieval=None, injected_items=None, users, recent_turns,
             user_message, gate_passed, limits=None, diary_excerpts=(), metrics=None) -> PromptBundle
```

- `PromptBundle` 字段：`system_prompt / messages / memory_block / memory_ids / summary / protocol` 原有；
  v1.6 追加 `diary_block: str = ""`、`total_tokens: int = 0`、`truncated: bool = False`。
- 行头：`[记忆 m_0001|用户A|episode|R=0.82|affect=anger:0.6|10-23]`；bridge 行头
  `[群聊记忆 ...]`；affect 缺失/neutral 省略。
- 记忆块前附系统提示：「群聊记忆是用户本人在群里说过的话……不是别人说的」。
- 昵称 / content / 日记内容过 `sanitize_for_prompt`；记忆块只进用户上下文区，不与系统指令混排。
- 协议只在 `gate_passed` 时追加到系统提示末尾。
- **日记注入（v1.6）**：位置 = 用户上下文区「人格上下文」区，与【会话摘要】并列、在【相关记忆】之前；
  文本 = `diary.DIARY_PREFIX` + `diary.render_diary_block(excerpts)`；不注入时为空串。
- **总预算（v1.6）**：`measure_prompt_tokens(...) <= limits.total_tokens`（默认 3850）。超限按
  「先丢日记（2→1→0，记 `diary.truncated`）→ 再从尾部逐条丢检索 → 再从最旧逐轮丢最近轮（至少留 1 轮）
  → 最小可用集（persona + 最近 1 轮 + 协议）+ `prompt.budget_over`」截断，每步重新测量。
- 摘要先截 `limits.summary_tokens`（200）；persona 截 `persona_tokens`（500）。

## 7. `diary.py`（v1.6 新增；规范 §8 / 附录 C D7）

```python
DIARY_SCOPES = ("group", "dm")
DIARY_PREFIX: str      # §8.4 注入前缀原文

@dataclass(frozen=True)
class DiaryEntry:      # id / date / scope_tag / content / tokens / turn_range: tuple[str,...] / created_at
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: Mapping) -> "DiaryEntry": ...

@dataclass
class DiaryLoadReport: # total_lines / valid / bad / new_bad / bad_samples
@dataclass(frozen=True)
class DiaryPrompt:     # scope_tag / prompt / due / date / turn_range / reason
@dataclass
class DiaryRunReport:  # scope_tag / status / reason / entry_id / tokens
# status ∈ ok / fail / skip / dup / cap_hit / privacy / disabled / not_due
```

`DiaryStore`（唯一 `diary.jsonl` 读写入口；只读模式不得写盘）：

```python
DiaryStore(cfg, metrics=None, read_only=False)
load() -> DiaryLoadReport
entries -> dict[str, DiaryEntry]
append(entry) / rewrite() / archive(ids) -> int
archive_expired(now) -> int                # created_at <= now-retain_days；先 append archive 再 rewrite
active(scope_tag, now) -> list[DiaryEntry] # 窗口 (now-30d, now]，created_at 倒序，再 id 倒序
active_count(scope_tag, now) -> int
last_created_at(scope_tag) -> datetime | None
has_idempotent(date, scope_tag, turn_range) -> bool
next_id() -> "d_0001"                      # 加载时扫描 archive 续号，避免 id 重用
counts() -> dict
```

`DiaryWorker`（独立 worker；不 import pipeline / memory / retrieval，依赖全部由 callable 注入）：

```python
DiaryWorker(cfg, store, *, metrics=None, memory_items=None, state_loader=None, users=None,
            affect_snapshot=None, now_fn=None)
record_turn(scope, turn_id, at=None) -> None
due(scope, now=None) -> bool
prepare(scope, *, now=None, force=False) -> DiaryPrompt
finish(prepared, raw, *, now=None) -> DiaryRunReport      # 隐私 → 截断 → cap → 幂等 → 落盘；异常 → fail 不抛
generate(scope, llm, *, now=None, force=False) -> DiaryRunReport   # prepare + llm（失败重试 ≤1）+ finish
run_due(llm, *, now=None) -> list[DiaryRunReport]         # group / dm 各一篇，互不混合
```

模块函数：

```python
select_excerpts(store, scope_tag, *, now, budget=None, count=None, metrics=None) -> dict
# {"excerpts": [...], "tokens": int, "injected": int, "truncated": bool}
# 第 1 篇必装，第 2 篇仅在剩余预算内整篇装；记 diary.inject / diary.tokens；放不下记 diary.truncated；
# scope_tag 只允许 group/dm（mixed 返回空）。
render_diary_block(excerpts) -> str
# 每篇：[日记 YYYY-MM-DD | 群聊] 换行 <content>（dm → 私聊），篇间空行；无节选返回空串。
```

约束：daily 按 scope 各一篇（不是全局一篇）；every_n 按 scope 分别计数；trigger ∈ daily/every_n/both
（both 时 daily 优先，当日同 scope 已有日记 → dup）；幂等键 `(date, scope_tag, turn_range)`；
隐私护栏（他人昵称 → 有人、bot 昵称 → 我、user_id 字面量 / 哨兵 / 围栏 → 拒写）；
超 max_tokens 截断；同 scope 窗口内 ≥30 篇 cap；scope_tag 落盘只允许 group/dm，mixed 绝不自动生成/落盘。
`diary.py` 不得 import `parser` 下划线内部名（过 `sanitize_for_prompt` 即可），
且不得直接 IO `user_affect.json`（用注入的 `affect_snapshot` callable）。

## 8. `memory.py`

```python
class MemorySystem:
    affect_store: UserAffectStore
    diary: DiaryStore            # v1.6
    diary_worker: DiaryWorker    # v1.6（llm=None，由宿主经 CLI / HTTP 两阶段注入）
    def update_memory(self, item_id, ops, *, reason="") -> bool: ...
    def remember(self, ...) -> ...       # 去重（E4）→ compute_S("init") → 落盘
    def reinforce(self, ...) -> ...      # E5 复活优先 / active 强化，全部经 update_memory
    def search(self, ...) -> RetrievalResult   # 检索前 affect_store.decay
    def select_diary_excerpts(self, scope, *, now=None) -> dict   # v1.6：select_excerpts 的门面
    def retry_pending(self) -> ...       # 只重放新记忆；turn_key 幂等
    def maintain(self) -> MaintenanceReport
```

- `update_memory`：Set 白名单 `S / status / last_access`；Inc 白名单 `access_count / mention_count`；
  Inc 内部读旧值；S 写 clamp [0,90]；非法字段/值 → `state.write_guard`、整体不生效、返回 False。
- `remember`：新建条目 `mention_count=0`（E1）；去重只比对同 user + scope + type 的 **active** 条目
  （E4）；命中 active 只 `Set(last_access, now)` + `Inc(mention_count,1)`，不插新条、不强化、
  不写 JSONL 新行、不进 pending；失败记 `mem_write.mention_fail`。
- `reinforce`：dormant 且 sim>0.7 → 只 revive（`compute_S("revive")`、status=active、
  last_access=now；不 `Inc(access_count)`、不叠 reinforce）；active 且 sim>0.5 →
  `compute_S("reinforce", S, R_eff)` + `Inc(access_count,1)`；`revived_this_turn` 同轮去重（E5）。
- `_refresh_statuses` / `maintain` 的 status 写也经 `update_memory`。
- `maintain()`：逐阶段 try/except（失败记 `state.write_guard` / `mem_write.fail` / `diary.fail`）；
  原逻辑 + `diary.archive_expired` + `diary.rewrite`，report 增 `diary_archived`。
  维护任务**不生成日记**（生成走 `DiaryWorker` 的两阶段，由宿主在 daily_at / 启动补偿触发）。
- pending：只存新记忆（带 turn_key），重放前查已存在则跳过；强化/感情更新永不进 pending；
  v1.6 重放位置固定在 write_back 的 8b 开始处（单一位置）。
- `search()` 前 `affect_store.decay(user_id)`，并把 `UserAffect` 传给 Retriever。

## 9. `pipeline.py`

```python
PreparedTurn / TurnContext:
    # 冻结（构造后不可改）
    persona, state, summary, candidates, retrieval, prompt, injected_items,
    user_id, scope, gate_passed, turn_key, light_path, last_event_affect_at_read,
    diary_excerpts, users                     # v1.6 新增冻结字段
    # 可写白名单
    last_event_affect, metrics, pending

class TurnPipeline:
    def prepare(self, event) -> PreparedTurn: ...
    def finish(self, ctx, raw, sender=None) -> TurnResult: ...
    def run_turn(self, event, llm_call) -> TurnResult: ...
```

- 越界写冻结字段 → 抛 `ContextViolation` 并记 `ctx.violation`。
- `prepare`：`cfg.diary.enabled` 时 `select_excerpts(scope)`（失败 → 空 + `diary.fail`）；
  `build_prompt(..., diary_excerpts=..., metrics=metrics)`；metrics 记 `diary.inject` / `diary.tokens`。
- `finish` 顺序：parse → `clean_answer_for_send` → emit → summary/recent → write_back。
- `write_back` 顺序：8a familiarity → 8b（开始时 pending 重放 ≤1 次，再逐条 persist）→
  9a 强化/复活（只改内存）→ 9b affect 聚合（单次 step_update）→ 10 last_event_affect。
- 日记记账：`finish` 在 write_back 之前（摘要之后）对非重复 turn 调
  `diary_worker.record_turn(scope, turn_id)`（失败只记指标；不生成、不落盘）。
- `aggregate()` 唯一聚合函数；`first_non_neutral()` 只决定 `last_event_affect`。
- 各段独立 try/except；失败只记 `turn.*_error`，回答照常返回。

## 10. `service.py` / `cli.py` / `tools.py`

### 10.1 HTTP（v1.6 增量）

- 既有端点不变：`/health`、`/state`、`/user_affect`、`/metrics`、`/memory/list`、`/memory/search`、
  `/turn/prepare`、`/turn/finish`、`/obsidian/*`、`/users/refresh`、`/maintain`。
- `/health` 版本随包版本 **0.3.0**，并新增 `diary` 计数（无叙事层时为空 dict）。
- v1.6 新增：

| 方法 | 路径 | 请求 / 响应 |
|---|---|---|
| GET | `/diary/list?scope=&limit=` | → `{ok, total, counts, items:[{id,date,scope_tag,content,tokens,turn_range,created_at}]}`；created_at 倒序 |
| GET | `/diary/excerpts?scope=` | → `{ok, scope_tag, injected, tokens, truncated, excerpts:[...]}`；scope 必填 group/dm |
| POST | `/diary/prepare` | `{scope, force?}` → `{ok, diary_id, scope_tag, due, date, turn_range, reason, prompt}` |
| POST | `/diary/finish` | `{diary_id, raw}` → `{ok, scope_tag, status, reason, entry_id, tokens}`；`diary_id` 未知 → 404 |

- 错误码风格与现有一致；所有异常不外泄栈；token 鉴权覆盖全部端点（含 `/diary/*`）。

### 10.2 CLI（v1.6 增量）

```bash
python -m qihun_memory.cli diary list   [--scope group|dm] [--limit N] [--json]
python -m qihun_memory.cli diary prompt --scope group|dm [--force] [--json]   # --force=调试/补生成
python -m qihun_memory.cli diary add    --scope group|dm --raw-file PATH [--force] [--json]
```

- `init` 会创建空 `diary.jsonl`；`maintain` 帮助文本说明含「日记移档」。
- `--version` 输出 `qihun-memory 0.3.0`（帮助描述含 v1.6）。
- 既有 `list` / `affect` 等子命令不变。

### 10.3 `tools.py`

- 导出公共符号：`Set / Inc / compute_S / UserAffectStore / UserAffect / fill / should_bridge /
  clean_answer_for_send / normalize_text / dedup_key / normalize_item / dedup_within_batch /
  split_raw_candidate / parse_memories / validate_affect / aggregate / first_non_neutral /
  ContextViolation / TurnContext / load_bot_state` 等（以 `tools.__all__` 为准）；
  v1.6 如 re-export diary 公共函数以 `tools.__all__` 实际内容为准。

## 11. 验证命令

```bash
python -m pytest -q                                   # 全量（最小 CI：L1~L4 + A/B + D）
python -m pytest tests/test_examples.py -q            # 示例文件可用性
python -m pytest tests/test_v16_diary.py tests/test_v16_semantics.py \
  tests/test_v16_budget.py tests/test_v16_narrative.py tests/test_v16_integration.py \
  tests/test_v16_guards.py tests/test_v16_service_cli.py -q
python -m qihun_memory.cli --version                  # qihun-memory 0.3.0
```
