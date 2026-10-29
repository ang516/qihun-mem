# v1.6-rc4 工程升级契约（工程边界与回归验收）

> 目标：把仓库从 **v1.5-r2 冻结整合版** 升级到 **v1.6-rc4 冻结候选版**。
> 规范正文：[../QiHun-Memory-System-v1.6-rc4.md](../QiHun-Memory-System-v1.6-rc4.md)（仓库根目录副本与 `review/QiHun-Memory-System-v1.6-rc4.md` 逐字节一致）。
> 接口细节以 [INTERFACES.md](INTERFACES.md) 为准；回归索引见 [regression-rc1.md](regression-rc1.md)。
> 公式、阈值、语义以规范正文为准；本文件只钉接口、文件归属、指标、验收与回滚方式。
> 规范与本文件冲突时：公式/语义以规范为准，接口/归属以本文件为准，并立即修订另一份文件。

## 0. 总原则

- **语义只做登记过的增量**：事实层冻结基准保持 v1.5-r2，唯一变更来自规范 §0.1 显式登记项
  （E1 mention_count、E4 去重范围、E5 复活优先级、叙事层预算 300、桥接方向措辞）；
  未登记的行为一律不改。
- **向后兼容**：`MemorySystem`、`TurnPipeline.prepare/finish`、既有 HTTP 路由与 CLI 子命令
  保持可用；v1.6 只做增量（新参数全部带关键字默认值）。
- **标准库 only**：`pyproject.toml` 的 `dependencies` 保持 `[]`；Python ≥ 3.11（CI 矩阵 3.11~3.14）。
- **异常兜底**：叙事层 / 感情 / 写盘任一步失败只记指标 + 降级，绝不降低回答可用性
  （规范硬规则 13 / 17 / 19）。日记生成走独立 worker，不计入每轮 LLM 调用（硬规则 4 / 20）。
- **指标隐私**：只记长度 / 哈希 / 计数 / 异常类型 / scope / 日期，默认不落正文与昵称（规范 §14.2）。
- **注入防护**：外部文本（persona / 用户消息 / 日记）进 prompt 前必须过
  `parser.sanitize_for_prompt`；`diary.py` 不得 import parser 内部名。
- **状态单点不破**：S / status / 计数只经 `update_memory`；user_affect 只经 `UserAffectStore`；
  S 公式只经 `compute_S`；去重键只经 `dedup_key`；日记落盘只经 `DiaryStore`。
- 跨文件接口变更必须先改规范与 [INTERFACES.md](INTERFACES.md)，再改代码。

## 1. v1.6 功能增量（相对 v1.5-r2）

1. **叙事层**：新增 `diary.jsonl` / `diary.archive.jsonl` / `diary.bad.jsonl`，
   `DiaryStore`（唯一读写入口）与 `DiaryWorker`（独立 worker，不依赖 pipeline / memory / retrieval）；
   日记只提供人格语境，不参与检索、打分、装填 1200 预算、S/R、scope 过滤与 per-user 记忆（§8.1 / D7）。
2. **叙事层预算**：8k 窗口最坏预算新增「叙事层（日记节选）300」，合计 **3850 tokens**
   （500 persona + 200 摘要 + 300 日记 + 1200 检索 + 750 最近 5 轮 + 300 协议 + 600 系统其余）。
   prompt 组装前用同一 tokenizer 实测；超限按「日记节选 → 检索结果 → 最近 5 轮」顺序截断，
   仍超记 `prompt.budget_over` 并按最小可用集（persona + 最近 1 轮 + 输出协议）组装（§2 / §9）。
3. **E1 mention_count**：新建条目 `mention_count=0`；仅当 write_back 去重命中 **active** 条目时
   `Inc("mention_count",1)`（§3.1 / §6.3 / 附录 C D1）。
4. **E4 去重范围**：同 `user_id` + 同 `scope` + 同 `type` 的 active 条目；dormant 不参与比对、
   不因去重复活、不计 mention_count。命中 active → 只 `Set("last_access", now)` +
   `Inc("mention_count",1)`，不 compute_S、不强化 S、不写 JSONL 新行、不进 pending；
   更新失败记 `mem_write.mention_fail`（§6.3 / 附录 C D1）。
