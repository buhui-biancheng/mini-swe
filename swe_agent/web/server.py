"""Mini-SWE 本地 web 控制台服务器。

借鉴 waku-agent dashboard 的契约（waku/ops/dashboard.py）：
- 前端是纯静态文件（无构建），后端只用标准库 HTTP 服务器
- 实时性靠浏览器 450ms 轮询 /api/events?cursor=N 读 trace JSONL 新行
- 事件双写：JSONL（type 字段，供轮询亮灯）+ SSE（kind 字段，供聊天 dock）

端点（我们需要的子集）：
  GET  /api/data              完整载荷（5s 轮询）
  GET  /api/events?cursor=N   增量事件（450ms 轮询，驱动实时高亮）
  POST /api/chat/stream       SSE，跑一次 FSM 修复并流式推送事件
  POST /api/chat              非流式跑一次（curl 用）
  POST /api/session           会话历史 / 新建 / 切换
  POST /api/query             只读 SQL 控制台（state.db）
  GET  /static/* 及兜底       index.html
"""

import json
import os
import socket
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from swe_agent.web import payload as pl
from swe_agent.web.graphdata import GraphService
from swe_agent.web.runner import FixRunner

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
HOME = Path(os.getenv("SWE_WEB_HOME", str(Path.home() / ".swe_agent_web")))
TRACES = HOME / "traces"
DB_PATH = HOME / "state.db"
DEFAULT_PORT = int(os.getenv("SWE_WEB_PORT", "7777"))

graph_service = GraphService()
_run_lock = threading.Lock()   # 一次只跑一个修复任务


class _State:
    session = "default"        # 当前会话 id
    code_dir: str | None = None
    project_root: str | None = None
    thinking_enabled: bool = True   # 思考强度（/api/config 调整）
    reasoning_effort: str = "high"


ST = _State()


# ---------- trace 写入 ----------

