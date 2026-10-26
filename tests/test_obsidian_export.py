"""Obsidian 只读视图导出：内容、幂等、scope 拆分、归档、路径安全、CLI。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qihun_memory.cli import main
from qihun_memory.config import Config
from qihun_memory.memory import MemorySystem
from qihun_memory.obsidian_export import ExportError, export_obsidian_view
from qihun_memory.summary import RollingSummary


def build_config(tmp_path: Path, vault: Path | None = None) -> Config:
    data = {
        "data_dir": str(tmp_path / "data"),
        "embedding": {"enabled": False, "backend": "none", "dim": 8},
    }
    if vault is not None:
        data["vault_dir"] = str(vault)
    cfg = Config.from_dict(data, base_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def seed_data(cfg: Config) -> None:
    system = MemorySystem(cfg)
    try:
        system.remember(
            [
                {"type": "fact", "content": "管理员喜欢大白饭和鲸鱼表情包", "tags": ["rice"]},
                {"type": "episode", "content": "2026-10-25 事件：识图上限实测成功"},
            ],
            user_id="g1",
            scope="group",
            valence=0.5,
        )
        system.remember(
            [{"type": "preference", "content": "私聊里喜欢被叫姜小鱼"}],
            user_id="u1",
            scope="dm",
            valence=0.2,
        )
        system.users.refresh({"g1": "群记忆", "u1": "管理员"})
        cfg.bot_state_file.write_text(
            json.dumps(
                {"valence": 0.4, "avoidance": 0.0, "updated_at": "2026-10-26T20:00:00+08:00"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        RollingSummary(cfg.summary_file, cfg.prompt).update("group", "今天吃什么", "大白饭")
    finally:
        system.close()


def test_export_creates_view_files(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    seed_data(cfg)
    out = tmp_path / "vault" / "器魂记忆"
    report = export_obsidian_view(cfg, out)

    assert report.items == 3
    assert report.archived == 0
    for name in (
        "00_器魂记忆说明.md",
        "10_群聊记忆.md",
        "11_私聊记忆.md",
        "20_滚动摘要.md",
        "30_用户与状态.md",
        "40_已归档记忆.md",
    ):
        assert (out / name).is_file(), name
    group_text = (out / "10_群聊记忆.md").read_text(encoding="utf-8")
    assert "管理员喜欢大白饭和鲸鱼表情包" in group_text
    assert "scope" not in group_text  # 视图面向人，不暴露内部字段名
    dm_text = (out / "11_私聊记忆.md").read_text(encoding="utf-8")
    assert "私聊里喜欢被叫姜小鱼" in dm_text
    summary_text = (out / "20_滚动摘要.md").read_text(encoding="utf-8")
    assert "RollingSummary" not in summary_text
    assert "今天吃什么" in summary_text
    users_state = (out / "30_用户与状态.md").read_text(encoding="utf-8")
    assert "管理员" in users_state and "群记忆" in users_state
    assert "0.40" in users_state
    readme = (out / "00_器魂记忆说明.md").read_text(encoding="utf-8")
    assert "只读视图" in readme
    assert "readonly: true" in readme


def test_export_is_idempotent(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    seed_data(cfg)
    out = tmp_path / "view"
    first = export_obsidian_view(cfg, out)
    assert first.written == 6
    assert first.updated == 0
    snapshot = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    second = export_obsidian_view(cfg, out)
    assert second.written == 0
    assert second.updated == 0
    assert second.unchanged == 6
    assert {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()} == snapshot


def test_export_includes_archived_items(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    seed_data(cfg)
    system = MemorySystem(cfg)
    target = system.all_items()[0]
    assert system.store.archive([target.id]) == 1
    system.close()

    out = tmp_path / "view"
    report = export_obsidian_view(cfg, out)
    assert report.archived == 1
    archived = (out / "40_已归档记忆.md").read_text(encoding="utf-8")
    assert target.id in archived
    assert target.content in archived


def test_export_refuses_output_inside_data_dir(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    with pytest.raises(ExportError):
        export_obsidian_view(cfg, cfg.data_path)
    with pytest.raises(ExportError):
        export_obsidian_view(cfg, Path(cfg.data_path) / "view")


def test_cli_export_obsidian(tmp_path: Path, capsys) -> None:
    cfg = build_config(tmp_path)
    seed_data(cfg)
    out = tmp_path / "view"
    code = main(["export-obsidian", "--data-dir", str(cfg.data_path), "--out", str(out), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["items"] == 3
    assert (out / "10_群聊记忆.md").is_file()


def test_cli_export_without_vault_or_out_fails(tmp_path: Path, capsys) -> None:
    cfg = build_config(tmp_path)
    code = main(["export-obsidian", "--data-dir", str(cfg.data_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "vault_dir" in captured.err


def test_export_uses_vault_dir_as_default(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "data_dir": str(tmp_path / "data"),
                "vault_dir": str(vault),
                "embedding": {"enabled": False, "backend": "none", "dim": 8},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    from qihun_memory.config import load_config

    cfg = load_config(config_file)
    seed_data(cfg)
    code = main(["export-obsidian", "-c", str(config_file), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["out_dir"].endswith("器魂记忆")
    assert (vault / "器魂记忆" / "00_器魂记忆说明.md").is_file()
