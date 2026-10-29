# 器魂记忆系统 v1.6-rc4 · 现网桥接与旧库导入契约

本文件是 qihun-mem 与 qq-bridge 两侧并行开发的唯一契约。任何改动先改本文件再改代码。
设计规范（当前权威）：[../QiHun-Memory-System-v1.6-rc4.md](../QiHun-Memory-System-v1.6-rc4.md)。
本文件 §1~§10 为 v1.4 起沿用的接入主契约；§11 是 v1.5-r2 增量（user_affect / 桥接 / 指标）；
§12 是 v1.6-rc4 增量（叙事层日记 / 预算 3850 / E1/E4/E5）；三者同时有效。

## 1. 目标

1. 把旧 Obsidian 记忆库 `D:\AI-Agent-Vault`（persona/state/summary/episodes/clues）
   一次性导入新版器魂记忆系统（`memory.jsonl` 等）。
2. qq-bridge 每轮改为调用新版 HTTP 记忆服务：注入记忆块 + 提取协议，回合结束回传原文写记忆。
3. 提供一键接入脚本：建运行时配置 → 导入旧库 → 改 bridge 配置 → 启动新体系。
4. 旧库只读归档，不再写入；旧 `vault-memory.mjs` 保留但配置关闭。

## 2. 运行时目录（不放进 git）

```
D:\大肥鱼的聊天记录\qq-bridge\state\qihun-memory\
  config.json          # qihun 配置（含 service.token）
  data\               # memory.jsonl / memory.vec.jsonl / memory.bad.jsonl /
                       # memory.archive.jsonl / summary.json / users.json / persona.md / logs\
  service.log          # 服务 stdout/stderr（桥接拉起时写入）
```

qihun 配置模板（config.json；data_dir 相对 config.json 所在目录解析）：

```json
{
  "data_dir": "data",
  "vault_dir": "D:/AI-Agent-Vault",
  "persona_path": "data/persona.md",
  "embedding": {"enabled": false, "backend": "none", "model": "", "dim": 0},
  "prompt": {"persona_tokens": 2000, "summary_tokens": 500},
  "gate": {"enforce_write": true},
  "affect": {"step_alpha": 0.1, "stale_days": 30.0},
  "bridge": {"enabled": true, "weight": 0.7, "budget_tokens": 300},
  "metrics": {"enabled": true, "debug_raw": false},
  "service": {"host": "127.0.0.1", "port": 18765, "token": "<随机 32 hex>"}
}
```

bridge 侧配置（config.json 新增；由 setup 脚本写入）：

```json
"qihunMemory": {
  "enabled": true,
  "baseUrl": "http://127.0.0.1:18765",
  "tokenFile": "state/qihun-memory/config.json",
  "onlyRole": "大肥鱼",
  "autoStart": true,
  "repo": "D:/qihun-mem",
  "python": "python",
  "requestTimeoutMs": 3000,
  "maxInjectChars": 6000
}
```

## 3. 服务端契约（`src/qihun_memory/service.py`）

保留现有全部端点。`POST /turn/prepare` 响应**新增** `injection` 字段（桥接只用它，不再自己拼 system）：

```json
{
  "ok": true,
  "turn_id": "...",
  "gate": true,
  "injection": {
    "system_prompt": "...",     // persona + 历史数据声明 +（命中闸门时）提取协议
    "memory_block": "...",      // 【相关记忆】块，可能为空串
    "summary": "...",           // 滚动摘要，可能为空串（由桥接按需注入）
    "protocol": "...",          // 命中闸门时=提取协议原文，否则 ""
    "memory_ids": ["..."]
  },
  "retrieval": {...},
  "state": {...}
}
```

- prepare 额外行为：payload 带 `nickname` 且 `user_id` 非空时，刷新 users.json 昵称（只在该用户昵称变化时写盘）。
- `/turn/finish` 不变：`{turn_id, raw}` → `{ok, answer, written, ...}`；`answer` 是去掉哨兵后的正文。
- 所有端点（含 /health）在配置了 token 时都要求 `Authorization: Bearer <token>`。
- 写入闸门、turn_key 幂等、坏数据隔离等规则全部沿用 v1.5-r2 规范（本文件 §11 为增量契约），桥接不得绕过。

## 4. 桥接适配器契约（bridge-dev 负责 `src/qihun-memory.mjs`）

