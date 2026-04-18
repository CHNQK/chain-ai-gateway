"""
db.py - Request logging and per-key/model statistics via SQLite.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = "/home/qiankai/chain-ai-gateway/gateway.db"
_local = threading.local()

@contextmanager
def _conn():
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    yield _local.conn


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS request_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        REAL    NOT NULL,
            request_id TEXT DEFAULT '',
            virtual_model TEXT,
            requested_model TEXT,
            provider  TEXT,
            api_key_hint TEXT,
            model     TEXT,
            stream    INTEGER,
            status    TEXT,
            reason    TEXT,
            latency_ms INTEGER,
            prompt_tokens  INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            request_body  TEXT DEFAULT '',
            response_body TEXT DEFAULT '',
            upstream_request_body TEXT DEFAULT '',
            upstream_response_body TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS stats (
            provider  TEXT NOT NULL,
            api_key_hint TEXT NOT NULL,
            model     TEXT NOT NULL,
            success   INTEGER DEFAULT 0,
            failure   INTEGER DEFAULT 0,
            total_prompt_tokens     INTEGER DEFAULT 0,
            total_completion_tokens INTEGER DEFAULT 0,
            PRIMARY KEY (provider, api_key_hint, model)
        );
        CREATE TABLE IF NOT EXISTS bulk_test_report (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            started_at REAL DEFAULT 0,
            finished_at REAL DEFAULT 0,
            trigger TEXT DEFAULT '',
            refresh INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            error INTEGER DEFAULT 0,
            pending INTEGER DEFAULT 0,
            running_count INTEGER DEFAULT 0,
            progress_pct REAL DEFAULT 0,
            llm_status TEXT DEFAULT '',
            llm_source TEXT DEFAULT '',
            llm_model TEXT DEFAULT '',
            llm_error TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            stats_json TEXT DEFAULT ''
        );
        """)
        # Add request_id column if upgrading from old schema
        try:
            c.execute("ALTER TABLE request_log ADD COLUMN request_id TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE request_log ADD COLUMN upstream_request_body TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE request_log ADD COLUMN upstream_response_body TEXT DEFAULT ''")
        except Exception:
            pass
        # RR index persistence table
        c.execute("""
            CREATE TABLE IF NOT EXISTS rr_index (
                rr_key TEXT PRIMARY KEY,
                idx    INTEGER DEFAULT 0
            )
        """)
        c.commit()


def upsert_bulk_test_report(
    *,
    run_id: str,
    started_at: float = 0.0,
    finished_at: float = 0.0,
    trigger: str = "",
    refresh: bool = False,
    total: int = 0,
    success: int = 0,
    error: int = 0,
    pending: int = 0,
    running_count: int = 0,
    progress_pct: float = 0.0,
    llm_status: str = "",
    llm_source: str = "",
    llm_model: str = "",
    llm_error: str = "",
    summary: str = "",
    stats: dict | None = None,
):
    payload = json.dumps(stats or {}, ensure_ascii=False)
    created_at = time.time()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO bulk_test_report (
                run_id, created_at, started_at, finished_at, trigger, refresh,
                total, success, error, pending, running_count, progress_pct,
                llm_status, llm_source, llm_model, llm_error, summary, stats_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                trigger=excluded.trigger,
                refresh=excluded.refresh,
                total=excluded.total,
                success=excluded.success,
                error=excluded.error,
                pending=excluded.pending,
                running_count=excluded.running_count,
                progress_pct=excluded.progress_pct,
                llm_status=excluded.llm_status,
                llm_source=excluded.llm_source,
                llm_model=excluded.llm_model,
                llm_error=excluded.llm_error,
                summary=excluded.summary,
                stats_json=excluded.stats_json
            """,
            (
                run_id,
                created_at,
                started_at,
                finished_at,
                trigger,
                int(bool(refresh)),
                int(total or 0),
                int(success or 0),
                int(error or 0),
                int(pending or 0),
                int(running_count or 0),
                float(progress_pct or 0.0),
                llm_status,
                llm_source,
                llm_model,
                llm_error,
                summary,
                payload,
            ),
        )
        c.commit()


def get_bulk_test_report_page(limit: int = 3, page: int = 1) -> dict:
    limit = max(1, min(int(limit or 3), 20))
    page = max(1, int(page or 1))
    offset = (page - 1) * limit
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM bulk_test_report").fetchone()[0]
        rows = c.execute(
            """
            SELECT id, run_id, created_at, started_at, finished_at, trigger, refresh,
                   total, success, error, pending, running_count, progress_pct,
                   llm_status, llm_source, llm_model, llm_error, summary, stats_json
            FROM bulk_test_report
            ORDER BY started_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["refresh"] = bool(item.get("refresh"))
        item["total"] = int(item.get("total") or 0)
        item["success"] = int(item.get("success") or 0)
        item["error"] = int(item.get("error") or 0)
        item["pending"] = int(item.get("pending") or 0)
        item["running_count"] = int(item.get("running_count") or 0)
        item["progress_pct"] = float(item.get("progress_pct") or 0.0)
        try:
            item["stats"] = json.loads(item.get("stats_json") or "{}")
        except Exception:
            item["stats"] = {}
        item.pop("stats_json", None)
        items.append(item)
    pages = (total + limit - 1) // limit if total else 1
    return {
        "items": items,
        "total": int(total or 0),
        "page": page,
        "limit": limit,
        "pages": pages,
    }


def get_rr_index(rr_key: str) -> int:
    with _conn() as c:
        row = c.execute("SELECT idx FROM rr_index WHERE rr_key=?", (rr_key,)).fetchone()
    return row[0] if row else 0


def save_rr_index(rr_key: str, idx: int):
    import sqlite3 as _sq
    conn = _sq.connect(DB_PATH, timeout=5)
    try:
        conn.execute("INSERT INTO rr_index(rr_key,idx) VALUES(?,?) ON CONFLICT(rr_key) DO UPDATE SET idx=excluded.idx", (rr_key, idx))
        conn.commit()
    finally:
        conn.close()


def insert_pending(*, virtual_model, requested_model="", stream, request_body="", request_id=""):
    """Insert a log entry immediately when request arrives. Returns row id."""
    ts_ = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO request_log (ts,request_id,virtual_model,requested_model,stream,status,request_body,provider,api_key_hint,model) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts_, request_id, virtual_model, requested_model, int(stream), "pending", request_body, "", "", "")
        )
        c.commit()
        return cur.lastrowid


