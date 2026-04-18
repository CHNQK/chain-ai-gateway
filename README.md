# Chain-AI-Gateway

OpenAI-compatible gateway for multi-key upstream routing, 429-only failover, upstream model catalog sync, bulk model testing, and a lightweight admin UI.

## What it does

- Downstream decides the model name
- Upstream uses the same model when available
- If the model does not exist upstream, route to `openrouter/free`
- Only `429` triggers API-key failover
- `/v1/models` syncs from upstream model catalogs
- Admin UI includes provider status, request logs, upstream model testing, and test reports

## Quick start

1. Copy `config.example.yaml` to `config.yaml`
2. Fill in your upstream API keys
3. Create a virtualenv and install dependencies
4. Start the gateway with `python main.py`

```bash
cp config.example.yaml config.yaml
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Important files

- `main.py`: gateway server and admin endpoints
- `scheduler.py`: upstream selection and health state
- `db.py`: SQLite persistence
- `static/index.html`: admin UI
- `DEPLOYMENT_GUIDE.md`: deployment notes

## Security

Do not commit real `config.yaml`, databases, logs, or API keys. This repository is set up to keep those files out of Git by default.
