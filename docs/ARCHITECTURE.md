# 实现架构与设计对照（v1.6-rc4）

本文件说明「设计规范的每一节落在哪段代码」，以及实现时做的决策与已知偏差。
设计规范（当前权威）：[../QiHun-Memory-System-v1.6-rc4.md](../QiHun-Memory-System-v1.6-rc4.md)；
工程契约：[UPGRADE-CONTRACT-v1.6-rc4.md](UPGRADE-CONTRACT-v1.6-rc4.md)；
冻结回归索引：[regression-rc1.md](regression-rc1.md)。

## 1. 目录结构

```text
qihun-mem/
├── QiHun-Memory-System-v1.6-rc4.md  # 当前设计规范（冻结候选，review/ 原文逐字节副本）
├── QiHun-Memory-System-v1.5-r2.md   # 历史规范（v1.6 的基线）
├── QiHun-Memory-System-v1.4.md      # 历史规范（已被取代，仅作追溯）
├── README.md
├── pyproject.toml                  # Python 包配置（version=0.3.0；pytest pythonpath=src）
├── docs/                           # ARCHITECTURE / INTERFACES / DEPLOYMENT / INTEGRATION / 工程契约 / 回归索引
├── examples/                       # 示例配置、人设、state、memory、user_affect、diary、raw 输出
├── src/qihun_memory/
│   ├── config.py                   # §10 参数总表 → 不可变配置（含 affect / bridge / diary 节）
│   ├── models.py                   # §3.1 memory schema / affect 规整 / Set,Inc 操作符
│   ├── tokens.py                   # 固定 token 预算的估算
│   ├── parser.py                   # §5.2 协议 + §5.3 解析流水线 + §5.6 脱敏 + 发送层清洗
│   ├── gate.py                     # §5.1 写入闸门 + §7.1 轻路径
│   ├── lifecycle.py                # §5.7 / §6 / 公式（compute_S 唯一入口）
│   ├── state.py                    # §3.2 state.json 只读 + users.json + §3.3 UserAffectStore
│   ├── store.py                    # §3 JSONL 容错、affect.bad、原子重写、向量 sidecar、幂等
│   ├── embedding.py                # §7.5 embedding 后端与降级
│   ├── bridge.py                   # §7.6 桥接触发判定（无 I/O、无状态）
│   ├── retrieval.py                # §7.1~7.4 候选池 / 打分 / fill 装填 / 跨池去重
│   ├── prompt.py                   # §7.4 注入格式 + §5.2 协议 + §2/§9 总预算与截断
│   ├── diary.py                    # §8 叙事层：DiaryStore / DiaryWorker / 节选 / 渲染（v1.6 新增）
│   ├── summary.py                  # §9 滚动摘要 ≤200 tokens
│   ├── memory.py                   # §6/§9 写入·强化·维护门面（update_memory / E1/E4/E5）
│   ├── pipeline.py                 # §9 每轮流程：prepare / finish / ctx 冻结 / write_back
│   ├── obsidian.py                 # Obsidian 只读库（无写 API）
│   ├── legacy_import.py            # 旧 Obsidian 记忆库一次性导入（只读/幂等/干跑）
│   ├── service.py                  # 本地 HTTP JSON API（宿主接入层；含 /diary/*）
│   ├── cli.py                      # init/status/search/list/affect/turn/maintain/diary/...
│   ├── metrics.py                  # §14 结构化指标
│   ├── fsutil.py                   # 原子写（tmp → fsync → replace）
│   ├── version.py                  # 包版本 0.3.0
│   └── tools.py                    # 公共 API 门面
├── tests/                          # L1~L4 + A/B/D 用例（见 §6）
└── .github/workflows/ci.yml        # Python 3.11~3.14 矩阵
```

## 2. 模块与规范对照

