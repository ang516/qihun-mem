# 实现架构与设计对照（v1.4）

本文件说明「设计文档的每一节落在哪段代码」，以及实现时做的决策与已知偏差。
设计规范：[../QiHun-Memory-System-v1.4.md](../QiHun-Memory-System-v1.4.md)。

## 1. 目录结构

```text
qihun-mem/
├── QiHun-Memory-System-v1.4.md   # 设计规范（人工维护，不改）
├── README.md
├── pyproject.toml                # Python 包配置；pytest pythonpath=src
├── src/qihun_memory/
│   ├── config.py                 # §8 参数总表 → 不可变配置
│   ├── models.py                 # §2.1 memory.jsonl schema / MemoryItem
│   ├── tokens.py                 # 固定 token 预算的估算
│   ├── parser.py                 # §4.2 哨兵解析 + 兜底 + §4.2.6 脱敏
│   ├── gate.py                   # §4.1 写入闸门 + §6.1 轻路径
│   ├── lifecycle.py              # §4.3 / §5 公式（S₀ / R / S_eff / 强化 / 心境）
│   ├── store.py                  # §2.4 JSONL 容错、原子重写、向量 sidecar、幂等
│   ├── state.py                  # §2.3 state.json 只读 + users.json 昵称快照
│   ├── embedding.py              # §6.5 embedding 后端与降级
│   ├── retrieval.py              # §6.2 / §6.3 过滤、打分、预算装填
│   ├── prompt.py                 # §6.4 注入格式 + §4.2.1 协议 + §4.2.6 拼接
│   ├── summary.py                # §7 步骤 2 / §8 滚动摘要 ≤200 tokens
│   ├── memory.py                 # §4.2.5/§4.3/§5/§6/§7 写入·遗忘·强化·维护门面
│   ├── pipeline.py               # §7 每轮流程：prepare / finish
│   ├── obsidian.py               # Obsidian 只读库（无写 API）
│   ├── legacy_import.py          # 旧 Obsidian 记忆库一次性导入（只读/幂等/干跑）
│   ├── service.py                # 本地 HTTP JSON API（宿主接入层）
│   ├── cli.py                    # init/status/search/list/turn/maintain/...
│   ├── metrics.py                # §12 结构化指标
│   └── tools.py                  # 公共 API 门面
├── docs/                         # 本文件 / INTERFACES / DEPLOYMENT
├── examples/                     # 示例配置、人设、state、memory.jsonl、raw 输出
└── tests/                        # 见 §5 测试覆盖矩阵
```

## 2. 模块与规范对照

| 规范 | 实现 | 关键点 |
|---|---|---|
| §2 存储结构 | `store.py` + `config.py` 路径 | 数据在 `data_dir`；JSONL 单写者 |
| §2.1 schema | `models.MemoryItem` | 字段与规范一一对应；`vector` 仅运行时 |
| §2.3 state | `state.load_bot_state` | 缺失/过期 → 中性 + `state.stale`；无任何写函数 |
| §2.4 容错 | `store.load/append/rewrite` | 坏行隔离到 `memory.bad.jsonl`；原子替换；`turn_key` 幂等 |
| §3 归属 | `retrieval.Retriever.search` 第一步 + `memory._scope_items` | 先过滤后打分；dm 只命同 `user_id` |
| §4.1 闸门 | `gate.should_extract` | 四条规则 + @/回复；与检索解耦 |
| §4.2 解析 | `parser.py` | 哨兵→括号平衡→清洗→loads→校验；永不抛异常 |
| §4.2.5 写盘失败 | `memory.remember/retry_pending` | pending 队列重试 1 次后丢弃；不阻塞回答 |
| §4.2.6 注入防护 | `parser.sanitize_for_prompt` + `prompt.py` | 用户消息/persona/摘要全脱敏；记忆只进用户上下文 |
| §4.3 S₀ | `lifecycle.initial_stability` | `S₀ = S0(type)·(1+k_v·|valence|)`，cap 90 |
| §5.1 遗忘 | `lifecycle.retention` / `memory._refresh_statuses` | 检索时惰性判定 dormant |
| §5.1 复活 | `memory.reinforce` | dormant & sim > 0.7 → S 重置 S₀ |
| §5.2 压抑 | `lifecycle.effective_stability` | 仅 valence<-0.3 且 avoidance≥0.5；S_eff≥0.8S |
| §5.3 强化 | `lifecycle.reinforce_stability` / `memory.reinforce` | 注入且 sim>0.5：S·(1+R)，cap 90 |
| §6.1 路由 | `gate.is_light_message` + `pipeline.prepare` | 轻路径不检索；闸门不影响检索 |
| §6.2 打分 | `retrieval.Retriever` | 0.6·cos + 0.4·bm25 → ×R → ×type_bonus → dormant×0.3 → 心境× |
| §6.3 预算 | `retrieval` 装填 | ≤1200 tokens，含 20 tokens/条头部；大条目跳过不截断 |
| §6.4 注入 | `prompt.render_memory_block` | 每行带 id/昵称/type/R/日期；不混入系统指令 |
| §6.5 embedding | `embedding.py` + `store` sidecar | bge-small-zh 可选；float16+base64；缺失降级 BM25 |
| §7 每轮流程 | `pipeline.TurnPipeline` | prepare/finish 两阶段；先发后写；摘要/最近 5 轮 |
| §8 参数 | `config.py` | 默认值与 §8 一致 |
| §10 用例 | `tests/` | 见 §5 |
| §11 硬规则 | 全模块 | 见 §4 |
| §12 观测 | `metrics.py` + 日志字段 | 事件名与告警阈值按 §12.1 |

