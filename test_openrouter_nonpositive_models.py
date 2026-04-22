import argparse
import base64
import json
import time
from collections import Counter
from pathlib import Path

import httpx
import yaml


MODELS_API_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
VIDEO_MODELS_API_URL = "https://openrouter.ai/api/v1/videos/models"
DEFAULT_JSONL = "/tmp/openrouter-nonpositive-model-test.jsonl"
DEFAULT_REPORT = "/tmp/openrouter-nonpositive-model-test-report.json"
DEFAULT_TIMEOUT = 45.0
DEFAULT_MAX_PROVIDERS = 8
REGION_RETRY_PROXY = "http://pm.tsss.com.cn:7911"
REGION_HINT = "this model is not available in your region"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Directly test current non-positive-priced OpenRouter models with upstream API calls."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--jsonl", default=DEFAULT_JSONL)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-providers", type=int, default=DEFAULT_MAX_PROVIDERS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--provider-prefix", default="openrouter_")
    return parser.parse_args()


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_openrouter_providers(cfg: dict, prefix: str) -> list[dict]:
    providers = []
    for name, item in sorted((cfg.get("providers") or {}).items()):
        if prefix and not str(name).startswith(prefix):
            continue
        base_url = str((item or {}).get("base_url") or "").rstrip("/")
        api_key = str((item or {}).get("api_key") or "")
        if not api_key or "openrouter.ai" not in base_url:
            continue
        providers.append(
            {
                "name": name,
                "api_key": api_key,
                "base_url": base_url,
                "proxy": str((item or {}).get("proxy") or "").strip(),
            }
        )
    return providers


def fetch_json(url: str, timeout: float = 30.0) -> dict:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.get(url).json()


def redact_headers(headers: dict) -> dict:
    safe = dict(headers)
    auth = safe.get("Authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 18:
        safe["Authorization"] = f"Bearer {auth[7:15]}..."
    return safe


def build_nonpositive_models() -> list[dict]:
    payload = fetch_json(MODELS_API_URL)
    items = []
    for model in payload.get("data") or []:
        pricing = model.get("pricing") or {}
        numeric = []
        has_positive = False
        has_negative = False
        for _, value in pricing.items():
            try:
                number = float(value)
            except Exception:
                continue
            numeric.append(number)
            if number > 0:
                has_positive = True
            if number < 0:
                has_negative = True
        if not numeric or has_positive:
            continue
        arch = model.get("architecture") or {}
        items.append(
            {
                "id": model["id"],
                "name": model.get("name", ""),
                "pricing": pricing,
                "context_length": model.get("context_length"),
                "input_modalities": arch.get("input_modalities") or [],
                "output_modalities": arch.get("output_modalities") or [],
                "modality": arch.get("modality") or "",
                "has_negative_price": has_negative,
            }
        )
    items.sort(key=lambda item: item["id"])
    return items


def build_video_capabilities() -> dict[str, dict]:
    payload = fetch_json(VIDEO_MODELS_API_URL)
    capabilities = {}
    for item in payload.get("data") or []:
        capabilities[item.get("id") or item.get("canonical_slug")] = item
    return capabilities


def modality_key(model: dict) -> str:
    outputs = model.get("output_modalities") or []
    key = ",".join(outputs)
    if key == "text":
        return "text"
    if key == "image":
        return "image"
    if key == "text,image":
        return "text,image"
    if key == "text,audio":
        return "text,audio"
    if key == "rerank":
        return "rerank"
    if key == "embeddings":
        return "embeddings"
    if key == "video":
        return "video"
    return key or "(unknown)"


def region_restricted(reason: str) -> bool:
    return REGION_HINT in (reason or "").lower()


def reason_from_response(resp: httpx.Response, body_text: str) -> str:
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("metadata", {}).get("raw")
            if message:
                return f"HTTP {resp.status_code}: {message}"
        detail = payload.get("detail")
        if detail:
            return f"HTTP {resp.status_code}: {detail}"
    return f"HTTP {resp.status_code}: {body_text[:240]}"


def choose_video_params(cap: dict) -> dict:
    resolutions = cap.get("supported_resolutions") or []
    aspects = cap.get("supported_aspect_ratios") or []
    sizes = cap.get("supported_sizes") or []
    payload = {
        "prompt": "A very short clip of a red ball bouncing on a white floor.",
        "duration": 5,
    }
    if "480p" in resolutions:
        payload["resolution"] = "480p"
    elif resolutions:
        payload["resolution"] = resolutions[0]
    if "1:1" in aspects:
        payload["aspect_ratio"] = "1:1"
    elif aspects:
        payload["aspect_ratio"] = aspects[0]
    elif sizes:
        payload["size"] = sizes[0]
    return payload


def build_request(model: dict, video_caps: dict) -> tuple[str, dict, bool]:
    model_id = model["id"]
    key = modality_key(model)
    if key == "text":
        return (
            "/chat/completions",
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply exactly with MODEL_TEST_OK"}],
                "max_tokens": 16,
                "temperature": 0,
                "stream": False,
            },
            False,
        )
    if key == "image":
        return (
            "/chat/completions",
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "Generate a tiny red square image."}],
                "modalities": ["image"],
                "stream": False,
            },
            False,
        )
    if key == "text,image":
        return (
            "/chat/completions",
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "Generate a tiny red square image and briefly say ok."}],
                "modalities": ["image", "text"],
                "stream": False,
            },
            False,
        )
    if key == "text,audio":
        return (
            "/chat/completions",
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "Say hello in a friendly tone."}],
                "modalities": ["text", "audio"],
                "audio": {"voice": "alloy", "format": "wav"},
                "stream": True,
            },
            True,
        )
    if key == "rerank":
        return (
            "/rerank",
            {
                "model": model_id,
                "query": "Which document mentions Paris as the capital of France?",
                "documents": [
                    "Paris is the capital of France.",
                    "Berlin is the capital of Germany.",
                    "Tokyo is the capital of Japan.",
                ],
                "top_n": 2,
            },
            False,
        )
    if key == "embeddings":
        return (
            "/embeddings",
            {
                "model": model_id,
                "input": "The quick brown fox jumps over the lazy dog.",
            },
            False,
        )
    if key == "video":
        caps = video_caps.get(model_id) or {}
        payload = {"model": model_id}
        payload.update(choose_video_params(caps))
        return ("/videos", payload, False)
    raise ValueError(f"Unsupported output modalities for {model_id}: {model.get('output_modalities')}")


