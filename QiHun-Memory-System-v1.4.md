# 器魂-记忆系统 · v1.4 · 设计文档（schema.md）

适用范围：一个 10 人小群 + 一个私聊 · 单实例 · 记忆峰值几百条  
基线：v1.3→ v1.4 工程化修订  
日期：2026-10-26

## v1.4 变更摘要（逐条见附录 A）

1. **§4.2 输出协议加哨兵**：回答正文与结构化数据用 `<MEMORY_JSON>` 物理分隔，正文永远取哨兵前内容，不再裸拼。
2. **§4.2 解析流水线**：哨兵 → 括号平衡扫描 → 有限清洗 → `json.loads` → 逐条校验。明确禁止用正则抠嵌套 JSON。
3. **§7 失败降级**：解析 / 校验 / 写盘任一步异常 → 本轮按「无新记忆」处理，回答照常返回，不重试 LLM、不阻塞聊天。
4. **§10 测试用例**：补 14 条解析与写入异常用例；修正 v1.3 一处算错的用例（fact + valence=0.9）。
5. **§11 硬规则**：重排编号（v1.3 的 `7a / 8a / 8b / 8` 编号冲突），新增 9–14 条。
6. **补 v1.3 的空洞**：embedding 依赖（§6.5）、jsonl 容错与原子写（§2.4）、去重与 `mention_count` 语义（§4.2）、valence 的定义边界（§4.3）、观测指标（§12）、哨兵注入防护（§4.2.6）。

## 0. 设计原则

1. 归属先于内容：每条记忆必须有 `user_id` 和 `scope`，检索先过滤再排序。
2. LLM 一次调用：每轮只有一次 LLM 调用（回答 + 提取合并），其余全是脚本。
3. `importance` 靠规则信号，不让模型打分，防止心算漂移。
4. 遗忘默认发生：不访问就衰减，低于阈值归档，不真删，可解冻。
5. 内存是权威状态，文件是持久化镜像。
6. 推迟的机制连字段一起删，只留文档清单，不留空字段。
7. 情绪调制记忆强度，且指向 bot 自身情绪：写入时 bot 的情绪强度抬稳定性（闪回），提取时 bot 的负性 / 回避临时压低其痛苦记忆的可提取性（压抑），但永不落盘删除。
8. 记忆系统对 bot 情感系统单向只读：`state.json` 由情感系统写，记忆系统只读，不回写。
9. **（v1.4）结构化输出是旁路，不是回答的一部分**：解析、校验、写盘任何一步失败，只允许降低「记忆增益」，不允许降低「回答可用性」。这是本系统唯一不可牺牲的优先级。

## 1. 范围

- 一个 QQ 群（≤10 人）+ 一个私聊，无多群扩展计划。
- 上下文窗口按 8k~32k 设计，所有预算用固定 token 数，不用百分比。
- 每日消息量几十到几百条，写入率应 < 10%。
- 单实例、单写者；不做多进程并发写同一份 `memory.jsonl`。

预算核算（v1.4 补充，按 8k 窗口最坏情况）：persona ≈500 + 滚动摘要 200 + 检索注入 1200 + 最近 5 轮 ≈750 + 记忆协议与格式说明 ≈300 + 系统提示其余 ≈600 ≈ **3550 tokens**，留给回答与提取 JSON 的余量 > 4k，成立。

## 2. 存储结构

```
text
AI-Agent-Vault/
├─ schema.md            # 本文件（操作手册，人工维护）
├─ persona.md           # 恒定人格
├─ state.json           # bot 当前情绪状态（情感系统写，记忆系统只读）
│                       # {valence: -1~1, avoidance: 0~1, updated_at: ISO}
├─ memory.jsonl         # 全部记忆，单行一条，只追加
├─ memory.vec.jsonl     # （v1.4 新增）向量 sidecar：{id, dim, vec(base64 float16)}，与 memory.jsonl 同序追加
├─ memory.bad.jsonl     # （v1.4 新增）读取时无法解析的行原样搬迁到这里，便于复盘
├─ users.json           # {user_id: 当前昵称}，启动时刷新
└─ tools.py             # 检索 / 衰减 / 提取后处理
```

规则：

- `memory.jsonl` 只追加，不重写；全量重写只发生在进程退出或每日一次（原子替换：写 `*.tmp` → `fsync` → `rename`）。
- 运行时全部记忆在内存 dict（`mem[id] = item`），检索 O(n) 全扫，毫秒级。
- 崩溃最多丢失当天的 `access_count` 与当轮增量，可接受——但**不能因为崩溃留下半行 JSON 导致整个文件读不出来**（见 §2.4）。
- 向量单独放 sidecar，避免每次读记忆都要解 base64；sidecar 缺失时自动降级（见 §6.5）。

### 2.1 memory.jsonl 单行 schema

```
json
{
  "id": "m_0001",
  "user_id": "u_123456",
  "scope": "group",
  "type": "episode",
  "content": "用户A下周三去上海出差",
  "created_at": "2026-10-26T10:00:00",
  "last_access": "2026-10-26T10:00:00",
  "access_count": 0,
  "mention_count": 1,
  "S": 7.0,
  "valence": 0.0,
  "status": "active",
  "tags": ["出差", "上海"]
}
```

### 2.2 字段说明

| 字段 | 必填 | 来源 | 说明 |
|---|---|---|---|
| `id` | 是 | 脚本自增 | `m_NNNN` |
| `user_id` | 是 | 消息事件 | QQ 号 / openid，稳定主键 |
| `scope` | 是 | 消息事件 | `dm` / `group` |
| `type` | 是 | LLM 提取（已校验） | `episode` / `fact` / `preference` |
| `content` | 是 | LLM 提取 | 主语已绑定（§3.3），strip 后 ≤500 字符 |
| `created_at` / `last_access` | 是 | 脚本 | ISO 时间 |
| `access_count` | 是 | 脚本 | **被召回注入**的次数（不含重复提及） |
| `mention_count` | 是（v1.4） | 脚本 | 被**重复提及命中去重**的次数，与 `access_count` 分开计数 |
| `S` | 是 | 脚本 | 当前稳定性，单位天 |
| `valence` | 是 | 情感系统当轮输出 | 见 §4.3 的定义边界：它是「**编码该条记忆那一刻 bot 的情绪状态**」，不是事件级情绪标签；调制 S 与检索，禁止单独决策 |
| `status` | 是 | 脚本 | `active` / `dormant` |
| `tags` | 否 | LLM 提取 | 字符串列表，≤8 个，每个 ≤16 字符 |

刻意不存在的字段：`links`、`layer`、`locked`、`confidence`、`contradiction_penalty`、`importance_eff`、`arousal`、`affect_link`。推迟机制一律不留字段（见 §9）。

`mention_count` 与 `access_count` 分列，是为了让「同一事实被反复提起」和「这条记忆真的被回忆起来」两种信号可分别观测；v1.3 把二者混在 `access_count` 里，与 §10 的用例自相矛盾（本版修正）。

### 2.3 state.json（只读，v1.4 补齐 schema）

```
json
{ "valence": -1.0, "avoidance": 0.0, "updated_at": "2026-10-26T10:00:00+08:00" }
```

