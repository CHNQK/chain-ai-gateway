"""
scheduler.py - Health check, circuit breaker, and weighted routing logic.
"""
import time
import threading
import logging
import random
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("chain-ai-gateway")

DYNAMIC_MODEL_SENTINEL = "*"


def _is_dynamic_model(model: Optional[str]) -> bool:
    return not model or model == DYNAMIC_MODEL_SENTINEL


@dataclass
class UpstreamEndpoint:
    provider: str
    model: str
    priority: int
    weight: int
    # Circuit breaker state
    available: bool = True
    failed_at: Optional[float] = None
    auth_failed_once: bool = False  # 401/403 first strike
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    recovering: bool = False  # True = just recovered, one chance only

    def mark_failed(self, reason: str):
        with self._lock:
            self.available = False
            self.failed_at = time.monotonic()
            self.recovering = False
        logger.warning(f"[CIRCUIT OPEN] {self.key} | Reason: {reason}")

    def try_recover(self, cooldown: float) -> bool:
        with self._lock:
            if not self.available and self.failed_at and (time.monotonic() - self.failed_at) >= cooldown:
                self.available = True
                self.recovering = True
                self.failed_at = None
                logger.info(f"[CIRCUIT HALF-OPEN] {self.key} probing")
                return True
        return False

    def is_available(self, cooldown: float) -> bool:
        if self.available:
            return True
        if self.failed_at is None:
            return False
        self.try_recover(cooldown)
        return self.available


@dataclass
class ModelStats:
    """Runtime stats for a model, updated periodically from DB."""
    model: str
    success_rate: float = 1.0   # 0.0 ~ 1.0
    avg_latency_ms: float = 5000.0
    score: float = 1.0          # computed score, higher = better
    last_updated: float = 0.0


