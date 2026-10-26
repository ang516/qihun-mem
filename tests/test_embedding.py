"""embedding.py 测试（v1.4 §6.5）。

覆盖：NullEmbedder 契约、get_embedder 降级路径（禁用/其它后端/导入失败/模型缺失）、
本地模型加载、L2 归一化、离线加载（禁止联网）、warning 只一次、模块 import 不加载
sentence-transformers。

不依赖 sentence-transformers、不联网：用注入的假模块验证加载路径。
"""

from __future__ import annotations

import inspect
import logging
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qihun_memory import embedding
from qihun_memory.config import EmbeddingConfig
from qihun_memory.embedding import (
    DEFAULT_DIM,
    DEFAULT_MODEL,
    Embedder,
    EmbeddingUnavailable,
    NullEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
)


# --------------------------------------------------------------------------
# 测试替身（自包含，不依赖 conftest / sentence-transformers）
# --------------------------------------------------------------------------


class FakeModel:
    """假 sentence-transformers 模型：按文本查表返回未归一化向量。"""

    def __init__(
        self,
        vectors: dict[str, list[float]] | None = None,
        *,
        dim: int | None = None,
        fail: bool = False,
        short: bool = False,
        garbage: bool = False,
    ) -> None:
        self.vectors = dict(vectors or {})
        self.dim = dim
        self.fail = fail
        self.short = short
        self.garbage = garbage

    def get_sentence_embedding_dimension(self):
        if self.dim is None:
            raise RuntimeError("dim unknown")
        return self.dim

    def encode(self, texts, **kwargs):
        if self.fail:
            raise RuntimeError("encode boom")
        if self.garbage:
            return [["not-a-number"] for _ in texts]
        vectors = [list(self.vectors[text]) for text in texts]
        if self.short and vectors:
            vectors = vectors[:-1]
        return vectors


def install_fake_backend(monkeypatch, model: FakeModel) -> dict:
    """注入假 sentence_transformers 模块，返回捕获到的调用信息。"""
    captured: dict = {}

    class FakeSentenceTransformer:
        def __init__(self, source, device="cpu", **kwargs):
            captured["source"] = source
            captured["device"] = device
            captured["kwargs"] = kwargs
            captured["offline_hf"] = os.environ.get("HF_HUB_OFFLINE")
            captured["offline_transformers"] = os.environ.get("TRANSFORMERS_OFFLINE")
            self.model = model

        def encode(self, texts, **kwargs):
            captured["encode_kwargs"] = kwargs
            return self.model.encode(texts, **kwargs)

        def get_sentence_embedding_dimension(self):
            return self.model.get_sentence_embedding_dimension()

    module = SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
    monkeypatch.setattr(embedding, "_import_sentence_transformers", lambda: module)
    monkeypatch.setattr(embedding, "_warned", set())
    return captured


def fake_import_failure():
    def _raise():
        raise ImportError("sentence_transformers is not installed")

    return _raise


# --------------------------------------------------------------------------
# 1. NullEmbedder / Embedder 契约
# --------------------------------------------------------------------------


def test_null_embedder_contract():
    null = NullEmbedder()
    assert isinstance(null, Embedder)
    assert null.available is False
    assert null.dim == 0
    assert null.name == "null"
    with pytest.raises(EmbeddingUnavailable):
        null.encode(["上海"])
    with pytest.raises(EmbeddingUnavailable):
        null.encode([])


def test_embedder_is_abstract():
    with pytest.raises(TypeError):
        Embedder()  # type: ignore[abstract]


def test_default_signature_matches_contract():
    params = inspect.signature(SentenceTransformerEmbedder.__init__).parameters
    assert params["model_name"].default == DEFAULT_MODEL == "BAAI/bge-small-zh-v1.5"
    assert params["model_path"].default == ""
    assert params["device"].default == "cpu"
    assert params["dim"].default == DEFAULT_DIM == 512


# --------------------------------------------------------------------------
# 2. get_embedder 降级路径
# --------------------------------------------------------------------------


def test_get_embedder_disabled_or_other_backend_returns_null():
    assert isinstance(get_embedder(EmbeddingConfig(enabled=False)), NullEmbedder)
    assert isinstance(get_embedder(EmbeddingConfig(backend="none")), NullEmbedder)
    assert isinstance(get_embedder(EmbeddingConfig(backend="")), NullEmbedder)


def test_get_embedder_disabled_does_not_import_sentence_transformers(monkeypatch):
    def _must_not_import():
        raise AssertionError("disabled embedding must not import sentence-transformers")

    monkeypatch.setattr(embedding, "_import_sentence_transformers", _must_not_import)
    assert isinstance(get_embedder(EmbeddingConfig(enabled=False)), NullEmbedder)