def assess_success(model: dict, path: str, status_code: int, payload: dict | None) -> tuple[bool, str]:
    key = modality_key(model)
    if key == "video":
        if status_code in (200, 202) and isinstance(payload, dict) and payload.get("id"):
            return True, payload.get("status") or "accepted"
        return False, "Video job not accepted"
    if key == "rerank":
        if status_code == 200 and isinstance(payload, dict) and payload.get("results"):
            return True, f"rerank_results={len(payload.get('results') or [])}"
        return False, "No rerank results"
    if key == "embeddings":
        if status_code == 200 and isinstance(payload, dict) and (payload.get("data") or [{}])[0].get("embedding"):
            return True, f"embedding_dim={len(payload['data'][0]['embedding'])}"
        return False, "No embedding vector"
    if key == "image":
        choices = (payload or {}).get("choices") or []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        if status_code == 200 and message.get("images"):
            return True, f"images={len(message.get('images') or [])}"
        return False, "No image output"
    if key == "text,image":
        choices = (payload or {}).get("choices") or []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        if status_code == 200 and (message.get("images") or message.get("content")):
            detail = []
            if message.get("images"):
                detail.append(f"images={len(message.get('images') or [])}")
            if message.get("content"):
                detail.append("text=yes")
            return True, ",".join(detail) or "ok"
        return False, "No image/text output"
    choices = (payload or {}).get("choices") or []
    message = ((choices[0] or {}).get("message") or {}) if choices else {}
    content = message.get("content")
    if status_code == 200 and content:
        return True, str(content)[:80]
    return False, "No text content"


def stream_audio_request(client: httpx.Client, url: str, headers: dict, body: dict, timeout: float) -> tuple[int, str, dict | None, str]:
    audio_chunks = []
    transcript = []
    last_status = 0
    with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
        last_status = resp.status_code
        if resp.status_code != 200:
            text = resp.read().decode("utf-8", "ignore")
            return resp.status_code, text, None, ""
        for line in resp.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            if not line.startswith("data: "):
                continue
            chunk = line[6:].strip()
            if chunk == "[DONE]":
                break
            try:
                payload = json.loads(chunk)
            except Exception:
                continue
            delta = (((payload.get("choices") or [{}])[0]).get("delta") or {})
            audio = delta.get("audio") or {}
            if audio.get("data"):
                audio_chunks.append(audio["data"])
            if audio.get("transcript"):
                transcript.append(audio["transcript"])
            if audio_chunks or transcript:
                return 200, json.dumps({"transcript": "".join(transcript)}), {"transcript": "".join(transcript)}, "audio_chunk"
        return 200, json.dumps({"transcript": "".join(transcript)}), {"transcript": "".join(transcript)}, "stream_completed"


def test_once(model: dict, provider: dict, timeout: float, video_caps: dict, proxy_override: str = "") -> dict:
    path, body, streaming = build_request(model, video_caps)
    base_url = provider["base_url"]
    proxy = proxy_override or provider.get("proxy") or ""
    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}{path}"
    started = time.time()
    event = {
        "model": model["id"],
        "provider": provider["name"],
        "path": path,
        "proxy": proxy,
        "request_body": body,
        "started_at": started,
    }
    try:
        with httpx.Client(proxy=proxy or None, timeout=timeout, follow_redirects=True) as client:
            if streaming:
                status_code, raw_text, payload, success_detail = stream_audio_request(client, url, headers, body, timeout)
                latency_ms = int((time.time() - started) * 1000)
                ok = status_code == 200 and success_detail in {"audio_chunk", "stream_completed"}
                reason = "" if ok else reason_from_response(httpx.Response(status_code=status_code), raw_text)
                return {
                    **event,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                    "ok": ok,
                    "detail": success_detail,
                    "reason": reason,
                    "response_preview": raw_text[:240],
                }

            resp = client.post(url, headers=headers, json=body)
            latency_ms = int((time.time() - started) * 1000)
            raw_text = resp.text
            try:
                payload = resp.json()
            except Exception:
                payload = None
            ok, detail = assess_success(model, path, resp.status_code, payload)
            reason = "" if ok else reason_from_response(resp, raw_text)
            return {
                **event,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "ok": ok,
                "detail": detail,
                "reason": reason,
                "response_preview": raw_text[:240],
            }
    except Exception as exc:
        return {
            **event,
            "status_code": 0,
            "latency_ms": int((time.time() - started) * 1000),
            "ok": False,
            "detail": "",
            "reason": str(exc),
            "response_preview": "",
        }


