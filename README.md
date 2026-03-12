# Terminals

> [!NOTE]
> This project is in **alpha**. APIs, configuration, and behavior may change between releases.

Multi-tenant terminal orchestrator for [Open Terminal](https://github.com/open-webui/open-terminal). Provisions isolated, policy-configured terminal instances per user.

## Quick Start

```bash
pip install -e .
terminals serve
```

Or with Docker:

```bash
docker run -p 3000:3000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/data:/app/data \
  terminals
```

## Policies

Policies define per-environment configuration. Manage via REST API:

```bash
curl -X PUT http://localhost:3000/api/v1/policies/data-science \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "ghcr.io/open-webui/open-terminal:python-ds",
    "cpu_limit": "2",
    "memory_limit": "4Gi",
    "env": {"OPENAI_API_KEY": "sk-proj-..."},
    "allowed_domains": ["*.pypi.org", "github.com"],
    "idle_timeout_minutes": 30
  }'
```

Route requests through a policy via `/p/{policy_id}/`:

```bash
curl -X POST http://localhost:3000/p/data-science/execute \
  -H "Authorization: Bearer $API_KEY" -H "X-User-Id: user-123" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello"}'
```

| Field | Type | Description |
|-------|------|-------------|
| `image` | string | Container image |
| `env` | dict | Environment variables |
| `cpu_limit` | string | Max CPU (e.g. `"2"`) |
| `memory_limit` | string | Max memory (e.g. `"4Gi"`) |
| `storage` | string | Persistent volume size (absent = ephemeral) |
| `allowed_domains` | list | `["*"]` = full, `[]` = none, `["*.pypi.org"]` = restricted |
| `idle_timeout_minutes` | int | Idle timeout before cleanup |

## Configuration

Environment variables prefixed with `TERMINALS_` (or `.env` file).

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_BACKEND` | `docker` | `docker`, `kubernetes`, `kubernetes-operator` |
| `TERMINALS_API_KEY` | *(auto)* | Bearer token for API auth |
| `TERMINALS_OPEN_WEBUI_URL` | | Open WebUI URL for JWT auth |
| `TERMINALS_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Default container image |
| `TERMINALS_MAX_CPU` | | Hard cap on CPU |
| `TERMINALS_MAX_MEMORY` | | Hard cap on memory |
| `TERMINALS_MAX_STORAGE` | | Hard cap on storage |
| `TERMINALS_ALLOWED_IMAGES` | | Comma-separated image globs |

See [`config.py`](terminals/config.py) for the full list.

## Authentication

| Mode | Trigger |
|------|---------|
| **Open WebUI JWT** | Set `TERMINALS_OPEN_WEBUI_URL` |
| **API Key** | Set `TERMINALS_API_KEY` |
| **Open** | Neither set (dev only) |

## Backends

- **`docker`** – One container per user via Docker socket
- **`kubernetes`** – Pod + PVC + Service per user
- **`kubernetes-operator`** – Kopf operator watching `Terminal` CRDs

## License

[Open WebUI Enterprise License](LICENSE)
