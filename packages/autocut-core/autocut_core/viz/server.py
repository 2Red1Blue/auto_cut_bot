"""可视化观测服务 — 纯 stdlib 的本地 HTTP/SSE 服务 (viz 子命令后端)。

由 ``autocut viz <job_root> --port 8787`` 延迟导入启动, 只绑定
127.0.0.1, 零第三方依赖 (http.server.ThreadingHTTPServer)。

路由契约:
  - ``GET /`` 与 ``/assets/*`` — 静态托管 React 构建产物
    (``autocut_core/viz/static/``); index.html 缺失时返回友好提示页;
  - ``GET /api/state`` — 全量快照 JSON: 拓扑顺序 (registry 权威) +
    各 Stage 折叠状态 (project.json/failure.json) + 产物计数 + 控制面状态;
  - ``GET /api/events`` — SSE 尾随 ``.sd-viz/events.jsonl``; 支持
    ``Last-Event-ID`` 请求头 (值为 seq, 从该 seq 之后重放); 每条事件按
    ``id: {seq}\\ndata: {原始行}\\n\\n`` 发送; 空闲期发送心跳注释行;
  - ``GET /api/stage/<name>`` — 节点详情: 产物列表 (``.sd-cache/index.json``
    按 stage 前缀过滤) + JSON 内容预览 (≤64KB 截断) + failure.json
    (若失败 Stage 匹配) + 最近 200 条该 Stage 的 log 事件;
  - ``POST /api/control`` — body ``{"action", "target_stage"?}``,
    经 viz.control.write_control 原子写入控制文件。

容错原则: 所有数据源读取 try/except 容错 (文件可能瞬时被重写);
所有路由异常统一返回 500 JSON; SSE 流客户端断开时优雅退出。
"""

from __future__ import annotations

import json
import mimetypes
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from autocut_core.io import load_json, utc_now
from autocut_core.logging import get_logger
from autocut_core.registry import HUMAN_NODES, _PIPELINE_ORDER, StageRegistry
from autocut_core.viz.control import read_control, write_control
from autocut_core.viz.events import (
    EVENTS_FILENAME,
    EV_LOG,
    VIZ_DIR_NAME,
)

logger = get_logger(__name__)

__all__ = ["serve", "create_server"]

# ── 常量 ────────────────────────────────────────────────────────────────────

# 前端构建产物目录 (viz-web 的 vite build 输出, 随仓库提交)
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 产物 JSON 预览上限 — 超过截断并标记 truncated
_PREVIEW_LIMIT = 64 * 1024

# SSE 尾随轮询间隔与心跳间隔 (秒)
_SSE_POLL_SECONDS = 0.3
_SSE_HEARTBEAT_SECONDS = 15.0

# 节点详情中回传的 log 事件上限
_STAGE_LOG_LIMIT = 200

# 控制面允许的 action 取值 (与 viz.control 契约一致)
_CONTROL_ACTIONS = {"pause", "resume", "pause_before"}


# ── 数据读取 (全部容错) ─────────────────────────────────────────────────────


def _read_json_safe(path: Path) -> Any | None:
    """读取 JSON 文件; 不存在/损坏/IO 失败一律返回 None。"""
    try:
        if not path.is_file():
            return None
        return load_json(path)
    except Exception as exc:  # noqa: BLE001 — 观测服务绝不因数据抖动崩溃
        logger.debug("viz: 读取 %s 失败 (已忽略): %s", path, exc)
        return None


def _events_path(job_root: Path) -> Path:
    return job_root / VIZ_DIR_NAME / EVENTS_FILENAME


def _read_event_lines(job_root: Path) -> list[str]:
    """读取 events.jsonl 全部非空行; 任何失败返回空列表。"""
    path = _events_path(job_root)
    try:
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.debug("viz: 事件文件读取失败 (已忽略): %s", exc)
        return []


def _line_seq(line: str) -> int | None:
    """从事件行解析 seq; 解析失败返回 None。"""
    try:
        seq = json.loads(line).get("seq")
        return int(seq) if isinstance(seq, int) else None
    except (ValueError, TypeError, AttributeError):
        return None


# ── 拓扑与快照 ──────────────────────────────────────────────────────────────