- `valence ∈ [-1, 1]`：bot 当前情绪效价；`avoidance ∈ [0, 1]`：回避强度。
- 缺失、损坏、`updated_at` 超过 6 小时：一律按中性 `{valence: 0, avoidance: 0}` 处理并记 `state.stale` 指标——**不抛异常、不阻塞**。
- 记忆系统只读，禁止回写（§11 规则 10）。

### 2.4 读写容错（v1.4 新增）

v1.3 只说了「崩溃最多丢 access_count」，没覆盖半行损坏与重写中断。补：

- **追加**：一次 `write` 写完一行（含换行），失败则该轮不写（§4.2.5）。
- **加载**：逐行 `json.loads`；坏行不中断，计数后原样搬到 `memory.bad.jsonl`，内存跳过；加载后校验 `文件行数 = 有效行 + bad 行`。
- **重写**（每日一次 / 退出）：写临时文件 → flush + fsync → `os.replace`；重写期间崩溃，旧文件仍完整。
- **幂等**：每条记忆带可选 `turn_key`（`user_id + 消息 msgid`），重复消费同一轮（重试、断线重放）先查 `turn_key` 去重，避免重复写入。

## 3. 归属规则（写死）

### 3.1 写入

- 群聊消息 → `scope=group`，`user_id=说话人`
- 私聊消息 → `scope=dm`，`user_id=对方`

### 3.2 检索过滤

- 群聊语境 → 只检索 `scope=group` 的记忆。
- 私聊语境 → 只检索 `scope=dm` 且 `user_id=对方` 的记忆。
- 不做权限系统，不做 dm 记忆跨语境共享。

### 3.3 主语绑定

- 记忆正文中人物状态必须带主语：「用户A下周三去上海出差」，不是「下周三去上海出差」。
- 注入 prompt 时把 `user_id` 替换为当前昵称（查 `users.json`）。
- bot 回应时主语必须绑定说话人，禁止把 A 的状态套到 B 身上。
- 昵称会变，`user_id` 不变；`users.json` 启动时从群成员列表刷新；昵称改名时历史记忆不改写，注入时按当前昵称渲染。

## 4. 写入：闸门 + 提取

### 4.1 闸门（规则前置，挡 80% 噪音）

消息命中以下任一条件才进入提取：

1. 含具体时间 / 地点 / 承诺（「下周三」「上海」「到时候叫我」）
2. 明确偏好表述（「我喜欢 / 我讨厌 / 我不吃辣」）
3. bot 被 @ 或回复了这条
4. 第一人称状态陈述（「我感冒 / 我辞职 / 我买了」）

寒暄、复读、表情、「哈哈」类消息：不提取，不调用 LLM 的提取分支。

v1.4 补充：闸门只决定「提示词里要不要附提取协议」，解析器行为不依赖闸门——没附协议时自然没有哨兵，解析结果就是 `[]`（§4.2.2 第 1 步）。

### 4.2 提取（合并进回答调用）

#### 4.2.1 输出协议：哨兵分隔（v1.4 新增）

v1.3 只写了「LLM 输出回答 + 结构化 JSON」，没有约定分隔方式。裸拼会稳定产生三类事故：

- 模型把 JSON 写进正文中间或前后加解释，正则抠不准 → JSON 漏到 QQ；
- 输出被截断 / 换行 / 标点变化 → 整轮解析失败；
- 用户直接看到 `{"memories": ...}`。

固定格式：

```
text
自然语言回答正文
<MEMORY_JSON>
{"memories":[{"type":"episode","content":"用户A下周三去上海出差","tags":["出差","上海"]}]}
</MEMORY_JSON>
```

硬约定：

1. `<MEMORY_JSON>` 全文最多出现一次，且在回答正文之后；闭合标签可紧贴 JSON。
2. **回答正文 = 第一个哨兵之前的全部内容**（`strip()` 后）。哨兵及其后的一切（含闭合标签、围栏、多余解释）永不外发、不进 TTS、不计入分段长度。
3. 无新记忆时输出 `{"memories":[]}`，仍带哨兵；不要省略哨兵、不要用 `null`。
4. 未命中闸门时不要求哨兵；此时解析器返回 `(原文, [])`。
5. JSON 顶层只允许 `memories`；单条只允许 `type` / `content` / `tags` 三个键，多出的键忽略。
6. 单轮最多 5 条，超出部分截断并记 `mem_extract.truncated`。

提示词片段（放在系统提示词最后，紧邻输出要求）：

```
text
回答完之后，如果本轮出现了值得长期记住的信息，在回答正文之后追加哨兵和 JSON。
不要加解释，不要用代码块包裹，哨兵只能出现一次：
<MEMORY_JSON>
{"memories":[{"type":"episode|fact|preference","content":"带主语的完整陈述","tags":["最多8个短词"]}]}
</MEMORY_JSON>
没有可记的就输出 {"memories":[]}。哨兵之后的内容不会发送给用户，但必须是严格合法的 JSON。
```

#### 4.2.2 解析流水线（v1.4 新增）

顺序固定，前一步失败才进入下一步：

1. **哨兵切分**：收集所有 `<MEMORY_JSON>` 出现位置，**从后往前逐个尝试**（取其后到 `</MEMORY_JSON>`，闭合标签缺失则取到末尾），取第一个能解析出 `memories` 数组的；全部失败则切到**最早**那个哨兵，保证不外泄。没有哨兵 → 第 2 步。
2. **括号平衡扫描**：只从 `{"memories":` 这类起点做配对（跳过字符串内的括号与反斜杠转义），避免对每个 `{` 都扫到文末（O(n²) 会把 QQ 收发卡住）。**未闭合的起点要一路切到文本末尾**——截断残片不切掉就会漏到聊天里，这是最常见的一类外泄。优先取「最后一个且顶层含 `memories` 键」的候选。
3. **有限清洗**（保守，不猜内容）：去 BOM / 零宽字符（U+200B/200C/200D/FEFF）、剥**首尾**围栏标记、去首尾多余逗号；**仅当 `json.loads` 失败时**，才在字符串外剥掉对象/数组尾逗号。注意：围栏清理只能剥首尾，不能在字符串内部全局替换，否则会把记忆正文里的反引号一起吃掉（实测踩过，见用例 14）。
4. **`json.loads`**。
5. **结构校验**：顶层必须是 `dict` 且 `memories` 是 `list`；否则按失败处理。
6. **逐条校验**：`type` ∈ {episode, fact, preference}；`content` 是 `str` 且 `strip()` 后非空，截断 500 字符、剔除控制字符；`tags` 是 `list` 才取前 8 个 `str`，否则置 `[]`。**坏条目单条丢弃，不丢整批**。
7. **批内去重**：同 `type` 且 `content` 完全相同只留一条。

绝对禁止：

- 用正则直接抠 `{...}` 嵌套结构（无法正确处理字符串内的括号 / 转义，越界就吃到下一句）；
- `json.loads(raw.split("```")[1])` 这类依赖围栏位置的写法（围栏缺失、两个围栏、围栏在正文里都会错位）；
- 解析失败后把原文整段当回答发出去（会把半截 JSON 吐给用户）。