| 规范 | 实现 | 关键点 |
|---|---|---|
| §2 范围与预算 | `config.PromptConfig` + `tokens` + `prompt.build_prompt` | 8k 最坏预算 3850；同一 tokenizer 实测；截断顺序与 `prompt.budget_over` |
| §3 存储结构 | `store.py` + `diary.DiaryStore` + `config.py` 路径 | 数据在 `data_dir`；JSONL 单写者；diary 三文件独立 |
| §3.1 schema | `models.MemoryItem` | 字段一一对应；`mention_count` 新建 0（E1）；`affect` 可选；`vector` 仅运行时 |
| §3.2 state | `state.load_bot_state` | 缺失/过期 → 中性 + `state.stale`；无任何写函数 |
| §3.3 user_affect | `state.UserAffectStore` | 唯一读写入口；`get/step_update/decay/save` |
| §3.4 diary schema | `diary.DiaryEntry` | id/date/scope_tag/content/tokens/turn_range/created_at；无 user_id/status 等字段 |
| §3.5 维护 | `memory.maintain` | 逐阶段 try/except；R 重算/移档/dormant/全量重写/pending 重试；`diary.archive_expired` + `rewrite` |
| §4 归属 | `retrieval.Retriever.search` 候选池 + `memory._scope_items` | 群聊混合池；dm 只同 user；桥接同 user 单向（群聊池 → 私聊上下文） |
| §5.1 闸门 | `gate.should_extract` | 四条规则 + @/回复；与检索解耦；`enforce_write` |
| §5.3 解析 | `parser.py` | `split_raw_candidate` / `parse_memories` / `parse_llm_output` / `clean_answer_for_send` |
| §5.4/5.5 失败与 pending | `memory.remember/retry_pending` + `pipeline.write_back` | pending 只存新记忆；重试 1 次后丢弃；不重放强化/感情 |
| §5.6 注入防护 | `parser.sanitize_for_prompt` + `prompt.py` + `diary.py` | 用户消息/persona/摘要/昵称/日记全脱敏；记忆只进用户上下文 |
| §5.7 S_init | `lifecycle.compute_S("init", ...)` | valence + affect 双调制，cap 90 |
| §6.1 遗忘/复活 | `lifecycle.retention` / `memory._refresh_statuses` / `compute_S("revive")` | 惰性判定；E5 复活优先；三个 sim 阈值分工 |
| §6.2 压抑 | `lifecycle.effective_stability` | 仅 valence<-0.3 且 avoidance≥0.5；S_eff≥0.8S |
| §6.3 强化/去重/感情 | `compute_S("reinforce")` / `memory.reinforce` / `memory.remember` | E4 active-only 去重；mention_count 只 +1；经 update_memory |
| §6.4 user_affect 衰减 | `state.UserAffectStore.decay` | 0.9^floor(days/7)；30 天 stale 归零 |
| §7.1 路由/轻路径 | `gate.is_light_message` + `pipeline.prepare` | 轻路径不检索；与闸门解耦（E3） |
| §7.2 打分 | `retrieval.Retriever` | 混合池；rel/familiarity/γ_u/affect_match；β=0.2；确定性排序 |
| §7.3/7.4 装填/注入 | `retrieval.fill` + `prompt.render_memory_block` | 桥接预算来自 constraints；行头 header_tokens=24 |
| §7.5 embedding | `embedding.py` + sidecar | 可选 bge-small-zh；float16+base64；缺失降级 BM25 |
| §7.6 桥接 | `bridge.should_bridge` + `retrieval` | 单向、同 user、显式触发、≤0.7、≤300、标注、跨池去重 |
| §8 叙事层 | `diary.py` + `prompt._render_diary_block` + `pipeline.prepare` | 独立 worker；scope_tag 过滤；≤300 / 2 篇；隐私护栏；30 天窗口 / 30 篇 cap |
| §9 每轮流程 | `pipeline.TurnPipeline` | prepare/finish；ctx 冻结；顺序锁定；日记记录 turn 但不阻塞 |
| §10 参数 | `config.py` | 默认值与 §10 一致（3850 / diary 300 / header 24 / k_a 0.3 / bridge 0.7/300） |
| §11 推迟清单 | 全模块 | 裸「群」收敛推迟 v1.7；mixed 人工确认 v1.7 |
| §12 用例 | `tests/` | 见 `regression-rc1.md` |
| §13 硬规则 | 全模块 | 见 §4 |
| §14 观测 | `metrics.py` + 各模块事件 | 事件名与告警阈值按 §14.1；日记只记计数 |

