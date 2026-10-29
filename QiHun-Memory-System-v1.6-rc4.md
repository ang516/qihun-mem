QQ-Bot 记忆系统设计文档 v1.6-rc4（完整冻结候选版 · 审阅修订）
适用范围：一个 QQ 群（≤10 人）+ 一个私聊 · 单实例 · 记忆峰值几百条
基线：v1.4 → v1.5 → v1.5-r2 → v1.6 → v1.6-rc1 → v1.6-rc2 → v1.6-rc2-frozen → v1.6-rc3 → v1.6-rc4（审阅修订：只修文字/公式/接口/引用一致性，机制与参数不变；语义登记见 §0.1）
日期：2026-10-29
状态：技术冻结候选；B1/B3/B5 为产品拍板项，不阻塞技术冻结；B7 已拍板。

0. 修订说明与变更摘要
0.1 相对 v1.5-r2 的显式语义登记（rc3 已定；回归矩阵须按此重算 A/B 类断言）
（1）复活计数：dormant 被检索强匹配（sim>0.7）复活时，S = compute_S("revive")（裸 S₀）、status = active、last_access = now，但不 Inc("access_count")（v1.5-r2 为复活当轮仍 access_count += 1）；同轮不叠 reinforce（E5）。
（2）去重复活路径：write_back 去重命中 dormant 条目不复活、不计 mention_count；dormant 只走检索路径复活（v1.5-r2 曾允许去重命中 dormant 即复活）。
（3）叙事层：v1.6 新增 diary.jsonl 与叙事层预算 300，8k 最坏预算固定量由 v1.5-r2 的 ≈3550 变为 3850（§2）。
（4）桥接方向口径：统一按"群聊池 → 私聊上下文"表述（v1.5-r2 写作"dm → group"，语义相同但箭头方向易误读，§7.6 已定死）。
注记：v1.5-r2 §6.2 原计划"裸'群'强词在 v1.6 收敛"，v1.6-rc4 仍保留裸"群"（v1.7 候选），本处记录滑期原因：避免在冻结候选版引入未回归的行为变更。

0.2 rc3 相对 rc2-frozen 的变更摘要
§3.1 示例 mention_count 改 0：新建条目为 0，仅去重命中 active 时 +1（E1）。
§7.6 桥接方向措辞定死："仅私聊场景可桥接群聊记忆；群聊场景禁止检索任何 dm 记忆"；数据流向明确为"群聊池 → 私聊上下文，反向禁止"（E2）。
§7.1 补轻路径与闸门关系：轻路径只决定是否检索，不决定是否提取；提取闸门独立判定；轻路径消息若命中 §5.1 闸门，仍附提取协议（E3）。
§6.3 补 active 去重命中后的落库动作：命中 active → 不插新条，只 Inc("mention_count",1) + Set("last_access", now)；不触发 compute_S、不强化 S、不写 JSONL 新行（E4）。
§6.1 补 revive 与 reinforce 优先级：复活优先；同轮 sim>0.7 且 dormant → 只 revive，不叠 reinforce（不 Inc("access_count")、不乘 (1+R)）；实现用 revived_this_turn 去重（E5）。
附录 E 保留 rc3 备忘：E1~E5 显式列出，作为实现期收口条目，不阻塞冻结。

硬规则 1~20 语义不变（编号见 §13）；事实层冻结基准除 §0.1 登记项外不变。

1. 设计原则
归属先于内容：每条记忆必须有 user_id 和 scope，检索先过滤再排序。
LLM 一次调用：每轮只有一次 LLM 调用（回答 + 提取合并）；日记生成走独立异步 worker，不计入每轮调用。
importance 靠规则信号，不让模型打分。
遗忘默认发生：不访问就衰减，低于阈值归档，不真删，可解冻。
内存是权威状态，文件是持久化镜像。
推迟的机制连字段一起删，只留文档清单，不留空字段。
情绪调制记忆强度，且指向 bot 自身情绪；永不落盘删除。
记忆系统对 bot 情感系统单向只读：state.json 由情感系统写，记忆系统只读。
结构化输出是旁路：解析、校验、写盘任一步失败，只允许降低"记忆增益"，不允许降低"回答可用性"。
感情只调制，不决策：item.valence、affect、user_affect 只用于稳定性调制、排序微调、压抑触发，永不单独驱动行为。
双轨不交叉：叙事层只提供人格语境，不参与事实层的检索、S/R 计算、scope 隔离与 per-user 记忆。

2. 范围与预算
一个 QQ 群（≤10 人）+ 一个私聊，无多群扩展计划。
上下文窗口按 8k~32k 设计，固定 token 数；单实例、单写者。
每日消息量几十到几百条，写入率 <10%。
8k 窗口最坏预算：
persona ≈500
事实摘要（滚动）200
叙事层（日记节选）300
检索注入 1200
最近 5 轮 ≈750
记忆协议与格式说明 ≈300
系统提示其余 ≈600
合计 3850 tokens（500+200+300+1200+750+300+600），8k 窗口下留给回答与提取 JSON 的余量 ≈4340 > 4k，成立。
预算实测：所有预算项在组装 prompt 前用同一 tokenizer 实测；总开销 ≤3850；超限按"日记节选 → 检索结果 → 最近 5 轮"顺序截断；仍超记 prompt.budget_over，按最小可用集组装（persona + 最近 1 轮 + 输出协议）。
容量承诺：仅单实例；不承诺多群横向扩展容量（附录 D R3）。

3. 存储结构
```text
<data_dir>/            # 由 config.data_dir 指定，不进 git；与 Obsidian 库（vault_dir）分离
  schema.md
  persona.md
  state.json           # bot 当前情绪状态（情感系统写，记忆系统只读）
  memory.jsonl         # 事实层：单行一条，只追加
  memory.vec.jsonl     # 向量 sidecar
  memory.bad.jsonl     # 坏行原样搬迁
  memory.archive.jsonl # R < 0.02 移档，不真删
  users.json           # {user_id: 当前昵称}
  user_affect.json     # per-user 感情聚合（唯一写入口 UserAffectStore）
  affect.bad.jsonl     # affect 非法原样搬迁
  summary.json         # 滚动事实摘要（≤200 tokens，原子写）
  diary.jsonl          # 叙事层：情感日记，单行一篇，只追加
  diary.archive.jsonl  # 满 30 天的日记移档
  diary.bad.jsonl      # 日记坏行原样搬迁
  logs/
    metrics.jsonl      # 指标落盘（每事件一行）
    raw-debug.jsonl    # 可选：memory.debug_raw=true 时保留，7 天清理
  tools.py
```
规则：
memory.jsonl 与 diary.jsonl 只追加；全量重写每日维护或退出时原子替换：写 *.tmp → fsync → os.replace。
每日维护：按 S 重算 R，把 status=dormant 落库；R<0.02 的条目移 memory.archive.jsonl（不真删）；先移档、再触发当日 diary 生成与 cap 检查。
加载容错：逐行 json.loads；坏行搬对应 .bad.jsonl，不中断。坏行判定以"整行"为单位；写入端一行一次 write，崩溃可能留下半行，半行按 §5.5 的 pending / 崩溃恢复流程处理。
时间：所有时间戳一律带时区偏移、禁止 naive；跨文件比较前统一换算到 UTC。示例用 +08:00，落盘允许任意合法偏移。diary.date 按本地时区（Asia/Shanghai）取日。

