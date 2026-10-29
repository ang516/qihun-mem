# 部署与接入（qq-bridge / DSH）

本文说明如何把器魂记忆系统接入现网 QQ bot。设计规范见
[../QiHun-Memory-System-v1.5-r2.md](../QiHun-Memory-System-v1.5-r2.md)（v1.5-r2 冻结整合版），实现架构见
[ARCHITECTURE.md](ARCHITECTURE.md)，本次现网接入与旧库导入的完整契约见
[INTEGRATION.md](INTEGRATION.md)。

## 1. 前置条件

- Python >= 3.11（现网 DSH 桌面版自带 3.14 可用），且已 `python -m pip install -e .`（当前包版本 0.2.0 / 规范 v1.5-r2）
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
- v1.5 新增两节配置可省略（走默认值）：`affect`（用户感情初值/步长/衰减）与
  `bridge`（私聊→群聊桥接开关/权重/预算）；完整默认值见规范 §8。

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
   `memory_block`（【相关记忆】）、`summary`（滚动摘要）。不把服务端 `prompt.messages` 整段塞给模型，
   避免和宿主自己的群聊上下文重复。
3. **只发送 `fin.answer`**，绝不发送 `raw` 或任何含 `<MEMORY_JSON>` 的文本（§11 规则 11）。
4. `msgid` 尽量传（QQ messageId / seq），服务端用它做 `turn_key` 幂等，断线重放不会重复写记忆。
5. 群成员昵称：`/turn/prepare` 带 `nickname` 时会顺手刷新 `users.json`，也可启动时
   `POST /users/refresh {"members": {...}}`。
6. 定时维护：每天调一次 `POST /maintain`（dormant 判定 / 死记忆移档 / 全量重写）。
7. 私聊用 `scope=dm` + 对方 QQ 号；群聊用 `scope=group` + 说话人 QQ 号（§3.1）。
8. **HTTP 模式时延**：`/turn/finish` 返回时服务端已完成解析与落盘，所以「写盘耗时」发生在发送之前。
   append 写盘是 ms 级，实际可忽略；如果未来要求严格先发后写，需要把协议拆成
   `/turn/finish-parse`（返回 answer）+ `/turn/commit`（落盘），当前版本未做。

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
├─ config.json        # 器魂配置（含 token、data_dir、vault_dir、persona_path）
├─ service.log        # 服务 stdout/stderr（桥接拉起时写入）
└─ data/              # memory.jsonl / summary.json / users.json / persona.md / logs/
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

- 每轮 `prepare`：把 `injection` 拼成注入块（persona + 相关记忆 + 摘要 + 提取协议）放在 prompt 最前面，
  上限 `maxInjectChars`；服务不可用时降级为不注入，不影响 QQ 主链路。
- 回合结束 `finish`：把模型原文回传服务，用返回 `answer` 作为发送正文；**本地还会再剥一次哨兵**
  （含模型输出被截断、只剩开哨兵的情况），任何路径都不会把隐藏 JSON 漏到 QQ。
- 静默 / 后台回合（控制台提醒、摘要投喂等）不注入、不写记忆；
  `onlyRole` 为「大肥鱼」时人格卡被删（role=null）仍视为这条鱼，切到别的角色才停用。
- 旧的 `vaultMemory` 逻辑保留在代码里，但配置已关闭；旧库只读归档。

### 4.3 服务自启动

桥接启动时`GET /health`（带 Bearer）；不通且 `autoStart=true` 时执行：

```text
python -m qihun_memory.cli serve --config <qq-bridge/state/qihun-memory/config.json>
```

以 detached 子进程拉起，stdout/stderr 追加到 `state/qihun-memory/service.log`，最多等约 10 秒。
失败只降级（不注入/不写记忆），不会阻塞 QQ 消息链路；桥接日志会出现
`器魂记忆已接入` / `[qihun] 记忆服务已启动并就绪`。

### 4.4 情感状态同步（state.json）

记忆系统只读 `state.json`（§11 规则 9）。桥接会在每轮 prepare 前把情感引擎快照写成：

```json
{"valence": -0.35, "avoidance": 0.35, "updated_at": "2026-10-26T20:15:00+08:00"}
```

- `valence` 取情感引擎的加权情绪值（-1 ~ 1）；
- `avoidance = max(0, -valence)`；
- 情感系统关闭 / 快照不可用时**不写**，避免用 0 覆盖有效状态；
- 记忆服务侧只读，缺失 / 损坏 / 超过 6 小时按中性处理。

### 4.5 最小手动接入示例（不用现成适配器时）

