# 部署与接入（qq-bridge / DSH）

本文说明如何把器魂记忆系统接入现网 QQ bot。设计规范见
[../QiHun-Memory-System-v1.6-rc4.md](../QiHun-Memory-System-v1.6-rc4.md)（v1.6-rc4 冻结候选版），实现架构见
[ARCHITECTURE.md](ARCHITECTURE.md)，现网接入与旧库导入的完整契约见
[INTEGRATION.md](INTEGRATION.md)。

## 1. 前置条件

- Python >= 3.11（现网 DSH 桌面版自带 3.14 可用），且已 `python -m pip install -e .`（当前包版本 0.3.0 / 规范 v1.6-rc4）
- 一个可写的记忆数据目录（不要放在只读 Obsidian 库内）
- 可选：`sentence-transformers` + 本地 `bge-small-zh-v1.5`（不装则自动降级纯 BM25）
- qq-bridge：Node >= 20（现网 24），仓库在 `D:/大肥鱼的聊天记录/qq-bridge`

## 2. 安装与初始化

```bash
cd qihun-mem
python -m pip install -e .
python -m qihun_memory.cli init --data-dir D:/qihun-mem-data
```

把 `examples/config.example.json` 复制为项目根目录 `config.json`，按现网修改：

```json
{
  "data_dir": "D:/qihun-mem-data",
  "vault_dir": "D:/AI-Agent-Vault",
  "service": { "host": "127.0.0.1", "port": 8765, "token": "换成一个随机串" }
}
```

- `data_dir`：记忆系统自己的数据目录（记忆写入只发生在这里）。
- `vault_dir`：Obsidian 库，**只读**。系统只会读 `/obsidian/*` 接口列出的笔记（人格默认已复制到
  `data/persona.md`，不依赖 vault），不会创建 / 修改 / 删除库内任何文件。
- `state.json` 由情感系统写入 `data_dir/state.json`，记忆系统只读；6 小时未更新按中性处理。
  现网 qq-bridge 适配器会在每轮 prepare 前把情感引擎快照同步过去（见 §4.4）。
- v1.5 两节配置可省略（走默认值）：`affect`（用户感情初值/步长/衰减）与
  `bridge`（私聊→群聊桥接开关/权重/预算）。
- v1.6 新增 `diary` 节（叙事层开关 / 触发时刻 / 预算 / 窗口）与 `prompt` 预算分项
  （`total_tokens=3850` 等），可整节省略走默认值；完整默认值见规范 §10。

### 2.1 旧 Obsidian 记忆库 → 新记忆系统（一次性导入）

旧库（`AI-Agent-Vault`：persona / state / 滚动摘要 / episodes / clues）可以一次性导入新库。
导入器只读旧库，绝不修改；带 `turn_key` 幂等，重复执行不会产生重复记忆。

```bash
# 先干跑确认将写入什么
python -m qihun_memory.cli import-legacy -c config.json \
    --vault D:/AI-Agent-Vault --owner-id <群号> --owner-nickname 群记忆 --dry-run --json

# 真正导入
python -m qihun_memory.cli import-legacy -c config.json \
    --vault D:/AI-Agent-Vault --owner-id <群号> --owner-nickname 群记忆 --json
```

映射规则（详见 INTEGRATION.md §6）：

| 旧库 | 新库 |
|---|---|
| `memory/episodes/*.md` | `type=episode`，created_at 取文件名日期 20:00 +08:00，S 按 importance/情绪计算 |
| `clues/*.md`（排除 index） | `type=fact`，S=30，tags 带 slug |
| `memory/summaries/current_summary.md` | `type=fact`（S=60）+ 写入新 `summary.json` 种子 |
| `persona/state.json` | `type=fact`（关系状态快照，S=60） |
| `persona/core.md` | 复制为 `data_dir/persona.md`（已存在则跳过，`--force` 覆盖）；运行时 `persona_path` 指向该副本 |
| 全部 | `users.json` 写入 owner 昵称快照 |

导入后旧库保持原样，仅作为只读归档（`vaultMemory.enabled=false` 后 qq-bridge 不再写它）。

## 3. 启动 HTTP 服务

