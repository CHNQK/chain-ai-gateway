import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from fastapi import HTTPException

import main
from db import get_bulk_test_report_page
from scheduler import Scheduler


REGION_REASON_NEEDLE = "not available in your region"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trace gateway upstream requests for specific models."
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Model id to test. Repeat for multiple models.",
    )
    parser.add_argument(
        "--latest-region-failures",
        action="store_true",
        help="Auto-load models from the latest bulk-test report whose failure reason contains region restriction.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit auto-loaded models. Ignored when explicit --model values are provided.",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="Force this proxy for upstream requests. Defaults to direct unless --use-region-proxy is set.",
    )
    parser.add_argument(
        "--use-region-proxy",
        action="store_true",
        help="Force main.REGION_RETRY_PROXY for upstream requests.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=main.BULK_MODEL_TEST_TIMEOUT_SECONDS,
        help="Per-model timeout in seconds.",
    )
    parser.add_argument(
        "--body-chars",
        type=int,
        default=400,
        help="Max chars to print for upstream request/response bodies.",
    )
    parser.add_argument(
        "--jsonl",
        default="",
        help="Optional JSONL output path for all events.",
    )
    return parser.parse_args()


def shorten_text(value, limit: int) -> str:
    text = main._payload_text(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


class EventWriter:
    def __init__(self, jsonl_path: str, body_chars: int):
        self.body_chars = max(80, int(body_chars or 400))
        self._file = None
        if jsonl_path:
            path = Path(jsonl_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("w", encoding="utf-8")

    def emit(self, event_type: str, **payload):
        event = {
            "ts": round(time.time(), 3),
            "event": event_type,
            **payload,
        }
        line = json.dumps(event, ensure_ascii=False)
        print(line, flush=True)
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


def summarize_candidates(candidates: list, limit: int = 12) -> dict:
    names = [f"{ep.provider}:{ep.model}" for ep in candidates]
    return {
        "count": len(names),
        "sample": names[:limit],
        "truncated": max(len(names) - limit, 0),
    }


def _normalize_model_list(items) -> list[str]:
    models = []
    seen = set()
    for item in items or []:
        model = main._normalize_model_name(item)
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def resolve_models(args: argparse.Namespace, writer: EventWriter) -> list[str]:
    explicit_models = _normalize_model_list(args.models or [])
    if explicit_models:
        return explicit_models

    if not args.latest_region_failures:
        print("至少传一个 --model，或使用 --latest-region-failures", file=sys.stderr)
        return []

    page = get_bulk_test_report_page(limit=1, page=1)
    items = page.get("items") or []
    if not items:
        writer.emit(
            "selection_error",
            source="latest_bulk_test_region_failures",
            reason="No bulk_test_report rows found",
        )
        return []

    report = items[0]
    stats = report.get("stats") or {}
    categories = stats.get("failure_categories") or []
    target = None
    for item in categories:
        reason = str(item.get("reason") or "")
        if REGION_REASON_NEEDLE in reason.lower():
            target = item
            break

    if not target:
        writer.emit(
            "selection_error",
            source="latest_bulk_test_region_failures",
            run_id=report.get("run_id"),
            reason=f'No failure category matched "{REGION_REASON_NEEDLE}"',
        )
        return []

    all_models = _normalize_model_list(target.get("models") or [])
    selected_models = all_models[: max(0, int(args.limit or 0))] if args.limit else all_models
    writer.emit(
        "selected_models",
        source="latest_bulk_test_region_failures",
        run_id=report.get("run_id"),
        report_started_at=report.get("started_at"),
        report_finished_at=report.get("finished_at"),
        category_reason=target.get("reason"),
        total_models=len(all_models),
        selected_count=len(selected_models),
        selected_models=selected_models,
    )
    return selected_models


async def trace_model(model: str, proxy_override: str, timeout_s: int, writer: EventWriter) -> dict:
    requested_model = main._normalize_model_name(model)
    body = {
        "model": requested_model,
        "messages": [{"role": "user", "content": main.BULK_MODEL_TEST_PROMPT}],
        "stream": False,
        "temperature": 0,
        "max_tokens": main.BULK_MODEL_TEST_MAX_TOKENS,
    }
    default_model = main._normalize_model_name((main.scheduler._config or {}).get("default_model", ""))
    virtual_model, dynamic_route = main._resolve_virtual_model(requested_model, default_model)
    model_attempts = main._build_model_attempts(requested_model, dynamic_route)
    started_at = time.time()

    for upstream_model in model_attempts:
        attempt_virtual_model, candidates = main._select_candidates(virtual_model, upstream_model)
        writer.emit(
            "candidate_pool",
            requested_model=requested_model,
            virtual_model=attempt_virtual_model,
            upstream_model=upstream_model or "",
            **summarize_candidates(candidates),
        )
        if not candidates:
            continue

        for endpoint in candidates:
            provider_cfg = main.scheduler.get_provider_config(endpoint.provider)
            if not provider_cfg:
                writer.emit(
                    "skip_provider_config",
                    requested_model=requested_model,
                    provider=endpoint.provider,
                    endpoint_model=endpoint.model,
                )
                continue

            effective_model = main._effective_upstream_model(endpoint, upstream_model)
            request_proxy = main._proxy_for_requested_model(
                requested_model or effective_model,
                provider_cfg,
                proxy_override=proxy_override,
            )
            client = await main.get_client(request_proxy)
            compressed_body = await main._maybe_compress(body, provider_cfg, effective_model)
            url, headers, payload, effective_model = main._build_upstream_request(
                endpoint,
                compressed_body,
                provider_cfg,
                upstream_model,
            )
            safe_headers = dict(headers)
            auth = safe_headers.get("Authorization", "")
            if auth.startswith("Bearer ") and len(auth) > 15:
                safe_headers["Authorization"] = f"Bearer {auth[7:15]}..."

            writer.emit(
                "upstream_request",
                requested_model=requested_model,
                provider=endpoint.provider,
                endpoint_model=endpoint.model,
                effective_model=effective_model,
                proxy=request_proxy,
                url=url,
                headers=safe_headers,
                body=shorten_text(payload, writer.body_chars),
            )

            t0 = time.monotonic()
            try:
                resp = await asyncio.wait_for(client.post(url, headers=headers, json=payload), timeout=timeout_s)
                latency_ms = int((time.monotonic() - t0) * 1000)
                raw_text = resp.text
                writer.emit(
                    "upstream_response",
                    requested_model=requested_model,
                    provider=endpoint.provider,
                    effective_model=effective_model,
                    proxy=request_proxy,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    body=shorten_text(raw_text, writer.body_chars),
                )

                if resp.status_code == 200:
                    data = resp.json()
                    has_reply, reply_preview = main._extract_bulk_test_reply(data)
                    result = {
                        "model": requested_model,
                        "status": "success" if has_reply else "error",
                        "reason": "" if has_reply else "模型返回为空",
                        "provider": endpoint.provider,
                        "effective_model": effective_model,
                        "response_model": main._normalize_model_name(data.get("model", "")),
                        "reply_preview": reply_preview[:240],
                        "proxy": request_proxy,
                        "latency_ms": latency_ms,
                        "finished_at": time.time(),
                        "started_at": started_at,
                    }
                    writer.emit("result", **result)
                    return result

                try:
                    error_payload = resp.json()
                except Exception:
                    error_payload = raw_text[:writer.body_chars]

                reason = main._extract_error_reason(resp.status_code, error_payload)
                if main._is_model_not_found(resp.status_code, reason):
                    writer.emit(
                        "upstream_model_fallback",
                        requested_model=requested_model,
                        provider=endpoint.provider,
                        effective_model=effective_model,
                        proxy=request_proxy,
                        status_code=resp.status_code,
                        reason=reason,
                    )
                    break

                if main._is_retryable(resp.status_code):
                    writer.emit(
                        "upstream_retryable_error",
                        requested_model=requested_model,
                        provider=endpoint.provider,
                        effective_model=effective_model,
                        proxy=request_proxy,
                        status_code=resp.status_code,
                        reason=reason,
                    )
                    continue

                result = {
                    "model": requested_model,
                    "status": "error",
                    "reason": reason,
                    "provider": endpoint.provider,
                    "effective_model": effective_model,
                    "response_model": "",
                    "reply_preview": "",
                    "proxy": request_proxy,
                    "latency_ms": latency_ms,
                    "finished_at": time.time(),
                    "started_at": started_at,
                }
                writer.emit("result", **result)
                return result
            except asyncio.TimeoutError:
                result = {
                    "model": requested_model,
                    "status": "error",
                    "reason": f"模型测试超时 {timeout_s}s",
                    "provider": endpoint.provider,
                    "effective_model": effective_model,
                    "response_model": "",
                    "reply_preview": "",
                    "proxy": request_proxy,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "finished_at": time.time(),
                    "started_at": started_at,
                }
                writer.emit("result", **result)
                return result
            except HTTPException as exc:
                result = {
                    "model": requested_model,
                    "status": "error",
                    "reason": str(exc.detail or exc.status_code),
                    "provider": endpoint.provider,
                    "effective_model": effective_model,
                    "response_model": "",
                    "reply_preview": "",
                    "proxy": request_proxy,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "finished_at": time.time(),
                    "started_at": started_at,
                }
                writer.emit("result", **result)
                return result
            except Exception as exc:
                result = {
                    "model": requested_model,
                    "status": "error",
                    "reason": str(exc),
                    "provider": endpoint.provider,
                    "effective_model": effective_model,
                    "response_model": "",
                    "reply_preview": "",
                    "proxy": request_proxy,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "finished_at": time.time(),
                    "started_at": started_at,
                }
                writer.emit("result", **result)
                return result

    result = {
        "model": requested_model,
        "status": "error",
        "reason": "All upstreams failed",
        "provider": "",
        "effective_model": "",
        "response_model": "",
        "reply_preview": "",
        "proxy": proxy_override,
        "latency_ms": int((time.time() - started_at) * 1000),
        "finished_at": time.time(),
        "started_at": started_at,
    }
    writer.emit("result", **result)
    return result


async def async_main() -> int:
    args = parse_args()
    proxy_override = ""
    if args.use_region_proxy:
        proxy_override = main._normalize_proxy_url(main.REGION_RETRY_PROXY)
    if args.proxy:
        proxy_override = main._normalize_proxy_url(args.proxy)

    writer = EventWriter(args.jsonl, args.body_chars)
    models = resolve_models(args, writer)
    if not models:
        return 2
    main.scheduler = Scheduler(main.load_config())
    try:
        writer.emit(
            "start",
            models=models,
            proxy_override=proxy_override,
            timeout=args.timeout,
        )
        results = []
        for model in models:
            results.append(await trace_model(model, proxy_override, args.timeout, writer))
        writer.emit("done", results=results)
        return 0
    finally:
        writer.close()
        for client in list(main._clients.values()):
            try:
                await client.aclose()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
