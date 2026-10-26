"""Obsidian 只读访问：只读、路径安全、无写入副作用。"""

from __future__ import annotations

import os
import time

import pytest

from qihun_memory.config import Config
from qihun_memory.obsidian import ReadOnlyObsidianVault, VaultError, vault_from_config


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "persona.md").write_text("# 器魂\n只读库", encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "memory.md").write_text("器魂记得 上海 出差", encoding="utf-8")
    (root / "notes" / "other.md").write_text("无关内容", encoding="utf-8")
    (root / "notes" / "data.txt").write_text("txt", encoding="utf-8")
    (root / "notes" / "image.png").write_bytes(b"\x89PNG")
    return root


def test_list_notes(vault) -> None:
    view = ReadOnlyObsidianVault(vault)
    notes = view.list_notes()
    assert notes == ["notes/memory.md", "notes/other.md", "persona.md"]
    assert all(not note.startswith(".obsidian") for note in notes)


def test_read_note_and_persona(vault) -> None:
    view = ReadOnlyObsidianVault(vault)
    assert "只读库" in view.read_note("persona.md")
    assert view.read_persona() == view.read_note("persona.md")
    assert view.read_persona("missing.md") == ""


def test_path_traversal_rejected(vault, tmp_path) -> None:
    view = ReadOnlyObsidianVault(vault)
    for bad in ["../outside.md", "notes/../../x.md", str(tmp_path / "x.md"), "", "notes"]:
        with pytest.raises(VaultError):
            view.read_note(bad)


def test_unsupported_filetype_rejected(vault) -> None:
    view = ReadOnlyObsidianVault(vault)
    with pytest.raises(VaultError):
        view.read_note("notes/image.png")


def test_search_readonly(vault) -> None:
    view = ReadOnlyObsidianVault(vault)
    hits = view.search("上海")
    assert len(hits) == 1
    assert hits[0]["path"] == "notes/memory.md"
    assert view.search("") == []


def test_no_write_side_effects(vault) -> None:
    view = ReadOnlyObsidianVault(vault)
    before = {}
    for path in vault.rglob("*"):
        if path.is_file():
            before[str(path)] = (os.stat(path).st_mtime_ns, path.read_bytes())
    time.sleep(0.01)
    for note in view.list_notes():
        view.read_note(note)
    view.search("器魂")
    after = {}
    for path in vault.rglob("*"):
        if path.is_file():
            after[str(path)] = (os.stat(path).st_mtime_ns, path.read_bytes())
    assert before == after
    assert view.readonly is True
    # 只读类不提供任何写入 API
    for name in ("write", "append", "delete", "remove", "save", "create"):
        assert not hasattr(view, name)


def test_vault_from_config(tmp_path) -> None:
    cfg = Config.from_dict(
        {"data_dir": str(tmp_path / "data"), "vault_dir": str(tmp_path / "vault")}, base_dir=tmp_path
    )
    view = vault_from_config(cfg)
    assert view is not None
    assert view.root == (tmp_path / "vault").resolve()
    empty = Config.from_dict({"data_dir": str(tmp_path / "data2")}, base_dir=tmp_path)
    assert vault_from_config(empty) is None
