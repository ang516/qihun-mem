# 器魂记忆系统（QiHun Memory System）v1.5-r2

面向「一个 QQ 群（≤10 人）+ 一个私聊」的轻量长期记忆引擎，按仓库根目录设计文档
[QiHun-Memory-System-v1.5-r2.md](QiHun-Memory-System-v1.5-r2.md) 实现（v1.5-r2 冻结整合版），
用于 DSH + SnowLuma + qq-bridge 搭建的 QQ bot。核心目标：记住该记的，忘掉该忘的，
任何解析 / 落盘 / 感情状态失败都不影响聊天。

## 当前状态

**v1.5-r2 冻结整合版落地**（2026-10-29，基线 v1.4 实现）：

- v1.4 的全部机制保持：哨兵解析 / JSONL 存储 / 遗忘强化 / 检索 / 每轮流程 / HTTP 适配层 / CLI /
  Obsidian 严格只读 / 旧库导入 / 只读视图导出。
- v1.5 新增：`item.affect`（事件级情绪标签）、`user_affect.json`（`UserAffectStore` 唯一入口）、
  私聊 → 群聊单向桥接、混合池检索 + rel / familiarity / affect_match 调制、`fill(items, constraints)`
  装填接口、解析层 / 发送层分离、四个状态单点、ctx 只读冻结、pending 只重放新记忆。
- 回归四层 L1~L4 + 最小 CI 冻结门槛（附录 D5）：`python -m pytest -q` → **648 passed**
  （Python 3.14；CI 矩阵 3.11 / 3.12 / 3.13 / 3.14）。
- 已接入现网 qq-bridge：每轮 prepare 注入（persona + 相关记忆 + 摘要 + 提取协议），finish 回写；
  `<MEMORY_JSON>` 服务端与桥接侧双层剥离，任何路径都不会漏到 QQ。
- Obsidian 只做只读镜像：`export-obsidian` 把记忆导出为只读视图，记忆系统本身不读 Obsidian。
- 源码与提交记录：<https://github.com/ang516/qihun-mem>

## 核心特性

- **归属先于内容**：每条记忆必须有 `user_id` 和 `scope`，检索先过滤再打分
- **写入有闸门**：只对时间 / 地点 / 承诺 / 偏好 / 第一人称状态等消息附提取协议
- **每轮最多一次 LLM 调用**：回答与记忆提取合并在同一次输出（哨兵分隔，正文永不含 JSON）
- **结构化输出是旁路**：解析 / 校验 / 写盘任一步失败 = 本轮无新记忆，回答照常
- **遗忘默认发生**：`R = exp(-Δt/S)` 惰性衰减，低于阈值转 dormant，强匹配可复活，不真删
- **情绪调制**：写入时 bot 情绪与事件 affect 强度抬稳定性（闪回）；检索时心境一致性微调排序；
  负性 / 回避状态临时压低痛苦记忆可提取性（压抑，永不改写 S）
- **用户感情（v1.5）**：`user_affect.json` 聚合 affection / annoyance / familiarity，每轮步进、
  按 7 天档位 ×0.9 衰减、30 天 stale 归零；只调制排序，不驱动行为
- **群聊记忆桥接（v1.5）**：私聊里显式触发时，低权重（0.7）引用同用户在群聊里的记忆，
  独立预算 300 tokens，跨池去重，注入时标注「群聊记忆」；群聊检索永不读私聊
- **状态单点（v1.5）**：S / status / 计数只经 `update_memory`；用户感情只经 `UserAffectStore`；
  S 公式只经 `compute_S`；去重键只经 `dedup_key`（绕开单点会被架构测试拦截）
- **单向只读情感状态**：只读 `state.json`，绝不回写
- **Obsidian 仅只读**：本项目没有任何写回 Obsidian 的代码路径

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
python -m qihun_memory.cli affect get --user u_123456 --data-dir ./data
```

启动本地 HTTP 服务（给 qq-bridge 等宿主调用）：

```bash
python -m qihun_memory.cli serve --data-dir ./data --port 8765
```

## CLI

| 命令 | 作用 |
|---|---|
| `init` | 初始化数据目录与示例人设 |
| `status` | 计数、embedding 可用性、state、user_affect、指标 |
| `search <query>` | 检索（不强化；群聊混合池；私聊可触发桥接） |
| `list` | 列出记忆（可按 scope / user / status 过滤，显示 affect） |
| `affect get --user <id>` / `affect decay --user <id> [--days N]` | 查看 / 惰性衰减用户感情（v1.5 新增） |
| `turn` | 两阶段回合；无 `--raw` 时只打印将发给 LLM 的 prompt |
| `maintain` | dormant 判定、死记忆移档、全量原子重写、向量补算 |
| `metrics` | 结构化指标与最近事件 |
| `obsidian list/read/search` | Obsidian **只读**访问 |
| `import-legacy` | 旧 Obsidian 记忆库一次性导入（幂等 / 可 `--dry-run`） |
| `export-obsidian` | 导出 Obsidian 只读视图（单向镜像，幂等） |
| `serve` | 启动本地 HTTP JSON API |

通用参数：`-c/--config config.json`、`-d/--data-dir data`；`--version` 输出 `qihun-memory 0.2.0`
（对应规范 v1.5-r2；CLI 帮助描述里写明 v1.5-r2）。

## 架构

```text
QQ 消息 ─┐
         ├─ prepare：state 只读 → 闸门(§4.1) → 路由(§6.1) → 检索(§6.2~6.4) → prompt(§6.5/§4.2)