## 3. 每轮数据流

```text
宿主（qq-bridge / DSH）
   │  POST /turn/prepare {user_id, scope, message, msgid?, at_bot?, reply_to_bot?}
   ▼
TurnPipeline.prepare
   ├─ load_bot_state()            只读 state.json（缺失/过期→中性）
   ├─ should_extract()            闸门：决定是否附提取协议
   ├─ is_light_message()          轻路径：决定是否检索（与闸门无关）
   ├─ MemorySystem.search()       过滤 → BM25/cos → R/bonus/心境 → 预算装填
   ├─ Summary.get()               滚动摘要
   └─ build_prompt()              system(persona + 协议) + messages(摘要/记忆/最近 5 轮/当前消息)
   │  返回 {turn_id, prompt, retrieval, state, gate, light_path}
   ▼
宿主调用 LLM（每轮一次；输出 = 正文 + <MEMORY_JSON>...）
   │  POST /turn/finish {turn_id, raw}
   ▼
TurnPipeline.finish
   ├─ parser.parse(raw)           永不抛异常；answer = 哨兵前内容
   ├─ sender(answer)              先发送（空正文不发空气泡）
   ├─ summary.update()            滚动摘要 ≤200 tokens
   ├─ MemorySystem.remember()     去重 → S₀ → 追加 JSONL → 更新内存；失败进 pending
   ├─ MemorySystem.reinforce()    被注入且 sim>0.5 的条目 S·(1+R)
   └─ metrics: turn.finish 等
```

## 4. 硬规则（§11）与实现方式

| # | 规则 | 实现 |
|---|---|---|
| 1 | 记忆必须有 `user_id` + `scope` | `memory.remember` 入口校验，缺失不写 |
| 2 | 先过滤 scope 再打分 | `retriever.search` 第一步过滤 |
| 3 | 注入必须标注说话人 | `render_memory_block` 用 `users.json` 当前昵称 |
| 4 | 每轮最多一次 LLM 调用 | 本系统不发起 LLM 调用；协议只描述单次输出 |
| 5 | 写入必须过闸门 | 只有 `gate_passed` 时才在 system prompt 附协议 |
| 6 | valence 永不单独驱动行为 | valence 只用于 S₀/压抑/排序 |
| 7 | S ≤ 90 | `initial_stability` / `reinforce_stability` 均封顶 |
| 8 | 压抑只调制 S_eff | `effective_stability` 不动 `item.S` |
| 9 | 禁止回写 state.json | 代码中无 state 写路径；测试验证 |
| 10 | 机制可推迟，字段不留空 | schema 仅含 §2.1 + 可选 `turn_key` |
| 11 | 正文 = 哨兵前内容 | parser 所有分支返回清洗后 answer |
| 12 | 禁止正则抠嵌套 JSON | 只用于找哨兵 / 剥围栏 / 去尾逗号 |
| 13 | 提取失败不影响回答 | parser 兜底 + pipeline 先发后写 + try/except |
| 14 | 坏条目单条丢弃 | `validate_items` 逐条校验；日志不含正文 |

