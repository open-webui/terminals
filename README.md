# Terminals

> [!NOTE]
> This project is **actively under development**. APIs, configuration, and behavior may change between releases.

Per-user [Open Terminal](https://github.com/open-webui/open-terminal) orchestration for Docker and Kubernetes.

Giving terminal access to users on Open WebUI requires per-user isolation: separate containers, each with their own credentials and resource constraints. Terminals handles the full lifecycle: provisioning containers on demand, proxying traffic per user, enforcing resource and network policies, validating Open WebUI JWTs natively, and cleaning up idle instances.

> [!IMPORTANT]
> **Production use requires an [Open WebUI Enterprise License](LICENSE) that includes Terminals.** Contact the Open WebUI team to verify your plan covers it.

| Capability | |
|---|---|
| **Backends** | Docker, Kubernetes, K8s Operator |
| **Provisioning** | On-demand per user, transparent to the client |
| **Policies** | Per-environment image, CPU, memory, network, env vars via REST API |
| **Auth** | Open WebUI JWT validation or static API key |
| **Hard caps** | Admin-enforced limits on CPU, memory, storage, and allowed images |
| **Multi-environment** | Named policies with routing via `/p/{policy_id}/` |
| **Network control** | Egress filtering via `env.OPEN_TERMINAL_ALLOWED_DOMAINS` |
| **Idle cleanup** | Automatic teardown of inactive instances |
| **Runtime changes** | Update policies via API without redeployment |

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
    "env": {"OPENAI_API_KEY": "sk-proj-...", "OPEN_TERMINAL_ALLOWED_DOMAINS": "*.pypi.org,github.com"},
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
| `storage_mode` | string | `per-user`, `shared`, `shared-rwo` (absent = global default) |
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
| `TERMINALS_KUBERNETES_STORAGE_MODE` | `per-user` | `per-user`, `shared`, `shared-rwo` |

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

[Open WebUI Enterprise License](LICENSE) — production use requires an enterprise plan that includes Terminals.