```bash
python -m qihun_memory.cli serve -c config.json
```

Windows 建议用计划任务 / NSSM 托管；Linux 可用 systemd：

```ini
[Unit]
Description=QiHun Memory Service
After=network.target

[Service]
WorkingDirectory=/opt/qihun-mem
ExecStart=/usr/bin/python3 -m qihun_memory.cli serve -c /opt/qihun-mem/config.json
Restart=always

[Install]
WantedBy=multi-user.target
```

现网 qq-bridge 场景下可以完全不手动启动：桥接发现 `/health` 不通时会按配置
`qihunMemory.autoStart` 自动拉起服务（见 §4.3）。

## 4. qq-bridge 接入（Node，现网已落地）

现网用 `src/qihun-memory.mjs` 适配器，一次接入后每轮自动注入与回写。设计要点：

1. **两阶段**：`POST /turn/prepare` 取 `turn_id` + `injection`；宿主调模型；
   `POST /turn/finish {turn_id, raw}` 返回去掉哨兵的 `answer`，服务端解析并写记忆。
2. **注入内容只用 `injection`**：`system_prompt`（persona + 历史数据声明 + 命中闸门时的提取协议）、
   `memory_block`（【相关记忆】）、`summary`（滚动摘要）、`diary_block`（【近期日记】，v1.6）。
   不把服务端 `prompt.messages` 整段塞给模型，避免和宿主自己的群聊上下文重复。
3. **只发送 `fin.answer`**，绝不发送 `raw` 或任何含 `<MEMORY_JSON>` 的文本（硬规则 11）。
4. `msgid` 尽量传（QQ messageId / seq），服务端用它做 `turn_key` 幂等，断线重放不会重复写记忆。
5. 群成员昵称：`/turn/prepare` 带 `nickname` 时会顺手刷新 `users.json`，也可启动时
   `POST /users/refresh {"members": {...}}`。
6. 定时维护：每天调一次 `POST /maintain`（dormant 判定 / 死记忆移档 / **日记 30 天移档** / 全量重写）。
   v1.6 服务启动后也会补偿执行一次（失败只记指标）。
7. 私聊用 `scope=dm` + 对方 QQ 号；群聊用 `scope=group` + 说话人 QQ 号（规范 §4）。
8. **HTTP 模式时延**：`/turn/finish` 返回时服务端已完成解析与落盘，所以「写盘耗时」发生在发送之前。
   append 写盘是 ms 级，实际可忽略；如果未来要求严格先发后写，需要把协议拆成
   `/turn/finish-parse`（返回 answer）+ `/turn/commit`（落盘），当前版本未做。
   日记生成走 `/diary/*` 独立两阶段，不在聊天主链路内，不增加可感延迟。

### 4.1 一键接入

```bat
:: 在 qq-bridge 根目录双击（或命令行运行）
一键桥接.bat
```

内部执行 `scripts/setup-qihun-memory.mjs`，幂等：

1. 生成/补全运行时配置 `state/qihun-memory/config.json`（含随机 `service.token`）；
2. 调 `import-legacy` 导入旧库；
3. 备份 `config.json` 到 `state/_lead-backup-<时间戳>/config.json`，然后原子改写：
   `vaultMemory.enabled=false` + 写入 `qihunMemory` 段；
4. 检测桥接是否在跑，提示重启；
5. 成功后启动 `一键启动.bat`（DSH / QQ / SnowLuma / 桥接 / 控制台）。

只想预览不改配置：`node scripts/setup-qihun-memory.mjs --dry-run`；
暂时不导入旧库：`--skip-import`；只导入不修改桥接配置：`--no-patch`。

运行时目录（不放进 git）：

```text
qq-bridge/state/qihun-memory/
├─ config.json        # 器魂配置（含 token、data_dir、vault_dir、persona_path、diary）
├─ service.log        # 服务 stdout/stderr（桥接拉起时写入）
└─ data/              # memory.jsonl / diary.jsonl / summary.json / users.json / persona.md / logs/
```

桥接 `config.json` 新增段：

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

### 4.2 桥接侧行为