def _write_trace(kind: str, ev: dict) -> None:
    TRACES.mkdir(parents=True, exist_ok=True)
    path = TRACES / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    record = {"type": kind, "ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **ev}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _events_since(cursor):
    path = TRACES / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    if not path.exists():
        return {"events": [], "cursor": 0}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"events": [], "cursor": 0}
    if cursor is None or cursor < 0 or cursor > len(lines):
        return {"events": [], "cursor": len(lines)}
    out = []
    for ln in lines[cursor:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return {"events": out, "cursor": len(lines)}


# ---------- state.db（chat_log 会话） ----------

def _get_db() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT,
            meta TEXT,
            consolidated INTEGER DEFAULT 0,
            source TEXT DEFAULT 'web',
            session_id TEXT DEFAULT 'default',
            created_at TEXT
        )""")
    conn.commit()
    return conn


def _write_chat_row(role: str, content: str, meta: dict | None = None) -> None:
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO chat_log (role, content, meta, source, session_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (role, content,
             json.dumps(meta, ensure_ascii=False) if meta else None,
             "web", ST.session,
             datetime.now(UTC).isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------- 任务解析 ----------

def parse_task(message: str) -> tuple[str, str]:
    """聊天输入 → (bug_file, test_command)。支持 `fix <file> <cmd>` 或裸 `<file> <cmd>`。"""
    msg = message.strip()
    if msg.startswith("fix "):
        msg = msg[4:].strip()
    parts = msg.split(None, 1)
    if not parts:
        raise ValueError("空任务")
    bug_file = parts[0]
    test_cmd = parts[1] if len(parts) > 1 else "pytest"
    return bug_file, test_cmd


def _persist_project() -> None:
    """记住上次的 code_dir，重启后图拓扑还能显示。"""
    if not ST.code_dir:
        return
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / "last_project").write_text(ST.code_dir, encoding="utf-8")
    except Exception:
        pass


def run_task(message: str, on_event) -> dict:
    """执行一次修复任务；on_event(kind, ev) 同步回调。返回 done 事件负载。"""
    bug_file, test_cmd = parse_task(message)
    root = ST.project_root or os.getcwd()
    ST.code_dir = os.path.dirname(os.path.abspath(os.path.join(root, bug_file)))
    _persist_project()
    runner = FixRunner(root, bug_file, test_cmd, on_event=on_event,
                       graph=graph_service)
    return runner.run()


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, *, no_cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_cache:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----

    def do_GET(self):
        if self.path == "/api/data":
            payload = pl.collect(
                home=HOME, db_path=DB_PATH, traces_dir=TRACES,
                graph_service=graph_service, code_dir=ST.code_dir,
                current_session=ST.session,
                provider=os.getenv("DEEPSEEK_PROVIDER", "deepseek"),
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            )
            self._send(json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json")
        elif self.path.startswith("/api/events"):
            from urllib.parse import parse_qs, urlparse
            raw = parse_qs(urlparse(self.path).query).get("cursor", [None])[0]
            cursor = int(raw) if raw and raw.lstrip("-").isdigit() else None
            self._send(json.dumps(_events_since(cursor), default=str).encode(),
                       "application/json")
        elif self.path == "/api/config":
            self._send_json({
                "thinking_enabled": ST.thinking_enabled,
                "reasoning_effort": ST.reasoning_effort,
            })
        elif self.path.startswith("/api/models"):
            self._send(json.dumps({"models": [], "error": ""}).encode(), "application/json")
        elif self.path.startswith("/static/"):
            self._serve_static(self.path)
        else:
            self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

    def _serve_static(self, path: str) -> None:
        name = path.split("/static/", 1)[1].split("?")[0]
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = {".css": "text/css", ".js": "text/javascript",
                 ".html": "text/html; charset=utf-8"}.get(target.suffix,
                                                          "application/octet-stream")
        self._send(target.read_bytes(), ctype, no_cache=True)

    # ---- POST ----

    def do_POST(self):
        # 2026-08-08：更新思考强度配置
        if self.path == "/api/config":
            import json as _json
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = _json.loads(self.rfile.read(length).decode("utf-8"))
                if "thinking_enabled" in body:
                    ST.thinking_enabled = bool(body["thinking_enabled"])
                if "reasoning_effort" in body and body["reasoning_effort"] in ("low", "medium", "high", "max"):
                    ST.reasoning_effort = body["reasoning_effort"]
                self._send_json({"ok": True, "thinking_enabled": ST.thinking_enabled,
                                 "reasoning_effort": ST.reasoning_effort})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if self.path == "/api/chat/stream":
            self._chat_stream(body)
            return
        if self.path == "/api/chat":
            self._chat_sync(body)
            return
        if self.path == "/api/session":
            self._session(body)
            return
        if self.path == "/api/query":
            self._query(body)
            return
        self.send_response(404)
        self.end_headers()

    def _chat_stream(self, body: bytes) -> None:
        try:
            message = (json.loads(body or "{}").get("message") or "").strip()
        except Exception:
            message = ""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(kind, ev):
            try:
                _write_trace(kind, ev)
                frame = {"kind": kind, **ev}
                self.wfile.write(
                    ("data: " + json.dumps(frame, ensure_ascii=False, default=str) + "\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # 浏览器离开页面——任务继续跑，trace 继续写

        if not message:
            emit("done", {"error": "空任务"})
            return
        if not _run_lock.acquire(blocking=False):
            emit("done", {"error": "上一个修复任务还在运行，请等待完成"})
            return
        try:
            _write_chat_row("user", message)
            done = run_task(message, emit)
            _write_chat_row("assistant", done.get("reply", ""),
                            meta={"iterations": done.get("iterations"),
                                  "latency_ms": done.get("latency_ms")})
        except Exception as exc:
            emit("done", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            _run_lock.release()

    def _chat_sync(self, body: bytes) -> None:
        try:
            message = (json.loads(body or "{}").get("message") or "").strip()
        except Exception:
            message = ""
        if not message:
            self._send(json.dumps({"error": "空任务"}).encode(), "application/json")
            return
        if not _run_lock.acquire(blocking=False):
            self._send(json.dumps({"error": "busy"}).encode(), "application/json")
            return
        try:
            done = run_task(message, lambda k, e: _write_trace(k, e))
            self._send(json.dumps(done, ensure_ascii=False, default=str).encode(),
                       "application/json")
        except Exception as exc:
            self._send(json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                       "application/json")
        finally:
            _run_lock.release()

    def _session(self, body: bytes) -> None:
        try:
            payload = json.loads(body or "{}")
        except Exception:
            payload = {}
        action = payload.get("action")
        conn = _get_db()
        try:
            if action in ("history", "switch"):
                sid = payload.get("id") or "default"
                if action == "switch":
                    ST.session = sid
                rows = conn.execute(
                    "SELECT role, content, meta FROM chat_log WHERE session_id=? ORDER BY id",
                    (sid,)).fetchall()
                history = [{"role": r["role"], "content": r["content"],
                            "meta": json.loads(r["meta"]) if r["meta"] else None}
                           for r in rows]
                self._send(json.dumps({"ok": True, "session_id": sid,
                                       "history": history}, ensure_ascii=False).encode(),
                           "application/json")
            elif action == "new":
                sid = datetime.now().strftime("s-%Y%m%d-%H%M%S")
                ST.session = sid
                self._send(json.dumps({"ok": True, "session_id": sid,
                                       "history": []}).encode(), "application/json")
            else:
                self._send(json.dumps({"error": f"unknown action {action}"}).encode(),
                           "application/json")
        finally:
            conn.close()

    def _query(self, body: bytes) -> None:
        try:
            sql = (json.loads(body or "{}").get("sql") or "").strip().rstrip(";").strip()
        except Exception:
            sql = ""
        low = sql.lower()
        if not low.startswith(("select", "with")):
            self._send(json.dumps({"error": "只允许 SELECT/WITH 查询"}).encode(),
                       "application/json")
            return
        try:
            c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            cur = c.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchmany(200)]
            c.close()
            self._send(json.dumps({"columns": cols, "rows": rows}, default=str).encode(),
                       "application/json")
        except sqlite3.Error as exc:
            self._send(json.dumps({"error": str(exc)}).encode(), "application/json")

    def log_message(self, *args):  # 保持终端安静
        pass


# ---------- 入口 ----------

class _DualStackServer(ThreadingHTTPServer):
    """IPv6 双栈监听：localhost 常解析为 ::1，只在 127.0.0.1 上监听会被拒绝。

    注意：address_family 是「服务器类」的属性，放在 handler 类上不生效。
    """

    address_family = socket.AF_INET6


def _try_bind(port: int):
    """先试 IPv6 双栈（:: 同时接受 IPv4/IPv6），失败再回退纯 IPv4。"""
    try:
        return _DualStackServer(("::", port), Handler)
    except OSError:
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            return None


def main(project: str | None = None, port: int | None = None) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    TRACES.mkdir(parents=True, exist_ok=True)
    if not project:
        # 图生成隔离（2026-08-08）：必须显式指定工作目录，
        # 禁止用 cwd / last_project 兜底——防止误在上级目录生成大范围图
        raise SystemExit(
            "必须指定 --project <工作目录>（图生成隔离：只扫该目录及子目录）")
    ST.project_root = os.path.abspath(project)
    ST.code_dir = ST.project_root
    base = port or DEFAULT_PORT
    server = None
    for candidate in range(base, base + 10):  # 端口被占则顺延
        server = _try_bind(candidate)
        if server is not None:
            break
        print(f"端口 {candidate} 被占用，尝试 {candidate + 1}…")
    if server is None:
        raise SystemExit(f"{base}–{base + 9} 无可用端口")
    print(f"Mini-SWE 控制台 → http://localhost:{server.server_port}  (Ctrl-C 停止)")
    print(f"  数据目录: {HOME}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
