# v1.5-r2 工程升级契约（工程边界与回归验收）

> 目标：把仓库从 v1.4 实现升级到 **v1.5-r2 冻结整合版**。
> 本文件记录 v1.4 → v1.5-r2 升级期的工程边界、接口归属与验收方式；规范正文见 `QiHun-Memory-System-v1.5-r2.md`，接口细节以 `docs/INTERFACES.md` 为准。
> 公式、阈值、语义以规范正文为准；本文件只钉接口、文件归属和验收方式。

## 0. 总原则

- 不改 v1.5 的公式/阈值/语义；只做 v1.4 → v1.5-r2 的功能落地与工程边界。
- 兼容既有公开 API 的地方尽量保留（`MemorySystem`、`TurnPipeline.prepare/finish`、HTTP 服务、
  CLI 子命令）；新增接口按本文件。
- 所有新增/修改代码必须有测试；`python -m pytest -q` 必须全绿。
- 模块级写入边界以本文件的文件归属为准；跨文件接口变更必须先改规范与接口文档。

## 1. v1.5 功能增量（相对 v1.4）

1. `memory.jsonl` 每条新增 `affect: {label, intensity} | null`；非法 affect 只丢字段不丢条。
2. 新增 `user_affect.json` + `UserAffectStore`（唯一读写入口）。
3. 私聊 → 群聊记忆单向桥接（同 user、显式触发、低权重、独立预算、跨池去重）。
4. 检索打分新增 rel / familiarity / user_affect / affect_match；群聊为混合池。
5. 装填接口收敛为 `fill(items, constraints) -> {injected_items, truncated, total_tokens}`。
6. 解析器拆分 + 发送层 `clean_answer_for_send`；`normalize_item` / `dedup_key` / `dedup_within_batch`。
7. 状态单点：`update_memory(id, ops, reason)`（Set/Inc）、`UserAffectStore`、`compute_S(mode,...)`、`dedup_key(item)`。
8. turn 编排：build_context → call_llm（宿主）→ parse → clean → emit → write_back；ctx 只读冻结。
9. pending 只重放“新记忆写入”（带 turn_key 幂等）；强化与感情更新不重放。
10. 指标新增：`dedup.degraded`、`state.write_guard`、`ctx.violation`、`user_affect.*`、`group_bridge.*` 等。

## 2. 模块与接口（按文件归属）

### 2.1 `src/qihun_memory/models.py`

- `ALLOWED_AFFECT_LABELS = ("joy","sadness","anger","anxiety","affection","disgust","neutral")`
- `MemoryItem.affect: dict | None`（key: label / intensity）；`to_dict()` 非空时输出 `affect`；
  `from_dict()` 对非法 affect 容忍（置 None，不抛）。
- `Set(field, value)` / `Inc(field, delta=1)` 操作符 dataclass（供 update_memory 使用）。
- `JOY_AFFECT_LABELS = {"joy","affection"}`，`ANGER_AFFECT_LABELS = {"anger","disgust"}`。

### 2.2 `src/qihun_memory/config.py`

新增 section（默认值照规范）：
- `AffectConfig`：`initial_affection=0.5`, `initial_annoyance=0.0`, `initial_familiarity=0.3`,
  `step_alpha=0.1`, `stale_days=30.0`, `annoyance_decay_base=0.9`, `annoyance_decay_period_days=7.0`
- `BridgeConfig`：`enabled=True`, `weight=0.7`, `budget_tokens=300`, 强/弱触发词与实体模式（见 §2.6）
- `DecayConfig` 增 `ka=0.3`
- `RetrievalConfig`：`header_tokens=24`, `gamma_u=0.15`, `familiarity_eta=0.1`,
  `affect_match=False`, `gamma_a=0.1`
路径：`Config.user_affect_file`、`Config.affect_bad_file`。

### 2.3 `src/qihun_memory/lifecycle.py`

- `compute_S(mode, type, valence=0.0, affect=None, S=None, R=None, cfg=None)` 唯一 S 计算入口：
  - init：`S0*(1+0.5*|valence|)`，非 neutral affect 再乘 `(1+0.3*intensity)`，cap 90。
  - revive：裸 S0（episode 7 / fact|preference 30）。
  - reinforce：`min(S + S*(1+R), 90)`。
  - 其它 mode：`ValueError`。
- 旧函数 `initial_stability/revive_stability/reinforce_stability` 保留为薄封装（内部调 compute_S），
  避免破坏 `legacy_import` 等调用方；但禁止再出现第二份公式。

### 2.4 `src/qihun_memory/state.py`

`UserAffect` dataclass：`affection/annoyance/familiarity/updated_at/stale`。
`UserAffectStore(path, cfg: AffectConfig|None, metrics=None)`：
- `load()`：容错，未知字段（如 trust）忽略；坏文件按中性处理并记 `user_affect.fail`。
- `get(user_id, now=None) -> UserAffect`：中性兜底；stale（≥30 天）→ 中性且 familiarity=0。
- `step_update(user_id, deltas, *, now=None) -> UserAffect`：`x += delta*(1-x)`，clamp[0,1]，
  updated_at 刷新；每轮每分量调用方只调一次。
