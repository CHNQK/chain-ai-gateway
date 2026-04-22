"""
main.py - Chain-AI-Gateway: High-availability OpenAI-compatible proxy.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import AsyncGenerator

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from scheduler import Scheduler, UpstreamEndpoint, DYNAMIC_MODEL_SENTINEL
from db import (
    init_db,
    log_request,
    get_logs,
    get_log_page,
    get_log_by_id,
    get_stats,
    insert_pending,
    update_log,
    get_request_summary,
    get_recent_issues,
    get_provider_metrics,
    get_all_provider_ids,
    get_model_metrics,
    get_bulk_test_report_page,
    upsert_bulk_test_report,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("chain-ai-gateway")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("GATEWAY_CONFIG", os.path.join(BASE_DIR, "config.yaml"))


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


scheduler: Scheduler = None


class _ConfigHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if os.path.abspath(event.src_path) == os.path.abspath(CONFIG_PATH):
            try:
                _models_cache["data"] = []
                _models_cache["ts"] = 0.0
                scheduler.reload(load_config())
            except Exception as e:
                logger.error(f"[CONFIG] Reload failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler, _bulk_model_test_daily_task, _bulk_model_test_task
    init_db()
    # Mark stale pending requests from previous run as error
    from db import _conn
    with _conn() as c:
        c.execute("UPDATE request_log SET status='error', reason='服务重启' WHERE status='pending'")
        c.commit()
    scheduler = Scheduler(load_config())
    observer = Observer()
    observer.schedule(_ConfigHandler(), path=os.path.dirname(os.path.abspath(CONFIG_PATH)) or ".", recursive=False)
    observer.start()
    _bulk_model_test_daily_task = asyncio.create_task(_bulk_model_test_scheduler_loop())
    yield
    if _bulk_model_test_daily_task is not None:
        _bulk_model_test_daily_task.cancel()
        with suppress(asyncio.CancelledError):
            await _bulk_model_test_daily_task
        _bulk_model_test_daily_task = None
    if _bulk_model_test_task is not None and not _bulk_model_test_task.done():
        _bulk_model_test_task.cancel()
        with suppress(asyncio.CancelledError):
            await _bulk_model_test_task
        _bulk_model_test_task = None
    observer.stop()
    observer.join()
    for client in list(_clients.values()):
        with suppress(Exception):
            await client.aclose()


app = FastAPI(title="Chain-AI-Gateway", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

_clients: dict[str, httpx.AsyncClient] = {}
_client_lock: asyncio.Lock = None
_models_cache = {"data": [], "ts": 0.0}
_bulk_model_test_lock: asyncio.Lock = None
_bulk_model_test_task: asyncio.Task | None = None
_bulk_model_test_daily_task: asyncio.Task | None = None
_config_update_lock: asyncio.Lock = None

BULK_MODEL_TEST_TIMEOUT_SECONDS = max(10, int(os.environ.get("GATEWAY_BULK_MODEL_TEST_TIMEOUT_SECONDS", "180")))
BULK_MODEL_TEST_CONCURRENCY = max(1, int(os.environ.get("GATEWAY_BULK_MODEL_TEST_CONCURRENCY", "3")))
BULK_MODEL_TEST_MAX_TOKENS = max(8, int(os.environ.get("GATEWAY_BULK_MODEL_TEST_MAX_TOKENS", "32")))
BULK_MODEL_TEST_REPORT_MODEL = os.environ.get("GATEWAY_BULK_MODEL_TEST_REPORT_MODEL", "openrouter/free")
BULK_MODEL_TEST_REPORT_TIMEOUT_SECONDS = max(30, int(os.environ.get("GATEWAY_BULK_MODEL_TEST_REPORT_TIMEOUT_SECONDS", "60")))
BULK_MODEL_TEST_PROMPT = os.environ.get(
    "GATEWAY_BULK_MODEL_TEST_PROMPT",
    "你正在执行网关连通性测试。请直接回复一行文本：MODEL_TEST_OK。不要解释，不要 Markdown。",
)
def _new_bulk_model_test_state() -> dict:
    return {
        "run_id": "",
        "running": False,
        "trigger": "",
        "refresh": False,
        "started_at": 0.0,
        "finished_at": 0.0,
        "total": 0,
        "results": {},
        "report_status": "",
        "report_summary": "",
        "report_source": "",
        "report_model": "",
        "report_error": "",
        "report_overview": {},
    }


_bulk_model_test = _new_bulk_model_test_state()


def _normalize_proxy_url(proxy: str | None) -> str:
    value = (proxy or "").strip()
    if not value:
        return ""
    if "://" not in value:
        return f"http://{value}"
    return value


async def _get_config_update_lock() -> asyncio.Lock:
    global _config_update_lock
    if _config_update_lock is None:
        _config_update_lock = asyncio.Lock()
    return _config_update_lock


async def _write_live_config(cfg: dict) -> dict:
    config_dir = os.path.dirname(os.path.abspath(CONFIG_PATH)) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".config.", suffix=".yaml", dir=config_dir)
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_PATH)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp_path)
    if scheduler:
        scheduler.reload(cfg)
    return cfg


def _proxy_for_requested_model(requested_model: str, provider_cfg: dict, proxy_override: str | None = None) -> str:
    if proxy_override:
        return _normalize_proxy_url(proxy_override)
    return _normalize_proxy_url(provider_cfg.get("proxy", ""))


def _payload_text(payload) -> str:
    if payload is None:
        return ""
    if isinstance(payload, (dict, list)):
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)
    return str(payload)


async def get_client(proxy: str = None) -> httpx.AsyncClient:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    proxy = _normalize_proxy_url(proxy)
    key = proxy if proxy else None
    if key not in _clients or _clients[key].is_closed:
        async with _client_lock:
            if key not in _clients or _clients[key].is_closed:
                kwargs = dict(http2=True, timeout=httpx.Timeout(120.0, connect=10.0))
                if proxy:
                    kwargs["proxy"] = proxy
                _clients[key] = httpx.AsyncClient(**kwargs)
    return _clients[key]


VISION_MODELS = {
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "z-ai/glm-4.5-air:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-3-4b-it:free",
    "google/gemma-3-12b-it:free",
    "google/gemma-3-27b-it:free",
}

DEFAULT_FREE_FALLBACK_MODEL = os.environ.get("GATEWAY_FREE_FALLBACK_MODEL", "openrouter/free")
TRACE_FILTER_ENABLED = os.environ.get("GATEWAY_FILTER_AGENT_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}
LEGACY_TOOL_SANITIZE_ENABLED = os.environ.get("GATEWAY_LEGACY_TOOL_SANITIZE", "").strip().lower() in {"1", "true", "yes", "on"}
MODEL_NOT_FOUND_HINTS = (
    "model not found",
    "no such model",
    "does not exist",
    "unknown model",
    "invalid model",
    "unsupported model",
    "not a valid model",
)

def get_exclude_from_pool() -> set:
    """Read exclude_from_pool from live scheduler config, fallback to VISION_MODELS."""
    if scheduler and scheduler._config:
        cfg_list = scheduler._config.get("exclude_from_pool", None)
        if cfg_list is not None:
            return set(cfg_list)
    return VISION_MODELS


def _normalize_model_name(model: str) -> str:
    return (model or "").strip()


def _is_dynamic_endpoint_model(model: str) -> bool:
    return not model or model == DYNAMIC_MODEL_SENTINEL


def _default_free_model() -> str:
    if scheduler and scheduler._config:
        configured = _normalize_model_name(scheduler._config.get("fallback_free_model", ""))
        if configured:
            return configured
    return DEFAULT_FREE_FALLBACK_MODEL


def _resolve_virtual_model(requested_model: str, default_model: str) -> tuple[str, bool]:
    requested_model = _normalize_model_name(requested_model)
    if requested_model and requested_model in scheduler._endpoints:
        return requested_model, False
    resolved = _normalize_model_name(default_model) or "free"
    return resolved, bool(requested_model)


def _build_model_attempts(requested_model: str, dynamic_route: bool) -> list[str | None]:
    if not dynamic_route:
        return [None]
    requested_model = _normalize_model_name(requested_model)
    attempts: list[str | None] = []
    if requested_model:
        attempts.append(requested_model)
    fallback_model = _default_free_model()
    if fallback_model and fallback_model not in attempts:
        attempts.append(fallback_model)
    return attempts or [fallback_model]


def _route_pool_for_model(base_virtual_model: str, upstream_model: str | None) -> str:
    if upstream_model and upstream_model.endswith(":free") and "free" in scheduler._endpoints:
        return "free"
    return base_virtual_model


def _select_candidates(virtual_model: str, upstream_model: str | None) -> tuple[str, list[UpstreamEndpoint]]:
    target_virtual_model = _route_pool_for_model(virtual_model, upstream_model)
    candidates = scheduler.get_candidates(
        target_virtual_model,
        preferred_model=upstream_model or "",
        dynamic_mode=bool(upstream_model),
    )
    if not upstream_model:
        non_vision = [ep for ep in candidates if ep.model not in get_exclude_from_pool()]
        if non_vision:
            candidates = non_vision
    return target_virtual_model, candidates


def _effective_upstream_model(endpoint: UpstreamEndpoint, upstream_model: str | None) -> str:
    if upstream_model:
        return upstream_model
    if _is_dynamic_endpoint_model(endpoint.model):
        return _default_free_model()
    return endpoint.model


def _extract_error_reason(status_code: int, payload) -> str:
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error", {})
        if isinstance(error, dict):
            detail = error.get("message", "") or error.get("code", "")
        elif isinstance(error, str):
            detail = error
        if not detail:
            detail = payload.get("message", "")
        if not detail:
            detail = str(payload)[:160]
    elif payload:
        detail = str(payload)[:160]
    return f"HTTP {status_code}: {detail}" if detail else f"HTTP {status_code}"


def _error_response_body(reason: str) -> str:
    return json.dumps({"detail": reason}, ensure_ascii=False)


def _estimate_usage_from_body(body: dict, reply: str) -> dict[str, int]:
    prompt_parts = []
    for message in body.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, str):
            prompt_parts.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    prompt_parts.append(part["text"])
    prompt_text = " ".join(prompt_parts)
    return {
        "prompt_tokens": max(1, len(prompt_text) // 3),
        "completion_tokens": max(1, len(reply) // 3) if reply else 0,
    }


def _is_model_not_found(status_code: int, reason: str) -> bool:
    if status_code not in (400, 404):
        return False
    reason_l = (reason or "").lower()
    if "model" in reason_l and any(hint in reason_l for hint in MODEL_NOT_FOUND_HINTS):
        return True
    return "model unavailable" in reason_l or "no endpoints found" in reason_l


async def _fetch_provider_models_from_upstream(provider_name: str, provider_cfg: dict) -> list[str]:
    url = provider_cfg["base_url"].rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {provider_cfg['api_key']}"}
    client = await get_client(provider_cfg.get("proxy"))
    resp = await client.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return sorted({m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m})


async def _list_upstream_models(force_refresh: bool = False) -> list[str]:
    now = time.time()
    if not force_refresh and _models_cache["data"] and (now - _models_cache["ts"]) < 60:
        return list(_models_cache["data"])

    providers = (scheduler._config or {}).get("providers", {}) if scheduler else {}
    deduped_targets = {}
    for name, provider_cfg in providers.items():
        if not provider_cfg.get("base_url") or not provider_cfg.get("api_key"):
            continue
        key = (provider_cfg["base_url"].rstrip("/"), provider_cfg.get("proxy", ""))
        deduped_targets.setdefault(key, (name, provider_cfg))
    if not deduped_targets:
        return []
    names = []
    tasks = []
    for name, provider_cfg in deduped_targets.values():
        names.append(name)
        tasks.append(_fetch_provider_models_from_upstream(name, provider_cfg))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    models = set()
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            logger.warning(f"[MODELS] Fetch failed from {name}: {result}")
            continue
        models.update(result)
    model_list = sorted(models)
    if model_list:
        _models_cache["data"] = model_list
        _models_cache["ts"] = now
    return model_list


async def _get_bulk_model_test_lock() -> asyncio.Lock:
    global _bulk_model_test_lock
    if _bulk_model_test_lock is None:
        _bulk_model_test_lock = asyncio.Lock()
    return _bulk_model_test_lock


def _next_bulk_model_test_ts(now: datetime | None = None) -> float:
    current = now.astimezone() if now else datetime.now().astimezone()
    target = current.replace(hour=3, minute=0, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target.timestamp()


def _bulk_model_test_active_locked() -> bool:
    if _bulk_model_test.get("running"):
        return True
    for info in (_bulk_model_test.get("results") or {}).values():
        if info.get("status") in {"pending", "running"}:
            return True
    return False


async def _reconcile_bulk_model_test_state() -> None:
    global _bulk_model_test_task
    task_to_cancel: asyncio.Task | None = None
    should_finalize = False
    stale_report_snapshot: dict | None = None
    lock = await _get_bulk_model_test_lock()
    async with lock:
        now = time.time()
        for model, info in (_bulk_model_test.get("results") or {}).items():
            if info.get("status") != "running":
                continue
            started_at = float(info.get("started_at") or 0)
            if not started_at:
                continue
            if (now - started_at) <= (BULK_MODEL_TEST_TIMEOUT_SECONDS + 5):
                continue
            info["status"] = "error"
            info["reason"] = f"模型测试超时 {BULK_MODEL_TEST_TIMEOUT_SECONDS}s"
            info["finished_at"] = now
            info["updated_at"] = now
            info["latency_ms"] = max(int(info.get("latency_ms") or 0), BULK_MODEL_TEST_TIMEOUT_SECONDS * 1000)
            logger.warning(f"[MODEL_TEST] Reconciled stale running model: {model}")
        active = _bulk_model_test_active_locked()
        if _bulk_model_test.get("running") and not active:
            _bulk_model_test["running"] = False
            _bulk_model_test["finished_at"] = float(_bulk_model_test.get("finished_at") or now) or now
            should_finalize = True
        report_status = str(_bulk_model_test.get("report_status") or "")
        report_anchor_ts = float(_bulk_model_test.get("finished_at") or _bulk_model_test.get("started_at") or 0)
        if (
            _bulk_model_test.get("run_id")
            and report_status == "generating"
            and report_anchor_ts
            and (now - report_anchor_ts) > BULK_MODEL_TEST_REPORT_TIMEOUT_SECONDS
        ):
            stale_report_snapshot = dict(_bulk_model_test)
            stale_report_snapshot["results"] = {
                model: dict(result)
                for model, result in sorted((_bulk_model_test.get("results") or {}).items())
            }
            stale_report_snapshot["report_overview"] = dict(_bulk_model_test.get("report_overview", {}) or {})
            if _bulk_model_test_task is not None and not _bulk_model_test_task.done():
                task_to_cancel = _bulk_model_test_task
                _bulk_model_test_task = None
        if _bulk_model_test_task is not None and not _bulk_model_test_task.done() and not active:
            task_to_cancel = _bulk_model_test_task
            _bulk_model_test_task = None
    if task_to_cancel is not None:
        task_to_cancel.cancel()
    if stale_report_snapshot is not None:
        overview = dict(stale_report_snapshot.get("report_overview") or {})
        if not overview:
            overview = _build_bulk_model_test_overview(stale_report_snapshot)
        report_error = f"LLM 报告生成超时 {BULK_MODEL_TEST_REPORT_TIMEOUT_SECONDS}s"
        report_summary = _render_fallback_bulk_test_report(overview, llm_error=report_error)
        upsert_bulk_test_report(
            run_id=str(stale_report_snapshot.get("run_id") or ""),
            started_at=float(stale_report_snapshot.get("started_at") or 0),
            finished_at=float(stale_report_snapshot.get("finished_at") or 0),
            trigger=str(stale_report_snapshot.get("trigger") or ""),
            refresh=bool(stale_report_snapshot.get("refresh")),
            total=int(stale_report_snapshot.get("total") or 0),
            success=int(stale_report_snapshot.get("success") or 0),
            error=int(stale_report_snapshot.get("error") or 0),
            pending=int(stale_report_snapshot.get("pending") or 0),
            running_count=int(stale_report_snapshot.get("running_count") or 0),
            progress_pct=float(stale_report_snapshot.get("progress_pct") or 0.0),
            llm_status="fallback",
            llm_source="fallback",
            llm_model="",
            llm_error=report_error,
            summary=report_summary,
            stats=overview,
        )
        lock = await _get_bulk_model_test_lock()
        async with lock:
            if _bulk_model_test.get("run_id") == stale_report_snapshot.get("run_id") and _bulk_model_test.get("report_status") == "generating":
                _bulk_model_test["report_status"] = "fallback"
                _bulk_model_test["report_summary"] = report_summary
                _bulk_model_test["report_source"] = "fallback"
                _bulk_model_test["report_model"] = ""
                _bulk_model_test["report_error"] = report_error
                _bulk_model_test["report_overview"] = overview
    if should_finalize:
        snapshot = await _snapshot_bulk_model_test()
        with suppress(Exception):
            await _finalize_bulk_model_test_report(snapshot)


async def _snapshot_bulk_model_test() -> dict:
    lock = await _get_bulk_model_test_lock()
    async with lock:
        snapshot = dict(_bulk_model_test)
        snapshot["results"] = {
            model: dict(result)
            for model, result in sorted(_bulk_model_test.get("results", {}).items())
        }
        snapshot["report_overview"] = dict(_bulk_model_test.get("report_overview", {}) or {})
    results = list(snapshot["results"].values())
    total = int(snapshot.get("total") or len(results))
    success = sum(1 for item in results if item.get("status") == "success")
    error = sum(1 for item in results if item.get("status") == "error")
    running = sum(1 for item in results if item.get("status") == "running")
    done = success + error
    snapshot["total"] = total
    snapshot["success"] = success
    snapshot["error"] = error
    snapshot["running_count"] = running
    snapshot["done"] = done
    snapshot["pending"] = max(total - done - running, 0)
    snapshot["progress_pct"] = round((done / total) * 100, 1) if total else 0.0
    snapshot["next_run_at"] = _next_bulk_model_test_ts()
    return snapshot


def _normalize_bulk_test_reason(reason: str) -> str:
    text = re.sub(r"https?://\S+", "", str(reason or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180] if text else "未返回错误详情"


def _top_items_from_counter(counter: dict[str, int], limit: int = 5) -> list[dict]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _chunk_items(items: list[str], size: int = 8) -> list[list[str]]:
    size = max(1, int(size or 8))
    return [items[idx:idx + size] for idx in range(0, len(items), size)]


def _list_models_from_latest_completed_report(limit: int = 10) -> list[str] | None:
    page = get_bulk_test_report_page(limit=max(1, min(int(limit or 10), 20)), page=1)
    for item in page.get("items") or []:
        if float(item.get("finished_at") or 0) <= 0:
            continue
        stats = item.get("stats") or {}
        models = []
        seen = set()
        for model in stats.get("available_models") or []:
            normalized = _normalize_model_name(model)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            models.append(normalized)
        return models
    return None


def _build_bulk_model_test_overview(snapshot: dict) -> dict:
    results = snapshot.get("results", {}) or {}
    success_items = []
    error_items = []
    fallback_items = []
    reason_counts: dict[str, int] = {}
    reason_models: dict[str, list[str]] = {}
    reason_providers: dict[str, set[str]] = {}
    provider_counts: dict[str, dict] = {}

    for model, info in results.items():
        status = info.get("status", "")
        provider = info.get("provider", "") or "unknown"
        provider_item = provider_counts.setdefault(provider, {"provider": provider, "total": 0, "success": 0, "error": 0})
        provider_item["total"] += 1

        if status == "success":
            provider_item["success"] += 1
            success_items.append({
                "model": model,
                "provider": provider,
                "latency_ms": int(info.get("latency_ms") or 0),
                "reply_preview": str(info.get("reply_preview") or "")[:80],
            })
        elif status == "error":
            provider_item["error"] += 1
            reason_key = _normalize_bulk_test_reason(info.get("reason", ""))
            error_items.append({
                "model": model,
                "provider": provider,
                "reason": reason_key,
                "failover_count": int(info.get("failover_count") or 0),
            })
            reason_counts[reason_key] = reason_counts.get(reason_key, 0) + 1
            reason_models.setdefault(reason_key, []).append(model)
            reason_providers.setdefault(reason_key, set()).add(provider)
        if int(info.get("failover_count") or 0) > 0:
            fallback_items.append({
                "model": model,
                "provider": provider,
                "reason": _normalize_bulk_test_reason(info.get("reason", "")),
                "failover_count": int(info.get("failover_count") or 0),
                "effective_model": info.get("effective_model", ""),
            })

    success_latencies = [item["latency_ms"] for item in success_items if item["latency_ms"] > 0]
    total = int(snapshot.get("total") or len(results))
    success = int(snapshot.get("success") or len(success_items))
    error = int(snapshot.get("error") or len(error_items))
    available_models = sorted(item["model"] for item in success_items)
    failure_categories = [
        {
            "reason": reason,
            "count": len(sorted(models)),
            "providers": sorted(reason_providers.get(reason, set())),
            "models": sorted(models),
        }
        for reason, models in sorted(
            reason_models.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    ]
    overview = {
        "run_id": snapshot.get("run_id", ""),
        "trigger": snapshot.get("trigger", ""),
        "started_at": float(snapshot.get("started_at") or 0),
        "finished_at": float(snapshot.get("finished_at") or 0),
        "total": total,
        "success": success,
        "error": error,
        "pending": int(snapshot.get("pending") or 0),
        "running_count": int(snapshot.get("running_count") or 0),
        "success_rate": round((success / total) * 100, 1) if total else 0.0,
        "available_model_count": len(available_models),
        "available_models": available_models,
        "failure_category_count": len(failure_categories),
        "failure_categories": failure_categories,
        "fallback_model_count": len(fallback_items),
        "avg_success_latency_ms": round(sum(success_latencies) / len(success_latencies), 1) if success_latencies else 0.0,
        "top_error_reasons": _top_items_from_counter(reason_counts, limit=6),
        "top_provider_breakdown": sorted(provider_counts.values(), key=lambda item: (-item["total"], item["provider"]))[:8],
        "success_examples": sorted(success_items, key=lambda item: (item["latency_ms"] or 0, item["model"]))[:8],
        "fallback_examples": fallback_items[:8],
        "error_examples": error_items[:8],
    }
    return overview


def _render_fallback_bulk_test_report(overview: dict, llm_error: str = "") -> str:
    lines = []
    total = int(overview.get("total") or 0)
    success = int(overview.get("success") or 0)
    error = int(overview.get("error") or 0)
    success_rate = float(overview.get("success_rate") or 0.0)
    available_models = [str(item) for item in (overview.get("available_models") or []) if str(item).strip()]
    failure_categories = overview.get("failure_categories") or []

    lines.append("总体结论")
    lines.append(f"本轮共测试 {total} 个模型，成功 {success} 个，失败 {error} 个，成功率 {success_rate:.1f}%。")
    lines.append(
        f"出现过 failover 的模型有 {int(overview.get('fallback_model_count') or 0)} 个，成功模型平均延迟约 {overview.get('avg_success_latency_ms') or 0} ms。"
    )
    lines.append("")
    lines.append(f"可用模型（{len(available_models)}）")
    if available_models:
        for chunk in _chunk_items(available_models, size=6):
            lines.append(" - " + "、".join(chunk))
    else:
        lines.append(" - 无")
    lines.append("")
    lines.append(f"失败分类（{len(failure_categories)}）")
    if failure_categories:
        for item in failure_categories:
            models = [str(model) for model in (item.get("models") or []) if str(model).strip()]
            model_preview = "、".join(models[:12])
            if len(models) > 12:
                model_preview += f" 等 {len(models)} 个"
            provider_preview = "、".join(item.get("providers") or [])
            provider_suffix = f"；provider: {provider_preview}" if provider_preview else ""
            lines.append(
                f" - {item.get('reason', '未分类')}（{int(item.get('count') or 0)}）: {model_preview or '未记录模型'}{provider_suffix}"
            )
    else:
        lines.append(" - 无失败模型")
    lines.append("")
    lines.append("建议")
    if error:
        lines.append(" - 优先处理数量最多的失败分类，尤其是限流、余额或上游返回 5xx 的模型。")
    else:
        lines.append(" - 当前这批模型均已通过测试，可优先把可用模型同步给下游客户端。")
    if int(overview.get("fallback_model_count") or 0) > 0:
        lines.append(" - 这轮已出现 429 转移，建议继续保留只针对 429 的 apikey 级 failover。")
    if llm_error:
        lines.append(f" - LLM 总结不可用，已改用程序摘要。原因: {_normalize_bulk_test_reason(llm_error)}。")
    return "\n".join(lines)


async def _generate_bulk_test_report_summary(overview: dict) -> tuple[str, str, str, str]:
    prompt_payload = {
        "trigger": overview.get("trigger"),
        "total": overview.get("total"),
        "success": overview.get("success"),
        "error": overview.get("error"),
        "success_rate": overview.get("success_rate"),
        "available_model_count": overview.get("available_model_count"),
        "available_models": overview.get("available_models"),
        "failure_category_count": overview.get("failure_category_count"),
        "failure_categories": overview.get("failure_categories"),
        "fallback_model_count": overview.get("fallback_model_count"),
        "avg_success_latency_ms": overview.get("avg_success_latency_ms"),
        "top_error_reasons": overview.get("top_error_reasons"),
        "top_provider_breakdown": overview.get("top_provider_breakdown"),
        "success_examples": overview.get("success_examples"),
        "fallback_examples": overview.get("fallback_examples"),
        "error_examples": overview.get("error_examples"),
    }
    body = {
        "model": BULK_MODEL_TEST_REPORT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是网关测试报告撰写器。请严格基于输入 JSON 输出中文报告，不要编造任何未提供的数据。"
                    "输出必须包含 4 个小节，允许使用标题和项目符号，但不要使用 Markdown 表格。"
                    "1. 总体判断: 必须写出测试总数、成功数、失败数、成功率、failover 情况、平均成功延迟。"
                    "2. 可用模型: 必须逐个列出所有 available_models，不能省略；如果没有，明确写“无”。"
                    "3. 失败分类: 必须按 failure_categories 分类失败模型。每一类都要写明原因、数量；如果模型很多，可以列出代表模型并说明总数。"
                    "4. 建议: 给出 2 到 4 条面向网关维护的后续建议。"
                    "请保持结构清楚，适合直接展示在管理后台详情窗口。"
                ),
            },
            {
                "role": "user",
                "content": "请根据以下 JSON 生成测试报告：\n" + json.dumps(prompt_payload, ensure_ascii=False),
            },
        ],
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 1600,
    }
    default_model = _normalize_model_name((scheduler._config or {}).get("default_model", ""))
    requested_model = _normalize_model_name(BULK_MODEL_TEST_REPORT_MODEL)
    virtual_model, dynamic_route = _resolve_virtual_model(requested_model, default_model)
    model_attempts = _build_model_attempts(requested_model, dynamic_route)
    trace: dict = {}
    data = await asyncio.wait_for(
        _forward_non_stream(
            virtual_model,
            body,
            requested_model=requested_model,
            request_id=f"bulk-test-report-{uuid.uuid4().hex}",
            model_attempts=model_attempts,
            record_log=False,
            trace=trace,
            mutate_scheduler=False,
        ),
        timeout=45,
    )
    summary = _extract_reply_text(data).strip()
    final = trace.get("final") or {}
    used_model = _normalize_model_name(data.get("model", "")) or _normalize_model_name(final.get("model", "")) or requested_model
    return summary, "llm", used_model, ""


async def _finalize_bulk_model_test_report(snapshot: dict):
    run_id = snapshot.get("run_id", "")
    if not run_id:
        return
    overview = _build_bulk_model_test_overview(snapshot)
    await _get_bulk_model_test_lock()
    upsert_bulk_test_report(
        run_id=run_id,
        started_at=float(snapshot.get("started_at") or 0),
        finished_at=float(snapshot.get("finished_at") or 0),
        trigger=str(snapshot.get("trigger") or ""),
        refresh=bool(snapshot.get("refresh")),
        total=int(snapshot.get("total") or 0),
        success=int(snapshot.get("success") or 0),
        error=int(snapshot.get("error") or 0),
        pending=int(snapshot.get("pending") or 0),
        running_count=int(snapshot.get("running_count") or 0),
        progress_pct=float(snapshot.get("progress_pct") or 0.0),
        llm_status="generating",
        summary="",
        stats=overview,
    )
    lock = await _get_bulk_model_test_lock()
    async with lock:
        if _bulk_model_test.get("run_id") == run_id:
            _bulk_model_test["report_status"] = "generating"
            _bulk_model_test["report_summary"] = ""
            _bulk_model_test["report_source"] = ""
            _bulk_model_test["report_model"] = ""
            _bulk_model_test["report_error"] = ""
            _bulk_model_test["report_overview"] = overview

    report_error = ""
    report_source = ""
    report_model = ""
    report_status = "ready"
    try:
        report_summary, report_source, report_model, report_error = await _generate_bulk_test_report_summary(overview)
        if not report_summary:
            raise RuntimeError("LLM 报告为空")
    except Exception as e:
        report_error = str(e)
        report_source = "fallback"
        report_model = ""
        report_status = "fallback"
        report_summary = _render_fallback_bulk_test_report(overview, llm_error=report_error)

    upsert_bulk_test_report(
        run_id=run_id,
        started_at=float(snapshot.get("started_at") or 0),
        finished_at=float(snapshot.get("finished_at") or 0),
        trigger=str(snapshot.get("trigger") or ""),
        refresh=bool(snapshot.get("refresh")),
        total=int(snapshot.get("total") or 0),
        success=int(snapshot.get("success") or 0),
        error=int(snapshot.get("error") or 0),
        pending=int(snapshot.get("pending") or 0),
        running_count=int(snapshot.get("running_count") or 0),
        progress_pct=float(snapshot.get("progress_pct") or 0.0),
        llm_status=report_status,
        llm_source=report_source,
        llm_model=report_model,
        llm_error=report_error,
        summary=report_summary,
        stats=overview,
    )
    lock = await _get_bulk_model_test_lock()
    async with lock:
        if _bulk_model_test.get("run_id") == run_id:
            _bulk_model_test["report_status"] = report_status
            _bulk_model_test["report_summary"] = report_summary
            _bulk_model_test["report_source"] = report_source
            _bulk_model_test["report_model"] = report_model
            _bulk_model_test["report_error"] = report_error
            _bulk_model_test["report_overview"] = overview

def _sanitize_messages(messages: list, keep_images: bool = False) -> list:
    """
    Normalize messages for models that don't support tool calls or multimodal content.
    - Convert list content to plain text (unless keep_images=True)
    - Trim to last 40 messages to avoid context overflow
    """
    result = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        current = dict(m)

        # Flatten list content (multimodal) to plain text, unless model supports vision
        if isinstance(content, list) and not keep_images:
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            ).strip()
        current["content"] = content

        if LEGACY_TOOL_SANITIZE_ENABLED:
            # Legacy compatibility mode for upstreams that cannot ingest standard
            # OpenAI tool-call history. Disabled by default because it can cause
            # tool_calls to collapse into plain text.
            if role == "tool":
                result.append({"role": "user", "content": f"[Tool result]\n{content}"})
                continue
            if role == "assistant" and m.get("tool_calls"):
                calls_text = "\n".join(
                    f"[Tool call: {tc.get('function',{}).get('name','')}] {tc.get('function',{}).get('arguments','')}"
                    for tc in m["tool_calls"]
                )
                content = (content or "") + ("\n" if content else "") + calls_text
                result.append({"role": "assistant", "content": content})
                continue

        result.append(current)

    # Keep system + last 40 to avoid context overflow
    system = [m for m in result if m["role"] == "system"]
    non_system = [m for m in result if m["role"] != "system"]
    if len(non_system) > 40:
        logger.info(f"[SANITIZE] Trimmed {len(non_system)} messages to 40")
        non_system = non_system[-40:]
    return system + non_system


def _tools_to_system_prompt(tools: list) -> str:
    """Convert tools definition to a system prompt describing available functions."""
    lines = ["You have access to the following tools. When you want to call a tool, respond with ONLY a JSON object in this exact format (no other text):",
             '{"tool_call": {"name": "<tool_name>", "arguments": {<args>}}}',
             "",
             "Available tools:"]
    for t in tools:
        fn = t.get("function", {})
        params = fn.get("parameters", {}).get("properties", {})
        param_str = ", ".join(f"{k}: {v.get('type','any')}" for k, v in params.items())
        lines.append(f"- {fn.get('name','')}: {fn.get('description','')[:100]} | params: {param_str}")
    return "\n".join(lines)


def _parse_tool_call_from_text(text: str) -> dict | None:
    """Try to extract a tool call JSON from model response text."""
    import re
    # Format 1: {"tool_call": {"name": "...", "arguments": {...}}}
    match = re.search(r'\{["\s]*tool_call["\s]*:.*?\}(?:\s*\})', text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group())
            tc = obj.get("tool_call", {})
            if tc.get("name"):
                return tc
        except Exception:
            pass
    # Format 2: [Tool call: name] {...}  or  [Tool call: name] "args"
    match = re.search(r'\[Tool call:\s*(\w+)\]\s*(\{.*?\})', text, re.DOTALL)
    if match:
        name = match.group(1)
        try:
            args = json.loads(match.group(2))
            return {"name": name, "arguments": args}
        except Exception:
            return {"name": name, "arguments": match.group(2)}
    return None


def _wrap_tool_call_response(tc: dict, original_response: dict) -> dict:
    """Wrap a parsed tool call back into OpenAI tool_calls format."""
    import uuid
    call_id = "call_" + uuid.uuid4().hex[:24]
    args = tc.get("arguments", {})
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    choice = {
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": tc["name"], "arguments": args}
            }]
        },
        "finish_reason": "tool_calls"
    }
    return {**original_response, "choices": [choice]}


async def _compress_messages(messages: list, model: str, provider_cfg: dict) -> list:
    """Compress middle history into a summary, keep system + last 20."""
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    recent = non_system[-20:]
    middle = non_system[:-20]
    if not middle:
        return messages
    history_text = "\n".join(
        f"[{m['role']}]: {str(m.get('content') or m.get('tool_calls',''))[:200]}"
        for m in middle
    )
    summary_prompt = f"请用简洁的中文总结以下对话历史的关键信息、已完成的操作和重要结论，不超过300字：\n\n{history_text}"
    try:
        client = await get_client(provider_cfg.get("proxy"))
        url = provider_cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {provider_cfg['api_key']}", "Content-Type": "application/json"}
        resp = await client.post(url, headers=headers,
            json={"model": model, "messages": [{"role": "user", "content": summary_prompt}]},
            timeout=30)
        if resp.status_code == 200:
            summary = resp.json()["choices"][0]["message"]["content"]
            logger.info(f"[COMPRESS] {len(middle)} messages -> summary ({len(summary)} chars)")
            # Drop leading tool results after trim
            while recent and recent[0].get("role") == "tool":
                recent = recent[1:]
            return system + [{"role": "user", "content": f"[历史对话摘要]\n{summary}"}] + recent
    except Exception as e:
        logger.warning(f"[COMPRESS] Failed: {e}, falling back to trim")
    # Fallback: trim
    while recent and recent[0].get("role") == "tool":
        recent = recent[1:]
    return system + recent


async def _maybe_compress(body: dict, provider_cfg: dict, model: str) -> dict:
    msgs = body.get("messages", [])
    if len(msgs) <= 60:
        return body
    compressed = await _compress_messages(msgs, model, provider_cfg)
    return {**body, "messages": compressed}


def _clean_request_tool_calls(body: dict) -> dict:
    """Remove assistant messages with empty tool_call names from request history.
    Prevents models from inheriting corrupted tool calls in conversation context.
    """
    messages = body.get("messages", [])
    cleaned_messages = []
    for m in messages:
        if m.get("role") == "assistant":
            tc = m.get("tool_calls")
            if tc and isinstance(tc, list):
                valid_tc = [t for t in tc if t.get("function", {}).get("name", "").strip()]
                if len(valid_tc) < len(tc):
                    logger.info(f"[CLEAN_REQ] Removed {len(tc) - len(valid_tc)} empty tool calls from request history")
                if valid_tc:
                    m["tool_calls"] = valid_tc
                else:
                    m.pop("tool_calls", None)
                    if m.get("finish_reason") == "tool_calls":
                        m["finish_reason"] = "stop"
        cleaned_messages.append(m)
    return {**body, "messages": cleaned_messages}


def _build_upstream_request(endpoint: UpstreamEndpoint, body: dict, provider_cfg: dict, upstream_model: str | None = None):
    url = provider_cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {provider_cfg['api_key']}", "Content-Type": "application/json"}
    effective_model = _effective_upstream_model(endpoint, upstream_model)
    keep_images = effective_model in get_exclude_from_pool()
    # Clean empty tool calls from request history
    cleaned_body = _clean_request_tool_calls(body)
    sanitized_messages = _sanitize_messages(cleaned_body.get("messages", []), keep_images=keep_images)
    
    is_agentic_cli = _is_agentic_cli_request(sanitized_messages)
    final_body = {**cleaned_body, "model": effective_model, "messages": sanitized_messages}
    
    return url, headers, final_body, effective_model


def _is_retryable(status: int) -> bool:
    return status == 429

def _is_permanent_failure(status: int) -> bool:
    return status == 401 or status == 403


AGENTIC_PROMPT_HINTS = (
    "Hermes Agent",
    "You are Droid",
    "You are OpenWork",
    "interactive cli tool",
    "AskUser tool",
    "software engineering agent",
    "built by Factory",
)


def _is_agentic_cli_request(messages: list[dict]) -> bool:
    for message in messages:
        if message.get("role") != "system":
            continue
        content = str(message.get("content", ""))
        lower = content.lower()
        if any(hint.lower() in lower for hint in AGENTIC_PROMPT_HINTS):
            return True
    return False


def _request_has_tool_support(body: dict) -> bool:
    tools = body.get("tools")
    functions = body.get("functions")
    return bool(tools) or bool(functions) or body.get("tool_choice") is not None


def _should_normalize_tool_call_response(body: dict, messages: list[dict] | None = None) -> bool:
    if _request_has_tool_support(body):
        return True
    if messages is None:
        messages = body.get("messages", [])
    return _is_agentic_cli_request(messages)


def _find_trace_start(text: str) -> int:
    lower = text.lower()
    positions = []
    for marker in ("[tool call", "<tool_call", "</tool_call"):
        pos = lower.find(marker)
        if pos != -1:
            positions.append(pos)
    for marker in ("⛬", "计划已更新"):
        pos = text.find(marker)
        if pos != -1:
            positions.append(pos)
    return min(positions) if positions else -1


def _sanitize_assistant_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text

    cleaned = re.sub(r"(?is)<tool_call\b.*?</tool_call>", "", text)
    if _find_trace_start(cleaned) != -1:
        kept_lines = []
        for line in cleaned.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if not stripped:
                if kept_lines and kept_lines[-1] != "":
                    kept_lines.append("")
                continue
            if stripped == "计划已更新":
                continue
            if stripped.startswith("⛬"):
                continue
            if "[tool call" in lower or "<tool_call" in lower or "</tool_call" in lower:
                continue
            if re.match(r"(?i)^(let me\b|first,\s*let me\b|i'll\b|i will\b)", stripped):
                continue
            kept_lines.append(line)
        cleaned = "\n".join(kept_lines)
        trace_start = _find_trace_start(cleaned)
        if trace_start != -1:
            cleaned = cleaned[:trace_start]

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _sanitize_completion_response(data: dict) -> dict:
    try:
        for choice in data.get("choices", []):
            message = choice.get("message", {})
            content = message.get("content")
            if isinstance(content, str) and content:
                cleaned = _sanitize_assistant_text(content)
                if cleaned != content:
                    logger.warning("[TRACE_CLEAN] Removed leaked tool/planning text from assistant content")
                    message["content"] = cleaned or None
        return data
    except Exception as e:
        logger.warning(f"[TRACE_CLEAN] Failed to sanitize response content: {e}")
        return data


def _extract_reply_text(data: dict) -> str:
    try:
        return data["choices"][0]["message"].get("content") or ""
    except Exception:
        return ""


def _build_sse_chunk(base_event: dict, *, delta: dict, finish_reason=None, usage: dict | None = None) -> bytes:
    event = {
        "id": base_event.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": base_event.get("object") or "chat.completion.chunk",
        "created": base_event.get("created") or int(time.time()),
        "model": base_event.get("model") or "",
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if usage is not None:
        event["usage"] = usage
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def _completion_to_sse_chunks(data: dict) -> list[bytes]:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {}) or {}
    base_event = {
        "id": data.get("id"),
        "object": "chat.completion.chunk",
        "created": data.get("created"),
        "model": data.get("model"),
    }
    chunks: list[bytes] = []
    delta = {"role": "assistant"}
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    if content is not None:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    if len(delta) > 1:
        chunks.append(_build_sse_chunk(base_event, delta=delta, finish_reason=None))
    chunks.append(_build_sse_chunk(
        base_event,
        delta={},
        finish_reason=choice.get("finish_reason") or "stop",
        usage=data.get("usage"),
    ))
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def _serialize_tool_arguments(arguments) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if not stripped:
            return "{}"
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            return json.dumps({"input": arguments}, ensure_ascii=False)
    return json.dumps(arguments, ensure_ascii=False)


def _parse_text_tool_call(content: str) -> tuple[str | None, object, str]:
    if not isinstance(content, str) or not content.strip():
        return None, None, content

    decoder = json.JSONDecoder()

    bracket_match = re.search(r"(?is)\[Tool call:\s*([^\]\n]+)\]", content)
    if bracket_match:
        func_name = bracket_match.group(1).strip()
        trailing = content[bracket_match.end():].lstrip()
        try:
            args_obj, consumed = decoder.raw_decode(trailing)
        except json.JSONDecodeError:
            pass
        else:
            clean_content = (content[:bracket_match.start()] + trailing[consumed:]).strip()
            return func_name, args_obj, clean_content

    # Format: >[{"name": "func", "arguments": {...}}] or [{"name":...}]
    arr_match = re.search(r">?\s*(\[\s*\{.*?\}\s*\])", content, re.DOTALL)
    if arr_match:
        try:
            arr = json.loads(arr_match.group(1))
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                item = arr[0]
                func_name = item.get("name")
                arguments = item.get("arguments") or item.get("parameters") or {}
                if func_name:
                    clean_content = (content[:arr_match.start()] + content[arr_match.end():]).strip()
                    return func_name, arguments, clean_content
        except (json.JSONDecodeError, KeyError):
            pass

    tag_match = re.search(r"(?is)<tool_call>\s*(\{.*?\})\s*</tool_call>", content)
    if not tag_match:
        return None, None, content
    try:
        payload = json.loads(tag_match.group(1))
    except json.JSONDecodeError:
        return None, None, content
    if not isinstance(payload, dict):
        return None, None, content

    function_payload = payload.get("function") if isinstance(payload.get("function"), dict) else {}
    func_name = payload.get("name") or function_payload.get("name")
    arguments = payload.get("arguments", function_payload.get("arguments"))
    clean_content = (content[:tag_match.start()] + content[tag_match.end():]).strip()
    if isinstance(func_name, str):
        func_name = func_name.strip()
    return func_name or None, arguments, clean_content


def fix_tool_call_format(data: dict, original_body: dict) -> dict:
    """Fix models that output tool calls as text instead of proper tool_calls format.
    
    Detects content like: [Tool call: terminal] {"command":"echo hello"}
    or <tool_call>{"name":"write","arguments":{...}}</tool_call>
    and converts it to proper tool_calls array.
    """
    try:
        choices = data.get("choices", [])
        if not choices:
            return data
        choice = choices[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        
        # Check if tool_calls already exists and is valid
        existing_tool_calls = message.get("tool_calls")
        if existing_tool_calls and isinstance(existing_tool_calls, list) and len(existing_tool_calls) > 0:
            return data
        
        finish_reason = choice.get("finish_reason", "")
        if finish_reason != "stop":
            return data

        func_name, args_payload, clean_content = _parse_text_tool_call(content)
        if not func_name:
            return data

        call_id = f"call_{uuid.uuid4().hex[:24]}"
        tool_calls = [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": func_name,
                "arguments": _serialize_tool_arguments(args_payload)
            }
        }]

        logger.info(f"[TOOL_CALL_FIX] Converted content tool call: {func_name}")

        message["content"] = clean_content if clean_content else None
        message["tool_calls"] = tool_calls
        choice["finish_reason"] = "tool_calls"

        return data
    except Exception as e:
        logger.warning(f"[TOOL_CALL_FIX] Failed to fix tool call format: {e}")
        return data


def clean_empty_tool_calls(data: dict) -> dict:
    """Remove tool calls with empty or invalid function names.
    
    Models like gpt-oss-120b sometimes generate tool_calls with empty names,
    which corrupts the conversation context and breaks downstream processing.
    """
    try:
        choices = data.get("choices", [])
        for choice in choices:
            message = choice.get("message", {})
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                continue
            
            cleaned = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "") if isinstance(fn, dict) else ""
                if name and name.strip():
                    cleaned.append(tc)
                else:
                    logger.warning(f"[CLEAN_TC] Filtered out empty tool call name in response")
            
            if len(cleaned) < len(tool_calls):
                message["tool_calls"] = cleaned if cleaned else None
                if not cleaned:
                    choice["finish_reason"] = "stop"
                    logger.info(f"[CLEAN_TC] All tool calls were empty, changed finish_reason to 'stop'")
        
        return data
    except Exception as e:
        logger.warning(f"[CLEAN_TC] Failed to clean tool calls: {e}")
        return data


def _normalize_completion_data(data: dict, original_body: dict) -> dict:
    data = clean_empty_tool_calls(data)
    if _should_normalize_tool_call_response(original_body):
        data = fix_tool_call_format(data, original_body)
    if TRACE_FILTER_ENABLED and _is_agentic_cli_request(original_body.get("messages", [])):
        data = _sanitize_completion_response(data)
    return data


def _record_forward_trace(trace: dict | None, **entry):
    if trace is None:
        return
    item = dict(entry)
    attempts = trace.setdefault("attempts", [])
    attempts.append(item)
    trace["last"] = item
    if item.get("status") == "success":
        trace["final"] = item


async def _forward_non_stream(
    virtual_model: str,
    body: dict,
    requested_model: str = "",
    log_id: int = 0,
    request_id: str = "",
    model_attempts: list[str | None] | None = None,
    *,
    record_log: bool = True,
    trace: dict | None = None,
    mutate_scheduler: bool = True,
    proxy_override: str | None = None,
) -> dict:
    last_error = None
    last_status = 429
    attempts = model_attempts or [None]
    for upstream_model in attempts:
        attempt_virtual_model, candidates = _select_candidates(virtual_model, upstream_model)
        if not candidates:
            last_error = f"No available upstream for '{attempt_virtual_model}'"
            last_status = 404
            continue
        should_try_next_model = False
        for endpoint in candidates:
            provider_cfg = scheduler.get_provider_config(endpoint.provider)
            if not provider_cfg:
                continue
            effective_model = _effective_upstream_model(endpoint, upstream_model)
            request_proxy = _proxy_for_requested_model(requested_model or effective_model, provider_cfg, proxy_override=proxy_override)
            client = await get_client(request_proxy)
            _body = await _maybe_compress(body, provider_cfg, effective_model)
            url, headers, payload, effective_model = _build_upstream_request(endpoint, _body, provider_cfg, upstream_model)
            upstream_request_body = json.dumps(payload, ensure_ascii=False)
            if record_log and log_id:
                update_log(log_id, upstream_request_body=upstream_request_body)
            t0 = time.monotonic()
            try:
                resp = await client.post(url, headers=headers, json=payload)
                latency = int((time.monotonic() - t0) * 1000)
                raw_upstream_response = resp.text
                if resp.status_code == 200:
                    data = resp.json()
                    data = _normalize_completion_data(data, body)
                    usage = data.get("usage", {})
                    reply = _extract_reply_text(data)
                    downstream_response_body = JSONResponse(content=data).body.decode(errors="ignore")
                    trace_entry = {
                        "provider": endpoint.provider,
                        "api_key_hint": provider_cfg["api_key"][:8]+"...",
                        "model": effective_model,
                        "proxy": request_proxy,
                        "status": "success",
                        "reason": "",
                        "latency_ms": latency,
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "response_model": data.get("model", ""),
                    }
                    _record_forward_trace(trace, **trace_entry)
                    if record_log and log_id:
                        update_log(log_id, provider=endpoint.provider,
                                   api_key_hint=(provider_cfg["api_key"][:8]+"..."),
                                   model=effective_model, status="success", reason="", latency_ms=latency,
                                   prompt_tokens=usage.get("prompt_tokens", 0),
                                   completion_tokens=usage.get("completion_tokens", 0),
                                   response_body=downstream_response_body,
                                   upstream_response_body=raw_upstream_response)
                    return data
                try:
                    error_payload = resp.json()
                except Exception:
                    error_payload = resp.text[:160]
                reason = _extract_error_reason(resp.status_code, error_payload)
                if _is_model_not_found(resp.status_code, reason):
                    _record_forward_trace(trace, provider=endpoint.provider,
                                          api_key_hint=provider_cfg["api_key"][:8]+"...",
                                          model=effective_model, status="failover",
                                          reason=reason, latency_ms=latency,
                                          proxy=request_proxy, error_text=_payload_text(error_payload))
                    if record_log:
                        log_request(virtual_model=attempt_virtual_model, requested_model=requested_model,
                                    provider=endpoint.provider, api_key=provider_cfg["api_key"],
                                    model=effective_model, stream=False, status="failover",
                                    reason=reason, latency_ms=latency, request_id=request_id)
                    last_error = reason
                    last_status = resp.status_code
                    should_try_next_model = True
                    logger.warning(f"[MODEL_FALLBACK] {effective_model} unavailable on {endpoint.provider} -> next model")
                    break
                if _is_retryable(resp.status_code):
                    if mutate_scheduler:
                        scheduler.mark_failed(endpoint, reason)
                    _record_forward_trace(trace, provider=endpoint.provider,
                                          api_key_hint=provider_cfg["api_key"][:8]+"...",
                                          model=effective_model, status="failover",
                                          reason=reason, latency_ms=latency,
                                          proxy=request_proxy, error_text=_payload_text(error_payload))
                    if record_log:
                        log_request(virtual_model=attempt_virtual_model, requested_model=requested_model,
                                    provider=endpoint.provider, api_key=provider_cfg["api_key"],
                                    model=effective_model, stream=False, status="failover",
                                    reason=reason, latency_ms=latency, request_id=request_id)
                    last_error = reason
                    last_status = resp.status_code
                    logger.warning(f"[FAILOVER] {endpoint.key} -> next | Reason: {reason}")
                    continue
                if _is_permanent_failure(resp.status_code):
                    if mutate_scheduler:
                        endpoint.available = False
                        endpoint.failed_at = None
                        logger.warning(f"[DISABLED] {endpoint.key} | {reason} — remove manually")
                downstream_response_body = _error_response_body(reason)
                _record_forward_trace(trace, provider=endpoint.provider,
                                      api_key_hint=provider_cfg["api_key"][:8]+"...",
                                      model=effective_model, status="error", reason=reason,
                                      latency_ms=latency, proxy=request_proxy,
                                      error_text=_payload_text(error_payload))
                if record_log and log_id:
                    update_log(log_id, provider=endpoint.provider,
                               api_key_hint=(provider_cfg["api_key"][:8]+"..."),
                               model=effective_model, status="error", latency_ms=latency, reason=reason,
                               response_body=downstream_response_body,
                               upstream_response_body=raw_upstream_response)
                raise HTTPException(status_code=resp.status_code, detail=reason)
            except HTTPException:
                raise
            except Exception as e:
                downstream_response_body = _error_response_body(str(e))
                _record_forward_trace(trace, provider=endpoint.provider,
                                      api_key_hint=provider_cfg["api_key"][:8]+"...",
                                      model=effective_model, status="error", reason=str(e),
                                      proxy=request_proxy)
                if record_log and log_id:
                    update_log(log_id, provider=endpoint.provider,
                               api_key_hint=(provider_cfg["api_key"][:8]+"..."),
                               model=effective_model, status="error", reason=str(e),
                               response_body=downstream_response_body,
                               upstream_request_body=upstream_request_body)
                raise HTTPException(status_code=502, detail=str(e))
        if should_try_next_model:
            continue
        break
    final_reason = f"All upstreams failed: {last_error}"
    if record_log and log_id:
        update_log(log_id, status="error", reason=final_reason, response_body=_error_response_body(final_reason))
    raise HTTPException(status_code=last_status, detail=last_error or "All upstreams failed")


async def _forward_stream(virtual_model: str, body: dict, requested_model: str = "", log_id: int = 0, request_id: str = "", model_attempts: list[str | None] | None = None, proxy_override: str | None = None) -> AsyncGenerator[bytes, None]:
    last_error = "All upstreams failed"
    last_status = 429
    attempts = model_attempts or [None]
    for upstream_model in attempts:
        attempt_virtual_model, candidates = _select_candidates(virtual_model, upstream_model)
        if not candidates:
            last_error = f"No available upstream for '{attempt_virtual_model}'"
            last_status = 404
            continue
        should_try_next_model = False
        for endpoint in candidates:
            provider_cfg = scheduler.get_provider_config(endpoint.provider)
            if not provider_cfg:
                continue
            effective_model = _effective_upstream_model(endpoint, upstream_model)
            request_proxy = _proxy_for_requested_model(requested_model or effective_model, provider_cfg, proxy_override=proxy_override)
            client = await get_client(request_proxy)
            _body = await _maybe_compress(body, provider_cfg, effective_model)
            url, headers, payload, effective_model = _build_upstream_request(endpoint, _body, provider_cfg, upstream_model)
            upstream_request_body = json.dumps(payload, ensure_ascii=False)
            update_log(log_id, upstream_request_body=upstream_request_body)
            t0 = time.monotonic()
            try:
                if not payload.get("stream", True):
                    resp = await client.post(url, headers=headers, json=payload)
                    latency = int((time.monotonic() - t0) * 1000)
                    raw_upstream_response = resp.text
                    if resp.status_code == 200:
                        data = resp.json()
                        data = _normalize_completion_data(data, body)
                        usage = data.get("usage", {})
                        reply = _extract_reply_text(data)
                        downstream_chunks = _completion_to_sse_chunks(data)
                        downstream_response_body = b"".join(downstream_chunks).decode(errors="ignore")
                        update_log(log_id, provider=endpoint.provider,
                                   api_key_hint=(provider_cfg["api_key"][:8]+"..."),
                                   model=effective_model, status="success", reason="", latency_ms=latency,
                                   prompt_tokens=usage.get("prompt_tokens", 0),
                                   completion_tokens=usage.get("completion_tokens", 0),
                                   response_body=downstream_response_body,
                                   upstream_response_body=raw_upstream_response)
                        for sse_chunk in downstream_chunks:
                            yield sse_chunk
                        return
                    try:
                        error_payload = resp.json()
                    except Exception:
                        error_payload = resp.text[:160]
                    reason = _extract_error_reason(resp.status_code, error_payload)
                    if _is_model_not_found(resp.status_code, reason):
                        log_request(virtual_model=attempt_virtual_model, requested_model=requested_model,
                                    provider=endpoint.provider, api_key=provider_cfg["api_key"],
                                    model=effective_model, stream=True, status="failover",
                                    reason=reason, latency_ms=latency, request_id=request_id)
                        last_error = reason
                        last_status = resp.status_code
                        should_try_next_model = True
                        logger.warning(f"[MODEL_FALLBACK] {effective_model} unavailable on {endpoint.provider} -> next model")
                        break
                    if _is_retryable(resp.status_code):
                        scheduler.mark_failed(endpoint, reason)
                        log_request(virtual_model=attempt_virtual_model, requested_model=requested_model,
                                    provider=endpoint.provider, api_key=provider_cfg["api_key"],
                                    model=effective_model, stream=True, status="failover", reason=reason,
                                    latency_ms=latency, request_id=request_id)
                        last_error = reason
                        last_status = resp.status_code
                        logger.warning(f"[FAILOVER] {endpoint.key} -> next | Reason: {reason}")
                        continue
                    if _is_permanent_failure(resp.status_code):
                        endpoint.available = False
                        endpoint.failed_at = None
                        logger.warning(f"[DISABLED] {endpoint.key} | {reason} — remove manually")
                    downstream_response_body = _error_response_body(reason)
                    update_log(log_id, model=effective_model, status="error", reason=reason,
                               response_body=downstream_response_body,
                               upstream_response_body=raw_upstream_response)
                    raise HTTPException(status_code=resp.status_code, detail=reason)

                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        raw_body = await resp.aread()
                        try:
                            error_payload = json.loads(raw_body)
                        except Exception:
                            error_payload = raw_body.decode(errors="ignore")[:160]
                        reason = _extract_error_reason(resp.status_code, error_payload)
                        if _is_model_not_found(resp.status_code, reason):
                            log_request(virtual_model=attempt_virtual_model, requested_model=requested_model,
                                        provider=endpoint.provider, api_key=provider_cfg["api_key"],
                                        model=effective_model, stream=True, status="failover",
                                        reason=reason, request_id=request_id)
                            last_error = reason
                            last_status = resp.status_code
                            should_try_next_model = True
                            logger.warning(f"[MODEL_FALLBACK] {effective_model} unavailable on {endpoint.provider} -> next model")
                            break
                        if _is_retryable(resp.status_code):
                            scheduler.mark_failed(endpoint, reason)
                            log_request(virtual_model=attempt_virtual_model, requested_model=requested_model,
                                        provider=endpoint.provider, api_key=provider_cfg["api_key"],
                                        model=effective_model, stream=True, status="failover", reason=reason, request_id=request_id)
                            last_error = reason
                            last_status = resp.status_code
                            logger.warning(f"[FAILOVER] {endpoint.key} -> next | Reason: {reason}")
                            continue
                        if _is_permanent_failure(resp.status_code):
                            endpoint.available = False
                            endpoint.failed_at = None
                            logger.warning(f"[DISABLED] {endpoint.key} | {reason} — remove manually")
                        downstream_response_body = _error_response_body(reason)
                        update_log(log_id, model=effective_model, status="error", reason=reason,
                                   response_body=downstream_response_body,
                                   upstream_response_body=raw_body.decode(errors="ignore"))
                        raise HTTPException(status_code=resp.status_code, detail=reason)
                    collected = []
                    raw_stream_chunks = []
                    last_usage = {}
                    stream_completed = False
                    stream_aborted_reason = ""
                    yielded_any = False
                    record_aborted = False
                    done_chunk = b"data: [DONE]\n\n"
                    try:
                        async for chunk in resp.aiter_bytes():
                            raw_stream_chunks.append(chunk)
                            yielded_any = True
                            # Hold back [DONE] so we can inject a fix chunk before it
                            forwarded = chunk.replace(b"data: [DONE]\n\n", b"")
                            if forwarded:
                                yield forwarded
                            try:
                                for line in chunk.decode(errors="ignore").splitlines():
                                    if not line.startswith("data:"):
                                        continue
                                    payload_str = line[5:].strip()
                                    if payload_str == "[DONE]":
                                        continue
                                    d = json.loads(payload_str)
                                    if "usage" in d and d["usage"]:
                                        last_usage = d["usage"]
                                    choices = d.get("choices") or []
                                    if choices:
                                        c = choices[0].get("delta", {}).get("content") or ""
                                        if c:
                                            collected.append(c)
                            except Exception:
                                pass
                        # After stream ends, check for text tool_call and inject fix
                        full_content = "".join(collected)
                        func_name, args_payload, _ = _parse_text_tool_call(full_content)
                        if func_name:
                            call_id = f"call_{uuid.uuid4().hex[:24]}"
                            fix_chunk = {
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [{
                                            "index": 0,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {
                                                "name": func_name,
                                                "arguments": _serialize_tool_arguments(args_payload)
                                            }
                                        }]
                                    },
                                    "finish_reason": "tool_calls"
                                }]
                            }
                            yield f"data: {json.dumps(fix_chunk, ensure_ascii=False)}\n\n".encode()
                            logger.info(f"[TOOL_CALL_FIX] Stream injected fix chunk: {func_name}")
                        yield done_chunk
                        stream_completed = True
                    except asyncio.CancelledError:
                        stream_aborted_reason = "客户端中断了流式响应"
                        record_aborted = True
                        raise
                    except GeneratorExit:
                        stream_aborted_reason = "客户端关闭了流式响应"
                        record_aborted = True
                        raise
                    except Exception as e:
                        stream_aborted_reason = f"上游流式响应中断: {e}"
                        if yielded_any:
                            record_aborted = True
                            logger.warning(f"[STREAM_ABORTED] {endpoint.key} | {effective_model} | {stream_aborted_reason}")
                            return
                        raise
                    finally:
                        reply = "".join(collected)
                        raw_stream_body = b"".join(raw_stream_chunks).decode(errors="ignore")
                        usage = last_usage or _estimate_usage_from_body(body, reply)
                        latency = int((time.monotonic() - t0) * 1000)
                        if stream_completed:
                            update_log(log_id, provider=endpoint.provider,
                                       api_key_hint=(provider_cfg["api_key"][:8]+"..."),
                                       model=effective_model, status="success", reason="", latency_ms=latency,
                                       prompt_tokens=usage.get("prompt_tokens", 0),
                                       completion_tokens=usage.get("completion_tokens", 0),
                                       response_body=raw_stream_body,
                                       upstream_response_body=raw_stream_body)
                        elif record_aborted:
                            update_log(log_id, provider=endpoint.provider,
                                       api_key_hint=(provider_cfg["api_key"][:8]+"..."),
                                       model=effective_model, status="aborted", latency_ms=latency,
                                       reason=stream_aborted_reason or "流式响应在完成前中断",
                                       prompt_tokens=usage.get("prompt_tokens", 0),
                                       completion_tokens=usage.get("completion_tokens", 0),
                                       response_body=raw_stream_body,
                                       upstream_response_body=raw_stream_body)
                    if stream_completed:
                        return
            except HTTPException:
                raise
            except Exception as e:
                downstream_response_body = _error_response_body(str(e))
                update_log(log_id, provider=endpoint.provider,
                           api_key_hint=(provider_cfg["api_key"][:8]+"..."),
                           model=effective_model, status="error", reason=str(e),
                           response_body=downstream_response_body,
                           upstream_request_body=upstream_request_body)
                raise HTTPException(status_code=502, detail=str(e))
        if should_try_next_model:
            continue
        break
    update_log(log_id, status="error", reason=last_error, response_body=_error_response_body(last_error))
    raise HTTPException(status_code=last_status, detail=last_error)


def _extract_bulk_test_reply(data: dict) -> tuple[bool, str]:
    text = _extract_reply_text(data).strip()
    if text:
        return True, text
    try:
        tool_calls = data["choices"][0]["message"].get("tool_calls") or []
    except Exception:
        tool_calls = []
    if isinstance(tool_calls, list) and tool_calls:
        names = []
        for tool_call in tool_calls[:3]:
            fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
            name = (fn.get("name", "") or "").strip()
            if name:
                names.append(name)
        summary = ", ".join(names) if names else f"{len(tool_calls)} tool call(s)"
        return True, f"[tool_calls] {summary}"
    return False, ""


async def _set_bulk_model_test_result(model: str, **updates):
    lock = await _get_bulk_model_test_lock()
    async with lock:
        current = _bulk_model_test.setdefault("results", {}).setdefault(model, {})
        current_status = str(current.get("status") or "")
        incoming_status = str(updates.get("status") or "")
        if current_status in {"success", "error"} and incoming_status and incoming_status != current_status:
            return
        current.update(updates)
        current["updated_at"] = time.time()


async def _run_single_bulk_model_test(model: str) -> dict:
    started_at = time.time()
    request_id = f"bulk-model-test-{uuid.uuid4().hex}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": BULK_MODEL_TEST_PROMPT}],
        "stream": False,
        "temperature": 0,
        "max_tokens": BULK_MODEL_TEST_MAX_TOKENS,
    }
    default_model = _normalize_model_name((scheduler._config or {}).get("default_model", ""))
    virtual_model, dynamic_route = _resolve_virtual_model(model, default_model)
    model_attempts = _build_model_attempts(model, dynamic_route)
    await _set_bulk_model_test_result(
        model,
        status="running",
        request_id=request_id,
        started_at=started_at,
        finished_at=0.0,
        reason="",
        reply_preview="",
    )
    def _attempt_counts(local_trace: dict) -> tuple[int, int]:
        attempts = local_trace.get("attempts") or []
        return len(attempts), sum(1 for item in attempts if item.get("status") == "failover")

    def _build_success_result(data: dict, local_trace: dict, *, note: str = "", proxy_used: str = "") -> dict:
        has_reply, reply_preview = _extract_bulk_test_reply(data)
        final = local_trace.get("final") or {}
        effective_model = _normalize_model_name(final.get("model", ""))
        response_model = _normalize_model_name(data.get("model", ""))
        try:
            finish_reason = data["choices"][0].get("finish_reason", "") or ""
        except Exception:
            finish_reason = ""
        attempt_count, failover_count = _attempt_counts(local_trace)
        result = {
            "status": "success",
            "reason": note,
            "reply_preview": reply_preview[:240],
            "provider": final.get("provider", ""),
            "effective_model": effective_model or model,
            "response_model": response_model,
            "latency_ms": int(final.get("latency_ms") or 0),
            "prompt_tokens": int(final.get("prompt_tokens") or 0),
            "completion_tokens": int(final.get("completion_tokens") or 0),
            "attempt_count": attempt_count,
            "failover_count": failover_count,
            "started_at": started_at,
            "finished_at": time.time(),
            "finish_reason": finish_reason,
            "proxy_used": proxy_used,
            "proxy_retry": bool(proxy_used),
        }
        if effective_model and effective_model != model:
            result["status"] = "error"
            result["reason"] = f"请求 {model} 时实际命中 {effective_model}，已触发回退"
            return result
        if not has_reply:
            result["status"] = "error"
            result["reason"] = "模型返回为空"
            result["reply_preview"] = ""
        return result

    def _build_error_result(reason: str, local_trace: dict, *, proxy_used: str = "", proxy_retry: bool = False) -> dict:
        final = local_trace.get("last") or {}
        attempt_count, failover_count = _attempt_counts(local_trace)
        return {
            "status": "error",
            "reason": reason,
            "reply_preview": "",
            "provider": final.get("provider", ""),
            "effective_model": final.get("model", ""),
            "response_model": "",
            "latency_ms": int(final.get("latency_ms") or 0),
            "prompt_tokens": int(final.get("prompt_tokens") or 0),
            "completion_tokens": int(final.get("completion_tokens") or 0),
            "attempt_count": attempt_count,
            "failover_count": failover_count,
            "started_at": started_at,
            "finished_at": time.time(),
            "finish_reason": "",
            "proxy_used": proxy_used,
            "proxy_retry": proxy_retry,
        }

    async def _attempt_request(proxy_override: str | None = None) -> dict:
        local_trace: dict = {}
        normalized_proxy = _normalize_proxy_url(proxy_override)
        try:
            data = await asyncio.wait_for(
                _forward_non_stream(
                    virtual_model,
                    body,
                    requested_model=model,
                    request_id=request_id,
                    model_attempts=model_attempts,
                    record_log=False,
                    trace=local_trace,
                    mutate_scheduler=False,
                    proxy_override=normalized_proxy,
                ),
                timeout=BULK_MODEL_TEST_TIMEOUT_SECONDS,
            )
            return {"ok": True, "data": data, "trace": local_trace, "proxy": normalized_proxy}
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "status_code": 0,
                "reason": f"模型测试超时 {BULK_MODEL_TEST_TIMEOUT_SECONDS}s",
                "trace": local_trace,
                "proxy": normalized_proxy,
            }
        except HTTPException as exc:
            return {
                "ok": False,
                "status_code": int(exc.status_code or 0),
                "reason": str(exc.detail or exc.status_code),
                "trace": local_trace,
                "proxy": normalized_proxy,
            }
        except Exception as e:
            return {
                "ok": False,
                "status_code": 0,
                "reason": str(e),
                "trace": local_trace,
                "proxy": normalized_proxy,
            }

    first_attempt = await _attempt_request()
    if first_attempt.get("ok"):
        return _build_success_result(first_attempt["data"], first_attempt["trace"])

    return _build_error_result(
        str(first_attempt.get("reason") or "模型测试失败"),
        first_attempt.get("trace") or {},
        proxy_used=str(first_attempt.get("proxy") or ""),
        proxy_retry=bool(first_attempt.get("proxy")),
    )


async def _run_bulk_model_tests(models: list[str], trigger: str, refresh: bool):
    logger.info(f"[MODEL_TEST] Started bulk model test: trigger={trigger} total={len(models)}")
    semaphore = asyncio.Semaphore(BULK_MODEL_TEST_CONCURRENCY)

    async def _worker(model: str):
        async with semaphore:
            result = await _run_single_bulk_model_test(model)
            await _set_bulk_model_test_result(model, **result)

    try:
        await asyncio.gather(*[_worker(model) for model in models])
    except asyncio.CancelledError:
        lock = await _get_bulk_model_test_lock()
        async with lock:
            now = time.time()
            for model, info in _bulk_model_test.get("results", {}).items():
                if info.get("status") in {"pending", "running"}:
                    info["status"] = "error"
                    info["reason"] = "测试任务已取消"
                    info["finished_at"] = now
                    info["updated_at"] = now
            _bulk_model_test["running"] = False
            _bulk_model_test["finished_at"] = now
        snapshot = await _snapshot_bulk_model_test()
        with suppress(Exception):
            await _finalize_bulk_model_test_report(snapshot)
        logger.warning("[MODEL_TEST] Bulk model test cancelled")
        raise
    except Exception as e:
        logger.exception(f"[MODEL_TEST] Bulk model test failed: {e}")
        lock = await _get_bulk_model_test_lock()
        async with lock:
            _bulk_model_test["running"] = False
            _bulk_model_test["finished_at"] = time.time()
        snapshot = await _snapshot_bulk_model_test()
        with suppress(Exception):
            await _finalize_bulk_model_test_report(snapshot)
        return

    lock = await _get_bulk_model_test_lock()
    async with lock:
        _bulk_model_test["running"] = False
        _bulk_model_test["finished_at"] = time.time()
    snapshot = await _snapshot_bulk_model_test()
    with suppress(Exception):
        await _finalize_bulk_model_test_report(snapshot)
    logger.info(f"[MODEL_TEST] Finished bulk model test: trigger={trigger} total={len(models)} refresh={refresh}")


async def _start_bulk_model_test(*, models: list[str] | None = None, refresh: bool = False, trigger: str = "manual") -> dict:
    global _bulk_model_test, _bulk_model_test_task
    await _reconcile_bulk_model_test_state()
    normalized_models = sorted({_normalize_model_name(model) for model in (models or []) if _normalize_model_name(model)})
    already_running = False
    lock = await _get_bulk_model_test_lock()
    async with lock:
        if (_bulk_model_test_task is not None and not _bulk_model_test_task.done()) and _bulk_model_test_active_locked():
            already_running = True
    if already_running:
        logger.info("[MODEL_TEST] Bulk model test already running, returning current status")
        return await _snapshot_bulk_model_test()
    if not normalized_models:
        normalized_models = await _list_upstream_models(force_refresh=refresh)
    run_id = uuid.uuid4().hex
    started_at = time.time()
    lock = await _get_bulk_model_test_lock()
    async with lock:
        _bulk_model_test = {
            "run_id": run_id,
            "running": True,
            "trigger": trigger,
            "refresh": bool(refresh),
            "started_at": started_at,
            "finished_at": 0.0,
            "total": len(normalized_models),
            "results": {
                model: {
                    "status": "pending",
                    "reason": "",
                    "reply_preview": "",
                    "updated_at": started_at,
                }
                for model in normalized_models
            },
            "report_status": "pending",
            "report_summary": "",
            "report_source": "",
            "report_model": "",
            "report_error": "",
            "report_overview": {},
        }
        if not normalized_models:
            _bulk_model_test["running"] = False
            _bulk_model_test["finished_at"] = started_at
        else:
            _bulk_model_test_task = asyncio.create_task(_run_bulk_model_tests(normalized_models, trigger, refresh))
    upsert_bulk_test_report(
        run_id=run_id,
        started_at=started_at,
        finished_at=0.0,
        trigger=trigger,
        refresh=bool(refresh),
        total=len(normalized_models),
        success=0,
        error=0,
        pending=len(normalized_models),
        running_count=0,
        progress_pct=0.0,
        llm_status="pending",
        summary="",
        stats={},
    )
    return await _snapshot_bulk_model_test()


async def _bulk_model_test_scheduler_loop():
    while True:
        try:
            next_run_at = _next_bulk_model_test_ts()
            await asyncio.sleep(max(1, next_run_at - time.time()))
            snapshot = await _start_bulk_model_test(trigger="daily-03:00", refresh=True)
            logger.info(
                f"[MODEL_TEST] Scheduled bulk test trigger fired | running={snapshot.get('running')} total={snapshot.get('total')}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[MODEL_TEST] Scheduled bulk test failed: {e}")


# ── API ───────────────────────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    import uuid
    request_id = uuid.uuid4().hex
    default_model = _normalize_model_name(scheduler._config.get("default_model", ""))
    requested_model = _normalize_model_name(body.get("model", ""))
    virtual_model, dynamic_route = _resolve_virtual_model(requested_model, default_model)
    model_attempts = _build_model_attempts(requested_model, dynamic_route)
    req_str = json.dumps(body, ensure_ascii=False)
    log_id = insert_pending(virtual_model=virtual_model, requested_model=requested_model,
                            stream=body.get("stream", False), request_body=req_str, request_id=request_id)
    if body.get("stream", False):
        # Run stream but catch early errors before any bytes are yielded
        gen = _forward_stream(virtual_model, body, requested_model, log_id, request_id, model_attempts)
        # Peek at first chunk to catch immediate errors
        try:
            first_chunk = await gen.__anext__()
        except HTTPException:
            raise
        except StopAsyncIteration:
            return StreamingResponse(iter([]), media_type="text/event-stream")

        async def _stream_with_first(first, rest):
            try:
                yield first
                async for chunk in rest:
                    yield chunk
            except Exception as e:
                logger.warning(f"[STREAM_CLOSE] downstream stream closed after first chunk: {e}")
                return
            finally:
                aclose = getattr(rest, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        pass

        return StreamingResponse(_stream_with_first(first_chunk, gen), media_type="text/event-stream")
    return JSONResponse(content=await _forward_non_stream(virtual_model, body, requested_model, log_id, request_id, model_attempts))


@app.get("/v1/models")
async def list_models(refresh: bool = False):
    tested_models = _list_models_from_latest_completed_report()
    if tested_models is not None:
        model_ids = tested_models
    else:
        upstream_models = await _list_upstream_models(force_refresh=refresh)
        explicit_virtual_models = list(((scheduler._config or {}).get("virtual_models", {}) or {}).keys()) if scheduler else []
        model_ids = upstream_models or []
        if not model_ids:
            model_ids = explicit_virtual_models or scheduler.list_virtual_models()
        else:
            for virtual_model in explicit_virtual_models:
                model_ids.append(virtual_model)
            model_ids = sorted(set(model_ids))
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "chain-ai-gateway"}
        for m in model_ids
    ]}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Admin API ─────────────────────────────────────────────────────────────────


def _beijing_midnight_ts(now_ts: float | None = None) -> int:
    now_ts = now_ts or time.time()
    offset = 8 * 60 * 60
    return int(((int(now_ts) + offset) // 86400) * 86400 - offset)


def _collect_provider_live_status() -> dict[str, dict]:
    now = time.monotonic()
    cooldown = scheduler._cooldown if scheduler else 0
    providers = {}

    cfg_providers = (scheduler._config or {}).get("providers", {}) if scheduler else {}
    for name, cfg in cfg_providers.items():
        providers[name] = {
            "provider": name,
            "base_url": cfg.get("base_url", ""),
            "proxy": cfg.get("proxy", ""),
            "available": True,
            "recover_in": None,
            "endpoint_count": 0,
            "dynamic": False,
            "models": set(),
        }

    if not scheduler:
        return providers

    seen = set()
    for endpoints in scheduler._endpoints.values():
        for ep in endpoints:
            marker = (ep.provider, ep.model)
            if marker in seen:
                continue
            seen.add(marker)
            item = providers.setdefault(ep.provider, {
                "provider": ep.provider,
                "base_url": "",
                "proxy": "",
                "available": True,
                "recover_in": None,
                "endpoint_count": 0,
                "dynamic": False,
                "models": set(),
            })
            item["endpoint_count"] += 1
            item["models"].add(ep.model)
            item["dynamic"] = item["dynamic"] or ep.model == DYNAMIC_MODEL_SENTINEL
            is_available = ep.is_available(cooldown)
            item["available"] = item["available"] and is_available
            if not is_available and ep.failed_at:
                secs = max(int(cooldown - (now - ep.failed_at)), 0)
                if item["recover_in"] is None or secs < item["recover_in"]:
                    item["recover_in"] = secs

    for item in providers.values():
        item["models"] = sorted(m for m in item["models"] if m and m != DYNAMIC_MODEL_SENTINEL)
    return providers


@app.get("/admin/api/overview")
async def admin_overview():
    now_ts = time.time()
    today_start = _beijing_midnight_ts(now_ts)
    today = get_request_summary(today_start)
    live_status = _collect_provider_live_status()
    total_providers = len(live_status)
    available_providers = sum(1 for item in live_status.values() if item.get("available"))
    return {
        "today": {
            **today,
            "success_rate": round((today["success"] / today["total"]) * 100, 1) if today["total"] else 0.0,
        },
        "providers": {
            "total": total_providers,
            "available": available_providers,
            "down": max(total_providers - available_providers, 0),
        },
        "recent_issues": get_recent_issues(limit=8, since_ts=now_ts - 7 * 86400),
        "top_models": get_model_metrics(today_start, limit=8),
    }


@app.get("/admin/api/providers/status")
async def admin_provider_status(days: int = 7):
    days = max(1, min(days, 90))
    since_ts = time.time() - days * 86400
    metrics_map = {row["provider"]: row for row in get_provider_metrics(since_ts)}
    historical_provider_ids = set(get_all_provider_ids())
    live_status = _collect_provider_live_status()
    provider_names = sorted(set(live_status.keys()) | set(metrics_map.keys()) | historical_provider_ids)
    items = []
    for name in provider_names:
        live = live_status.get(name, {
            "provider": name,
            "base_url": "",
            "proxy": "",
            "available": True,
            "recover_in": None,
            "endpoint_count": 0,
            "dynamic": True,
            "models": [],
        })
        metric = metrics_map.get(name, {})
        total = int(metric.get("total") or 0)
        success = int(metric.get("success") or 0)
        items.append({
            "provider": name,
            "base_url": live.get("base_url", ""),
            "proxy": live.get("proxy", ""),
            "available": bool(live.get("available", True)),
            "recover_in": live.get("recover_in"),
            "dynamic": bool(live.get("dynamic", False)),
            "endpoint_count": int(live.get("endpoint_count") or 0),
            "models": live.get("models", []),
            "total": total,
            "success": success,
            "failover": int(metric.get("failover") or 0),
            "error": int(metric.get("error") or 0),
            "avg_latency_ms": int(metric.get("avg_latency_ms") or 0),
            "model_count": int(metric.get("model_count") or 0),
            "last_seen": float(metric.get("last_seen") or 0),
            "last_error_reason": metric.get("last_error_reason", ""),
            "last_error_ts": float(metric.get("last_error_ts") or 0),
            "success_rate": round((success / total) * 100, 1) if total else 0.0,
        })
    return {"days": days, "items": items}


@app.get("/admin/api/model-metrics")
async def admin_model_metrics(days: int = 7, limit: int = 50):
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 200))
    since_ts = time.time() - days * 86400
    return {"days": days, "items": get_model_metrics(since_ts, limit)}


@app.get("/admin/api/upstream-models")
async def admin_upstream_models(refresh: bool = False):
    items = await _list_upstream_models(force_refresh=refresh)
    fetched_at = float(_models_cache["ts"] or time.time())
    return {
        "items": items,
        "count": len(items),
        "fetched_at": fetched_at,
        "refresh": bool(refresh),
    }


@app.get("/admin/api/upstream-models/test-status")
async def admin_upstream_models_test_status():
    await _reconcile_bulk_model_test_state()
    return await _snapshot_bulk_model_test()


@app.get("/admin/api/upstream-models/test-reports")
async def admin_upstream_models_test_reports(limit: int = 3, page: int = 1):
    await _reconcile_bulk_model_test_state()
    limit = max(1, min(limit, 20))
    page = max(1, page)
    return get_bulk_test_report_page(limit=limit, page=page)


@app.post("/admin/api/upstream-models/test-all")
async def admin_upstream_models_test_all(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    models = body.get("models") if isinstance(body.get("models"), list) else None
    refresh = bool(body.get("refresh", False))
    return await _start_bulk_model_test(models=models, refresh=refresh, trigger="manual")

@app.get("/admin/api/model-stats")
async def model_stats_api(vmodel: str = "free"):
    return scheduler.get_model_stats(vmodel)


@app.post("/admin/api/model-stats/refresh")
async def refresh_model_stats():
    scheduler._refresh_model_stats()
    return {"ok": True}


# In-memory store for model probe results: {model: {result, running, ts}}
_model_probe: dict = {}

TEST_PROMPT = """You are a helpful assistant. Respond ONLY with a valid JSON object (no markdown, no explanation):
{
  "model_name": "your actual model name/version",
  "context_window": <number or null>,
  "max_output_tokens": <number or null>,
  "knowledge_cutoff": "YYYY-MM or null",
  "supports_function_calling": <true/false/null>,
  "supports_vision": <true/false/null>,
  "supports_streaming": <true/false/null>,
  "languages": "top supported languages, comma separated, max 5",
  "notes": "key limitations or features, max 20 words"
}"""

async def _run_model_probe(vmodel: str, model: str):
    _model_probe[model] = {"running": True, "result": None, "ts": time.time()}
    try:
        candidates = [ep for ep in scheduler._endpoints.get(vmodel, [])
                      if ep.model == model and ep.is_available(scheduler._cooldown)]
        if not candidates:
            _model_probe[model] = {"running": False, "result": None, "error": "no available upstream", "ts": time.time()}
            return
        ep = candidates[0]
        provider_cfg = scheduler.get_provider_config(ep.provider)
        client = await get_client(provider_cfg.get("proxy"))
        url = provider_cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {provider_cfg['api_key']}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        resp = await client.post(url, headers=headers,
            json={"model": model, "messages": [{"role": "user", "content": TEST_PROMPT}]},
            timeout=60)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"] or ""
            import re, json as _json
            m = re.search(r'\{[\s\S]*\}', text)
            info = _json.loads(m.group()) if m else {}
            _model_probe[model] = {"running": False, "result": info, "latency_ms": latency, "ts": time.time()}
            logger.info(f"[PROBE] {model} done in {latency}ms")
        else:
            _model_probe[model] = {"running": False, "result": None, "error": f"HTTP {resp.status_code}", "ts": time.time()}
    except Exception as e:
        _model_probe[model] = {"running": False, "result": None, "error": str(e), "ts": time.time()}
        logger.warning(f"[PROBE] {model} failed: {e}")


@app.post("/admin/api/probe-model")
async def probe_model(request: Request):
    body = await request.json()
    vmodel = body.get("vmodel", "free")
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="model required")
    if _model_probe.get(model, {}).get("running"):
        return {"ok": True, "status": "already_running"}
    asyncio.create_task(_run_model_probe(vmodel, model))
    return {"ok": True, "status": "started"}


@app.get("/admin/api/probe-model")
async def get_probe_result(model: str):
    return _model_probe.get(model, {"running": False, "result": None})


@app.delete("/admin/api/logs")
async def clear_logs():
    from db import _conn
    with _conn() as c:
        c.execute("DELETE FROM request_log")
        c.execute("DELETE FROM stats")
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
    return {"ok": True}


@app.get("/admin/api/logs")
async def api_logs(limit: int = 50, page: int = 1, status: str = "", q: str = ""):
    return get_log_page(limit=limit, page=page, status=status.strip(), search=q.strip())


@app.get("/admin/api/logs/{log_id}")
async def api_log_detail(log_id: int):
    log = get_log_by_id(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    return log


@app.get("/admin/api/stats")
async def api_stats():
    return get_stats()


@app.get("/admin/api/config")
async def api_config():
    return load_config()


@app.post("/admin/api/config/providers")
async def update_providers(request: Request):
    updates = await request.json()
    cfg = load_config()
    for name, fields in updates.items():
        if name in cfg.get("providers", {}):
            if fields.get("api_key"): cfg["providers"][name]["api_key"] = fields["api_key"]
            if fields.get("base_url"): cfg["providers"][name]["base_url"] = fields["base_url"]
            cfg["providers"][name]["proxy"] = fields.get("proxy", "")
        else:
            cfg.setdefault("providers", {})[name] = fields
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    return {"ok": True}


@app.post("/admin/api/config/exclude_from_pool")
async def save_exclude_from_pool(request: Request):
    models = await request.json()  # expects a list of strings
    cfg = load_config()
    cfg["exclude_from_pool"] = models
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    scheduler.reload(cfg)
    return {"ok": True}


@app.put("/admin/api/config/providers/{name}/rename")
async def rename_provider(name: str, request: Request):
    body = await request.json()
    new_name = body.get("new_name", "").strip()
    if not new_name or new_name == name:
        return {"ok": False, "detail": "invalid name"}
    cfg = load_config()
    providers = cfg.get("providers", {})
    if name not in providers:
        raise HTTPException(status_code=404, detail="not found")
    if new_name in providers:
        raise HTTPException(status_code=409, detail="name already exists")
    providers[new_name] = providers.pop(name)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    return {"ok": True}


@app.delete("/admin/api/config/providers/{name}")
async def delete_provider(name: str):
    cfg = load_config()
    cfg.get("providers", {}).pop(name, None)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    return {"ok": True}


@app.get("/admin/api/analyze")
async def analyze():
    cfg = scheduler._config
    stats = get_stats()
    from db import get_logs
    logs = get_logs(limit=500)

    total = len(logs)
    success = sum(1 for l in logs if l['status'] == 'success')
    errors = sum(1 for l in logs if l['status'] == 'error')
    failovers = sum(1 for l in logs if l['status'] == 'failover')
    avg_latency = int(sum(l['latency_ms'] for l in logs if l['latency_ms']) / max(sum(1 for l in logs if l['latency_ms']), 1))
    total_tokens = sum(s['total_prompt_tokens'] + s['total_completion_tokens'] for s in stats)

    error_reasons = {}
    for l in logs:
        if l['status'] == 'error' and l['reason']:
            error_reasons[l['reason']] = error_reasons.get(l['reason'], 0) + 1
    top_errors = sorted(error_reasons.items(), key=lambda x: -x[1])[:5]

    upstream_count = len(cfg.get('virtual_models', {}).get(cfg.get('default_model', 'free'), []))

    prompt = f"""你是一个 AI 网关运维专家。请根据以下运行数据，用中文给出简洁的分析报告和改进建议。