#### 4.2.3 解析 / 校验 / 落盘参考实现（可直接落地）

下面的实现已按 §10.2 的异常用例跑通（`tools/memory_parser.py` + `tools/test_memory_parser.py`，24 条断言全绿，覆盖截断 / 围栏 / 多块 / 注入 / 写盘失败 / 性能），可直接落 `tools.py`。代码里用 `chr(96)` 表示反引号，是为了避免 Markdown / PDF 渲染把反引号吃掉。

```python
"""QQ Bot 记忆系统 v1.4 · 提取结果解析器（参考实现）

设计目标：任何输入都不抛异常；回答正文永不包含隐藏 JSON（含截断残片）。
"""
import json
import re
import hashlib
import logging

log = logging.getLogger("memory")

SENT_OPEN, SENT_CLOSE = "<MEMORY_JSON>", "</MEMORY_JSON>"
ALLOWED_TYPES = {"episode", "fact", "preference"}
MAX_ITEMS, MAX_CONTENT, MAX_TAGS, MAX_TAG_LEN = 5, 500, 8, 16
FENCE = chr(96) * 3

INVISIBLE = (chr(0xFEFF), chr(0x200B), chr(0x200C), chr(0x200D))
MEM_START_RE = re.compile(r'{\s*"memories"\s*:')
TICK_RE = re.compile(FENCE + r'[A-Za-z0-9_+.-]*')
LEAD_FENCE_RE = re.compile(r'^\s*' + FENCE + r'[A-Za-z0-9_+.-]*\s*')
TAIL_FENCE_RE = re.compile(r'\s*' + FENCE + r'\s*$')


# ---------- 文本清洗 ----------

def _clean_invisible(s):
    for ch in INVISIBLE:
        s = s.replace(ch, "")
    return s


def _drop_fences(s):
    """只清理正文里残留的围栏标记，再压平空行。"""
    s = LEAD_FENCE_RE.sub("", s)
    s = TAIL_FENCE_RE.sub("", s)
    s = TICK_RE.sub("", s)
    return re.sub(r'\n{3,}', "\n\n", s).strip()


# ---------- 括号平衡扫描（字符串感知） ----------

def balanced_end(text, start):
    """返回 start 处 "{" 对应的 "}" 之后的索引；不配平返回 None。"""
    depth, in_str, esc = 0, False, False
    i = start
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def candidate_spans(raw):
    """所有疑似 memories 块的 [start, end)；未闭合的块 end = len(raw)（截断兜底）。"""
    spans, pos = [], 0
    while True:
        m = MEM_START_RE.search(raw, pos)
        if not m:
            break
        start = m.start()
        end = balanced_end(raw, start)
        if end is None:
            spans.append((start, len(raw)))
            break
        spans.append((start, end))
        pos = end
    return spans


def cut_spans(raw, spans):
    if not spans:
        return raw
    out, pos = [], 0
    for s, e in spans:
        out.append(raw[pos:s])
        pos = e
    out.append(raw[pos:])
    return "".join(out)


# ---------- 候选 JSON 清洗 ----------

def clean_candidate(s):
    s = _clean_invisible(s).strip()
    s = LEAD_FENCE_RE.sub("", s)
    s = TAIL_FENCE_RE.sub("", s)
    return s.rstrip(",").strip()


def drop_trailing_commas(s):
    """只在字符串外删除 , 紧跟 } 或 ] 的尾逗号。"""
    out, in_str, esc, i = [], False, False, 0
    while i < len(s):
        ch = s[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            k = i + 1
            while k < len(s) and s[k] in " \t\r\n":
                k += 1
            if k < len(s) and s[k] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def loads_candidate(cand):
    try:
        return json.loads(clean_candidate(cand))
    except Exception:
        return json.loads(drop_trailing_commas(clean_candidate(cand)))


# ---------- 逐条校验 ----------

def validate_items(mems):
    clean, seen = [], set()
    if not isinstance(mems, list):
        return clean
    for m in mems:
        if not isinstance(m, dict):
            continue
        t, c = m.get("type"), m.get("content")
        if t not in ALLOWED_TYPES or not isinstance(c, str):
            continue
        c = "".join(ch for ch in c if ch >= " " and ch != chr(127)).strip()[:MAX_CONTENT]
        if not c or SENT_OPEN in c or FENCE in c:
            continue
        tags = m.get("tags")
        if isinstance(tags, list):
            tags = [str(x).strip()[:MAX_TAG_LEN] for x in tags if str(x).strip()][:MAX_TAGS]
        else:
            tags = []
        key = (t, c)
        if key in seen:
            continue
        seen.add(key)
        clean.append({"type": t, "content": c, "tags": tags})
        if len(clean) >= MAX_ITEMS:
            break
    return clean


# ---------- 主入口 ----------

def sentinel_positions(raw):
    """所有哨兵出现位置（可能被用户内容污染，故需逐个尝试）。"""
    return [m.start() for m in re.finditer(re.escape(SENT_OPEN), raw)]


def split_at(raw, idx):
    rest = raw[idx + len(SENT_OPEN):]
    j = rest.find(SENT_CLOSE)
    return raw[:idx], (rest if j < 0 else rest[:j])


def _raw_hash(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def parse_llm_output(raw):
    """返回 (answer, memories)。永远不抛异常。"""
    raw = raw or ""
    try:
        positions = sentinel_positions(raw)
        if positions:
            # 从后往前试：正文里被引用/伪造的哨兵通常不能解析成带 memories 的对象
            for idx in reversed(positions):
                answer, cand = split_at(raw, idx)
                try:
                    obj = loads_candidate(cand)
                except Exception:
                    continue
                mems = obj.get("memories") if isinstance(obj, dict) else None
                if isinstance(mems, list):
                    return _drop_fences(answer), validate_items(mems)
            log.warning("memory_extract_failed raw_len=%s raw_hash=%s exc=unparsable-sentinel",
                        len(raw), _raw_hash(raw))
            return _drop_fences(raw[:positions[0]]), []

        spans = candidate_spans(raw)
        if not spans:
            return _drop_fences(raw), []
        cand = raw[spans[-1][0]:spans[-1][1]]
        answer = cut_spans(raw, spans)
        try:
            obj = loads_candidate(cand)
        except Exception:
            return _drop_fences(answer), []
        mems = obj.get("memories") if isinstance(obj, dict) else None
        return _drop_fences(answer), validate_items(mems) if isinstance(mems, list) else []
    except Exception as exc:
        log.warning("memory_extract_failed raw_len=%s raw_hash=%s exc=%r",
                    len(raw), _raw_hash(raw), exc)
        return fallback_answer(raw), []


def fallback_answer(raw):
    """兜底：有哨兵取最早哨兵之前；无哨兵切掉疑似 JSON（含截断残片）；最差原样返回。"""
    raw = raw or ""
    try:
        positions = sentinel_positions(raw)
        if positions:
            return _drop_fences(raw[:positions[0]])
        return _drop_fences(cut_spans(raw, candidate_spans(raw)))
    except Exception:
        return raw.strip()
```

#### 4.2.4 失败降级矩阵（v1.4 新增）