## 5. 测试覆盖矩阵

| 规范用例 | 测试文件 |
|---|---|
| §10.3 公式自检 + §4.3/§5/§6.2 公式 | `tests/test_formulas.py` |
| §10.2 解析异常 1–14 + 性能 + metrics | `tests/test_parser.py` |
| §10.1 遗忘/去重/强化/复活/scope/心境/预算 | `tests/test_memory.py` + `tests/test_retrieval.py` |
| §2.4 存储容错/原子重写/向量/幂等/移档 | `tests/test_store.py` |
| §4.1 闸门 + §6.1 轻路径 | `tests/test_gate.py` |
| §6.4 注入格式 + §4.2.6 脱敏 | `tests/test_prompt.py` |
| §7 每轮流程（先发后写/无空气泡/幂等） | `tests/test_pipeline.py` |
| §12 指标 | 各测试内的 `Metrics` 断言 |
| Obsidian 只读 | `tests/test_obsidian.py` |
| HTTP 适配层（含 `injection` 契约） | `tests/test_service.py` |
| 旧库导入（映射/幂等/干跑/坏数据） | `tests/test_legacy_import.py` |
| CLI | `tests/test_cli.py` |

## 6. 实现决策与已知偏差

1. **Python 实现（附录 B1）**：与 §2 / §4.2.3 的参考实现一致；宿主通过 HTTP 两阶段接口调用，
   不改变「每轮一次 LLM 调用」的既有链路。Node 侧不引入第二套记忆逻辑。
2. **token 预算用确定性估算**（`tokens.estimate_tokens`）：CJK 1 token/字、其余 4 字符/token。
   设计文档要求「固定 token 数」，未指定 tokenizer；该估算偏保守，且无外部依赖。
3. **BM25 中文分词**：无第三方分词器，CJK 用字符 bigram（单字 run 用单字）+ 拉丁词。
   对几百条记忆的短文本检索足够稳定；训练/生产如需精确分词可替换 `retrieval` 内部实现。
4. **embedding 可选**：默认 `bge-small-zh-v1.5`（需 `pip install -e ".[embed]"` 且模型可本地加载）；
   不可用时 cos 权重置 0、BM25 权重置 1，并记 `retrieval.degraded`（§6.5 允许的降级）。
5. **存储位置 = `data_dir`**，Obsidian 库（`vault_dir`）只读：设计文档的 `AI-Agent-Vault/` 布局
   在实现中拆成「系统数据目录」与「只读 Obsidian 库」——因为用户要求 Obsidian 仅做只读，
   系统绝不通过 Obsidian 写回任何记忆；`persona.md` 可以从只读库读取。
6. **两阶段回合**：§7 的 `turn(msg)` 假设本系统自己调 LLM；实际宿主是 qq-bridge（Node）/DSH。
   拆成 `prepare` / `finish` 后，「先发后写」顺序仍由 `finish(sender=...)` 保证；
   `run_turn()` 提供等价的一次调用封装。
7. **`summary.json` 是新增文件**：§2 存储结构未给滚动摘要落点，实现放在数据目录（原子写）。
8. **dormant 复活的同轮强化**：§5.1 说「S 重置为 S₀」，§5.3 说「被注入且 sim>0.5 则 S·(1+R)」。
   实现取更具体的 §5.1：复活当轮 S = S₀，跳过同轮强化（但仍 `access_count += 1`、更新 `last_access`）。
9. **embedding 不可用时的去重**：§4.2.8 要求 cos > 0.9；降级时退回「strip 后内容完全相等」，
   并记 `memory.dedupe_degraded`（宁可不合并，也不误合并）。
