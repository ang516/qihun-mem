"""memory.jsonl 持久化（v1.4 §2 / §2.4）。

- 追加：一次 write 写完一行（含换行），失败则该轮不写；
- 加载：坏行不中断，原样搬到 memory.bad.jsonl，内存跳过；
- 重写：写 *.tmp → flush + fsync → os.replace，崩溃不破坏旧文件；
- 向量：memory.vec.jsonl sidecar（float16 + base64），与 memory.jsonl 同序追加。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import Config
from .fsutil import atomic_write_text
from .metrics import Metrics
from .models import MemoryItem

log = logging.getLogger("memory")


@dataclass
class LoadReport:
    total_lines: int = 0
    valid: int = 0
    bad: int = 0
    new_bad: int = 0
    vector_loaded: int = 0
    vector_errors: int = 0
    bad_samples: list[str] = field(default_factory=list)


def _pack_vector(vector: list[float]) -> str:
    """float16 + base64（§6.5）。"""
    payload = struct.pack("<%de" % len(vector), *[float(x) for x in vector])
    return base64.b64encode(payload).decode("ascii")


def _unpack_vector(payload: str, dim: int) -> list[float]:
    raw = base64.b64decode(payload.encode("ascii"), validate=True)
    if len(raw) != dim * 2:
        raise ValueError(f"vector bytes mismatch: {len(raw)} != {dim * 2}")
    return list(struct.unpack("<%de" % dim, raw))


class MemoryStore:
    """单实例、单写者的 jsonl 存储。"""

    def __init__(
        self,
        cfg: Config,
        metrics: Metrics | None = None,
        *,
        vector_dim: int | None = None,
        read_only: bool = False,
    ) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.read_only = bool(read_only)
        self.items: dict[str, MemoryItem] = {}
        self._seq = 0
        self._turn_keys: dict[str, str] = {}
        self.loaded = False
        self.load_report = LoadReport()
        # 优先用 embedder 的实际维度（配置 dim 可能与真实模型不一致，见 B6）
        self.vector_dim = int(vector_dim) if vector_dim and vector_dim > 0 else max(1, int(cfg.embedding.dim))

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    @property
    def memory_path(self) -> Path:
        return self.cfg.memory_file

    @property
    def vector_path(self) -> Path:
        return self.cfg.vector_file

    @property
    def bad_path(self) -> Path:
        return self.cfg.bad_file

    @property
    def archive_path(self) -> Path:
        return self.cfg.archive_file

    # ------------------------------------------------------------------
    # 加载 / 追加 / 重写
    # ------------------------------------------------------------------

    def load(self) -> LoadReport:
        self.items.clear()
        self._turn_keys.clear()
        report = LoadReport()
        raw_lines = self._read_lines(self.memory_path)
        bad_lines: list[str] = []
        for line in raw_lines:
            if not line.strip():
                continue
            report.total_lines += 1
            try:
                item = MemoryItem.from_dict(json.loads(line))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                report.bad += 1
                bad_lines.append(line)
                if len(report.bad_samples) < 5:
                    report.bad_samples.append(str(exc)[:120])
                continue
            self.items[item.id] = item
            report.valid += 1
            if item.turn_key:
                self._turn_keys[item.turn_key] = item.id
        if bad_lines and not self.read_only:
            # 只读模式（--dry-run）只统计/告警，绝不写 memory.bad.jsonl（P1-1）
            report.new_bad = self._append_bad_lines(bad_lines)
        # 归档条目：id 自增要避免重用（B14）；turn_key 也要参与幂等判定，
        # 否则「导入→维护归档→重跑导入」会重复导入已归档条目（P1-3）。
        archived = self._archived_records()
        archived_seq = max((self._seq_of(item_id) for item_id, _ in archived), default=0)
        self._seq = max(
            max((self._seq_of(item_id) for item_id in self.items), default=0),
            archived_seq,
        )
        for item_id, turn_key in archived:
            if turn_key:
                self._turn_keys.setdefault(turn_key, item_id)
        report.vector_loaded, report.vector_errors = self.load_vectors()
        self.loaded = True
        self.load_report = report
        if self.metrics is not None:
            self.metrics.event(
                "store.load",
                total=report.total_lines,
                valid=report.valid,
                bad=report.bad,
                vector_loaded=report.vector_loaded,
                vector_errors=report.vector_errors,
            )
            if report.bad:
                self.metrics.event("store.bad_lines", count=report.bad)
        # §2.4：加载后校验 文件行数 = 有效行 + bad 行
        if report.total_lines != report.valid + report.bad:
            log.error(
                "memory load invariant broken: total=%s valid=%s bad=%s",
                report.total_lines,
                report.valid,
                report.bad,
            )
        return report

    def _read_lines(self, path: Path) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
                return handle.read().splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("cannot read %s: %r", path, exc)
            return []

    def _append_bad_lines(self, lines: list[str]) -> int:
        existing: set[str] = set()
        if self.bad_path.is_file():
            existing = set(self._read_lines(self.bad_path))
        fresh = [line for line in lines if line not in existing]
        if not fresh:
            return 0
        try:
            self.bad_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.bad_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(fresh) + "\n")
        except OSError as exc:
            log.warning("cannot append bad lines: %r", exc)
            return 0
        return len(fresh)

    @staticmethod
    def _seq_of(item_id: str) -> int:
        try:
            return int(str(item_id).split("_")[-1])
        except (ValueError, IndexError):
            return 0

    def _archived_records(self) -> list[tuple[str, str | None]]:
        """归档条目 (id, turn_key)；坏行跳过（归档文件只用于 id 续号与幂等判定）。"""
        records: list[tuple[str, str | None]] = []
        for line in self._read_lines(self.archive_path):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                item_id = str(data["id"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            turn_key = data.get("turn_key")
            records.append((item_id, str(turn_key) if turn_key else None))
        return records

    def _archived_ids(self) -> list[str]:
        return [item_id for item_id, _turn_key in self._archived_records()]

    def next_id(self) -> str:
        self._seq += 1
        return f"m_{self._seq:04d}"

    def append(self, item: MemoryItem) -> None:
        """追加一行（先落盘，再更新内存；失败不更新内存）。"""
        if self.read_only:
            raise RuntimeError("memory store is read-only")
        line = json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":"))
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.memory_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
        self.items[item.id] = item
        if item.turn_key:
            self._turn_keys[item.turn_key] = item.id
        self._seq = max(self._seq, self._seq_of(item.id))

    def rewrite(self, items: Iterable[MemoryItem] | None = None, *, vectors: bool = True) -> None:
        """全量原子重写（每日一次 / 归档后）。"""
        if self.read_only:
            raise RuntimeError("memory store is read-only")
        item_list = list(items if items is not None else self.items.values())
        body = "".join(
            json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in item_list
        )
        atomic_write_text(self.memory_path, body)
        if vectors:
            vec_lines = [
                json.dumps(
                    {"id": item.id, "dim": len(item.vector), "vec": _pack_vector(item.vector)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for item in item_list
                if item.vector
            ]
            atomic_write_text(self.vector_path, "".join(line + "\n" for line in vec_lines))
        self.items = {item.id: item for item in item_list}
        self._turn_keys = {item.turn_key: item.id for item in item_list if item.turn_key}
        # 归档条目的 turn_key 也要保留在内存里（P1-3：重写后本进程仍能识别已导入文件）
        for item_id, turn_key in self._archived_records():
            if turn_key:
                self._turn_keys.setdefault(turn_key, item_id)

    def archive(self, item_ids: Iterable[str]) -> int:
        """把死记忆移入 memory.archive.jsonl（不物理删除，§8 / 原则 4）。

        幂等：已在 archive 里的 id 不会重复追加（archive 成功但 rewrite 失败的重试场景）。
        """
        if self.read_only:
            raise RuntimeError("memory store is read-only")
        ids = [item_id for item_id in item_ids if item_id in self.items]
        if not ids:
            return 0
        already = set(self._archived_ids())
        to_append = [self.items[item_id] for item_id in ids if item_id not in already]
        try:
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            if to_append:
                with open(self.archive_path, "a", encoding="utf-8", newline="\n") as handle:
                    for item in to_append:
                        handle.write(json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            log.warning("archive append failed, keep items in memory: %r", exc)
            return 0
        remaining = [item for item_id, item in self.items.items() if item_id not in set(ids)]
        self.rewrite(remaining)
        return len(ids)

    # ------------------------------------------------------------------
    # 向量 sidecar（§2 / §6.5）
    # ------------------------------------------------------------------

    def load_vectors(self) -> tuple[int, int]:
        loaded = 0
        errors = 0
        dim = self.vector_dim
        for line in self._read_lines(self.vector_path):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                item_id = str(record["id"])
                record_dim = int(record["dim"])
                if record_dim != dim:
                    errors += 1
                    continue
                vector = _unpack_vector(str(record["vec"]), record_dim)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                errors += 1
                continue
            item = self.items.get(item_id)
            if item is None:
                errors += 1
                continue
            item.vector = vector
            loaded += 1
        return loaded, errors

    def append_vector(self, item: MemoryItem) -> bool:
        """追加向量到 sidecar；失败返回 False（该条暂不参与语义分）。"""
        if self.read_only:
            raise RuntimeError("memory store is read-only")
        if not item.vector:
            return False
        try:
            self.vector_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "id": item.id,
                "dim": len(item.vector),
                "vec": _pack_vector(item.vector),
            }
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with open(self.vector_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
            return True
        except OSError as exc:
            log.warning("append vector failed for %s: %r", item.id, exc)
            if self.metrics is not None:
                self.metrics.event("storage.vector_write_fail", item_id=item.id)
            return False

    # ------------------------------------------------------------------
    # 幂等（§2.4 turn_key）
    # ------------------------------------------------------------------

    def has_turn_key(self, turn_key: str) -> bool:
        return bool(turn_key) and turn_key in self._turn_keys

    def item_by_turn_key(self, turn_key: str) -> MemoryItem | None:
        item_id = self._turn_keys.get(turn_key)
        return self.items.get(item_id) if item_id else None

    def register_turn_key(self, turn_key: str, item_id: str) -> None:
        if turn_key:
            self._turn_keys[turn_key] = item_id

    # ------------------------------------------------------------------
    # 归档文件清理 / 统计
    # ------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        active = sum(1 for item in self.items.values() if item.status == "active")
        dormant = len(self.items) - active
        return {"total": len(self.items), "active": active, "dormant": dormant}

    def file_sizes(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for name, path in (
            ("memory", self.memory_path),
            ("vectors", self.vector_path),
            ("bad", self.bad_path),
            ("archive", self.archive_path),
        ):
            try:
                result[name] = os.path.getsize(path)
            except OSError:
                result[name] = 0
        return result
