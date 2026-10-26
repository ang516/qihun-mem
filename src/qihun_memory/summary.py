"""滚动会话摘要（v1.4 §7 步骤 2 / §8 ≤200 tokens）。

脚本裁剪、不调用 LLM：每轮把「用户消息 + bot 回答」压成短句追加，
超过 200 token 预算就从最旧的句子开始丢弃。持久化到 summary.json（原子写）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .config import PromptConfig
from .fsutil import atomic_write_text
from .models import utc_now, utc_now_iso
from .tokens import estimate_tokens

log = logging.getLogger("memory")

_MAX_LINE_CHARS = 120


def conversation_key(scope: str, user_id: str) -> str:
    """一个群 + 一个私聊：group 共用摘要，dm 按 user_id 分。"""
    if scope == "group":
        return "group"
    return f"dm:{user_id}"


class RollingSummary:
    def __init__(self, path: str | Path, cfg: PromptConfig | None = None) -> None:
        self.path = Path(path)
        self.cfg = cfg or PromptConfig()
        self._data: dict[str, list[str]] = {}
        self.load()

    def load(self) -> None:
        self._data = {}
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            conversations = raw.get("conversations") if isinstance(raw, dict) else None
            if not isinstance(conversations, dict):
                return
            for key, value in conversations.items():
                units = value.get("units") if isinstance(value, dict) else None
                if isinstance(units, list):
                    self._data[str(key)] = [str(u) for u in units if str(u).strip()]
        except (OSError, ValueError) as exc:
            log.warning("summary.json unreadable, starting empty: %r", exc)

    def get(self, key: str) -> str:
        return "\n".join(self._data.get(key, []))

    def update(self, key: str, user_text: str, answer_text: str) -> str:
        units = self._data.setdefault(key, [])
        for prefix, text in (("用户：", user_text), ("器魂：", answer_text)):
            line = (text or "").strip().replace("\n", " ")[:_MAX_LINE_CHARS]
            if line:
                units.append(prefix + line)
        while units and estimate_tokens("\n".join(units)) > self.cfg.summary_tokens:
            units.pop(0)
        self.save()
        return "\n".join(units)

    def seed(self, key: str, units: list[str]) -> str:
        """用外部内容（旧库导入）整体替换某个会话的摘要单元。

        与 update 的区别：不追加「用户：/器魂：」前缀，直接采用调用方给出的
        单元顺序；每个单元压缩成单行并裁剪到 120 字，总长超过 summary_tokens
        预算时**从尾部**丢弃（旧库摘要按重要性排序，头部最值得保留）。
        写入后立即 save()，返回最终生效的摘要文本。
        """
        cleaned: list[str] = []
        for unit in units:
            text = " ".join(str(unit).split())
            if text:
                cleaned.append(text[:_MAX_LINE_CHARS])
        while cleaned and estimate_tokens("\n".join(cleaned)) > self.cfg.summary_tokens:
            cleaned.pop()
        self._data[key] = cleaned
        self.save()
        return "\n".join(cleaned)

    def reset(self, key: str) -> None:
        self._data.pop(key, None)
        self.save()

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": utc_now_iso(),
            "conversations": {
                key: {"units": units, "updated_at": utc_now_iso()} for key, units in self._data.items()
            },
        }
        try:
            atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            log.warning("summary.json write failed: %r", exc)