- `decay(user_id, days=None, *, now=None) -> UserAffect`：annoyance *= 0.9^floor(days/7)，stale 归零。
- `save()` 原子写；禁止其他模块直接读写 user_affect.json。

### 2.5 `src/qihun_memory/parser.py`

- `normalize_text(text)`：NFKC；全角→半角（NFKC）；中文标点→半角（仅键用）；去零宽/BOM/控制字符；
  折叠空白；ASCII 大小写折叠；emoji 保留；不折叠重复字符；不截断。
- `dedup_key(item) = (item["type"].lower(), normalize_text(item["content"]))`
- `validate_affect(item) -> dict|None`：非法 label/intensity → None；越界截断；非数字 → None。
- `normalize_item(m) -> dict|None`：只清洗/校验/截断（content 500、tags 8×16），不做去重；affect 可选。
- `dedup_within_batch(items) -> list[dict]`：按 dedup_key 精确去重，保留首次（含 affect）。
- `split_raw_candidate(raw) -> (raw_answer, candidate|None)`：只做哨兵/括号定位，不清洗 answer。
- `parse_memories(candidate) -> list[dict]`：loads → normalize → dedup → 上限；不碰 answer。
- `parse_llm_output(raw) -> (raw_answer, memories)`：只组合；永不抛异常。
- `clean_answer_for_send(raw_answer) -> str`：最早哨兵前；无哨兵剔除候选 JSON 块；最差原样；
  发送层唯一清洗点（含 fence/不可见字符/哨兵残片）。
- `MemoryOutputParser.parse()` 保留（兼容），内部用以上函数；`ExtractResult` 增加 `raw_answer` 字段，
  `.answer` 为清洗后正文。
- affect 非法记 `mem_extract.affect_invalid`；坏条记 `mem_extract.item_invalid`。
- 边界：解析层不调 `_drop_fences`；发送层不调 `_loads_candidate`。

### 2.6 `src/qihun_memory/bridge.py`

- `should_bridge(query) -> BridgeDecision(triggered: bool, strong: bool)`。
- 强词单命中触发；弱词需 ≥2 或弱词+实体指代。
- `entity_patterns` 只收时间/地点/具体事件名词，不收日常活动动词。
- 触发词表与实体表来自 `BridgeConfig`（可默认内置）。
- 该模块无 I/O、无状态。

### 2.7 `src/qihun_memory/retrieval.py`

- `SelectedMemory` 增加字段：`bridge: bool=False`, `source_pool: str="dm|group"|"bridge"`,
  `tokens: int=0`, `score_detail: dict|None`。
- `fill(entries, constraints) -> {"injected_items":[...], "truncated": bool, "total_tokens": int}`
  - constraints：`{"total":1200,"subgroup":{"bridge":300},"head_per_item":24}`
  - injected item = 记忆字段 dict + `bridge/source_pool/tokens`；9b 直接消费，不重新判断。
  - 桥接预算来自 constraints，装填层不硬编码；超限记 `group_bridge.truncated`。
- `RetrievalResult` 增加：`injected_items`, `bridge_hit: int`, `bridge_truncated: bool`,
  `dedup_removed: int`, `dedup_degraded: bool`；保留 `selected` 兼容。
- `Retriever.search(..., user_affect=None, last_event_affect=None)`：
  - 群聊：混合池（scope=group，不按 user_id 预过滤），rel=1 仅当前用户。
  - 私聊：dm 池；`should_bridge` 命中时追加同 user 的 active group 记忆（低权重 0.7）。
  - 打分：base=0.6cos+0.4bm25；×R；×type_bonus；dormant ×0.3；心境一致性 ×(1+β(2A-1))；
    rel=1 时 type_bonus ×(1+η·familiarity)、× (1+γ_u·(affection-annoyance))；
    affect_match 开启且 last_event_affect 非空时 ×(1+γ_a·affect_sim)。
  - 跨池去重：dedup_key 精确优先，余弦>0.9 仅作二次判定；向量不可用退化原文匹配并记
    `dedup.degraded`，与 `group_bridge.dedup` 分开。
  - 注入格式由 `prompt.render_memory_block` 消费 injected_items（含 bridge 标记）。
- 检索层只读：不调用 store.step_update，不写 user_affect.json。

### 2.8 `src/qihun_memory/prompt.py`

- `EXTRACTION_PROTOCOL` 增加 affect 可选字段示例。
- `render_memory_block(injected_items, users)`：每行头部
  `[记忆 m_0001|用户A|episode|R=0.82|affect=anger:0.6|10-23]`；bridge 行头为 `[群聊记忆|...]`；
  affect 缺失/neutral 省略；附桥接系统提示（“群聊记忆是用户本人在群里发言……”）。
- `build_prompt` 支持 injected_items（或继续接受 RetrievalResult）。

