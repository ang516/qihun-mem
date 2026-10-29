"""v1.6-rc4 叙事层传输层回归：HTTP 两阶段 + CLI diary 子命令。

覆盖：
- /diary/list、/diary/excerpts、/diary/prepare、/diary/finish 的正常路径与错误码；
- CLI diary list / prompt / add（宿主两段式接 LLM，进程内不调用 LLM）；
- health 与 status 暴露 diary 计数。
"""

from __future__ import annotations

import json

import pytest

from qihun_memory.cli import main
from qihun_memory.config import Config
from qihun_memory.service import MemoryService


DIARY_TEXT = "今天群里有人聊到出差，我想起自己其实不太喜欢机场。"


def make_config(tmp_path, diary: dict | None = None) -> Config:
    cfg = Config.from_dict(
        {
            "data_dir": str(tmp_path / "data"),
            "embedding": {"enabled": False, "backend": "none", "dim": 8},
            "diary": dict(diary or {}),
        },
        base_dir=tmp_path,
    )
    cfg.ensure_dirs()
    return cfg


def run_cli(argv: list[str], capsys) -> tuple[int, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out


# --------------------------------------------------------------------------
# HTTP 两阶段
# --------------------------------------------------------------------------

def test_diary_http_two_phase_and_health(tmp_path) -> None:
    service = MemoryService(make_config(tmp_path))
    try:
        status, payload = service.handle("GET", "/diary/list")
        assert status == 200 and payload["total"] == 0
        status, health = service.handle("GET", "/health")
        assert status == 200 and health["diary"]["total"] == 0

        status, prepared = service.handle(
            "POST", "/diary/prepare", body=json.dumps({"scope": "group", "force": True})
        )
        assert status == 200
        assert prepared["due"] is True
        assert prepared["prompt"]
        diary_id = prepared["diary_id"]

        status, report = service.handle(
            "POST",
            "/diary/finish",
            body=json.dumps({"diary_id": diary_id, "raw": DIARY_TEXT}, ensure_ascii=False),
        )
        assert status == 200 and report["status"] == "ok"
        assert report["entry_id"]

        status, listing = service.handle("GET", "/diary/list", query={"scope": "group"})
        assert status == 200 and listing["total"] == 1
        assert listing["items"][0]["scope_tag"] == "group"

        status, excerpts = service.handle("GET", "/diary/excerpts", query={"scope": "group"})
        assert status == 200 and excerpts["injected"] == 1
        assert excerpts["excerpts"][0]["content"] == DIARY_TEXT

        status, health = service.handle("GET", "/health")
        assert status == 200 and health["diary"]["total"] == 1
    finally:
        service.close()


def test_diary_http_errors(tmp_path) -> None:
    service = MemoryService(make_config(tmp_path))
    try:
        status, _ = service.handle("POST", "/diary/prepare", body=json.dumps({"scope": "mixed"}))
        assert status == 400
        status, _ = service.handle("GET", "/diary/excerpts", query={"scope": "mixed"})
        assert status == 400
        status, _ = service.handle("POST", "/diary/finish", body=json.dumps({"diary_id": "nope", "raw": "x"}))
        assert status == 404
        status, prepared = service.handle(
            "POST", "/diary/prepare", body=json.dumps({"scope": "dm", "force": True})
        )
        status, _ = service.handle(
            "POST", "/diary/finish", body=json.dumps({"diary_id": prepared["diary_id"], "raw": 123})
        )
        assert status == 400
    finally:
        service.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_diary_list_prompt_add(tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    raw_file = tmp_path / "diary.txt"
    raw_file.write_text(DIARY_TEXT, encoding="utf-8")

    code, out = run_cli(["diary", "list", "--data-dir", str(data_dir)], capsys)
    assert code == 0 and "暂无日记" in out

    code, out = run_cli(
        ["diary", "prompt", "--data-dir", str(data_dir), "--scope", "group", "--force"], capsys
    )
    assert code == 0
    assert "第一人称" in out

    code, out = run_cli(
        [
            "diary",
            "add",
            "--data-dir",
            str(data_dir),
            "--scope",
            "group",
            "--raw-file",
            str(raw_file),
            "--force",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "ok"

    code, out = run_cli(["diary", "list", "--data-dir", str(data_dir), "--json"], capsys)
    assert code == 0
    listing = json.loads(out)
    assert listing["total"] == 1
    assert listing["items"][0]["content"] == DIARY_TEXT

    code, out = run_cli(["status", "--data-dir", str(data_dir)], capsys)
    assert code == 0
    status_payload = json.loads(out)
    assert status_payload["diary"]["total"] == 1