## 3. 每轮数据流

```text
宿主（qq-bridge / DSH）
   │  POST /turn/prepare {user_id, scope, message, msgid?, at_bot?, reply_to_bot?}
   ▼
TurnPipeline.prepare（build_context）
   ├─ load_bot_state()            只读 state.json（缺失/过期→中性）
   ├─ should_extract()            闸门：决定是否附提取协议
   ├─ is_light_message()          轻路径：决定是否检索（与闸门无关）
   ├─ affect_store.decay(user)    惰性衰减（每轮检索前）
   ├─ MemorySystem.search()       候选池 → should_bridge → 打分 → fill() → injected_items
   ├─ diary.select_excerpts()     叙事层节选（scope 过滤，≤300 / ≤2 篇；失败 → 空 + diary.fail）
   ├─ Summary.get()               滚动摘要
   └─ build_prompt()              system(persona + 数据声明 + 协议)
                                  + messages(摘要 / 日记 / 记忆 / 最近5轮 / 当前)；总预算 3850
   │  冻结 ctx（frozen + 白名单，含 diary_excerpts），返回 {turn_id, prompt, injection, retrieval, state, gate}
   ▼
宿主调用 LLM（每轮一次；输出 = 正文 + <MEMORY_JSON>...，affect 可选）
   │  POST /turn/finish {turn_id, raw}
   ▼
TurnPipeline.finish
   ├─ split_raw_candidate / parse_memories   永不抛异常；raw_answer 未清洗
   ├─ clean_answer_for_send                  唯一发送清洗点
   ├─ sender(answer)                         先发送（空正文不发空气泡）
   ├─ summary.update()                       滚动摘要 ≤200 tokens
   └─ write_back（顺序锁定）：
       8a affect_store.step_update(familiarity += alpha)
       8b pending 重放（≤1 次）→ 新记忆写入（E4 去重：命中 active 只 mention+last_access）
       9a reinforce/revive：E5 复活优先（revived_this_turn 同轮去重）
       9b 聚合 injected_items 中当前用户的非 neutral affect → 单次 step_update
       10 ctx.last_event_affect = first_non_neutral(memories)
   （write_back 之前还有 finish 的 9b 日记记账：非重复 turn 调 diary_worker.record_turn，失败只记指标）

日记 worker（独立、异步、不计入每轮 LLM 调用）：
  触发（daily 23:50 按 scope 各一篇 / every_n 按 scope 计数 / both）
    → prepare：当段记忆 + state + user_affect + users 快照 → 生成 prompt
    → 宿主 LLM（失败重试 ≤1）
    → finish：隐私护栏 → 截断 300 → cap 30 → 幂等 → 追加 diary.jsonl（任何异常 → fail 不抛）
  每日维护：只做日记移档（created_at ≤ now-30d → diary.archive.jsonl）+ 全量重写，不触发生成；
  当日生成由宿主在 daily_at / 启动补偿经两阶段触发，cap 在 DiaryWorker.finish 落盘前检查
```

## 4. 硬规则（§13）与实现方式