3.1 memory.jsonl schema（事实层）
```json
{
  "id": "m_0001",
  "user_id": "u_123456",
  "scope": "group",
  "type": "episode",
  "content": "用户A下周三去上海出差",
  "created_at": "2026-10-26T10:00:00+08:00",
  "last_access": "2026-10-26T10:00:00+08:00",
  "access_count": 0,
  "mention_count": 0,
  "S": 7.0,
  "valence": 0.0,
  "affect": {"label": "neutral", "intensity": 0.3},
  "status": "active",
  "tags": ["出差", "上海"],
  "turn_key": "u_123456:msgid_xxx"
}
```
type：枚举 episode / fact / preference（缺失或非法 → 丢弃该条，不写盘）。
status：枚举 active / dormant；R<0.1 时置 dormant，复活时置 active；其它值按坏行处理。
turn_key = f"{user_id}:{msgid}"（msgid 由宿主消息 id 提供；缺失为 None，该条不参与 pending 幂等）。
mention_count：新建条目为 0；仅当 write_back 去重命中 active 条目时 Inc(1)（E1）。
valence：编码时 bot 全局心情快照，不由 LLM 输出；只参与 compute_S("init")。
affect：事件级标签，枚举 joy/sadness/anger/anxiety/affection/disgust/neutral，强度 [0,1]；非法只丢字段，不丢整条。
access_count 只计召回注入；mention_count 只计重复提及去重命中。
刻意不存在：links、layer、locked、confidence、contradiction_penalty、importance_eff、arousal、dominance、多标签 affect。

3.2 state.json（只读）
```json
{"valence": -1.0, "avoidance": 0.0, "updated_at": "2026-10-26T10:00:00+08:00"}
```
valence ∈ [-1,1]：bot 当前情绪效价。
avoidance ∈ [0,1]：回避强度。
缺失、损坏、updated_at 距今超过 6 小时：按中性 {valence:0, avoidance:0} 处理，记 state.stale；禁止回写（硬规则 9）。
stale 联动：state.stale 与 user_affect.stale 独立判定，互不清零。state.json 过期会影响 S_init 的 valence 调制、压抑判据与心境一致性；user_affect 过期只影响检索偏向与感情聚合的初值基线；diary worker 读到 stale state 时按中性生成，不阻断日记生成。

3.3 user_affect.json
```json
{
  "u_123456": {
    "affection": 0.62,
    "annoyance": 0.12,
    "familiarity": 0.75,
    "updated_at": "2026-10-27T10:00:00+08:00"
  }
}
```
中性初值 {affection:0.5, annoyance:0.0, familiarity:0.3}。
updated_at 距今 ≥30 天 → stale，按中性处理且 familiarity 归零，记 user_affect.stale。
唯一读写入口：UserAffectStore（附录 C D1）。

3.4 diary.jsonl schema（叙事层）
```json
{
  "id": "d_0001",
  "date": "2026-10-29",
  "scope_tag": "group",
  "content": "今天群里有人聊到出差，我想起自己其实不太喜欢机场……",
  "tokens": 280,
  "turn_range": ["t_1021", "t_1087"],
  "created_at": "2026-10-29T23:50:00+08:00"
}
```
scope_tag：落盘只允许 group / dm；mixed 只作为内存临时上下文（§8.5），不写入 diary.jsonl。
刻意不存在：user_id、affect、S、R、access_count、status。日记不承载 per-user 记忆，不参与事实层计算。
active 定义：存在于 diary.jsonl 且 created_at ∈ (now-30d, now]；archive 条件：created_at ≤ now-30d，移出后不再注入、不参与 cap 计数。禁止为 diary 新增 status 字段（字段冻结，见本节"刻意不存在"与硬规则 10）。
生命周期：30 天硬窗口，窗口内同一 scope_tag 最多 30 篇；满 30 天移 diary.archive.jsonl。

3.5 维护任务（maintain()，独立 worker，不进入 turn()）
每日 03:00（Asia/Shanghai）执行一次，启动后补偿执行一次：按未压抑的 R 重算 status（R<0.1 → status=dormant，经 update_memory）；R<0.02 → 移 memory.archive.jsonl；diary 中 created_at ≤ now-30d → 移 diary.archive.jsonl；随后执行全量重写（*.tmp → fsync → os.replace）。维护任务任何失败只记指标（state.write_guard / mem_write.fail / diary.fail），不阻塞聊天。

4. 归属规则
群聊 → scope=group, user_id=说话人；私聊 → scope=dm, user_id=对方。
只从用户消息提取，bot 自身发言不写记忆（user_id 必须是真实用户）。
群聊检索候选池为混合池：先按 scope 过滤，不按 user_id 预过滤；rel 在池内区分归属。
私聊基础池为 scope=dm 且 user_id=对方；触发桥接（§7.6）时追加 scope=group 且 user_id=对方 且 status=active。
群聊禁止检索任何 dm 记忆。
主语绑定：记忆正文必须带主语；注入时 user_id 替换为当前昵称（查 users.json 当前快照）。
感情更新只在 item.user_id == 当前说话人 时触发；转述场景不跨用户传播。

5. 事实层：写入（闸门 + 提取）
5.1 闸门
命中任一才附提取协议：
① 具体时间/地点/承诺；
② 明确偏好；
③ bot 被 @ 或回复；
④ 第一人称状态陈述。
寒暄/复读/表情不提取。闸门不影响检索（硬规则 5）。
执行口径：闸门未命中时，即使模型漂移输出哨兵块，也按本轮无新记忆处理（不进入 8b），记 mem_extract.ungated；gate.enforce_write=false 时恢复宽松口径；gate.enabled=false 时永不附协议。

5.2 输出协议
```text
回答正文
<MEMORY_JSON>{"memories":[...]}</MEMORY_JSON>
```
哨兵最多一次，在正文后；正文 = 第一个哨兵前内容。
无记忆输出 {"memories":[]}。单条只允许 type/content/tags/affect 四个键，多余键忽略；最多 5 条。
affect 可省略；非法只丢 affect。type 缺失/非法 → 丢弃该条。

