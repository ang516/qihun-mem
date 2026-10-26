"""memory.jsonl 读写容错、原子重写、向量 sidecar、幂等与移档（§2.4 / §6.5 / §8）。"""

from __future__ import annotations

import json

import pytest

from qihun_memory.config import Config
from qihun_memory.models import MemoryItem, utc_now_iso
from qihun_memory.store import MemoryStore


def make_cfg(tmp_path, **overrides) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    data.update(overrides)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def make_item(store: MemoryStore, **overrides) -> MemoryItem:
    now = utc_now_iso()
    base = dict(
        id=store.next_id(),
        user_id="u_1",
        scope="group",
        type="episode",
        content="用户A下周三去上海出差",
        created_at=now,
        last_access=now,
        access_count=0,
        mention_count=1,
        S=7.0,
        valence=0.0,
        status="active",
        tags=["出差"],
    )
    base.update(overrides)
    return MemoryItem(**base)


def test_append_and_reload(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    store = MemoryStore(cfg)
    store.load()
    item = make_item(store)
    store.append(item)
    assert cfg.memory_file.read_text(encoding="utf-8").count("\n") == 1

    reloaded = MemoryStore(cfg)
    report = reloaded.load()
    assert report.valid == 1
    assert report.bad == 0
    assert reloaded.items[item.id].content == item.content
    assert reloaded.counts() == {"total": 1, "active": 1, "dormant": 0}


def test_load_isolates_bad_lines(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    good = json.dumps(
        {
            "id": "m_0001",
            "user_id": "u_1",
            "scope": "group",
            "type": "fact",
            "content": "用户A喜欢本帮菜",
            "created_at": utc_now_iso(),
            "last_access": utc_now_iso(),
            "access_count": 0,
            "mention_count": 1,
            "S": 30.0,
            "valence": 0.0,
            "status": "active",
            "tags": [],
        },
        ensure_ascii=False,
    )
    cfg.memory_file.write_text(good + "\n{not json}\n" + good.replace("m_0001", "m_0002")[:10] + "\n", encoding="utf-8")
    store = MemoryStore(cfg)
    report = store.load()
    assert report.total_lines == 3
    assert report.valid == 1
    assert report.bad == 2
    # 坏行原样搬到 memory.bad.jsonl
    bad_text = cfg.bad_file.read_text(encoding="utf-8")
    assert "{not json}" in bad_text
    # 二次加载不重复搬（幂等）
    store2 = MemoryStore(cfg)
    store2.load()
    assert cfg.bad_file.read_text(encoding="utf-8").count("{not json}") == 1


def test_atomic_rewrite_leaves_no_tmp(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    store = MemoryStore(cfg)
    store.load()
    items = [make_item(store, id=f"m_{i:04d}", content=f"记忆 {i}") for i in range(1, 4)]
    for item in items:
        store.append(item)
    store.rewrite()
    assert not (cfg.memory_file.parent / (cfg.memory_file.name + ".tmp")).exists()
    reloaded = MemoryStore(cfg)
    assert reloaded.load().valid == 3


def test_vector_sidecar_roundtrip(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    store = MemoryStore(cfg)
    store.load()
    item = make_item(store)
    item.vector = [0.5, -0.25, 0.125, 1.0, 0.0, 0.75, -0.5, 0.25]
    store.append(item)
    assert store.append_vector(item) is True
    reloaded = MemoryStore(cfg)
    report = reloaded.load()
    assert report.vector_loaded == 1
    assert report.vector_errors == 0
    loaded = reloaded.items[item.id].vector
    assert loaded is not None
    assert len(loaded) == 8
    for expected, actual in zip(item.vector, loaded):
        assert actual == pytest.approx(expected, abs=1e-3)


def test_vector_dim_mismatch_is_skipped(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    store = MemoryStore(cfg)
    store.load()
    item = make_item(store)
    store.append(item)
    # 直接写一条 dim 不对的 sidecar
    cfg.vector_file.write_text(
        json.dumps({"id": item.id, "dim": 512, "vec": "AAAA"}) + "\n",
        encoding="utf-8",
    )
    reloaded = MemoryStore(cfg)
    report = reloaded.load()
    assert report.vector_loaded == 0
    assert report.vector_errors == 1
    assert reloaded.items[item.id].vector is None


def test_turn_key_idempotency(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    store = MemoryStore(cfg)
    store.load()
    item = make_item(store, turn_key="u_1:msg-1")
    store.append(item)
    assert store.has_turn_key("u_1:msg-1") is True
    assert store.has_turn_key("u_1:msg-2") is False
    reloaded = MemoryStore(cfg)
    reloaded.load()
    assert reloaded.has_turn_key("u_1:msg-1") is True


def test_archive_moves_to_archive_file(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    store = MemoryStore(cfg)
    store.load()
    items = [make_item(store, id=f"m_{i:04d}", content=f"记忆 {i}") for i in range(1, 4)]
    for item in items:
        store.append(item)
    archived = store.archive([items[0].id])
    assert archived == 1
    assert store.counts()["total"] == 2
    assert cfg.memory_file.read_text(encoding="utf-8").count("\n") == 2
    archive_lines = cfg.archive_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(archive_lines) == 1
    assert json.loads(archive_lines[0])["id"] == items[0].id


def test_blank_lines_ignored(tmp_path) -> None:
    cfg = make_cfg(tmp_path)
    store = MemoryStore(cfg)
    store.load()
    item = make_item(store)
    cfg.memory_file.write_text("\n\n", encoding="utf-8")
    report = store.load()
    assert report.total_lines == 0
    assert report.valid == 0
    assert report.bad == 0
