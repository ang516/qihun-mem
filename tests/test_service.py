"""HTTP 适配层：请求处理、鉴权、两阶段回合、只读 Obsidian、HTTP 端到端。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from qihun_memory.config import Config
from qihun_memory.service import MemoryService, ServiceError, create_server


def build_config(tmp_path, **overrides) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    data.update(overrides)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def make_service(tmp_path, **overrides) -> MemoryService:
    return MemoryService(build_config(tmp_path, **overrides))


def test_health_state_metrics(tmp_path) -> None:
    service = make_service(tmp_path)
    status, payload = service.handle("GET", "/health")
    assert status == 200
    assert payload["ok"] is True
    assert payload["items"]["total"] == 0

    status, state = service.handle("GET", "/state")
    assert status == 200
    assert state["stale"] is True
    assert state["valence"] == 0.0

    status, metrics = service.handle("GET", "/metrics")
    assert status == 200
    assert "counters" in metrics["metrics"]


def test_memory_list_and_search(tmp_path) -> None:
    service = make_service(tmp_path)
    service.system.remember(
        [{"type": "episode", "content": "用户A下周三去上海出差", "tags": ["出差"]}],
        user_id="u_1",
        scope="group",
        valence=0.0,
    )
    status, listing = service.handle("GET", "/memory/list", {"scope": "group"})
    assert status == 200
    assert listing["total"] == 1
    assert listing["items"][0]["content"] == "用户A下周三去上海出差"

    status, search = service.handle("GET", "/memory/search", {"q": "上海出差", "scope": "group"})
    assert status == 200
    assert search["retrieval"]["candidates"] >= 1
    assert search["selected"]

    status, error = service.handle("GET", "/memory/search", {"scope": "group"})
    assert status == 400


def test_turn_prepare_finish(tmp_path) -> None:
    service = make_service(tmp_path)
    status, prepared = service.handle(
        "POST",
        "/turn/prepare",
        body=json.dumps(
            {
                "user_id": "u_1",
                "scope": "group",
                "message": "我下周三去上海出差",
                "msgid": "m-1",
            }
        ).encode("utf-8"),
    )
    assert status == 200
    assert prepared["gate"] is True
    assert prepared["prompt"]["system"]
    turn_id = prepared["turn_id"]

    raw = json.dumps(
        {
            "memories": [{"type": "episode", "content": "用户A下周三去上海出差", "tags": ["出差"]}]
        },
        ensure_ascii=False,
    )
    status, finished = service.handle(
        "POST",
        "/turn/finish",
        body=json.dumps({"turn_id": turn_id, "raw": "好的，记下了。<MEMORY_JSON>" + raw + "</MEMORY_JSON>"}).encode("utf-8"),
    )
    assert status == 200
    assert finished["answer"] == "好的，记下了。"
    assert finished["written"]["created"] == 1
    assert len(service.system.all_items()) == 1

    # 同一个 turn 不能 finish 两次
    status, error = service.handle(
        "POST", "/turn/finish", body=json.dumps({"turn_id": turn_id, "raw": "好"}).encode("utf-8")
    )
    assert status == 404
    assert error["ok"] is False


def test_auth_token(tmp_path) -> None:
    cfg = build_config(tmp_path, service={"token": "secret-token"})
    service = MemoryService(cfg)
    status, _ = service.handle("GET", "/health")
    assert status == 401
    status, _ = service.handle("GET", "/health", headers={"authorization": "Bearer wrong"})
    assert status == 401
    status, payload = service.handle("GET", "/health", headers={"Authorization": "Bearer secret-token"})
    assert status == 200
    assert payload["ok"] is True


def test_obsidian_readonly_endpoints(tmp_path) -> None:
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "persona.md").write_text("器魂人设", encoding="utf-8")
    (vault / "notes" / "a.md").write_text("上海笔记", encoding="utf-8")
    service = make_service(tmp_path, vault_dir=str(vault))

    status, notes = service.handle("GET", "/obsidian/notes")
    assert status == 200
    assert notes["readonly"] is True
    assert "persona.md" in notes["notes"]

    status, note = service.handle("GET", "/obsidian/note", {"path": "notes/a.md"})
    assert status == 200
    assert note["readonly"] is True
    assert "上海笔记" in note["content"]

    status, error = service.handle("GET", "/obsidian/note", {"path": "../secret.md"})
    assert status == 400
    assert error["ok"] is False


def test_users_refresh_and_maintain(tmp_path) -> None:
    service = make_service(tmp_path)
    status, payload = service.handle(
        "POST", "/users/refresh", body=json.dumps({"members": {"u_1": "用户A"}}).encode("utf-8")
    )
    assert status == 200
    assert payload["changed"] == 1
    assert service.system.users.nickname("u_1") == "用户A"

    status, payload = service.handle("POST", "/maintain")
    assert status == 200
    assert payload["report"]["rewritten"] is True


def test_unknown_route_and_bad_json(tmp_path) -> None:
    service = make_service(tmp_path)
    status, payload = service.handle("GET", "/nope")
    assert status == 404
    assert payload["ok"] is False
    status, payload = service.handle("POST", "/turn/prepare", body=b"{bad json")
    assert status == 400


def test_http_end_to_end(tmp_path) -> None:
    cfg = build_config(tmp_path)
    server = create_server(cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True

        request = urllib.request.Request(
            base + "/turn/prepare",
            data=json.dumps({"user_id": "u_1", "scope": "group", "message": "我下周三去上海出差"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            prepared = json.loads(response.read().decode("utf-8"))
        assert prepared["ok"] is True
        assert prepared["turn_id"]

        raw = json.dumps(
            {"memories": [{"type": "fact", "content": "用户A下周三去上海出差", "tags": ["出差"]}]},
            ensure_ascii=False,
        )
        request = urllib.request.Request(
            base + "/turn/finish",
            data=json.dumps({"turn_id": prepared["turn_id"], "raw": "好<MEMORY_JSON>" + raw + "</MEMORY_JSON>"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            finished = json.loads(response.read().decode("utf-8"))
        assert finished["answer"] == "好"
        assert finished["written"]["created"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        server.service.close()