5. **E5 复活优先**：dormant 且检索 `sim>0.7` → 只 revive
   （`S = compute_S("revive")`、`status=active`、`last_access=now`），
   **不** `Inc("access_count")`、不叠 reinforce；active 且 `sim>0.5` 才强化。
   实现用 `revived_this_turn: set` 同轮去重（§6.1 / §6.3 / §9）。
6. **maintain 加固与补偿**：逐阶段 try/except，失败记 `state.write_guard` / `mem_write.fail` 不抛；
   追加 `diary.archive_expired` + `diary.rewrite`（report 增 `diary_archived`）；服务启动后补偿执行一次（§3.5）。
   维护任务**不生成日记**：生成走独立 worker 的两阶段，由宿主在 `diary.daily_at` / 启动补偿触发。
7. **HTTP / CLI 增量**：`GET /diary/list`、`GET /diary/excerpts`、`POST /diary/prepare`、
   `POST /diary/finish`；CLI `diary list / prompt / add --raw-file`（两段式，供宿主接 LLM）。
8. **指标新增**：`diary.ok/fail/skip/dup/privacy/truncated/cap_hit/inject/tokens/archive`、
   `prompt.budget_over`、`mem_write.mention_fail`；其余沿用 v1.5-r2（§14.1）。
9. **明确不改**：裸「群」强词过触发保留，原计划 v1.6 收敛改为 **推迟到 v1.7**（§0.1 注记 / §11）；
   mixed 日记 v1.6 只验证「不自动生成 / 不落盘 / 不自动注入」，人工确认入口列入 v1.7（§8.5 / D10）。

## 2. 模块与接口（按文件归属）

### 2.1 `src/qihun_memory/config.py`

`PromptConfig` 追加（关键字默认值）：`total_tokens=3850`、`diary_tokens=300`、
`protocol_tokens=300`、`system_misc_tokens=600`；已有 `summary_tokens=200` / `recent_turns=5` /
`persona_tokens=500` / `recent_tokens=750` 保持不变。

新增 `DiaryConfig`（frozen dataclass，字段名/默认值固定）：

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

`Config`：`diary: DiaryConfig`、`_SECTIONS["diary"]`；
路径属性 `diary_file / diary_archive_file / diary_bad_file`（data_dir 下
`diary.jsonl / diary.archive.jsonl / diary.bad.jsonl`）。

### 2.2 `src/qihun_memory/diary.py`（v1.6 新增）

模块常量：`DIARY_SCOPES = ("group", "dm")`；`DIARY_PREFIX`（规范 §8.4 注入前缀原文）。

数据类型（冻结）：

```python
@dataclass(frozen=True)
class DiaryEntry:            # id/date/scope_tag/content/tokens/turn_range(tuple[str,...])/created_at
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data) -> "DiaryEntry": ...

@dataclass
class DiaryLoadReport:       # total_lines/valid/bad/new_bad/bad_samples
@dataclass(frozen=True)
class DiaryPrompt:           # scope_tag/prompt/due/date/turn_range/reason
@dataclass
class DiaryRunReport:        # scope_tag/status/reason/entry_id/tokens
# status ∈ ok / fail / skip / dup / cap_hit / privacy / disabled / not_due
```

`DiaryStore`（唯一 `diary.jsonl` 读写入口；只读模式不得写盘）：

```python
DiaryStore(cfg, metrics=None, read_only=False)
load() -> DiaryLoadReport                  # 坏行搬 diary.bad.jsonl（幂等、去重）
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

`DiaryWorker`（独立 worker；不 import pipeline / memory / retrieval，依赖由 callable 注入）：

```python
DiaryWorker(cfg, store, *, metrics=None, memory_items=None, state_loader=None,
            users=None, affect_snapshot=None, now_fn=None)