- 每轮 `prepare`：把 `injection` 拼成注入块（persona + 相关记忆 + 摘要 + 日记 + 提取协议）放在 prompt 最前面，
  上限 `maxInjectChars`；服务不可用时降级为不注入，不影响 QQ 主链路。
  宿主要么用 `injection.diary_block` 单独拼日记，要么直接使用 `prompt.messages`（已含全部上下文；
  注意它包含「当前消息」段，宿主需自行去重）。
- 回合结束 `finish`：把模型原文回传服务，用返回 `answer` 作为发送正文；**本地还会再剥一次哨兵**
  （含模型输出被截断、只剩开哨兵的情况），任何路径都不会把隐藏 JSON 漏到 QQ。
- 静默 / 后台回合（控制台提醒、摘要投喂等）不注入、不写记忆、不记录日记 turn；
  `onlyRole` 为「大肥鱼」时人格卡被删（role=null）仍视为这条鱼，切到别的角色才停用。
- 旧的 `vaultMemory` 逻辑保留在代码里，但配置已关闭；旧库只读归档。

### 4.3 服务自启动

桥接启动时 `GET /health`（带 Bearer）；不通且 `autoStart=true` 时执行：

```text
python -m qihun_memory.cli serve --config <qq-bridge/state/qihun-memory/config.json>
```

以 detached 子进程拉起，stdout/stderr 追加到 `state/qihun-memory/service.log`，最多等约 10 秒。
失败只降级（不注入/不写记忆），不会阻塞 QQ 消息链路；桥接日志会出现
`器魂记忆已接入` / `[qihun] 记忆服务已启动并就绪`。

### 4.4 情感状态同步（state.json）

记忆系统只读 `state.json`（硬规则 9）。桥接会在每轮 prepare 前把情感引擎快照写成：

```json
{"valence": -0.35, "avoidance": 0.35, "updated_at": "2026-10-26T20:15:00+08:00"}
```

- `valence` 取情感引擎的加权情绪值（-1 ~ 1）；
- `avoidance = max(0, -valence)`；
- 情感系统关闭 / 快照不可用时**不写**，避免用 0 覆盖有效状态；
- 记忆服务侧只读，缺失 / 损坏 / 超过 6 小时按中性处理。

### 4.5 叙事层（日记）的宿主触发

v1.6 的日记生成是**离线/异步批处理**，不计入每轮 LLM 调用（硬规则 4 / 20）。服务端不主动调用 LLM，
由宿主按以下两种方式之一触发两阶段流程：

```text
POST /diary/prepare {scope: "group"|"dm", force?: bool}
  → {diary_id, due, date, turn_range, reason, prompt}
宿主调用自己的模型（主模型或 `diary.gen_model` 指定的小模型）
POST /diary/finish {diary_id, raw}
  → {status, reason, entry_id, tokens}   # 隐私/截断/cap/幂等全部在服务端执行
```

触发时机（二选一或都做）：

1. **每日 23:50（`diary.daily_at`，Asia/Shanghai）**：宿主定时器对 `group` / `dm` 各跑一次两阶段；
   服务端 `due()` 会判断「过了 daily_at 且当日同 scope 无日记」才生成（不是全局一篇）。
2. **启动补偿**：服务/宿主重启后若已过 23:50，启动时对两个 scope 各调一次 `/diary/prepare`；
   `due()` 自然补齐，不会重复（幂等键 + 当日同 scope 防重）。
3. 也可配置 `diary.trigger=every_n`（默认 `every_n=50`，按 scope 分别计数）或 `both`；
   服务端 `record_turn` 在每轮 finish 后累计，达到阈值时 `due()` 返回 True。

CLI 等价命令（手工/脚本触发）：

```bash
python -m qihun_memory.cli diary prompt -c config.json --scope group --json     # 取 prompt
python -m qihun_memory.cli diary add -c config.json --scope group --raw-file diary.txt --json
```

失败降级：生成失败 / 读取失败 / 隐私命中 / 超预算 / 幂等命中 → 静默降级 + 对应指标，不影响聊天（§8.6）。

### 4.6 最小手动接入示例（不用现成适配器时）

