"""CLI：init / status / turn / search / list / maintain / metrics / obsidian。"""

from __future__ import annotations

import json

from qihun_memory.cli import main


def run_cli(argv: list[str], capsys) -> tuple[int, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out


def test_init_and_status(tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    code, out = run_cli(["init", "--data-dir", str(data_dir)], capsys)
    assert code == 0
    assert (data_dir / "memory.jsonl").is_file()
    assert (data_dir / "persona.md").is_file()
    assert (data_dir / "state.json").is_file()
    assert (data_dir / "users.json").is_file()

    code, out = run_cli(["status", "--data-dir", str(data_dir)], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["counts"]["total"] == 0


def test_turn_and_search_and_list(tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    raw_file = tmp_path / "raw.txt"
    raw_file.write_text(
        "好的，记下了。\n<MEMORY_JSON>\n"
        + json.dumps(
            {"memories": [{"type": "episode", "content": "用户A下周三去上海出差", "tags": ["出差"]}]},
            ensure_ascii=False,
        )
        + "\n</MEMORY_JSON>\n",
        encoding="utf-8",
    )
    code, out = run_cli(
        [
            "turn",
            "--data-dir",
            str(data_dir),
            "--user",
            "u_1",
            "--scope",
            "group",
            "--message",
            "我下周三去上海出差",
            "--raw-file",
            str(raw_file),
        ],
        capsys,
    )
    assert code == 0
    assert "好的，记下了。" in out

    code, out = run_cli(
        ["list", "--data-dir", str(data_dir), "--json"], capsys
    )
    assert code == 0
    items = json.loads(out)
    assert len(items) == 1
    assert items[0]["content"] == "用户A下周三去上海出差"

    code, out = run_cli(
        ["search", "上海出差", "--data-dir", str(data_dir), "--scope", "group", "--json"], capsys
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["selected"]
    assert payload["selected"][0]["content"] == "用户A下周三去上海出差"


def test_turn_prompt_only(tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    code, out = run_cli(
        [
            "turn",
            "--data-dir",
            str(data_dir),
            "--user",
            "u_1",
            "--scope",
            "group",
            "--message",
            "我下周三去上海出差",
        ],
        capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["gate"] is True
    assert payload["prompt"]["system"]
    assert payload["prompt"]["messages"]


def test_maintain_and_metrics(tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    code, out = run_cli(["maintain", "--data-dir", str(data_dir)], capsys)
    assert code == 0
    report = json.loads(out)
    assert report["rewritten"] is True

    code, out = run_cli(["metrics", "--data-dir", str(data_dir), "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert "counters" in payload
    assert "log_path" in payload


def test_obsidian_cli(tmp_path, capsys) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "persona.md").write_text("器魂人设", encoding="utf-8")
    code, out = run_cli(["obsidian", "--vault", str(vault), "list"], capsys)
    assert code == 0
    assert "persona.md" in out

    code, out = run_cli(["obsidian", "--vault", str(vault), "read", "persona.md"], capsys)
    assert code == 0
    assert "器魂人设" in out

    code, out = run_cli(["obsidian", "--vault", str(vault), "search", "器魂"], capsys)
    assert code == 0
    assert "persona.md" in out
