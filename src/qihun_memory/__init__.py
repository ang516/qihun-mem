"""器魂记忆系统（QiHun Memory System）v1.6-rc4。

面向「一个 QQ 群 + 一个私聊」的轻量长期记忆引擎。设计规范见仓库根目录
QiHun-Memory-System-v1.6-rc4.md，模块与规范的对应关系见 docs/ARCHITECTURE.md。
v1.6-rc4 在 v1.5-r2 事实层之上新增叙事层（情感日记）：diary.py 独立 worker，
不参与检索 / S / R / user_affect；日记只提供人格连续性语境。
"""

from .version import __version__

__all__ = ["__version__"]