def provider_retryable(result: dict) -> bool:
    code = int(result.get("status_code") or 0)
    reason = str(result.get("reason") or "").lower()
    if code in {401, 402, 408, 409, 429, 500, 502, 503, 504}:
        return True
    if code == 0:
        return True
    if "server disconnected" in reason or "timed out" in reason or "connection" in reason:
        return True
    return False


def test_model(model: dict, providers: list[dict], timeout: float, max_providers: int, video_caps: dict, writer) -> dict:
    attempts = []
    limited = providers[: max(1, max_providers)]
    for provider in limited:
        result = test_once(model, provider, timeout, video_caps)
        attempts.append(result)
        writer.write(json.dumps({"event": "attempt", **sanitize_attempt(result)}, ensure_ascii=False) + "\n")
        writer.flush()
        if result["ok"]:
            return finalize_result(model, attempts, "success", result)
        if region_restricted(result.get("reason", "")):
            proxy_result = test_once(model, provider, timeout, video_caps, proxy_override=REGION_RETRY_PROXY)
            attempts.append(proxy_result)
            writer.write(json.dumps({"event": "attempt", **sanitize_attempt(proxy_result)}, ensure_ascii=False) + "\n")
            writer.flush()
            if proxy_result["ok"]:
                return finalize_result(model, attempts, "success", proxy_result)
            return finalize_result(model, attempts, "error", proxy_result)
        if not provider_retryable(result):
            return finalize_result(model, attempts, "error", result)
    return finalize_result(model, attempts, "error", attempts[-1] if attempts else {})


def sanitize_attempt(result: dict) -> dict:
    item = dict(result)
    body = dict(item.get("request_body") or {})
    item["request_body"] = body
    return item


def finalize_result(model: dict, attempts: list[dict], status: str, terminal: dict) -> dict:
    return {
        "model": model["id"],
        "name": model.get("name", ""),
        "output_modalities": model.get("output_modalities") or [],
        "context_length": model.get("context_length"),
        "pricing": model.get("pricing") or {},
        "has_negative_price": bool(model.get("has_negative_price")),
        "status": status,
        "final_reason": terminal.get("reason", ""),
        "detail": terminal.get("detail", ""),
        "provider": terminal.get("provider", ""),
        "proxy": terminal.get("proxy", ""),
        "status_code": terminal.get("status_code", 0),
        "latency_ms": terminal.get("latency_ms", 0),
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def main():
    args = parse_args()
    cfg = load_config(args.config)
    providers = load_openrouter_providers(cfg, args.provider_prefix)
    if not providers:
        raise SystemExit("No OpenRouter providers found in config.")
    models = build_nonpositive_models()
    if args.limit:
        models = models[: max(1, args.limit)]
    video_caps = build_video_capabilities()

    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.time()
    with jsonl_path.open("w", encoding="utf-8") as writer:
        writer.write(json.dumps({"event": "start", "model_count": len(models), "provider_count": len(providers)}, ensure_ascii=False) + "\n")
        writer.flush()
        for index, model in enumerate(models, start=1):
            result = test_model(model, providers, args.timeout, args.max_providers, video_caps, writer)
            result["index"] = index
            result["finished_at"] = time.time()
            results.append(result)
            writer.write(json.dumps({"event": "result", **result}, ensure_ascii=False) + "\n")
            writer.flush()

    status_counts = Counter(item["status"] for item in results)
    reason_counts = Counter(item["final_reason"] for item in results if item.get("final_reason"))
    modality_counts = Counter(modality_key(item) for item in models)
    success_by_modality = Counter(modality_key(item) for item in results if item["status"] == "success")
    report = {
        "started_at": started,
        "finished_at": time.time(),
        "elapsed_seconds": round(time.time() - started, 2),
        "provider_count": len(providers),
        "tested_models": len(results),
        "status_counts": dict(status_counts),
        "modality_counts": dict(modality_counts),
        "success_by_modality": dict(success_by_modality),
        "top_failure_reasons": reason_counts.most_common(20),
        "results": results,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "tested_models": report["tested_models"],
        "status_counts": report["status_counts"],
        "success_by_modality": report["success_by_modality"],
        "top_failure_reasons": report["top_failure_reasons"][:10],
        "jsonl": str(jsonl_path),
        "report": str(report_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