class Scheduler:
    def __init__(self, config: dict):
        self._config = config
        self._cooldown = config.get("circuit_breaker", {}).get("cooldown_seconds", 600)
        self._endpoints: dict[str, list[UpstreamEndpoint]] = {}
        self._rr_index: dict[str, int] = {}  # round-robin cursor per virtual model per model
        self._lock = threading.RLock()
        # model_stats: vmodel -> {model_name -> ModelStats}
        self._model_stats: dict[str, dict[str, ModelStats]] = {}
        self._stats_updated_at: float = 0.0
        self._load_endpoints()
        # Start background stats refresh
        self._stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
        self._stats_thread.start()

    def _load_endpoints(self):
        from db import get_rr_index
        with self._lock:
            self._endpoints = {}
            self._model_stats = {}
            providers = list(self._config.get("providers", {}).keys())
            virtual_models = dict(self._config.get("virtual_models", {}) or {})
            default_vmodel = self._config.get("default_model") or "free"
            if providers and default_vmodel not in virtual_models:
                virtual_models[default_vmodel] = []
            if providers and not virtual_models:
                virtual_models[default_vmodel] = []

            for vmodel, upstreams in virtual_models.items():
                entries = upstreams or [
                    {
                        "provider": provider,
                        "model": DYNAMIC_MODEL_SENTINEL,
                        "priority": 1,
                        "weight": 1,
                    }
                    for provider in providers
                ]
                self._endpoints[vmodel] = [
                    UpstreamEndpoint(
                        provider=u["provider"],
                        model=u.get("model") or DYNAMIC_MODEL_SENTINEL,
                        priority=u.get("priority", 1),
                        weight=u.get("weight", 1),
                    )
                    for u in entries
                ]
                models = set(ep.model for ep in self._endpoints[vmodel]) or {DYNAMIC_MODEL_SENTINEL}
                self._model_stats[vmodel] = {m: ModelStats(model=m) for m in models}
                # Restore RR index from DB (persists across restarts)
                for model in models:
                    rr_key = f"{vmodel}:{model}"
                    if rr_key not in self._rr_index:
                        self._rr_index[rr_key] = get_rr_index(rr_key)
                # Randomize RR start position to avoid always starting from first provider
                for model in models:
                    rr_key = f"{vmodel}:{model}"
                    if rr_key not in self._rr_index:
                        n = sum(1 for ep in self._endpoints[vmodel] if ep.model == model)
                        self._rr_index[rr_key] = random.randint(0, max(n - 1, 0))

    def _stats_loop(self):
        """Background thread: refresh model stats from DB every 60s."""
        while True:
            time.sleep(60)
            try:
                self._refresh_model_stats()
            except Exception as e:
                logger.warning(f"[STATS] Refresh failed: {e}")

    def _refresh_model_stats(self):
        """Read recent request_log and compute per-model success_rate + avg_latency."""
        try:
            import sqlite3
            from db import DB_PATH
            conn = sqlite3.connect(DB_PATH, timeout=5)
            rows = conn.execute("""
                SELECT model,
                       COUNT(*) as total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as ok,
                       AVG(CASE WHEN status='success' AND latency_ms > 0 THEN latency_ms END) as avg_lat
                FROM request_log
                WHERE provider != '' AND ts > strftime('%s','now') - 3600
                GROUP BY model
            """).fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"[STATS] DB read failed: {e}")
            return

        stats_map = {}
        for model, total, ok, avg_lat in rows:
            if total < 3:
                continue
            sr = ok / total if total else 1.0
            lat = avg_lat or 10000.0
            # Score: success_rate weighted heavily, latency as tiebreaker
            # Normalize latency: 1000ms=1.0, 10000ms=0.1
            lat_score = min(1.0, 1000.0 / max(lat, 100))
            score = sr * 0.8 + lat_score * 0.2
            stats_map[model] = ModelStats(
                model=model, success_rate=sr, avg_latency_ms=lat,
                score=score, last_updated=time.monotonic()
            )

        with self._lock:
            for vmodel, ms_dict in self._model_stats.items():
                for model in ms_dict:
                    if model in stats_map:
                        ms_dict[model] = stats_map[model]
        self._stats_updated_at = time.monotonic()
        logger.info(f"[STATS] Refreshed {len(stats_map)} model stats")

    def _pick_model(self, vmodel: str) -> Optional[str]:
        """Pick best model for this vmodel based on scores. Falls back to any available."""
        with self._lock:
            ms_dict = self._model_stats.get(vmodel, {})
            if not ms_dict:
                return None
            if len(ms_dict) == 1:
                return next(iter(ms_dict))
            # Filter models that have available endpoints
            available_models = set(
                ep.model for ep in self._endpoints.get(vmodel, [])
                if ep.is_available(self._cooldown)
            )
            candidates = {m: s for m, s in ms_dict.items() if m in available_models}
            if not candidates:
                return None
            # Weighted random selection by score (gives top model most traffic but not all)
            models = list(candidates.keys())
            scores = [max(candidates[m].score, 0.01) for m in models]
            total = sum(scores)
            r = random.random() * total
            cumulative = 0
            for m, s in zip(models, scores):
                cumulative += s
                if r <= cumulative:
                    return m
            return models[-1]

    def reload(self, new_config: dict):
        """Hot-reload: preserve circuit breaker state for existing endpoints."""
        with self._lock:
            old_states = {
                ep.key: (ep.available, ep.failed_at)
                for eps in self._endpoints.values()
                for ep in eps
            }
            self._config = new_config
            self._cooldown = new_config.get("circuit_breaker", {}).get("cooldown_seconds", 600)
            self._load_endpoints()
            for eps in self._endpoints.values():
                for ep in eps:
                    if ep.key in old_states:
                        ep.available, ep.failed_at = old_states[ep.key]
        logger.info("[CONFIG] Hot-reload complete")

    def _ordered_pool(self, virtual_model: str, pool: list[UpstreamEndpoint], rr_key: str) -> list[UpstreamEndpoint]:
        n = len(pool)
        if n == 0:
            return []
        start = self._rr_index.get(rr_key, 0) % n
        next_idx = (start + 1) % n
        self._rr_index[rr_key] = next_idx
        from db import save_rr_index
        save_rr_index(rr_key, next_idx)
        return [pool[(start + i) % n] for i in range(n)]

    def _dedupe_by_provider(self, endpoints: list[UpstreamEndpoint]) -> list[UpstreamEndpoint]:
        seen = set()
        unique = []
        for ep in endpoints:
            if ep.provider in seen:
                continue
            seen.add(ep.provider)
            unique.append(ep)
        return unique

    def get_candidates(self, virtual_model: str, preferred_model: str = "", dynamic_mode: bool = False) -> list[UpstreamEndpoint]:
        """Pick endpoints for this virtual model.

        Legacy mode keeps the historical behavior of choosing a configured model first.
        Dynamic mode ignores fixed upstream models and uses the provider pool directly,
        letting the downstream request decide the actual upstream model.
        """
        with self._lock:
            endpoints = self._endpoints.get(virtual_model, [])
            if not endpoints:
                return []

        if dynamic_mode:
            with self._lock:
                exact = [ep for ep in endpoints if ep.model == preferred_model]
                dynamic = [ep for ep in endpoints if _is_dynamic_model(ep.model)]
                pool = exact or dynamic or endpoints
                ordered = self._ordered_pool(
                    virtual_model,
                    self._dedupe_by_provider(pool),
                    f"{virtual_model}:{DYNAMIC_MODEL_SENTINEL}",
                )
            available = [ep for ep in ordered if ep.is_available(self._cooldown)]
            if not available and pool is not endpoints:
                with self._lock:
                    fallback_pool = self._dedupe_by_provider(endpoints)
                    ordered = self._ordered_pool(
                        virtual_model,
                        fallback_pool,
                        f"{virtual_model}:{DYNAMIC_MODEL_SENTINEL}",
                    )
                available = [ep for ep in ordered if ep.is_available(self._cooldown)]
            return available

        # Pick model based on stats
        chosen_model = self._pick_model(virtual_model)

        with self._lock:
            # Filter endpoints by chosen model
            if _is_dynamic_model(chosen_model):
                pool = self._dedupe_by_provider(
                    [ep for ep in endpoints if _is_dynamic_model(ep.model)] or endpoints
                )
            else:
                pool = [ep for ep in endpoints if ep.model == chosen_model] if chosen_model else endpoints
            if not pool:
                pool = endpoints  # fallback to all

            rr_model = chosen_model or DYNAMIC_MODEL_SENTINEL
            ordered = self._ordered_pool(virtual_model, pool, f"{virtual_model}:{rr_model}")

        available = [ep for ep in ordered if ep.is_available(self._cooldown)]
        if not available and chosen_model:
            # Chosen model all down, fallback to any available endpoint
            with self._lock:
                all_eps = self._endpoints.get(virtual_model, [])
                if _is_dynamic_model(chosen_model):
                    all_eps = self._dedupe_by_provider(all_eps)
            available = [ep for ep in all_eps if ep.is_available(self._cooldown)]
            if available:
                logger.info(f"[FALLBACK] {chosen_model} all down, falling back to other models")
        return available

    def mark_failed(self, endpoint: UpstreamEndpoint, reason: str):
        endpoint.mark_failed(reason)

    def get_provider_config(self, provider: str) -> dict:
        return self._config.get("providers", {}).get(provider, {})

    def list_virtual_models(self) -> list[str]:
        return list(self._endpoints.keys())

    def get_model_stats(self, vmodel: str) -> list[dict]:
        """Return model stats for admin UI."""
        with self._lock:
            ms_dict = self._model_stats.get(vmodel, {})
            endpoints = self._endpoints.get(vmodel, [])
        result = []
        for model, ms in ms_dict.items():
            total = sum(1 for ep in endpoints if ep.model == model)
            avail = sum(1 for ep in endpoints if ep.model == model and ep.is_available(self._cooldown))
            result.append({
                "model": model,
                "success_rate": round(ms.success_rate * 100, 1),
                "avg_latency_ms": int(ms.avg_latency_ms),
                "score": round(ms.score, 3),
                "providers_total": total,
                "providers_available": avail,
                "last_updated": ms.last_updated,
            })
        result.sort(key=lambda x: -x["score"])
        return result
