# Changelog

All notable changes to this project will be documented in this file.

## [v0.1.0] - 2026-04-18

### Added
- OpenAI-compatible gateway endpoints for `/v1/chat/completions` and `/v1/models`.
- Automatic upstream model sync so downstream clients can query the current upstream model catalog.
- Bulk upstream model testing from the downstream path with 180s timeout, progress tracking, daily 03:00 scheduled runs, and per-run reports.
- Admin UI for overview, provider real-time status, logs, upstream model catalog, available model list, and chat playground.
- LLM-assisted bulk test summaries with fallback programmatic summaries when the summary model returns empty output.

### Changed
- Downstream model names now directly drive upstream routing; the gateway no longer pins a fixed upstream model per channel.
- When a requested upstream model does not exist, the gateway now falls back to `openrouter/free` instead of appending `:free` to the original model name.
- Failover is restricted to `429` responses only, and transfers across different upstream API keys/providers rather than retrying the same logical key pool.
- Bulk testing reuses the downstream forwarding path while isolating test traffic from production logs and runtime circuit state.
- Homepage KPI cards now support continuous animated deltas, including request volume, token counters, success/error counters, and live `tok/s`.

### Fixed
- Preserved structured tool call streaming instead of degrading tool calls into `content` strings.
- Corrected copy actions in the available-model list for HTTP and non-clipboard-safe contexts.
- Cleaned up stale bulk-test running state so expired jobs no longer remain stuck in `running`.

### Notes
- Sensitive local files such as `config.yaml`, `gateway.db`, logs, and API keys are excluded from the public repository.
- Use `config.example.yaml` as the deployment template.