| # | 规则 | 实现 |
|---|---|---|
| 1 | 记忆必须有 `user_id` + `scope` | `memory.remember` 入口校验，缺失不写 |
| 2 | 先过滤候选池再打分；私聊桥接受 §7.6 全部约束；群聊禁 dm | `retriever.search` 第一步过滤/建池；`bridge` + 子预算 |
| 3 | 注入必须标注说话人 | `render_memory_block` 用 `users.json` 当前昵称 |
| 4 | 每轮最多一次 LLM 调用 | 本系统不发起 LLM 调用；日记走独立 worker |
| 5 | 写入必须过闸门 | `gate.enforce_write=true`：未命中不写（记 `mem_extract.ungated`） |
| 6 | valence 永不单独驱动行为 | 只用于 S_init / 压抑 / 排序 |
| 7 | S ≤ 90 | `compute_S` 与加载路径双重封顶 |
| 8 | 压抑只调制 S_eff | `effective_stability` 不动 `item.S` |
| 9 | 禁止回写 state.json | 代码中无 state 写路径；桥接侧负责写 |
| 10 | 机制可推迟，字段不留空 | schema 仅含 §3.1 字段（+可选 affect/turn_key）；diary 不含 status 等字段 |
| 11 | 正文 = 哨兵前内容；无哨兵 = 去掉疑似 JSON 块 | `clean_answer_for_send` 是唯一发送源（P17） |
| 12 | 禁止正则抠嵌套 JSON | 只用于找哨兵 / 剥围栏 / 去尾逗号 |
| 13 | 提取失败不影响回答 | 解析兜底 + finish 分段 try/except + 先发后写 |
| 14 | 坏条单条丢弃 | `normalize_item` 逐条；日志不含正文 |
| 15 | affect 描述事件，不描述 bot 心情 | `item.affect` 仅来自 LLM；`item.valence` 仅来自 state |
| 16 | user_affect 只调制排序 | 只进 type_bonus（familiarity）与 γ_u 因子；不参与 compute_S |
| 17 | affect/user_affect 失败只降增益 | 非法 affect 只丢字段；user_affect 写失败只记指标 |
| 18 | 桥接单向/同用户/显式/≤0.7/≤300/标注/去重 | `bridge.py` + `retrieval.fill` + `prompt`；群聊不读 dm；反向断层话术只定态度（见 §7 #41） |
| 19 | 叙事层只提供人格语境 | `diary.py` 不 import pipeline/memory/retrieval；不参与检索/S/R/scope 过滤 |
| 20 | 日记离线/异步；只按 scope_tag 注入；mixed 不自动生成/落盘/注入 | `DiaryWorker` 独立 worker；`select_excerpts` 只读 store；mixed 不写盘 |

## 5. 状态单点与接口边界（附录 C）

- **D1 五个单点（v1.6 增 DiaryStore）**：
  - 记忆状态：`MemorySystem.update_memory(item_id, ops, reason="")`，白名单 `Set(S/status/last_access)`、
    `Inc(access_count/mention_count)`；非法 → `state.write_guard` + 整体不生效。
  - 用户感情：`UserAffectStore`（`get/step_update/decay/save`）。
  - S 公式：`lifecycle.compute_S(mode, type, ...)`（init/revive/reinforce）。
  - 去重键：`parser.dedup_key(item)`。
  - 日记落盘：`diary.DiaryStore`（唯一 `diary.jsonl` 读写入口；只读模式不写盘）。
- **D2 解析/发送分离**：解析层只返回 `raw_answer`；发送层 `clean_answer_for_send` 唯一清洗；
  `MemoryOutputParser` 保留兼容。
- **D3 装填接口**：`retrieval.fill(items, constraints)`，桥接预算来自 `constraints.subgroup.bridge`，
  无 `bridge_items` 双源。
- **D4 回归四层**：L1 解析 / L2 状态 / L3 turn 集成 / L4 组合与时序；最小 CI = 四层全绿。
- **D5 顺序锁定**：emit 早于 write_back；8a 早于 8b；9a 早于 9b；9b 在装填之后；
  write_back 内部为 8a → 8b（开始时执行 pending 重放，≤1 次）→ 9a → 9b → 10；
  日记记账（`record_turn`）在 write_back 之前、摘要之后；由 L3/L4 断言守护。
- **D6 不变量 I1~I3**：轮级 user_affect 只经 `UserAffectStore.step_update`（write_back）；
  item.affect 不直接聚合；9b 只用本轮 injected_items 且 `user_id == current_user_id`。
- **D7 叙事层边界**：D7.1 不参与检索打分/装填/S/R/去重键/user_affect 聚合；
  D7.2 注入只读 `ctx.diary_excerpts`，write_back 不改日记；
  D7.3 独立 worker 不调用 `turn()`、不共享 mem dict 写权限；
  D7.4 隐私护栏在生成后落盘前；D7.5 mixed 不进入正常生命周期（v1.6 无人工确认入口）。

## 6. 测试覆盖矩阵