导出 `createQihunMemory({ config, log, root })`，返回对象：

- `get enabled`：`config.enabled !== false` 且可解析 token。
- `async ensureReady()`：检查 `/health`；不可用时按 `config.autoStart` 用
  `spawn(python, ['-m','qihun_memory.cli','serve','--config', <qihun config 绝对路径>], {cwd: repo, detached:true, stdio:['ignore', logFd, logFd], windowsHide:true})`
  拉起服务并最多等 ~10s；失败只 log，不抛。
- `async prepare(sessionId, ctx) -> string`：ctx = `{ key, promptText, silent, internal, wakeSnapshot, userId, nickname, message, msgid }`。
  - 从 key 推导 scope：`group:*` → `"group"`，其余 → `"dm"`。
  - 优先用 ctx 里显式的 userId/message/msgid；否则取 `wakeSnapshot.messages` 中最后一条非本人消息
    （字段 userId/sender/messageId/text）；再否则取 `state/social-v2.json` 里该会话最近未读/最近消息；
    仍没有（主动开话题）用 `message = "（主动开话题）"`，user_id 用 key 后缀（群号/对方 QQ）。
  - 把 `turn_id` 与 key 记到 sessionId 映射表；返回要拼进 prompt 的注入块：
    `injection.system_prompt` + `injection.memory_block` +（summary 非空时）`【会话摘要】...`，
    总长上限 `config.maxInjectChars`；失败/服务不可用返回原 promptText（不注入）。
  - 服务不可用时本回合标记 skip，finish 时同样跳过写记忆。
- `async finish(sessionId, rawText) -> { text, changed, wrote, status }`：
  - 有该 session 的 turn 映射时 POST `/turn/finish`，返回 `answer` 作为 text；
  - 无映射/请求失败时必须本地兜底剥离 `<MEMORY_JSON>...</MEMORY_JSON>`（含只有开哨兵的截断情况），
    并 log 错误；**任何情况下都不能把哨兵/JSON 漏到 QQ**。
  - 完成后删除映射；映射表要有上限（≤256）与超时清理（>10min）。
- `async syncState(emotionSnapshot)`：把情感引擎快照写成 qihun `data/state.json`
  `{"valence": snapshot.valence, "avoidance": max(0,-valence), "updated_at": ISO+08:00}`（原子写、失败只 log）。
  state.json 只由桥接侧写，qihun 侧只读。
- 所有异常都不向桥接主循环抛；所有 HTTP 都要 `AbortSignal.timeout(config.requestTimeoutMs)`。

## 5. bridge.js 集成点（bridge-dev 负责，精确定位）

1. 顶部 import `createQihunMemory`；在 `createVaultMemory` 创建处（约 1615 行）后创建
   `const qihunMemory = createQihunMemory({ config: cfg.qihunMemory, log, root: ROOT })`，并 log 接入状态。
2. config 解析：在 vaultMemory 解析块（约 1164-1182 行）后增加 `cfg.qihunMemory` 规范化（字段见 §2）。
3. `async function main()` 启动阶段 `await qihunMemory.ensureReady()`（服务先起来再收消息）。
4. `deliverPromptNow`（约 10501 行）：`ensureSession` + wake prompt 组装完成后、
   `withSlangContext` 之前，若 `qihunMemoryActive()` 且非 silent/internal：
   `promptText = await qihunMemory.prepare(sessionId, { key, promptText, silent, internal, wakeSnapshot })`。
   同时保留旧 `withVaultMemory`（默认配置已关闭）。
5. `handleIncoming` 直连路径（约 12590 行，非 social 分支）：`ensureSession` 后同样
   `await qihunMemory.prepare(sessionId, { key, promptText, userId: String(event.user_id), nickname: sender, message: textContent, msgid: messageRef })`。
6. `pumpMux` 回合结束钩子（约 13021 行，vault 回写之后）：`ended.reason.kind === 'completed'` 时
   `const r = await qihunMemory.finish(frame.sessionId, turnText); turnText = r.text;`（静默回合已在更早 continue）。
7. 每轮 prepare 前调用 `qihunMemory.syncState(emotionSnapshotSafe())`（情感系统不可用就跳过）。

## 6. 旧库导入契约（importer-dev 负责）

新增 CLI 子命令（保持现有子命令不变）：