def update_log(row_id: int, **kwargs):
    """Update an existing log row after response."""
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    with _conn() as c:
        c.execute(f"UPDATE request_log SET {sets} WHERE id=?", (*kwargs.values(), row_id))
        c.commit()



def log_request(*, virtual_model, requested_model="", provider, api_key, model, stream,
                status, reason="", latency_ms=0, prompt_tokens=0, completion_tokens=0,
                request_body="", response_body="", request_id=""):
    hint = api_key[:8] + "..." if api_key else ""
    ts = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO request_log (ts,request_id,virtual_model,requested_model,provider,api_key_hint,model,stream,status,reason,latency_ms,prompt_tokens,completion_tokens,request_body,response_body) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, request_id, virtual_model, requested_model, provider, hint, model, int(stream), status, reason, latency_ms, prompt_tokens, completion_tokens, request_body, response_body)
        )
        if status == "success":
            c.execute("""
                INSERT INTO stats (provider,api_key_hint,model,success,total_prompt_tokens,total_completion_tokens)
                VALUES (?,?,?,1,?,?)
                ON CONFLICT(provider,api_key_hint,model) DO UPDATE SET
                  success=success+1,
                  total_prompt_tokens=total_prompt_tokens+excluded.total_prompt_tokens,
                  total_completion_tokens=total_completion_tokens+excluded.total_completion_tokens
            """, (provider, hint, model, prompt_tokens, completion_tokens))
        else:
            c.execute("""
                INSERT INTO stats (provider,api_key_hint,model,failure)
                VALUES (?,?,?,1)
                ON CONFLICT(provider,api_key_hint,model) DO UPDATE SET failure=failure+1
            """, (provider, hint, model))
        c.commit()


def get_logs(limit=200, offset=0):
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, request_id, virtual_model, requested_model, provider, api_key_hint, model, stream, status, reason, latency_ms, prompt_tokens, completion_tokens FROM request_log ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    return [dict(r) for r in rows]


