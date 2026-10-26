# 器魂记忆系统（QiHun Memory System）v1.4

面向「一个 QQ 群（≤10 人）+ 一个私聊」的轻量长期记忆引擎，按仓库根目录设计文档
[QiHun-Memory-System-v1.4.md](QiHun-Memory-System-v1.4.md) 实现，用于 dsh + SnowLuma + qq-bridge
搭建的 QQ bot。核心目标：记住该记的，忘掉该忘的，任何解析/落盘失败都不影响聊天。

## 当前状态

v1.4 设计已完整落地为 Python 包（解析器 / 存储 / 遗忘强化 / 检索 / 每轮流程 / HTTP 适配层 / CLI），
测试覆盖设计文档 §10 的解析与公式用例。生产接入见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 核心特性

- **归属先于内容**：每条记忆必须有 `user_id` 和 `scope`，检索先过滤再打分
- **写入有闸门**：只对时间 / 地点 / 承诺 / 偏好 / 第一人称状态等消息附提取协议
- **每轮最多一次 LLM 调用**：回答与记忆提取合并在同一次输出（哨兵分隔，正文永不含 JSON）
- **遗忘默认发生**：`R = exp(-Δt/S)` 惰性衰减，低于阈值转 dormant，强匹配可复活，不真删
- **情绪调制**：写入时 bot 情绪抬稳定性（闪回），检索时心境一致性微调排序，负性/回避状态临时压低痛苦记忆可提取性（压抑，永不改写 S）
- **解析失败不阻塞聊天**：解析 / 校验 / 写盘任一步失败 = 本轮无新记忆，回答照常
- **单向只读情感状态**：只读 `state.json`，绝不回写（§11 规则 9）
- **Obsidian 仅只读**：`vault_dir` 只用于读取笔记 / 人设；本项目没有任何写回 Obsidian 的代码路径

## 快速开始

```bash
git clone https://github.com/ang516/qihun-mem.git
cd qihun-mem
python -m pip install -e ".[dev]"

# 初始化数据目录（persona / state / users / memory.jsonl）
python -m qihun_memory.cli init --data-dir ./data

# 查看状态
python -m qihun_memory.cli status --data-dir ./data
```

模拟一轮完整流程（用示例 LLM 输出代替真实调用）：

```bash
python -m qihun_memory.cli turn --data-dir ./data --user u_123456 --scope group \
  --message "我下周三去上海出差" --raw-file examples/raw-output.example.txt

python -m qihun_memory.cli search 上海 --data-dir ./data --scope group
```

启动本地 HTTP 服务（给 qq-bridge 等宿主调用）：

```bash
python -m qihun_memory.cli serve --data-dir ./data --port 8765
```

## CLI

| 命令 | 作用 |
|---|---|
| `init` | 初始化数据目录与示例人设 |
| `status` | 计数、embedding 可用性、state、指标 |
| `search <query>` | 检索记忆（不强化） |
| `list` | 列出记忆（可按 scope / user / status 过滤） |
| `turn` | 两阶段回合；无 `--raw` 时只打印将发给 LLM 的 prompt |
| `maintain` | dormant 判定、死记忆移档、全量原子重写 |
| `metrics` | 结构化指标与最近事件 |
| `obsidian list/read/search` | Obsidian **只读**访问 |
| `serve` | 启动本地 HTTP JSON API |

通用参数：`-c/--config config.json`、`-d/--data-dir data`。

## 架构

```text
QQ 消息 ─┐
         ├─ prepare：state 只读 → 闸门(§4.1) → 检索(§6) → prompt(§6.4/§4.2.1)
LLM 调用 ┘                                      │
                                                ▼
                       finish：parser(§4.2) → 先发送 answer → 写记忆(§4.2.5)
                                → 强化(§5.3) → 指标(§12) → summary/最近轮次
```

两阶段接口对应设计文档 §7 的每轮流程，同时适配「LLM 调用由宿主发起」的现实：
宿主先 `POST /turn/prepare` 拿到 prompt，自己调模型，再把原始输出 `POST /turn/finish` 回来。
如果宿主愿意把模型调用交给本系统，也可以直接用 Python API
`TurnPipeline.run_turn(event, llm_call)`。

- 解析：`src/qihun_memory/parser.py`（哨兵 → 括号平衡 → 清洗 → `json.loads` → 逐条校验）
- 检索：`src/qihun_memory/retrieval.py`（scope 过滤 → BM25+cos → R/type_bonus/心境 → 预算装填）
- 遗忘与强化：`src/qihun_memory/lifecycle.py` + `memory.py`
- 存储：`src/qihun_memory/store.py`（JSONL 追加 / 坏行隔离 / 原子重写 / 向量 sidecar）
- 模块与规范逐节对照、实现决策与偏差：见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 接口契约（贡献者必读）：[docs/INTERFACES.md](docs/INTERFACES.md)