LLM 调用 ┘                                      │
                                                ▼
          finish：parse(§4.3) → clean_answer_for_send → emit → 摘要/最近轮次
                  → write_back：8a 感情步进 → 8b 写记忆 → 9a 强化 → 9b affect 聚合 → 10 last_event_affect
```

两阶段接口对应设计文档 §7 的每轮流程，同时适配「LLM 调用由宿主发起」的现实：
宿主先 `POST /turn/prepare` 拿到 prompt，自己调模型，再把原始输出 `POST /turn/finish` 回来。
如果宿主愿意把模型调用交给本系统，也可以直接用 Python API
`TurnPipeline.run_turn(event, llm_call)`。

- 解析：`src/qihun_memory/parser.py`（`split_raw_candidate` → `parse_memories` → `parse_llm_output`；
  发送层唯一清洗点 `clean_answer_for_send`）
- 检索：`src/qihun_memory/retrieval.py`（候选池 → 桥接 → 打分 → `fill` 装填）
- 桥接触发：`src/qihun_memory/bridge.py`（无 I/O、无状态）
- 遗忘与强化：`src/qihun_memory/lifecycle.py`（`compute_S` 唯一入口）+ `memory.py`
- 用户感情：`src/qihun_memory/state.py`（`UserAffectStore` 唯一入口）
- 存储：`src/qihun_memory/store.py`（JSONL 追加 / 坏行隔离 / 原子重写 / 向量 sidecar）
- 模块与规范逐节对照、实现决策与偏差：见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 接口契约（贡献者必读）：[docs/INTERFACES.md](docs/INTERFACES.md)

## HTTP API（给 qq-bridge 集成）

默认 `127.0.0.1:8765`，配置 `service.token` 时要求 `Authorization: Bearer <token>`。
Obsidian 相关端点只有读，没有任何写接口。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 / 版本（0.2.0）/ 记忆计数 / embedding 可用性 |
| GET | `/state` | 只读的 bot 情绪快照（§2.3） |
| GET | `/user_affect?user_id=` | 只读的用户感情快照（v1.5 新增，§2.4） |
| GET | `/metrics` | 指标计数与最近事件 |
| GET | `/memory/list` | 列出记忆（含 affect） |
| GET | `/memory/search?q=&scope=&user_id=` | 检索（不强化；返回 `bridge={hit,candidates,truncated}`、`dedup={removed,degraded}` 摘要） |
| POST | `/turn/prepare` | `{user_id, scope, message, msgid?, at_bot?, reply_to_bot?, nickname?}` → prompt + `injection`（system_prompt / memory_block / summary / protocol / memory_ids）+ retrieval 摘要 |
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
├─ memory.jsonl             # 全部记忆，一行一条，只追加（§2.1，含 affect）
├─ memory.vec.jsonl         # 向量 sidecar：float16 + base64（§6.6）
├─ memory.bad.jsonl         # 加载时无法解析的行，原样搬入（§2.6）
├─ memory.archive.jsonl     # 死记忆移档，不物理删除（§8）
├─ user_affect.json         # 用户感情聚合（v1.5，唯一入口 UserAffectStore）
├─ affect.bad.jsonl         # affect 非法的原始行（条目仍加载，§2.6）
├─ summary.json             # 滚动会话摘要（≤200 tokens，脚本裁剪）
└─ logs/
   ├─ metrics.jsonl         # 每事件一行结构化日志（§12）
   └─ raw-debug.jsonl       # 可选：metrics.debug_raw=true 时保留 7 天
```

## 配置

复制 [examples/config.example.json](examples/config.example.json) 为 `config.json` 后按需修改；
所有默认值即设计文档 §8 参数总表。v1.5 新增 `affect` / `bridge` 两节：

```json
{
  "affect": {
    "initial_affection": 0.5,
    "initial_annoyance": 0.0,
    "initial_familiarity": 0.3,
    "step_alpha": 0.1,
    "stale_days": 30.0,
    "annoyance_decay_base": 0.9,
    "annoyance_decay_period_days": 7.0
  },
  "bridge": {"enabled": true, "weight": 0.7, "budget_tokens": 300},
  "decay": {"ka": 0.3},
  "retrieval": {
    "header_tokens": 24,
    "gamma_u": 0.15,
    "familiarity_eta": 0.1,
    "affect_match": false,
    "gamma_a": 0.1
  }
}
```

