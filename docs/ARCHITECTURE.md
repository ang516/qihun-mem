# 实现架构与设计对照（v1.5-r2）

本文件说明「设计规范的每一节落在哪段代码」，以及实现时做的决策与已知偏差。
设计规范（当前权威）：[../QiHun-Memory-System-v1.5-r2.md](../QiHun-Memory-System-v1.5-r2.md)；
工程契约：[UPGRADE-CONTRACT-v1.5-r2.md](UPGRADE-CONTRACT-v1.5-r2.md)。

## 1. 目录结构

```text
qihun-mem/
├── QiHun-Memory-System-v1.5-r2.md  # 当前设计规范（冻结整合版，人工维护）
├── QiHun-Memory-System-v1.4.md     # 历史规范（已被取代，仅作追溯）
├── README.md
├── pyproject.toml                  # Python 包配置（version=0.2.0；pytest pythonpath=src）
├── docs/                           # ARCHITECTURE / INTERFACES / DEPLOYMENT / INTEGRATION / 工程契约
├── examples/                       # 示例配置、人设、state、memory、user_affect、raw 输出
├── src/qihun_memory/
│   ├── config.py                   # §8 参数总表 → 不可变配置（含 affect / bridge 节）
│   ├── models.py                   # §2.1 memory schema / affect 规整 / Set,Inc 操作符
│   ├── tokens.py                   # 固定 token 预算的估算
│   ├── parser.py                   # §4.2 协议 + §4.3 解析流水线 + §4.5 脱敏 + 发送层清洗
│   ├── gate.py                     # §4.1 写入闸门 + §6.1 轻路径
│   ├── lifecycle.py                # §4.7 / §5 / §6.3 公式（compute_S 唯一入口）
│   ├── state.py                    # §2.3 state.json 只读 + users.json + §2.4 UserAffectStore
│   ├── store.py                    # §2.6 JSONL 容错、affect.bad、原子重写、向量 sidecar、幂等
│   ├── embedding.py                # §6.6 embedding 后端与降级
│   ├── bridge.py                   # §6.2 / §6.7 桥接触发判定（无 I/O、无状态）
│   ├── retrieval.py                # §6.2~6.4 候选池 / 打分 / fill 装填 / 跨池去重
│   ├── prompt.py                   # §6.5 注入格式 + §4.2 协议 + §4.5 拼接
│   ├── summary.py                  # §7.1 滚动摘要 ≤200 tokens
│   ├── memory.py                   # §4.4/§4.7/§4.8/§5 写入·强化·维护门面（update_memory）
│   ├── pipeline.py                 # §7 每轮流程：prepare / finish / ctx 冻结 / write_back
│   ├── obsidian.py                 # Obsidian 只读库（无写 API）
│   ├── legacy_import.py            # 旧 Obsidian 记忆库一次性导入（只读/幂等/干跑）
│   ├── service.py                  # 本地 HTTP JSON API（宿主接入层）
│   ├── cli.py                      # init/status/search/list/affect/turn/maintain/...
│   ├── metrics.py                  # §12 结构化指标
│   ├── fsutil.py                   # 原子写（tmp → fsync → replace）
│   ├── version.py                  # 包版本 0.2.0
│   └── tools.py                    # 公共 API 门面
├── tests/                          # L1~L4 + A/B 用例（见 §6）
└── .github/workflows/ci.yml        # Python 3.11~3.14 矩阵
```

## 2. 模块与规范对照