## HTTP API（给 qq-bridge 集成）

默认 `127.0.0.1:8765`，配置 `service.token` 时要求 `Authorization: Bearer <token>`。
Obsidian 相关端点只有读，没有任何写接口。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 / 版本 / 记忆计数 / embedding 可用性 |
| GET | `/state` | 只读的 bot 情绪快照（§2.3） |
| GET | `/metrics` | 指标计数与最近事件 |
| GET | `/memory/list` | 列出记忆 |
| GET | `/memory/search?q=&scope=&user_id=` | 检索（不强化） |
| POST | `/turn/prepare` | `{user_id, scope, message, msgid?, at_bot?, reply_to_bot?}` → prompt |
| POST | `/turn/finish` | `{turn_id, raw}` → 回答正文 + 写记忆结果 |
| GET | `/obsidian/notes` | 列出只读库笔记 |
| GET | `/obsidian/note?path=` | 读取笔记（只读） |
| POST | `/users/refresh` | `{members: {user_id: nickname}}` 刷新昵称快照 |
| POST | `/maintain` | 每日维护（建议定时调用） |

Node 侧示例见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 数据目录

```text
data/                       # 由 config.data_dir 指定，已在 .gitignore
├─ persona.md               # 恒定人格（只读使用）
├─ state.json               # bot 情绪（情感系统写，本系统只读）
├─ users.json               # 昵称快照（启动/接口刷新）
├─ memory.jsonl             # 全部记忆，一行一条，只追加（§2.1）
├─ memory.vec.jsonl         # 向量 sidecar：float16 + base64（§6.5）
├─ memory.bad.jsonl         # 加载时无法解析的行，原样搬入（§2.4）
├─ memory.archive.jsonl     # R < 0.02 的死记忆移档，不物理删除（§8）
├─ summary.json             # 滚动会话摘要（≤200 tokens，脚本裁剪）
└─ logs/
   ├─ metrics.jsonl         # 每事件一行结构化日志（§12）
   └─ raw-debug.jsonl       # 可选：metrics.debug_raw=true 时保留 7 天
```

## 配置

复制 [examples/config.example.json](examples/config.example.json) 为 `config.json` 后按需修改；
所有默认值即设计文档 §8 参数总表。常用项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `data_dir` | `./data` | 数据目录（相对配置文件目录解析） |
| `vault_dir` | 空 | Obsidian 库根目录，**仅只读** |
| `persona_path` | 空 | 空则 `data_dir/persona.md`，不存在时回退 `vault_dir/persona.md` |
| `decay.*` | §8 | S₀、cap 90、0.1/0.02 阈值、0.5/0.7/0.9 sim 门槛、k_v、λ |
| `retrieval.*` | §8 | 1200 tokens 预算、0.6/0.4 权重、β=0.2、dormant×0.3 |
| `embedding.*` | bge-small-zh | 可选 `sentence-transformers`；不可用自动退化为纯 BM25 |
| `metrics.debug_raw` | false | 是否记录 LLM 原文前 500 字符（7 天清理） |
| `service.token` | 空 | HTTP 鉴权 token |

## 设计决策（对应设计文档附录 B）

- **B1 Python**：设计文档 §2 与 §4.2.3 的参考实现均为 Python，本仓库按此落地；宿主（Node/qq-bridge）通过
  HTTP 适配层调用，不破坏「每轮只一次 LLM 调用」的约束。
- **B5 embedding**：默认 `bge-small-zh-v1.5`（512 维、CPU、本地、无网络），作为可选依赖
  `pip install -e ".[embed]"`；不可用时自动降级为纯 BM25 并记 `retrieval.degraded`。
- **B6 用户可见/可删**：当前版本未提供 `/forget` 入口（设计文档标注为产品待定），记忆只归档不物理删除。

## 测试

```bash
python -m pytest -q
```

覆盖：§10.2 解析异常用例、§10.3 公式自检、§10.1 行为用例（遗忘/去重/强化/复活/scope 隔离/
心境偏向/预算装填）、存储容错、闸门、summary、只读 Obsidian、HTTP 适配层与 CLI。

## 文档

- 设计规范：[QiHun-Memory-System-v1.4.md](QiHun-Memory-System-v1.4.md)
- 实现映射与决策：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 部署与接入：[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- 模块接口契约：[docs/INTERFACES.md](docs/INTERFACES.md)

## License

待定（见设计文档附录 B；在确定前不添加 LICENSE 文件）。