| 规范用例 / 层 | 测试文件 |
|---|---|
| L1 解析 P01~P17 + affect 校验 + 公式 C01~C11 | `tests/test_parser.py`、`tests/test_v15_parser.py`、`tests/test_formulas.py`、`tests/test_v15_state.py`、`tests/test_v16_integration.py`（P17） |
| L2 状态单点 / 绕单点 G01~G07 | `tests/test_memory.py`、`tests/test_v15_state.py`、`tests/test_store.py`、`tests/test_v15_guards.py` |
| L3 turn 集成 | `tests/test_pipeline.py`、`tests/test_retrieval.py`、`tests/test_prompt.py`、`tests/test_v15_pipeline.py` |
| L4 组合 43~46 / X01~X04 / B01~B07 | `tests/test_v15_scenarios.py`、`tests/test_v15_capacity.py`、`tests/test_hardening.py`、`tests/test_v16_integration.py` |
| 叙事层 D01~D12 | `tests/test_v16_diary.py`、`tests/test_v16_narrative.py`、`tests/test_v16_budget.py` |
| v1.6 架构守卫（D7 边界 / 新模块不越界） | `tests/test_v16_guards.py`、`tests/test_v15_guards.py` |
| 语义修正 E1/E4/E5 + 轻路径 | `tests/test_v16_semantics.py`、`tests/test_gate.py`、`tests/test_memory.py`、`tests/test_pipeline.py` |
| A01~A27 affect/user_affect/桥接 | `tests/test_v15_state.py`、`tests/test_retrieval.py`、`tests/test_v15_pipeline.py`、`tests/test_memory.py`、`tests/test_v16_integration.py`（A27） |
| Obsidian 只读 + 只读视图导出 | `tests/test_obsidian.py`、`tests/test_obsidian_export.py` |
| HTTP 适配层（含 injection / user_affect / diary） | `tests/test_service.py`、`tests/test_v16_service_cli.py` |
| 旧库导入（映射/幂等/干跑/坏数据） | `tests/test_legacy_import.py` |
| CLI（affect / diary 子命令） | `tests/test_cli.py` |
| Python 3.11 语法兼容守卫 | `tests/test_syntax_compat.py` |

冻结用例编号到真实测试 node 的逐行索引见 [regression-rc1.md](regression-rc1.md)。

## 7. 实现决策与已知偏差

1. **Python 实现（B1，已拍板）**：宿主通过 HTTP 两阶段接口调用；Node 侧不引入第二套记忆逻辑。
2. **token 预算用确定性估算**（`tokens.estimate_tokens`）：CJK 1 token/字、其余 4 字符/token；
   规范要求固定 token 数，未指定 tokenizer；该估算偏保守、无外部依赖。v1.6 总预算按同一函数实测。
3. **BM25 中文分词**：CJK 字符 bigram（单字 run 用单字）+ 拉丁/数字小写词；对几百条短文本足够稳定。
4. **embedding 可选**：默认 bge-small-zh-v1.5（`pip install -e ".[embed]"`）；不可用时 cos 权重置 0、
   BM25 权重置 1，记 `retrieval.degraded`；去重退化原文匹配，记 `dedup.degraded`。
5. **存储位置 = data_dir**：Obsidian 库只读，不依赖 vault 运行；`persona.md` 可来自只读副本。
6. **两阶段回合**：`prepare/finish` 保留「先发后写」；HTTP 模式下 finish 返回前已完成落盘（固有边界，
   见 DEPLOYMENT.md §4.8）。日记同样两阶段（`/diary/prepare` → 宿主 LLM → `/diary/finish`）。
7. **summary.json / user_affect.json / diary.jsonl 的落点**：规范 §3 已给出；实现均原子写。
8. **E5 复活优先（v1.6）**：dormant 被检索强匹配（sim>0.7）复活时 S = 裸 S₀、status=active、
   last_access=now，**不** `Inc(access_count)`、不叠 reinforce；`revived_this_turn` 同轮去重。
   v1.5-r2 的「复活当轮 access_count+1」已按 §0.1 废止。
9. **affect 非法只丢字段**：原始行搬 `affect.bad.jsonl`，条目以 `affect=None` 加载；不丢整条。
10. **turn_key 持久化边界**：新建条目随条目落盘；整轮全部命中合并时 turn_key 仅在内存生效，
    每日重写后随条目持久化；单实例、崩溃最多丢当天增量。