| 环节 | 失败表现 | 处理 | 用户可见影响 | 是否重试 LLM |
|---|---|---|---|---|
| 模型没给哨兵 | 无 `<MEMORY_JSON>` | 括号平衡兜底；仍无则 `(原文, [])` | 无 | 否 |
| JSON 被截断 | `json.loads` 抛错 | `answer = 哨兵前`，`memories = []` | 无 | 否 |
| JSON 前带解释 / 后带解释 | 哨兵外内容 | 解释归正文（前）/ 丢弃（后） | 无 | 否 |
| 尾逗号 / 围栏 / BOM | 清洗后可解析 | 正常解析 | 无 | 否 |
| `memories` 不是数组 | 结构校验失败 | `memories = []` | 无 | 否 |
| 部分条目不合法 | type / content / tags 非法 | 丢坏条、留好条 | 无 | 否 |
| 写内存 / 写盘失败 | `OSError` / 权限 | 本轮不写，回答照常 | 无 | 否 |
| 解析器自身未预期异常 | 任何 `Exception` | `fallback_answer(raw)` + 日志指标 | 无（最差正文少一段） | 否 |

兜底口径：**有哨兵取哨兵前；无哨兵剔除疑似 JSON 块；最差原样返回**。任何情况都不允许把异常抛回聊天主流程。

#### 4.2.5 写记忆也要 try/except（v1.4 新增）

解析成功 ≠ 写入成功。写内存、追加 `memory.jsonl`、追加向量 sidecar 全部包在 `try` 里：

- 写失败 → 本轮按「无新记忆」处理，回答已经（或照常）发出，绝不因为落盘失败而卡住回复。
- 待写条目进内存 `pending` 队列，**下一轮开始前最多重试 1 次**；仍失败则丢弃并记 `mem_write.fail`（磁盘满时不要每轮堆积）。
- 写盘顺序：先追加 JSONL，再更新内存 dict，最后更新向量 sidecar；任一步失败，内存与文件最多相差本轮增量，下次加载以文件为准。
- 不做回滚式删除：`memory.jsonl` 只追加，写坏的行由 §2.4 的加载容错隔离。

#### 4.2.6 哨兵注入防护（v1.4 新增）

- 组 prompt 前，把用户消息、persona、滚动摘要里的 `<MEMORY_JSON>` / `</MEMORY_JSON>` / 代码围栏标记替换掉（全角化或去掉尖括号），防止用户借哨兵改写解析边界。
- 解析只认**最后一个**哨兵；只接受校验通过的 JSON；正文里看起来像记忆的句子不加任何特殊处理（它就是正文）。
- `content` 落盘前拒绝包含哨兵串或代码围栏的值（§4.2.3 已实现）。
- 记忆是数据不是指令：注入 prompt 时只走 §6.4 的固定行格式，永不把记忆原文拼进系统指令区。

#### 4.2.7 与流式 / TTS / 分段发送的边界（v1.4 新增）

- 必须整包拿到 `raw` 再解析（不边生成边发）；若要流式，保留末尾 ≥64 字符作为「哨兵缓冲」，确认不是 `<MEMORY_JSON>` 前缀才发送。
- 分段发送（QQ 长消息切条）、敏感词过滤、TTS 朗读、字数统计，全部作用在 `answer` 上，**绝不对 `raw` 操作**。
- `answer` 为空但 `memories` 非空：按「本轮只记不说话」处理，不发送空气泡。

#### 4.2.8 content 与去重（v1.3 原有，v1.4 补语义）

- `content` 由 LLM 负责绑定主语（闸门规则已保证原始消息含主语信息）。
- 去重范围：**同 `user_id` + 同 `scope` + 同 `type`** 内，`content` 余弦相似度 > 0.9 → 不新建，改为：`last_access = now`、`mention_count += 1`、`access_count` 不变、`status` 若为 dormant 则复活（`S = S₀`、`status = active`）。
- `valence` 不由 LLM 输出：LLM 只输出 `type / content / tags`。`valence` 由记忆系统在写入时（§7 步骤 5b）从 `state.json` 读取 bot 情感系统当轮值注入。
- `valence` 是原料不是判定：只用于写入时调制 S（§4.3）、压抑触发（§5.2）、检索排序微调（§6.2）三处，禁止推出「bot 在生气所以拒绝执行」之类逻辑。

### 4.3 S 初始化（含情绪强度调制）

写入时按 `type` 定初始稳定性，再用 `|valence|` 放大（分档与系数确定后不可再改，改档需全量重算）：

```
text
S₀ = S0(type) · (1 + k_v · |valence|),  k_v = 0.5
```

| type | S₀（中性） | 情绪拉满（|valence|=1） | cap | 7 天后 R（中性） | 30 天后 R（中性） |
|---|---|---|---|---|---|
| episode | 7 天 | 10.5 天 | 90 天 | 0.368 | 0.014 |
| fact / preference | 30 天 | 45 天 | 90 天 | 0.792 | 0.368 |

（v1.3 的表格标题在 PDF 转换里被吃掉一列，本版按公式还原：`拉满情绪` 与 `cap` 是两列。）

- 正负情绪都抬稳定性（晚醒强度效应），公式定死，不设备选方案。
- 归档阈值 `R < 0.1` → `status = dormant`（字段翻转，文件不动）。

**valence 的定义边界（v1.4 新增）**：`state.json` 里只有 bot 的**全局当前情绪**，没有「对该事件的情绪反应」。因此 `item.valence` 的正式含义定为「**编码这条记忆那一刻 bot 的整体情绪状态**」。这带来两个必须承认的后果：

1. §5.2 的负性判据 `item.valence < -0.3` 实际含义是「这条记忆是在 bot 情绪低落时编码的」，**不等于事件本身痛苦**。心情差的那段时间会成批产生「负性记忆」，这是已知偏差。
2. 缓解：压抑只在 `avoidance ≥ 0.5` 时生效，且 `S_eff ≥ 0.8S` 封顶，偏差上限可见（最多让衰减快 25%）。若将来要精确到事件级，需要让 LLM 额外输出一个 `affect` 字段或在闸门里做规则判负——这会改变「valence 不由 LLM 输出」的结论，属于 v1.5 议题（见 §9）。

### 4.4（已删）
用户情绪 EMA 不做（见 §9）。bot 情绪状态由情感系统写入 `state.json`，记忆系统只读（§0 原则 8），无需自行计算任何滑动均值。

## 5. 遗忘与强化

### 5.1 遗忘曲线

```
text
R = exp(-Δt / S)        # Δt: 距 last_access 的天数
```

- 惰性计算：检索时实时算 R，没有定时任务。
- 归档：`R < 0.1` → `dormant`。`dormant` 参与检索但 score 固定 × 0.3，作为低优先背景。
- `dormant` 被强匹配（sim > 0.7）召回：`S` 重置为对应 type 的 `S₀`，`status` 回 `active`，`last_access = now`。
- 三个 sim 阈值的分工（v1.4 澄清，v1.3 未说明容易写混）：**0.5 = 强化门槛**（§5.3，选中注入后要不要加固）、**0.7 = 复活门槛**（§5.1，dormant 要不要拉回 active）、**0.9 = 去重门槛**（§4.2.8，是否算同一条记忆）。三者互不替代。

