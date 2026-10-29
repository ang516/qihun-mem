# 器魂记忆系统（QiHun Memory System）v1.6-rc4

面向「一个 QQ 群（≤10 人）+ 一个私聊」的轻量长期记忆引擎，按仓库根目录设计文档
[QiHun-Memory-System-v1.6-rc4.md](QiHun-Memory-System-v1.6-rc4.md) 实现（v1.6-rc4 冻结候选版），
用于 DSH + SnowLuma + qq-bridge 搭建的 QQ bot。核心目标：记住该记的，忘掉该忘的，
任何解析 / 落盘 / 感情状态 / 日记生成失败都不影响聊天。

## 当前状态

**v1.6-rc4 冻结候选版落地**（2026-10-29，基线 v1.5-r2）：

- v1.4 / v1.5-r2 的全部机制保持：哨兵解析 / JSONL 存储 / 遗忘强化 / 检索 / 每轮流程 / HTTP 适配层 /
  CLI / Obsidian 严格只读 / 旧库导入 / 只读视图导出 / affect / user_affect / 私聊记忆单向桥接 /
  混合池检索 / 状态单点 / ctx 只读冻结 / pending 只重放新记忆。
- **v1.6 新增叙事层**：`diary.jsonl` 情感日记（`DiaryStore` 唯一读写入口 + 独立 `DiaryWorker`）。
  每日 23:50（`diary.daily_at`，Asia/Shanghai）按 scope 各生成一篇（group / dm 不混写），
  或 every_n 按 scope 独立计数触发；30 天硬窗口、同 scope 最多 30 篇、注入 ≤300 tokens / 最多 2 篇；
  隐私护栏（他人昵称 → 有人、bot 昵称 → 我、user_id / 哨兵 / 围栏拒写）。
  叙事层只提供人格语境，不参与检索、打分、S/R 与 per-user 记忆（规范 §8 / 硬规则 19~20）。
- **v1.6 预算修正**：8k 窗口最坏预算由 ≈3550 调为 **3850 tokens**
  （persona 500 + 摘要 200 + 日记 300 + 检索 1200 + 最近 5 轮 750 + 协议 300 + 系统其余 600）；
  prompt 组装前用同一 tokenizer 实测，超限按「日记节选 → 检索结果 → 最近 5 轮」截断，
  仍超记 `prompt.budget_over` 并按最小可用集组装。
- **v1.6 语义登记修正（§0.1）**：E1 新建条目 `mention_count=0`，去重命中 active 才 +1；
  E4 去重只比对同 user + scope + type 的 active 条目（dormant 不参与、不复活、不计数）；
  E5 检索强匹配复活优先（裸 S₀，不 `Inc(access_count)`、不叠 reinforce）。
- **v1.6 轻路径与维护**：`is_light_message` 五条规则（纯表情/短消息/寒暄整句/复读/纯 @bot）；
  轻路径只决定是否检索，命中闸门仍附提取协议；每日维护逐阶段 try/except 加固，
  追加日记移档与全量重写，启动后补偿执行一次。
- 回归四层 L1~L4 + 最小 CI 冻结门槛：`python -m pytest -q` → **781 passed**
  （Python 3.14；CI 矩阵 3.11 / 3.12 / 3.13 / 3.14）。
- 已接入现网 qq-bridge：每轮 prepare 注入（persona + 相关记忆 + 摘要 + 日记节选 + 提取协议），finish 回写；
  `<MEMORY_JSON>` 服务端与桥接侧双层剥离，任何路径都不会漏到 QQ。
- Obsidian 只做只读镜像：`export-obsidian` 把记忆导出为只读视图，记忆系统本身不读 Obsidian。
- 源码与提交记录：<https://github.com/ang516/qihun-mem>

## 核心特性

- **归属先于内容**：每条记忆必须有 `user_id` 和 `scope`，检索先过滤再打分
- **写入有闸门**：只对时间 / 地点 / 承诺 / 偏好 / 第一人称状态等消息附提取协议
- **轻路径省一次检索**：纯表情 / <4 字 / 寒暄 / 复读 / 纯 @bot 不检索；但闸门独立判定，
  「去上海」这类 3 字消息仍会提取记忆（v1.6）
