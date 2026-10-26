"""pytest 公共夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from qihun_memory.config import Config


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def config(tmp_path: Path, data_dir: Path) -> Config:
    cfg = Config.from_dict(
        {
            "data_dir": str(data_dir),
            "embedding": {"enabled": False, "backend": "none", "dim": 8},
        },
        base_dir=tmp_path,
    )
    cfg.ensure_dirs()
    return cfg
