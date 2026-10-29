"""写入闸门与检索轻路径（v1.6-rc4 §5.1 / §7.1）。

闸门只决定「要不要在提示词末尾附提取协议」（§5.1），不影响检索；
检索路由（轻路径）与闸门完全解耦（§7.1 / E3）：轻路径只决定是否检索，
不决定是否提取；轻路径消息若命中 §5.1 闸门，仍附提取协议（硬规则 5）。
"""

from __future__ import annotations

import re
import unicodedata

from .config import GateConfig

# §4.1 规则 1：具体时间 / 地点 / 承诺
TIME_RE = re.compile(
    r"(?:昨|今|明|后|大后)天|(?:今|昨|明)晚|(?:上午|中午|下午|晚上|早上|凌晨|半夜)"
    r"|(?:这|下|上)(?:周|星期|个月|月)|周末|月底|年底|明年|今年"
    r"|(?:周|星期)[一二三四五六日天]|\d{1,2}\s*[点时:：]\s*\d{0,2}"
    r"|\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?|\d{4}\s*[年/-]"
    r"|(?:几|哪)天|多久|什么时候|等会|待会|过几天"
)

MOVEMENT_RE = re.compile(
    r"(?:去|到|回|飞|前往|赶到|搬去|搬回)(?:了|过)?[\u4e00-\u9fff]{2,10}"
)

PLACE_RE = re.compile(
    r"[\u4e00-\u9fff]{2,8}(?:市|省|区|县|广场|机场|车站|火车站|高铁站|医院|公司|学校|大学"
    r"|公园|酒店|餐厅|景区|岛|山|湖|路|街)"
    r"|上海|北京|广州|深圳|杭州|成都|重庆|武汉|西安|南京|天津|苏州|长沙|郑州|青岛"
    r"|厦门|福州|合肥|济南|昆明|大连|沈阳|哈尔滨|长春|石家庄|太原|南昌|贵阳|南宁|海口"
    r"|三亚|兰州|西宁|银川|乌鲁木齐|拉萨|呼和浩特|香港|澳门|台北|东京|大阪|首尔|新加坡"
)

# §4.1 规则 2：明确偏好表述
PREFERENCE_RE = re.compile(
    r"我(?:很|超|超级|特别|不太|不|最|比较|挺|有点|一向|一直|平时|从来)?"
    r"(?:喜欢|爱|讨厌|不喜欢|不吃|不爱吃|不想吃|偏好|习惯|受不了|怕|忌口|过敏|更想让)"
    r"|(?:喜欢|讨厌|爱吃|不吃|忌口|过敏)(?:吃|喝|玩)?[\u4e00-\u9fff]{1,6}"
)

# §4.1 规则 3：承诺 / 约定 / 提醒
COMMITMENT_RE = re.compile(
    r"到时候|叫我|喊我|提醒我|记得(?:帮我|提醒|给)|答应|约好|约了|别忘|别忘了|帮我|帮带"
    r"|带给|给我留|留给我|说好了|一言为定|包在我身上"
)

# §4.1 规则 4：第一人称状态陈述
FIRST_PERSON_RE = re.compile(
    r"我(?:今天|最近|刚刚|刚|昨天|前天|前几天|这周|下周|上个月|这个月|等会|马上|已经)?"
    r"(?:感冒|生病|发烧|咳嗽|头疼|胃疼|嗓子|过敏|辞职|离职|入职|转学|毕业|结婚|离婚"
    r"|分手|怀孕|生了|住院|体检|考试|面试|加班|出差|旅游|搬家|搬去|搬回|回老家|买了|换了"
    r"|养了|开始学|准备考|打算考|决定|升职|涨工资|被裁|中奖|丢|捡)"
)

# §7.1 寒暄词表默认值：GateConfig.greeting_words 存在时以配置为准（A 负责字段），
# 本模块保留同值兜底，保证直接构造/鸭子类型 cfg 时行为一致。
# 单字项只做「整句相等」匹配，不做子串匹配。
# 与 config.GateConfig.greeting_words 默认值保持同一份语义（配置字段存在时以配置为准）。
_DEFAULT_GREETING_WORDS: tuple[str, ...] = (
    "你好",
    "您好",
    "在吗",
    "在么",
    "早",
    "早安",
    "早上好",
    "晚安",
    "哈哈",
    "呵呵",
    "嗯",
    "嗯嗯",
    "哦",
    "哦哦",
    "好",
    "好的",
    "行",
    "收到",
    "谢谢",
    "再见",
)

# 纯 @bot 判定用：CQ 码 @ 提及与本机渲染的 @昵称
_CQ_AT_RE = re.compile(r"\[CQ:at,[^\]]*\]", re.IGNORECASE)
_AT_RE = re.compile(r"@[^\s@,，。.!！?？]{1,32}")


