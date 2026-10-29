"""本地 HTTP JSON API（给 qq-bridge 等 Node 宿主调用）。

设计原则：
- 单实例单写者：所有请求用一把 RLock 串行化；
- 两阶段回合：POST /turn/prepare → 宿主调用 LLM → POST /turn/finish；
- Obsidian 接口只读（/obsidian/notes、/obsidian/note），没有任何写接口；
- 默认只绑 127.0.0.1；配置了 service.token 时要求 Bearer 鉴权。

端点：
  GET  /health
  GET  /state
  GET  /user_affect?user_id=
  GET  /metrics
  GET  /memory/list?scope=&user_id=&status=&limit=
  GET  /memory/search?q=&scope=&user_id=
  POST /turn/prepare  {user_id, scope, message, msgid?, at_bot?, reply_to_bot?, nickname?}
  POST /turn/finish   {turn_id, raw}
  GET  /diary/list?scope=&limit=
  GET  /diary/excerpts?scope=
  POST /diary/prepare {scope, force?}
  POST /diary/finish  {diary_id, raw}
  GET  /obsidian/notes
  GET  /obsidian/note?path=
  POST /users/refresh {members: {user_id: nickname}}
  POST /maintain
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .diary import DIARY_SCOPES
from .memory import MemorySystem
from .obsidian import ReadOnlyObsidianVault, VaultError, vault_from_config
from .pipeline import PreparedTurn, TurnEvent, TurnPipeline, retrieval_summary
from .prompt import EXTRACTION_PROTOCOL
from .tokens import estimate_tokens
from .version import __version__

log = logging.getLogger("memory")

MAX_BODY_BYTES = 2 * 1024 * 1024


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class MemoryService:
    """与传输层解耦的请求处理器（便于单测）。"""

    def __init__(
        self,
        cfg: Config,
        *,
        system: MemorySystem | None = None,
        pipeline: TurnPipeline | None = None,
        startup_maintenance: bool = False,
    ) -> None:
        self.cfg = cfg
        self.system = system or MemorySystem(cfg)
        self.pipeline = pipeline or TurnPipeline(self.system, cfg)
        self.vault: ReadOnlyObsidianVault | None = vault_from_config(cfg)
        self._lock = threading.RLock()
        self._prepared: dict[str, PreparedTurn] = {}
        # 两阶段日记生成（宿主持有 LLM）：POST /diary/prepare → POST /diary/finish
        self._diary_prepared: dict[str, Any] = {}
        # §3.5 启动后补偿执行一次维护任务；失败只记指标，不阻塞服务启动
        if startup_maintenance:
            try:
                self.system.maintain()
            except Exception as exc:  # pragma: no cover - 防御性兜底
                log.warning("startup maintenance failed: %r", exc)

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
    ) -> tuple[int, dict]:
        method = method.upper()
        query = query or {}
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        try:
            with self._lock:
                self._check_auth(headers)
                return self._route(method, path.rstrip("/") or "/", query, body)
        except ServiceError as exc:
            return exc.status, {"ok": False, "error": str(exc)}
        except Exception as exc:  # 任何未预期异常都不外泄栈
            log.exception("service internal error")
            return 500, {"ok": False, "error": type(exc).__name__}

    def _check_auth(self, headers: dict[str, str]) -> None:
        token = self.cfg.service.token
        if not token:
            return
        provided = headers.get("authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(provided, expected):
            raise ServiceError(401, "unauthorized")

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def _route(self, method: str, path: str, query: dict[str, str], body) -> tuple[int, dict]:
        if path == "/health" and method == "GET":
            diary_counts: dict = {}
            try:
                diary_store = getattr(self.system, "diary", None)
                if diary_store is not None:
                    diary_counts = diary_store.counts()
            except Exception:  # 健康检查不允许因叙事层异常而失败
                diary_counts = {}
            return 200, {
                "ok": True,
                "version": __version__,
                "items": self.system.store.counts(),
                "diary": diary_counts,
                "embedder_available": bool(self.system.embedder.available),
                "vault": str(self.vault.root) if self.vault else "",
            }
        if path == "/state" and method == "GET":
            return 200, self._state_payload()
        if path == "/user_affect" and method == "GET":
            return 200, self._user_affect_payload(query)
        if path == "/metrics" and method == "GET":
            return 200, {"ok": True, "metrics": self.system.metrics.snapshot()}
        if path == "/memory/list" and method == "GET":
            return 200, self._list_memories(query)
        if path == "/memory/search" and method == "GET":
            return 200, self._search_memories(query)

        if path == "/diary/list" and method == "GET":
            return 200, self._diary_list(query)
        if path == "/diary/excerpts" and method == "GET":
            return 200, self._diary_excerpts(query)

        if path == "/turn/prepare" and method == "POST":
            return 200, self._prepare_turn(self._json_body(body))
        if path == "/turn/finish" and method == "POST":
            return 200, self._finish_turn(self._json_body(body))

        if path == "/diary/prepare" and method == "POST":
            return 200, self._diary_prepare(self._json_body(body))
        if path == "/diary/finish" and method == "POST":
            return 200, self._diary_finish(self._json_body(body))

        if path == "/obsidian/notes" and method == "GET":
            return 200, self._obsidian_notes()
        if path == "/obsidian/note" and method == "GET":
            return 200, self._obsidian_note(query)

        if path == "/users/refresh" and method == "POST":
            payload = self._json_body(body)
            members = payload.get("members")
            if not isinstance(members, dict):
                raise ServiceError(400, "members must be an object")
            changed = self.system.refresh_users({str(k): str(v) for k, v in members.items()})
            return 200, {"ok": True, "changed": changed, "users": len(self.system.users.snapshot())}
        if path == "/maintain" and method == "POST":
            report = self.system.maintain()
            return 200, {"ok": True, "report": report.__dict__}
        raise ServiceError(404, f"not found: {method} {path}")

    # ------------------------------------------------------------------
    # 处理函数
    # ------------------------------------------------------------------

    @staticmethod
    def _json_body(body) -> dict:
        if body is None or body == b"" or body == "":
            return {}
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ServiceError(400, "body must be UTF-8") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceError(400, "invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ServiceError(400, "body must be a JSON object")
        return payload

    def _state_payload(self) -> dict:
        state = self.system.state()
        return {
            "valence": state.valence,
            "avoidance": state.avoidance,
            "updated_at": state.updated_at,
            "stale": state.stale,
            "source": state.source,
            "reason": state.reason,
        }

    def _user_affect_payload(self, query: dict[str, str]) -> dict:
        """用户感情快照（唯一入口 UserAffectStore.get；不做隐式 decay）。"""
        user_id = (query.get("user_id") or "").strip()
        if not user_id:
            raise ServiceError(400, "user_id is required")
        affect = self.system.affect_store.get(user_id)
        payload = affect.to_dict()
        payload["user_id"] = user_id
        payload["ok"] = True
        return payload

    def _list_memories(self, query: dict[str, str]) -> dict:
        scope = (query.get("scope") or "").strip()
        user_id = (query.get("user_id") or "").strip()
        status = (query.get("status") or "").strip()
        try:
            limit = min(500, max(1, int(query.get("limit") or 100)))
        except ValueError as exc:
            raise ServiceError(400, "limit must be an integer") from exc
        items = self.system.all_items()
        if scope:
            items = [item for item in items if item.scope == scope]
        if user_id:
            items = [item for item in items if item.user_id == user_id]
        if status:
            items = [item for item in items if item.status == status]
        items.sort(key=lambda item: item.id)
        return {
            "ok": True,
            "total": len(items),
            "items": [self._item_payload(item) for item in items[:limit]],
        }

    def _search_memories(self, query: dict[str, str]) -> dict:
        text = (query.get("q") or "").strip()
        scope = (query.get("scope") or "group").strip()
        user_id = (query.get("user_id") or "").strip()
        if not text:
            raise ServiceError(400, "q is required")
        if scope not in ("group", "dm"):
            raise ServiceError(400, "scope must be group or dm")
        result = self.system.search(text, scope, user_id)
        summary = retrieval_summary(result)
        # §2.11：search payload 增加 bridge / dedup 摘要（与检索指标同名同源）
        summary["bridge"] = {
            "hit": int(getattr(result, "bridge_hit", 0) or 0),
            "candidates": int(getattr(result, "bridge_candidates", 0) or 0),
            "truncated": bool(getattr(result, "bridge_truncated", False)),
        }
        summary["dedup"] = {
            "removed": int(getattr(result, "dedup_removed", 0) or 0),
            "degraded": bool(getattr(result, "dedup_degraded", False)),
        }
        return {
            "ok": True,
            "retrieval": summary,
            "selected": [
                {
                    "id": entry.item.id,
                    "user_id": entry.item.user_id,
                    "type": entry.item.type,
                    "content": entry.item.content,
                    "score": round(entry.score, 6),
                    "r": round(entry.r, 6),
                    "cos": round(entry.cos, 6),
                    "bm25": round(entry.bm25, 6),
                    "status": entry.item.status,
                    "affect": dict(entry.item.affect) if entry.item.affect is not None else None,
                    "bridge": bool(getattr(entry, "bridge", False)),
                    "source_pool": str(getattr(entry, "source_pool", "") or ""),
                }
                for entry in result.selected
            ],
        }

    def _prepare_turn(self, payload: dict) -> dict:
        user_id = str(payload.get("user_id") or "").strip()
        scope = str(payload.get("scope") or "").strip()
        message = payload.get("message")
        if not user_id:
            raise ServiceError(400, "user_id is required")
        if scope not in ("group", "dm"):
            raise ServiceError(400, "scope must be group or dm")
        if not isinstance(message, str) or not message.strip():
            raise ServiceError(400, "message is required")
        event = TurnEvent(
            user_id=user_id,
            scope=scope,
            message=message,
            msgid=str(payload.get("msgid") or ""),
            at_bot=bool(payload.get("at_bot")),
            reply_to_bot=bool(payload.get("reply_to_bot")),
            nickname=str(payload.get("nickname") or ""),
        )
        prepared = self.pipeline.prepare(event)
        # 宿主把当前昵称带进来时顺手刷新 users.json（昵称会变、user_id 不变，§2.3）
        if event.nickname:
            try:
                self.system.refresh_users({event.user_id: event.nickname})
            except Exception:  # 昵称写盘失败不影响本轮
                log.debug("users.json refresh failed", exc_info=True)
        self._prepared[prepared.turn_id] = prepared
        while len(self._prepared) > TurnPipeline.MAX_PREPARED:
            self._prepared.pop(next(iter(self._prepared)))
        return {
            "ok": True,
            "turn_id": prepared.turn_id,
            "turn_key": prepared.turn_key,
            "gate": prepared.gate_passed,
            "light_path": prepared.light_path,
            # 给宿主（qq-bridge 等）直接使用的注入素材：不包含当前消息，避免重复喂
            "injection": {
                "system_prompt": prepared.prompt.system_prompt,
                "memory_block": prepared.prompt.memory_block,
                "summary": prepared.summary_text,
                "diary_block": prepared.prompt.diary_block,
                "diary_tokens": estimate_tokens(prepared.prompt.diary_block)
                if prepared.prompt.diary_block
                else 0,
                "protocol": EXTRACTION_PROTOCOL if prepared.gate_passed else "",
                "memory_ids": list(prepared.prompt.memory_ids),
            },
            "retrieval": retrieval_summary(prepared.retrieval),
            "prompt": prepared.prompt.to_dict(),
            "state": {
                "valence": prepared.bot_state.valence,
                "avoidance": prepared.bot_state.avoidance,
                "stale": prepared.bot_state.stale,
            },
        }

    def _finish_turn(self, payload: dict) -> dict:
        turn_id = str(payload.get("turn_id") or "")
        raw = payload.get("raw")
        if not turn_id:
            raise ServiceError(400, "turn_id is required")
        prepared = self._prepared.get(turn_id)
        if prepared is None:
            raise ServiceError(404, f"unknown or already finished turn: {turn_id}")
        # 成功后才清理：finish 异常时保留该 turn，宿主可原样重试（A2）
        result = self.pipeline.finish(prepared, raw if isinstance(raw, str) else "")
        self._prepared.pop(turn_id, None)
        return {"ok": True, **result.to_dict()}

    # ------------------------------------------------------------------
    # 叙事层（v1.6 §8）：日记只读查询 + 两阶段生成（LLM 由宿主持有）
    # ------------------------------------------------------------------

    @staticmethod
    def _diary_entry_payload(entry) -> dict:
        return {
            "id": entry.id,
            "date": entry.date,
            "scope_tag": entry.scope_tag,
            "content": entry.content,
            "tokens": int(entry.tokens),
            "turn_range": list(entry.turn_range),
            "created_at": entry.created_at,
        }

    def _diary_store(self):
        store = getattr(self.system, "diary", None)
        if store is None:
            raise ServiceError(503, "diary store unavailable")
        return store

    def _diary_list(self, query: dict[str, str]) -> dict:
        scope = (query.get("scope") or "").strip()
        if scope and scope not in DIARY_SCOPES:
            raise ServiceError(400, "scope must be group or dm")
        try:
            limit = min(200, max(1, int(query.get("limit") or 50)))
        except ValueError as exc:
            raise ServiceError(400, "limit must be an integer") from exc
        store = self._diary_store()
        entries = [entry for entry in store.entries.values() if not scope or entry.scope_tag == scope]
        entries.sort(key=lambda entry: (entry.created_at, entry.id), reverse=True)
        return {
            "ok": True,
            "total": len(entries),
            "counts": store.counts(),
            "items": [self._diary_entry_payload(entry) for entry in entries[:limit]],
        }

    def _diary_excerpts(self, query: dict[str, str]) -> dict:
        scope = (query.get("scope") or "").strip()
        if scope not in DIARY_SCOPES:
            raise ServiceError(400, "scope must be group or dm")
        picked = self.system.select_diary_excerpts(scope)
        return {
            "ok": True,
            "scope_tag": scope,
            "injected": int(picked.get("injected", 0) or 0),
            "tokens": int(picked.get("tokens", 0) or 0),
            "truncated": bool(picked.get("truncated", False)),
            "excerpts": [
                self._diary_entry_payload(entry) for entry in picked.get("excerpts") or []
            ],
        }

    def _diary_worker(self):
        worker = getattr(self.system, "diary_worker", None)
        if worker is None:
            raise ServiceError(503, "diary worker unavailable")
        return worker

    def _diary_prepare(self, payload: dict) -> dict:
        scope = str(payload.get("scope") or "").strip()
        if scope not in DIARY_SCOPES:
            raise ServiceError(400, "scope must be group or dm")
        worker = self._diary_worker()
        prepared = worker.prepare(scope, force=bool(payload.get("force")))
        diary_id = uuid.uuid4().hex
        self._diary_prepared[diary_id] = prepared
        while len(self._diary_prepared) > TurnPipeline.MAX_PREPARED:
            self._diary_prepared.pop(next(iter(self._diary_prepared)))
        return {
            "ok": True,
            "diary_id": diary_id,
            "scope_tag": prepared.scope_tag,
            "due": bool(prepared.due),
            "date": prepared.date,
            "turn_range": list(prepared.turn_range),
            "reason": prepared.reason,
            "prompt": prepared.prompt,
        }

    def _diary_finish(self, payload: dict) -> dict:
        diary_id = str(payload.get("diary_id") or "").strip()
        raw = payload.get("raw")
        if not diary_id:
            raise ServiceError(400, "diary_id is required")
        prepared = self._diary_prepared.get(diary_id)
        if prepared is None:
            raise ServiceError(404, f"unknown or already finished diary: {diary_id}")
        if not isinstance(raw, str):
            raise ServiceError(400, "raw must be a string")
        report = self._diary_worker().finish(prepared, raw)
        self._diary_prepared.pop(diary_id, None)
        return {
            "ok": True,
            "scope_tag": report.scope_tag,
            "status": report.status,
            "reason": report.reason,
            "entry_id": report.entry_id,
            "tokens": int(report.tokens),
        }

    def _obsidian_notes(self) -> dict:
        if self.vault is None:
            raise ServiceError(400, "obsidian vault not configured (vault_dir)")
        return {"ok": True, "readonly": True, "notes": self.vault.list_notes()}

    def _obsidian_note(self, query: dict[str, str]) -> dict:
        if self.vault is None:
            raise ServiceError(400, "obsidian vault not configured (vault_dir)")
        relative = (query.get("path") or "").strip()
        if not relative:
            raise ServiceError(400, "path is required")
        try:
            content = self.vault.read_note(relative)
        except VaultError as exc:
            raise ServiceError(400, str(exc)) from exc
        return {"ok": True, "readonly": True, "path": relative, "content": content}

    @staticmethod
    def _item_payload(item) -> dict:
        return {
            "id": item.id,
            "user_id": item.user_id,
            "scope": item.scope,
            "type": item.type,
            "content": item.content,
            "created_at": item.created_at,
            "last_access": item.last_access,
            "access_count": item.access_count,
            "mention_count": item.mention_count,
            "S": round(item.S, 6),
            "valence": round(item.valence, 6),
            "status": item.status,
            "tags": list(item.tags),
            # §2.11 / §3.1：affect 事件级标签（缺失时为 None，老数据兼容）
            "affect": dict(item.affect) if item.affect is not None else None,
        }

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self.pipeline.close()


# --------------------------------------------------------------------------
# HTTP 传输层
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "QiHunMemory/" + __version__

    def __init__(self, request, client_address, server) -> None:
        self.service: MemoryService = server.service
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._write(413, {"ok": False, "error": "body too large"})
            return
        body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        status, payload = self.service.handle(method, parsed.path, query, headers, body)
        self._write(status, payload)

    def _write(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("http %s - %s", self.address_string(), fmt % args)


class MemoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: MemoryService) -> None:
        self.service = service
        super().__init__(address, _Handler)


def create_server(
    cfg: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    service: MemoryService | None = None,
    startup_maintenance: bool = False,
):
    service = service or MemoryService(cfg, startup_maintenance=startup_maintenance)
    address = (host if host is not None else cfg.service.host, port if port is not None else cfg.service.port)
    return MemoryHTTPServer(address, service)


def serve(
    cfg: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    startup_maintenance: bool = False,
) -> None:
    server = create_server(cfg, host=host, port=port, startup_maintenance=startup_maintenance)
    try:
        log.info("memory service listening on %s:%s", *server.server_address)
        server.serve_forever()
    finally:
        server.server_close()
        server.service.close()
