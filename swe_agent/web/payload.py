"""构造 /api/data 载荷：trace 事件 → turns/统计，state.db → 会话，图 → 拓扑。

字段形状与 waku dashboard 的 collect() 兼容——前端 JS 只读这些字段，
只取我们需要的子集，砍掉 memory/calendar/compare 等废数据。
"""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _tool_status(output: str) -> str:
    """从工具输出字符串判断 ok/warn/error。"""
    low = (output or "").lower()
    if low.startswith("错误") or low.startswith("error") or "error" in low:
        return "error"
    if "exit_code:" in low:
        rest = low.split("exit_code:", 1)[1].strip()
        code = rest.split()[0] if rest else ""
        if code and code != "0":
            return "error"
    return "ok"


def _tool_summary(output: str) -> str:
    return (output or "")[:120].split(". ")[0]


def build_turns(events: list[dict]) -> list[dict]:
    """把 trace 事件分组为 turns（turn_start → turn_end），新的在前。"""
    turns: list[dict] = []
    current: dict | None = None
    for ev in events:
        kind = ev.get("type")
        if kind == "turn_start":
            current = {
                "user_message": ev.get("user_message"), "ts": ev.get("ts"),
                "tools": [], "gate": None, "graph": None,
                "llm_calls": [], "reply": None, "iterations": None,
            }
        elif current is not None:
            if kind == "tool":
                current["tools"].append(ev)
            elif kind == "turn_end":
                current["reply"] = ev.get("reply")
                current["iterations"] = ev.get("iterations")
                try:
                    start = datetime.fromisoformat(current["ts"])
                    end = datetime.fromisoformat(ev["ts"])
                    current["latency_ms"] = int((end - start).total_seconds() * 1000)
                except Exception:
                    current["latency_ms"] = None
                for x in current["tools"]:
                    x["status"] = _tool_status(x.get("output", ""))
                    x["summary"] = _tool_summary(x.get("output", ""))
                current["cost"] = 0
                turns.append(current)
                current = None
    if current is not None:  # 未收尾的 turn = 运行中/挂起
        current["reply"] = current.get("reply") or "运行中…"
        current["unfinished"] = True
        current["latency_ms"] = None
        current["tools"] = [dict(x, status=_tool_status(x.get("output", "")),
                                 summary=_tool_summary(x.get("output", "")))
                            for x in current["tools"]]
        turns.append(current)
    return turns[::-1]


def _table_info(conn: sqlite3.Connection, name: str) -> dict:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
    types = {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()}
    count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    sample = [dict(zip(cols, row)) for row in
              conn.execute(f"SELECT * FROM {name} ORDER BY rowid DESC LIMIT 200").fetchall()]
    return {"name": name, "columns": cols, "types": types, "count": count, "sample": sample}


def _db_info(conn: sqlite3.Connection, db_path: Path) -> dict:
    tables: list[dict] = []
    all_tables: list[str] = []
    try:
        all_tables = [r[0] for r in
                      conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        for name in ("chat_log",):
            if name in all_tables:
                tables.append(_table_info(conn, name))
    except Exception:
        pass
    size = db_path.stat().st_size if db_path.exists() else 0
    return {"path": str(db_path), "size": size, "tables": tables,
            "all_tables": all_tables, "fts": []}


def _sessions(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT session_id, COUNT(*) AS n, MAX(created_at) AS last_at "
            "FROM chat_log GROUP BY session_id ORDER BY last_at DESC").fetchall()
    except Exception:
        return []
    out: list[dict] = []
    for sid, n, last_at in rows:
        first = conn.execute(
            "SELECT content FROM chat_log WHERE session_id=? AND role='user' "
            "ORDER BY id LIMIT 1", (sid,)).fetchone()
        out.append({
            "id": sid,
            "title": (first[0][:60] if first else sid),
            "last": last_at or "",
            "sources": ["web"],
            "messages": n,
            "last_at": last_at or "",
        })
    return out


def collect(home: Path, db_path: Path, traces_dir: Path,
            graph_service=None, code_dir: str | None = None,
            current_session: str = "default",
            provider: str = "deepseek", model: str = "deepseek-v4-pro") -> dict:
    """构造完整 /api/data 载荷。"""
    events: list[dict] = []
    for path in sorted(traces_dir.glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        except OSError:
            pass

    turns = build_turns(events)
    graph_runs = [
        {"workflow": e.get("workflow"), "ms": e.get("ms"), "at": e.get("ts"),
         "steps": e.get("steps"), "path": e.get("path") or []}
        for e in events if e.get("type") == "graph_end"
    ][-8:][::-1]
    trace_tail = [
        {"type": e.get("type"), "ts": e.get("ts"),
         "detail": (e.get("user_message") or e.get("state") or e.get("tool")
                    or e.get("node") or e.get("reply") or "")}
        for e in events[-18:]
    ][::-1]

    tool_calls = sum(len(t["tools"]) for t in turns)
    tool_errors = sum(1 for t in turns for x in t["tools"]
                      if x.get("status") == "error")
    latencies = [t["latency_ms"] for t in turns if t.get("latency_ms") is not None]
    stats = {
        "turns": len(turns),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "gate_skips": 0,
        "gate_retrieves": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost": 0.0,
        "latency_avg": int(sum(latencies) / len(latencies)) if latencies else 0,
        "latency_p95": 0,
        "trace_files": len(list(traces_dir.glob("*.jsonl"))),
    }

    graph = {"enabled": True, "workflows": [], "runs": graph_runs,
             "stats": {"quick": 0, "full": 0}, "node_count": 0}
    if graph_service is not None and code_dir:
        try:
            topo = graph_service.topology(code_dir)
            graph["workflows"] = [topo]
            graph["node_count"] = topo["node_count"]
        except Exception:
            pass

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sessions = _sessions(conn)
        chat_log = [
            dict(r) for r in conn.execute(
                "SELECT id, role, content, consolidated, source, session_id, created_at "
                "FROM chat_log ORDER BY id DESC LIMIT 80").fetchall()
        ][::-1]
        chat_pending = 0
        db_info = _db_info(conn, db_path)
    except Exception:
        sessions, chat_log, chat_pending, db_info = [], [], 0, {
            "path": str(db_path), "size": 0, "tables": [], "all_tables": [], "fts": []}
    finally:
        conn.close()

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "home": str(home.resolve()),
        "provider": provider,
        "model": model,
        "stats": stats,
        "turns": turns[:50],
        "wake_scans": [],
        "trace_tail": trace_tail,
        "trace_file": (sorted(traces_dir.glob("*.jsonl"))[-1].name
                       if list(traces_dir.glob("*.jsonl")) else ""),
        "trace_errors": [],
        "facts": [],
        "episodes": [],
        "episodes_source": "sqlite",
        "episodes_error": "",
        "soul": "",
        "chat_pending": chat_pending,
        "chat_log": chat_log,
        "sessions": sessions,
        "current_session": current_session,
        "consolidate_every": 6,
        "calendar": [],
        "outbox": [],
        "skills": [],
        "eval_report": None,
        "eval_history": [],
        "graph": graph,
        "db": db_info,
        "settings": {
            "provider": provider, "model": model, "small_model": "",
            "providers": [], "pinned": [], "experimental": False,
        },
        "usage": {"calls": 0, "total_in": 0, "total_out": 0, "total_cost": 0.0,
                  "by_day": [], "by_provider": []},
        "tools": {"catalog": [], "mcp": {"configured": False, "servers": [], "live": False},
                  "apple_on": False, "planned": []},
    }
