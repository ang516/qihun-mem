# 部署与接入（qq-bridge / DSH）

本文说明如何把器魂记忆系统接入现网 QQ bot。设计规范见
[../QiHun-Memory-System-v1.4.md](../QiHun-Memory-System-v1.4.md)，实现架构见
[ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 前置条件

- Python >= 3.11（现网 DSH 桌面版自带 3.14 可用）
- 一个可写的记忆数据目录（不要放在只读 Obsidian 库内）
- 可选：`sentence-transformers` + 本地 `bge-small-zh-v1.5`（不装则自动降级纯 BM25）

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
- `vault_dir`：Obsidian 库，**只读**。系统只会读 `persona.md`（以及 `/obsidian/*` 接口列出的笔记），
  不会创建 / 修改 / 删除库内任何文件。
- `state.json` 由情感系统写入 `data_dir/state.json`，记忆系统只读；6 小时未更新按中性处理。

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

## 4. qq-bridge 接入（Node）

接入点只需要两个 HTTP 请求。伪代码如下（把 LLM 调用替换成现网 DSH 的调用）：

```js
const MEMORY = "http://127.0.0.1:8765";
const HEADERS = {
  "content-type": "application/json",
  authorization: "Bearer " + process.env.QIHUN_TOKEN,
};

async function memoryTurn({ userId, scope, text, msgid, atBot, replyToBot }) {
  // 1) 取 prompt（检索 + 摘要 + 人格 + 提取协议）
  const prepRes = await fetch(MEMORY + "/turn/prepare", {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify({
      user_id: userId,
      scope: scope,                // "group" | "dm"
      message: text,
      msgid: msgid,                // 消息 id，用于 turn_key 幂等
      at_bot: !!atBot,
      reply_to_bot: !!replyToBot,
    }),
  });
  const prep = await prepRes.json();
  if (!prep.ok) throw new Error(prep.error);

  // 2) 用 prep.prompt 调模型（每轮一次）
  const raw = await callYourLLM(prep.prompt.system, prep.prompt.messages);

  // 3) 回传原始输出：answer 由服务端解析，正文永远不含 <MEMORY_JSON> 块
  const finRes = await fetch(MEMORY + "/turn/finish", {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify({ turn_id: prep.turn_id, raw }),
  });
  const fin = await finRes.json();
  if (!fin.ok) throw new Error(fin.error);
  return fin.answer; // 只发这个；不要发 raw
}
```

要点：

1. **只发送 `fin.answer`**，绝不要把 `raw` 或 `prompt` 发给 QQ（§11 规则 11）。
2. `msgid` 尽量传（QQ 消息 id / seq），服务端用它做 `turn_key` 幂等，断线重放不会重复写记忆。
3. 群成员昵称刷新：拿到群成员列表后
   `POST /users/refresh {"members": {"123456": "昵称A"}}`（启动时 + 成员变动时）。
4. 定时维护：每天调一次 `POST /maintain`（dormant 判定 / 死记忆移档 / 全量重写）。
5. 私聊用 `scope=dm` + 对方 QQ 号；群聊用 `scope=group` + 说话人 QQ 号（§3.1）。

## 5. 观测与故障排查

- `GET /metrics` 查看 §12.1 指标；`metrics.jsonl` 里每行一个事件。
- 告警阈值（§12.1）：
  - `mem_extract.bad_json` > 2%/天 → 模型输出漂移；
  - `mem_extract.no_sentinel` > 5% → 提示词被截断；
  - `mem_write.fail` / `retrieval.degraded` / `state.stale` 非零 → 查磁盘 / 模型 / 情感系统。
- 用户看到 `{"memories"` 片段：检查发送链路是否用了 `raw` 而不是 `answer`。
- 记忆不增长：看 `mem_extract.bad_json` 与 `no_sentinel`。
- 回复变慢：看 `turn.prepare` / `turn.finish` 耗时；写盘在发送之后，理论上不影响可感延迟。

## 6. Obsidian 只读

```bash
# 只读测试：列出 / 读取 / 搜索
python -m qihun_memory.cli obsidian list -c config.json
python -m qihun_memory.cli obsidian read persona.md -c config.json
python -m qihun_memory.cli obsidian search 上海 -c config.json
```

项目内没有任何写入 Obsidian 的代码路径（`obsidian.ReadOnlyObsidianVault` 只提供读方法），
文件打开模式固定为 `"r"`，并且路径访问限制在库根目录内（拒绝 `..` 穿越与绝对路径）。
记忆数据由 `store.py` 直接写 `data_dir`，与 Obsidian 无关。

## 7. 备份与迁移

- 需要备份的只有 `data_dir`：`memory.jsonl` + `memory.vec.jsonl` 是权威数据；
  `*.json` 与 `summary.json` 可由进程重建但建议一起备份。
- 直接复制整个 `data_dir` 到新机器、修改 `config.json` 即完成迁移。
- `memory.archive.jsonl` 是死记忆档案（不参与检索），保留即可。