- **每轮最多一次 LLM 调用**：回答与记忆提取合并在同一次输出（哨兵分隔，正文永不含 JSON）；
  日记生成走独立 worker，不计入每轮调用
- **结构化输出是旁路**：解析 / 校验 / 写盘任一步失败 = 本轮无新记忆，回答照常
- **遗忘默认发生**：`R = exp(-Δt/S)` 惰性衰减，低于阈值转 dormant，强匹配可复活，不真删；
  复活优先于强化，去重命中只计 mention 不强化（v1.6 §0.1）
- **情绪调制**：写入时 bot 情绪与事件 affect 强度抬稳定性（闪回）；检索时心境一致性微调排序；
  负性 / 回避状态临时压低痛苦记忆可提取性（压抑，永不改写 S）
- **用户感情（v1.5）**：`user_affect.json` 聚合 affection / annoyance / familiarity，每轮步进、
  按 7 天档位 ×0.9 衰减、30 天 stale 归零；只调制排序，不驱动行为
- **群聊记忆桥接（v1.5）**：私聊里显式触发时，低权重（0.7）引用同用户在群聊里的记忆，
  独立预算 300 tokens，跨池去重，注入时标注「群聊记忆」；群聊检索永不读私聊
  （裸「群」强词的过触发问题按 rc4 §0.1 推迟到 v1.7 处理）
- **叙事层（v1.6）**：日记只提供人格连续性语境，不提供事实依据；注入按 scope_tag 过滤，
  群聊只注 group、私聊只注 dm，不做跨 scope 桥接；失败只记指标、不影响回答
- **状态单点（v1.5）**：S / status / 计数只经 `update_memory`；用户感情只经 `UserAffectStore`；
  S 公式只经 `compute_S`；去重键只经 `dedup_key`（绕开单点会被架构测试拦截）
- **单向只读情感状态**：只读 `state.json`，绝不回写
- **Obsidian 仅只读**：本项目没有任何写回 Obsidian 的代码路径

## 快速开始

```bash
git clone https://github.com/ang516/qihun-mem.git
cd qihun-mem
python -m pip install -e ".[dev]"

# 初始化数据目录（persona / state / users / memory.jsonl / diary.jsonl）
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

叙事层（日记）：宿主持有 LLM，本进程只做两段式准备 / 落盘。

```bash
# 列出已有日记（group / dm，按 created_at 倒序）
python -m qihun_memory.cli diary list --data-dir ./data --scope group

# 两段式之一：打印日记生成 prompt（LLM 由宿主调用；--force 用于调试/补生成）
python -m qihun_memory.cli diary prompt --data-dir ./data --scope group --force --json

# 两段式之二：把 LLM 生成的日记文本写回（隐私护栏 / 幂等 / cap 仍生效）
python -m qihun_memory.cli diary add --data-dir ./data --scope group --raw-file diary.txt --force --json
```

启动本地 HTTP 服务（给 qq-bridge 等宿主调用）：

```bash
python -m qihun_memory.cli serve --data-dir ./data --port 8765
```

## CLI

| 命令 | 作用 |
|---|---|
| `init` | 初始化数据目录与示例人设（含空 diary.jsonl） |
| `status` | 计数、embedding 可用性、state、user_affect、日记计数、指标 |
| `search <query>` | 检索（不强化；群聊混合池；私聊可触发桥接） |
| `list` | 列出记忆（可按 scope / user / status 过滤，显示 affect） |
| `affect get --user <id>` / `affect decay --user <id> [--days N]` | 查看 / 惰性衰减用户感情 |
| `turn` | 两阶段回合；无 `--raw` 时只打印将发给 LLM 的 prompt |
| `maintain` | dormant 判定、死记忆移档、日记移档、全量原子重写、向量补算 |
| `diary list` | 列出日记（`--scope group\|dm`、`--limit`、`--json`） |
| `diary prompt` | 准备日记生成 prompt（`--scope` 必填、`--force`、`--json`） |
| `diary add --raw-file` | 落盘宿主生成的日记（prepare + finish，护栏全生效） |
| `metrics` | 结构化指标与最近事件 |
| `obsidian list/read/search` | Obsidian **只读**访问 |
| `import-legacy` | 旧 Obsidian 记忆库一次性导入（幂等 / 可 `--dry-run`） |
| `export-obsidian` | 导出 Obsidian 只读视图（单向镜像，幂等） |
| `serve` | 启动本地 HTTP JSON API |

通用参数：`-c/--config config.json`、`-d/--data-dir data`；`--version` 输出 `qihun-memory 0.3.0`
（对应规范 v1.6-rc4；CLI 帮助描述里写明 v1.6）。

## 架构

```text
QQ 消息 ─┐
         ├─ prepare：state 只读 → 闸门 → 轻路径/路由 → 检索 → 日记节选 → prompt(总预算 3850)