def build_topology() -> list[dict[str, Any]]:
    """构建权威拓扑 — registry 发现 + _PIPELINE_ORDER 全序 (含人工节点)。

    discover() 失败时回退静态顺序表, 保证观测服务始终可用。
    """
    try:
        registry = StageRegistry()
        registry.discover()
        registered = set(registry.pipeline_order())
        human = set(registry.human_nodes())
    except Exception as exc:  # noqa: BLE001 — 回退静态顺序表
        logger.warning("viz: Stage 发现失败, 回退静态拓扑: %s", exc)
        registered = set()
        human = set(HUMAN_NODES)
    return [
        {
            "name": name,
            "is_human": name in human,
            "registered": name in registered or name in human,
        }
        for name in _PIPELINE_ORDER
    ]


def _fold_stage_status(entry: Any, failed_stage: str | None, name: str) -> str:
    """把 project.json 条目折叠为 completed/failed/pending 三态。"""
    if failed_stage == name:
        return "failed"
    if isinstance(entry, dict):
        raw = entry.get("status")
        if raw == "completed":
            return "completed"
        if raw == "failed":
            return "failed"
    return "pending"


def snapshot_state(job_root: Path, topology: list[dict[str, Any]]) -> dict[str, Any]:
    """全量快照 — /api/state 的响应体。"""
    project = _read_json_safe(job_root / "project.json")
    stages_map = project.get("stages") if isinstance(project, dict) else None
    if not isinstance(stages_map, dict):
        stages_map = {}

    failure = _read_json_safe(job_root / "failure.json")
    failed_stage = failure.get("stage") if isinstance(failure, dict) else None

    index = _read_json_safe(job_root / ".sd-cache" / "index.json")
    index_map = index if isinstance(index, dict) else {}

    per_stage_counts: dict[str, int] = {}
    for key in index_map:
        if isinstance(key, str) and "/" in key:
            stage_name = key.split("/", 1)[0]
            per_stage_counts[stage_name] = per_stage_counts.get(stage_name, 0) + 1

    stages: list[dict[str, Any]] = []
    for node in topology:
        name = str(node["name"])
        entry = stages_map.get(name)
        outputs = entry.get("outputs") if isinstance(entry, dict) else None
        stages.append(
            {
                **node,
                "status": _fold_stage_status(entry, failed_stage, name),
                "updated_at": entry.get("updated_at") if isinstance(entry, dict) else None,
                "output_count": len(outputs) if isinstance(outputs, dict) else 0,
                "artifact_count": per_stage_counts.get(name, 0),
            }
        )

    return {
        "job_root": str(job_root),
        "generated_at": utc_now(),
        "topology": stages,
        "artifact_count": len(index_map),
        "event_count": len(_read_event_lines(job_root)),
        "control": read_control(job_root),
    }


# ── 节点详情 ────────────────────────────────────────────────────────────────


def _preview_file(path: Path) -> tuple[str | None, bool, int | None]:
    """读取产物文件预览 — 返回 (内容, 是否截断, 文件字节大小)。

    超过 _PREVIEW_LIMIT 的部分截断; 文件缺失/读失败返回 (None, False, None)。
    """
    try:
        size = path.stat().st_size if path.is_file() else None
        if size is None:
            return None, False, None
        with path.open("rb") as handle:
            raw = handle.read(_PREVIEW_LIMIT + 1)
        truncated = len(raw) > _PREVIEW_LIMIT
        return raw[:_PREVIEW_LIMIT].decode("utf-8", errors="replace"), truncated, size
    except Exception as exc:  # noqa: BLE001
        logger.debug("viz: 产物预览读取失败 %s: %s", path, exc)
        return None, False, None