| 规范 | 实现 | 关键点 |
|---|---|---|
| §2 存储结构 | `store.py` + `config.py` 路径 | 数据在 `data_dir`；JSONL 单写者 |
| §2.1 schema | `models.MemoryItem` | 字段一一对应；`affect` 可选；`vector` 仅运行时 |
| §2.3 state | `state.load_bot_state` | 缺失/过期 → 中性 + `state.stale`；无任何写函数 |
| §2.4 user_affect | `state.UserAffectStore` | 唯一读写入口；`get/step_update/decay/save` |
| §2.6 容错 | `store.load/append/rewrite` | 坏行隔离；affect 非法只丢字段并搬 `affect.bad.jsonl`；原子替换；`turn_key` 幂等 |
| §3 归属 | `retrieval.Retriever.search` 候选池 + `memory._scope_items` | 群聊混合池；dm 只同 user；桥接同 user 单向 |
| §4.1 闸门 | `gate.should_extract` | 四条规则 + @/回复；与检索解耦；`enforce_write` |
| §4.3 解析 | `parser.py` | `split_raw_candidate` / `parse_memories` / `parse_llm_output` / `clean_answer_for_send` |
| §4.4 落盘失败 | `memory.remember/retry_pending` | pending 只存新记忆；重试 1 次后丢弃；不重放强化/感情 |
| §4.5 注入防护 | `parser.sanitize_for_prompt` + `prompt.py` | 用户消息/persona/摘要/昵称全脱敏；记忆只进用户上下文 |
| §4.7 S_init | `lifecycle.compute_S("init", ...)` | valence + affect 双调制，cap 90 |
| §5.1 遗忘/复活 | `lifecycle.retention` / `memory._refresh_statuses` / `compute_S("revive")` | 惰性判定；三个 sim 阈值分工 |
| §5.2 压抑 | `lifecycle.effective_stability` | 仅 valence<-0.3 且 avoidance≥0.5；S_eff≥0.8S |
| §5.3 强化 | `compute_S("reinforce", ...)` / `memory.reinforce` | 注入且 sim>0.5；经 update_memory |
| §5.4/5.5 感情 | `state.UserAffectStore` + `pipeline.write_back` | 8a/8b/9a/9b/10 顺序锁定 |
| §6.1 路由 | `gate.is_light_message` + `pipeline.prepare` | 轻路径不检索；闸门不影响检索 |
| §6.2/6.3 检索 | `retrieval.Retriever` + `bridge.should_bridge` | 混合池；打分 → 跨池去重 → 排序 → fill；rel/familiarity/γ_u/affect_match 调制 |
| §6.4 装填 | `retrieval.fill` | `fill(entries, constraints, *, metrics=None)`；桥接预算来自 constraints；不做缺省兜底 |
| §6.5 注入 | `prompt.render_memory_block` | 行头含 affect / bridge；`header_tokens=24` |
| §6.6 embedding | `embedding.py` + sidecar | 可选 bge-small-zh；float16+base64；缺失降级 BM25 |
| §7 每轮流程 | `pipeline.TurnPipeline` | prepare/finish；ctx 冻结；顺序锁定 |
| §8 参数 | `config.py` | 默认值与 §8 一致（header 24 / k_a 0.3 / bridge 0.7/300） |
| §10 用例 | `tests/` | 见 §6 |
| §11 硬规则 | 全模块 | 见 §4 |
| §12 观测 | `metrics.py` + 各模块事件 | 事件名与告警阈值按 §12.1 |
| §13 治理 | 文档 + CI | 契约/规范/实现/测试四同步 |

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
   ├─ Summary.get()               滚动摘要
   └─ build_prompt()              system(persona + 数据声明 + 协议) + messages(摘要/记忆/最近5轮/当前)
   │  冻结 ctx（frozen + 白名单），返回 {turn_id, prompt, injection, retrieval, state, gate}
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
       8b remember()：去重 → compute_S("init") → 追加 JSONL（失败 pending）
       9a reinforce()：强化 / 复活本轮注入的记忆（只改内存，永不进 pending）
       9b 聚合 injected_items 中当前用户的非 neutral affect → 单次 step_update
       10 ctx.last_event_affect = first_non_neutral(memories)