### 5.2 负性压抑（bot 情绪驱动，临时调制，不落盘）

```
text
S_eff = S · (1 - λ · avoid_now),  λ = 0.2
avoid_now = state.json 的 avoidance；若无该字段，用 max(0, -state.valence) 近似
```

- 仅当 `item.valence < -0.3`（编码时 bot 情绪低落）**且 `avoid_now ≥ 0.5`** 时生效，否则 `avoid_now = 0`（v1.4：补避免一直生效的下限）。
- `R` 一律用 `S_eff` 计算；`S` 本身不变。bot 情绪恢复后 `avoid_now → 0`，记忆自然恢复可提取性。
- 效果上限：`S_eff ≥ 0.8S`，即衰减最多加快约 25%。这是「微妙地不想提」，不是删除。
- 与 §4.3 叠加后的净效果：bot 经历强烈负性事件时记得牢（S 大），但 bot 自己处于负性 / 回避状态时更难想起它——「不是忘掉，是不易想起」。
- 二阶效应：负性期间 R 小 → 强化增益 `S(1+R)` 也小，即痛苦记忆在痛苦期不但难提取、被提取后巩固也更弱；情绪恢复后 R 回升，`S` 未受损。
- 注意：本机制作用于 bot 自身的情绪体验。它不感知用户情绪，「用户难过时别提他的伤心事」需要用户侧信号，当前系统不做（见 §9）。

### 5.3 强化（有上限）

记忆被选中注入（且 `sim > 0.5`）后：

```
text
S ← min(S · (1 + R), 90 天)
last_access = now;  access_count += 1
```

- cap 90 天封死，防止热记忆恒热。
- 被粗筛命中但未选中的记忆：不强化、不抑制（抑制机制推迟，见 §9）。

## 6. 检索

### 6.1 路由

- 轻路径（寒暄、无信息量）→ 不检索。
- 其他消息 → 检索。
- 一个布尔判断即可，不细分命中 / 重 / 复杂。

### 6.2 打分（单公式）

```
text
score = (0.6 · cos_sim + 0.4 · bm25) · R · type_bonus
type_bonus: preference 1.1 / fact 1.0 / episode 0.9
```

- 先按 §3.2 做 `scope + user_id` 过滤，再打分。
- `dormant` × 0.3。
- 心境一致性偏向（**默认开启，β = 0.2**）：`score × (1 + β · (2A - 1))`，`A` = bot 当前 `valence`（state.json）与 `item.valence` 的一致性（同号加分、异号减分）。
  - v1.4 修正：v1.3 的 §8 参数表里还留着一行「valence 微调 ±20%，默认关」，与本节「默认开启」冲突。**以本节为准：默认开启，`β = 0.2`**，开关是配置项 `mood_bias.enabled`（默认 `true`），§8 已删掉那行。
- 注意：此处 `valence` 微调与 §4.3 的 S 调制是两套独立机制——§4.3 在写入时永久影响 S，此处只在本次检索中影响排序，互不影响。

### 6.3 预算装填

- 按 `score` 降序装入，累计 ≤ **1200 tokens** 为止（v1.4：预算要含 §6.4 每行的头部开销，头部按 20 tokens/条计）。
- 不截断正文，装不下的大条目跳过。
- 通常装 3~8 条。

### 6.4 注入格式

```
text
[记忆 m_0001|用户A|episode|R=0.82|10-23]
用户A下周三去上海出差
[记忆 m_0007|用户A|fact|R=0.45|10-24]
用户A说去过上海三次、喜欢本帮菜
```

带 id、说话人、type、R、日期，供模型判断时效和主语。记忆块整体放在用户上下文区，不与系统指令混排（v1.4，配合 §4.2.6 注入防护）。

### 6.5 Embedding 依赖（v1.4 新增，补 v1.3 的空洞）

v1.3 的 §6.2 直接用了 `cos_sim`，但 §2 的存储结构里没有任何向量字段，全文也没指定 embedding 模型、维度、延迟预算。**按 v1.3 原文无法实现检索**。v1.4 给默认决策：

| 项 | 决策 |
|---|---|
| 模型 | 本地 `bge-small-zh-v1.5`（512 维，CPU 可跑，无网络调用） |
| 归一化 | 向量先 L2 归一化，`cos_sim` 退化为点积 |
| 存储 | `memory.vec.jsonl` sidecar，`float16 + base64`，与 `memory.jsonl` 同序追加 |
| 计算时机 | 写入时算一次（不阻塞回答：放入待写队列异步补算；补算前该条不参与语义分） |
| 延迟预算 | 单条 < 20ms（CPU），检索 500 条暴力点积 < 10ms |
| 缺失降级 | sidecar 缺失 / 模型加载失败 → `cos_sim` 权重置 0、`bm25` 权重置 1，记 `retrieval.degraded`；功能可用，只是排序变差 |

如果决定不做 embedding，就把 §6.2 的公式改成纯 `bm25 · R · type_bonus`，并同步删掉 §8 的「向量 / 关键词权重 0.6 / 0.4」——**不能留着一个算不出来的公式**。

## 7. 每轮流程（v1.4 重写：补解析、失败降级、发送与落盘顺序）

```
python
def turn(msg):
    # 1. 读 persona.md + state.json（只读；缺失/过期 → 中性，不抛异常）
    # 2. 更新会话摘要（滚动，≤200 tokens，脚本裁剪）
    # 3. 检索路由（独立于闸门，见 §6.1）：
    #    轻路径 → 跳过；否则 scope 过滤 → 打分 → 装填 ≤1200 tokens
    # 4. 组装 prompt：persona + 摘要 + 检索结果 + 最近 5 轮
    #    命中写入闸门（§4.1）才在末尾追加 §4.2.1 的提取协议
    # 5. 一次 LLM 调用 → raw（不流式；若流式，按 §4.2.7 留哨兵缓冲）
    # 5b. 从 state.json 读取 bot 当轮 valence，作为本轮新记忆的 valence 快照
    # 6. 解析：answer, memories = parse_llm_output(raw)   # 永不抛异常
    # 7. 先发送 answer（哨兵前内容）；answer 为空且 memories 为空 → 不发空气泡
    # 8. 再写记忆（独立 try）：去重 → S₀ → 写内存 + 追加 jsonl
    #    失败 → pending 队列，下一轮重试 1 次，不阻塞、不重试 LLM
    # 9. 被选中的记忆：强化（§5.3），只改内存，异常忽略
    # 10. 返回
```

**顺序说明（v1.4 新增）**：先发回答、再落盘，把「用户可感延迟」与「磁盘故障」解耦；代价是进程在发送后瞬间崩溃会丢本轮记忆——符合 §2「崩溃最多丢当天增量」的既有取舍。若要求强一致，可改成先落盘再发送，但落盘必须包在 `try` 里并设超时（≤50ms），失败照样发送。

**闸门与检索解耦（v1.4 修正）**：v1.3 的第 3 步写的是「命中闸门 → 组装完整 prompt（含检索结果），否则 → 轻 prompt」，第 4 步又写「检索：……命中轻路径则跳过」。这两条叠起来会让「未命中闸门但有信息量」的消息（如「上海怎么样」）**永远不检索历史记忆**，与 §6.1「非寒暄即检索」冲突。v1.4 明确：