### 2.9 `src/qihun_memory/memory.py`

- `update_memory(item_id, ops, *, reason="") -> bool`：
  - Set 白名单：`S/status/last_access`；Inc 白名单：`access_count/mention_count`；
  - Inc 内部读旧值；非法字段/值 → `state.write_guard` 计数、整体不生效、返回 False；
  - S 写 clamp [0,90]。
- `remember()`：使用 parser 归一化后的条目；affect 写入；`compute_S("init", ...)`；
  去重先 dedup_key 精确、再余弦；向量不可用记 `dedup.degraded`。
- `reinforce()`：dormant 复活 `compute_S("revive", type)`；active 强化 `compute_S("reinforce", S=..., R=...)`；
  全部经 update_memory（Set/Inc）。
- `_refresh_statuses` / `maintain` 的 status 写也经 update_memory。
- pending：只存新记忆（带 turn_key），重放前查 turn_key 已存在则跳过；强化/感情更新永进 pending。
- `MemorySystem.affect_store` 暴露 UserAffectStore；`search()` 前 `decay`，并把 UserAffect 传入 Retriever。

### 2.10 `src/qihun_memory/pipeline.py`

- `TurnContext`（可继续用 `PreparedTurn` 名）只读冻结：persona/state/summary/candidates/retrieval/
  prompt/injected_items/user_id/scope/gate_passed/turn_key 等不可改；可写字段仅
  `last_event_affect / metrics / pending`；越界写抛 `ContextViolation` 并记 `ctx.violation`。
- `finish()` 顺序：parse → clean_answer_for_send → emit → summary/recent → write_back。
- `write_back(ctx, memories)`：
  1. 8a `affect_store.step_update(user_id, {"familiarity": alpha})`；
  2. 8b 逐条 persist（失败 pending + `mem_write.fail`）；
  3. 9b 聚合 ctx.injected_items 中 `user_id==当前用户` 且非 neutral 的 affect：
     I_joy=max(joy/affection)，I_anger=max(anger/disgust)，单次 step_update；`aggregate` 唯一聚合函数；
  4. 10 `ctx.last_event_affect = first_non_neutral(memories)`。
- 顺序由测试锁定：emit 早于 write_back；8a 早于 8b；9b 在 fill 之后。
- `retrieval.affect_bias` 指标：本轮 rel=1 且 affect_match 生效条数。

### 2.11 `service.py / cli.py / tools.py`

- 服务：item payload 增加 affect；search payload 增加 bridge/dedup 摘要；新增
  `GET /user_affect?user_id=`；health 版本随包版本。
- CLI：`list` 显示 affect；新增 `affect` 子命令（get/decay 可选）；帮助文本版本改 v1.5-r2。
- `tools.py` 导出新增公共符号（Set/Inc、compute_S、UserAffectStore、fill、clean_answer_for_send 等）。

### 2.12 文档

- 新增/替换 `QiHun-Memory-System-v1.5-r2.md`（完整规范，含 §0~§13、附录 A~E）。
- 更新 `README.md`、`docs/ARCHITECTURE.md`、`docs/INTERFACES.md`、`docs/DEPLOYMENT.md`、
  `docs/INTEGRATION.md`、`examples/*`、`pyproject.toml`、`src/qihun_memory/version.py`。
- README 状态改为 v1.5-r2；测试数、接口示例同步。

### 2.13 测试（模块测试随模块更新；新增 L1~L4 / A / B 回归矩阵单列）

- 旧测试按新公式/接口更新，不得删除覆盖点。
- 新增：
  - L1：解析器 10.2 全 16 条 + affect 17~21 + dedup_key 金样/property。
  - L2：update_memory 白名单、UserAffectStore 四方法、compute_S 三支、dedup_key 单点。
  - L3：turn 集成（顺序、ctx 冻结、pending、指标）。
  - L4：用例 43 组合、44 时序、45 迁移、46 降级链。
  - A1~A7 负面/绕开单点；B1~B7 数据/恢复/容量/对抗。
- `python -m pytest -q` 全绿。

## 3. 指标事件名（规范 §12.1）

`mem_extract.ok/empty/bad_json/no_sentinel/fail/affect_invalid/item_invalid`,
`mem_write.fail`, `retrieval.degraded`, `retrieval.pool.mixed`, `retrieval.affect_bias`,
`state.stale`, `state.write_guard`, `ctx.violation`, `user_affect.stale/fail`,
`group_bridge.triggered.strong/weak/hit/truncated/fail/dedup/userid_mismatch`,
`dedup.degraded`, `memory.*`, `turn.*`, `store.*`。

## 4. 验收

1. `python -m pytest -q` 全绿（含新增 L1~L4、A/B）。
2. `python -m qihun_memory.cli --version` 输出 v1.5-r2 对应版本。
3. 规范文档、README、接口文档与代码一致。
4. `git diff main` 中无越界文件修改；禁止提交 data/、日志、__pycache__。
