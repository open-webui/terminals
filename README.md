# Terminals

> [!NOTE]
> This project is **actively under development**. APIs, configuration, and behavior may change between releases.

Per-user [Open Terminal](https://github.com/open-webui/open-terminal) orchestration for Docker and Kubernetes.

Terminals gives every Open WebUI user their own isolated container, with separate credentials, resource limits, and network rules. It handles the full lifecycle automatically: spinning up containers when a user connects, proxying traffic, enforcing limits, and cleaning up when they're done.

```
Open WebUI  →  Terminals service  →  per-user containers
               (this project)        (Open Terminal images)
```

> [!IMPORTANT]
> **Production use requires an [Open WebUI Enterprise License](LICENSE) with Terminals access.** Contact the Open WebUI team to get started.

## Quick Start

The fastest way to get running is with Docker. Terminals will manage sibling containers through the Docker socket.

### Docker (recommended for single-node)

```bash
docker run -p 3000:3000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/data:/app/data \
  terminals
```

**Prerequisites:** Docker running on the host.

### Kubernetes Operator (recommended for clusters)

For Kubernetes deployments, the operator manages `Terminal` custom resources automatically, handling pod creation, storage, and cleanup through CRDs.

```bash
# Install the CRD and operator
kubectl apply -f manifests/terminal-crd.yaml
kubectl apply -f manifests/operator-deployment.yaml
```

Set `TERMINALS_BACKEND=kubernetes-operator` when deploying the Terminals service.

For OpenShift, use restricted mode and an OpenShift-compatible Open Terminal image. See [OpenShift deployment](docs/openshift.md).

### From source (development)

```bash
pip install -e .
terminals serve
```

## Choosing a Backend

| Backend | Best for | How it works |
|---------|----------|-------------|
| `docker` | Single-node, local dev | One container per user via Docker socket |
| `kubernetes-operator` | Production K8s clusters | Operator watches `Terminal` CRDs for automated lifecycle |
| `kubernetes` | K8s without CRDs | Direct Pod + PVC + Service per user (you manage resources) |

Set the backend with `TERMINALS_BACKEND` (defaults to `docker`).

## Policies

Policies let you define different environments, for example, a "data-science" environment with extra CPU and specific Python packages, or a "sandbox" environment with restricted network access.

Without any policies, Terminals uses the defaults from your configuration. Once you're ready to customize, manage policies through the REST API:

```bash
# Create a "data-science" policy
curl -X PUT http://localhost:3000/api/v1/policies/data-science \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "ghcr.io/open-webui/open-terminal:python-ds",
    "cpu_limit": "2",
    "memory_limit": "4Gi",
    "env": {
      "OPENAI_API_KEY": "sk-proj-...",
      "OPEN_TERMINAL_ALLOWED_DOMAINS": "*.pypi.org,github.com"
    },
    "idle_timeout_minutes": 30
  }'
```

Route requests through a policy by adding `/p/{policy_id}/` to the URL:

```bash
curl -X POST http://localhost:3000/p/data-science/execute \
  -H "Authorization: Bearer $API_KEY" -H "X-User-Id: user-123" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello"}'
```

### Policy fields

| Field | Type | Description |
|-------|------|-------------|
| `image` | string | Container image to use |
| `env` | dict | Environment variables passed to the container |
| `cpu_limit` | string | Max CPU (e.g. `"2"`) |
| `memory_limit` | string | Max memory (e.g. `"4Gi"`) |
| `storage` | string | Persistent volume size (omit for ephemeral storage) |
| `storage_mode` | string | `per-user`, `shared`, or `shared-rwo` |
| `idle_timeout_minutes` | int | Minutes of inactivity before the container is cleaned up |
| `restricted` | bool | Enable restricted Kubernetes/OpenShift pod defaults for this policy |
| `pod_security_context` | dict | Pod security context override for Kubernetes backends |
| `container_security_context` | dict | Container security context override for Kubernetes backends |
> [!NOTE]
> **Storage limits are fully enforced only on the Kubernetes backends** (via sized PVCs). On the `docker` backend, `storage` (and `TERMINALS_MAX_STORAGE`) caps the container's *writable layer* via Docker's `StorageOpt`, which requires a storage driver that supports it (e.g. overlay2 on XFS with the `pquota` mount option). On unsupported drivers such as overlay2-on-ext4, Terminals logs a warning and provisions without the limit. The persistent `/home/user` directory is bind-mounted from the host and is **not** quota-limited on Docker. Use a Kubernetes backend if you need hard per-user storage caps.

### Policy lifecycle

Policies define what gets provisioned. Policy lifecycle config defines ongoing maintenance for that policy, such as scheduled resets of persisted terminal files.

```bash
curl -X PUT http://localhost:3000/api/v1/policies/data-science/lifecycle \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "reset": {
      "schedule": "@weekly",
      "timezone": "UTC"
    }
  }'
```

Reset schedules support one-time ISO datetimes, `@weekly`, `@monthly`, and 5-field cron expressions.

### Applying policy changes

Policy updates apply to newly provisioned terminals. To stop matching terminals
so the next access starts with the current image, env, and resource settings:

```bash
curl -X POST http://localhost:3000/api/v1/terminals/refresh \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"policy_id":"data-science","only_idle":true}'
```

Use `user_id` to target one user, `policy_id` to target one policy, and
`reset:true` to wipe the matched users' persisted files while
refreshing. `only_idle` defaults to `true` so active users are not
interrupted.

## Configuration

All settings are configured through environment variables prefixed with `TERMINALS_`, or via a `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_BACKEND` | `docker` | `docker`, `kubernetes`, or `kubernetes-operator` |
| `TERMINALS_API_KEY` | *(auto-generated)* | Bearer token for API auth |
| `TERMINALS_ENABLE_UI` | `true` | Serve the built-in minimal admin UI at `/`. Set to `false` for API-only deployments. |
| `TERMINALS_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Default container image |
| `TERMINALS_MAX_CPU` | | Hard cap on CPU per container |
| `TERMINALS_MAX_MEMORY` | | Hard cap on memory per container |
| `TERMINALS_MAX_STORAGE` | | Hard cap on storage per container |
| `TERMINALS_ALLOWED_IMAGES` | | Comma-separated list of allowed image patterns |
| `TERMINALS_KUBERNETES_STORAGE_MODE` | `per-user` | `per-user`, `shared`, or `shared-rwo` |
| `TERMINALS_KUBERNETES_RESTRICTED` | `false` | Enable restricted Kubernetes/OpenShift pod defaults globally |
| `TERMINALS_KUBERNETES_POD_SECURITY_CONTEXT` | | JSON pod security context merged into Kubernetes terminal pods |
| `TERMINALS_KUBERNETES_CONTAINER_SECURITY_CONTEXT` | | JSON container security context merged into Kubernetes terminal containers |
| `TERMINALS_DATABASE_URL` | `sqlite+aiosqlite:///.../data/terminals.db` | SQLAlchemy database URL. SQLite is the default; PostgreSQL is optional. |
| `TERMINALS_LOG_LEVEL` | `INFO` | Minimum log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

See [`config.py`](terminals/config.py) for the full list.

## Authentication

| Mode | How to enable |
|------|---------------|
| **API Key** | Set `TERMINALS_API_KEY` to a static token |
| **Open (dev only)** | Leave unset, no auth, for local development only |

## License

[Open WebUI Enterprise License](LICENSE)