```js
const MEMORY = "http://127.0.0.1:18765";
const HEADERS = { "content-type": "application/json", authorization: "Bearer " + TOKEN };

const prep = await (await fetch(MEMORY + "/turn/prepare", {
  method: "POST", headers: HEADERS,
  body: JSON.stringify({ user_id: userId, scope, message: text, msgid, at_bot: atBot }),
})).json();

const inject = [prep.injection.system_prompt, prep.injection.memory_block,
  prep.injection.summary && "【会话摘要】\n" + prep.injection.summary].filter(Boolean).join("\n\n");
const raw = await callYourLLM(inject + "\n\n" + yourPrompt);   // 每轮一次

const fin = await (await fetch(MEMORY + "/turn/finish", {
  method: "POST", headers: HEADERS,
  body: JSON.stringify({ turn_id: prep.turn_id, raw }),
})).json();
return fin.answer;   // 只发这个；不要发 raw
```

## 5. 观测与故障排查

- `GET /metrics` 查看 §12.1 指标；`metrics.jsonl` 里每行一个事件。
- 告警阈值（§12.1）：
  - `mem_extract.bad_json` > 2%/天 → 模型输出漂移；
  - `mem_extract.no_sentinel` > 5% → 提示词被截断；
  - `mem_write.fail` / `retrieval.degraded` / `state.stale` 非零 → 查磁盘 / 模型 / 情感系统。
- v1.5 新增观测（同见 §12.1）：
  - `dedup.degraded`、`group_bridge.hit / truncated / dedup / userid_mismatch`：桥接与跨池去重健康度；
  - `user_affect.stale / fail`：用户感情文件健康度；
  - `state.write_guard`、`ctx.violation`：任何非零都是实现缺陷（有模块绕开单点 / 越界写 ctx）；
  - `retrieval.affect_bias`：`affect_match` 打开后本轮匹配条数。
- 用户感情只读接口：`GET /user_affect?user_id=`（桥接/控制台可展示，禁止写回）。
- 用户看到 `{"memories"` 片段：检查发送链路是否用了 `raw` 而不是 `answer`。
- 记忆不增长：看 `mem_extract.bad_json` / `no_sentinel`；以及桥接日志里有没有
  `器魂记忆回写` / `prepare 失败`。
- 服务没起来：看 `state/qihun-memory/service.log` 与桥接日志的 `[qihun]` 行；
  确认 `python -m qihun_memory.cli --version` 可用、端口 18765 未被占用。
- 回复变慢：看 `turn.prepare` / `turn.finish` 耗时；写盘在发送之后，理论上不影响可感延迟。

## 6. Obsidian 只读与「记忆视图」

记忆系统运行时不写 Obsidian（`obsidian.ReadOnlyObsidianVault` 只提供读方法，无任何写路径）。
如果希望在 Obsidian 里**看到**新记忆，使用独立的只读视图导出命令（单向：记忆数据 → Markdown）：

```bash
# 默认导出到 <vault_dir>/器魂记忆/；也可 --out 指定
python -m qihun_memory.cli export-obsidian -c config.json --out D:/AI-Agent-Vault/器魂记忆
```

生成 6 个文件：`00_器魂记忆说明` / `10_群聊记忆` / `11_私聊记忆` / `20_滚动摘要` /
`30_用户与状态` / `40_已归档记忆`。导出是幂等的：数据没变不会重写文件，不会打扰 Obsidian。

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

- 需要备份的只有 `data_dir`：`memory.jsonl` + `memory.vec.jsonl` 是权威数据；
  `*.json` 与 `summary.json` 可由进程重建但建议一起备份。
  现网位置：`qq-bridge/state/qihun-memory/data/`。
- 直接复制整个 `data_dir` 到新机器、修改 `config.json` 即完成迁移。
- v1.5 新增文件 `user_affect.json` / `affect.bad.jsonl` 随 `data_dir` 一起备份即可；老数据无需迁移。
- `memory.archive.jsonl` 是死记忆档案（不参与检索），保留即可。
- 旧 Obsidian 库 `D:/AI-Agent-Vault` 导入后保持原样，可直接归档备份。

## 9. 升级到 v1.5-r2（从 v1.4）

1. **拉代码 + 重装**：`python -m pip install -e .`（包版本 0.2.0）。
2. **配置**：老 `config.json` 可直接沿用；按需补 `affect` / `bridge` 节（不写走默认）。
   若现网把 `retrieval.header_tokens` 显式设成 20，建议删除或改为 24（affect 行头开销）。
3. **数据**：无需迁移；老 `memory.jsonl` 无 `affect` 照常加载；`user_affect.json` 不存在时从初值开始。
4. **重启服务**：`GET /health` 的 version 应为 0.2.0；跑一次 `POST /maintain`（补向量 / 重写 / 清理）。
5. **验证**：
   - `python -m qihun_memory.cli --version` 输出 `qihun-memory 0.2.0`（对应规范 v1.5-r2）；
   - `GET /user_affect?user_id=<活跃用户>` 返回初值/已聚合值；
   - 私聊发「刚才群里说的那个」：有同用户群聊记忆时应出现 `group_bridge.triggered.*` 与
     `group_bridge.hit`；
   - `GET /metrics` 无 `state.write_guard` / `ctx.violation` / `user_affect.fail`。
6. **回滚**：v1.4 读取会忽略 `affect` / 新配置节，数据不会丢；回滚后 `user_affect.json` 保留但对 v1.4 无影响。