常用项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `data_dir` | `./data` | 数据目录（相对配置文件目录解析） |
| `vault_dir` | 空 | Obsidian 库根目录，**仅只读** |
| `persona_path` | 空 | 空则 `data_dir/persona.md`，不存在时回退 `vault_dir/persona.md` |
| `decay.*` | §8.1 | S₀、cap 90、0.1/0.02 阈值、0.5/0.7/0.9 sim 门槛、k_v=0.5、k_a=0.3、λ=0.2 |
| `retrieval.*` | §8.2 | 1200 tokens 预算、header 24、0.6/0.4 权重、β=0.2、γ_u=0.15、η=0.1、affect_match=false |
| `affect.*` | §8.3 | 用户感情初值 / 步长 / 30 天 stale / 7 天衰减周期 |
| `bridge.*` | §8.4 | 桥接开关 / 0.7 权重 / 300 tokens 子预算 / 触发词与实体表 |
| `embedding.*` | bge-small-zh | 可选 `sentence-transformers`；不可用自动退化为纯 BM25 |
| `metrics.debug_raw` | false | 是否记录 LLM 原文前 500 字符（7 天清理） |
| `service.token` | 空 | HTTP 鉴权 token |

## v1.4 → v1.5-r2 升级说明

1. **数据兼容**：老 `memory.jsonl` 无 `affect` 照常加载（`affect=None`）；老 `user_affect.json`
   若不存在从初值开始，含未知字段（如 `trust`）自动忽略。无需迁移脚本。
2. **配置新增**：老 `config.json` 可直接沿用；未出现的 `affect` / `bridge` / `decay.ka` /
   `retrieval.gamma_u / familiarity_eta / affect_match / gamma_a` 走默认值（见 §8）。
3. **行为变化**（都是增量，不改变回答可用性）：
   - 私聊在「群里 / 刚才 / 上次…」等显式触发下会引用同用户的群聊记忆（可用 `bridge.enabled=false` 关闭）；
   - 群聊检索改为混合池（不再只检索说话人自己的记忆）；
   - 注入行头 `header_tokens` 从 20 调为 24（`affect=` 段开销），预算总量仍 1200；
   - S 初始化在 affect 非 neutral 时额外乘 `(1 + 0.3·intensity)`。
4. **升级后建议**：跑一次 `maintain`（补向量 / 重写文件）；观察 `/metrics` 中
   `retrieval.degraded` / `dedup.degraded` / `group_bridge.*` / `user_affect.*`。
5. **回滚**：新字段都是可选的；v1.4 读取 ignore 未知字段，回滚到 v1.4 不会丢数据（`affect` 字段被忽略）。

## 设计决策（对应设计规范附录 B）

- **B1 Python**（已拍板）：§2 与 §4.3 的参考实现均为 Python；宿主（Node/qq-bridge）通过 HTTP 适配层调用。
- **B2 事件级 affect**（已拍板）：允许 LLM 输出可选 `affect`；非法只丢字段，不丢记忆；
  `valence` 语义不变。
- **B5 embedding**（已拍板）：默认 `bge-small-zh-v1.5`（512 维、CPU、本地、无网络），可选依赖
  `pip install -e ".[embed]"`；不可用时自动降级为纯 BM25 并记 `retrieval.degraded`。
- **B3/B4/B6 待拍板**：哨兵 nonce、物理删除、`/forget` 用户可见入口（见规范附录 B）。

## 测试

```bash
python -m pytest -q
```

回归四层（规范附录 D4/D5）：

- **L1** 解析器 + affect + 公式自检：§10.2 全 16 条、§10.3 affect 17~21、§10.4 公式。
- **L2** 状态单点：`update_memory` 白名单、`UserAffectStore` 四方法、`compute_S` 三支、`dedup_key`。
- **L3** turn 集成：LLM / 时钟 / 磁盘 mock；覆盖 1~16、23~31、33~42。
- **L4** 组合场景：43 组合、44 时序、45 迁移、46 降级链；另含 A1~A7 负面与 B1~B7 数据类。

## 文档

- 设计规范（当前权威）：[QiHun-Memory-System-v1.5-r2.md](QiHun-Memory-System-v1.5-r2.md)
- 工程契约：[docs/UPGRADE-CONTRACT-v1.5-r2.md](docs/UPGRADE-CONTRACT-v1.5-r2.md)
- 实现映射与决策：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 部署与接入：[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- 现网桥接与旧库导入契约：[docs/INTEGRATION.md](docs/INTEGRATION.md)
- 模块接口契约：[docs/INTERFACES.md](docs/INTERFACES.md)
- 历史规范：[QiHun-Memory-System-v1.4.md](QiHun-Memory-System-v1.4.md)

## License

待定（见设计规范附录 B；在确定前不添加 LICENSE 文件）。