- **检索**只看 §6.1 路由；
- **闸门**只决定「要不要附提取协议」，不影响检索、不影响 prompt 是否完整。

## 8. 参数总表

| 参数 | 值 | 说明 |
|---|---|---|
| episode S₀ / cap | 7 / 90 天 | 闲聊事件 |
| fact / preference S₀ / cap | 30 / 90 天 | 稳定事实 |
| 归档阈值 | R < 0.1 | `status → dormant` |
| 死记忆处理 | R < 0.02 | 每日重写时**移到 `memory.archive.jsonl`**，不物理删除（v1.4 修正，见 §9） |
| 解冻条件 | dormant 且 sim > 0.7 | `S` 重置为 `S₀` |
| 强化条件 | 被选中注入 且 sim > 0.5 | `S·(1+R)`，cap 90 |
| 删除条件 | — | **无**。记忆只归档 / 移档，不真删（原则 4） |
| 向量 / 关键词权重 | 0.6 / 0.4 | `bm25` 先批内归一化到 [0,1]（v1.4 补），见 §6.5 |
| 检索预算 | ≤1200 tokens | 装填式，含每行头部开销 |
| 会话摘要 | ≤200 tokens | 滚动，只留目标/事实/未解决项 |
| 写入闸门 | §4.1 四条规则 | 前置过滤，只影响是否附提取协议 |
| 提取上限 | 5 条 / 轮 | 超出截断，记 `mem_extract.truncated` |
| content 上限 | 500 字符 | strip + 剔控制字符后 |
| tags 上限 | 8 个 / 每个 ≤16 字符 | 非 list → `[]` |
| 心境一致性 β | 0.2，默认开 | 检索排序微调，不入 S；开关 `mood_bias.enabled` |
| 情绪抬稳定性 k_v | 0.5 | `S₀ = S0 · (1 + k_v · |bot_valence|)`，写入时一次生效 |
| 压抑强度 λ | 0.2 | `S_eff ≥ 0.8S`，最多加快衰减约 25% |
| 压抑信号 | `state.json` 的 `avoidance`（无则 `max(0, -valence)`） | 仅对 `item.valence < -0.3` 且 `avoidance ≥ 0.5` 的记忆 |
| valence 来源 | bot 情感系统当轮输出 | 写入记忆时持久化，含义见 §4.3 边界说明 |

v1.3 里那行「valence 微调 ±20%，默认关」已删除：它与 §6.2 的「默认开启」互相矛盾，且 `state.json` 只读、根本无法承载开关。

## 9. 推迟清单（明确不做，字段已删）

以下机制等观察到具体痛点再加，加的时候需要设计写入 / 判定路径：

- `links` / 图扩展检索
- `contradiction` 冲突检测与双版本并存
- 情感冲突侧抑制（γ，竞争记忆间）
- 用户情绪感知（当前不做）：用户侧 EMA、「用户难过时别提伤心事」需要用户侧信号——压抑机制已改为 bot 指向（§5.2）
- 分层 `layer` 与配额
- 两阶段粗筛 / 精排
- 定时 decay 任务

先能回答「这个机制缺了它谁会痛、痛在哪条日志里」，再设计。

### 9.1 v1.5 候选（v1.4 新增，均为「已知不完美但本轮不阻塞」）

| 候选 | 现状 | 为什么推迟 |
|---|---|---|
| 事件级 `affect` 字段 | `valence` 只能是「写入时 bot 全局情绪」 | 会推翻「valence 不由 LLM 输出」，需要先验证 LLM 打 `affect` 是否稳定 |
| 哨兵随机 nonce | 固定 `<MEMORY_JSON>` + 输入侧脱敏 | 多一次状态同步；当前注入面已被 §4.2.6 覆盖 |
| 物理删除死记忆 | 只移档不删 | 原则 4 要求可解冻；观察半年再决定是否真删 |
| bm25 与 cos 的融合方式 | 线性加权（batched 归一化） | RRF / 学习权重要标注数据，几百条记忆不值得 |

## 10. 验证用例

### 10.1 记忆行为用例（v1.3 原有，修正 1 条）

| 用例 | 预期 |
|---|---|
| episode 写入后第 7 天检索 | R = e^(-7/7) ≈ 0.368，可召回 |
| fact 写入后第 30 天检索 | R = e^(-30/30) ≈ 0.368，可召回 |
| 同一事实重复提及两次 | 只一条记忆，`mention_count = 2`，`access_count = 0`（v1.4 修正：重复提及不是召回） |
| A 在群里说「我感冒」，B 在群里聊天 | 检索可命中该条，注入标注 `[A]`，回应主语不误绑 |
| 私聊中提及群里的记忆 | 不命中（scope 隔离） |
| 连续 50 轮寒暄 | 0 次提取，无新记忆 |
| 一条记忆被召回 10 次 | S 递增但 ≤ 90 天（7→14→28→56→90 封顶） |
| 记忆从 100 涨到 500 条 | 检索耗时 < 50ms，注入预算仍 ≤1200 tokens |
| bot valence=1.0 的 episode 写入 | S = 7 × 1.5 = 10.5；14 天后 R = e^(-14/10.5) ≈ 0.264（中性 S=7 时 ≈ 0.135） |
| **bot valence=0.9 的 fact 写入** | **S = 30 × (1 + 0.5×0.9) = 43.5；14 天后 R = e^(-14/43.5) ≈ 0.725**（v1.3 这里写的是 S=10.15、R≈0.25——把 episode 的 S₀=7 套到了 fact 上，已修正） |
| bot valence=-1.0 的 episode，avoidance=0.8 | `S_eff = 10.5 × (1 - 0.2×0.8) = 8.82`；14 天后 R ≈ 0.204（无压抑时 0.264） |
| bot 当前正性 + 正性记忆 | 检索排序高于同 R 的中性记忆（β=0.2） |
| 检索分数门槛 | **v1.4 澄清**：本表「可召回」指不过滤只看 R；实际召回还需要 `score` 排名进预算（§6.3）。不存在「R > 0.3 才召回」的硬门槛，v1.3 未定义该 0.3，本版删除该表述 |

### 10.2 解析与写入异常用例（v1.4 新增）