5.3 解析流水线
哨兵切分（正文截断点与 JSON 解析点相互独立）：正文截断点 = 最早哨兵前（cut_first）；JSON 解析点 = 从最后一个哨兵向前逐个尝试，取第一个能解析出 memories 数组的块；全部解析失败时 memories=[]，正文仍取最早哨兵前；多哨兵之间的文本一律丢弃，保证不外泄。cut_spans(raw, spans) = 剔除候选块区间后按原序拼接剩余文本。
括号平衡扫描：从 {"memories": 起点做配对，必须字符串感知（跳过字符串内的括号与反斜杠转义）；未闭合的起点切到文本末尾。
有限清洗：去 BOM/零宽、剥首尾围栏、字符串外去尾逗号。
json.loads。
结构校验：顶层 dict，memories 是 list。
normalize_item(m)：只做清洗/截断/affect 校验，不做去重。
dedup_within_batch(items)：按 dedup_key(item) 精确去重。
```python
TYPE_S0 = {"episode": 7.0, "fact": 30.0, "preference": 30.0}

def split_raw_candidate(raw) -> tuple[str, str | None]
def parse_memories(candidate_json) -> list[dict]
def parse_llm_output(raw) -> tuple[str, list[dict]]  # 只做组合，内部兜底、永不抛异常
```
dedup_key 规范化：Unicode NFKC、全角→半角、中文标点统一、删零宽/BOM/控制符、折叠空白、ASCII 大小写折叠、emoji 保留、不折叠重复字符、不截断。
dedup_key(item) = (item["type"].strip().lower(), normalize_text(item["content"]))（唯一去重键定义，附录 C D1）。
解析层不调用 _drop_fences，不抛异常；发送层 clean_answer_for_send(raw_answer) 单点清洗。

5.4 失败降级
无哨兵 → 括号平衡兜底；仍无 → (原文, [])。
无哨兵兜底边界：无哨兵但括号平衡捞出疑似 JSON 时，正文仍为"去掉该 JSON 块后的文本"，不是整段 raw；硬规则 11 已分别规定有哨兵/无哨兵两种正文口径；无哨兵时正文 = cut_spans(raw) 结果。两者都由 clean_answer_for_send 单点产出。
截断/坏 JSON → answer = 第一个哨兵前，memories=[]。
部分条目不合法 → 丢坏条，留好条。
affect 非法 → 只丢 affect。
写盘失败 → 本轮无新记忆，回答照常，记 mem_write.fail；不重试 LLM。
user_affect 失败 → 中性处理，不阻塞。
解析器未预期异常 → 解析入口内部兜底（parse_llm_output 永不抛异常）；发送层兜底；不把异常抛回聊天主流程。

5.5 写记忆与 pending 边界
写内存、追加 JSONL、追加 sidecar 均 try。
待写条目进 pending；pending 在下一轮 write_back 的 8b 开始时重放，最多重试 1 次；重放失败或成功都移出 pending（失败记 mem_write.fail，不再重试）。
pending 只存"新记忆写入"，带 turn_key；重放前查 turn_key，已存在跳过。
强化与感情更新不进 pending；失败直接丢弃并记指标。
写盘顺序：JSONL → 内存 dict → sidecar；不做跨文件事务。

5.6 注入防护
组 prompt 前替换用户消息、persona、摘要中的哨兵/围栏标记；解析优先采用最后一个可解析的哨兵块；正文一律取第一个哨兵前。
content 含哨兵串或围栏 → 丢弃该条；affect 含非法内容 → 只丢 affect；落盘前拒绝哨兵串或围栏。

5.7 S 初始化
```python
TYPE_S0 = {"episode": 7.0, "fact": 30.0, "preference": 30.0}

def compute_S(mode, type, valence=0.0, affect=None, S=None, R=None):
    S0 = TYPE_S0[type]
    if mode == "init":
        s = S0 * (1 + 0.5 * abs(valence))
        if affect and affect["label"] != "neutral":
            s *= (1 + 0.3 * affect["intensity"])
        return min(s, 90.0)
    if mode == "revive":
        return S0
    if mode == "reinforce":
        return min(S + S * (1 + R), 90.0)
    raise ValueError(mode)
```
k_v=0.5、k_a=0.3、cap=90。归档阈值 R<0.1 → dormant。
设计说明：episode 的 S0=7 天，强化公式 S + S·(1+R) 在 R≈0.5 时约 2.5 倍；约 3 次强化即到 90 天封顶，这是有意的"高频提及=长期重要"信号，不是遗忘设计缺陷；低频 episode 仍按 7 天自然衰减。fact/preference 同样受 90 天 cap 约束。
valence 边界：valence 只参与 compute_S("init") 的初始化调制，不参与 revive、不参与 reinforce；一次写入后，item.valence 不再改变 S。revive 裸 S0，reinforce 只看旧 S 与 R。state.stale 时 valence 按 0，等同中性初始化。

6. 事实层：遗忘与强化
6.1 遗忘曲线
R = exp(-dt / S)，dt = (now - last_access) 的天数（浮点，≥0）；R<0.1 → status=dormant（检索时懒计算并持久化，score ×0.3）；R<0.02 → 每日维护移档 memory.archive.jsonl。强匹配 sim>0.7 复活（compute_S("revive")）。三个阈值：0.5 强化、0.7 复活、0.9 去重。
last_access 在新建、强化、复活、以及去重命中（mention）时刷新为 now。
revive 与 reinforce 优先级（rc3 E5）：复活优先。同一条记忆在一轮内若 sim>0.7 且 status=dormant，只执行 revive（S = compute_S("revive")、status=active、last_access=now），不再执行 reinforce（不 Inc("access_count")、不乘 (1+R)）；实现用 revived_this_turn: set 去重（每轮开始时初始化为空 set，9a 写入，轮末丢弃），避免同轮重复复活或复活后叠强化。

6.2 负性压抑
S_eff = S · (1 - 0.2 · avoid_now)，其中 avoid_now = state.avoidance（字段缺失时取 max(0, -state.valence)；state.stale 时取 0）。
仅当 item.valence < -0.3 且 avoid_now ≥ 0.5 时生效，否则 S_eff = S；S_eff ≥ 0.8S；S 本身不变；affect 不参与压抑。
接线：R_eff = exp(-dt / S_eff) 用于 §7.2 打分与 §6.3 强化；dormant / archive 判定使用未压抑的 R = exp(-dt / S)，避免临时回避造成永久降级。

6.3 强化与感情更新
强化（9a，作用于本轮 injected_items 中 status=active 且 sim>0.5 的条目）：
```python
update_memory(id, [
    Set("S", compute_S("reinforce", type, S=old_S, R=R_eff)),
    Set("last_access", now),
    Inc("access_count", 1),
], reason="reinforce")
```
mention_count 更新条件（active-only，rc3 E4）：
去重范围：同 user_id + 同 scope + 同 type 的 active 条目（dedup_key 精确优先、向量可用时 sim>0.9 二次判定）；
命中 active → 不插入新条，只 Inc("mention_count",1) + Set("last_access", now)；不触发 compute_S，不强化 S，不写 JSONL 新行；
不进 pending；失败直接丢弃，记 mem_write.mention_fail；
批内去重、跨池去重不触发 mention_count（它们不是"重复提及"）；
dormant 不参与 mention 比对；dormant 复活只走检索路径（sim>0.7 → revive），不走 write_back 去重路径。
affect → 分量映射（定死）：
joy, affection → affection 分量，取本轮 max(intensity)；
anger, disgust → annoyance 分量，取本轮 max(intensity)；
sadness, anxiety → 不映射（trust 已删，v1.7 再议）；
neutral → 不映射。
一轮内每分量至多更新一次；同一分量多标签命中取 max，不累加。
感情更新（step update，经 UserAffectStore）：
统一更新式：x ← clamp(x + Δ·(1-x), 0, 1)；每轮每分量至多一次。
familiarity：每轮该 user 发送 ≥1 条消息即更新一次，Δ_f = 0.1。
affection/annoyance：只在 9b 被注入且 item.affect.label != neutral 且 item.user_id == current_user_id 时更新；Δ_aff = 0.1·I_joy、Δ_ann = 0.1·I_anger（I 为本轮该分量的 max intensity，为 0 的分量不传）。trust 已删除。

6.4 user_affect 衰减
days = (now - updated_at) / 86400；annoyance *= 0.9^floor(days/7)；familiarity 不设独立衰减；stale（≥30 天）整条中性且 familiarity=0。全部经 UserAffectStore。调用点：每轮 build_context 检索前对当前 user 调用一次 decay（幂等）。

7. 事实层：检索
7.1 路由、轻路径与候选池
轻路径判定（规则，零 LLM）：命中任一即走轻路径，不检索：
纯表情 / 纯标点 / 纯 emoji（无文字）；
消息长度 <4 字（按 Unicode 字符数，去首尾空白后）且不含问号、不含 @bot；
寒暄词表匹配（你好 / 在吗 / 早 / 晚安 / 哈哈 / 嗯 / 哦 / 好 等，配置项；按"去标点后整句完全相等"匹配，单字项不做子串匹配）；
复读：与当前会话最近一条用户消息（任意发送者）去标点后完全相同；
纯 @bot 无正文（仅提及，无问题）。
轻路径与闸门解耦（rc3 E3）：轻路径只决定是否检索，不决定是否提取；提取闸门独立判定。轻路径消息若命中 §5.1 闸门，仍附提取协议。闸门未命中 ≠ 轻路径（硬规则 5 不变）。例如"去上海"是 3 字、走轻路径不检索，但含具体地点，命中 §5.1 第 ① 条（具体时间/地点/承诺），仍附提取协议。
候选池：
群聊：混合池，先 scope 过滤，不按 user_id 预过滤。
私聊：dm 池 + 可选桥接池。私聊 rel 恒为 1，γ_u 不承诺改变排序。

7.2 打分
```text
base = 0.6·cos_sim + 0.4·bm25_norm           # bm25_norm 为 BM25 批内 min-max 归一值；cos 不可用时 base = bm25_norm
score = base · R_eff · type_bonus · dormant_factor · bridge_weight
score *= (1 + β·(2A-1))                      # β=0.2
type_bonus = base_bonus · (1 + η·rel·familiarity(current_user_id))
# base_bonus: episode 0.9 / fact 1.0 / preference 1.1
# η=0.1，仅当 item.user_id == current_user_id（rel=1）时生效；
# 混合池中 rel=0 的条目 η=0，不取条目归属者的 familiarity。
score *= (1 + γ_u·rel·(affection - annoyance)) # γ_u=0.15
# dormant_factor = 0.3 if status=dormant else 1；bridge_weight = 0.7 if 桥接条目 else 1
# affect_match 默认关：
# score *= (1 + γ_a·sim_affect(item.affect, last_event_affect))，γ_a=0.1
rel = 1 if item.user_id == current_user_id else 0。两路微调只调制排序，不单独决策。
排序确定性：score 降序 → r 降序 → id 升序（同分顺序可复现）。
sim 定义：cos_sim（向量可用时，L2 归一化后点积）；向量缺失降级时 sim = BM25 批内归一值（§7.5）。
心境一致性 A 的定义（定死）：
A = 1.0：sign(bot_valence) == sign(item.valence) 且两者均非零（同号）；
A = 0.0：sign(bot_valence) != sign(item.valence) 且两者均非零（异号）；
A = 0.5：任一方为 0 或缺失（中性）；
量程 [0,1]；bot_valence 取 state.json 当前值（stale 时按 0，即 A=0.5）；
item.valence 取记忆条目写入时快照值；缺失按 0（A=0.5）。
β=0.2；A=0.5 时该项乘子为 1.0，不改变排序。
familiarity 偏置声明：仅作用于 rel=1 条目，是稳定排序偏置，不是相关性证据；rel=1 相对 rel=0 的微调总增益上限为 1.265×（1.1×1.15），当 rel=0 条目 base 分高出 rel=1 条目超过 26.5% 时必须排在其前；固定样例断言见 §12.4 A27。
```

7.3 装填接口
```python
def fill(items, constraints):
    # constraints 由 Retriever 显式传入，fill 自身不做 1200/300 缺省兜底：
    # {"total": cfg.retrieval.budget_tokens, "head_per_item": cfg.retrieval.header_tokens,
    #  "subgroup": {"bridge": min(300, cfg.bridge.budget_tokens)}}
    # 返回 {"injected_items": [{**item, "r": float, "score": float, "affect": dict|None,
    #         "bridge": bool, "source_pool": "dm|group|bridge", "tokens": int, "header_tokens": int}],
    #       "truncated": bool, "total_tokens": int}
```
单条成本 = head_per_item + estimate_tokens(content)；装不下就跳过该条继续（不截断正文），记 retrieval.truncated。
输入 items 必须已按 §7.2 排序；fill 只装填、不重排。
删除 bridge_items 双源；9b 只消费 injected_items，不重新判断来源、不重新打分。桥接限额由 constraints.subgroup.bridge 传入。

7.4 注入格式
```text
[记忆 m_0001|用户A|episode|R=0.82|affect=anger:0.6|10-23]
用户A下周三去上海出差
[群聊记忆 m_0007|用户A|fact|R=0.45|10-24]
用户A说去过上海三次、喜欢本帮菜
```
行头 = [记忆 id|昵称|type|R=..|(affect=label:intensity)|MM-DD]；affect 缺失或 neutral 省略该段；header_tokens=24 是含 affect 段的固定行头预算。
桥接行头为 [群聊记忆 ...]，并在记忆块前附系统提示：「群聊记忆是用户本人在群里说过的话，本轮按私聊语境桥接参考，不是别人说的话」（约 40 tokens，计入系统提示其余 ≈600）。
记忆块放用户上下文区，不与系统指令混排。

7.5 Embedding
本地 BAAI/bge-small-zh-v1.5（512 维）；L2 归一化；sidecar float16+base64；写入异步补算；缺失降级：cos 权重置 0、bm25 权重置 1，sim = BM25 批内 min-max 归一值（max==min 时：全 0 → 0，同为正分含单候选 → 1.0），记 retrieval.degraded；去重同步降级为 dedup_key 原文匹配，记 dedup.degraded；空库不降级。

7.6 群聊记忆桥接
方向定死（rc3 E2）：仅私聊场景可桥接群聊记忆；群聊场景禁止检索任何 dm 记忆。数据流向：群聊池 → 私聊上下文，反向禁止。不做 dm → 群聊的桥接，也不做群聊里引用私聊内容。
单向：仅私聊检索时可引用同用户群聊记忆；群聊检索永不引用任何私聊记忆。
同用户：只桥接当前私聊对方自己的 active 群聊记忆；不桥接 dormant、他人。
显式触发：强词单命中（群里 / 群 / 刚才群里 / 群里说的 / 聊天记录 / 群聊）；弱词（刚才 / 刚刚 / 之前 / 上次 / 前面 / 说过 / 提到 / 聊过 / 记录）需 ≥2 个或弱词+实体指代（实体表只收时间/地点/具体事件名词）。已知边界：裸"群"是 v1.5-r2 已知过触发项，原计划 v1.6 收敛，本版沿用并显式推迟到 v1.7（§11）。
权重 0.7：在 §7.2 打分时对桥接条目乘 bridge_weight=0.7（打分后、跨池去重前生效）；单独限额 ≤300，计入 1200。
跨池去重：dedup_key 原文精确优先，向量可用时余弦>0.9 二次判定；向量不可用退化原文匹配，记 dedup.degraded；保留 score 高者、同分保留 dm。
桥接失败只降排序，不影响回答。
反向断层话术：群聊不引用私聊内容，用户追问时回应"私聊的事不方便在群里说"；只定态度，不做功能。
桥接偏置叠加声明：桥接条目 rel=1，与同分同用户 dm 条目共享同一 (1+η·familiarity) 与 γ_u 系数，仅多乘 0.7；因 0.7 × base_bonus 上界 0.7×1.1=0.77 < 0.9（episode 的 base_bonus 下界），同分条件下桥接恒弱于 dm 记忆。示例：fact 类型、familiarity=1、γ_u=1.15 时桥接净系数约 0.7×1.1×1.15≈0.89；同分 dm 净系数范围为 0.77~1.39（base_bonus 0.9~1.1 × η 因子 1.0~1.1 × γ_u 0.85~1.15）。若后续调大 η 或 γ_u，必须重评"桥接恒弱"是否仍成立；A27 回归需同步扩展断言。

8. 叙事层：情感日记
8.1 定位
叙事层只提供人格延续语境，不参与检索、不参与 score、不参与装填 1200 预算、不参与 S/R 计算、不参与事实层的 scope/user_id 过滤、不承载 per-user 记忆。日记注入只按 scope_tag 过滤（§8.4）。user_affect 仍只由事实层 injected_items 驱动。

8.2 生成
触发：
daily（默认）：每日一次；按 scope 分别生成，群聊一篇 group、私聊一篇 dm；不是全局一篇；默认本地时间 23:50（Asia/Shanghai，配置项 diary.daily_at）触发。
every_n（N=50）：按 scope 分别计数，群聊 turn 与私聊 turn 独立；达到 N 只触发对应 scope 的日记；turn_range 只记录该 scope 的 turn。
diary.trigger ∈ daily / every_n / both（默认 daily）；every_n 仅在触发值为 every_n 或 both 时生效。
输入：当段新增 memory.jsonl 条目（当段 = 自该 scope 上次成功日记的 created_at 之后；daily 取当日 00:00 起）+ state.json 当前快照 + user_affect.json 当前快照 + users.json 昵称快照（均只读；bot 自身昵称取 config.bot_nickname，用于隐私护栏）。不要求历史轨迹（避免依赖未持久化的历史序列）；当段无新增条目时 daily 仍生成、every_n 跳过并记 diary.skip。
Prompt 约束：
```text
你是 bot。回顾这段时间发生在{scope_tag}语境里的事和你的感受，用第一人称写一段话。
- 不超过 300 tokens（约 280 字）。
- 只写你自己的视角和感受，不要复述事实细节。
- 不要点名任何具体的人；群聊事件只写"群里有人提到…"，私聊事件只写"私下里…"。
- 只写本语境（{scope_tag}）内的事件，不要跨语境混合。
```
调用隔离：走独立异步 diary_worker，不计入每轮 LLM 调用次数，不阻塞聊天；失败不重试超过 1 次。
幂等：(date, scope_tag, turn_range) 去重；daily/every_n 共用；重复生成跳过，记 diary.dup。
隐私护栏（生成后落盘前）：
命中他人昵称 → 替换为"有人"，记 diary.privacy；
命中 bot 自身昵称 → 替换为"我"，不拒写；
命中 user_id 字面量或哨兵/围栏标记 → 拒写，记 diary.privacy；
脱敏后仍超 300 tokens → 截断，记 diary.truncated。
scope_tag 判定：只群聊 → group；只私聊 → dm；两者都有 → 拆成两篇，默认不自动生成 mixed。

8.3 存储与生命周期
30 天窗口定义：active = created_at ∈ (now-30d, now]；archive 条件：created_at ≤ now-30d。每日维护先移档，再做当日生成与 cap 检查。
窗口内上限：同一 scope_tag 的 active 日记 ≥30 篇时，新日记不写，记 diary.cap_hit；cap check 在落盘前执行；daily 与 every_n 共用同一 cap check。
daily 触发是"每个 scope 各一篇"，不是全局一篇；群聊一篇 group、私聊一篇 dm。
every_n 按 scope 分别计数，达到 N 只触发对应 scope 的日记。
幂等键：(date, scope_tag, turn_range)；daily/every_n 共用；重复生成跳过，记 diary.dup。turn_range = 该 scope 自上次成功日记（daily 则当日 00:00）以来已发生 turn 的首尾 id。
回退规则：注入时窗口内按 created_at 倒序取前 2 篇；不足 2 篇按实际取，不回退 archive，不记 error；archive 不参与注入，不定义 diary.archive_fallback。
v1.6 不生成月度回顾；月度汇总列 v1.7 候选。
不做 S/R 衰减，只按时间滑动窗口。

8.4 注入
位置：prompt 的"人格上下文"区，与事实摘要并列，不进检索结果区。
预算：≤300 tokens/轮，独立于检索 1200。装填规则：按 created_at 倒序逐篇装入（最多 2 篇）；第 1 篇必装，第 2 篇仅在剩余预算内装全文，放不下则不装（不截断正文），记 diary.truncated；不足 2 篇按实际篇数注入，只记 diary.inject 实际值。
scope 过滤：群聊语境只注 group；私聊语境只注 dm；mixed 默认不自动注入。不做跨 scope 桥接。
格式（群聊语境示例；私聊语境同理，只出现 dm 日记）：
```text
[日记 2026-10-29 | 群聊]
今天群里有人聊到出差，我想起自己其实不太喜欢机场……
```
前缀：以下是你的近期日记，用于保持人格连续性。日记只提供语境，不是事实依据；具体事实以记忆条目为准。

8.5 mixed 处理
mixed 只允许作为内存临时上下文存在，不写入 diary.jsonl、不参与 cap 计数与后续注入。
人工确认后仅以临时上下文注入当轮，不落盘、不参与后续注入。
v1.6 不提供人工确认入口与持久化字段（diary.mixed.confirmed 列入 v1.7 候选），因此 D10 用例推迟到 v1.7；v1.6 只回归"mixed 不自动生成、不落盘、不自动注入"。

8.6 失败降级
生成失败 / 读取失败 / 隐私命中 / 超预算 / 幂等命中 → 均静默降级，记对应指标，不影响回答可用性（硬规则 19）。

9. 每轮流程
```python
def turn(msg):
    ctx = build_context(msg)      # 1~5：读 persona/state/users/摘要/日记/检索，返回后只读冻结
    raw = call_llm(ctx)           # 6（仍只一次）
    raw_answer, memories = parse_llm_output(raw)   # 7 内部兜底，永不抛异常
    answer = clean_answer_for_send(raw_answer)     # 7b
    emit(answer)                  # 8
    update_summary(msg, answer)   # 9 独立 try/except，失败不影响回答
    write_back(ctx, memories)     # 10：pending 重放 → 8a → 8b → 9a → 9b → 10
    return

def build_context(msg):
    # 1. 读 persona.md + state.json + users.json（刷新昵称快照）；读并裁剪滚动摘要（≤200 tokens）
    # 2. 日记：按语境 scope 过滤，窗口内按 created_at 倒序取前 2 篇；
    #     不足 2 篇按实际取，不回退 archive，不记 error；装填 ≤300 tokens（§8.4）。
    # 3. 对当前 user 执行 user_affect.decay；检索（事实层，叙事层不参与）
    # 4. 组装 prompt：persona + 事实摘要 + 日记节选 + 检索结果 + 最近5轮
    # 预算实测：组装前用同一 tokenizer 实测；总 ≤3850；
    # 超限按"日记节选 → 检索结果 → 最近5轮"截断；仍超记 prompt.budget_over。
    # 5. 冻结 ctx，返回；turn_key = f"{user_id}:{msgid}"
    ...
```
write_back 步骤展开（顺序锁定，附录 C D5）：
8a：familiarity step update（每轮该 user 发送 ≥1 条消息即更新一次）；
8b：pending 重放（≤1 次）→ 新记忆写入（去重范围同 §6.3；命中 active 只 Inc("mention_count",1) + Set("last_access")，不插新条、不强化、不写新行；否则 compute_S("init") → 写内存 + JSONL + sidecar；失败进 pending）；
9a：强化/复活本轮 injected_items（只改内存、永不进 pending）：status=active 且 sim>0.5 → reinforce；status=dormant 且 sim>0.7 → revive（S=compute_S("revive")、status=active、last_access=now，记入 revived_this_turn，同轮不 reinforce、不 Inc access_count）；
9b：user_affect step update（仅 injected_items 中 affect 非 neutral 且 user_id 匹配的条目，每轮每分量至多一次）；
10：last_event_affect = 本轮新提取 memories 中第一条非 neutral affect（仅内存）+ 指标落盘。
ctx 冻结白名单：
只读：persona / state / summary / diary_excerpts / candidates / injected_items / users / user_id / scope / turn_key / gate_passed / light_path / last_event_affect_at_read
可写：last_event_affect / metrics / pending
越界写抛异常并记 ctx.violation。
顺序锁定：emit 早于 write_back；8a 早于 8b；9a 早于 9b；9b 在装填之后。日记生成与 §3.5 维护任务走独立 worker，均不阻塞 turn()。

10. 参数总表
参数	值
episode S0 / cap	7 / 90 天
fact/preference S0 / cap	30 / 90 天
type 枚举	episode / fact / preference（缺失/非法丢弃该条）
status 枚举	active / dormant（R<0.1 置 dormant；复活置 active）
归档 / 移档	R<0.1 dormant / R<0.02 每日维护移档
强化 / 复活 / 去重 sim	0.5 / 0.7 / 0.9（sim 定义：cos_sim；降级=BM25 归一值）
prompt 总预算	≤3850 tokens（8k 窗口；分项见 §2）
检索预算	≤1200 tokens，24 tokens/条头部
检索权重	cos 0.6 / bm25 0.4（bm25 批内 min-max 归一；cos 不可用时 base = bm25 归一值）
轻路径阈值	去首尾空白后字符数 <4 且不含问号、不含 @bot
revive 落库动作	Set S/status/last_access；不 Inc access_count，不叠 reinforce
提取上限	5 条/轮
content / tags	500 字符 / 8×16
k_v / k_a	0.5 / 0.3
压抑 λ	0.2，S_eff≥0.8S；avoid_now 缺失时取 max(0,-valence)，stale 取 0
β / γ_u / η / γ_a	0.2 / 0.15 / 0.1 / 0.1
base_bonus episode/fact/preference	0.9 / 1.0 / 1.1
type_bonus 公式	base_bonus · (1 + η·familiarity(current_user_id))，仅 rel=1
dormant / bridge 因子	dormant score ×0.3；桥接 score ×0.7（打分后、跨池去重前）
心境一致性 A	同号 1.0 / 异号 0.0 / 中性 0.5；β=0.2
user_affect 初值	{0.5,0,0.3}
stale	≥30 天，整条中性+familiarity=0
step 更新	x ← clamp(x + Δ·(1-x), 0, 1)；Δ_f=0.1；Δ_aff=0.1·I_joy；Δ_ann=0.1·I_anger；每轮每分量至多一次
annoyance 衰减	0.9^floor(days/7)，days = (now-updated_at)/86400
桥接 weight / budget / dedup_sim	0.7 / 300 / 0.9
affect 枚举	7 类；intensity [0,1]
neutral 注入	跳过
state.stale 阈值	updated_at 距今 >6 小时
diary.enabled	true
diary.trigger / every_n	daily（可 every_n/both）/ 50（按 scope 分别计数）
diary.daily_at	23:50（Asia/Shanghai）
diary.max_tokens	300（prompt 限 300 tokens，约 280 字）
diary.inject_budget / count	300 / 窗口内倒序前 2 篇，不足按实际；第 2 篇放不下则不装
diary.retain_days	30（硬窗口，同 scope_tag 最多 30 篇；created_at ≤ now-30d 移档）
diary.scope_tag	落盘 group / dm；mixed 仅内存临时上下文
diary.privacy_check	true
diary.gen_model	主模型或本地小模型（独立 worker）

11. 推迟清单
不做：links、contradiction、情感冲突侧抑制、用户情绪感知、分层、两阶段粗筛/精排、定时 decay、trust 分量、多标签 affect/arousal/dominance。
v1.7 候选：事件级多标签 affect、trust 接入、user_affect 可视化/可删、affect_match 默认开、bridgeable 标记、反向桥接话术、强词"群"收敛（v1.6 滑期）、物理删除死记忆、bm25/cos 融合方式、感情被检索频率绑架的归一化、Unicode 模糊测试、多群容量重评、ctx 冻结跨框架表达、月度回顾生成机制、mixed 人工确认字段与审核入口、reinforce 陡度可配置化。

12. 验证用例（回归矩阵 + 索引表）
12.0 索引表
类别	编号	层
事实层行为	F01~F15	L3/L4
解析异常	P01~P17	L1
公式自检	C01~C11	L1
affect/user_affect/桥接	A01~A27	L3/L4
组合/时序/迁移/降级	X01~X04	L4
绕开单点	G01~G07	L2/L4
数据/恢复/容量/对抗	B01~B07	L2/L4
叙事层	D01~D12	L3/L4

12.1 事实层行为（F01~F15）
遗忘曲线、重复提及、scope 隔离、S 封顶、valence/affect 调制、混合池 rel 区分；含 v1.5 两条 affect 调制断言。
12.2 解析异常（P01~P17）
P01~P16 同 rc1；P17：无哨兵，正文含 {"memories":[...]} 片段 → clean_answer_for_send 返回去掉 JSON 块后的正文，不整段保留 raw。
12.3 公式自检（C01~C11）
C01~C10 同 rc1；C11：compute_S("init", "episode", valence=+1.0) 与 valence=-1.0 的 S 相同（|valence| 对称）；valence=0 时 S=7；revive/reinforce 不受 valence 影响。
12.4 affect/user_affect/桥接（A01~A27）
A01~A26 同 rc1；A27（固定数值样例，消除"高/低 sim"歧义）：群聊混合池，同为 fact、R=1、valence=0（A=0.5）；rel=1 条目 sim=0.55、familiarity=1.0、affection=1.0、annoyance=0；rel=0 条目 sim=0.85。期望 rel=0 排在 rel=1 前（0.51 > 0.42）；若前者胜出视为回归失败。
12.5 组合与时序（X01~X04）
X01 桥接+去重+截断；X02 last_event_affect 跨轮+重启（期望：重启后 last_event_affect 为空，affect_match 自动失效、不报错）；X03 迁移；X04 降级链。
12.6 绕开单点（G01~G07）
G01 直接写 mem；G02 非法 patch；G03 Inc 顺序/原子性（单写者下重放不重复计数）；G04 直接写 json；G05 非法 mode；G06 发送层碰 memories；G07 fill 硬编码 bridge。
12.7 数据/恢复/容量/对抗（B01~B07）
B01 迁移；B02 pending 重放；B03 半写/崩溃；B04 跨文件一致性；B05 容量边界；B06 对抗输入；B07 降级链。
12.8 叙事层（D01~D12）
#	场景	期望
D01	日记生成失败	不写日记，记 diary.fail；回答正常
D02	私聊不注群日记	只注 dm 日记；group 不出现
D03	群聊不注私日记	只注 group 日记；dm 不出现
D04	他人昵称脱敏	替换为"有人"，记 diary.privacy
D05	日记+检索+桥接互不挤占	组装前同一 tokenizer 实测；日记 ≤300、检索（含桥接子预算 ≤300）≤1200、最近轮 ≤750；加 persona/摘要/协议/系统提示后总 ≤3850；超限按"日记→检索→最近轮"截断，记 prompt.budget_over
D06	幂等	同 (date, scope_tag, turn_range) 跳过，记 diary.dup
D07	满 30 天移档	created_at ≤ now-30d 移 diary.archive.jsonl，不再注入
D08	every_n 按 scope 分别计数	群聊 50 轮只触发 group 日记；私聊计数不受影响
D09	隐私护栏三策略	他人昵称替换为"有人"、bot 自身昵称替换为"我"、user_id 拒写
D10	mixed 临时注入	v1.6 无人工确认入口：只验证 mixed 不自动生成/不落盘/不自动注入；临时注入用例推迟到 v1.7
D11	同 scope_tag 窗口已有 30 篇 active，再触发 daily	不写新日记，记 diary.cap_hit；已有 30 篇不被移档
D12	窗口内只有 1 篇 dm，注入请求 2 篇	只注入 1 篇，不回退 archive；diary.inject=1，无 error
说明：P01~P16、C01~C10、A01~A26 的"同 rc1"判据以 v1.6-rc1 冻结用例表为准；冻结包必须随附该表（docs/regression-rc1.md），否则相关断言不可复核、不得宣称最小 CI 全绿。
最小 CI = L1 + L2 + L3 + L4 全绿，才算冻结通过。

13. 硬规则（1~20）
1. 每条记忆必须有 user_id 和 scope，缺则不写入。
2. 检索必须先过滤 scope；私聊允许追加同用户群聊记忆，但必须满足 §7.6 全部约束（显式触发、同用户、低权重 ≤0.7、单独限额 ≤300、注入标注、跨池去重、失败只降排序）；群聊禁止检索任何 dm 记忆。
3. 注入 prompt 的记忆必须标注说话人。
4. 每轮最多一次 LLM 调用（日记生成走独立异步 worker，不计入）。
5. 写入必须过闸门；闸门不影响检索。
6. valence 永不单独驱动行为。
7. S 永不超过 90 天。
8. 压抑只调制 S_eff，永不改写 S。
9. 记忆系统禁止回写 state.json；valence 由情感系统单向供给。
10. 机制可推迟，字段不留空。
11. 回答正文的产出：
（a）有哨兵时：正文 = 第一个哨兵前的全部内容（strip 后）；
（b）无哨兵时：正文 = 去掉疑似 JSON 块后的文本（cut_spans 结果），不整段保留 raw；
（c）两种情形均由 clean_answer_for_send 单点产出。
哨兵块、解析中间产物、向量、日志永不发往 QQ / 不进 TTS / 不计入分段长度。
12. 解析禁止正则抠嵌套 JSON；必须走哨兵→括号平衡（字符串感知）→有限清洗→json.loads→逐条校验。
13. 结构化提取失败不得影响回答：解析/校验/写盘任一步异常，本轮按无新记忆处理；不重试 LLM、不阻塞聊天。
14. 坏条目单条丢弃，不丢整批；日志只记 raw_len/raw_hash/异常类型，默认不落原文。
15. affect 只描述事件，不描述 bot 心情；item.valence 语义不变，两者禁止互相替代。
16. user_affect 只调制检索排序，永不单独驱动行为，不参与 S 调制。
17. affect/user_affect 任一步失败，只降记忆增益/排序质量，不降回答可用性；非法 affect 单条丢弃。
18. 群聊记忆桥接只允许单向、同用户、显式触发、低权重≤0.7、单独限额≤300（计入 1200）、注入标注、跨池去重；群聊检索 dm 一律禁止；桥接失败只降排序，不降回答可用性。
19. 叙事层只提供人格语境，不参与检索、不参与 S/R、不参与事实层的 scope/user_id 过滤、不承载 per-user 记忆；日记注入只按 scope_tag 过滤（规则 20）；日记生成/注入失败不影响回答；日记不得包含可识别他人信息。
20. 日记生成是离线/异步批处理，不计入每轮 LLM 调用；日记注入按 scope_tag 过滤，群聊只注 group、私聊只注 dm，不做跨 scope 桥接；mixed 默认不自动生成、不自动注入、不落盘。

14. 观测与故障排查
14.1 指标
指标	含义	告警
mem_extract.*	解析健康	按阈值
mem_extract.affect_invalid	affect 非法	>5%/天查提示词/模型
mem_extract.ungated	未过闸门仍解析出哨兵块	长期非零查提示词/闸门
mem_write.fail	落盘失败	非零查磁盘/权限
mem_write.mention_fail	mention_count 更新失败	非零查写入链路
retrieval.degraded	向量不可用	持续查模型加载
retrieval.pool.mixed	混合池候选数	确认生效
retrieval.truncated	装填因预算跳过条目	长期非零查预算/内容长度
retrieval.affect_bias	本轮 rel=1 且 affect_match 实际改变排序（affect_sim>0）的条数	—
state.stale	state.json 过期	查情感系统
user_affect.stale	user_affect 过期	informational
user_affect.fail	user_affect 读写失败	查磁盘/格式
group_bridge.*	桥接触发/命中/截断/异常；hit 记 count（最终注入条数）与 candidates（去重后进 fill 前候选数）	长期 0 查 user_id 语义
dedup.degraded	向量不可用去重退化	与 retrieval.degraded 同看
state.write_guard	拒绝非法 patch	查绕单点
ctx.violation	越界写 ctx	查重构
diary.ok/fail	生成成功/失败	fail 非零查 worker
diary.privacy	隐私护栏命中	非零查 prompt/脱敏
diary.truncated	超 300 tokens 截断或第 2 篇未装	频繁才预警（建议日均 >3 次）
diary.cap_hit	30 篇上限触发	观测；长期非零说明生成频率偏高
diary.dup	幂等命中	正常接近 0
diary.inject	本轮注入篇数	长期 0 查 trigger/scope
diary.tokens	本轮注入 tokens	仅观测，不作告警阈值
prompt.budget_over	组装前总开销超限	任何非零查 tokenizer 与截断顺序
diary.cap_hit 解读：主要由 every_n 与 daily 叠加触发；daily 单独运行稳态恰为 30 篇，不触发 cap_hit（每日维护先移档再生成）。长期非零应先查 every_n 频率，再查是否重复触发同一 scope。

14.2 日志
默认：turn_id / user_id_hash / raw_len / raw_hash / items_in/out / exc_type / 耗时；默认不落正文；memory.debug_raw=true 时最多 raw[:500] + 哈希，7 天清理。group_bridge.hit 额外记 has_other_entity: bool（查询串是否命中非当前用户的实体指代，只记布尔，不落原文/昵称）与 count/candidates。

14.3 故障定位
用户看到 {"memories" → 查发送链路是否用 raw 而非 answer。
记忆不增长 → 查 bad_json/no_sentinel。
回复变慢 → 查 mem_write / 向量计算 / 日记 worker 是否同步阻塞。
桥接长期 0 → 查词表、写入、群聊记忆 user_id 是否等于发言者。
日记长期 0 → 查 diary.trigger、worker 状态、scope_tag 过滤。
附注：episode 在 R<0.02 时移档，对应 dt ≈ 7·ln50 ≈ 27.4 天（fact 约 117 天）。低 episode 记忆 27 天即移档，与 diary 30 天窗口几乎同周期；运维可将 memory.archive.jsonl 与 diary.archive.jsonl 一起巡检。

附录 A：待拍板（不阻塞冻结）
#	问题	默认	拍板
B1	affect 枚举是否够用	7 类	产品
B2	affect_match 是否默认开	默认关	实现方
B3	user_affect 是否用户可见/可删	不可见	产品
B4	user_affect 写者归属	记忆系统	架构
B5	是否允许多标签 affect	不允许	产品+实现
B6	affect 与 item.valence 冲突谁优先	各自独立	实现方
B7	日记是否允许 mixed 自动生成	不允许	产品（已拍板）

附录 B：与现网 qq-bridge 对照（修复序列 Q2~Q8）
Q1 为 v1.5-r2 已完成的解析器原子化前置项，本序列从 Q2 起。先修 Q2（截断 JSON 漏出）→ Q3（读失败分支泄漏）→ Q4（围栏残留）→ Q5（无类型校验/块数上限）→ Q6（无注入防护）→ Q7（同步 fs 阻塞发送）→ Q8（全量扫描性能）。桥接层与叙事层可在 Q 系列修完后单独合入。

附录 C：模块边界与回归约定（冻结级工程约定）
D1 四个状态单点：
- update_memory(id, ops, reason)：已存在记忆的唯一变更入口；新记忆插入经 remember(item)（与 update_memory 同属存储单点，不得绕过）；Set 只允许 S/status/last_access，Inc 只允许 access_count/mention_count；Inc("mention_count",1) 仅在 write_back 去重命中 active 条目时调用；命中 active 不插新条、不强化、不写新行；dormant 不参与去重命中，复活只走检索；不进 pending，失败记 mem_write.mention_fail。
- UserAffectStore：唯一 user_affect 读写入口。
- compute_S(mode, ...)：唯一 S 计算入口。
- dedup_key(item)：唯一去重键定义。
D2 解析与发送分离：split_raw_candidate / parse_memories / parse_llm_output 三层；发送层 clean_answer_for_send 单点清洗。
D3 装填接口：fill(items, constraints) → {injected_items, truncated, total_tokens}；删除 bridge_items 双源；fill 不做预算缺省兜底。
D4 回归套件四层：L1 解析器单测；L2 状态单测；L3 turn 集成；L4 组合场景/负面/叙事。最小 CI 全绿才算冻结通过。
D5 顺序锁定：emit 早于 write_back；8a 早于 8b；9a 早于 9b；9b 在装填之后；write_back 内部顺序为 pending 重放（8b 内）→ 8a → 8b → 9a → 9b → 10。
D6 不变量 I1~I3：
- I1. 轮级 user_affect 只能由 write_back 经 UserAffectStore.step_update 修改；检索层不得用 memories[].affect 推导轮级感情（affect_match 排序微调除外，且默认关闭）。
- I2. item.affect 不直接参与 user_affect 聚合；只通过本轮注入的 injected_items 在 9b 间接参与，并用于 compute_S("init") 与注入展示。
- I3. 9b 聚合只用本轮 injected_items 的 affect，且 item.user_id == current_user_id。
D7 叙事层边界：
- D7.1 叙事层不参与检索打分、装填、S/R、去重键、user_affect 聚合。
- D7.2 日记注入只读 ctx.diary_excerpts；write_back 不得修改日记。
- D7.3 日记生成走独立 worker，不调用 turn()，不共享 mem dict 写权限。
- D7.4 隐私护栏在生成后、落盘前执行；落盘后不可再注入含昵称内容。
- D7.5 mixed 不进入 diary.jsonl 正常生命周期；v1.6 不提供人工确认入口（v1.7 再定义持久化确认字段与审核入口）。

附录 D：残余风险披露（accepted residual risks）
R1、R4、R5 已由 CI 架构测试或护栏守护；R2 有金样+property 守护；R3 为承诺边界。均不影响冻结。
R1. ctx 冻结依赖实现约束（有守护）：frozen dataclass + 白名单，架构测试守护，越界记 ctx.violation；换框架需重评。
R2. normalize_text 金样覆盖有限（能力边界，有 property 守护）：已知字符类金样全绿；property 1 幂等；property 2 NFC/NFKC 归一后键稳定；不承诺覆盖全部 Unicode。
R3. 容量承诺仅单实例（承诺边界）：不承诺多群横向扩展容量；签字人知悉。
R4. affect_match 与 user_affect 语义分离已声明，形式化证明 postponed（有守护）：架构检查禁止检索层用 memories[].affect 推导轮级感情；affect_match 的排序微调为显式豁免（默认关）；用例 X02 验证重启失效；形式化推迟 v1.7。
R5. 叙事层幻觉与情绪漂移（accepted residual risk）：日记由 LLM 生成，可能虚构/夸大/混淆；靠 scope_tag 三值护栏 + 隐私检查 + 30 天移档 + 定期人工抽检（每周 3 篇）守护；不做自动化事实一致性断言；日记只提供人格语境，事实以 memory.jsonl 为准。

附录 E：rc3 备忘（5 处文字级收口，不阻塞冻结）
```text
E1. §3.1 示例 mention_count 改 0；新建条目为 0，仅去重命中 active 时 +1。
E2. §7.6 桥接方向措辞改为"仅私聊场景可桥接群聊记忆；群聊禁止检索 dm"。
E3. §7.1 补：轻路径只决定是否检索，不决定是否提取；闸门独立判定；
    轻路径消息若命中 §5.1 闸门，仍附提取协议。
E4. §6.3 补：命中 active → 不插新条，只 Inc(mention_count) + Set(last_access)；
    不触发 compute_S、不强化 S、不写 JSONL 新行。
E5. §6.1 补：revive 优先于 reinforce；同轮 sim>0.7 且 dormant → 只 revive，
    不叠 reinforce（不 Inc access_count、不乘 (1+R)）；实现用 revived_this_turn 去重。
```

附录 F：rc4 审阅修订清单（相对 rc3，仅列修正点）
1. §7.6 删除"单向：私聊 → 群聊可桥接"反向残留句，统一方向口径。
2. §7.1 轻路径示例改为"去上海"（3 字，<4 字规则成立；含地点命中闸门）。
3. §6.1 补 dt 定义（now - last_access，天）；R<0.1/R<0.02 的落库与每日维护移档写死。
4. §6.2 补 avoid_now 定义与 S_eff 消费接线（R_eff 用于打分/强化；dormant/archive 用未压抑 R）。
5. §6.3 补 affection/annoyance 的 Δ 公式与 step_update 统一更新式；补去重范围（同 user+scope+type）。
6. §5.3 补 dedup_key 组成、字符串感知括号扫描、哨兵切分（正文=最早哨兵前 / 解析=最后一个可解析块，相互独立）、cut_spans 定义；修正类型注解。
7. §5.4 修正对硬规则 11 的引用；§5.5 补 pending 重放时机与次数。
8. §5.1 补闸门执行口径（enforce_write / mem_extract.ungated）。
9. §9 补 9a（强化/复活）步骤、pending 重放、turn_key/scope/users 入 ctx、摘要更新移到 emit 后。
10. §7.3 fill 返回字段补 r/score/affect/header_tokens，source_pool 补 group；不做预算缺省兜底。
11. §7.2 打分公式补 dormant×0.3 与桥接×0.7，补确定性排序与 sim 定义。
12. §7.4 行头补空格，补桥接注入行头与系统提示原文/预算。
13. §7.5 补降级模式 sim/权重定义。
14. §8.2 统一日记字数/ token 单位，prompt 按 scope 化，隐私替换措辞明确；补触发时刻与当段定义。
15. §8.3/§3.4 30 天窗口改半开区间（(now-30d, now]），archive ≤ now-30d，消除 cap 边界矛盾。
16. §8.4 注入预算补逐篇装填规则；示例按 scope 分开。
17. §8.5 明确 mixed 不落盘、v1.6 无确认入口；D10 移入 v1.7。
18. §12.4 A27 给出固定数值样例；§12.5 X02 补期望；D05/D07/D10 同步修正。
19. §13 补显式编号 1~20，修正规则 19 的 scope 过滤措辞。
20. §14.1 修正 affect_bias 定义、补 ungated/retrieval.truncated、修 cap_hit 解读；§14.2 补 count/candidates 与 has_other_entity 定义。
21. §3 存储树补 summary.json / logs/，根目录改为 data_dir；补时间戳带偏移规则。
22. §0.1 增补相对 v1.5-r2 的语义登记（复活计数、去重复活路径、叙事层预算、桥接口径）。
23. §3.1 补 type/status 枚举与 turn_key 规则；§3.2 修正 stale 影响范围表述。
24. 新增 §3.5 维护任务 maintain()（每日 + 启动补偿；dormant/移档/日记移档/全量重写）。
25. §10 补 prompt 总预算、检索权重、轻路径阈值、revive 落库动作；§12 说明 rc1 用例表随冻结包发布。

文档结束。 配套（另行交付）：tools/memory_parser.py（事实层参考实现）、tools/diary_worker.py（叙事层 worker）、tools/test_memory_parser.py（L1 + 局部 L2 用例与断言）。