10. **`turn_key` 持久化边界**：本轮有新建条目时 `turn_key` 随条目落盘，可跨重启去重；
    若整轮条目全部命中合并（没有新建），该 `turn_key` 只在进程内存中生效（每日重写后随条目持久化）。
    这是单实例、崩溃最多丢当天增量的取舍下可接受的边界。
11. **每次写入都会尝试 embedding**：发生在「先发送 answer」之后，因此不增加用户可感延迟；
    失败条目暂不参与语义分，由 finish 末尾与每日维护的 `ensure_vectors` 补算（§6.5）；
    **不在 prepare/检索路径里补算**，避免宿主取 prompt 时被模型编码阻塞。
12. **指标事件**按 §12.1 命名，另补充 `store.* / memory.* / turn.*` 便于排障；默认不记正文，
    `metrics.debug_raw=true` 时才记录 raw 前 500 字符，并在维护时清理超过 7 天的记录。
13. **parser 的 answer 切在「最早哨兵」之前**：§4.2.3 参考实现切在「解析成功的那个哨兵」之前；
    实现取更严格的口径（符合 §4.2.1 硬规则 2「正文 = 第一个哨兵之前」），保证多哨兵 / 伪造哨兵时
    正文也不含任何隐藏 JSON 块。同时解析成功时重置「未闭合」标记，避免成功块之后的丢弃残片
    被误报为 `truncated`。
14. **检索空候选不降级**：过滤后 0 条候选时不记 `retrieval.degraded`（空库每轮刷指标没有信息量）；
    非法 `scope` 返回空结果 + warning 而不是抛异常；单条时间戳损坏时该条 R=0、不中断检索。
15. **§11 规则 5「写入必须过闸门」的落地**：闸门未命中的轮次，即使模型漂移输出了哨兵块也不写记忆
    （`gate.enforce_write=true`，记 `mem_extract.ungated`）。闸门仍然只影响「写入」这一侧，
    检索与回答按 §7 保持解耦；把 `enforce_write` 设为 false 可恢复「协议只约束提示词」的宽松口径。
16. **压抑不产生持久后果**：检索 R 用 §5.2 的 `S_eff`（临时压低可提取性），但 dormant / archive
    的判定用**未压抑的 R**。否则几轮回避状态就会把记忆永久降级或移档，违背「不是忘掉，是不易想起」。
17. **降级模式的相似度语义**：无 embedding 时 `sim` = BM25 批内归一值（词面相似度，不是余弦）。
    `max==min` 的边界：全 0 → 0；同为正分（含单候选）→ 1.0，避免「单候选永远不强化/复活」退化。
    0.5 / 0.7 阈值在此模式下按词面相似度解释；接入真实 embedding 后恢复余弦语义。
18. **HTTP 两阶段的时延语义**：`POST /turn/finish` 在服务端完成解析、摘要、写记忆后才返回 answer，
    因此 HTTP 接入时「写盘耗时」在宿主发送之前；这是两阶段协议的固有边界，不是库逻辑偏差。
    进程内的 `TurnPipeline.finish(sender=...)` 严格先调用 sender 再写盘；DB/embedding 都属于 ms 级，
    HTTP 模式实际用户可感延迟影响很小。若未来需要严格拆分，可加 `/turn/finish-parse` + `/turn/commit`。
19. **加载路径的防脏数据策略**：时间戳不可解析、`S` 非正/非有限 → 该行进 `memory.bad.jsonl`；
    `S>90` 加载时 clamp 到 90（§11 规则 7）；`valence` 非有限 → 0 并 clamp 到 [-1,1]；
    `state.json` 的 NaN/inf → 中性 + `state.stale`，`avoidance` 缺失时按 `max(0,-valence)` 回退（§5.2）。
20. **finish 失败隔离**（§11 规则 13）：解析后的摘要 / 写记忆 / 强化 / 向量补算 / 调试原文各自独立
    `try/except`，任何一段失败只记 `turn.finish_error`，回答照常返回；`/turn/finish` 成功后才清理
    prepared，失败时宿主可用同一 `turn_id` 重试。`turn_key` 重放不重复摘要、不重复计数。