```js
const MEMORY = "http://127.0.0.1:18765";
const HEADERS = { "content-type": "application/json", authorization: "Bearer " + TOKEN };

const prep = await (await fetch(MEMORY + "/turn/prepare", {
  method: "POST", headers: HEADERS,
  body: JSON.stringify({ user_id: userId, scope, message: text, msgid, at_bot: atBot }),
})).json();

const inject = [prep.injection.system_prompt, prep.injection.memory_block,
  prep.injection.diary_block,
  prep.injection.summary && "【会话摘要】\n" + prep.injection.summary].filter(Boolean).join("\n\n");
const raw = await callYourLLM(inject + "\n\n" + yourPrompt);   // 每轮一次

const fin = await (await fetch(MEMORY + "/turn/finish", {
  method: "POST", headers: HEADERS,
  body: JSON.stringify({ turn_id: prep.turn_id, raw }),
})).json();
return fin.answer;   // 只发这个；不要发 raw
```

## 5. 观测与故障排查

- `GET /metrics` 查看 §14.1 指标；`metrics.jsonl` 里每行一个事件。
- 告警阈值（§14.1）：
  - `mem_extract.bad_json` > 2%/天 → 模型输出漂移；
  - `mem_extract.no_sentinel` > 5% → 提示词被截断；
  - `mem_write.fail` / `retrieval.degraded` / `state.stale` 非零 → 查磁盘 / 模型 / 情感系统。
- v1.5 观测：
  - `dedup.degraded`、`group_bridge.hit / truncated / dedup / userid_mismatch`：桥接与跨池去重健康度；
  - `user_affect.stale / fail`：用户感情文件健康度；
  - `state.write_guard`、`ctx.violation`：任何非零都是实现缺陷（有模块绕开单点 / 越界写 ctx）；
  - `retrieval.affect_bias`：`affect_match` 打开后本轮匹配条数。
- v1.6 新增观测：
  - `diary.ok / fail / skip / dup / privacy / truncated / cap_hit / inject / tokens / archive`：
    日记生成与注入健康度；`diary.fail` 非零查 worker；`diary.privacy` 非零查 prompt/脱敏；
    `diary.truncated` 频繁（建议日均 >3 次）才预警；`diary.cap_hit` 长期非零先查 every_n 频率；
  - `prompt.budget_over`：任何非零查 tokenizer 与截断顺序；
  - `mem_write.mention_fail`：mention_count 更新失败（E4）。
- 用户感情只读接口：`GET /user_affect?user_id=`（桥接/控制台可展示，禁止写回）。
- 日记只读接口：`GET /diary/list?scope=group`、`GET /diary/excerpts?scope=dm`。
- 用户看到 `{"memories"` 片段：检查发送链路是否用了 `raw` 而不是 `answer`。
- 记忆不增长：看 `mem_extract.bad_json` / `no_sentinel`；以及桥接日志里有没有
  `器魂记忆回写` / `prepare 失败`。
- 日记不增长：看 `diary.trigger`、宿主定时器是否在 23:50 调用了 `/diary/prepare`、两个 scope 是否都跑；
  以及 `diary.skip`（every_n 无新增）/ `diary.dup`（重启补偿重复）。
- 服务没起来：看 `state/qihun-memory/service.log` 与桥接日志的 `[qihun]` 行；
  确认 `python -m qihun_memory.cli --version` 可用、端口 18765 未被占用。
- 回复变慢：看 `turn.prepare` / `turn.finish` 耗时；写盘在发送之后，日记 worker 不在主链路，
  理论上不影响可感延迟。

## 6. Obsidian 只读与「记忆视图」

记忆系统运行时不写 Obsidian（`obsidian.ReadOnlyObsidianVault` 只提供读方法，无任何写路径）。
如果希望在 Obsidian 里**看到**新记忆，使用独立的只读视图导出命令（单向：记忆数据 → Markdown）：

```bash
# 默认导出到 <vault_dir>/器魂记忆/；也可 --out 指定
python -m qihun_memory.cli export-obsidian -c config.json --out D:/AI-Agent-Vault/器魂记忆
```