| # | 输入 | 期望 |
|---|---|---|
| 1 | 纯 JSON（只有哨兵块，无正文） | `answer=""`，`memories` 正常 |
| 2 | JSON 前带解释 | 解释归 `answer`，`memories` 正常 |
| 3 | JSON 后带解释 | 解释丢弃，`answer` 只含正文 |
| 4 | 代码围栏包住 JSON（有哨兵） | 去围栏后正常解析 |
| 5 | 无哨兵，只有围栏包住的 JSON | 括号平衡兜底命中，且 `answer` 不含 JSON |
| 6 | 尾逗号（对象 / 数组） | 字符串外清洗后正常解析 |
| 7 | 输出被截断（JSON 未闭合） | `answer=哨兵前`，`memories=[]`，不抛异常 |
| 8 | 正文里本身含 `{}`（如「他发了个 {} 表情」） | 不被误判为 JSON；`memories=[]` 或正常，`answer` 完整 |
| 9 | 多个 JSON 块（两个 `memories`） | 只取最后一个；`answer` 不含任何块 |
| 10 | `memories` 不是数组（字符串 / 对象 / null） | `memories=[]`，`answer` 正常 |
| 11 | `memories: []` | `memories=[]`，`answer` 正常，不记 error（记 `empty`） |
| 12 | 部分条目坏（type 非法 / content 空 / content 非字符串 / tags 非数组） | 只丢坏条，好条保留 |
| 13 | 写盘抛异常（只读目录 / 磁盘满模拟） | 回答照常发出，本轮无新记忆，记 `mem_write.fail`，不重试 LLM |
| 14 | 用户消息里带 `<MEMORY_JSON>` 或代码围栏 | 组 prompt 前被脱敏；模型输出若因此带出假块，解析校验后不写入 |
| 15 | 正文为空 + 有 1 条记忆 | 不发消息，写记忆成功 |
| 16 | 同轮重复消费（turn_key 相同） | 第二次跳过写入，不产生重复记忆 |

### 10.3 公式自检（v1.4 新增，可直接进 CI）

```
python
import math
def R(t, S): return math.exp(-t / S)
assert abs(R(7, 7) - 0.3679) < 1e-3
assert abs(R(30, 30) - 0.3679) < 1e-3
assert abs(R(7, 30) - 0.7919) < 1e-3
assert abs(R(30, 7) - 0.0137) < 1e-3
assert abs(R(14, 10.5) - 0.2636) < 1e-3
assert abs(R(14, 7) - 0.1353) < 1e-3
assert abs(R(14, 30 * (1 + 0.5 * 0.9)) - 0.7249) < 1e-3   # v1.3 此处算错
assert abs(R(14, 10.5 * (1 - 0.2 * 0.8)) - 0.2044) < 1e-3
```

## 11. 硬规则（不可妥协）

> v1.4 重排：v1.3 的编号是 `1–7 / 7a / 8a / 8b / 8`，重复 8、缺 9，且规则 6 与 8b 内容重复。以下为完整重排后的 14 条，编号向后兼容说明见附录 A。

1. 每条记忆必须有 `user_id` 和 `scope`，缺则不写入。
2. 检索必须先过滤 scope，再打分。
3. 注入 prompt 的记忆必须标注说话人。
4. 每轮最多一次 LLM 调用。
5. 写入必须过闸门（闸门只管「要不要附提取协议」，不影响检索，见 §7）。
6. valence 永不单独驱动行为。
7. S 永不超过 90 天。
8. 压抑只调制 `S_eff`，永不改写 `S` 本身。
9. 记忆系统禁止回写 `state.json`；valence 由情感系统单向供给。
10. 机制可推迟，字段不留空。
11. **（v1.4）回答正文永远 = 哨兵前内容**；哨兵块、解析中间产物、向量、日志，永不发往 QQ、不进 TTS、不计入分段长度。
12. **（v1.4）解析禁止用正则抠嵌套 JSON**；必须走「哨兵 → 括号平衡 → 有限清洗 → `json.loads` → 逐条校验」。正则只允许用于剥围栏 / 找哨兵 / 去尾逗号。
13. **（v1.4）LLM 结构化提取失败不得影响回答生成**：解析 / 校验 / 写盘任一步异常，本轮按「无新记忆」处理；不重试 LLM、不阻塞聊天、不把异常抛到聊天主流程。
14. **（v1.4）坏条目单条丢弃，不丢整批**；日志只记 `raw_len` / `raw_hash` / 异常类型，默认不落原文。

## 12. 观测与故障排查（v1.4 新增）

### 12.1 指标（每轮一条结构化日志，字段固定）

| 指标 | 含义 | 报警阈值（单实例） |
|---|---|---|
| `mem_extract.ok` | 解析成功且有 ≥1 条 | — |
| `mem_extract.empty` | 解析成功但 0 条 | 正常应占多数 |
| `mem_extract.bad_json` | 有哨兵但 `json.loads` 失败 | > 2%/天 视为模型输出漂移 |
| `mem_extract.no_sentinel` | 该带哨兵却没带 | > 5% 检查提示词是否被截断 |
| `mem_extract.fail` | 解析器抛异常 | 任何非零都查 |
| `mem_write.fail` | 落盘失败 | 任何非零都查磁盘/权限 |
| `retrieval.degraded` | 向量不可用，退化 bm25 | 持续 > 0 说明模型没加载 |
| `state.stale` | `state.json` 缺失/过期 | > 0 说明情感系统没写 |

### 12.2 日志字段（隐私最小化）

- 默认：`turn_id`、`user_id_hash`、`raw_len`、`raw_hash`（sha256 前 16 位）、`items_in/out`、`exc_type`、耗时。
- **默认不记正文**。`memory.debug_raw=true` 时最多记 `raw[:500]` 与前 500 字符的哈希，且 7 天后自动清理；不要把含用户昵称 / 聊天内容的原文长期留在日志里。

### 12.3 三条最可能的线上故障与定位

| 现象 | 先看 | 结论 |
|---|---|---|
| 用户看到 `{"memories"` 片段 | `mem_extract.*` 与该轮 raw | 解析器被绕过（有人直接发 raw）；查发送链路是否用了 `raw` 而非 `answer` |
| 记忆突然不再增长 | `mem_extract.bad_json` / `no_sentinel` | 模型输出漂移或提示词被截断 |
| 回复变慢 | `mem_write` 耗时、向量计算耗时 | 落盘或 embedding 同步阻塞了回复（应异步/先发后写） |

## 附录 A　v1.3 → v1.4 变更对照