```

## 4. 硬规则（§11）与实现方式

| # | 规则 | 实现 |
|---|---|---|
| 1 | 记忆必须有 `user_id` + `scope` | `memory.remember` 入口校验，缺失不写 |
| 2 | 先过滤候选池再打分 | `retriever.search` 第一步过滤/建池 |
| 3 | 注入必须标注说话人 | `render_memory_block` 用 `users.json` 当前昵称 |
| 4 | 每轮最多一次 LLM 调用 | 本系统不发起 LLM 调用；协议只描述单次输出 |
| 5 | 写入必须过闸门 | `gate.enforce_write=true`：未命中不写（记 `mem_extract.ungated`） |
| 6 | valence 永不单独驱动行为 | 只用于 S_init / 压抑 / 排序 |
| 7 | S ≤ 90 | `compute_S` 与加载路径双重封顶 |
| 8 | 压抑只调制 S_eff | `effective_stability` 不动 `item.S` |
| 9 | 禁止回写 state.json | 代码中无 state 写路径；桥接侧负责写 |
| 10 | 机制可推迟，字段不留空 | schema 仅含 §2.1 字段（+可选 affect/turn_key） |
| 11 | 正文 = 哨兵前内容 | `clean_answer_for_send` 是唯一发送源 |
| 12 | 禁止正则抠嵌套 JSON | 只用于找哨兵 / 剥围栏 / 去尾逗号 |
| 13 | 提取失败不影响回答 | 解析兜底 + finish 分段 try/except + 先发后写 |
| 14 | 坏条单条丢弃 | `normalize_item` 逐条；日志不含正文 |
| 15 | affect 描述事件，不描述 bot 心情 | `item.affect` 仅来自 LLM；`item.valence` 仅来自 state |
| 16 | user_affect 只调制排序 | 只进 type_bonus（familiarity）与 γ_u 因子；不参与 compute_S |
| 17 | affect/user_affect 失败只降增益 | 非法 affect 只丢字段；user_affect 写失败只记指标 |
| 18 | 桥接单向/同用户/显式/≤0.7/≤300/标注/去重 | `bridge.py` + `retrieval.fill` + `prompt`；群聊不读 dm |

## 5. 状态单点与接口边界（附录 D）

- **D1 四个单点**：
  - 记忆状态：`MemorySystem.update_memory(item_id, ops, reason="")`，白名单 `Set(S/status/last_access)`、
    `Inc(access_count/mention_count)`；非法 → `state.write_guard` + 整体不生效。
  - 用户感情：`UserAffectStore`（`get/step_update/decay/save`）。
  - S 公式：`lifecycle.compute_S(mode, type, ...)`（init/revive/reinforce）。
  - 去重键：`parser.dedup_key(item)`。
- **D2 解析/发送分离**：解析层只返回 `raw_answer`；发送层 `clean_answer_for_send` 唯一清洗；
  `MemoryOutputParser` 保留兼容。
- **D3 装填接口**：`retrieval.fill(items, constraints)`，桥接预算来自 `constraints.subgroup.bridge`，
  无 `bridge_items` 双源。
- **D6 顺序锁定**：emit 早于 write_back；8a 早于 8b；9b 在 fill 之后；由 L3/L4 断言守护（用例 31/44）。
  实现内部 write_back 顺序为 8a → 8b → 9a（强化，只改内存）→ 9b → 10。

## 6. 测试覆盖矩阵

| 规范用例 / 层 | 测试文件 |
|---|---|
| L1 解析 1~16 + affect 17~21 + 公式自检 | `tests/test_parser.py`、`tests/test_v15_parser.py`、`tests/test_formulas.py` |
| L2 状态单点 22~32（白名单 / UserAffectStore / compute_S / dedup_key） | `tests/test_memory.py`、`tests/test_v15_state.py`、`tests/test_store.py`、`tests/test_formulas.py` |
| L3 turn 集成 1~16、23~31、33~42（LLM/时钟/磁盘 mock） | `tests/test_pipeline.py`、`tests/test_retrieval.py`、`tests/test_prompt.py` |
| L4 组合 43~46 | `tests/test_pipeline.py`（组合场景用例） |
| A1~A7 绕开单点 / 负面 | `tests/test_v15_guards.py`（架构 / 静态断言） |
| B1~B7 数据 / 恢复 / 容量 / 对抗 | `tests/test_store.py`、`tests/test_hardening.py`、`tests/test_memory.py` |
| Obsidian 只读 + 只读视图导出 | `tests/test_obsidian.py`、`tests/test_obsidian_export.py` |
| HTTP 适配层（含 injection / user_affect） | `tests/test_service.py` |
| 旧库导入（映射/幂等/干跑/坏数据） | `tests/test_legacy_import.py` |
| CLI（含 affect 子命令） | `tests/test_cli.py` |
| Python 3.11 语法兼容守卫 | `tests/test_syntax_compat.py` |

## 7. 实现决策与已知偏差

1. **Python 实现（B1，已拍板）**：宿主通过 HTTP 两阶段接口调用；Node 侧不引入第二套记忆逻辑。
2. **token 预算用确定性估算**（`tokens.estimate_tokens`）：CJK 1 token/字、其余 4 字符/token；
   规范要求固定 token 数，未指定 tokenizer；该估算偏保守、无外部依赖。
3. **BM25 中文分词**：CJK 字符 bigram（单字 run 用单字）+ 拉丁/数字小写词；对几百条短文本足够稳定。
4. **embedding 可选**：默认 bge-small-zh-v1.5（`pip install -e ".[embed]"`）；不可用时 cos 权重置 0、
   BM25 权重置 1，记 `retrieval.degraded`；去重退化原文匹配，记 `dedup.degraded`。
5. **存储位置 = data_dir**：Obsidian 库只读，不依赖 vault 运行；`persona.md` 可来自只读副本。
6. **两阶段回合**：`prepare/finish` 保留「先发后写」；HTTP 模式下 finish 返回前已完成落盘（固有边界，
   见 DEPLOYMENT.md §4.8）。
7. **summary.json / user_affect.json 的落点**：规范 §2.4/§2.5 已给出；实现均原子写。
8. **dormant 复活的同轮强化**：复活当轮 S = S₀，跳过同轮强化（仍计数并更新 last_access）。
9. **affect 非法只丢字段**：原始行搬 `affect.bad.jsonl`，条目以 `affect=None` 加载；不丢整条。
10. **turn_key 持久化边界**：新建条目随条目落盘；整轮全部命中合并时 turn_key 仅在内存生效，
    每日重写后随条目持久化；单实例、崩溃最多丢当天增量。
11. **每次写入都会尝试 embedding**：发生在发送之后，不增加用户可感延迟；失败条目由 finish 末尾与
    每日维护补算，不在 prepare/检索路径补算。
12. **指标事件**按 §12.1 命名；默认不记正文；`metrics.debug_raw=true` 时记 raw 前 500 字符并在维护时清理。
13. **clean_answer_for_send 切在最早哨兵前**：比「解析成功那个哨兵」更严格，保证多哨兵/伪造哨兵时
    正文也不含任何隐藏 JSON 块。
14. **检索空候选不降级**；非法 scope 返回空结果 + warning；单条时间戳损坏时该条 R=0、不中断检索。
15. **gate.enforce_write**：未命中闸门时不写记忆（记 `mem_extract.ungated`）；置 false 恢复宽松口径。
16. **压抑不产生持久后果**：dormant/archive 判定用未压抑的 R；避免几轮回避把记忆永久降级。
17. **降级模式相似度语义**：无 embedding 时 `sim` = BM25 批内归一值；`max==min`：全 0 → 0，
    同为正分（含单候选）→ 1.0。
18. **HTTP 时延语义**：`finish` 在服务端完成解析、摘要、写记忆后返回；进程内 `finish(sender=...)`
    严格先发后写。
19. **加载防脏数据**：时间戳不可解析 / S 非正 → 坏行；S>90 clamp；valence 非有限 → 0；state NaN/inf → 中性。
20. **finish 失败隔离**：解析后的摘要/写记忆/强化/向量补算/调试原文各自 try/except；失败只记
    `turn.finish_error`，回答照常返回；`/turn/finish` 成功后才清理 prepared，失败可用同一 turn_id 重试。
21. **向量 sidecar 维度与 id 边界**：加载用 embedder 实际维度校验；归档后 next_id 扫描 archive，不重用 id；
    archive 写入幂等。
22. **旧库导入走迁移通道**（`legacy_import.py`）：保留原始时间戳/S，不经过提取闸门，登记
    `turn_key="legacy:<vault>:<相对路径>"` 幂等；不改 vault、不写 state.json。
23. **现网运行配置偏差**：persona 2000 tokens、summary 500 tokens、桥接注入上限 `maxInjectChars=6000`
    属运行配置上调，不改变库默认值（§8）。
24. **state.json 的现网写入者**：库内无写路径；qq-bridge 每轮 prepare 前把情感快照写成
    `{valence, avoidance=max(0,-valence), updated_at}`（原子写、快照为空则不写）。
25. **桥接宿主两阶段边界**：`/turn/prepare` 的 `injection` 由宿主注入；静默/后台回合不注入不写；
    `<MEMORY_JSON>` 服务端与桥接侧双层剥离（含截断开哨兵、全角、零宽、裸 JSON）。
26. **Obsidian 只读视图导出**：`export-obsidian` 是独立显式导出工具（单向：记忆 → Markdown），
    数据没变不重写；Obsidian 内容不回流。
27. **v1.5 单点收口**：`update_memory` / `UserAffectStore` / `compute_S` / `dedup_key` 之外不存在第二份实现；
    A1/A4/A6 用架构/静态测试守护。
28. **ctx 冻结机制**：frozen dataclass + `mutable_fields` 白名单（不是类型系统强制）；越界写抛
    `ContextViolation` 并记 `ctx.violation`；残余风险 R1 已冻结。
29. **fill 接口无第二份预算**：装填层不硬编码 1200 / 300，桥接子预算跟随 constraints；A7 守护。
    `injected_items` 含 r/score/affect/bridge/source_pool/tokens/header_tokens，下游不得重新判断来源。
30. **检索顺序**：过滤/建池 → 打分 → 跨池去重（保留 score 高者、同分保留 dm）→ 确定性排序 → fill；
    `group_bridge.hit` 记录最终注入条数并附 `candidates`（去重后进 fill 前候选数）。
31. **normalize_text 的能力边界**：金样 + 2 条 property（幂等 / NFKC 稳定）进最小 CI；不承诺覆盖全部
    Unicode（R2）。
32. **affect_match 默认关闭**：开启后只由 `last_event_affect` 驱动；重启后自动失效，不读历史
    `memories[].affect`（R4 / I1）。
33. **桥接触发词的已知偏差**：`strong_words` 含裸「群」，私聊里泛指话题也会触发桥接；
    这是 v1.5-r2 冻结的已知过触发，v1.6 收敛（规范 §9.1），本版不修。

## 8. 扩展点

- **换 embedding**：实现 `embedding.Embedder` 并在 `get_embedder` 分支返回；向量维度改配置。
- **换宿主**：HTTP API 是稳定边界；Node/Python 均可接入，不共享内存。
- **指标落地**：`Metrics.event` 可替换为 prometheus/文件轮转，不影响业务代码。
- **用户可见 / 可删**：附录 B B6 待定；预留 `store.archive`，未来加 `/forget` 只需归档入口。
- **多群扩展**：需重评候选池 / 混合池打分 / 桥接预算（R3），不在 v1.5-r2 承诺内。