生成 6 个文件：`00_器魂记忆说明` / `10_群聊记忆` / `11_私聊记忆` / `20_滚动摘要` /
`30_用户与状态` / `40_已归档记忆`。导出是幂等的：数据没变不会重写文件，不会打扰 Obsidian。
（日记属于叙事层，**不进入** Obsidian 只读视图；它只在 prompt 中按 scope 注入。）

现网 qq-bridge 已自动调用：启动后导出一次，之后每轮聊天结束（15s 防抖）与每日维护后刷新；
开关在 bridge `config.json` 的 `qihunMemory.obsidianExport`（`enabled` / `vaultDir` / `subdir`）。
Obsidian 里的编辑不会回流到记忆数据。

## 7. Obsidian 只读（MCP/CLI）

```bash
# 只读测试：列出 / 读取 / 搜索
python -m qihun_memory.cli obsidian list -c config.json
python -m qihun_memory.cli obsidian read persona.md -c config.json
python -m qihun_memory.cli obsidian search 上海 -c config.json
```

项目内没有任何写入 Obsidian 的代码路径（`obsidian.ReadOnlyObsidianVault` 只提供读方法），
文件打开模式固定为 `"r"`，并且路径访问限制在库根目录内（拒绝 `..` 穿越与绝对路径）。
记忆数据由 `store.py` 直接写 `data_dir`，与 Obsidian 无关。
旧库导入器也只读取 vault，不写入。

## 8. 备份与迁移

- 需要备份的只有 `data_dir`：`memory.jsonl` + `memory.vec.jsonl` 是事实层权威数据；
  `diary.jsonl` 是叙事层数据（可重建但建议一起备份）；`*.json` 可由进程重建但建议一起备份。
  现网位置：`qq-bridge/state/qihun-memory/data/`。
- 直接复制整个 `data_dir` 到新机器、修改 `config.json` 即完成迁移。
- v1.5 新增文件 `user_affect.json` / `affect.bad.jsonl`；v1.6 新增
  `diary.jsonl` / `diary.archive.jsonl` / `diary.bad.jsonl`，随 `data_dir` 一起备份即可；老数据无需迁移。
- `memory.archive.jsonl` 与 `diary.archive.jsonl` 是移档数据（不参与检索 / 注入），保留即可；
  低 episode 记忆约 27.4 天移档，与日记 30 天窗口几乎同周期，运维可一起巡检。

## 9. 升级到 v1.6-rc4（从 v1.5-r2）

1. **拉代码 + 重装**：`python -m pip install -e .`（包版本 0.3.0）。
2. **配置**：老 `config.json` 可直接沿用；按需补 `diary` 节与 `prompt` 预算分项（不写走默认）。
   若现网把 `retrieval.header_tokens` 显式设成 20 以外的值、或把 prompt 分项手工调过，复核总预算仍 ≤3850。
3. **数据**：无需迁移；老的 `memory.jsonl` / `user_affect.json` 照常加载；
   `diary.jsonl` 不存在时自动创建（也可先跑一次 `init`）。
4. **重启服务**：`GET /health` 的 version 应为 0.3.0；跑一次 `POST /maintain`
   （补向量 / 重写 / 清理 / 日记 30 天移档）。
5. **宿主侧**：在 qq-bridge 加日记定时器（每日 23:50 + 启动补偿），
   调 `/diary/prepare` → 宿主 LLM → `/diary/finish`；注入侧把 `injection.diary_block` 拼进 prompt。
6. **验证**：
   - `python -m qihun_memory.cli --version` 输出 `qihun-memory 0.3.0`（对应规范 v1.6-rc4）；
   - `GET /health` 返回 version=0.3.0 且带 `diary` 计数；
   - `GET /diary/list` 可列出新生成日记；`GET /diary/excerpts?scope=group` 返回节选；
   - `GET /metrics` 无 `state.write_guard` / `ctx.violation` / `user_affect.fail` / `diary.fail`。
7. **回滚**：v1.6 接口与文件都是增量；回退代码与版本号即可，`diary.*` 配置与新文件被忽略，
   数据不丢（建议保留 `diary.jsonl` 系列以便再次升级）。详见
   [UPGRADE-CONTRACT-v1.6-rc4.md](UPGRADE-CONTRACT-v1.6-rc4.md) §5。