record_turn(scope, turn_id, at=None) -> None    # 内存 turn 日志（按 scope），上限 10000 条
due(scope, now=None) -> bool
prepare(scope, *, now=None, force=False) -> DiaryPrompt
finish(prepared, raw, *, now=None) -> DiaryRunReport     # 隐私 → 截断 → cap → 幂等 → 落盘；异常 → fail 不抛
generate(scope, llm, *, now=None, force=False) -> DiaryRunReport   # llm 失败重试 ≤1
run_due(llm, *, now=None) -> list[DiaryRunReport]        # group / dm 各一篇，互不混合
```

模块函数：

```python
select_excerpts(store, scope_tag, *, now, budget=None, count=None, metrics=None) -> dict
# {"excerpts": [DiaryEntry...], "tokens": int, "injected": int, "truncated": bool}
# active() 倒序取前 count（默认 cfg.diary.inject_count）；第 1 篇必装，第 2 篇仅在剩余预算内整篇装；
# 记 diary.inject / diary.tokens；第 2 篇放不下记 diary.truncated；scope_tag 只允许 group/dm（mixed 返回空）。
render_diary_block(excerpts) -> str
# 每篇：[日记 YYYY-MM-DD | 群聊] 换行 <content>（dm → 私聊），篇间空行；无节选返回空串。
```

约束（可逐条测试）：daily 按 scope 各一篇（不是全局一篇）；every_n 按 scope 分别计数；
trigger ∈ daily/every_n/both（both 时 daily 优先，当日同 scope 已有日记 → dup）；
幂等键 `(date, scope_tag, turn_range)`；隐私护栏（他人昵称 → 有人、bot 昵称 → 我、user_id 字面量 /
哨兵 / 围栏 → 拒写）；超 300 tokens 截断；同 scope 窗口内 ≥30 篇 cap；scope_tag 落盘只允许 group/dm，
mixed 绝不自动生成/落盘；时间戳带时区，本地日按 `diary.timezone` 取（Windows 无 tzdata 时回退固定 +08:00）。

### 2.3 `models.py` / `memory.py` / `gate.py`

- `models.MemoryItem.mention_count` 默认 0；`memory.remember` 新建条目 `mention_count=0`。
- `memory.reinforce()`：E5 复活优先；`revived_this_turn` 同轮去重；active 且 sim>0.5 才强化；
  revive 不 `Inc("access_count")`、不叠 reinforce（全部经 `update_memory`）。
- 去重（E4）：只比对同 user + scope + type 的 active 条目；命中只 `Set(last_access)` +
  `Inc(mention_count,1)`，不写新行、不 compute_S、不进 pending；失败记 `mem_write.mention_fail`。
- `maintain()`：逐阶段 try/except（失败记 `state.write_guard` / `mem_write.fail`）；
  原有 R 重算 / R<0.02 移档 / dormant / 全量重写 / pending 重试 / debug 清理保持；
  追加 `diary.archive_expired` + `diary.rewrite`，失败记 `diary.fail`。
  **维护任务不生成日记**（生成需要宿主 LLM，走独立 worker 的 daily_at / 启动补偿两阶段；
  cap 检查在 `DiaryWorker.finish` 落盘前执行）。
- `gate.is_light_message(text, *, last_user_message=None, at_bot=False, cfg=None)`：
  纯表情/标点/emoji、<4 字且无问号且非 @bot、寒暄词表整句相等、复读、纯 @bot 无正文 → True；
  轻路径只决定是否检索，不影响闸门（§7.1 / 硬规则 5）。
- `MemorySystem`：新增 `self.diary = DiaryStore(...)` + `load()`、`self.diary_worker = DiaryWorker(...)`
  （memory_items / state_loader / users / affect_snapshot 绑定本实例，llm=None）。

### 2.4 `src/qihun_memory/prompt.py`

```python
PromptBundle 追加字段（有默认值）：diary_block: str = ""、total_tokens: int = 0、truncated: bool = False
measure_prompt_tokens(system_prompt, messages) -> int     # 用 tokens.estimate_tokens 求和
build_prompt(*, persona, summary, retrieval=None, users, recent_turns, user_message, gate_passed,
             limits=None, injected_items=None, diary_excerpts=(), metrics=None) -> PromptBundle