LLM 调用 ┘                                      │
                                                ▼
          finish：parse → clean_answer_for_send → emit → 摘要/最近轮次
                  → write_back：8a 感情步进 → 8b 写记忆 → 9a 强化/复活 → 9b affect 聚合 → 10 last_event_affect

日记 worker（独立、异步、不计入每轮 LLM 调用）：
  每日 23:50 / every_n → prepare(prompt) → 宿主 LLM → finish（隐私护栏 → 截断 → cap → 幂等 → 落盘）
```

两阶段接口对应设计文档 §9 的每轮流程，同时适配「LLM 调用由宿主发起」的现实：
宿主先 `POST /turn/prepare` 拿到 prompt，自己调模型，再把原始输出 `POST /turn/finish` 回来。
日记同理：`POST /diary/prepare` → 宿主调模型 → `POST /diary/finish`。
如果宿主愿意把模型调用交给本系统，也可以直接用 Python API
`TurnPipeline.run_turn(event, llm_call)`。

- 解析：`src/qihun_memory/parser.py`（`split_raw_candidate` → `parse_memories` → `parse_llm_output`；
  发送层唯一清洗点 `clean_answer_for_send`）
- 检索：`src/qihun_memory/retrieval.py`（候选池 → 桥接 → 打分 → `fill` 装填）
- 桥接触发：`src/qihun_memory/bridge.py`（无 I/O、无状态）
- 遗忘与强化：`src/qihun_memory/lifecycle.py`（`compute_S` 唯一入口）+ `memory.py`
- 用户感情：`src/qihun_memory/state.py`（`UserAffectStore` 唯一入口）
- 叙事层（v1.6）：`src/qihun_memory/diary.py`（`DiaryStore` / `DiaryWorker` /
  `select_excerpts` / `render_diary_block`）
- 存储：`src/qihun_memory/store.py`（JSONL 追加 / 坏行隔离 / 原子重写 / 向量 sidecar）
- 模块与规范逐节对照、实现决策与偏差：见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 接口契约（贡献者必读）：[docs/INTERFACES.md](docs/INTERFACES.md)

## HTTP API（给 qq-bridge 集成）

默认 `127.0.0.1:8765`，配置 `service.token` 时要求 `Authorization: Bearer <token>`。
Obsidian 相关端点只有读，没有任何写接口。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 / 版本（0.3.0）/ 记忆计数 / 日记计数 / embedding 可用性 |
| GET | `/state` | 只读的 bot 情绪快照（§3.2） |
| GET | `/user_affect?user_id=` | 只读的用户感情快照（v1.5，§3.3） |
| GET | `/metrics` | 指标计数与最近事件 |
| GET | `/memory/list` | 列出记忆（含 affect） |
| GET | `/memory/search?q=&scope=&user_id=` | 检索（不强化；返回 `bridge={hit,candidates,truncated}`、`dedup={removed,degraded}` 摘要） |
| POST | `/turn/prepare` | `{user_id, scope, message, msgid?, at_bot?, reply_to_bot?, nickname?}` → prompt + `injection`（system_prompt / memory_block / summary / diary_block / protocol / memory_ids）+ retrieval 摘要 |
| POST | `/turn/finish` | `{turn_id, raw}` → 回答正文 + 写记忆结果 |
| GET | `/diary/list?scope=&limit=` | 日记列表（created_at 倒序，含 counts）（v1.6） |
| GET | `/diary/excerpts?scope=` | 当前 scope 的窗口日记节选（injected / tokens / truncated）（v1.6） |
| POST | `/diary/prepare` | `{scope, force?}` → `diary_id` + 生成 prompt（v1.6） |
| POST | `/diary/finish` | `{diary_id, raw}` → 隐私/截断/cap/幂等后落盘（v1.6） |
| GET | `/obsidian/notes` | 列出只读库笔记 |
| GET | `/obsidian/note?path=` | 读取笔记（只读） |
| POST | `/users/refresh` | `{members: {user_id: nickname}}` 刷新昵称快照 |
| POST | `/maintain` | 每日维护（建议定时调用；启动后补偿执行一次） |

Node 侧示例见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 数据目录

```text
data/                       # 由 config.data_dir 指定，已在 .gitignore
├─ persona.md               # 恒定人格（只读使用）
├─ state.json               # bot 情绪（情感系统写，本系统只读）
├─ users.json               # 昵称快照（启动/接口刷新）
├─ memory.jsonl             # 全部记忆，一行一条，只追加（§3.1，含 affect）
├─ memory.vec.jsonl         # 向量 sidecar：float16 + base64（§7.5）
├─ memory.bad.jsonl         # 加载时无法解析的行，原样搬入（§3）
├─ memory.archive.jsonl     # 死记忆移档，不物理删除（§6）
├─ user_affect.json         # 用户感情聚合（v1.5，唯一入口 UserAffectStore）
├─ affect.bad.jsonl         # affect 非法的原始行（条目仍加载，§3）
├─ summary.json             # 滚动会话摘要（≤200 tokens，脚本裁剪）
├─ diary.jsonl              # 叙事层：情感日记，单行一篇，只追加（v1.6）
├─ diary.archive.jsonl      # 满 30 天的日记移档（v1.6）
├─ diary.bad.jsonl          # 日记坏行原样搬迁（v1.6）
└─ logs/
   ├─ metrics.jsonl         # 每事件一行结构化日志（§14）
   └─ raw-debug.jsonl       # 可选：metrics.debug_raw=true 时保留 7 天
