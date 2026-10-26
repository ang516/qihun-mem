"""写入闸门与检索轻路径（v1.4 §4.1 / §6.1）。

闸门只决定「要不要在提示词末尾附提取协议」（§7），不影响检索；
检索路由（轻路径）与闸门完全解耦。
"""

from __future__ import annotations

import re

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

_ACK_RE = re.compile(
    r"^(?:哈+|呵+|嘿嘿+|嘿+|嗨+|hi+|hello|在吗|在么|在不在|在呢|早|早安|早啊|晚安|晚上好"
    r"|早上好|中午好|好|好的|好滴|好嘞|行|可以|嗯+|哦+|噢+|啊+|呀+|哟+|1|6|66+|666+|"
    r"\+1|收到|谢谢|谢啦|谢|拜拜|88|再见|草|绷|笑死|乐|难绷|确实|真实|支持|牛|牛啊|绝了"
    r"|来了|来了来了|打卡|签到)[!！。.~～?？\s，,、]*$"
)
_EMOJI_RE = re.compile(
    r"^[\s\u200b-\u200f\u2600-\u27bf\U0001F000-\U0001FAFF\ufe0f\u2764\u2b50]+$"
)
_PUNCT_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


def _normalized(text: str) -> str:
    return (text or "").strip().lower()


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


def is_light_message(text: str) -> bool:
    """§6.1 轻路径：寒暄、复读、表情、无信息量短句 → 不检索。"""
    content = _normalized(text)
    if not content:
        return True
    if _EMOJI_RE.match(content) or _PUNCT_RE.match(content):
        return True
    if _ACK_RE.match(content):
        return True
    # 纯重复字符（哈哈哈哈哈 / ？？？ / 666666）
    if len(content) >= 2 and len(set(content)) == 1:
        return True
    # 极短且无语义（"嗯嗯"、"哦哦" 由 _ACK_RE 覆盖；这里兜底 1-2 个字符）
    return len(content) <= 2