def test_get_embedder_import_failure_returns_null_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(embedding, "_warned", set())
    monkeypatch.setattr(embedding, "_import_sentence_transformers", fake_import_failure())
    caplog.set_level(logging.WARNING, logger="memory")

    first = get_embedder(EmbeddingConfig())
    second = get_embedder(EmbeddingConfig())

    assert isinstance(first, NullEmbedder)
    assert isinstance(second, NullEmbedder)
    warnings = [
        record
        for record in caplog.records
        if record.name == "memory" and "degrades to bm25" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_get_embedder_missing_local_model_path_returns_null(monkeypatch, tmp_path, caplog):
    install_fake_backend(monkeypatch, FakeModel(dim=2))
    caplog.set_level(logging.WARNING, logger="memory")
    missing = tmp_path / "missing-model"
    embedder = get_embedder(EmbeddingConfig(model_path=str(missing), dim=2))
    assert isinstance(embedder, NullEmbedder)
    assert any("degrades to bm25" in record.getMessage() for record in caplog.records)


def test_get_embedder_empty_model_configuration_returns_null(monkeypatch, caplog):
    install_fake_backend(monkeypatch, FakeModel(dim=2))
    caplog.set_level(logging.WARNING, logger="memory")
    embedder = get_embedder(EmbeddingConfig(model="", model_path="", dim=2))
    assert isinstance(embedder, NullEmbedder)
    assert any("degrades to bm25" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------
# 3. 本地模型加载与编码
# --------------------------------------------------------------------------


def test_sentence_transformer_embedder_loads_local_path(monkeypatch, tmp_path):
    model = FakeModel({"a": [3.0, 4.0], "b": [0.0, 5.0]}, dim=2)
    captured = install_fake_backend(monkeypatch, model)
    embedder = get_embedder(EmbeddingConfig(model_path=str(tmp_path), dim=2, device="cpu"))

    assert isinstance(embedder, SentenceTransformerEmbedder)
    assert embedder.available is True
    assert embedder.dim == 2
    assert embedder.name.startswith("sentence-transformers:")
    assert captured["source"] == str(tmp_path)
    assert captured["device"] == "cpu"

    vectors = embedder.encode(["a", "b"])
    assert vectors[0] == pytest.approx([0.6, 0.8])
    assert vectors[1] == pytest.approx([0.0, 1.0])
    for vector in vectors:
        assert math.sqrt(sum(value * value for value in vector)) == pytest.approx(1.0)
    assert embedder.encode([]) == []

    encode_kwargs = captured["encode_kwargs"]
    assert encode_kwargs["normalize_embeddings"] is True
    assert encode_kwargs["show_progress_bar"] is False


def test_encode_normalizes_unnormalized_model_output(monkeypatch, tmp_path):
    model = FakeModel({"x": [3.0, 4.0, 0.0], "zero": [0.0, 0.0, 0.0]}, dim=3)
    install_fake_backend(monkeypatch, model)
    embedder = SentenceTransformerEmbedder(model_path=str(tmp_path), dim=3)
    vectors = embedder.encode(["x", "zero"])
    assert vectors[0] == pytest.approx([0.6, 0.8, 0.0])
    assert vectors[1] == [0.0, 0.0, 0.0]  # 零向量不除零


def test_model_dim_overrides_misconfigured_dim(monkeypatch, tmp_path):
    model = FakeModel({"a": [1.0, 0.0]}, dim=2)
    install_fake_backend(monkeypatch, model)
    embedder = SentenceTransformerEmbedder(model_path=str(tmp_path), dim=512)
    assert embedder.dim == 2


def test_sentence_transformer_embedder_requires_existing_path(monkeypatch, tmp_path):
    install_fake_backend(monkeypatch, FakeModel(dim=2))
    with pytest.raises(EmbeddingUnavailable):
        SentenceTransformerEmbedder(model_path=str(tmp_path / "nope"))


def test_sentence_transformer_embedder_requires_model_configuration():
    with pytest.raises(EmbeddingUnavailable):
        SentenceTransformerEmbedder(model_name="", model_path="")


# --------------------------------------------------------------------------
# 4. 编码失败与异常输出 → EmbeddingUnavailable
# --------------------------------------------------------------------------


def test_encode_failure_raises_embedding_unavailable(monkeypatch, tmp_path):
    install_fake_backend(monkeypatch, FakeModel({"a": [1.0, 0.0]}, dim=2, fail=True))
    embedder = SentenceTransformerEmbedder(model_path=str(tmp_path), dim=2)
    with pytest.raises(EmbeddingUnavailable):
        embedder.encode(["a"])


def test_encode_garbage_output_raises_embedding_unavailable(monkeypatch, tmp_path):
    install_fake_backend(monkeypatch, FakeModel(dim=2, garbage=True))
    embedder = SentenceTransformerEmbedder(model_path=str(tmp_path), dim=2)
    with pytest.raises(EmbeddingUnavailable):
        embedder.encode(["a"])


def test_encode_vector_count_mismatch_raises_embedding_unavailable(monkeypatch, tmp_path):
    install_fake_backend(
        monkeypatch,
        FakeModel({"a": [1.0, 0.0], "b": [0.0, 1.0]}, dim=2, short=True),
    )
    embedder = SentenceTransformerEmbedder(model_path=str(tmp_path), dim=2)
    with pytest.raises(EmbeddingUnavailable):
        embedder.encode(["a", "b"])


# --------------------------------------------------------------------------
# 5. 离线加载（禁止联网）与 import 开销
# --------------------------------------------------------------------------


def test_load_uses_offline_env_and_restores_it(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    captured = install_fake_backend(monkeypatch, FakeModel({"a": [1.0, 0.0]}, dim=2))

    embedder = get_embedder(EmbeddingConfig(model_path=str(tmp_path), dim=2))
    assert embedder.available is True
    assert captured["offline_hf"] == "1"
    assert captured["offline_transformers"] == "1"
    # 加载完成必须恢复调用前的环境，不能污染进程
    assert "HF_HUB_OFFLINE" not in os.environ
    assert "TRANSFORMERS_OFFLINE" not in os.environ


def test_offline_env_preserves_pre_existing_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    captured = install_fake_backend(monkeypatch, FakeModel({"a": [1.0, 0.0]}, dim=2))
    get_embedder(EmbeddingConfig(model_path=str(tmp_path), dim=2))
    assert captured["offline_hf"] == "1"
    assert os.environ.get("HF_HUB_OFFLINE") == "0"


def test_module_import_does_not_import_sentence_transformers():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(root / 'src')!r}); "
        "import qihun_memory.embedding; "
        "print('sentence_transformers' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"