```

## 配置

复制 [examples/config.example.json](examples/config.example.json) 为 `config.json` 后按需修改；
所有默认值即设计文档 §10 参数总表。v1.6 新增 `diary` 一节与 prompt 预算分项：

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
    "bot_nickname": "",
    "gen_model": ""
  }
}
```

常用项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `data_dir` | `./data` | 数据目录（相对配置文件目录解析） |
| `vault_dir` | 空 | Obsidian 库根目录，**仅只读** |
| `persona_path` | 空 | 空则 `data_dir/persona.md`，不存在时回退 `vault_dir/persona.md` |
| `decay.*` | §10 | S₀、cap 90、0.1/0.02 阈值、0.5/0.7/0.9 sim 门槛、k_v=0.5、k_a=0.3、λ=0.2 |
| `retrieval.*` | §10 | 1200 tokens 预算、header 24、0.6/0.4 权重、β=0.2、γ_u=0.15、η=0.1、affect_match=false |
| `affect.*` | §10 | 用户感情初值 / 步长 / 30 天 stale / 7 天衰减周期 |
| `bridge.*` | §10 | 桥接开关 / 0.7 权重 / 300 tokens 子预算 / 触发词与实体表 |
| `prompt.*` | §10 | 3850 总预算 + 各分项（摘要 200 / persona 500 / 最近轮 750 / 日记 300 / 协议 300 / 系统其余 600） |
| `diary.*` | §10 | 叙事层开关 / 触发（daily\|every_n\|both）/ 23:50 / 300 tokens / 注入 2 篇 / 30 天窗口 / 30 篇上限 / 隐私护栏 |
| `embedding.*` | bge-small-zh | 可选 `sentence-transformers`；不可用自动退化为纯 BM25 |
| `metrics.debug_raw` | false | 是否记录 LLM 原文前 500 字符（7 天清理） |
| `service.token` | 空 | HTTP 鉴权 token |

## v1.5-r2 → v1.6-rc4 升级说明

1. **数据兼容**：老 `memory.jsonl` / `user_affect.json` / `summary.json` 直接沿用，无需迁移脚本；
   新增 `diary.jsonl` 系列为空文件时自动创建（`init` 会建空 `diary.jsonl`）。