21. **向量 sidecar 的维度与 id 边界**：加载用 **embedder 实际维度**校验（配置 `dim` 只是后备），
    避免「配置 dim ≠ 模型 dim」时重启后全部向量作废；归档后重启的 `next_id` 会扫描
    `memory.archive.jsonl`，不会重用已归档 id；archive 写入幂等，避免重试导致重复归档行。
22. **旧库导入走迁移通道**（`legacy_import.py`）：旧 Obsidian 库的 episodes / clues / 摘要 / 状态
    需要保留原始时间戳与 S，因此直接构造 `MemoryItem` 写 `store`（仍受 schema 校验与原子写保护），
    并登记 `turn_key="legacy:<vault>:<相对路径>"` 做幂等；它不经过提取闸门（导入的是人工确认过的旧数据），
    也不写 `state.json`、不修改 vault。重复执行只做幂等跳过。
23. **现网 persona 预算与注入约定**：设计默认 persona ≈500 tokens；旧库 `persona/core.md` 有 1937 字符，
    为保持人格完整，现网配置把 `prompt.persona_tokens` 提到 2000，桥接侧注入上限
    `qihunMemory.maxInjectChars=6000`（persona + 记忆块 + 摘要：超限先丢摘要再截记忆块，
    persona 与提取协议优先保留）。导入后 `persona_path` 指向 `data/persona.md`（vault 不再是运行依赖）。
    这是运行配置偏差，不改变库默认值。
24. **state.json 的现网写入者**：库内仍无 state 写路径（§11 规则 9）。qq-bridge 适配器在每轮 prepare 前
    把情感引擎快照写成 `{valence, avoidance=max(0,-valence), updated_at}`（原子写、快照为空则不写），
    对应 §2.3「由情感系统写入」；记忆系统只读，缺失/过期按中性处理。
25. **桥接宿主的两阶段边界**：`/turn/prepare` 新增 `injection`（system_prompt / memory_block / summary /
    protocol / memory_ids），宿主只注入这些片段，不整段复用服务端 messages；静默/后台回合不注入不写；
    `<MEMORY_JSON>` 在服务端 parser 与桥接适配器两层剥离（含截断开哨兵、全角、零宽、裸 JSON），任何路径都不漏到 QQ。
    详细契约见 `docs/INTEGRATION.md`。
26. **现网 summary 预算**：设计默认滚动摘要 ≤200 tokens；旧库导入的摘要信息密度高（6 段），200 会丢掉
    关系温度/未了结/教训。现网运行配置把 `prompt.summary_tokens` 提到 500，并按
    「一句话现状 → 关系温度 → 未了结 → 最近话题 → 近期事件 → 教训」的优先级装填与裁剪。
    这是运行配置偏差，不改变库默认值。

27. **独立审查后的加固**（2026-10-26）：修复同会话多回合 turn 映射（FIFO）、finish 时状态切换导致的
    哨兵外泄、一键接入对运行中服务的并发写、dry-run 落盘、归档后重导、非日期 episode 静默丢弃、
    本地兜底裸 JSON、服务崩溃后退避/恢复、`/maintain` 定时、摘要种子覆盖等；详见提交记录与
    `docs/INTEGRATION.md` §10。原则不变：任何失败都只降级，不阻塞聊天、不写坏数据。

28. **Obsidian 只读视图导出**：设计原则「记忆系统不写 Obsidian」保持——新增的 `export-obsidian`
    是独立、显式的导出工具（`obsidian_export.py` + CLI），把 memory.jsonl 渲染成 Markdown 镜像；
    桥接在启动/每轮结束/每日维护后调用它（失败只 log），数据没变不重写。Obsidian 里的内容不会回流。
    这样既保留「只读」语义，又能让人在 Obsidian 里直接看到新记忆。

## 7. 扩展点

- **换 embedding**：实现 `embedding.Embedder` 并在 `get_embedder` 分支返回；向量维度改配置即可。
- **换宿主**：HTTP API 是稳定边界；Node/Python 均可接入，不共享内存。
- **指标落地**：`Metrics.event` 可替换为 prometheus/文件轮转，不影响业务代码。
- **用户可见 / 可删**：附录 B6 未决；预留 `memory.store.archive`，未来加 `/forget` 只需归档入口。