def stage_detail(job_root: Path, name: str) -> dict[str, Any]:
    """节点详情 — /api/stage/<name> 的响应体。"""
    index = _read_json_safe(job_root / ".sd-cache" / "index.json")
    index_map = index if isinstance(index, dict) else {}

    artifacts: list[dict[str, Any]] = []
    for key, entry in sorted(index_map.items()):
        if not isinstance(entry, dict) or not isinstance(key, str):
            continue
        if entry.get("stage") != name and not key.startswith(f"{name}/"):
            continue
        raw_path = entry.get("_path") or entry.get("path") or ""
        path = Path(raw_path) if isinstance(raw_path, str) and raw_path else None
        preview, truncated, size = (
            _preview_file(path) if path is not None else (None, False, None)
        )
        artifacts.append(
            {
                "name": entry.get("name", ""),
                "sha256": entry.get("sha256", ""),
                "path": str(path) if path is not None else "",
                "size": size,
                "input_shas": entry.get("input_shas") or {},
                "preview": preview,
                "truncated": truncated,
            }
        )

    failure = _read_json_safe(job_root / "failure.json")
    failure_out = (
        failure if isinstance(failure, dict) and failure.get("stage") == name else None
    )

    logs: deque[dict[str, Any]] = deque(maxlen=_STAGE_LOG_LIMIT)
    for line in _read_event_lines(job_root):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == EV_LOG and event.get("stage") == name:
            logs.append(event)

    return {
        "stage": name,
        "generated_at": utc_now(),
        "artifacts": artifacts,
        "failure": failure_out,
        "logs": list(logs),
    }


# ── HTTP 服务 ───────────────────────────────────────────────────────────────


class VizHTTPServer(ThreadingHTTPServer):
    """携带 job_root 与拓扑缓存的 ThreadingHTTPServer。"""

    daemon_threads = True
    allow_reuse_address = True

    job_root: Path
    topology: list[dict[str, Any]]