def _has_question(text: str) -> bool:
    """§7.1：问号只认 ? / ？ 两种半/全角形式。"""
    return "?" in text or "？" in text


def _content_only(text: str) -> str:
    """去掉标点 / 符号（含 emoji）/ 空白 / 控制符后的正文字符（小写）。

    §7.1 的「去标点后整句完全相等」与「复读」都以本函数为比较口径；
    单字项因此天然只做整句相等，不会做子串匹配。
    """
    kept: list[str] = []
    for char in (text or "").lower():
        category = unicodedata.category(char)
        if category[0] in ("P", "S", "Z", "C"):
            continue
        kept.append(char)
    return "".join(kept)


def _is_no_text(text: str) -> bool:
    """§7.1 规则 (a)：纯表情 / 纯标点 / 纯 emoji / 纯重复字符（无文字信息量）。"""
    content = (text or "").strip()
    if not content:
        return True
    if _content_only(content) == "":
        return True
    # 整句只有一个字符重复（哈哈哈哈哈 / 666666 / ？？？）：不携带文字信息
    return len(set(content)) == 1


def _greeting_words(cfg: GateConfig) -> frozenset[str]:
    """取寒暄词表（配置优先，缺字段时用模块默认），统一为去标点后的整句集合。"""
    raw = getattr(cfg, "greeting_words", None) or _DEFAULT_GREETING_WORDS
    try:
        words = [str(word) for word in raw]
    except TypeError:  # 配置异常时退回默认，绝不因为词表让轻路径抛异常
        words = list(_DEFAULT_GREETING_WORDS)
    normalized = {_content_only(word) for word in words}
    normalized.discard("")
    return frozenset(normalized)


def _strip_mentions(text: str) -> str:
    """去掉 @ 提及 / CQ 码 / @昵称，用于「纯 @bot 无正文」判定。"""
    cleaned = _CQ_AT_RE.sub(" ", text or "")
    return _AT_RE.sub(" ", cleaned)


def should_extract(
    text: str,
    *,
    at_bot: bool = False,
    reply_to_bot: bool = False,
    cfg: GateConfig | None = None,
) -> bool:
    """§4.1：命中任一条件才附提取协议。"""
    cfg = cfg or GateConfig()
    if not cfg.enabled:
        return False
    if at_bot or reply_to_bot:
        return True
    content = text or ""
    if not content.strip():
        return False
    for pattern in (TIME_RE, MOVEMENT_RE, PLACE_RE, PREFERENCE_RE, COMMITMENT_RE, FIRST_PERSON_RE):
        if pattern.search(content):
            return True
    for extra in cfg.extra_patterns:
        try:
            if re.search(extra, content):
                return True
        except re.error:
            continue
    return False


def is_light_message(
    text: str,
    *,
    last_user_message: str | None = None,
    at_bot: bool = False,
    cfg: GateConfig | None = None,
) -> bool:
    """§7.1 轻路径：命中任一规则即不检索（零 LLM，与闸门完全解耦）。

    规则（v1.6-rc4 §7.1，E3）：
    (a) 纯表情 / 纯标点 / 纯 emoji / 纯重复字符（无文字）→ True；
    (b) 去首尾空白后 Unicode 字符数 <4 且不含问号（?/？）且非 @bot → True；
    (c) 寒暄词表「去标点后整句完全相等」（GateConfig.greeting_words 可配置；
        单字项只做整句相等，不做子串）；
    (d) 复读：与最近一条用户消息（任意发送者）去标点后完全相同 → True；
    (e) 纯 @bot 无正文（去掉 @提及 / CQ 码 / 昵称后无实质内容且无问号）→ True；
    其余 False。轻路径只决定是否检索，不决定是否提取（硬规则 5 / E3）。
    """
    gate_cfg = cfg or GateConfig()
    stripped = (text or "").strip()
    # (a) 无文字信息量（含空串）
    if _is_no_text(stripped):
        return True
    question = _has_question(stripped)
    # (b) 极短句：<4 字、非提问、非 @bot
    if len(stripped) < 4 and not question and not at_bot:
        return True
    # @提及 / CQ 码对用户不可见：寒暄与复读按「去掉提及后的可见文本」比较
    visible = _content_only(_strip_mentions(stripped))
    # (c) 寒暄词表：去标点后整句完全相等
    if visible and visible in _greeting_words(gate_cfg):
        return True
    # (d) 复读：与最近一条用户消息去标点后完全相同
    if last_user_message is not None:
        previous = _content_only(_strip_mentions(str(last_user_message)))
        if visible and previous and visible == previous:
            return True
    # (e) 纯 @bot 无正文（去掉 @提及 / CQ 码 / 昵称后无实质内容且无问号）
    if at_bot and not question and not visible:
        return True
    return False