```

- 日记渲染用 `diary.render_diary_block`；位置 = 用户上下文区「人格上下文」区，与【会话摘要】并列、
  在【相关记忆】之前；顺序：system(persona) + user(【会话摘要】+【近期日记】(DIARY_PREFIX+块)+【相关记忆】+【当前消息】)。
- 摘要先截 `limits.summary_tokens`（200）；persona 截 `persona_tokens`（500）。
- 总预算：`measure_prompt_tokens(...) <= limits.total_tokens`（3850）；超限按
  「先丢日记（2→1→0）→ 再从尾部逐条丢检索 → 再从最旧逐轮丢最近轮（至少留 1 轮）→ 最小可用集 +
  `prompt.budget_over`」截断并每次重测（§2 / §9）。
- 既有 `tests/test_prompt.py` 保持全绿（只允许新增断言，不改旧语义）。

### 2.5 `pipeline.py`（Lead 交付）

- `prepare`：`cfg.diary.enabled` 时 `select_excerpts(scope)`（失败 → 空 + `diary.fail`），
  `build_prompt(..., diary_excerpts=..., metrics=metrics)`；ctx 新增冻结字段 `diary_excerpts / users`；
  metrics 记 `diary.inject` / `diary.tokens`。
- `write_back`：pending 重放移到 8b 开始时单一位置（remove prepare 内 retry 与 remember 内部 retry）；
  9a 调 `reinforce`（E5 语义由 memory 层实现）。顺序为 8a → 8b（先重放）→ 9a → 9b → 10，
  emit 早于 write_back（附录 C D5）。日记记账 `diary_worker.record_turn` 在 finish 的
  write_back 之前（摘要之后），失败只记指标。

### 2.6 `service.py` / `cli.py` / 公共门面

- 服务：`GET /diary/list`（列表，可按 scope 过滤）、`GET /diary/excerpts`（当前 scope 节选）、
  `POST /diary/prepare`（两段式第一步，返回 prompt / due）、`POST /diary/finish`（第二步，raw → 解析护栏落盘）；
  错误码风格与现有一致，所有异常不外泄栈；token 鉴权沿用。
- CLI：`diary list`、`diary prompt`、`diary add --raw-file`（两段式，供宿主接 LLM）；`--json` 输出。
- `__init__.py` 文档字符串升级 v1.6；`tools.py` 可 re-export diary 公共函数（可选）。
- `/health`、`cli --version` 版本随包版本 **0.3.0**（对应规范 v1.6-rc4）。

### 2.7 文档 / 发布物

- 根目录 `QiHun-Memory-System-v1.6-rc4.md`（`review/` 原文逐字节副本，不重写正文）。
- `README.md`、`docs/ARCHITECTURE.md`、`docs/INTERFACES.md`、`docs/DEPLOYMENT.md`、
  `docs/INTEGRATION.md` 升级 v1.6-rc4；新增本文件与 `docs/regression-rc1.md`。
- `examples/config.example.json` 增 `diary` 段（键名与 `DiaryConfig` 一致）+ prompt 预算增量；
  `examples/diary.example.jsonl` 提供 group / dm 各一篇合法示例。
- `pyproject.toml` version=0.3.0、description 改 v1.6；`src/qihun_memory/version.py` 0.3.0。
- 历史引用（v1.5-r2 规范/契约）保留；「当前版本 / 下一步」指向 v1.6-rc4；
  v1.5-r2 的「v1.6 收敛裸群」表述按 rc4 §0.1 改为「推迟到 v1.7」。

### 2.8 测试

- 新增 `tests/test_v16_diary.py`（A）、`tests/test_v16_semantics.py`（B：E1/E4/E5/C11 单点）、
  `tests/test_v16_budget.py`（C：D05 单测级）、`tests/test_v16_narrative.py`（Lead：D01~D12）、
  `tests/test_v16_integration.py`（Lead：P17/A27/X02/X04/E3 端到端）、
  `tests/test_v16_guards.py`（Lead：D7 叙事层边界架构守卫）、
  `tests/test_v16_service_cli.py`（Lead：`/diary/*` 两阶段与 CLI diary 用例）。
- 既有测试按新语义适配（只改期望值/构造参数并加 E1/E4/E5 注释，不删覆盖点）。
- `tests/test_v15_guards.py` 的 AST 架构扫描对 v1.6 新模块同样生效：不直接写 `MemoryItem` 状态字段、
  不出现 `user_affect.json` 字面量 / 直接 IO、不实例化 `UserAffectStore`、不调用 `compute_S` / `dedup_key`、
  不 import parser 下划线内部名。

## 3. 指标事件名（规范 §14.1）

沿用 v1.5-r2：`mem_extract.*`（含 `mem_extract.affect_invalid` / `mem_extract.ungated`）、
`mem_write.fail`、`retrieval.degraded / truncated / affect_bias / pool.mixed`、
`state.stale / state.write_guard`、`user_affect.stale / fail`、`group_bridge.*`、`dedup.degraded`、
`ctx.violation`、`memory.* / turn.* / store.*`。

v1.6 新增：

| 事件 | 含义 |
|---|---|
| `mem_write.mention_fail` | mention_count 更新失败（E4） |
| `diary.ok` / `diary.fail` | 日记生成成功 / 失败 |
| `diary.skip` | every_n 当段无新增记忆等跳过 |
| `diary.dup` | 幂等命中 |
| `diary.privacy` | 隐私护栏命中（替换或拒写） |
| `diary.truncated` | 超 300 tokens 截断 / 第 2 篇未装 |
| `diary.cap_hit` | 同 scope 窗口 30 篇上限 |
| `diary.inject` / `diary.tokens` | 本轮注入篇数 / tokens |
| `diary.archive` | `archive_expired` 移档 |
| `prompt.budget_over` | 组装前总开销超 3850，已按最小可用集降级 |

字段只放计数 / 布尔 / scope / 日期；不落正文、昵称。

## 4. 验收

1. `python -m pytest -q` 全绿（含 L1~L4 + A/B + D 叙事层；最小 CI = L1+L2+L3+L4）；
   v1.6-rc4 本机最终：**781 passed, 0 failed**（Python 3.14）。
2. `python -m qihun_memory.cli --version` → `qihun-memory 0.3.0`（对应规范 v1.6-rc4）；
   `GET /health` 的 `version` 同为 0.3.0。
3. `python -m pytest tests/test_examples.py -q` 全绿（示例配置 / 示例日记可直接被模型加载）。
4. 规范 §12.8：D01~D12 全部覆盖；P01~P16 / C01~C10 / A01~A26 可追溯到
   [regression-rc1.md](regression-rc1.md) 的真实测试 node。
5. 文档与代码一致：HTTP / CLI 增量、Config 增量、指标名、文件归属与本文件一致。

## 5. 回滚方式（v1.6-rc4 → v1.5-r2）

- **代码 / 配置**：v1.6 的接口全部是增量（新参数带默认值、新模块、新路由、新子命令）；
  回滚到 v1.5-r2 只需回退代码与版本号 0.2.0。老 `config.json` 可直接用：
  多余的 `diary` 段、`prompt.total_tokens` 等键会被 v1.5-r2 的 `Config` 忽略（日志告警，不影响启动）。
- **数据**：`diary.jsonl / diary.archive.jsonl / diary.bad.jsonl` 是可加文件，回滚后不被读取，**不要删除**
  （保留以便再次升级）；`memory.jsonl` 无需迁移（v1.5-r2 读取 v1.6 条目只会忽略 `mention_count` 计数差异，
  E1/E4/E5 是计数与状态语义差异，不产生不可读字段）。
- **行为**：回滚后回到 v1.5-r2 语义（新条目 mention_count=1、去重命中 dormant 可复活、
  复活当轮 access_count+1、prompt 预算 ≈3550、无日记注入）；这是已知的语义回退，不是数据损坏。
- **提示**：回滚前建议停服务并备份整个 `data_dir`；升级前同样建议备份（幂等、无迁移脚本）。

## 6. 文件归属速查

| 层 | 文件 |
|---|---|
| 配置 | `src/qihun_memory/config.py` |
| 叙事层 | `src/qihun_memory/diary.py`（新增） |
| 事实层语义 | `models.py`、`memory.py`、`gate.py` |
| prompt 预算 | `src/qihun_memory/prompt.py` |
| 编排 / 接线 | `pipeline.py`、`service.py`、`cli.py`、`__init__.py`、`tools.py` |
| 测试 | `tests/test_v16_diary.py`、`test_v16_semantics.py`、`test_v16_budget.py`、`test_v16_narrative.py`、`test_v16_integration.py` + 既有测试适配 |
| 发布物 | 根目录规范副本、`README.md`、`docs/*`、`examples/*`、`pyproject.toml`、`src/qihun_memory/version.py` |
