# 器魂记忆系统（QiHun Memory System）
这个系统是基于dsh+SnowLuma+QQ-bridge制作的qqbot设计的一个轻量级记忆系统，使用obsidian进行只读操作，参考了部分人类记忆特征进行设计，设计了记忆曲线遗忘过程，解决了长期使用后容易一句话挤爆上下文的问题，有多因素影响记忆强度。（其中感情因素设计的感情模块不在该项目中）

## 当前状态

早期设计与实现阶段，尚未生产可用。设计规范见 [QiHun-Memory-System v1.4.md]。

## 核心特性

- **归属先于内容**：每条记忆必须有 `user_id` 和 `scope`，检索先过滤再排序
- **写入有闸门**：只提取时间、地点、承诺、偏好、第一人称状态等有效信息
- **每轮最多一次 LLM 调用**：回答与记忆提取合并
- **遗忘默认发生**：按遗忘曲线衰减，低于阈值转 dormant，不真删，可解冻
- **情绪调制记忆**：bot 当前情绪影响写入稳定性、检索排序和痛苦记忆的可提取性
- **解析失败不阻塞聊天**：LLM 提取 JSON 失败 = 本轮无新记忆，聊天照常返回
- **单向只读情感状态**：记忆系统只读 `state.json`，不回写

## 目录结构

```text
qihun-mem/
├── README.md
├── docs/
│   └── schema.md          # 记忆系统设计文档 v1.3
├── examples/
│   ├── persona.example.md
│   ├── state.example.json
│   ├── memory.example.jsonl
│   └── users.example.json
├── src/qihun_memory/
│   ├── parser.py          # LLM 输出解析与兜底
│   ├── memory.py          # 写入、检索、遗忘、强化
│   └── tools.py
└── tests/

## 快速开始

实现中，以下为规划命令。

bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

## 复制示例配置：

bash
cp examples/state.example.json state.json
cp examples/users.example.json users.json

## 运行测试：

bash
pytest

## 文档
**QiHun-Memory-System v1.4.md

## 硬规则摘要

每条记忆必须有 user_id 和 scope
检索必须先过滤 scope，再打分
注入 prompt 的记忆必须标注说话人
每轮最多一次 LLM 调用
写入必须过闸门
valence 永不单独驱动行为
S 永不超过 90 天
压抑只调制 S_eff，永不改写 S
记忆系统禁止回写 state.json
LLM 提取失败不得影响回答生成

## License

待定。