2. **配置新增**：老 `config.json` 可直接用；未出现的 `diary` 段与 `prompt` 预算分项走默认值。
3. **行为变化**（§0.1 显式登记，均为语义修正，不改变回答可用性）：
   - 新建记忆 `mention_count` 从 1 改为 0，仅去重命中 active 条目时 +1（E1）；
   - 去重只比对同 user + scope + type 的 active 条目；dormant 不再因去重复活或计数（E4）；
   - 检索强匹配复活优先：S = 裸 S₀，不再 `Inc(access_count)`、不叠 reinforce（E5）；
   - prompt 总预算 3550 → 3850（新增日记 300），超限先丢日记再丢检索再丢最近轮；
   - 每日维护追加日记 30 天移档与全量重写；启动后补偿执行一次。
4. **升级后建议**：跑一次 `maintain`（补向量 / 重写 / 日记移档）；观察 `/metrics` 中
   `diary.*` / `prompt.budget_over` / `mem_write.mention_fail` / `retrieval.degraded`。
5. **回滚**：v1.6 新字段 / 新文件 / 新接口都是增量；回滚到 v1.5-r2 只需回退代码与版本号，
   `diary.*` 配置与新文件被忽略（数据不丢，建议保留 `diary.jsonl` 系列以便再次升级）。
6. **已知滑期**：裸「群」强词的桥接过触发原计划 v1.6 收敛，按 rc4 §0.1 显式 **推迟到 v1.7**。

## 设计决策（对应设计规范附录 A）

- **B1 Python**（已拍板）：参考实现为 Python；宿主（Node/qq-bridge）通过 HTTP 适配层调用。
- **B2 事件级 affect**（已拍板）：允许 LLM 输出可选 `affect`；非法只丢字段，不丢记忆；`valence` 语义不变。
- **B5 embedding**（已拍板）：默认 `bge-small-zh-v1.5`（512 维、CPU、本地、无网络），可选依赖
  `pip install -e ".[embed]"`；不可用时自动降级为纯 BM25 并记 `retrieval.degraded`。
- **B7 日记 mixed**（已拍板）：不允许自动生成 mixed；v1.6 只验证「不自动生成 / 不落盘 / 不自动注入」，
  人工确认入口列入 v1.7。
- **B3/B4/B6 待拍板**：哨兵 nonce、物理删除、`/forget` 用户可见入口（见规范附录 A）。

## 测试

```bash
python -m pytest -q
```

回归四层（规范附录 C D4 / §12）：

- **L1** 解析器 + affect + 公式自检 + 日记单测：解析异常 P01~P17、公式 C01~C11、日记生成/注入边界。
- **L2** 状态单点：`update_memory` 白名单、`UserAffectStore` 四方法、`compute_S` 三支、`dedup_key`、
  绕单点 G01~G07。
- **L3** turn 集成：LLM / 时钟 / 磁盘 mock；写回顺序、ctx 冻结、pending 重放、日记接线。
- **L4** 组合 / 时序 / 迁移 / 降级 / 容量 / 对抗（X01~X04、B01~B07）+ 叙事层 D01~D12。
- 冻结用例索引：[docs/regression-rc1.md](docs/regression-rc1.md)。

## 文档

- 设计规范（当前权威）：[QiHun-Memory-System-v1.6-rc4.md](QiHun-Memory-System-v1.6-rc4.md)
- 工程契约：[docs/UPGRADE-CONTRACT-v1.6-rc4.md](docs/UPGRADE-CONTRACT-v1.6-rc4.md)
- 冻结回归索引：[docs/regression-rc1.md](docs/regression-rc1.md)
- 实现映射与决策：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 部署与接入：[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- 现网桥接与旧库导入契约：[docs/INTEGRATION.md](docs/INTEGRATION.md)
- 模块接口契约：[docs/INTERFACES.md](docs/INTERFACES.md)
- 历史规范：[QiHun-Memory-System-v1.5-r2.md](QiHun-Memory-System-v1.5-r2.md)、
  [QiHun-Memory-System-v1.4.md](QiHun-Memory-System-v1.4.md)
- 历史工程契约：[docs/UPGRADE-CONTRACT-v1.5-r2.md](docs/UPGRADE-CONTRACT-v1.5-r2.md)

## License

待定（见设计规范附录 A；在确定前不添加 LICENSE 文件）。