11. **每次写入都会尝试 embedding**：发生在发送之后，不增加用户可感延迟；失败条目由 finish 末尾与
    每日维护补算，不在 prepare/检索路径补算。
12. **指标事件**按 §14.1 命名；默认不记正文；`metrics.debug_raw=true` 时记 raw 前 500 字符并在维护时清理。
13. **clean_answer_for_send 切在最早哨兵前**：比「解析成功那个哨兵」更严格，保证多哨兵/伪造哨兵时
    正文也不含任何隐藏 JSON 块；无哨兵时剔除候选 JSON 块（P17 / 硬规则 11）。
14. **检索空候选不降级**；非法 scope 返回空结果 + warning；单条时间戳损坏时该条 R=0、不中断检索。
15. **gate.enforce_write**：未命中闸门时不写记忆（记 `mem_extract.ungated`）；置 false 恢复宽松口径。
16. **压抑不产生持久后果**：dormant/archive 判定用未压抑的 R；避免几轮回避把记忆永久降级。
17. **降级模式相似度语义**：无 embedding 时 `sim` = BM25 批内归一值；`max==min`：全 0 → 0，
    同为正分（含单候选）→ 1.0。
18. **HTTP 时延语义**：`finish` 在服务端完成解析、摘要、写记忆后返回；进程内 `finish(sender=...)`
    严格先发后写。日记 worker 不进入 `turn()`，不增加聊天可感延迟。
19. **加载防脏数据**：时间戳不可解析 / S 非正 → 坏行；S>90 clamp；valence 非有限 → 0；state NaN/inf → 中性。
20. **finish 失败隔离**：解析后的摘要/写记忆/强化/向量补算/调试原文各自 try/except；失败只记
    `turn.finish_error`，回答照常返回；`/turn/finish` 成功后才清理 prepared，失败可用同一 turn_id 重试。
21. **向量 sidecar 维度与 id 边界**：加载用 embedder 实际维度校验；归档后 next_id 扫描 archive，不重用 id；
    archive 写入幂等。日记 id 同样扫描 archive 续号（`d_0001` 起）。
22. **旧库导入走迁移通道**（`legacy_import.py`）：保留原始时间戳/S，不经过提取闸门，登记
    `turn_key="legacy:<vault>:<相对路径>"` 幂等；不改 vault、不写 state.json。
23. **现网运行配置偏差**：persona 2000 tokens、summary 500 tokens、桥接注入上限 `maxInjectChars=6000`
    属运行配置上调，不改变库默认值（§10）。
24. **state.json 的现网写入者**：库内无写路径；qq-bridge 每轮 prepare 前把情感快照写成
    `{valence, avoidance=max(0,-valence), updated_at}`（原子写、快照为空则不写）。
25. **桥接宿主两阶段边界**：`/turn/prepare` 的 `injection` 由宿主注入；静默/后台回合不注入不写；
    `<MEMORY_JSON>` 服务端与桥接侧双层剥离（含截断开哨兵、全角、零宽、裸 JSON）。
26. **Obsidian 只读视图导出**：`export-obsidian` 是独立显式导出工具（单向：记忆 → Markdown），
    数据没变不重写；Obsidian 内容不回流。
27. **状态单点收口**：`update_memory` / `UserAffectStore` / `compute_S` / `dedup_key` / `DiaryStore`
    之外不存在第二份实现；G01~G07 用架构/静态测试守护。
28. **ctx 冻结机制**：frozen dataclass + `mutable_fields` 白名单（不是类型系统强制）；越界写抛
    `ContextViolation` 并记 `ctx.violation`；残余风险 R1 已冻结。v1.6 ctx 新增冻结字段
    `diary_excerpts / users`。
29. **fill 接口无第二份预算**：装填层不硬编码 1200 / 300，桥接子预算跟随 constraints；G07 守护。
    `injected_items` 含 r/score/affect/bridge/source_pool/tokens/header_tokens，下游不得重新判断来源。