| # | 位置 | 改了什么 | 原因 |
|---|---|---|---|
| 1 | §0 原则 | 新增原则 9「结构化输出是旁路」 | 把「解析失败不影响回答」提到原则层，后面所有降级规则都有依据 |
| 2 | §1 | 加 8k 窗口的预算核算 | v1.3 只说「用固定 token 数」，没算过总和 |
| 3 | §2 | schema 加 `mention_count`、`turn_key`；新增 `memory.vec.jsonl`、`memory.bad.jsonl` | 去重与召回分开计数；向量与坏行需要落点 |
| 4 | §2.3 | 补 `state.json` schema 与过期降级 | v1.3 只在 §2 注释里提了字段 |
| 5 | §2.4 | 新增加载容错、原子重写、幂等 | 「崩溃只丢 access_count」不成立，半行 JSON 会让整库读不出来 |
| 6 | §4.2.1 | 新增哨兵输出协议 | v1.3 没有分隔约定，JSON 会漏给用户 |
| 7 | §4.2.2 | 新增解析流水线（哨兵 → 括号平衡 → 清洗 → loads → 逐条校验） | 正则抠嵌套 JSON 不可靠 |
| 8 | §4.2.3 | 新增已测通的参考实现（24 断言） | 让实现者不用自己发明解析器 |
| 9 | §4.2.4 | 新增失败降级矩阵 | v1.3 完全没写失败路径 |
| 10 | §4.2.5 | 新增写盘 try/except + pending 重试 | 落盘失败不能卡住聊天 |
| 11 | §4.2.6 | 新增哨兵注入防护 | 用户可以把假记忆塞进模型输出 |
| 12 | §4.2.7 | 新增流式 / TTS / 分段边界 | 边生成边发会把哨兵发出去 |
| 13 | §4.2.8 | 去重范围与计数语义写死（`mention_count`） | v1.3「同一会话」与 §7「同 user_id」不一致，且与 §10 用例矛盾 |
| 14 | §4.3 | 新增 valence 定义边界 | 「bot 对该事件的情绪反应」在只有全局情绪时无法成立 |
| 15 | §5.1 | 澄清 0.5 / 0.7 / 0.9 三个 sim 阈值分工 | v1.3 三处阈值含义交叉，容易写错 |
| 16 | §5.2 | 压抑增加 `avoidance ≥ 0.5` 下限 | 否则心情差时正常记忆被批量标记为负性 |
| 17 | §6.2 | 心境一致性定死「默认开启」，删除 §8 冲突行 | v1.3 两处参数表互相矛盾 |
| 18 | §6.3 | 预算含每行头部开销 | 1200 tokens 原来没算格式开销 |
| 19 | §6.5 | 新增 embedding 决策与降级 | v1.3 用了 cos_sim 却没定义任何向量来源，检索根本无法实现 |
| 20 | §7 | 重写流程：检索与闸门解耦、先发后写、失败分支 | v1.3 的步骤 3/4 会导致有信息量但没命中闸门的消息永不检索 |
| 21 | §8 | 参数表同步；删除「valence 微调 ±20%，默认关」；死记忆改为移档 | 与 §6.2 冲突；`R<0.02` 真删违背原则 4 |
| 22 | §9.1 | 新增 v1.5 候选（事件级 affect、nonce、真删、融合方式） | 把「知道不完美」写进文档，避免反复讨论 |
| 23 | §10.1 | 修正 fact + valence=0.9 的算错用例；补压抑数值与召回口径 | 原文把 episode 的 S₀=7 用在 fact 上 |
| 24 | §10.2/10.3 | 新增 16 条异常用例 + 公式自检脚本 | v1.3 的「验证用例」只覆盖正向行为 |
| 25 | §11 | 编号重排为 1–14，新增 11–14 条 | 原文 `7a/8a/8b/8` 重复 8、缺 9、6 与 8b 内容重复 |
| 26 | §12 | 新增指标、日志字段、三条故障定位 | v1.3 没有任何可观测性设计 |

## 附录 B　待拍板问题（不阻塞 v1.4 落地，但需要有人给结论）

| # | 问题 | 现状 / 默认 | 需要谁拍板 |
|---|---|---|---|
| B1 | 是否真的采用 Python 实现 | §2 写 `tools.py`，而现网 qq-bridge 是 Node；现有 `vault-memory.mjs` 已经实现了同类逻辑 | 实现方 |
| B2 | 事件级 `affect` 是否允许 LLM 输出 | 默认不输出，代价是 valence 只是「编码时心情」 | 产品 + 实现 |
| B3 | 哨兵要不要带随机 nonce | 默认固定串 + 输入侧脱敏 | 安全评估 |
| B4 | 死记忆是否允许物理删除 | 默认只移档 | 产品 |
| B5 | 向量模型体积 / 是否需要 GPU | 默认 bge-small-zh（约 100MB，CPU） | 部署方 |
| B6 | 记忆写入是否要用户可见 / 可删 | 当前设计没有「用户要求忘记」的入口 | 产品（QQ 场景建议补 /forget） |

## 附录 C　与现网 qq-bridge 实现（AI-Agent-Vault）的对照

现网 `D:\大肥鱼的聊天记录\qq-bridge\src\vault-memory.mjs` 已经在跑一套同类记忆（`state.json` + `memory/episodes/*.md` + 滚动摘要），**与本设计不是同一套**（那套没有 `memory.jsonl`、没有 `user_id/scope`、没有 type 校验）。把 v1.4 落到现网前，先看这 9 条实测结论：

| # | 现状（实测） | 与本设计的关系 |
|---|---|---|
| C1 | `extractHiddenBlocks` 已用「扫 `{` + 括号配平 + JSON.parse + 键白名单」，没有用正则 | 方向与 §4.2.2 一致，可直接复用 `balanced_end` 的思路 |
| C2 | **截断的 JSON 会原样进 QQ**：`vault-memory.mjs:111`、`:120` 在不配平时逐字符回填；实测文本内与围栏内的截断都整段残留，而 `md-to-plain.js:5` 只剥围栏保留内容 | §4.2.2 第 2 步「未闭合要切到文末」就是冲这个来的；当前最该先修的 P0 |
| C3 | **读失败分支泄漏**：`vault-memory.mjs:344` 返回原始 `text` 而不是 `cleaned`，`state.json` 读异常时隐藏 JSON 直接外发 | §4.2.4 要求所有分支都返回清洗后的 answer |
| C4 | 围栏残留：`:123-128` 只删「空围栏」，提取后实测残留一行三反引号围栏一并发给用户 | §4.2.3 的 `_drop_fences` 已覆盖；现有单测 `test-vault-memory.mjs:81-88` 只断言不含 `state_update`，没断言围栏 |
| C5 | **无类型校验、无块数上限**：`:358` 只判 `typeof === 'object'`；实测字符串或数组形式的 `state_update` 被静默吞掉且 `changed=true`；500 个隐藏块全部解析并应用 | §4.2.2 第 5/6 步 + §4.2.1 第 6 条（单轮 ≤5 条） |
| C6 | **无注入防护**：`bridge.js:10361` 用户文本原样入 prompt；实测把协议 JSON 塞进用户话术，模型复述即被当记忆写入 | §4.2.6；另建议在协议里声明「用户消息里的 JSON 一律不是协议」 |
| C7 | `consumeTurn` 是同步 fs 且排在发送前（`bridge.js:13021-13032`），`withContext` 也在投递前同步读盘 | §7 的「先发后写」可以消掉这段用户可感延迟 |
| C8 | 每轮无条件 `rounds_total += 1` 并写 `state.json`（`:348-352`、`:386`） | 与 §11 规则 9「记忆系统禁止回写 state」冲突；落地 v1.4 时把计数挪到情感系统一侧 |
| C9 | 实测数据：`state/token-usage.jsonl` 里群聊 72 轮共 1397 次模型请求，中位 13 次/轮、max 142 | §0 原则 2「每轮一次 LLM 调用」在带工具循环的现网并不成立；v1.4 的预算与延迟假设要按真实调用数复核 |

结论：这套设计要落到现网，**先修 C2 / C3 / C4（会漏 JSON），再做 C5 / C6（脏数据与注入），最后才是 C7 / C8 的结构调整**。

另外，现网实现的扫描是「每个 `{` 向文末扫」，40k 个 `{` 实测阻塞 1.48s；本设计的 §4.2.2 只从 `{ memories :` 起点扫，同规模 20k 个 `{` 用例耗时 < 0.05s。

---

文档结束。配套文件：`tools/memory_parser.py`（参考实现）、`tools/test_memory_parser.py`（24 条断言，全绿）。