def get_log_page(limit=50, page=1, status="", search=""):
    limit = max(1, min(int(limit or 50), 200))
    page = max(1, int(page or 1))
    offset = (page - 1) * limit

    filters = ["COALESCE(main.request_body, '') != ''"]
    params = []
    if status:
        filters.append("main.status = ?")
        params.append(status)
    if search:
        filters.append("""
            LOWER(
                COALESCE(main.provider, '') || ' ' ||
                COALESCE(main.requested_model, '') || ' ' ||
                COALESCE(main.model, '') || ' ' ||
                COALESCE(main.reason, '') || ' ' ||
                COALESCE(main.virtual_model, '') || ' ' ||
                COALESCE(main.request_id, '')
            ) LIKE ?
        """)
        params.append(f"%{search.strip().lower()}%")
    where_sql = " WHERE " + " AND ".join(filters)

    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) FROM request_log main{where_sql}",
            params,
        ).fetchone()[0]
        rows = c.execute(
            f"""
            SELECT
                main.id,
                main.ts,
                main.request_id,
                main.virtual_model,
                main.requested_model,
                main.provider,
                main.api_key_hint,
                main.model,
                main.stream,
                main.status,
                main.reason,
                main.latency_ms,
                main.prompt_tokens,
                main.completion_tokens,
                COALESCE(attempts.total_attempts, 1) AS total_attempts,
                COALESCE(attempts.failover_attempts, 0) AS failover_attempts,
                COALESCE(attempts.error_attempts, 0) AS error_attempts,
                COALESCE(attempts.last_attempt_ts, main.ts) AS last_attempt_ts
            FROM request_log main
            LEFT JOIN (
                SELECT
                    request_id,
                    COUNT(*) AS total_attempts,
                    SUM(CASE WHEN status='failover' THEN 1 ELSE 0 END) AS failover_attempts,
                    SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_attempts,
                    MAX(ts) AS last_attempt_ts
                FROM request_log
                WHERE request_id != ''
                GROUP BY request_id
            ) attempts ON attempts.request_id = main.request_id
            {where_sql}
            ORDER BY COALESCE(attempts.last_attempt_ts, main.ts) DESC, main.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["stream"] = int(item.get("stream") or 0)
        item["latency_ms"] = int(item.get("latency_ms") or 0)
        item["prompt_tokens"] = int(item.get("prompt_tokens") or 0)
        item["completion_tokens"] = int(item.get("completion_tokens") or 0)
        item["total_attempts"] = int(item.get("total_attempts") or 1)
        item["failover_attempts"] = int(item.get("failover_attempts") or 0)
        item["error_attempts"] = int(item.get("error_attempts") or 0)
        items.append(item)

    pages = (total + limit - 1) // limit if total else 1
    return {
        "items": items,
        "total": int(total or 0),
        "page": page,
        "limit": limit,
        "pages": pages,
    }


def get_log_by_id(log_id: int):
    with _conn() as c:
        row = c.execute("SELECT * FROM request_log WHERE id = ?", (log_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        request_id = data.get("request_id") or ""
        if request_id:
            attempt_rows = c.execute("""
                SELECT id, ts, provider, api_key_hint, model, stream, status, reason, latency_ms,
                       prompt_tokens, completion_tokens
                FROM request_log
                WHERE request_id = ?
                ORDER BY CASE WHEN id = ? THEN 1 ELSE 0 END ASC, ts ASC, id ASC
            """, (request_id, log_id)).fetchall()
        else:
            attempt_rows = [row]

    data["attempts"] = [dict(r) for r in attempt_rows]
    data["attempt_count"] = len(data["attempts"])
    data["failover_count"] = sum(1 for item in data["attempts"] if item.get("status") == "failover")
    data["error_count"] = sum(1 for item in data["attempts"] if item.get("status") == "error")
    return data


def get_stats():
    with _conn() as c:
        rows = c.execute("""
            SELECT provider, api_key_hint, model,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                   SUM(CASE WHEN status IN ('failover','error') THEN 1 ELSE 0 END) as failure,
                   SUM(prompt_tokens) as total_prompt_tokens,
                   SUM(completion_tokens) as total_completion_tokens
            FROM request_log
            WHERE provider != ''
            GROUP BY provider, api_key_hint, model
            ORDER BY provider, model
        """).fetchall()
    return [dict(r) for r in rows]


def get_request_summary(since_ts: float = 0) -> dict:
    with _conn() as c:
        row = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status='failover' THEN 1 ELSE 0 END) as failover,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as error,
                SUM(CASE WHEN status='aborted' THEN 1 ELSE 0 END) as aborted,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
                AVG(CASE WHEN status='success' AND latency_ms > 0 THEN latency_ms END) as avg_latency_ms,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                SUM(CASE WHEN status='success' AND latency_ms > 0 THEN latency_ms ELSE 0 END) as total_success_latency_ms
            FROM request_log
            WHERE ts >= ?
        """, (since_ts,)).fetchone()
    data = dict(row) if row else {}
    total_prompt_tokens = int(data.get("total_prompt_tokens") or 0)
    total_completion_tokens = int(data.get("total_completion_tokens") or 0)
    total_success_latency_ms = int(data.get("total_success_latency_ms") or 0)
    failover = int(data.get("failover") or 0)
    error = int(data.get("error") or 0)
    aborted = int(data.get("aborted") or 0)
    pending = int(data.get("pending") or 0)
    actual_tps = round(total_completion_tokens / (total_success_latency_ms / 1000), 1) if total_success_latency_ms > 0 else 0.0
    return {
        "total": int(data.get("total") or 0),
        "success": int(data.get("success") or 0),
        "failover": failover,
        "error": error,
        "aborted": aborted,
        "pending": pending,
        "total_failures": failover + error + aborted,
        "avg_latency_ms": int(data.get("avg_latency_ms") or 0),
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_success_latency_ms": total_success_latency_ms,
        "actual_tps": actual_tps,
    }