30. **检索顺序**：过滤/建池 → 打分 → 跨池去重（保留 score 高者、同分保留 dm）→ 确定性排序 → fill；
    `group_bridge.hit` 记录最终注入条数并附 `candidates`（去重后进 fill 前候选数）。
31. **normalize_text 的能力边界**：金样 + 2 条 property（幂等 / NFKC 稳定）进最小 CI；不承诺覆盖全部
    Unicode（R2）。
32. **affect_match 默认关闭**：开启后只由 `last_event_affect` 驱动；重启后自动失效，不读历史
    `memories[].affect`（R4 / I1；X02 守护）。
33. **桥接触发词的已知偏差**：`strong_words` 含裸「群」，私聊里泛指话题也会触发桥接；
    这是 v1.5-r2 冻结的已知过触发，原计划 v1.6 收敛，按 v1.6-rc4 §0.1 显式 **推迟到 v1.7**。
34. **叙事层隔离（v1.6）**：`diary.py` 只依赖注入的 callable（memory_items / state_loader / users /
    affect_snapshot / llm），不 import pipeline / memory / retrieval；生成走宿主 LLM，worker 不阻塞 turn。
35. **日记时间处理**：created_at 带时区；本地日按 `diary.timezone` 取（zoneinfo 可用则用，
    Windows 无 tzdata 时回退固定 +08:00）；比较前统一换算 UTC。
36. **日记隐私护栏**：他人昵称（最长优先）→「有人」、bot 昵称 →「我」（记 `diary.privacy`）；
    命中 user_id 字面量或哨兵/围栏 → 拒写；`privacy_check=false` 时跳过昵称替换但仍禁止哨兵/围栏落盘。
37. **日记预算两级**：`select_excerpts` 保证注入 ≤300 / ≤2 篇（第 1 篇必装、第 2 篇整篇装）；`prompt`
    在总预算超限时再按「日记 → 检索 → 最近轮」二次裁减，两级互不替代。
38. **日记移档 / 生成 / cap 的职责分离**：`memory.maintain()` 只做移档（created_at ≤ now-30d
    → diary.archive.jsonl）+ 全量重写，**不触发生成**（生成需要宿主 LLM，走独立 worker 的两阶段）；
    当日生成由宿主在 `diary.daily_at` / 启动补偿触发；cap 检查在 `DiaryWorker.finish` 落盘前执行。
    daily 稳态恰为 30 篇不触发 cap；every_n 与 daily 叠加才可能 cap_hit。
39. **日记只做时间窗口**：不做 S/R 衰减、不做月度回顾（v1.7 候选）；active = (now-30d, now]；
    archive 不参与注入，不足 2 篇按实际注入、不回退 archive、不记 error（D12）。
40. **日记 turn_range**：daily 取本地当日 00:00 起、every_n 取该 scope 上次成功日记起；
    只记录该 scope 的 turn，内存 turn 日志上限 10000 条，不持久化。
41. **反向断层话术（§7.6，只定态度不做功能）**：群聊不引用私聊内容；用户追问时只回应
    「私聊的事不方便在群里说」，不实现任何 dm → 群聊的查询/拼接/注入功能。该态度语句已写入默认人设
    （`cli.DEFAULT_PERSONA` 与 `examples/persona.example.md` 各一行）；代码层仍由
    「群聊禁止检索任何 dm 记忆」保证（无跨 scope 桥接功能）。

## 8. 扩展点

- **换 embedding**：实现 `embedding.Embedder` 并在 `get_embedder` 分支返回；向量维度改配置。
- **换宿主**：HTTP API 是稳定边界；Node/Python 均可接入，不共享内存。
- **指标落地**：`Metrics.event` 可替换为 prometheus/文件轮转，不影响业务代码。
- **日记生成模型**：`diary.gen_model` 仅文档/宿主用；worker 的 `llm` 由宿主注入（本地小模型或主模型）。
- **用户可见 / 可删**：附录 A B3 待定；预留 `store.archive`，未来加 `/forget` 只需归档入口。
- **多群扩展**：需重评候选池 / 混合池打分 / 桥接预算（R3），不在 v1.6 承诺内。