## 运行数据摘要
- 总请求数: {total}
- 成功: {success} ({round(success/max(total,1)*100)}%)
- 失败: {errors} ({round(errors/max(total,1)*100)}%)
- Failover 切换: {failovers} 次
- 平均延迟: {avg_latency}ms
- 总 Token 消耗: {total_tokens:,}
- 当前上游数量: {upstream_count}
- 主要错误原因: {dict(top_errors)}
- 模型统计: {[{'model': s['model'], 'success': s['success'], 'failure': s['failure']} for s in stats]}

## 请分析
1. 当前网关的健康状况（1-2句）
2. 主要瓶颈或风险点
3. 具体改进建议（3条以内，每条一句话）

回复要简洁，总字数不超过300字。"""

    # Call via gateway itself so it gets logged
    cfg = scheduler._config
    default_model = cfg.get("default_model", "")
    client = await get_client()
    try:
        resp = await client.post("http://127.0.0.1:" + str(cfg.get("server", {}).get("port", 8088)) + "/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": "Bearer admin"},
            json={"model": default_model or "any", "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        data = resp.json()
        return {"analysis": data["choices"][0]["message"]["content"]}
    except Exception as e:
        return {"error": str(e)}



@app.post("/admin/api/restart")
async def restart_service():
    """重启服务（使用soft restart）"""
    import subprocess
    import sys
    try:
        # 获取当前脚本路径
        script = sys.argv[0] if sys.argv[0] else 'main.py'
        # 使用execv重启
        os.execv(sys.executable, [sys.executable, script])
        return {"ok": True, "message": "Restarting..."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/favicon.ico")
async def favicon():
    # Return a simple SVG favicon as ICO
    from fastapi.responses import Response
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="16" fill="#7c8cf8"/><text x="16" y="22" text-anchor="middle" font-size="18" fill="white">G</text></svg>'
    return Response(content=svg, media_type="image/svg+xml")



@app.get("/admin/api/daily-stats")
async def daily_stats(days: int = 31):
    import sqlite3 as _sq
    from db import DB_PATH
    conn = _sq.connect(DB_PATH)
    rows = conn.execute("""
        SELECT date(ts,'unixepoch','+8 hours') as d,
               SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as ok,
               SUM(CASE WHEN status='failover' THEN 1 ELSE 0 END) as fail,
               SUM(CASE WHEN status='success' THEN prompt_tokens+completion_tokens ELSE 0 END) as tokens,
               AVG(CASE WHEN status='success' AND latency_ms>0 THEN latency_ms END) as avg_lat
        FROM request_log
        WHERE ts >= strftime('%s','now','-' || ? || ' days')
        GROUP BY d ORDER BY d
    """, (days,)).fetchall()
    conn.close()
    return [{"date": r[0], "ok": r[1], "fail": r[2], "tokens": r[3], "avg_lat": int(r[4] or 0)} for r in rows]


@app.get("/admin/api/network-stats")
async def network_stats():
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        ifaces = {}
        for line in lines[2:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            name = parts[0].rstrip(":")
            if name == "lo":
                continue
        ifaces[name] = {"rx": int(parts[1]), "tx": int(parts[9]),
                        "rx_pkt": int(parts[2]), "tx_pkt": int(parts[10]),
                        "rx_err": int(parts[3]), "tx_err": int(parts[11]),
                        "rx_drop": int(parts[4]), "tx_drop": int(parts[12])}
        # pick the interface with most traffic
        if not ifaces:
            return {"error": "no interface"}
        iface = max(ifaces, key=lambda k: ifaces[k]["rx"] + ifaces[k]["tx"])
        now = time.monotonic()
        prev = getattr(network_stats, "_prev", None)
        network_stats._prev = (now, ifaces[iface]["rx"], ifaces[iface]["tx"],
                               ifaces[iface]["rx_pkt"], ifaces[iface]["tx_pkt"])
        if prev is None:
            return {"iface": iface, "rx_bps": 0, "tx_bps": 0, "rx_pps": 0, "tx_pps": 0,
                    "rx_err": ifaces[iface]["rx_err"], "tx_err": ifaces[iface]["tx_err"],
                    "rx_drop": ifaces[iface]["rx_drop"], "tx_drop": ifaces[iface]["tx_drop"],
                    "rx_bytes": ifaces[iface]["rx"], "tx_bytes": ifaces[iface]["tx"]}
        dt = now - prev[0]
        if dt <= 0:
            return {"iface": iface, "rx_bps": 0, "tx_bps": 0}
        rx_bps = max(0, (ifaces[iface]["rx"] - prev[1]) / dt)
        tx_bps = max(0, (ifaces[iface]["tx"] - prev[2]) / dt)
        rx_pps = max(0, (ifaces[iface]["rx_pkt"] - prev[3]) / dt)
        tx_pps = max(0, (ifaces[iface]["tx_pkt"] - prev[4]) / dt)
        return {"iface": iface, "rx_bps": rx_bps, "tx_bps": tx_bps,
                "rx_pps": rx_pps, "tx_pps": tx_pps,
                "rx_err": ifaces[iface]["rx_err"], "tx_err": ifaces[iface]["tx_err"],
                "rx_drop": ifaces[iface]["rx_drop"], "tx_drop": ifaces[iface]["tx_drop"],
                "rx_bytes": ifaces[iface]["rx"], "tx_bytes": ifaces[iface]["tx"]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/admin/api/system-stats")
async def system_stats():
    import shutil
    try:
        # CPU
        with open("/proc/stat") as f:
            cpu = f.readline().split()
        idle, total = int(cpu[4]), sum(int(x) for x in cpu[1:])
        if not hasattr(system_stats, "_prev"):
            system_stats._prev = (idle, total)
            cpu_pct = 0.0
        else:
            p_idle, p_total = system_stats._prev
            d_idle, d_total = idle - p_idle, total - p_total
            cpu_pct = round((1 - d_idle / max(d_total, 1)) * 100, 1)
            system_stats._prev = (idle, total)
        # Memory
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.split()[0])
        mem_total = mem["MemTotal"]
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        mem_used = mem_total - mem_avail
        mem_pct = round(mem_used / mem_total * 100, 1)
        # Disk
        disk = shutil.disk_usage("/")
        disk_pct = round(disk.used / disk.total * 100, 1)
        return {
            "cpu": cpu_pct,
            "mem": {"used": mem_used, "total": mem_total, "pct": mem_pct},
            "disk": {"used": disk.used, "total": disk.total, "pct": disk_pct},
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/admin/api/add-openrouter-key")
async def add_openrouter_key(request: Request):
    """
    Accept JSON: {"api_key": "sk-or-v1-..."} or {"api_keys": ["sk-or-v1-...", ...]}
    Auto-creates provider(s) and adds stepfun upstreams to 'free' virtual model.
    """
    body = await request.json()
    keys = body.get("api_keys") or ([body["api_key"]] if body.get("api_key") else [])
    if not keys:
        raise HTTPException(status_code=400, detail="api_key or api_keys required")

    cfg = load_config()
    providers = cfg.setdefault("providers", {})
    vmodels = cfg.setdefault("virtual_models", {})
    free_ups = vmodels.setdefault("free", [])

    # Find next provider number
    existing_nums = [
        int(k.replace("openrouter_", ""))
        for k in providers if k.startswith("openrouter_") and k.replace("openrouter_", "").isdigit()
    ]
    next_num = max(existing_nums, default=0) + 1

    added = []
    # models to assign per key: from request body or fall back to existing models in vmodel
    req_models = body.get("models")
    if not req_models:
        existing_models = list(set(u["model"] for u in free_ups)) if free_ups else ["openai/gpt-4o-mini:free"]
        req_models = existing_models

    for key in keys:
        key = key.strip()
        if not key:
            continue
        if any(p.get("api_key") == key for p in providers.values()):
            continue
        name = f"openrouter_{next_num}"
        next_num += 1
        providers[name] = {"base_url": "https://openrouter.ai/api/v1", "api_key": key, "proxy": ""}
        for model in req_models:
            free_ups.append({"model": model, "priority": 1, "provider": name, "weight": 1})
        added.append(name)

    if added:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        scheduler.reload(cfg)

    return {"ok": True, "added": added, "count": len(added)}


@app.get("/admin/api/upstreams")
async def api_upstreams():
    cooldown = scheduler._cooldown
    now = time.monotonic()
    result = []
    for vmodel in scheduler.list_virtual_models():
        eps = scheduler._endpoints.get(vmodel, [])
        rr_pos = scheduler._rr_index.get(vmodel, 0) % max(len(eps), 1)
        for i, ep in enumerate(eps):
            recover_in = None
            if not ep.available and ep.failed_at:
                secs = int(cooldown - (now - ep.failed_at))
                recover_in = max(secs, 0)
            result.append({
                "virtual_model": vmodel,
                "provider": ep.provider,
                "model": ep.model,
                "priority": ep.priority,
                "weight": ep.weight,
                "available": ep.available,
                "recover_in": recover_in,
                "is_next": i == rr_pos,  # current RR pointer
            })
    return result


@app.post("/admin/api/reset-upstream")
async def reset_upstream(request: Request):
    body = await request.json()
    provider, model = body.get("provider"), body.get("model")
    for eps in scheduler._endpoints.values():
        for ep in eps:
            if ep.provider == provider and ep.model == model:
                ep.available = True
                ep.failed_at = None
    return {"ok": True}



@app.post("/admin/api/validate-model")
async def validate_model(request: Request):
    """Test a model through the full gateway message pipeline (sanitize etc.) but direct HTTP call."""
    import random as _random, re as _re
    body = await request.json()
    provider = body.get("provider")
    model = body.get("model")
    cfg = load_config()
    p = cfg.get("providers", {}).get(provider)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")

    a, b = _random.randint(1000, 9999), _random.randint(1000, 9999)
    expected = a + b
    messages = [{"role": "user", "content": f'Respond ONLY with valid JSON (no markdown): {{"greeting":"hi","math_result":<answer to {a}+{b}>}}'}]

    # Run through gateway message sanitizer
    messages = _sanitize_messages(messages)

    url = p["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}
    client = await get_client(p.get("proxy"))
    t0 = time.monotonic()
    try:
        resp = await client.post(url, headers=headers,
            json={"model": model, "messages": messages}, timeout=20)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}

        reply = resp.json()["choices"][0]["message"]["content"] or ""

        math_ok = False
        try:
            m = _re.search(r'\{[\s\S]*\}', reply)
            if m:
                math_ok = int(json.loads(m.group()).get("math_result", -1)) == expected
        except Exception:
            pass
        if not math_ok:
            math_ok = any(int(n) == expected for n in _re.findall(r'\b\d+\b', reply))

        if not math_ok:
            return {"ok": False, "latency_ms": latency,
                    "error": f"数学验证失败: {a}+{b}={expected}，模型回复: {reply[:120]}"}
        return {"ok": True, "latency_ms": latency, "reply": reply}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.monotonic()-t0)*1000), "error": str(e)}


@app.post("/admin/api/test-upstream")
async def test_upstream(request: Request):
    import random as _random, re as _re
    body = await request.json()
    provider = body.get("provider")
    model = body.get("model")
    validate = body.get("validate", False)  # if True, do math check
    cfg = load_config()
    p = cfg.get("providers", {}).get(provider)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    url = p["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}

    if validate:
        a, b = _random.randint(1000, 9999), _random.randint(1000, 9999)
        expected = a + b
        content = (
            f'Reply ONLY with a JSON object, no markdown:\n'
            f'{{"greeting":"hi","math_result":{expected - 1}}}\n'  # wrong example to not leak answer
            f'Actually compute: what is {a}+{b}? '
            f'Reply ONLY with: {{"greeting":"hi","math_result":<your answer>}}'
        )
        # cleaner prompt
        content = f'Respond ONLY with valid JSON (no markdown): {{"greeting":"hi","math_result":<answer to {a}+{b}>}}'
    else:
        content = "say hi in one word"

    payload = {"model": model, "messages": [{"role": "user", "content": content}]}
    client = await get_client(p.get("proxy"))
    t0 = time.monotonic()
    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=15)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            reply = data["choices"][0]["message"]["content"] or ""
            if validate:
                # Extract math_result from JSON or plain number
                math_ok = False
                try:
                    m = _re.search(r'\{[\s\S]*\}', reply)
                    if m:
                        obj = json.loads(m.group())
                        math_ok = int(obj.get("math_result", -1)) == expected
                except Exception:
                    pass
                if not math_ok:
                    # try plain number in reply
                    nums = _re.findall(r'\b\d+\b', reply)
                    math_ok = any(int(n) == expected for n in nums)
                if not math_ok:
                    return {"ok": False, "latency_ms": latency,
                            "error": f"数学验证失败: {a}+{b}={expected}，模型回复: {reply[:100]}"}
            return {"ok": True, "latency_ms": latency, "reply": reply}
        return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.monotonic()-t0)*1000), "error": str(e)}


@app.get("/admin/api/provider-models/{provider_name}")
async def fetch_provider_models(provider_name: str):
    """Fetch model list from upstream provider using its API key."""
    cfg = load_config()
    p = cfg.get("providers", {}).get(provider_name)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    try:
        return await _fetch_provider_models_from_upstream(provider_name, p)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/admin/api/virtual-models")
async def get_virtual_models():
    return load_config().get("virtual_models", {})


@app.post("/admin/api/virtual-models")
async def save_virtual_models(request: Request):
    """Replace entire virtual_models config."""
    vmodels = await request.json()
    cfg = load_config()
    cfg["virtual_models"] = vmodels
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    return {"ok": True}


@app.get("/admin", response_class=HTMLResponse)
async def admin_ui():
    with open(os.path.join(BASE_DIR, "static", "index.html")) as f:
        content = f.read()
    from fastapi.responses import HTMLResponse as HR
    return HR(content=content, headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = cfg.get("server", {}).get("port", 8088)
    uvicorn.run("main:app", host=host, port=port, reload=False)