def get_recent_issues(limit: int = 10, since_ts: float = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT id, ts, requested_model, provider, model, status, reason, latency_ms
            FROM request_log
            WHERE provider != ''
              AND status IN ('failover', 'error')
              AND ts >= ?
            ORDER BY ts DESC
            LIMIT ?
        """, (since_ts, limit)).fetchall()
    return [dict(r) for r in rows]


def get_provider_metrics(since_ts: float = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT provider,
                   COUNT(*) as total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                   SUM(CASE WHEN status='failover' THEN 1 ELSE 0 END) as failover,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as error,
                   AVG(CASE WHEN status='success' AND latency_ms > 0 THEN latency_ms END) as avg_latency_ms,
                   MAX(ts) as last_seen,
                   COUNT(DISTINCT model) as model_count
            FROM request_log
            WHERE provider != ''
              AND ts >= ?
            GROUP BY provider
            ORDER BY provider
        """, (since_ts,)).fetchall()
        issue_rows = c.execute("""
            SELECT provider, reason, ts
            FROM request_log
            WHERE provider != ''
              AND status IN ('failover', 'error')
              AND ts >= ?
            ORDER BY ts DESC
        """, (since_ts,)).fetchall()

    issues = {}
    for row in issue_rows:
        provider = row["provider"]
        if provider not in issues:
            issues[provider] = {
                "last_error_reason": row["reason"] or "",
                "last_error_ts": row["ts"],
            }

    result = []
    for row in rows:
        item = dict(row)
        item["total"] = int(item.get("total") or 0)
        item["success"] = int(item.get("success") or 0)
        item["failover"] = int(item.get("failover") or 0)
        item["error"] = int(item.get("error") or 0)
        item["avg_latency_ms"] = int(item.get("avg_latency_ms") or 0)
        item["last_seen"] = float(item.get("last_seen") or 0)
        item["model_count"] = int(item.get("model_count") or 0)
        item.update(issues.get(item["provider"], {"last_error_reason": "", "last_error_ts": 0}))
        result.append(item)
    return result


def get_all_provider_ids() -> list[str]:
    with _conn() as c:
        rows = c.execute("""
            SELECT DISTINCT provider
            FROM request_log
            WHERE provider != ''
            ORDER BY provider
        """).fetchall()
    return [str(row["provider"]) for row in rows]


def get_model_metrics(since_ts: float = 0, limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT model,
                   COUNT(*) as total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                   SUM(CASE WHEN status='failover' THEN 1 ELSE 0 END) as failover,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as error,
                   AVG(CASE WHEN status='success' AND latency_ms > 0 THEN latency_ms END) as avg_latency_ms,
                   MAX(ts) as last_seen,
                   COUNT(DISTINCT provider) as provider_count,
                   SUM(prompt_tokens) as total_prompt_tokens,
                   SUM(completion_tokens) as total_completion_tokens,
                   SUM(CASE WHEN requested_model != '' AND requested_model != model AND status='success' THEN 1 ELSE 0 END) as fallback_hits
            FROM request_log
            WHERE provider != ''
              AND model != ''
              AND model != '*'
              AND ts >= ?
            GROUP BY model
            ORDER BY total DESC, success DESC, model ASC
            LIMIT ?
        """, (since_ts, limit)).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        total = int(item.get("total") or 0)
        success = int(item.get("success") or 0)
        item["total"] = total
        item["success"] = success
        item["failover"] = int(item.get("failover") or 0)
        item["error"] = int(item.get("error") or 0)
        item["avg_latency_ms"] = int(item.get("avg_latency_ms") or 0)
        item["last_seen"] = float(item.get("last_seen") or 0)
        item["provider_count"] = int(item.get("provider_count") or 0)
        item["total_prompt_tokens"] = int(item.get("total_prompt_tokens") or 0)
        item["total_completion_tokens"] = int(item.get("total_completion_tokens") or 0)
        item["fallback_hits"] = int(item.get("fallback_hits") or 0)
        item["success_rate"] = round((success / total) * 100, 1) if total else 0.0
        result.append(item)
    return result
