"""Embedding 依赖（v1.4 §6.5）。

默认模型 `BAAI/bge-small-zh-v1.5`（512 维、CPU 可跑、无网络调用）。
未启用 / 后端不是 sentence-transformers / 加载失败时一律返回 NullEmbedder，
检索侧据此把 cos 权重置 0、bm25 权重置 1，并记 `retrieval.degraded`。

约束：
- import 本模块不做重 I/O，也不导入 sentence-transformers；
- 加载时禁止联网下载：优先本地 `model_path`，并用 HF 离线环境变量兜底；
- `encode` 返回的每个向量都是 L2 归一化的。
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import EmbeddingConfig

log = logging.getLogger("memory")

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIM = 512
_BATCH_SIZE = 32
_OFFLINE_ENV_KEYS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")

# 「只 warning 一次」：同一加载失败原因在进程内只提示一次，避免每轮刷日志。
_warned: set[str] = set()


class EmbeddingUnavailable(RuntimeError):
    """embedding 后端不可用（未安装 / 模型缺失 / 编码失败）。"""


class Embedder(ABC):
    """编码器接口。

    `available=False` 时检索降级为纯 BM25（§6.5）；`dim` 是向量维度，
    retrieval 用它校验 `MemoryItem.vector` 是否可用。
    """

    name: str = "null"
    dim: int = 0

    @property
    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """把一批文本编码为 L2 归一化向量；空输入返回 []。"""


class NullEmbedder(Embedder):
    """空编码器：不可用，`encode` 一律抛 `EmbeddingUnavailable`。"""

    name = "null"
    dim = 0

    def __init__(self) -> None:
        pass

    @property
    def available(self) -> bool:
        return False

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingUnavailable("embedding backend unavailable")

    def __repr__(self) -> str:
        return "NullEmbedder()"


def _import_sentence_transformers() -> Any:
    """延迟导入 sentence-transformers（未安装时抛 ImportError）。

    独立成函数既保证模块 import 阶段无重 I/O，也便于测试注入假实现。
    """
    import importlib

    return importlib.import_module("sentence_transformers")


@contextlib.contextmanager
def _offline_environment() -> Iterator[None]:
    """临时打开 HF 离线模式，禁止加载模型时联网下载（§6.5）。"""
    previous = {key: os.environ.get(key) for key in _OFFLINE_ENV_KEYS}
    for key in _OFFLINE_ENV_KEYS:
        os.environ[key] = "1"
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _warn_once(key: str, message: str, *args: Any) -> None:
    """同一个 key 只 warning 一次（失败降级不应刷屏）。"""
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message, *args)


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    """L2 归一化；零向量原样返回（避免除零）。"""
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0.0:
        return values
    return [value / norm for value in values]


class SentenceTransformerEmbedder(Embedder):
    """本地 sentence-transformers 编码器（默认 bge-small-zh-v1.5，512 维）。

    构造时同步加载模型；失败抛 `EmbeddingUnavailable`，由 `get_embedder` 降级。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        model_path: str = "",
        device: str = "cpu",
        dim: int = DEFAULT_DIM,
    ) -> None:
        self.model_name = str(model_name or "")
        self.model_path = str(model_path or "")
        self.device = str(device or "cpu")
        try:
            configured_dim = int(dim)
        except (TypeError, ValueError):
            configured_dim = DEFAULT_DIM
        self.dim = configured_dim if configured_dim > 0 else 0
        source = self.model_path or self.model_name
        label = Path(source).name if source else "unconfigured"
        self.name = f"sentence-transformers:{label}"
        self._model: Any = None
        self._model = self._load()
        self._sync_dim()

    # ---------- 加载 ----------

    def _load(self) -> Any:
        source = self.model_path or self.model_name
        if not source:
            raise EmbeddingUnavailable("embedding: no model configured")
        if self.model_path:
            local_path = Path(self.model_path).expanduser()
            if not local_path.exists():
                raise EmbeddingUnavailable(f"embedding: local model path not found: {local_path}")
            source = str(local_path)
        try:
            with _offline_environment():
                module = _import_sentence_transformers()
                factory = getattr(module, "SentenceTransformer")
                try:
                    return factory(source, device=self.device, local_files_only=True)
                except TypeError:
                    # 旧版 sentence-transformers 不认识 local_files_only 参数
                    return factory(source, device=self.device)
        except EmbeddingUnavailable:
            raise
        except Exception as exc:  # 导入失败 / 模型缺失 / 权重损坏……
            raise EmbeddingUnavailable(f"embedding: failed to load {source!r}: {exc}") from exc

    def _sync_dim(self) -> None:
        """以模型真实维度为准，避免配置写错维度和 sidecar 对不上。"""
        getter = getattr(self._model, "get_sentence_embedding_dimension", None)
        if not callable(getter):
            return
        try:
            actual = int(getter())
        except (TypeError, ValueError):
            return
        if actual > 0 and actual != self.dim:
            log.warning(
                "embedding: model dim %s != configured dim %s, using model dim",
                actual,
                self.dim,
            )
            self.dim = actual

    # ---------- 接口 ----------

    @property
    def available(self) -> bool:
        return self._model is not None

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            raise EmbeddingUnavailable("embedding model is not loaded")
        batch = [str(text or "") for text in texts]
        if not batch:
            return []
        try:
            raw = self._model.encode(
                batch,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
                batch_size=_BATCH_SIZE,
            )
        except Exception as exc:
            raise EmbeddingUnavailable(f"embedding: encode failed: {exc}") from exc
        vectors: list[list[float]] = []
        try:
            for vector in raw:
                vectors.append([float(value) for value in vector])
        except (TypeError, ValueError) as exc:
            raise EmbeddingUnavailable(f"embedding: invalid encode output: {exc}") from exc
        if len(vectors) != len(batch):
            raise EmbeddingUnavailable(f"embedding: expected {len(batch)} vectors, got {len(vectors)}")
        return [_l2_normalize(vector) for vector in vectors]

    def __repr__(self) -> str:
        return f"SentenceTransformerEmbedder(name={self.name!r}, dim={self.dim}, available={self.available})"


def get_embedder(cfg: EmbeddingConfig) -> Embedder:
    """按配置构造编码器；任何失败都降级为 NullEmbedder（§6.5）。

    - `enabled=False` 或 `backend != "sentence-transformers"` → NullEmbedder；
    - 否则尝试加载本地/已安装的 sentence-transformers。禁止联网下载：
      优先 `model_path`，模型名只在库已安装且用户配置了 model 时尝试；
    - 加载失败 → NullEmbedder，并且同一原因只 warning 一次。
    """
    if not getattr(cfg, "enabled", False):
        return NullEmbedder()
    backend = str(getattr(cfg, "backend", "") or "").strip().lower()
    if backend != "sentence-transformers":
        return NullEmbedder()
    model_name = str(getattr(cfg, "model", "") or "")
    model_path = str(getattr(cfg, "model_path", "") or "")
    device = str(getattr(cfg, "device", "") or "cpu")
    try:
        dim = int(getattr(cfg, "dim", DEFAULT_DIM))
    except (TypeError, ValueError):
        dim = DEFAULT_DIM
    warning_key = f"{backend}|{model_name}|{model_path}"
    try:
        return SentenceTransformerEmbedder(
            model_name=model_name,
            model_path=model_path,
            device=device,
            dim=dim,
        )
    except Exception as exc:
        _warn_once(
            warning_key,
            "embedding: %s unavailable, retrieval degrades to bm25: %s (%s)",
            backend,
            exc,
            type(exc).__name__,
        )
        return NullEmbedder()