```
python -m qihun_memory.cli import-legacy -c <config.json> [-d <data_dir>] \\
    --vault <vault 目录> --owner-id <群号或对方QQ> [--owner-nickname 群记忆] \\
    [--scope group|dm] [--dry-run] [--json]
```

映射（全部写入 `memory.jsonl`，`created_at` 用旧文件日期 20:00 +08:00）：

| 旧库 | type | user_id | S | 内容 |
|---|---|---|---|---|
| episodes/*.md（排除 _模板） | episode | owner-id | `min(90, 7*(1+0.5*\|valence\|)*{1:1.0,2:1.3,3:1.6})` | `日期 事件：标题。发生了什么（+以后要记得）` ≤500 字 |
| clues/*.md（排除 index） | fact | owner-id | 30 | `线索 slug：一句话。已确认：...` ≤500 字 |
| memory/summaries/current_summary.md | fact | owner-id | 60 | `旧库滚动摘要（截至日期）：一句话现状/关系温度/未了结…` ≤500 字（模板空摘要跳过） |
| persona/state.json | fact | owner-id | 60 | `旧库关系状态：阶段…，粘人x/傲娇y/亲密度z/馋饭w（截至日期）` |
| persona/core.md | persona.md | - | - | 复制到 `data/persona.md`（已存在且非 --force 则跳过）；运行时 `persona_path` 指向它，vault 不再是运行依赖 |
| 全部 | users.json | owner-id → owner-nickname | - | 昵称快照 |

- tags：`["legacy", ...]`，episode 加 `importance:N` 和旧 clues（总 ≤8，每个 ≤16 字）；clue 加 slug。
- valence：情绪词映射（实现表约 -0.7~0.7，如 开心/乐 +0.7、得意 +0.6、平静 0、怒 -0.6、哀 -0.5；未知词 0）。
- 幂等：`turn_key = "legacy:" + vault 目录名 + ":" + 相对路径`；已存在该 turn_key 的跳过并计入 skipped_existing。
- 不写 state.json（由情感系统/桥接写）；不改 vault 任何文件。
- 实现建议：`load_config` + `MemorySystem`，直接 `store.append(MemoryItem(...))` + `store.register_turn_key`，
  不要走 remember（需要保留原始时间/强度）；summary 用 `RollingSummary` 新增的 `seed(key, units)` 写入。
- 报告 JSON：`{vault, dry_run, episodes, clues, summary, state, persona, users, skipped_existing, failed, written}`。

## 7. 一键接入（bridge-dev 负责 `scripts/setup-qihun-memory.mjs` + `一键桥接.bat`）

脚本幂等，步骤：

1. 解析路径：qihun repo（默认 `path.resolve(ROOT,'..','..','qihun-mem')`，可用 `--repo` 覆盖）、
   qihun config（默认 `state/qihun-memory/config.json`）、data 目录。
2. qihun config 不存在则按 §2 模板生成（随机 token；data_dir 相对 config 解析）。
3. 调 importer：`python -m qihun_memory.cli import-legacy -c <config> --vault D:/AI-Agent-Vault
   --owner-id <config.allow 的第一群号> --owner-nickname 群记忆 --json`，打印计数；失败中止（exit 1）。
4. 备份并修改 bridge `config.json`：备份到 `state/_lead-backup-<yyyyMMdd-HHmmss>/config.json`；
   `vaultMemory.enabled=false`；写入 `qihunMemory`（§2，baseUrl 从 qihun config 的 host/port 生成）；
   原子写。
5. 若检测到桥接正在运行（`state/bridge.lock` PID 存活）：提示需要重启桥接。
6. 支持 `--dry-run`、`--skip-import`、`--no-patch` 便于测试；纯函数（读/合并/写配置）要可被单测调用。

`一键桥接.bat`：UTF-8（chcp 65001）→ 跑 setup → 成功后 `start "" cmd /c 一键启动.bat`。

## 8. 验收

- Python：`cd D:\qihun-mem && python -m pytest -q` 全绿（含新增 import/service 用例）。
- Node：`cd D:\大肥鱼的聊天记录\qq-bridge && node --test scripts/test-qihun-memory.mjs scripts/test-qihun-setup.mjs` 全绿；
  `node --check src/bridge.js`；`npm run test:vault` 保持全绿。
- 真实导入：`import-legacy` 对 D:\AI-Agent-Vault 产出 ≥12 episode + ≥1 clue（旧库实际只有 rice.md，index 不计）+ summary/state；
  `qihun-memory search --scope group "大白饭"` 能命中；重复执行 skipped_existing 全部命中、无新增。
- 服务：`/health` 200；`/turn/prepare` 含 injection 字段；`/turn/finish` 返回去哨兵正文并写记忆。
- 桥接：重启后 bridge.log 出现「器魂记忆已接入」；一轮真实消息后 memory.jsonl 新增/合并记录；
  QQ 侧看不到 `<MEMORY_JSON>`。

## 9. 变更约定（模块边界）

- 服务端：`src/qihun_memory/service.py`、`tests/test_service.py`、`docs/INTEGRATION.md`、`README.md`、`docs/DEPLOYMENT.md`
  与运行时 config/data、最终验收与提交。
- 导入器：`src/qihun_memory/legacy_import.py`、`src/qihun_memory/summary.py`、`src/qihun_memory/cli.py`、`tests/test_legacy_import.py`。
- 桥接侧：`src/qihun-memory.mjs`、`src/bridge.js`、`scripts/setup-qihun-memory.mjs`、`scripts/test-qihun-memory.mjs`、
  `scripts/test-qihun-setup.mjs`、`config.example.json`、`package.json`、`一键桥接.bat`、`docs/QIHUN-MEMORY.md`。
- 共享核心（memory.py / store.py / models.py / config.py）改动必须先更新设计规范与接口文档，再改代码；
  不允许绕过附录 D1 的状态单点。

## 10. 现网落地记录（2026-10-26）

- 运行时位置：`D:/大肥鱼的聊天记录/qq-bridge/state/qihun-memory/`（config.json + data/ + service.log），
  服务 `127.0.0.1:18765`（token 在 qihun config 的 `service.token`）。
- 旧库导入：12 篇 episode + 1 条 clue（rice）+ 滚动摘要 + persona 状态 + persona 核心 = 15 条记忆；
  第二次执行 `written=0 / skipped_existing=16`，数据零增长。
- bridge 配置已切换：`vaultMemory.enabled=false` + `qihunMemory`（原配置备份在
  `state/_lead-backup-20261026-201324/config.json`）；旧 vault 保持只读归档。
- 一键启动验证：桥接日志出现 `器魂记忆已接入` / `[qihun] 记忆服务已启动并就绪`，
  `/health` 返回 `total=15`，SnowLuma / QQ 网关已连接；`一键桥接.bat --dry-run` 不落盘、不启动桥接。
- 真实回合验证（现网 QQ 私聊，2026-10-26 20:18–20:27）：
  `/turn/prepare` 记录 `scope=dm, gate=true`（DM 尚无记忆，candidates=0），bridge 注入人格 + 协议；
  回合结束 `/turn/finish` 返回 `status=empty`（模型本轮只输出哨兵、无新记忆），bridge 日志出现
  `[qihun] 记忆回写完成` / `器魂记忆回写`，指标 `turn.finish` / `mem_extract.empty` 落盘；
  同时 `syncState` 已写出 `data/state.json`（valence=1.0），证明情感状态同步 → 记忆系统只读链路正常。
  DM `summary.json` 已生成 `dm:473148751` 会话滚动摘要。群聊导入的 15 条记忆在 `scope=group`
  的准备结果中命中 7 条（clue/多篇 episode/摘要/状态）。

### 10.1 独立审查与加固（同日）

现网跑起来后做了第二轮独立审查（只读代码 + 现网数据 + 临时目录测试），发现并修复：

- **P0-1 同会话 turn 映射**：适配器原先每个 session 只存一个 turn，重叠回合会错归属/丢写；
  改为 FIFO 队列（每 session ≤8、TTL 10min），成功/404 才消费，失败保留重试；`retireSession` 与
  所有 reset 路径调用 `forgetSession`。
- **P0-2 一键接入对运行中服务背后写盘**：setup 在导入前探测 `/health`，按端口找 PID 并停止服务、
  轮询确认关闭；导入成功/失败后都以 detached 方式重启服务并等待就绪；找不到 PID/停不掉则拒绝导入。
- **P0-3 finish 时状态切换导致哨兵外泄**：completed 回合在 vault/qihun 钩子之后**无条件**
  `stripMemorySentinels(turnText)`；本地兜底同时处理零宽字符、全角、裸 `{"memories":…}`、截断裸 JSON。
- **P1/P2**：`--dry-run` 零落盘（read_only Store）；归档 `turn_key` 参与重导幂等；非日期 episode 不再
  静默丢弃；`at_bot/reply_to_bot` 全链路透传；服务失败 60s 冷却重试/退避；每日 `/maintain` 定时；
  `persona_path=data/persona.md`（vault 不再是运行依赖）；`summary_tokens=500`（现网偏差）；
  `baseUrl` 留空时按 qihun host/port 推导；setup 先备份 qihun 配置、importer 180s 超时；私聊昵称不再污染
  `users.json`；注入顺序统一；日志打印原始 prompt。
- 修复后验收（v1.4 当时）：Python **292 passed**（3.11 上 291 passed + 1 skipped）；桥接 `npm run test:qihun` **66 pass**、`test:vault` 10 pass、
  audit 41/46（5 个为本机既有环境问题）；真实环境已重部署（新桥接进程 + 新服务进程，`/health`
  `total=15`，`users.json` 含「群记忆 / 管理员」，persona 从 `data/persona.md` 读取）。
### 10.2 Obsidian 只读视图（2026-10-26 追加）

- 新增 `python -m qihun_memory.cli export-obsidian`：把 memory.jsonl 渲染成 Markdown 只读视图，
  默认输出到 `<vault_dir>/器魂记忆/`（6 个文件，幂等、数据不变不重写）。
- qq-bridge 已接入：启动后导出一次；每轮聊天结束（15s 防抖）与每日维护后自动刷新；
  配置在 bridge `config.json` 的 `qihunMemory.obsidianExport`（vaultDir/subdir/minIntervalMs/onTurn）。
- 只读边界不变：记忆系统本身不读 Obsidian；导出是显式编辑动作，Obsidian 里的修改不会回流。
- 现网验证：`D:/AI-Agent-Vault/器魂记忆/` 已生成 6 个文件（15 条记忆），Obsidian REST API 搜索
  「大白饭」能命中 `器魂记忆/10_群聊记忆.md`；启动导出日志 `未变 6`（幂等）。旧库根目录新增
  `器魂记忆入口.md` 指路（旧 persona/memory/clues 保留为归档）。
- 修复 CI（3.11 矩阵）收集失败：`_render_users_state` 在 f-string 表达式里复用了同种双引号，
  属于 PEP 701 写法（仅 Python 3.12+ 支持），3.11 直接 SyntaxError；改为先算 `embed_status` 再插值。
  新增 `tests/test_syntax_compat.py` 在 3.12+ 用新版分词器扫描 src/tests，禁止 3.11 不兼容的
  f-string 写法（同引号复用、表达式内反斜杠/注释）。

- 已知限制：`vaultMemory` 与 `qihunMemory` 同时开启时，pumpMux 先执行旧 vault 的 `consumeTurn`
  （会先剥掉隐藏块），导致新版 finish 拿不到哨兵、从而不写记忆。setup 默认已关闭旧 vault；
  如手工双开，请把新版 finish 放在旧 vault 之前，或只保留一个记忆层。

## 11. v1.5-r2 增量契约（叠加在 §1~§10 之上）

### 11.1 服务端新增 / 变化

- `GET /user_affect?user_id=<id>`（只读）：返回该用户的感情快照
  `{affection, annoyance, familiarity, updated_at, stale}`；未记录时返回初值。**没有写接口**。
- `GET /health` 版本号随包版本（v1.5-r2 为 0.2.0；v1.6-rc4 起为 0.3.0，见 §12.1）。
- `/memory/list`、`/memory/search` 的 item payload 增加 `affect`；search 额外返回：
  - `retrieval.bridge = {hit, candidates, truncated}`：`hit`=最终注入桥接条数，`candidates`=去重后进 fill 前候选数；
  - `retrieval.dedup = {removed, degraded}`：`removed`=跨池去重删除的重复对数（去重发生在打分之后）。
- `/turn/prepare` 的 `injection` 结构不变；新增的检索调制（混合池 / 桥接）全部在服务端完成，
  桥接注入行头以 `[群聊记忆 ...]` 标注，宿主无需感知。
- `/turn/finish` 返回值不变；写回顺序（8a→8b→9a→9b→10）由服务端保证。

### 11.2 桥接侧（qq-bridge）必须遵守

1. **不碰 user_affect.json**：用户感情只由记忆系统的 `UserAffectStore` 写；桥接不得读写
   `data/user_affect.json`。
2. **只写 state.json**：情感快照仍由桥接每轮 prepare 前写入（`{valence, avoidance=max(0,-valence),
   updated_at}`，原子写、快照为空不写）；记忆系统只读。
3. **群聊检索不桥接**：桥接只发生在私聊语境；桥接预算 300、权重 0.7、单向（dm 读 group），
   宿主不得在群聊回合请求或拼装群聊→私聊方向的数据。
4. **只发送 `finish.answer`**：任何情况下不把 raw / 哨兵 / JSON 发到 QQ（服务端 parser +
   桥接本地兜底双层剥离；v1.5 的 `clean_answer_for_send` 是服务端唯一发送清洗点）。
5. **msgid 幂等**：继续传 msgid 作为 `turn_key`；断线重放不重复写记忆、不重复感情步进。
6. **静默 / 后台回合**：不注入、不写记忆、不参与感情聚合。
7. **指标透传**：桥接日志建议透出 `group_bridge.*`、`user_affect.*`、`dedup.degraded` 摘要，
   便于线上定位（服务端 `GET /metrics` 已是权威来源）。

### 11.3 验收（v1.5-r2 追加）

- Python：`python -m pytest -q` 全绿（L1~L4 + A/B）；`python -m qihun_memory.cli --version` → `qihun-memory 0.2.0`（v1.5-r2 当时；v1.6-rc4 起为 0.3.0）。
- 服务：`GET /user_affect?user_id=` 200；`GET /health` version=0.2.0（v1.5-r2 当时）；prepare/search 含 affect 与 bridge 摘要。
- 桥接：真实私聊一轮后 `/metrics` 出现 `turn.finish`；若触发桥接，`group_bridge.triggered.*`、
  `group_bridge.hit` 可见；回复不含 `<MEMORY_JSON>` / 裸 JSON。
- 兼容：老数据（无 affect / 无 user_affect.json）导入后正常检索与写入（用例 45）。

## 12. v1.6-rc4 增量契约（叠加在 §1~§11 之上）

### 12.1 服务端新增 / 变化

- `GET /health` 版本号随包版本 **0.3.0**，并返回 `diary` 计数（无叙事层时为空 dict）。
- **叙事层端点（v1.6 新增）**：日记只提供人格语境，不参与事实层检索 / 打分 / S/R / per-user 记忆。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/diary/list?scope=&limit=` | `{ok,total,counts,items:[{id,date,scope_tag,content,tokens,turn_range,created_at}]}`；created_at 倒序，limit 1~200 默认 50 |
| GET | `/diary/excerpts?scope=` | `{ok,scope_tag,injected,tokens,truncated,excerpts:[...]}`；scope 必填 group/dm |
| POST | `/diary/prepare` | `{scope, force?}` → `{ok, diary_id, scope_tag, due, date, turn_range, reason, prompt}`；worker 不可用 → 503 |
| POST | `/diary/finish` | `{diary_id, raw}` → `{ok, scope_tag, status, reason, entry_id, tokens}`；未知 diary_id → 404；raw 非字符串 → 400 |

- `/turn/prepare` 的 `injection` **新增 `diary_block`**（【近期日记】块，可能为空串）与
  `diary_tokens`（本轮日记节选估算 tokens）；原有 `system_prompt / memory_block / summary /
  protocol / memory_ids` 语义不变。宿主优先从 `injection.diary_block` 取日记注入；若宿主不想自己拼，
  也可直接使用响应里的 `prompt.messages`（已含摘要 / 日记 / 记忆 / 最近轮 / 当前消息）。
  注入总预算 3850 在服务端保证；超限时服务端会先丢日记、再丢检索、再丢最近轮。
- `/turn/finish` 返回值不变；写回顺序（8a→8b→9a→9b→10）由服务端保证；
  v1.6 非重复 turn 会额外调用日记 worker 的 `record_turn`（仅内存计数，失败只记指标）。
- `POST /maintain` 追加「日记 30 天移档」与日记全量重写；服务启动后会补偿执行一次。
- E1 / E4 / E5 是**服务端内部语义修正**（mention_count 新建 0、去重只比对 active、复活优先），
  HTTP 请求 / 响应结构不变，宿主无需适配。

### 12.2 桥接侧（qq-bridge）必须遵守

1. **日记由宿主触发**：服务端不调用 LLM。宿主在每日 `diary.daily_at`（默认 23:50，Asia/Shanghai）
   对 `group` / `dm` 各跑一次 `POST /diary/prepare` → 宿主 LLM → `POST /diary/finish`；
   启动后若已过当日 23:50，做一次补偿（服务端 `due()` 幂等，不会重复落盘）。
   任一步失败只 log，不阻塞聊天。
2. **注入 `diary_block`**：prepare 注入块顺序建议 persona → 摘要 → 日记 → 相关记忆 → 提取协议，
   总长仍受 `maxInjectChars` 限制；不要把日记内容放进系统指令区。
3. **不碰 diary.jsonl**：日记只由服务端 `DiaryStore` 读写；桥接不得读写
   `data/diary.jsonl / diary.archive.jsonl / diary.bad.jsonl`。
4. **静默 / 后台回合**：不注入、不写记忆、不记录日记 turn、不触发日记 generation。
5. **叙事层失败不降回答可用性**：日记生成 / 注入失败时宿主照常回复（硬规则 19）。
6. **指标透传**：建议在桥接日志透出 `diary.inject / diary.fail / prompt.budget_over` 摘要；
   服务端 `GET /metrics` 仍是权威来源。
7. **桥接方向不变**：仍只允许「群聊池 → 私聊上下文」单向桥接；群聊检索禁止任何 dm 记忆；
   裸「群」强词过触发按 rc4 §0.1 推迟到 v1.7，宿主不要依赖该行为做功能。
8. **反向断层话术只定态度**：群聊不引用私聊内容；用户追问时 bot 只回应「私聊的事不方便在群里说」。
   宿主不需要实现任何 dm → 群聊的查询/拼接/注入，也不得把私聊内容注入群聊回合。
   该句已写入默认人设（`qihun_memory.cli.DEFAULT_PERSONA` 与 `examples/persona.example.md`，
   通过 `init` 生成 `persona.md` 的部署天然生效）；宿主沿用该人设即可。

### 12.2.1 qihun 运行时配置增量

```json
{
  "prompt": {
    "total_tokens": 3850,
    "summary_tokens": 200,
    "recent_turns": 5,
    "persona_tokens": 500,
    "recent_tokens": 750,
    "diary_tokens": 300,
    "protocol_tokens": 300,
    "system_misc_tokens": 600
  },
  "diary": {
    "enabled": true,
    "trigger": "daily",
    "every_n": 50,
    "daily_at": "23:50",
    "timezone": "Asia/Shanghai",
    "max_tokens": 300,
    "inject_budget": 300,
    "inject_count": 2,
    "retain_days": 30,
    "max_per_scope": 30,
    "privacy_check": true,
    "bot_nickname": ""
  }
}
```

- 两节都可整节省略走默认值；现网 `persona_tokens=2000` / `summary_tokens=500` 之类的运行偏差保留时，
  请确认 `total_tokens` 仍覆盖实际分项（服务端会在组装前实测，超限按顺序截断并记 `prompt.budget_over`）。
- `diary.bot_nickname` 建议填 bot 当前群名片/昵称（生成时用于把自身昵称替换为「我」）。

### 12.3 验收（v1.6-rc4 追加）

- Python：`python -m pytest -q` 全绿（L1~L4 + A/B + D 叙事层）；
  `python -m qihun_memory.cli --version` → `qihun-memory 0.3.0`（对应规范 v1.6-rc4）。
- 服务：`GET /health` version=0.3.0 且含 `diary` 计数；
  `/turn/prepare` 的 injection 含 `diary_block`；
  `/diary/list`、`/diary/excerpts`、`/diary/prepare`、`/diary/finish` 全链路可用。
- 日记：真实跑一次两阶段（CLI 或 HTTP），`diary.jsonl` 新增一行且 `content` 不含他人昵称；
  重复调用同 turn_range / 当日同 scope → `diary.dup` 不写。
- 兼容：老数据（无 diary / 无新 prompt 配置）升级后正常检索与写入；回滚见
  [UPGRADE-CONTRACT-v1.6-rc4.md](UPGRADE-CONTRACT-v1.6-rc4.md) §5。