class VizRequestHandler(BaseHTTPRequestHandler):
    """viz 请求处理器 — 静态托管 + JSON API + SSE 尾随。"""

    protocol_version = "HTTP/1.1"
    server_version = "SDVizServer/5.0"
    server: VizHTTPServer  # 收窄父类属性类型

    # ── 路由入口 ─────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 — http.server 约定命名
        try:
            path = urlparse(self.path).path
            if path == "/api/state":
                return self._api_state()
            if path == "/api/events":
                return self._api_events()
            if path.startswith("/api/stage/"):
                return self._api_stage(unquote(path[len("/api/stage/"):]))
            if path.startswith("/api/"):
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"未知 API: {path}"})
            return self._serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            return  # 客户端提前断开 — 静默退出
        except Exception as exc:  # noqa: BLE001 — 统一 500 JSON 兜底
            logger.warning("viz: GET %s 处理失败: %s", self.path, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path == "/api/control":
                return self._api_control()
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"未知 API: {path}"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("viz: POST %s 处理失败: %s", self.path, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """访问日志降级为 debug — 避免 SSE 轮询刷屏。"""
        logger.debug("viz: " + format, *args)

    # ── JSON API ─────────────────────────────────────────────────

    def _api_state(self) -> None:
        body = snapshot_state(self.server.job_root, self.server.topology)
        self._send_json(HTTPStatus.OK, body)

    def _api_stage(self, name: str) -> None:
        if not name or "/" in name:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "非法 Stage 名"})
        self._send_json(HTTPStatus.OK, stage_detail(self.server.job_root, name))

    def _api_control(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "请求体必须是 JSON 对象"}
            )
        action = data.get("action")
        target_stage = data.get("target_stage")
        if action not in _CONTROL_ACTIONS:
            return self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"action 必须是 {sorted(_CONTROL_ACTIONS)} 之一"},
            )
        if target_stage is not None and not isinstance(target_stage, str):
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "target_stage 必须是字符串或省略"}
            )
        if action == "pause_before" and not target_stage:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "pause_before 必须指定 target_stage"}
            )
        path = write_control(self.server.job_root, str(action), target_stage)
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "action": action, "target_stage": target_stage, "path": str(path)},
        )

    # ── SSE 尾随 ─────────────────────────────────────────────────

    def _api_events(self) -> None:
        """SSE 尾随 events.jsonl — Last-Event-ID 之后的事件重放 + 增量推送。"""
        after = self._parse_last_event_id()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")  # 禁用代理缓冲
        self.end_headers()
        self.close_connection = True  # 无 Content-Length, 以连接关闭结束响应
        self.wfile.flush()

        path = _events_path(self.server.job_root)
        offset = 0
        buffer = ""
        last_beat = time.monotonic()
        try:
            while True:
                fresh = ""
                try:
                    if path.is_file():
                        with path.open("r", encoding="utf-8") as handle:
                            handle.seek(offset)
                            fresh = handle.read()
                            offset = handle.tell()
                except OSError:
                    fresh = ""  # 瞬时 IO 抖动 — 下一轮重试
                if fresh:
                    buffer += fresh
                    if buffer.endswith("\n"):
                        lines, buffer = buffer.split("\n")[:-1], ""
                    else:
                        parts = buffer.split("\n")
                        lines, buffer = parts[:-1], parts[-1]
                    for line in lines:
                        if not line.strip():
                            continue
                        seq = _line_seq(line)
                        if seq is not None and seq <= after:
                            continue  # Last-Event-ID 之前的事件已重放过
                        self._sse_write(seq, line)
                    last_beat = time.monotonic()
                elif time.monotonic() - last_beat >= _SSE_HEARTBEAT_SECONDS:
                    self._sse_write(None, None)  # 心跳注释行
                    last_beat = time.monotonic()
                time.sleep(_SSE_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # 客户端断开 — 优雅退出

    def _parse_last_event_id(self) -> int:
        """解析 Last-Event-ID 请求头 (或同名 query 参数兜底)。"""
        raw = self.headers.get("Last-Event-ID")
        if raw is None:
            raw = (parse_qs(urlparse(self.path).query).get("lastEventId") or [None])[0]
        try:
            return int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            return 0

    def _sse_write(self, seq: int | None, line: str | None) -> None:
        """写一条 SSE 帧; line 为 None 时写心跳注释行。"""
        frame = ": keep-alive\n\n" if line is None else f"id: {seq}\ndata: {line}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    # ── 静态托管 ─────────────────────────────────────────────────

    def _serve_static(self, url_path: str) -> None:
        rel = url_path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        target = (_STATIC_DIR / rel).resolve()
        if not target.is_relative_to(_STATIC_DIR):
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "路径越界"})
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if rel == "index.html":
                return self._serve_fallback_page()
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"静态资源缺失: {rel}"})
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_fallback_page(self) -> None:
        """index.html 缺失时的友好提示页 (构建产物未入库/未构建)。"""
        html = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>SD-Pipeline 观测台 — 前端未构建</title></head>"
            "<body style='font-family:monospace;background:#0b0f0e;color:#9df0c8;"
            "display:flex;min-height:100vh;align-items:center;justify-content:center'>"
            "<div style='max-width:520px;line-height:1.8'>"
            "<h1 style='letter-spacing:.2em'>SD-VIZ</h1>"
            "<p>观测服务已启动, 但前端构建产物缺失 (autocut_core/viz/static/index.html)。</p>"
            "<p>请在 <code>material_skill_manager/viz-web/</code> 下执行:</p>"
            "<pre style='color:#ffb347'>npm install\nnpm run build</pre>"
            "<p>API 仍可用: <a href='/api/state' style='color:#9df0c8'>/api/state</a></p>"
            "</div></body></html>"
        )
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── 响应工具 ─────────────────────────────────────────────────

    def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


# ── 启动入口 ────────────────────────────────────────────────────────────────


def create_server(job_root: Path | str, port: int = 8787) -> VizHTTPServer:
    """创建并返回已绑定端口的观测服务实例 (不启动循环)。

    port 传 0 时由系统分配空闲端口 (测试场景), 实际端口见
    ``server.server_address[1]``。拓扑在服务启动时发现一次并缓存。
    """
    root = Path(job_root).expanduser().resolve()
    server = VizHTTPServer(("127.0.0.1", port), VizRequestHandler)
    server.job_root = root
    server.topology = build_topology()
    return server


def serve(job_root: Path | str, port: int = 8787) -> None:
    """启动观测服务并阻塞 — ``autocut viz`` 子命令入口。"""
    server = create_server(job_root, port)
    host, actual_port = server.server_address[:2]
    url = f"http://{host}:{actual_port}"
    logger.info("viz: 观测服务已启动 %s (job_root=%s)", url, server.job_root)
    print(f"SD-VIZ 观测台: {url}  (job_root: {server.job_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSD-VIZ 观测台已停止")
    finally:
        server.server_close()
